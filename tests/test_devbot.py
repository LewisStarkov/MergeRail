from __future__ import annotations

import json
from email.message import Message
from pathlib import Path
from urllib.request import Request

import pytest

from mergerail.config import AgentConfig, Config
from mergerail.delivery import LOCAL_MERGE
from mergerail.detect import Check
from mergerail.devbot import DevBotWebFront, check_scope, read_record
from mergerail.devbot_deploy import DevBotDeployer
from mergerail.execution.policy import ExecutionPolicy
from mergerail.tasks import DeliveryRecord, Status, Task, TaskStore
from tests.conftest import run


def publication(
    repo: Path, monkeypatch: pytest.MonkeyPatch, backend: str = "opencode"
) -> tuple[DevBotWebFront, Task, Path]:
    base = run("rev-parse", "HEAD", cwd=repo)
    (repo / "bot").mkdir()
    (repo / "bot/value.py").write_text("value = 2\n")
    run("add", "bot", cwd=repo)
    run("commit", "-qm", "reviewed change", cwd=repo)
    sha = run("rev-parse", "HEAD", cwd=repo)
    password = repo.parent / "password"
    password.write_text("test-password-that-is-long-enough")
    outbox = repo.parent / "outbox"
    monkeypatch.setenv("MERGERAIL_WEB_PASSWORD_FILE", str(password))
    monkeypatch.setenv("MERGERAIL_DEVBOT_OUTBOX", str(outbox))
    config = Config(
        root=repo,
        base_branch="main",
        checks=[Check("test", ["true"])],
        conventions=[],
        state_dir=repo / ".mergerail",
        worktree=repo / ".mergerail/worktree",
        delivery="local",
        baseline_mode="strict",
        fixer=AgentConfig(backend=backend),
        reviewer=AgentConfig(backend=backend),
        execution=ExecutionPolicy(image="sha256:" + "a" * 64, release_scope="rivals-dev"),
    )
    store = TaskStore(config.queue_path)
    task = store.add("change value")
    store.update(
        task.id,
        status=Status.DONE,
        attempts=1,
        approved_sha=sha,
        delivery=DeliveryRecord(status="succeeded", outcome=LOCAL_MERGE),
        execution={
            "backend": "docker",
            "validated": True,
            "base_sha": base,
            "delivered_sha": sha,
            "policy_digest": "test",
        },
    )
    saved = store.get(task.id)
    assert saved is not None
    front = DevBotWebFront(store, config)
    front.publish(saved)
    return front, saved, outbox / front.key(saved)


def test_codex_task_publishes_the_reviewed_sha(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, task, directory = publication(repo, monkeypatch, backend="codex")
    assert read_record(directory / "request.json")["sha"] == task.approved_sha


def deployer(repo: Path, outbox: Path, monkeypatch: pytest.MonkeyPatch) -> DevBotDeployer:
    monkeypatch.setattr("mergerail.devbot_deploy.CHECKOUT", repo)
    worker = DevBotDeployer(outbox, repo.parent / "deploy-state", repo)
    monkeypatch.setattr(worker, "approval", lambda _: None)
    monkeypatch.setattr("mergerail.devbot_deploy.prepare_images", lambda *_: None)
    return worker


def test_outbox_is_immutable_and_retry_does_not_requeue_fixer(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    front, task, directory = publication(repo, monkeypatch)
    original = (directory / "request.json").read_bytes()
    front.publish(task)
    assert (directory / "request.json").read_bytes() == original
    request = read_record(directory / "request.json")
    (directory / "status.json").write_text(json.dumps({**request, "status": "failed"}))
    assert front.act(task.id, "retry-deploy")[0] == 200
    saved = front.store.get(task.id)
    assert saved is not None and saved.status == Status.DONE and saved.attempts == 1
    assert saved.approved_sha == request["sha"]
    assert front.tasks_payload()["tasks"][0]["deployment"]["status"] == "failed"


def test_checksum_failure_does_not_move_checkout(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    front, task, directory = publication(repo, monkeypatch)
    (directory / "result.bundle").write_bytes(b"tampered")
    worker = deployer(repo, front.outbox, monkeypatch)
    with pytest.raises(ValueError, match="checksum"):
        worker.validate(directory)
    assert run("rev-parse", "HEAD", cwd=repo) == task.approved_sha


def test_older_result_cannot_replace_newer_main(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    front, _, directory = publication(repo, monkeypatch)
    (repo / "bot/value.py").write_text("value = 3\n")
    run("commit", "-qam", "newer result", cwd=repo)
    worker = deployer(repo, front.outbox, monkeypatch)
    monkeypatch.setattr(worker, "run_wrapper", lambda *_: pytest.fail("must not deploy stale SHA"))
    worker.deploy(directory)
    assert read_record(directory / "status.json")["status"] == "superseded"


def test_failure_recovers_previous_and_explicit_retry_uses_same_sha(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    front, task, directory = publication(repo, monkeypatch)
    request = read_record(directory / "request.json")
    run("reset", "--hard", request["base_sha"], cwd=repo)
    worker = deployer(repo, front.outbox, monkeypatch)
    live = {"sha": request["base_sha"], "fail": True}
    calls: list[list[str]] = []

    def wrapper(command: list[str], _: Path) -> int:
        calls.append(command)
        if "rollback.sh" in " ".join(command):
            live["sha"] = request["base_sha"]
            return 0
        assert command[-2:] == ["--expected-sha", task.approved_sha]
        live["sha"] = task.approved_sha
        return 1 if live["fail"] else 0

    monkeypatch.setattr(worker, "run_wrapper", wrapper)
    monkeypatch.setattr(worker, "verified", lambda: str(live["sha"]))
    monkeypatch.setattr(worker, "remote", lambda _: str(live["sha"]))
    monkeypatch.setattr(worker, "healthy", lambda sha: live["sha"] == sha)
    worker.deploy(directory)
    assert read_record(directory / "status.json")["status"] == "failed"
    assert live["sha"] == request["base_sha"]
    worker.deploy(directory)
    assert len(calls) == 2
    live["fail"] = False
    assert front.act(task.id, "retry-deploy")[0] == 200
    worker.deploy(directory)
    assert read_record(directory / "status.json")["status"] == "succeeded"
    assert live["sha"] == task.approved_sha
    assert len(calls) == 3
    states = [read_record(p)["status"] for p in sorted((directory / "history").glob("*.json"))]
    assert "failed" in states and states[-1] == "succeeded"


@pytest.mark.parametrize("candidate_healthy", [False, True])
def test_restart_after_cpd_promotion_preserves_working_rollback_target(
    repo: Path, monkeypatch: pytest.MonkeyPatch, candidate_healthy: bool
) -> None:
    front, task, directory = publication(repo, monkeypatch)
    request = read_record(directory / "request.json")
    worker = deployer(repo, front.outbox, monkeypatch)
    previous = request["base_sha"]
    worker.write_status(directory, request, status="running", previous_sha=previous)
    live = {"sha": task.approved_sha}
    calls: list[list[str]] = []

    def wrapper(command: list[str], _: Path) -> int:
        calls.append(command)
        if "rollback.sh" in " ".join(command):
            assert command[3] == previous
            assert command[-1] == task.approved_sha
            live["sha"] = previous
            return 0
        assert command[command.index("--expected-previous-sha") + 1] == task.approved_sha
        return 1

    monkeypatch.setattr(worker, "run_wrapper", wrapper)
    monkeypatch.setattr(worker, "verified", lambda: live["sha"])
    monkeypatch.setattr(worker, "remote", lambda _: live["sha"])
    monkeypatch.setattr(worker, "healthy", lambda sha: candidate_healthy or sha == previous)
    worker.deploy(directory)
    record = read_record(directory / "status.json")
    assert record["previous_sha"] == previous
    assert record["status"] == ("succeeded" if candidate_healthy else "failed")
    assert live["sha"] == (task.approved_sha if candidate_healthy else previous)
    assert len(calls) == (0 if candidate_healthy else 2)


@pytest.mark.parametrize("previous", [None, "invalid"])
def test_interrupted_rollout_without_saved_previous_blocks_queue(
    repo: Path, monkeypatch: pytest.MonkeyPatch, previous: str | None
) -> None:
    front, task, directory = publication(repo, monkeypatch)
    request = read_record(directory / "request.json")
    worker = deployer(repo, front.outbox, monkeypatch)
    worker.write_status(directory, request, status="running", previous_sha=previous)
    monkeypatch.setattr(worker, "verified", lambda: task.approved_sha)
    monkeypatch.setattr(worker, "run_wrapper", lambda *_: pytest.fail("unknown recovery target"))
    with pytest.raises(RuntimeError, match="saved previous SHA"):
        worker.tick()
    assert read_record(directory / "status.json")["status"] == "blocked"


def test_missing_rollback_image_cannot_trigger_server_build(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    front, _, directory = publication(repo, monkeypatch)
    worker = deployer(repo, front.outbox, monkeypatch)
    previous = read_record(directory / "request.json")["base_sha"]

    def missing(script: str) -> str:
        assert "docker image inspect" in script
        assert "rivals-dev-" in script
        raise RuntimeError("Retained rollback image missing; isolated preparation required")

    monkeypatch.setattr(worker, "remote", missing)
    monkeypatch.setattr(worker, "run_wrapper", lambda *_: pytest.fail("server build fallback"))
    with pytest.raises(RuntimeError, match="rollback image missing"):
        worker.rollback(previous, previous)


@pytest.mark.parametrize(
    "name",
    [
        "Dockerfile",
        "migrations/x.py",
        "scripts/deploy.sh",
        "miniapp/package.json",
        ".env.development",
    ],
)
def test_release_scope_refuses_infrastructure_and_schema(
    repo: Path,
    name: str,
) -> None:
    base = run("rev-parse", "HEAD", cwd=repo)
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n")
    run("add", name, cwd=repo)
    run("commit", "-qm", "unapproved scope", cwd=repo)
    with pytest.raises(ValueError, match="release scope"):
        check_scope(repo, base, run("rev-parse", "HEAD", cwd=repo))


def test_outbox_fifo_is_rejected_without_waiting(tmp_path: Path) -> None:
    import os

    path = tmp_path / "status.json"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="invalid deployment record"):
        read_record(path)


def test_refused_rollback_blocks_queue_and_preserves_server_evidence(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    front, task, directory = publication(repo, monkeypatch)
    request = read_record(directory / "request.json")
    worker = deployer(repo, front.outbox, monkeypatch)
    monkeypatch.setattr(worker, "run_wrapper", lambda *_: 1)
    monkeypatch.setattr(worker, "verified", lambda: request["base_sha"])
    monkeypatch.setattr(
        worker,
        "remote",
        lambda command: (
            request["base_sha"]
            if "awk" in command
            else request["base_sha"] + "\nrivals-dev-webapp candidate"
        ),
    )
    monkeypatch.setattr(worker, "healthy", lambda _: False)
    with pytest.raises(RuntimeError, match="recovery needs"):
        worker.tick()
    record = read_record(directory / "status.json")
    assert record["status"] == "blocked"
    assert "candidate" in record["server_state"]
    assert front.act(task.id, "retry-deploy")[0] == 409
    with pytest.raises(RuntimeError, match="operator inspection"):
        worker.tick()
    saved = front.store.get(task.id)
    assert saved is not None and saved.approved_sha == task.approved_sha


def test_outbox_cannot_substitute_runner_approval(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    front, task, directory = publication(repo, monkeypatch)
    monkeypatch.setattr("mergerail.devbot_deploy.CHECKOUT", repo)
    worker = DevBotDeployer(front.outbox, repo.parent / "deploy-state", repo)
    saved = worker.state / f"approval-{task.id}-{task.attempts}.json"
    record = task.to_dict()
    record["approved_sha"] = "a" * 40
    saved.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="runner approval"):
        worker.validate(directory)


def test_release_scope_refuses_mode_only_change(repo: Path) -> None:
    (repo / "bot").mkdir()
    path = repo / "bot/value.py"
    path.write_text("value = 1\n")
    run("add", ".", cwd=repo)
    run("commit", "-qm", "regular runtime file", cwd=repo)
    base = run("rev-parse", "HEAD", cwd=repo)
    run("update-index", "--chmod=+x", "bot/value.py", cwd=repo)
    run("commit", "-qm", "mode-only change", cwd=repo)
    with pytest.raises(ValueError, match="release scope"):
        check_scope(repo, base, run("rev-parse", "HEAD", cwd=repo))


def test_completed_recovery_archives_are_cleaned_only_after_publication(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    front, task, directory = publication(repo, monkeypatch)
    cache = front.state_dir / "docker-execution/tasks" / str(task.id)
    cache.mkdir(parents=True)
    archive = cache / "fixer-home.tar"
    archive.write_bytes(b"private transient snapshot")
    preserved = cache / "unrelated.txt"
    preserved.write_text("keep")
    front.publish(task)
    assert directory.joinpath("request.json").exists()
    assert not archive.exists()
    assert preserved.exists()


def test_rollout_deadline_keeps_external_process_and_blocks_queue(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    front, _, _ = publication(repo, monkeypatch)
    worker = deployer(repo, front.outbox, monkeypatch)
    finish = threading.Event()
    ended = threading.Event()

    def stalled(*_: object) -> int:
        finish.wait(2)
        ended.set()
        return 0

    monkeypatch.setattr(worker, "run_wrapper", stalled)
    with pytest.raises(RuntimeError, match="process left active"):
        worker.run_rollout(["checked-cpd"], repo / "log", deadline=0.01)
    assert not ended.is_set()
    finish.set()
    assert ended.wait(1)


def test_health_failure_reports_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    from mergerail.devbot_deploy import probe_health

    def denied(*_: object, **__: object) -> object:
        raise urllib.error.HTTPError(
            "https://dev.rivals.baby/health", 503, "unhealthy", Message(), None
        )

    monkeypatch.setattr("mergerail.devbot_deploy.urllib.request.urlopen", denied)
    assert probe_health("a" * 40) == (False, "health endpoint returned HTTP 503")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (json.dumps({"status": "ok", "revision": "a" * 40}).encode(), (True, "")),
        (
            b'{"status":"ok","revision":"old"}',
            (False, "health status/revision differs from the approved SHA"),
        ),
        (b"x" * (64 * 1024 + 1), (False, "health response exceeds 64 KiB")),
        (b"not-json", (False, "health request failed: JSONDecodeError")),
    ],
)
def test_health_requires_bounded_matching_revision(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, expected: tuple[bool, str]
) -> None:
    import io

    from mergerail.devbot_deploy import probe_health

    def respond(request: Request, **_: object) -> io.BytesIO:
        assert request.headers["User-agent"] == "Rivals-Deploy/1.0"
        return io.BytesIO(payload)

    monkeypatch.setattr("mergerail.devbot_deploy.urllib.request.urlopen", respond)
    assert probe_health("a" * 40) == expected


@pytest.mark.parametrize("exposes_sha", [False, True])
def test_rollback_health_revision_follows_target_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exposes_sha: bool
) -> None:
    import subprocess

    sha, tag = "a" * 40, "b" * 20
    release = tmp_path / "release"
    release.mkdir()
    (release / ".release.env").write_text(f"APP_TAG={tag}\nRIVALS_RELEASE_SHA={sha}\n")
    (release / "compose.prod.yml").write_text(
        "RIVALS_APP_REVISION: ${RIVALS_RELEASE_SHA:-${APP_TAG}}"
        if exposes_sha
        else "RIVALS_APP_REVISION: ${APP_TAG}"
    )
    monkeypatch.setattr("mergerail.devbot_deploy.CHECKOUT", tmp_path)
    worker = DevBotDeployer(tmp_path / "outbox", tmp_path / "state", tmp_path)

    def remote(script: str) -> str:
        return subprocess.check_output(
            ["sh", "-c", script.replace(f"/opt/rivals-dev-releases/{sha}", str(release))],
            text=True,
        ).strip()

    monkeypatch.setattr(worker, "remote", remote)
    monkeypatch.setattr(
        worker, "healthy", lambda revision: revision == (sha if exposes_sha else tag)
    )
    assert worker.healthy_release(sha)
