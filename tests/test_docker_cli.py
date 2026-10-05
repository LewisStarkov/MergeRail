from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from mergerail import checks, update
from mergerail.backends import (
    AgentSession,
    BackendCapabilities,
    BackendInfo,
    BackendRegistry,
    EventSink,
    SessionSpec,
)
from mergerail.cli import doctor, main
from mergerail.config import AgentConfig, Config
from mergerail.detect import Check
from mergerail.execution.policy import ExecutionPolicy

IMAGE = "sha256:" + "a" * 64


class FixtureBackend:
    name = "fixture"

    def probe(self) -> BackendInfo:
        return BackendInfo(
            self.name,
            True,
            "fixture",
            BackendCapabilities(native_read_only=True, native_push_denial=True),
        )

    def open_session(self, spec: SessionSpec, events: EventSink | None = None) -> AgentSession:
        raise AssertionError("doctor must not start an agent turn")


class FixtureDocker:
    instances: ClassVar[list[FixtureDocker]] = []
    fail = False

    def __init__(
        self,
        policy: ExecutionPolicy,
        root: Path,
        state_dir: Path,
        external_backends: dict[str, list[str]] | None = None,
    ) -> None:
        self.policy = policy
        self.root = root
        self.ran_checks: list[Check] = []
        self.instances.append(self)

    def preflight(self) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("daemon unavailable")
        return {"validated": True, "image": self.policy.image}

    def registry(self) -> BackendRegistry:
        return BackendRegistry([FixtureBackend()])

    def run_checks(self, configured: list[Check], sha: str) -> tuple[bool, str]:
        assert len(sha) == 40
        self.ran_checks = configured
        return True, "isolated baseline passed"


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> type[FixtureDocker]:
    FixtureDocker.instances.clear()
    monkeypatch.setattr(FixtureDocker, "fail", False)
    monkeypatch.setattr("mergerail.execution.docker.DockerExecution", FixtureDocker)
    monkeypatch.setattr(update, "auto_update", lambda: pytest.fail("Docker must disable updater"))
    monkeypatch.setattr(checks, "run", lambda *a, **kw: pytest.fail("host checks must not run"))
    return FixtureDocker


def configuration(repo: Path) -> Config:
    config = Config.load(repo)
    config.execution = ExecutionPolicy(image=IMAGE)
    config.fixer = AgentConfig(backend="fixture")
    config.reviewer = AgentConfig(backend="fixture", permission="review")
    config.delivery = "local"
    config.strict_security = True
    config.checks = [Check("baseline", ["python3", "-c", "print('hello')"])]
    return config


def test_doctor_uses_only_container_baseline_and_reports_image(
    repo: Path, fake_docker: type[FixtureDocker], capsys: pytest.CaptureFixture[str]
) -> None:
    config = configuration(repo)
    assert doctor(config, run_checks=True) == 0
    assert fake_docker.instances[-1].ran_checks == config.checks
    output = capsys.readouterr().out
    assert IMAGE in output
    assert "isolated baseline passed" in output
    assert "fixture" in output


def test_doctor_unavailable_engine_fails_before_checks_or_agents(
    repo: Path, fake_docker: type[FixtureDocker], capsys: pytest.CaptureFixture[str]
) -> None:
    fake_docker.fail = True
    assert doctor(configuration(repo), run_checks=True) == 1
    assert fake_docker.instances[-1].ran_checks == []
    assert "host fallback is forbidden" in capsys.readouterr().out


def test_init_validates_before_persisting_docker_config(
    repo: Path, fake_docker: type[FixtureDocker]
) -> None:
    fake_docker.fail = True
    assert main(["init", "--path", str(repo), "--docker-image", IMAGE]) == 1
    assert not (repo / "mergerail.toml").exists()
    fake_docker.fail = False
    assert main(["init", "--path", str(repo), "--docker-image", IMAGE]) == 0
    saved = Config.load(repo)
    assert saved.execution is not None and saved.execution.image == IMAGE


def test_init_adds_docker_without_discarding_existing_project_settings(
    repo: Path, fake_docker: type[FixtureDocker]
) -> None:
    path = repo / "mergerail.toml"
    original = 'max_rounds = 2\n[project]\nenvironment = "staging"\nwork_mode = "maintenance"\n'
    path.write_text(original)
    assert main(["init", "--path", str(repo), "--docker-image", IMAGE]) == 0
    assert path.read_text().startswith(original)
    saved = Config.load(repo)
    assert saved.max_rounds == 2
    assert saved.project.environment == "staging"
    assert saved.execution is not None and saved.execution.image == IMAGE
