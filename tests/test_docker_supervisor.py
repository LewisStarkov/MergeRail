from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from mergerail.detect import Check
from mergerail.execution.docker import DockerExecution
from mergerail.execution.policy import ExecutionPolicy
from mergerail.supervisor import DockerSupervisor


class AppExecution:
    def __init__(self) -> None:
        self.policy = ExecutionPolicy(
            image="sha256:" + "a" * 64, idle_stop_seconds=1, log_limit_mib=1
        )
        self.started = threading.Event()
        self.calls = 0

    def run_checks(
        self, checks: list[Check], sha: str, *, cancelled: Callable[[], bool]
    ) -> tuple[bool, str]:
        assert checks[0].command == ["python3", "app.py"]
        assert len(sha) == 40
        self.calls += 1
        self.started.set()
        while not cancelled():
            threading.Event().wait(0.01)
        return False, "app: cancelled\nERROR final line"


def supervisor(repo: Path, execution: AppExecution) -> DockerSupervisor:
    return DockerSupervisor(
        ["python3", "app.py"], repo, repo / ".mergerail/app.log", cast(DockerExecution, execution)
    )


def test_idle_stop_does_not_restart_app(repo: Path) -> None:
    execution = AppExecution()
    app = supervisor(repo, execution)
    assert app.enabled and not app.alive
    app.start()
    assert execution.started.wait(5)
    app.start()
    assert execution.calls == 1
    assert app._thread is not None
    app._thread.join(5)
    assert not app.alive
    app.supervise()
    assert execution.calls == 1
    assert "ERROR final line" in app.error_digest()
    assert app.log_path.read_text().endswith("ERROR final line")
    app.stop()


def test_stop_finishes_before_explicit_restart(repo: Path) -> None:
    execution = AppExecution()
    app = supervisor(repo, execution)
    app.start()
    assert execution.started.wait(5)
    app.stop()
    assert not app.alive
    execution.started.clear()
    app.restart()
    assert execution.started.wait(5)
    assert execution.calls == 2
    app.stop()


def test_app_that_does_not_cancel_blocks_heavy_stage(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = AppExecution()
    release = threading.Event()

    def blocked(
        checks: list[Check], sha: str, *, cancelled: Callable[[], bool]
    ) -> tuple[bool, str]:
        execution.started.set()
        release.wait(5)
        return True, "stopped"

    monkeypatch.setattr(execution, "run_checks", blocked)
    app = supervisor(repo, execution)
    app.start()
    assert execution.started.wait(5)
    try:
        with pytest.raises(RuntimeError, match="heavy stages remain blocked"):
            app.stop(timeout=0)
    finally:
        release.set()
        app.stop()


def test_empty_app_command_never_starts(repo: Path) -> None:
    execution = AppExecution()
    app = DockerSupervisor([], repo, repo / "app.log", cast(DockerExecution, execution))
    app.start()
    assert not app.enabled and not app.alive
    assert execution.calls == 0
    app.stop()
