"""Controller lifecycle behavior at mocked Docker boundaries and real test Git repos."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from mergerail.backends.base import BackendInfo, NullEventSink, SessionSpec, TurnRequest
from mergerail.execution.docker import DockerAgentSession, DockerExecution, DockerExecutionError
from mergerail.execution.policy import ExecutionPolicy
from mergerail.execution.sync import host_git
from tests.conftest import run

IMAGE = "mergerail-runtime@sha256:" + "a" * 64


class RecordingLease:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def release(self) -> None:
        self.events.append("lease-released")


@pytest.fixture
def execution(tmp_path: Path) -> DockerExecution:
    return DockerExecution(
        ExecutionPolicy(image=IMAGE),
        tmp_path / "project",
        tmp_path / "state",
        external_backends={"fixture": [sys.executable, "-c", "pass"]},
    )


def _prime_agent_turn(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    *,
    base_sha: str = "a" * 40,
    head_sha: str = "b" * 40,
) -> None:
    execution._preflight_result = {"ready": True}
    execution._base_sha = base_sha
    execution._head_sha = head_sha
    execution._branch = f"mergerail-reviewed/{backend}-lifecycle"
    execution.set_task_id("agent-lifecycle")
    execution._probes[backend] = BackendInfo(backend, True)
    monkeypatch.setattr(execution, "_ensure_preflight", lambda: None)
    monkeypatch.setattr(execution.worktree, "head", lambda: execution._head_sha)
    monkeypatch.setattr(execution, "_selected_model_definition", lambda: {})


def test_online_fixer_turns_restore_session_archive_and_commit_only_verified_state(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base_sha = "a" * 40
    reviewed_sha = "b" * 40
    recovered_heads = ["c" * 40, "d" * 40]
    _prime_agent_turn(execution, monkeypatch, "opencode", base_sha=base_sha, head_sha=reviewed_sha)
    events: list[str] = []
    stage_requests: list[dict[str, str]] = []
    prepared_archives: list[bytes | None] = []
    worker_requests: list[dict[str, Any]] = []
    captured: list[tuple[str, bytes]] = []
    recovered_archives: list[bytes] = []

    def lease() -> RecordingLease:
        events.append("lease-acquired")
        return RecordingLease(events)

    def create_network() -> str:
        events.append("network-created")
        return "private-network"

    def start_gateway(network: str) -> tuple[str, str]:
        assert network == "private-network"
        events.append("gateway-started")
        return "gateway-stage", "172.30.0.2"

    def start_stage(
        _base: str,
        result: str,
        _branch: str,
        *,
        network: str,
        gateway_host: str,
    ) -> tuple[str, bytes, dict[str, str]]:
        stage_requests.append({"result": result, "network": network, "gateway_host": gateway_host})
        events.append("agent-started")
        return "agent-stage", b"snapshot", {base_sha: "refs/mergerail-docker/base"}

    def prepare(
        _container: str,
        _base: str,
        _result: str,
        _refs: dict[str, str],
        *,
        branch: str,
        role: str,
        read_only: bool,
        home_archive: bytes | None,
    ) -> None:
        assert branch == execution._branch
        assert role == "fixer"
        assert not read_only
        prepared_archives.append(home_archive)
        events.append("agent-prepared")

    def worker(
        _container: str,
        request: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        worker_requests.append(request)
        assert kwargs["cancelled"]() is False
        events.append("turn-ran")
        turn = len(worker_requests)
        return {"reply": {"text": f"turn {turn} complete", "session_id": f"session-{turn}"}}

    def capture(_container: str, source: str, *, purpose: str) -> Path:
        turn = len(captured) // 2 + 1
        data = f"{purpose} checkpoint {turn}".encode()
        artifact = tmp_path / f"{purpose}-{turn}.tar"
        artifact.write_bytes(data)
        captured.append((source, data))
        events.append(f"captured-{purpose}-{turn}")
        return artifact

    def remove_container(name: str) -> None:
        events.append(f"removed-{name}")
        with execution._active_lock:
            if execution._active_container == name:
                execution._active_container = None

    def recover(
        _base: str,
        result: str,
        _branch: str,
        archive: Path,
    ) -> str:
        turn = len(recovered_archives) + 1
        assert archive.read_bytes() == f"workspace checkpoint {turn}".encode()
        assert result == (reviewed_sha if turn == 1 else recovered_heads[0])
        assert events[-3:] == [
            "removed-agent-stage",
            "removed-gateway-stage",
            "network-removed",
        ]
        recovered_archives.append(archive.read_bytes())
        events.append(f"checkpoint-imported-{turn}")
        return recovered_heads[turn - 1]

    monkeypatch.setattr(execution, "_with_resource_lease", lease)
    monkeypatch.setattr(execution, "_create_network", create_network)
    monkeypatch.setattr(execution, "_start_gateway", start_gateway)
    monkeypatch.setattr(execution, "_start_stage", start_stage)
    monkeypatch.setattr(execution, "_prepare_stage", prepare)
    monkeypatch.setattr(execution, "_worker", worker)
    monkeypatch.setattr(execution, "_capture_tar", capture)
    monkeypatch.setattr(execution, "_remove_container", remove_container)
    monkeypatch.setattr(
        execution,
        "_remove_network",
        lambda _name: events.append("network-removed"),
    )
    monkeypatch.setattr(execution, "_recover_workspace", recover)
    session = DockerAgentSession(
        execution,
        "opencode",
        SessionSpec(role="fixer", cwd=Path("/work/repo")),
        NullEventSink(),
    )

    first = session.ask(TurnRequest("Implement the change"))
    first_recovery_end = len(events)
    second = session.ask(TurnRequest("Continue the same task"))

    task_dir = execution._task_dir()
    assert first.text == "turn 1 complete" and not first.is_error
    assert second.text == "turn 2 complete" and not second.is_error
    assert worker_requests[0]["resume_session_id"] is None
    assert worker_requests[1]["resume_session_id"] == "session-1"
    assert all(request["gateway"] is True for request in worker_requests)
    assert all(request["gateway_host"] == "172.30.0.2" for request in worker_requests)
    assert [item["result"] for item in stage_requests] == [reviewed_sha, recovered_heads[0]]
    assert [item["network"] for item in stage_requests] == ["private-network"] * 2
    assert prepared_archives == [None, b"fixer-home checkpoint 1"]
    assert recovered_archives == [b"workspace checkpoint 1", b"workspace checkpoint 2"]
    assert (task_dir / "fixer-home.tar").read_bytes() == b"fixer-home checkpoint 2"
    assert list(task_dir.glob("recovery-*.workspace.tar")) == []
    assert execution.metadata["recovery"] == {
        "status": "verified",
        "result_sha": recovered_heads[1],
    }
    assert execution.metadata["agent_result_sha"] == recovered_heads[1]
    assert execution._head_sha == recovered_heads[1]
    assert events.index("removed-agent-stage") < events.index("checkpoint-imported-1")
    assert events.index("removed-gateway-stage") < events.index("checkpoint-imported-1")
    assert events.index("network-removed") < events.index("checkpoint-imported-1")
    assert events[first_recovery_end:].count("lease-released") == 1
    assert events[-1] == "lease-released"


def test_offline_reviewer_returns_reply_without_mutating_fixer_checkpoint(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prime_agent_turn(execution, monkeypatch, "fixture")
    events: list[str] = []
    home = execution._task_dir() / "fixer-home.tar"
    home.write_bytes(b"fixer state remains untouched")
    stage: dict[str, Any] = {}
    prepared: dict[str, Any] = {}
    worker_request: dict[str, Any] = {}

    def lease() -> RecordingLease:
        return RecordingLease(events)

    def start_stage(
        _base: str,
        _result: str,
        _branch: str,
        *,
        network: str,
        gateway_host: str,
    ) -> tuple[str, bytes, dict[str, str]]:
        stage.update(network=network, gateway_host=gateway_host)
        return "review-stage", b"snapshot", {}

    def prepare(
        _container: str,
        _base: str,
        _result: str,
        _refs: dict[str, str],
        **kwargs: Any,
    ) -> None:
        prepared.update(kwargs)

    def worker(
        _container: str,
        request: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        worker_request.update(request)
        assert kwargs["uid"] == 65533
        return {"reply": {"text": "review is clean", "session_id": "review-session"}}

    monkeypatch.setattr(execution, "_with_resource_lease", lease)
    monkeypatch.setattr(execution, "_create_network", lambda: pytest.fail("offline used a network"))
    monkeypatch.setattr(
        execution,
        "_start_gateway",
        lambda _network: pytest.fail("offline started gateway"),
    )
    monkeypatch.setattr(execution, "_start_stage", start_stage)
    monkeypatch.setattr(execution, "_prepare_stage", prepare)
    monkeypatch.setattr(execution, "_worker", worker)
    monkeypatch.setattr(
        execution,
        "_capture_tar",
        lambda *_args, **_kwargs: pytest.fail("reviewer captured mutable state"),
    )
    monkeypatch.setattr(
        execution,
        "_recover_workspace",
        lambda *_args, **_kwargs: pytest.fail("reviewer recovered a workspace"),
    )

    def remove_container(name: str) -> None:
        events.append(f"removed:{name}")

    monkeypatch.setattr(execution, "_remove_container", remove_container)
    session = DockerAgentSession(
        execution,
        "fixture",
        SessionSpec(role="reviewer", cwd=Path("/work/repo"), model="fixture-test"),
        NullEventSink(),
    )

    reply = session.ask(TurnRequest("Review only", schema={"type": "object"}))

    assert reply.text == "review is clean"
    assert session.session_id == "review-session"
    assert stage == {"network": "none", "gateway_host": ""}
    assert prepared["read_only"] is True
    assert prepared["home_archive"] is None
    assert worker_request["read_only"] is True
    assert worker_request["schema"] == {"type": "object"}
    assert worker_request["gateway"] is False
    assert worker_request["model"] == "fixture-test"
    assert home.read_bytes() == b"fixer state remains untouched"
    assert events == ["removed:review-stage", "lease-released"]


def test_cancelled_turn_kills_only_active_stage_and_preserves_saved_home(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prime_agent_turn(execution, monkeypatch, "fixture")
    events: list[str] = []
    home = execution._task_dir() / "fixer-home.tar"
    home.write_bytes(b"last good home archive")
    docker_calls: list[tuple[str, ...]] = []

    def docker(*args: str, **_kwargs: Any) -> str:
        docker_calls.append(args)
        return ""

    def remove_container(name: str) -> None:
        events.append(f"removed:{name}")
        with execution._active_lock:
            if execution._active_container == name:
                execution._active_container = None

    monkeypatch.setattr(execution, "_with_resource_lease", lambda: RecordingLease(events))
    monkeypatch.setattr(
        execution,
        "_start_stage",
        lambda *_args, **_kwargs: ("active-stage", b"", {}),
    )
    monkeypatch.setattr(execution, "_prepare_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(execution, "_docker", docker)
    monkeypatch.setattr(execution, "_remove_container", remove_container)

    session = DockerAgentSession(
        execution,
        "fixture",
        SessionSpec(role="fixer", cwd=Path("/work/repo"), resume_session_id="old-session"),
        NullEventSink(),
    )

    def cancelled_worker(
        _container: str,
        _request: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        session.cancel()
        assert kwargs["cancelled"]() is True
        raise DockerExecutionError("Docker stage was cancelled; last checkpoint preserved")

    monkeypatch.setattr(execution, "_worker", cancelled_worker)

    with pytest.raises(DockerExecutionError, match="last checkpoint preserved"):
        session.ask(TurnRequest("Stop safely"))

    assert docker_calls == [("kill", "active-stage")]
    assert events == ["removed:active-stage", "lease-released"]
    assert execution._active_container is None
    assert session.session_id == "old-session"
    assert home.read_bytes() == b"last good home archive"
    assert execution.metadata["recovery"]["status"] == "last-checkpoint-preserved"


def _result_bundle(repo: Path, tmp_path: Path) -> tuple[str, str, bytes]:
    base_sha = run("rev-parse", "HEAD", cwd=repo)
    run("checkout", "-qb", "sandbox-checkpoint", base_sha, cwd=repo)
    (repo / "checkpoint.txt").write_text("durable checkpoint\n", encoding="utf-8")
    run("add", "checkpoint.txt", cwd=repo)
    run("commit", "-qm", "sandbox checkpoint", cwd=repo)
    result_sha = run("rev-parse", "HEAD", cwd=repo)
    run("update-ref", "refs/heads/mergerail-result", result_sha, cwd=repo)
    path = tmp_path / "result.bundle"
    run("bundle", "create", str(path), "refs/heads/mergerail-result", cwd=repo)
    bundle = path.read_bytes()
    run("update-ref", "-d", "refs/heads/mergerail-result", cwd=repo)
    run("checkout", "-q", "main", cwd=repo)
    return base_sha, result_sha, bundle


@pytest.mark.parametrize("valid_bundle", [True, False], ids=["verified", "malformed-import"])
def test_workspace_recovery_imports_durable_commit_or_preserves_checkpoint_on_error(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    valid_bundle: bool,
) -> None:
    base_sha, result_sha, bundle = _result_bundle(repo, tmp_path)
    execution = DockerExecution(ExecutionPolicy(image=IMAGE), repo, tmp_path / "state")
    checkpoint = tmp_path / "workspace.tar"
    checkpoint.write_bytes(b"captured workspace bytes")
    candidate_branch = "mergerail-reviewed/recovered-task"
    events: list[str] = []
    worker_request: dict[str, Any] = {}
    copied: list[str] = []

    monkeypatch.setattr(execution, "_create_container", lambda: "recovery-stage")
    monkeypatch.setattr(execution, "_runtime_zip", lambda: b"trusted-runtime")

    def copy_into(_container: str, path: str, data: bytes, *, maximum: int) -> None:
        assert data
        assert maximum >= len(data)
        copied.append(path)

    def copy_file_into(
        _container: str,
        path: str,
        source: Path,
        *,
        maximum: int,
    ) -> None:
        assert source.read_bytes() == b"captured workspace bytes"
        assert maximum >= source.stat().st_size
        copied.append(path)

    def worker(
        _container: str,
        request: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        worker_request.update(request)
        return {"head": result_sha}

    def remove_container(name: str) -> None:
        events.append(f"removed:{name}")
        with execution._active_lock:
            if execution._active_container == name:
                execution._active_container = None

    monkeypatch.setattr(execution, "_copy_into", copy_into)
    monkeypatch.setattr(execution, "_copy_file_into", copy_file_into)
    monkeypatch.setattr(execution, "_worker", worker)

    def read_result(_container: str, path: str, _maximum: int) -> bytes:
        if valid_bundle and path == "/tmp/result.bundle":
            return bundle
        return b"not a bundle"

    monkeypatch.setattr(execution, "_read_container_file", read_result)
    monkeypatch.setattr(execution, "_remove_container", remove_container)

    if valid_bundle:
        imported = execution._recover_workspace(base_sha, result_sha, candidate_branch, checkpoint)
        assert imported == result_sha
        assert host_git(repo, "rev-parse", f"refs/heads/{candidate_branch}") == result_sha
        assert host_git(repo, "show", f"{result_sha}:checkpoint.txt") == "durable checkpoint"
        assert host_git(repo, "rev-parse", "HEAD") == base_sha
    else:
        with pytest.raises(DockerExecutionError, match="checkpoint import failed"):
            execution._recover_workspace(base_sha, result_sha, candidate_branch, checkpoint)
        assert (
            host_git(
                repo,
                "rev-parse",
                "--verify",
                f"refs/heads/{candidate_branch}",
                check=False,
            )
            == ""
        )
        assert host_git(repo, "rev-parse", "HEAD") == base_sha

    assert worker_request["mode"] == "recover"
    assert worker_request["base_sha"] == base_sha
    assert worker_request["result_sha"] == result_sha
    assert worker_request["branch"] == candidate_branch
    assert set(worker_request["bundle_refs"]) == {base_sha, result_sha}
    assert copied == ["/tmp/mergerail-runtime.zip", "/tmp/snapshot.bundle", "/tmp/workspace.tar"]
    assert events == ["removed:recovery-stage"]
    assert execution._active_container is None
    assert checkpoint.read_bytes() == b"captured workspace bytes"


def test_docker_backend_proxy_opens_only_virtual_repository_sessions(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prime_agent_turn(execution, monkeypatch, "fixture")
    backend = execution.registry().get("fixture")

    session = backend.open_session(
        SessionSpec(role="reviewer", cwd=Path("/work/repo")),
        NullEventSink(),
    )

    assert isinstance(session, DockerAgentSession)
    assert session.capabilities == BackendInfo("fixture", True).capabilities
    with pytest.raises(DockerExecutionError, match="virtual /work/repo checkout"):
        backend.open_session(SessionSpec(role="reviewer", cwd=Path("/tmp/project")))
