"""Lifecycle checks for Docker stages using a real Git repository and fake Engine boundary."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from mergerail.backends import (
    BackendCapabilities,
    BackendInfo,
    NullEventSink,
    SessionSpec,
    TurnRequest,
)
from mergerail.detect import Check
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
def execution(repo: Path, tmp_path: Path) -> DockerExecution:
    instance = DockerExecution(
        ExecutionPolicy(image=IMAGE),
        repo,
        tmp_path / "state",
        external_backends={"fixture": [sys.executable, "-c", "pass"]},
    )
    instance._preflight_result = {"ready": True}
    return instance


def test_worktree_reset_and_detach_preserve_host_checkout_and_branch(
    execution: DockerExecution, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = run("rev-parse", "HEAD", cwd=repo)
    events: list[str] = []

    def remove_container(name: str) -> None:
        events.append(f"removed:{name}")

    monkeypatch.setattr(execution, "_with_resource_lease", lambda: RecordingLease(events))
    monkeypatch.setattr(execution, "_create_container", lambda: "reset-stage")
    monkeypatch.setattr(execution, "_runtime_zip", lambda: b"runtime")
    monkeypatch.setattr(execution, "_copy_into", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(execution, "_remove_container", remove_container)

    def worker(_container: str, request: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        assert request["mode"] == "prepare"
        assert request["role"] == "fixer"
        return {"head": base}

    monkeypatch.setattr(execution, "_worker", worker)

    execution.worktree.reset("mergerail-candidate/task-1", base)
    execution.worktree.detach(base)

    assert execution.worktree.head() == base
    assert run("rev-parse", "refs/heads/mergerail-candidate/task-1", cwd=repo) == base
    assert run("symbolic-ref", "--short", "HEAD", cwd=repo) == "main"
    assert execution.worktree.is_dirty() is False
    assert execution._base_sha == execution._head_sha == base
    assert events == ["removed:reset-stage", "lease-released"]


def test_fixer_cleans_sandboxes_before_recovery_and_preserves_unverified_archive(
    execution: DockerExecution, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base_sha = "a" * 40
    reviewed_sha = "b" * 40
    execution.set_task_id("task-8")
    execution._base_sha = base_sha
    execution._head_sha = reviewed_sha
    execution._branch = "mergerail-reviewed/task-8"
    monkeypatch.setattr(execution.worktree, "head", lambda: reviewed_sha)
    monkeypatch.setattr(execution, "probe", lambda name: BackendInfo(name, True))
    monkeypatch.setattr(execution, "_selected_model_definition", lambda: {})

    events: list[str] = []

    def lease() -> RecordingLease:
        events.append("lease-acquired")
        return RecordingLease(events)

    monkeypatch.setattr(
        execution,
        "_with_resource_lease",
        lease,
    )
    monkeypatch.setattr(execution, "_create_network", lambda: "ai-network")
    monkeypatch.setattr(execution, "_start_gateway", lambda _network: ("gateway", "172.20.0.2"))
    monkeypatch.setattr(execution, "_start_stage", lambda *_args, **_kwargs: ("agent", b"", {}))
    monkeypatch.setattr(execution, "_prepare_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        execution,
        "_worker",
        lambda *_args, **_kwargs: {"reply": {"text": "work complete", "session_id": "next"}},
    )

    captured: dict[str, Path] = {}

    def capture(_container: str, _source: str, *, purpose: str) -> Path:
        artifact = tmp_path / f"{purpose}.tar"
        artifact.write_bytes(b"workspace checkpoint" if purpose == "workspace" else b"home state")
        captured[purpose] = artifact
        return artifact

    monkeypatch.setattr(execution, "_capture_tar", capture)

    def remove_container(name: str) -> None:
        events.append(f"removed:{name}")

    monkeypatch.setattr(execution, "_remove_container", remove_container)
    monkeypatch.setattr(
        execution,
        "_remove_network",
        lambda name: events.append(f"removed:{name}"),
    )

    def recover(_base: str, _result: str, _branch: str, artifact: Path) -> str:
        events.append("recovery-started")
        assert artifact.exists()
        assert artifact.read_bytes() == b"workspace checkpoint"
        raise DockerExecutionError("offline checkpoint validation failed")

    monkeypatch.setattr(execution, "_recover_workspace", recover)
    session = DockerAgentSession(
        execution,
        "opencode",
        SessionSpec(role="fixer", cwd=Path("/work/repo")),
        NullEventSink(),
    )

    with pytest.raises(DockerExecutionError, match="offline checkpoint validation failed"):
        session.ask(TurnRequest("Make the fix"))

    assert events.index("removed:agent") < events.index("recovery-started")
    assert events.index("removed:gateway") < events.index("recovery-started")
    assert events.index("removed:ai-network") < events.index("recovery-started")
    assert events[-1] == "lease-released"
    assert execution._head_sha == reviewed_sha
    assert session.session_id is None
    recovery_artifacts = list(execution._task_dir().glob("recovery-*.workspace.tar"))
    assert len(recovery_artifacts) == 1
    assert recovery_artifacts[0].read_bytes() == b"workspace checkpoint"
    assert execution.metadata["recovery"]["status"] == "unverified"
    assert not captured["fixer-home"].exists()


def test_cancelled_checks_release_the_container_and_resource_lease(
    execution: DockerExecution,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sha = run("rev-parse", "HEAD", cwd=repo)
    events: list[str] = []

    def cancelled() -> bool:
        return True

    def remove_container(name: str) -> None:
        events.append(f"removed:{name}")

    monkeypatch.setattr(execution, "_with_resource_lease", lambda: RecordingLease(events))
    monkeypatch.setattr(execution, "_create_container", lambda: "check-stage")
    monkeypatch.setattr(execution, "_runtime_zip", lambda: b"runtime")
    monkeypatch.setattr(execution, "_copy_into", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(execution, "_remove_container", remove_container)
    requests: list[str] = []

    def worker(
        _container: str,
        request: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        requests.append(str(request["mode"]))
        if request["mode"] == "prepare":
            return {"head": sha}
        assert kwargs["cancelled"] is cancelled
        raise DockerExecutionError("Docker stage was cancelled; last checkpoint preserved")

    monkeypatch.setattr(execution, "_worker", worker)

    passed, report = execution.run_checks(
        [Check("quick", [sys.executable, "-c", "pass"])],
        sha,
        cancelled=cancelled,
    )

    assert not passed
    assert "cancelled" in report
    assert requests == ["prepare", "checks"]
    assert events == ["removed:check-stage", "lease-released"]


def test_baseline_does_not_hide_execution_infrastructure_errors(
    execution: DockerExecution, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(_checks: list[Check], _sha: str, **_kwargs: Any) -> dict[str, Any]:
        raise DockerExecutionError("Docker daemon disappeared")

    monkeypatch.setattr(execution, "_run_checks", unavailable)

    with pytest.raises(DockerExecutionError, match="Docker daemon disappeared"):
        execution.baseline([Check("lint", ["lint"])], "f" * 40)


def test_check_runner_fails_when_a_successful_response_omits_a_check(
    execution: DockerExecution, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = [Check("lint", ["lint"]), Check("unit", ["unit"])]
    monkeypatch.setattr(
        execution,
        "_run_checks",
        lambda *_args, **_kwargs: {
            "passed": True,
            "results": [{"name": "lint", "passed": True, "output": "ok"}],
        },
    )

    passed, report = execution.run_checks(checks, "f" * 40)

    assert not passed
    assert "unit: FAIL — check did not run" in report


def _result_bundle(repo: Path, sha: str, target: Path) -> bytes:
    run("update-ref", "refs/heads/mergerail-result", sha, cwd=repo)
    run("bundle", "create", str(target), "refs/heads/mergerail-result", cwd=repo)
    return target.read_bytes()


def test_merge_candidate_imports_combined_commit_without_moving_reviewed_ref(
    execution: DockerExecution,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen_base = run("rev-parse", "HEAD", cwd=repo)
    run("checkout", "-qb", "agent-result", frozen_base, cwd=repo)
    (repo / "agent.txt").write_text("reviewed result\n", encoding="utf-8")
    run("add", "agent.txt", cwd=repo)
    run("commit", "-qm", "approved agent result", cwd=repo)
    reviewed_sha = run("rev-parse", "HEAD", cwd=repo)
    run("checkout", "-q", "main", cwd=repo)
    (repo / "base-advance.txt").write_text("concurrent base update\n", encoding="utf-8")
    run("add", "base-advance.txt", cwd=repo)
    run("commit", "-qm", "advance base", cwd=repo)
    advanced_base = run("rev-parse", "HEAD", cwd=repo)
    run("merge", "--no-ff", "-m", "integrate approved result", "agent-result", cwd=repo)
    candidate_sha = run("rev-parse", "HEAD", cwd=repo)
    reviewed_branch = "mergerail-reviewed/task-12"
    run("update-ref", f"refs/heads/{reviewed_branch}", reviewed_sha, cwd=repo)
    bundle = _result_bundle(repo, candidate_sha, tmp_path / "candidate.bundle")

    execution._branch = reviewed_branch
    execution._head_sha = reviewed_sha
    execution._base_sha = frozen_base
    execution.set_task_id("task-12")
    monkeypatch.setattr(execution, "_with_resource_lease", lambda: RecordingLease([]))
    monkeypatch.setattr(execution, "_create_container", lambda: "merge-stage")
    monkeypatch.setattr(execution, "_runtime_zip", lambda: b"runtime")
    monkeypatch.setattr(execution, "_copy_into", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(execution, "_read_container_file", lambda *_args, **_kwargs: bundle)
    monkeypatch.setattr(execution, "_remove_container", lambda _name: None)
    worker_requests: list[dict[str, Any]] = []

    def worker(_container: str, request: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        worker_requests.append(request)
        return {"head": candidate_sha}

    monkeypatch.setattr(execution, "_worker", worker)

    imported = execution.merge_candidate(advanced_base, reviewed_sha, reviewed_branch)

    candidate_branch = str(execution.metadata["candidate_branch"])
    assert imported == candidate_sha
    assert execution.worktree.head() == reviewed_sha
    assert execution._branch == reviewed_branch
    assert execution._head_sha == reviewed_sha
    assert execution.metadata["agent_result_sha"] == reviewed_sha
    assert execution.metadata["candidate_sha"] == candidate_sha
    assert host_git(repo, "rev-parse", f"refs/heads/{candidate_branch}") == candidate_sha
    assert worker_requests[0]["branch"] == reviewed_branch
    assert run("show", f"{candidate_sha}:agent.txt", cwd=repo) == "reviewed result"
    assert run("show", f"{candidate_sha}:base-advance.txt", cwd=repo) == "concurrent base update"


def test_probe_normalizes_capabilities_caches_result_and_keeps_native_wrappers(
    execution: DockerExecution,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sha = run("rev-parse", "HEAD", cwd=repo)
    events: list[str] = []
    monkeypatch.setattr(execution, "_with_resource_lease", lambda: RecordingLease(events))
    monkeypatch.setattr(execution, "_create_container", lambda: "probe-stage")
    monkeypatch.setattr(execution, "_runtime_zip", lambda: b"runtime")
    monkeypatch.setattr(execution, "_copy_into", lambda *_args, **_kwargs: None)

    def remove_container(name: str) -> None:
        events.append(f"removed:{name}")

    monkeypatch.setattr(execution, "_remove_container", remove_container)
    worker_modes: list[str] = []

    def worker(_container: str, request: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        worker_modes.append(str(request["mode"]))
        if request["mode"] == "prepare":
            return {"head": sha}
        return {
            "probe": {
                "available": True,
                "version": "fixture-1",
                "capabilities": {"streaming": True, "native_read_only": True},
            }
        }

    monkeypatch.setattr(execution, "_worker", worker)

    info = execution.probe(" FIXTURE ")
    cached = execution.probe("fixture")
    claude = execution.probe("claude")
    registry = execution.registry()

    assert info.name == "fixture" and info.available and info.version == "fixture-1"
    assert info.capabilities.streaming
    assert info.capabilities.native_read_only
    assert not info.capabilities.structured_output
    assert cached is info
    assert worker_modes == ["prepare", "probe"]
    assert events == ["removed:probe-stage", "lease-released"]
    assert not claude.available
    assert claude.capabilities == BackendCapabilities()
    assert "unsupported" in claude.reason
    assert registry.names() == ("claude", "codex", "fixture", "opencode")
    assert registry.get("FIXTURE").name == "fixture"


def test_artifact_transport_helpers_run_only_static_python_against_tmp_files(
    execution: DockerExecution,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "upload.bundle"
    source.write_bytes(b"bounded input bytes")
    result = tmp_path / "result.bundle"
    result.write_bytes(b"bounded result bytes")
    real_popen = subprocess.Popen
    redirected_targets: list[Path] = []

    def run_static_helper(command: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        assert isinstance(command, list)
        assert command[:2] == ["docker", "exec"]
        assert command[-4] == "-c"
        remote_path = command[-2]
        assert remote_path.startswith("/tmp/")
        local_path = tmp_path / Path(remote_path).name
        assert local_path.resolve().parent == tmp_path.resolve()
        redirected_targets.append(local_path)
        return real_popen(
            [sys.executable, "-I", "-c", command[-3], str(local_path), command[-1]],
            **kwargs,
        )

    monkeypatch.setattr(subprocess, "Popen", run_static_helper)

    execution._copy_file_into("fake-container", "/tmp/uploaded.bundle", source, maximum=1024)
    received = execution._read_container_file("fake-container", "/tmp/result.bundle", 1024)

    assert (tmp_path / "uploaded.bundle").read_bytes() == source.read_bytes()
    assert received == result.read_bytes()
    assert all(target.resolve().parent == tmp_path.resolve() for target in redirected_targets)

    with pytest.raises(DockerExecutionError, match="copy path is invalid"):
        execution._copy_file_into("fake-container", "/tmp/../outside", source, maximum=1024)
    with pytest.raises(DockerExecutionError, match="result file path is invalid"):
        execution._read_container_file("fake-container", "/tmp/other.bundle", 1024)
