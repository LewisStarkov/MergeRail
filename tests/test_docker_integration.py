"""Opt-in acceptance against a real local Docker engine and a pinned runtime."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from mergerail.backends import AgentReply, AgentSession, TurnRequest
from mergerail.config import AgentConfig, Config
from mergerail.detect import Check
from mergerail.execution.policy import ExecutionPolicy
from mergerail.runner import Runner
from mergerail.tasks import Status, Task
from tests.conftest import run

IMAGE = os.environ.get("MERGERAIL_TEST_DOCKER_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="set MERGERAIL_TEST_DOCKER_IMAGE to a pinned image"
)

DRIVER = r"""
import json, os, pathlib, sys
role = ""
for raw in sys.stdin:
    f = json.loads(raw)
    if f["type"] == "hello":
        print(json.dumps({"type":"hello", "protocol":1, "backend":"fixture", "capabilities":{
            "structured_output":True, "native_read_only":True, "native_push_denial":True,
            "exact_cost_reporting":True}}), flush=True)
    elif f["type"] == "open_session":
        role = f["role"]
        answer = {"type":"session_opened", "request_id":f["request_id"], "session_id":role}
        print(json.dumps(answer), flush=True)
    elif f["type"] == "turn":
        p = pathlib.Path("value.txt")
        if role == "fixer":
            p.write_text("fixed\n")
            text = "Changed value.txt"
            structured = None
        else:
            assert p.read_text() == "fixed\n", p.read_text()
            try:
                p.write_text("reviewer mutation\n")
            except PermissionError:
                pass
            else:
                raise AssertionError("reviewer source is writable")
            try:
                p.chmod(0o777)
            except PermissionError:
                pass
            else:
                raise AssertionError("reviewer can chmod source")
            text = "VERDICT: APPROVE"
            structured = {"approved":True, "reason":"checked immutable source"}
        print(json.dumps({"type":"result", "request_id":f["request_id"], "text":text,
            "structured":structured, "cost_usd":0, "is_error":False}), flush=True)
    elif f["type"] == "close_session":
        break
"""


def configuration(repo: Path, *, checks: list[Check] | None = None) -> Config:
    (repo / "value.txt").write_text("old\n")
    run("add", "value.txt", cwd=repo)
    run("commit", "-qm", "fixture input", cwd=repo)
    state = repo / ".mergerail"
    return Config(
        root=repo,
        base_branch="main",
        checks=checks or [],
        conventions=[],
        state_dir=state,
        worktree=state / "worktree",
        delivery="local",
        baseline_checks=False,
        fixer=AgentConfig(backend="fixture", timeout=30),
        reviewer=AgentConfig(backend="fixture", permission="review", timeout=30),
        external_backends={"fixture": ["python3", "-u", "-c", DRIVER]},
        execution=ExecutionPolicy(image=IMAGE),
    )


def test_fixer_checks_readonly_reviewer_and_delivery(repo: Path) -> None:
    checks = [
        Check(
            "side effects",
            [
                "python3",
                "-c",
                "from pathlib import Path; Path('value.txt').write_text('test mutation\\n')",
            ],
        )
    ]
    config = configuration(repo, checks=checks)
    runner = Runner(config, "folder", supervise=False)
    task = runner.store.add("fix value", source="test")
    runner.run(until=task.id)
    result = runner.store.get(task.id)
    assert result is not None and result.status == Status.DONE, result and result.note
    assert (repo / "value.txt").read_text() == "fixed\n"
    assert result.execution["backend"] == "docker"
    assert result.execution["base_sha"] != result.execution["result_sha"]
    assert run("rev-parse", "HEAD", cwd=repo) == result.approved_sha


def test_dirty_checkout_preserves_result_for_delivery_retry(repo: Path) -> None:
    config = configuration(repo)
    (repo / "README.md").write_text("user edits\n")
    runner = Runner(config, "folder", supervise=False)
    task = runner.store.add("fix value")
    runner.run(until=task.id)
    result = runner.store.get(task.id)
    assert result is not None and result.status == Status.BLOCKED, result and result.note
    assert (repo / "README.md").read_text() == "user edits\n"
    assert (repo / "value.txt").read_text() == "old\n"
    assert run("show", f"{result.approved_sha}:value.txt", cwd=repo) == "fixed"
    assert runner.store.retry_delivery(task.id) is not None
    (repo / "README.md").write_text("# project\n")
    runner = Runner(config, "folder", supervise=False)
    runner.run(until=task.id)
    result = runner.store.get(task.id)
    assert result is not None and result.status == Status.DONE, result and result.note
    assert (repo / "value.txt").read_text() == "fixed\n"


def test_failed_checks_leave_main_unchanged(repo: Path) -> None:
    config = configuration(repo, checks=[Check("fail", ["python3", "-c", "raise SystemExit(7)"])])
    config.max_rounds = 1
    base = run("rev-parse", "HEAD", cwd=repo)
    runner = Runner(config, "folder", supervise=False)
    task = runner.store.add("fix value")
    runner.run(until=task.id)
    result = runner.store.get(task.id)
    assert result is not None and result.status == Status.FAILED, result and result.note
    assert run("rev-parse", "HEAD", cwd=repo) == base
    assert (repo / "value.txt").read_text() == "old\n"
    assert run("show", f"{result.branch}:value.txt", cwd=repo) == "fixed"


def test_cancelled_checks_release_resources_for_next_stage(repo: Path) -> None:
    from mergerail.execution.docker import DockerExecution

    config = configuration(repo)
    assert config.execution is not None
    execution = DockerExecution(config.execution, repo, config.state_dir)
    execution.preflight()
    try:
        sha = run("rev-parse", "HEAD", cwd=repo)
        passed, report = execution.run_checks(
            [Check("long running", ["python3", "-c", "import time; time.sleep(120)"])],
            sha,
            cancelled=lambda: True,
        )
        assert not passed
        assert "cancel" in report.lower()
        assert not execution._containers
        assert not execution._networks
        passed, report = execution.run_checks(
            [Check("next stage", ["python3", "-c", "print('ready')"])], sha
        )
        assert passed, report
    finally:
        execution.close()


def test_running_orphan_is_removed_before_new_stage_without_losing_checkpoint(repo: Path) -> None:
    from mergerail.execution.docker import DockerExecution

    config = configuration(repo)
    assert config.execution is not None
    previous = DockerExecution(config.execution, repo, config.state_dir)
    current = DockerExecution(config.execution, repo, config.state_dir)
    try:
        previous.preflight()
        previous.set_task_id("interrupted")
        pending = previous._task_dir() / "recovery-interrupted.workspace.tar"
        pending.write_bytes(b"unverified checkpoint must be retained")
        orphan = previous._create_container(workspace_mib=1)
        assert previous._inspect_container(orphan)["State"]["Running"]

        lease = current._with_resource_lease()
        try:
            assert (
                current._docker("ps", "--quiet", "--filter", "label=com.mergerail.managed=true")
                == ""
            )
            assert pending.read_bytes() == b"unverified checkpoint must be retained"
            assert current.metadata["recovery"]["containers_removed"] == 1
        finally:
            lease.release()
    finally:
        current.close()
        previous.close()


def test_private_controller_filesystems_have_distinct_lease_owners(repo: Path) -> None:
    from mergerail.execution.docker import DockerExecution

    execution = DockerExecution(
        ExecutionPolicy(
            image=IMAGE,
            memory_mib=256,
            workspace_limit_mib=16,
            tmp_limit_mib=16,
            max_bundle_mib=1,
            cpus=0.1,
        ),
        repo,
        repo / ".mergerail",
    )
    try:
        runtime = execution._runtime_zip()
        containers = [execution._create_container(), execution._create_container()]
        identities = []
        for container in containers:
            execution._copy_into(container, "/tmp/runtime.zip", runtime, maximum=8 * 1024 * 1024)
            code = (
                "import os,sys,json;os.environ['HOME']='/tmp/private-home';"
                "sys.path.insert(0,'/tmp/runtime.zip');from pathlib import Path;"
                "from mergerail.execution.docker import DockerExecution;"
                "from mergerail.execution.policy import ExecutionPolicy;"
                f"e=DockerExecution(ExecutionPolicy(image={IMAGE!r}),"
                "Path('/work/repo'),Path('/tmp/state'));"
                "print(json.dumps({'owner':e._owner_id,'repo':e._repo_id,"
                "'lease':str(e._resource_lease_path)}))"
            )
            result = subprocess.run(
                ["docker", "exec", container, "python3", "-I", "-c", code],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            identities.append(json.loads(result.stdout))
        assert identities[0]["repo"] == identities[1]["repo"]
        assert identities[0]["lease"] == identities[1]["lease"]
        assert identities[0]["owner"] != identities[1]["owner"]
    finally:
        execution.close()


def test_advanced_base_is_merged_and_validated_inside_docker(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner(configuration(repo), "folder", supervise=False)
    original = runner._ask

    def advance_base(session: AgentSession, task: Task, request: TurnRequest) -> AgentReply | None:
        response = original(session, task, request)
        if session is runner.reviewer:
            (repo / "user-change.txt").write_text("concurrent commit\n")
            run("add", "user-change.txt", cwd=repo)
            run("commit", "-qm", "advance main", cwd=repo)
        return response

    monkeypatch.setattr(runner, "_ask", advance_base)
    task = runner.store.add("fix value while main advances")
    runner.run(until=task.id)
    result = runner.store.get(task.id)
    assert result is not None and result.status == Status.DONE, result and result.note
    head = run("rev-parse", "HEAD", cwd=repo)
    assert head != result.approved_sha
    assert run("merge-base", "--is-ancestor", result.approved_sha, head, cwd=repo) == ""
    assert (repo / "value.txt").read_text() == "fixed\n"
    assert (repo / "user-change.txt").read_text() == "concurrent commit\n"
