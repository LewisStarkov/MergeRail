"""``agentq`` — the whole thing, from a directory that happens to be a repository."""

from __future__ import annotations

import argparse
import shutil
import signal
import sys
from pathlib import Path

from . import log
from .config import CONFIG_NAME, Config, render_config
from .delivery import can_open_pr
from .gitctl import current_branch, has_commits, is_repo, repo_root
from .runner import Runner

USAGE = """\
  agentq                     work the queue, tasks arrive as files in .agentq/inbox
  agentq --telegram          work the queue, tasks arrive from a Telegram bot
  agentq init                write agentq.toml with what this repository looks like
  agentq doctor              say whether this repository is ready, and what would run
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentq",
        description="Write the task down; an agent does it, a reviewer checks it, it lands.",
        epilog=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", default="run", choices=["run", "init", "doctor"])
    parser.add_argument("--front", default="", help="folder (default) or telegram")
    parser.add_argument("--telegram", action="store_true", help="shorthand for --front telegram")
    parser.add_argument("--once", action="store_true", help="handle one task, then exit")
    parser.add_argument("--delivery", default="", choices=["", "auto", "merge", "pr"])
    parser.add_argument("--model", default="", help="model for both agents")
    parser.add_argument("--no-process", action="store_true", help="do not run the configured app")
    parser.add_argument(
        "--dangerous",
        action="store_true",
        help="pass --dangerously-skip-permissions to the agents (they work in a worktree)",
    )
    parser.add_argument("--path", default=".", help="repository to work in")
    parser.add_argument("--log-level", default="INFO")
    return parser


def resolve(args: argparse.Namespace) -> Config:
    start = Path(args.path).expanduser().resolve()
    if not is_repo(start):
        raise SystemExit(f"agentq: {start} is not a git repository")
    config = Config.load(repo_root(start))
    if args.delivery:
        config.delivery = args.delivery
    if args.model:
        config.model = args.model
    if args.dangerous:
        config.permission = "skip"
    return config


def doctor(config: Config) -> int:
    """Everything the runner would do, before it does any of it."""
    checked_out = current_branch(config.root) or "(detached)"
    installed = shutil.which("claude") is not None
    lines = [
        f"repository   {config.root}",
        f"base branch  {config.base_branch} (checked out: {checked_out})",
        f"claude CLI   {'found' if installed else 'MISSING — install Claude Code'}",
        f"delivery     {config.delivery}"
        + ("" if can_open_pr(config.root) else "  (no remote or no gh: pull requests unavailable)"),
        f"conventions  {', '.join(config.conventions) or 'none found'}",
        f"worktree     {config.worktree}",
        f"queue        {config.queue_path}",
        "checks:",
    ]
    lines += [f"  {check.name:12} {' '.join(check.command)}" for check in config.checks] or [
        "  (none detected — add [[checks]] to agentq.toml)"
    ]
    if config.process:
        lines.append(f"process      {' '.join(config.process)}")
    if not has_commits(config.root):
        lines.append("\nwarning: this repository has no commits yet — commit something first.")
    if checked_out != config.base_branch:
        lines.append(
            f"\nwarning: merging needs '{config.base_branch}' checked out here; "
            f"approved work will become a pull request instead."
        )
    print("\n".join(lines))
    return 0 if installed and has_commits(config.root) else 1


def init(config: Config) -> int:
    target = config.root / CONFIG_NAME
    if target.exists():
        print(f"agentq: {target} already exists — leaving it alone")
        return 0
    target.write_text(render_config(config), encoding="utf-8")
    print(f"agentq: wrote {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log.setup(args.log_level)
    config = resolve(args)

    if args.command == "init":
        return init(config)
    if args.command == "doctor":
        return doctor(config)

    if shutil.which("claude") is None:
        log.error("agentq.no_claude_cli — install Claude Code and sign in first")
        return 1
    if config.permission == "skip":
        log.warn(
            "agentq.permissions_bypassed — the agents run with --dangerously-skip-permissions "
            "inside their worktree"
        )

    front = args.front or ("telegram" if args.telegram else "folder")
    runner = Runner(config, front, supervise=not args.no_process)

    def stop(*_: object) -> None:
        log.info("agentq.stopping")
        runner.stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    runner.run(once=args.once)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
