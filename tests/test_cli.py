from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mergerail import update
from mergerail.audit import AuditLog
from mergerail.backends.registry import BackendRegistry
from mergerail.cli import _runtime_updates_enabled, _share, build_parser, main, resolve
from mergerail.config import Config
from mergerail.fronts import WebFront, make_front
from mergerail.share import NgrokTunnel
from mergerail.tasks import Status, TaskStore
from mergerail.update import ReleaseCandidate
from tests.fake_front import EchoFront


def test_add_then_list(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["add", "fix", "the", "header", "--path", str(repo)]) == 0
    assert "#1" in capsys.readouterr().out

    assert main(["list", "--path", str(repo)]) == 0
    assert "fix the header" in capsys.readouterr().out

    store = TaskStore(repo / ".mergerail" / "tasks.json")
    (task,) = store.load()
    assert task.text == "fix the header"
    assert task.source == "cli"


def test_list_of_nothing_says_so(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list", "--path", str(repo)]) == 0
    assert "empty" in capsys.readouterr().out


def test_update_command_does_not_require_a_project_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[bool] = []

    def run_update(*, check_only: bool) -> int:
        called.append(check_only)
        return 0

    monkeypatch.setattr(update, "update", run_update)

    assert main(["update", "--check"]) == 0
    assert called == [True]


def test_startup_update_activation_failure_exits_nonzero_without_running_old_code(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["mergerail", "run", "--web", "--path", str(repo)])
    monkeypatch.setattr(update, "auto_update", lambda: "v0.2.0")

    def fail_activation(arguments: list[str]) -> None:
        del arguments
        raise update.UpdateError("exec failed")

    monkeypatch.setattr(update, "relaunch", fail_activation)
    monkeypatch.setattr(
        "mergerail.cli.resolve",
        lambda args: pytest.fail("old interpreter must not continue after install"),
    )

    assert main() == 1
    assert "could not restart after updating" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "--web", "--once"],
        ["run", "--web", "--front", "web"],
        ["run", "--web", "--share", "ngrok"],
        ["run", "--web", "--no-update"],
        ["run", "--web", "--telegram"],
    ],
)
def test_runtime_updates_opt_out_of_nonlocal_or_bounded_runs(
    arguments: list[str], tmp_path: Path
) -> None:
    args = build_parser().parse_args(arguments)
    front = WebFront(TaskStore(tmp_path / "tasks.json"))
    assert not _runtime_updates_enabled(args, None, front)


def test_runtime_updates_require_the_real_web_cli(tmp_path: Path) -> None:
    args = build_parser().parse_args(["run", "--web"])
    front = WebFront(TaskStore(tmp_path / "tasks.json"))
    assert _runtime_updates_enabled(args, None, front)
    assert not _runtime_updates_enabled(args, ["run", "--web"], front)


def test_runtime_updates_reject_resolved_nonlocal_or_unsafe_web_fronts(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = build_parser().parse_args(["run", "--web"])
    config = Config.load(repo)
    store = TaskStore(config.queue_path)

    config.fronts["web"] = {"host": "0.0.0.0"}
    configured_nonlocal = make_front("web", config, store)
    assert not _runtime_updates_enabled(args, None, configured_nonlocal)

    config.fronts["web"] = {"host": "127.0.0.1"}
    monkeypatch.setenv("MERGERAIL_WEB_HOST", "::")
    environment_nonlocal = make_front("web", config, store)
    assert not _runtime_updates_enabled(args, None, environment_nonlocal)

    monkeypatch.setenv("MERGERAIL_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("MERGERAIL_WEB_UNSAFE_EXPOSE", "1")
    unsafe = make_front("web", config, store)
    assert not _runtime_updates_enabled(args, None, unsafe)


def test_runtime_install_happens_after_runner_returns(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    found = ReleaseCandidate("v0.2.0", "1" * 40, update.REPOSITORY)
    runs = iter([found, None])

    class FakeRunner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.stopping = False
            self.front = WebFront(TaskStore(repo / ".mergerail" / "fake-tasks.json"))
            self.update_probe: object = None
            events.append("runner.created")

        def run(self, *, once: bool = False) -> ReleaseCandidate | None:
            assert once is False
            assert self.update_probe is update.runtime_candidate
            events.append("runner.returned")
            return next(runs)

    monkeypatch.setattr(sys, "argv", ["mergerail", "run", "--web", "--path", str(repo)])
    monkeypatch.setattr("mergerail.cli.Runner", FakeRunner)
    monkeypatch.setattr("mergerail.cli.preflight", lambda config: True)
    monkeypatch.setattr(update, "auto_update", lambda: None)

    def fail_install(found: ReleaseCandidate) -> bool:
        events.append(f"install:{found.tag}")
        return False

    monkeypatch.setattr(update, "install_candidate", fail_install)

    assert main() == 0
    assert events == [
        "runner.created",
        "runner.returned",
        "install:v0.2.0",
        "runner.created",
        "runner.returned",
    ]


def test_runtime_update_does_not_install_after_stop_is_requested(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = ReleaseCandidate("v0.2.0", "1" * 40, update.REPOSITORY)

    class FakeRunner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.stopping = False
            self.front = WebFront(TaskStore(repo / ".mergerail" / "fake-tasks.json"))

        def run(self, *, once: bool = False) -> ReleaseCandidate:
            del once
            self.stopping = True
            return found

    monkeypatch.setattr(sys, "argv", ["mergerail", "run", "--web", "--path", str(repo)])
    monkeypatch.setattr("mergerail.cli.Runner", FakeRunner)
    monkeypatch.setattr("mergerail.cli.preflight", lambda config: True)
    monkeypatch.setattr(update, "auto_update", lambda: None)
    monkeypatch.setattr(
        update,
        "install_candidate",
        lambda found: pytest.fail("stop must win before installation"),
    )

    assert main() == 0


def test_runtime_update_checks_stop_again_before_activation(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = ReleaseCandidate("v0.2.0", "1" * 40, update.REPOSITORY)

    class FakeRunner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self.stopping = False
            self.front = WebFront(TaskStore(repo / ".mergerail" / "fake-tasks.json"))
            instances.append(self)

        def run(self, *, once: bool = False) -> ReleaseCandidate:
            del once
            return found

    instances: list[FakeRunner] = []

    def install(found: ReleaseCandidate) -> bool:
        del found
        instances[-1].stopping = True
        return True

    monkeypatch.setattr(sys, "argv", ["mergerail", "run", "--web", "--path", str(repo)])
    monkeypatch.setattr("mergerail.cli.Runner", FakeRunner)
    monkeypatch.setattr("mergerail.cli.preflight", lambda config: True)
    monkeypatch.setattr(update, "auto_update", lambda: None)
    monkeypatch.setattr(update, "install_candidate", install)
    monkeypatch.setattr(
        update, "relaunch", lambda arguments: pytest.fail("stop must win before activation")
    )

    assert main() == 0


def test_activation_failure_exits_nonzero_without_resuming_old_code(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = ReleaseCandidate("v0.2.0", "1" * 40, update.REPOSITORY)
    created = 0

    class FakeRunner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            nonlocal created
            del args, kwargs
            created += 1
            self.stopping = False
            self.front = WebFront(TaskStore(repo / ".mergerail" / "fake-tasks.json"))

        def run(self, *, once: bool = False) -> ReleaseCandidate:
            del once
            return found

    monkeypatch.setattr(sys, "argv", ["mergerail", "run", "--web", "--path", str(repo)])
    monkeypatch.setattr("mergerail.cli.Runner", FakeRunner)
    monkeypatch.setattr("mergerail.cli.preflight", lambda config: True)
    monkeypatch.setattr(update, "auto_update", lambda: None)
    monkeypatch.setattr(update, "install_candidate", lambda found: True)

    def fail_activation(arguments: list[str]) -> None:
        del arguments
        raise update.UpdateError("exec failed")

    monkeypatch.setattr(update, "relaunch", fail_activation)

    assert main() == 1
    assert created == 1
    assert "restart manually" in capsys.readouterr().err


def test_events_reads_and_filters_the_audit_journal(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = Config.load(repo)
    audit = AuditLog(config.audit_path)
    audit.emit("task.started", task=1)
    audit.emit("task.finished", task=2, status="done")

    assert main(["events", "--task", "2", "--path", str(repo)]) == 0
    output = capsys.readouterr().out
    assert "task.finished #2" in output
    assert "task.started" not in output


def test_backend_flags_resolve_per_role(repo: Path) -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--path",
            str(repo),
            "--agent",
            "codex",
            "--reviewer-agent",
            "opencode",
            "--model",
            "provider/test",
        ]
    )
    config = resolve(args)
    assert config.fixer.backend == "codex"
    assert config.reviewer.backend == "opencode"
    assert config.fixer.model == "provider/test"
    assert config.reviewer.model == "provider/test"


def test_unsafe_expose_is_an_explicit_web_setting(repo: Path) -> None:
    args = build_parser().parse_args(["run", "--path", str(repo), "--unsafe-expose"])
    config = resolve(args)
    assert config.front("web")["unsafe_expose"] is True


def test_ngrok_share_implies_web_and_is_secure_by_default(repo: Path) -> None:
    args = build_parser().parse_args(["run", "--path", str(repo), "--share", "ngrok"])
    config = resolve(args)
    share = _share(args, config, "web")
    assert isinstance(share, NgrokTunnel)
    assert share.unsafe is False
    assert share.policy is None


def test_ngrok_share_flags_are_not_silently_ignored(repo: Path) -> None:
    config = Config.load(repo)
    args = build_parser().parse_args(["run", "--path", str(repo), "--share-unsafe"])
    with pytest.raises(SystemExit, match="require --share ngrok"):
        _share(args, config, "web")

    args = build_parser().parse_args(
        ["run", "--path", str(repo), "--share", "ngrok", "--front", "telegram"]
    )
    with pytest.raises(SystemExit, match="requires the web front"):
        _share(args, config, "telegram")


def test_backends_lists_an_explicit_external_driver(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "mergerail.toml").write_text(
        "[backends.fixture]\n"
        'protocol = "mergerail-jsonl-v1"\n'
        f"command = [{json.dumps(sys.executable)}]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("mergerail.config.default_registry", BackendRegistry)

    assert main(["backends", "--path", str(repo)]) == 0
    output = capsys.readouterr().out
    assert "fixture" in output
    assert "found" in output


def test_invalid_external_backend_config_has_no_traceback(repo: Path) -> None:
    (repo / "mergerail.toml").write_text(
        '[backends.fixture]\ncommand = ["fixture"]\n', encoding="utf-8"
    )

    with pytest.raises(SystemExit, match=r"invalid mergerail.toml.*mergerail-jsonl-v1"):
        main(["backends", "--path", str(repo)])


def test_show_includes_recoverable_delivery_details(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = Config.load(repo)
    store = TaskStore(config.queue_path)
    task = store.add("ship this")
    store.approve(
        task.id,
        branch="mergerail/1/a1",
        commit="abc123",
        requested_mode="pr",
        base_branch="main",
    )
    store.start_delivery(task.id, "pr")
    store.block_delivery(task.id, stage="push", code="push_failed", message="origin refused")

    assert main(["show", str(task.id), "--path", str(repo)]) == 0
    output = capsys.readouterr().out
    assert "mergerail/1/a1" in output
    assert "abc123" in output
    assert "blocked (push)" in output
    assert "origin refused" in output

    assert main(["show", "999", "--path", str(repo)]) == 1
    assert "no task #999" in capsys.readouterr().out


def test_retry_delivery_requeues_only_the_approved_commit(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = Config.load(repo)
    store = TaskStore(config.queue_path)
    task = store.add("ship this")
    store.approve(
        task.id,
        branch="mergerail/1/a1",
        commit="abc123",
        requested_mode="pr",
        base_branch="main",
    )
    store.start_delivery(task.id, "pr")
    store.block_delivery(task.id, stage="push", code="push_failed", message="origin refused")

    assert main(["retry-delivery", str(task.id), "--path", str(repo)]) == 0
    retried = store.get(task.id)
    assert retried is not None
    assert retried.status == Status.APPROVED
    assert retried.approved_sha == "abc123"
    assert retried.branch == "mergerail/1/a1"
    assert "commit abc123" in capsys.readouterr().out

    waiting = store.add("not approved")
    assert main(["retry-delivery", str(waiting.id), "--path", str(repo)]) == 1
    assert "no recoverable approved delivery" in capsys.readouterr().out


def test_retry_task_preserves_the_previous_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = Config.load(repo)
    store = TaskStore(config.queue_path)
    task = store.add("try again")
    store.update(task.id, status=Status.FAILED, branch="mergerail/1/a1", note="failed")

    assert main(["retry-task", str(task.id), "--path", str(repo)]) == 0
    retried = store.get(task.id)
    assert retried is not None
    assert retried.status == Status.NEW
    assert retried.branch is None
    assert retried.previous_branches == ["mergerail/1/a1"]
    assert "previous branches were preserved" in capsys.readouterr().out

    assert main(["retry-task", str(task.id), "--path", str(repo)]) == 1
    assert "cannot be retried" in capsys.readouterr().out


def test_cancel_queued_task(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = TaskStore(Config.load(repo).queue_path)
    task = store.add("stop me")

    assert main(["cancel", str(task.id), "--path", str(repo)]) == 0
    cancelled = store.get(task.id)
    assert cancelled is not None and cancelled.status == Status.CANCELLED
    assert "cancelled" in capsys.readouterr().out


def test_archive_command_keeps_recent_terminal_tasks(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = TaskStore(Config.load(repo).queue_path)
    for number in range(3):
        task = store.add(f"done {number}")
        store.update(task.id, status=Status.DONE)

    assert main(["archive", "--keep", "1", "--path", str(repo)]) == 0
    assert [task.id for task in store.load()] == [3]
    assert [task.id for task in store.archived()] == [2, 1]
    assert "archived 2" in capsys.readouterr().out


def test_init_writes_config_and_ignores_the_state(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", "--path", str(repo)]) == 0
    assert (repo / "mergerail.toml").exists()
    assert ".mergerail/" in (repo / ".gitignore").read_text(encoding="utf-8")

    # Running it again must neither duplicate the ignore line nor touch the file.
    before = (repo / ".gitignore").read_text(encoding="utf-8")
    assert main(["init", "--path", str(repo)]) == 0
    assert (repo / ".gitignore").read_text(encoding="utf-8") == before


def test_init_flags_write_agent_and_project_context(repo: Path) -> None:
    assert (
        main(
            [
                "init",
                "--path",
                str(repo),
                "--agent",
                "codex",
                "--environment",
                "production",
                "--work-mode",
                "maintenance",
                "--project-summary",
                "release the API",
                "--external-actions",
                "ask",
                "--constraint",
                "no downtime",
                "--non-interactive",
            ]
        )
        == 0
    )
    config = Config.load(repo)
    assert config.fixer.backend == "codex"
    assert config.reviewer.backend == "codex"
    assert config.project.environment == "production"
    assert config.project.work_mode == "maintenance"
    assert config.project.summary == "release the API"
    assert config.project.external_actions == "ask"
    assert config.project.constraints == ["no downtime"]


def test_interactive_init_asks_for_operating_context(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter(
        [
            "production",
            "restore payments",
            "ask",
        ]
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert (
        main(
            [
                "init",
                "--path",
                str(repo),
                "--fixer-agent",
                "codex",
                "--reviewer-agent",
                "claude",
            ]
        )
        == 0
    )
    config = Config.load(repo)
    assert config.fixer.backend == "codex"
    assert config.reviewer.backend == "claude"
    assert config.project.environment == "production"
    assert config.project.work_mode == "development"
    assert config.project.summary == "restore payments"
    assert config.project.external_actions == "ask"
    assert config.project.constraints == []


def test_outside_a_repository_it_refuses_plainly(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="not a git repository"):
        main(["list", "--path", str(tmp_path)])


def test_bare_flags_still_mean_run(repo: Path) -> None:
    # `mergerail --telegram` predates subcommands and must keep working; without a
    # token it exits with the message that says exactly what is missing.
    with pytest.raises(SystemExit, match="no Telegram token"):
        main(["--telegram", "--path", str(repo)])


def test_a_third_party_front_loads_by_dotted_path(repo: Path) -> None:
    config = Config.load(repo)
    store = TaskStore(config.queue_path)
    front = make_front("tests.fake_front:EchoFront", config, store)
    assert isinstance(front, EchoFront)
    assert front.config is config


def test_a_missing_front_fails_with_the_name_in_the_message(repo: Path) -> None:
    config = Config.load(repo)
    store = TaskStore(config.queue_path)
    with pytest.raises(SystemExit, match="no_such_module"):
        make_front("no_such_module:Nope", config, store)
