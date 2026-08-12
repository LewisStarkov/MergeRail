"""``agentq`` — the whole thing, from a directory that happens to be a repository."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from . import checks, log
from .audit import AuditLog
from .backends import BackendInfo, BackendRegistry
from .backends.policy import strict_security_gaps
from .config import CONFIG_NAME, Config, ensure_state_ignored, write_config
from .delivery import LOCAL, can_open_pr, resolve_mode
from .gitctl import current_branch, has_commits, is_repo, repo_root
from .runner import Runner
from .share import NgrokTunnel, Share
from .tasks import QueueCorruptError, Status, TaskStore

USAGE = """\
  agentq                     work the queue, tasks arrive as files in .agentq/inbox
  agentq --telegram          work the queue, tasks arrive from a Telegram bot
  agentq --web               work the queue, tasks arrive from a page on localhost
  agentq add "fix the …"     put one task on the queue and exit
  agentq once "fix the …"    put one task on the queue, work until it settles, exit
  agentq list                show the queue
  agentq init                write agentq.toml with what this repository looks like
  agentq doctor              say whether this repository is ready, and what would run
"""

COMMANDS = (
    "run",
    "init",
    "doctor",
    "add",
    "list",
    "show",
    "retry-task",
    "retry-delivery",
    "cancel",
    "archive",
    "backends",
    "events",
    "once",
)


def _add_agent_flags(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--backend", "--agent", dest="backend", default="", help="backend for both agents"
    )
    command.add_argument(
        "--fixer-backend", "--fixer-agent", dest="fixer_backend", default=""
    )
    command.add_argument(
        "--reviewer-backend", "--reviewer-agent", dest="reviewer_backend", default=""
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--path", default=".", help="repository to work in")
    common.add_argument("--log-level", default="INFO")

    parser = argparse.ArgumentParser(
        prog="agentq",
        description="Write the task down; an agent does it, a reviewer checks it, it lands.",
        epilog=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command")

    run = commands.add_parser("run", parents=[common], help="work the queue (the default)")
    run.add_argument(
        "--front", default="", help="folder (default), telegram, web, or pkg.mod:Class"
    )
    run.add_argument("--telegram", action="store_true", help="shorthand for --front telegram")
    run.add_argument("--web", action="store_true", help="shorthand for --front web")
    run.add_argument(
        "--share",
        choices=["ngrok"],
        help="publish the web front temporarily (currently: ngrok)",
    )
    run.add_argument(
        "--share-policy",
        default="",
        metavar="FILE",
        help="ngrok Traffic Policy file; default is generated Basic Auth",
    )
    run.add_argument(
        "--share-unsafe",
        action="store_true",
        help="publish without authentication (not recommended)",
    )
    run.add_argument("--once", action="store_true", help="handle one task, then exit")
    run.add_argument("--delivery", default="", choices=["", "auto", "local", "merge", "pr"])
    _add_agent_flags(run)
    run.add_argument("--model", default="", help="model for both agents")
    run.add_argument("--no-process", action="store_true", help="do not run the configured app")
    run.add_argument(
        "--dangerous",
        action="store_true",
        help="request each backend's unrestricted permission mode",
    )
    run.add_argument(
        "--strict-security",
        action="store_true",
        help="require native reviewer isolation and push denial",
    )
    run.add_argument(
        "--unsafe-expose",
        action="store_true",
        help="allow a non-local web bind without authentication",
    )

    initialize = commands.add_parser(
        "init", parents=[common], help="ask about the project and write agentq.toml"
    )
    _add_agent_flags(initialize)
    initialize.add_argument(
        "--environment", choices=["local", "staging", "production"], default=""
    )
    initialize.add_argument(
        "--work-mode", choices=["development", "maintenance", "incident"], default=""
    )
    initialize.add_argument("--project-summary", default="")
    initialize.add_argument("--external-actions", choices=["forbid", "ask"], default="")
    initialize.add_argument("--constraint", action="append", default=[])
    initialize.add_argument(
        "--non-interactive", action="store_true", help="write defaults and supplied flags"
    )

    doctor = commands.add_parser("doctor", parents=[common], help="preflight report")
    doctor.add_argument(
        "--run-checks", action="store_true", help="actually run the checks, not just list them"
    )

    add = commands.add_parser("add", parents=[common], help="queue one task and exit")
    add.add_argument("text", nargs="+", help="the task, as you would write it to a person")

    commands.add_parser("list", parents=[common], help="show the queue")

    show = commands.add_parser("show", parents=[common], help="show one task")
    show.add_argument("id", type=int)

    retry_task = commands.add_parser(
        "retry-task", parents=[common], help="run fixer and reviewer again"
    )
    retry_task.add_argument("id", type=int)

    retry_delivery = commands.add_parser(
        "retry-delivery", parents=[common], help="retry only merge/push/PR"
    )
    retry_delivery.add_argument("id", type=int)

    cancel_task = commands.add_parser(
        "cancel", parents=[common], help="cancel queued or active work"
    )
    cancel_task.add_argument("id", type=int)

    archive_tasks = commands.add_parser(
        "archive", parents=[common], help="move old completed tasks out of the hot queue"
    )
    archive_tasks.add_argument("--keep", type=int, default=100)

    commands.add_parser("backends", parents=[common], help="show available agent backends")

    events = commands.add_parser("events", parents=[common], help="show the audit journal")
    events.add_argument("--task", type=int)
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--json", action="store_true", dest="json_output")

    once = commands.add_parser("once", parents=[common], help="queue one task and work it now")
    once.add_argument("text", nargs="+", help="the task, as you would write it to a person")
    once.add_argument(
        "--delivery", default="", choices=["", "auto", "local", "merge", "pr"]
    )
    _add_agent_flags(once)
    once.add_argument("--model", default="", help="model for both agents")
    once.add_argument("--dangerous", action="store_true")
    once.add_argument("--strict-security", action="store_true")

    return parser


def resolve(args: argparse.Namespace) -> Config:
    start = Path(args.path).expanduser().resolve()
    if not is_repo(start):
        raise SystemExit(f"agentq: {start} is not a git repository")
    try:
        config = Config.load(repo_root(start))
    except ValueError as error:
        raise SystemExit(f"agentq: invalid {CONFIG_NAME}: {error}") from error
    if getattr(args, "delivery", ""):
        config.delivery = args.delivery
    if getattr(args, "model", ""):
        config.model = args.model
        config.fixer.model = args.model
        config.reviewer.model = args.model
    if getattr(args, "backend", ""):
        config.fixer.backend = args.backend
        config.reviewer.backend = args.backend
    if getattr(args, "fixer_backend", ""):
        config.fixer.backend = args.fixer_backend
    if getattr(args, "reviewer_backend", ""):
        config.reviewer.backend = args.reviewer_backend
    if getattr(args, "environment", ""):
        config.project.environment = args.environment
    if getattr(args, "work_mode", ""):
        config.project.work_mode = args.work_mode
    if getattr(args, "project_summary", ""):
        config.project.summary = args.project_summary.strip()
    if getattr(args, "external_actions", ""):
        config.project.external_actions = args.external_actions
    for constraint in getattr(args, "constraint", []):
        if text := constraint.strip():
            config.project.constraints.append(text)
    if getattr(args, "dangerous", False):
        config.permission = "skip"
        config.fixer.permission = "skip"
    if getattr(args, "strict_security", False):
        config.strict_security = True
    if getattr(args, "unsafe_expose", False):
        config.fronts.setdefault("web", {})["unsafe_expose"] = True
    return config


def doctor(config: Config, *, run_checks: bool = False) -> int:
    """Everything the runner would do, before it does any of it."""
    try:
        queue_detail = f"{config.queue_path} ({len(TaskStore(config.queue_path).load())} tasks)"
        queue_healthy = True
    except QueueCorruptError as exc:
        queue_detail = str(exc)
        queue_healthy = False
    checked_out = current_branch(config.root) or "(detached)"
    registry = config.backend_registry()
    probed = registry.probe()
    assert isinstance(probed, dict)
    selected, backend_errors = _selected_backends(config, registry)
    backend_errors += _strict_security_errors(config, selected, registry)
    delivery_mode = resolve_mode(config.root, config.delivery)
    if delivery_mode == LOCAL:
        delivery_detail = "resolves to local; does not push"
    elif can_open_pr(config.root):
        delivery_detail = "resolves to pr"
    else:
        delivery_detail = "resolves to pr; unavailable without origin and gh"
    lines = [
        f"repository   {config.root}",
        f"base branch  {config.base_branch} (checked out: {checked_out})",
        f"context      {config.project.environment} / {config.project.work_mode}",
        f"delivery     {config.delivery}  ({delivery_detail})",
        f"conventions  {', '.join(config.conventions) or 'none found'}",
        f"worktree     {config.worktree}",
        f"queue        {queue_detail}",
        f"runner lock  {config.state_dir / 'runner.lock'}",
        "checks:",
    ]
    lines.insert(2, "backends:")
    for name, info in probed.items():
        state = f"found {info.version or ''}" if info.available else f"missing ({info.reason})"
        lines.insert(3, f"  {name:12} {state}".rstrip())
    lines.insert(
        3 + len(probed),
        f"agents       fixer={selected.get('fixer', '?')} "
        f"reviewer={selected.get('reviewer', '?')}",
    )
    lines.insert(4 + len(probed), f"baseline     {config.baseline_mode}")
    lines.insert(
        5 + len(probed),
        f"security     {'strict' if config.strict_security else 'compatible'}",
    )
    lines += [f"  {check.name:12} {' '.join(check.command)}" for check in config.checks] or [
        "  (none detected — add [[checks]] to agentq.toml)"
    ]
    if config.process:
        lines.append(f"process      {' '.join(config.process)}")
    if not has_commits(config.root):
        lines.append("\nwarning: this repository has no commits yet — commit something first.")
    if delivery_mode == LOCAL and checked_out != config.base_branch:
        lines.append(
            f"\nwarning: local delivery needs '{config.base_branch}' checked out here; "
            "delivery will block until that is fixed."
        )
    print("\n".join(lines))
    healthy = queue_healthy
    if run_checks and config.checks:
        print("\nrunning the checks here, as a baseline:")
        healthy, report = checks.run(config.checks, config.root)
        print(report)
        if not healthy:
            print("\na check that fails on the base would fail every task — fix it first.")
    if backend_errors:
        for error in backend_errors:
            log.error("agentq.backend_unavailable", reason=error)
    return 0 if not backend_errors and has_commits(config.root) and healthy else 1


def _selected_backends(
    config: Config, registry: BackendRegistry
) -> tuple[dict[str, str], list[str]]:
    selected: dict[str, str] = {}
    errors: list[str] = []
    for role, requested in (
        ("fixer", config.fixer.backend),
        ("reviewer", config.reviewer.backend),
    ):
        choices = config.backend_order if requested in ("", "auto") else [requested]
        reasons: list[str] = []
        for name in choices:
            try:
                info = registry.probe(name)
            except LookupError as exc:
                reasons.append(str(exc))
                continue
            assert isinstance(info, BackendInfo)
            if info.available:
                selected[role] = name
                break
            reasons.append(f"{name}: {info.reason or 'not available'}")
        if role not in selected:
            errors.append(f"{role}: " + "; ".join(reasons))
    return selected, errors


def _strict_security_errors(
    config: Config, selected: dict[str, str], registry: BackendRegistry
) -> list[str]:
    if not config.strict_security:
        return []
    errors: list[str] = []
    for role, name in selected.items():
        info = registry.probe(name)
        assert isinstance(info, BackendInfo)
        gaps = strict_security_gaps(role, info.capabilities)
        if gaps:
            errors.append(f"{role}={name}: missing " + ", ".join(gaps))
    return errors


def init(config: Config, *, interactive: bool = False) -> int:
    target = config.root / CONFIG_NAME
    if target.exists():
        print(f"agentq: {target} already exists — leaving it alone")
    else:
        if interactive:
            _init_wizard(config)
        write_config(config)
        print(f"agentq: wrote {target}")
    ensure_ignored(config.root)
    return 0


def _init_wizard(config: Config) -> None:
    print("agentq: initial setup (press Enter to accept each default)")
    backends = list(
        dict.fromkeys(
            [
                "auto",
                *config.backend_registry().names(),
                config.fixer.backend,
                config.reviewer.backend,
            ]
        )
    )
    if config.fixer.backend == config.reviewer.backend:
        shared = _ask_choice("Agent", backends, config.fixer.backend)
        config.fixer.backend = shared
        config.reviewer.backend = shared
    config.project.environment = _ask_choice(
        "Target environment",
        ["local", "staging", "production"],
        config.project.environment,
    )
    config.project.summary = _ask_text(
        "What are we building or working on now", config.project.summary
    )
    config.project.external_actions = _ask_choice(
        "External actions (forbid=never, ask=request approval)",
        ["forbid", "ask"],
        config.project.external_actions,
    )


def _ask_choice(label: str, choices: list[str], default: str) -> str:
    options = "/".join(choices)
    while True:
        answer = _ask_text(f"{label} ({options})", default)
        if answer in choices:
            return answer
        print(f"agentq: choose one of: {', '.join(choices)}")


def _ask_text(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{label}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def ensure_ignored(root: Path) -> None:
    """State does not belong in history; say so once in .gitignore."""
    if ensure_state_ignored(root):
        print("agentq: added .agentq/ to .gitignore")


def add(config: Config, text: str) -> int:
    task = TaskStore(config.queue_path).add(text, source="cli")
    print(f"🕓 #{task.id} queued")
    return 0


def show_queue(config: Config) -> int:
    tasks = TaskStore(config.queue_path).load()
    if not tasks:
        print("the queue is empty")
        return 0
    for task in tasks:
        line = f"{task.icon} #{task.id:<4} {task.status:8} {log.clip(task.title, 80)}"
        if task.cost_usd:
            line += f"  ${task.cost_usd:.2f}"
        if task.url:
            line += f"  {task.url}"
        print(line)
    return 0


def show_task(config: Config, task_id: int) -> int:
    task = TaskStore(config.queue_path).get(task_id)
    if task is None:
        print(f"agentq: no task #{task_id}")
        return 1
    print(f"{task.icon} #{task.id} {task.status}: {task.title}")
    if task.branch:
        print(f"branch       {task.branch}")
    if task.approved_sha:
        print(f"commit       {task.approved_sha}")
    if task.delivery.stage:
        print(f"delivery     {task.delivery.status} ({task.delivery.stage})")
    if task.url:
        print(task.url)
    if task.note:
        print(task.note)
    return 0


def retry_task(config: Config, task_id: int) -> int:
    task = TaskStore(config.queue_path).retry_task(task_id)
    if task is None:
        print(f"agentq: task #{task_id} cannot be retried")
        return 1
    print(f"🕓 #{task_id} back in the queue; previous branches were preserved")
    return 0


def retry_delivery(config: Config, task_id: int) -> int:
    task = TaskStore(config.queue_path).retry_delivery(task_id)
    if task is None:
        print(f"agentq: task #{task_id} has no recoverable approved delivery")
        return 1
    print(f"🟢 #{task_id} delivery queued for commit {task.approved_sha}")
    return 0


def cancel_task(config: Config, task_id: int) -> int:
    task = TaskStore(config.queue_path).request_cancel(task_id)
    if task is None:
        print(f"agentq: task #{task_id} cannot be cancelled")
        return 1
    print(f"🚫 #{task_id} {'cancelled' if task.status == Status.CANCELLED else 'cancelling'}")
    return 0


def archive_tasks(config: Config, *, keep: int) -> int:
    if keep < 0:
        print("agentq: --keep must be zero or greater")
        return 1
    store = TaskStore(config.queue_path)
    count = store.archive(keep=keep)
    print(f"agentq: archived {count} task(s) to {store.archive_path}")
    return 0


def show_backends(config: Config) -> int:
    found = config.backend_registry().probe()
    assert isinstance(found, dict)
    for name, info in found.items():
        state = "found" if info.available else "missing"
        suffix = f" {info.version}" if info.version else ""
        reason = f" — {info.reason}" if info.reason else ""
        print(f"{name:12} {state}{suffix}{reason}")
    return 0 if any(info.available for info in found.values()) else 1


def show_events(
    config: Config, *, task: int | None = None, limit: int = 100, json_output: bool = False
) -> int:
    records = AuditLog(config.audit_path).read(task=task, limit=limit)
    if json_output:
        print(json.dumps(records, ensure_ascii=False, indent=2))
    elif not records:
        print("agentq: no audit events")
    else:
        for record in records:
            fields = " ".join(
                f"{key}={value}"
                for key, value in record.items()
                if key not in {"time", "event", "task", "note"}
            )
            task_text = f" #{record['task']}" if "task" in record else ""
            prefix = f"{record.get('time', '')} {record.get('event', '')}{task_text}"
            print(f"{prefix} {fields}".rstrip())
    return 0


def once(config: Config, text: str) -> int:
    """One task, start to verdict, in one command — made for scripts and CI."""
    if not preflight(config):
        return 1
    runner = Runner(config, "folder", supervise=False)
    task = runner.store.add(text, source="cli")
    log.info("agentq.once", task=task.id)
    runner.run(until=task.id)
    final = runner.store.get(task.id)
    if final is None:
        return 1
    print(f"{final.icon} #{final.id} {final.status}: {log.clip(final.note, 500)}")
    if final.url:
        print(final.url)
    return 0 if final.status == Status.DONE else 1


def preflight(config: Config) -> bool:
    registry = config.backend_registry()
    selected, errors = _selected_backends(config, registry)
    if errors:
        for reason in errors:
            log.error("agentq.backend_unavailable", reason=reason)
        return False
    security_errors = _strict_security_errors(config, selected, registry)
    if security_errors:
        for reason in security_errors:
            log.error("agentq.security_unsupported", reason=reason)
        return False
    if config.max_usd > 0:
        unsupported: list[str] = []
        for role, name in selected.items():
            info = registry.probe(name)
            assert isinstance(info, BackendInfo)
            if not info.capabilities.exact_cost_reporting:
                unsupported.append(f"{role}={name}")
        if unsupported:
            log.error(
                "agentq.budget_unsupported",
                backends=",".join(unsupported),
                reason="exact USD cost is not reported",
            )
            return False
    if config.fixer.permission == "skip":
        log.warn(
            "agentq.permissions_bypassed — the agents run with --dangerously-skip-permissions "
            "inside their worktree"
        )
    return True


def _share(args: argparse.Namespace, config: Config, front: str) -> Share | None:
    requested = getattr(args, "share", None)
    policy_arg = getattr(args, "share_policy", "")
    unsafe = bool(getattr(args, "share_unsafe", False))
    if not requested:
        if policy_arg or unsafe:
            raise SystemExit("agentq: --share-policy and --share-unsafe require --share ngrok")
        return None
    if front != "web":
        raise SystemExit("agentq: --share ngrok requires the web front")
    if policy_arg and unsafe:
        raise SystemExit("agentq: --share-policy and --share-unsafe cannot be used together")
    policy = Path(policy_arg).expanduser()
    if policy_arg and not policy.is_absolute():
        policy = config.root / policy
    return NgrokTunnel(
        config.root,
        config.state_dir,
        policy=policy if policy_arg else None,
        unsafe=unsafe,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # ``agentq --telegram`` has meant "run" since before there were
    # subcommands, and it still does.
    if not arguments:
        arguments = ["run"]
    elif arguments[0] not in COMMANDS and arguments[0] not in ("-h", "--help"):
        arguments = ["run", *arguments]
    args = build_parser().parse_args(arguments)
    log.setup(args.log_level)
    config = resolve(args)

    if args.command == "init":
        interactive = not args.non_interactive and bool(
            getattr(sys.stdin, "isatty", lambda: False)()
        )
        return init(config, interactive=interactive)
    if args.command == "doctor":
        return doctor(config, run_checks=args.run_checks)
    if args.command == "add":
        return add(config, " ".join(args.text))
    if args.command == "list":
        return show_queue(config)
    if args.command == "show":
        return show_task(config, args.id)
    if args.command == "retry-task":
        return retry_task(config, args.id)
    if args.command == "retry-delivery":
        return retry_delivery(config, args.id)
    if args.command == "cancel":
        return cancel_task(config, args.id)
    if args.command == "archive":
        return archive_tasks(config, keep=args.keep)
    if args.command == "backends":
        return show_backends(config)
    if args.command == "events":
        return show_events(
            config, task=args.task, limit=args.limit, json_output=args.json_output
        )
    if args.command == "once":
        return once(config, " ".join(args.text))

    front = args.front or (
        "telegram" if args.telegram else "web" if args.web or args.share else "folder"
    )
    ready = preflight(config)
    allows_setup = front in {"web", "telegram"}
    if not ready and not allows_setup:
        return 1
    share = _share(args, config, front)
    runner = Runner(
        config,
        front,
        supervise=not args.no_process,
        share=share,
        allow_setup=allows_setup,
    )

    def stop(*_: object) -> None:
        log.info("agentq.stopping")
        runner.stopping = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):  # Windows knows the name but never delivers it
        signal.signal(signal.SIGTERM, stop)

    runner.run(once=args.once)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
