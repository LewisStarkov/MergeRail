"""Behavioral checks for Docker controller boundaries without a Docker Engine."""

from __future__ import annotations

import io
import json
import platform
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from mergerail.backends.base import BackendInfo, NullEventSink, SessionSpec, TurnRequest
from mergerail.execution import docker
from mergerail.execution.docker import MIB, DockerExecution, DockerExecutionError
from mergerail.execution.policy import ExecutionPolicy

IMAGE = "mergerail-runtime@sha256:" + "a" * 64


def test_other_repository_orphan_blocks_new_stage_and_releases_lease(
    execution: DockerExecution, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(execution, "_resource_lease_path", tmp_path / "resource.lock")
    monkeypatch.setattr(execution, "_docker", lambda *args, **kw: "foreign")
    monkeypatch.setattr(
        execution,
        "_inspect_container",
        lambda name: {
            "Config": {
                "Labels": {
                    "com.mergerail.managed": "true",
                    "com.mergerail.repo": "another-repo",
                }
            }
        },
    )
    with pytest.raises(DockerExecutionError, match="orphaned"):
        execution._with_resource_lease()
    monkeypatch.setattr(execution, "_docker", lambda *args, **kw: "")
    lease = execution._with_resource_lease()
    lease.release()


@pytest.mark.parametrize("cleanup_succeeds", [True, False])
def test_own_orphan_is_reconciled_before_new_stage_and_failed_cleanup_blocks(
    execution: DockerExecution,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_succeeds: bool,
) -> None:
    monkeypatch.setattr(execution, "_resource_lease_path", tmp_path / "resource.lock")
    pending = execution._task_dir() / "recovery-before-crash.workspace.tar"
    pending.write_bytes(b"last recoverable input")
    running = True
    reconciled: list[bool] = []

    def docker(*_args: str, **_kwargs: Any) -> str:
        return "own-orphan" if running else ""

    def reconcile() -> dict[str, Any]:
        nonlocal running
        reconciled.append(True)
        if cleanup_succeeds:
            running = False
        return {}

    monkeypatch.setattr(execution, "_docker", docker)
    monkeypatch.setattr(execution, "_reconcile_resources", reconcile)
    monkeypatch.setattr(
        execution,
        "_inspect_container",
        lambda _name: {
            "Config": {
                "Labels": {
                    "com.mergerail.managed": "true",
                    "com.mergerail.repo": execution._repo_id,
                    "com.mergerail.owner": execution._owner_id,
                }
            },
        },
    )
    if cleanup_succeeds:
        lease = execution._with_resource_lease()
        lease.release()
    else:
        with pytest.raises(DockerExecutionError, match="remain active"):
            execution._with_resource_lease()
        running = False
        lease = execution._with_resource_lease()
        lease.release()
    assert reconciled == [True]
    assert pending.read_bytes() == b"last recoverable input"


@pytest.fixture(autouse=True)
def clean_docker_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("owner", [None, "another-user"])
def test_same_repository_container_with_foreign_owner_is_never_removed(
    execution: DockerExecution, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str | None
) -> None:
    monkeypatch.setattr(execution, "_resource_lease_path", tmp_path / "resource.lock")
    monkeypatch.setattr(execution, "_docker", lambda *_args, **_kwargs: "foreign-instance")
    monkeypatch.setattr(
        execution,
        "_inspect_container",
        lambda _name: {
            "Config": {
                "Labels": {
                    "com.mergerail.managed": "true",
                    "com.mergerail.repo": execution._repo_id,
                    "com.mergerail.owner": owner,
                }
            }
        },
    )
    monkeypatch.setattr(
        execution, "_reconcile_resources", lambda: pytest.fail("foreign owner removed")
    )
    with pytest.raises(DockerExecutionError, match="unknown owner"):
        execution._with_resource_lease()
    monkeypatch.setattr(execution, "_docker", lambda *_args, **_kwargs: "")
    lease = execution._with_resource_lease()
    lease.release()


@pytest.fixture
def execution(tmp_path: Path) -> DockerExecution:
    return DockerExecution(ExecutionPolicy(image=IMAGE), tmp_path / "project", tmp_path / "state")


def test_metadata_tracks_a_safe_task_id_and_returns_a_copy(execution: DockerExecution) -> None:
    execution.set_task_id("task-42.review")

    snapshot = execution.metadata
    snapshot["task_id"] = "changed"
    assert execution.metadata["task_id"] == "task-42.review"
    persisted = json.loads((execution._cache_dir / "metadata.json").read_text(encoding="utf-8"))
    assert persisted["task_id"] == "task-42.review"


@pytest.mark.parametrize("task_id", [".", "..", "../outside", "nested/name", "x" * 65, "bad\x00id"])
def test_task_id_rejection_does_not_change_metadata_or_create_paths(
    execution: DockerExecution, task_id: str
) -> None:
    before = execution.metadata

    with pytest.raises(DockerExecutionError, match="task id"):
        execution.set_task_id(task_id)

    assert execution.metadata == before
    assert not (execution._cache_dir / "tasks").exists()


def test_context_rejects_a_remote_daemon_endpoint(
    monkeypatch: pytest.MonkeyPatch, execution: DockerExecution
) -> None:
    monkeypatch.setattr("mergerail.execution.docker.platform.system", lambda: "Linux")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")
    calls: list[tuple[str, ...]] = []

    def inspect(*args: str, **_kwargs: Any) -> list[dict[str, Any]]:
        calls.append(args)
        return [{"Endpoints": {"docker": {"Host": "tcp://daemon.example:2376"}}}]

    monkeypatch.setattr(execution, "_docker_json", inspect)
    with pytest.raises(DockerExecutionError, match="not a supported local Unix socket"):
        execution._context_host()
    assert calls == [("context", "inspect", "remote")]


def test_context_rejects_unsupported_host_platform_before_running_docker(
    monkeypatch: pytest.MonkeyPatch, execution: DockerExecution
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        execution,
        "_docker",
        lambda *_args, **_kwargs: pytest.fail("Docker CLI ran"),
    )

    with pytest.raises(DockerExecutionError, match="native Linux and macOS"):
        execution._context_host()


def _image_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "Id": "sha256:" + "a" * 64,
        "RepoDigests": [],
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {"Volumes": None, "Env": []},
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"Id": "sha256:" + "b" * 64}, "identity does not match"),
        ({"Os": "windows"}, "must target Linux"),
        ({"Architecture": "arm64"}, "architecture does not match"),
        ({"Config": {"Volumes": {"/work": {}}, "Env": []}}, "declared volumes"),
        ({"Config": {"Volumes": None, "Env": ["API_TOKEN=embedded"]}}, "credential or proxy"),
    ],
)
def test_image_preflight_rejects_untrusted_image_configuration(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    message: str,
) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        execution,
        "_docker_json",
        lambda *_args, **_kwargs: [_image_record(**overrides)],
    )

    with pytest.raises(DockerExecutionError, match=message):
        execution._inspect_image(IMAGE)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"OSType": "windows"}, "must run Linux containers"),
        ({"ServerVersion": "27.9.0"}, "Engine 28 or newer"),
        ({"CgroupVersion": "1"}, "cgroup v2"),
        ({"Architecture": "arm64"}, "not native to this machine"),
        ({"NCPU": 0}, "configured CPU or memory capacity"),
        ({"MemTotal": 1024}, "configured CPU or memory capacity"),
        ({"SecurityOptions": []}, "seccomp support"),
    ],
)
def test_preflight_refuses_daemons_without_required_isolation(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    message: str,
) -> None:
    info: dict[str, Any] = {
        "OSType": "linux",
        "ServerVersion": "28.0.0",
        "CgroupVersion": "2",
        "Architecture": "x86_64",
        "NCPU": 8,
        "MemTotal": 8 * MIB * 1024,
        "SecurityOptions": ["name=seccomp,profile=builtin"],
    }
    info.update(overrides)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "amd64")
    monkeypatch.setattr(execution, "_context_host", lambda: "unix:///var/run/docker.sock")
    monkeypatch.setattr(
        docker,
        "host_git",
        lambda _root, *args, **_kwargs: "git version 2.48.0" if args == ("--version",) else "files",
    )
    monkeypatch.setattr(execution, "_docker_json", lambda *_args, **_kwargs: info)
    monkeypatch.setattr(
        execution,
        "_inspect_image",
        lambda _image: pytest.fail("image inspection followed a rejected daemon"),
    )

    with pytest.raises(DockerExecutionError, match=message):
        execution.preflight()
    assert execution.metadata["validated"] is False


def test_container_options_apply_limits_and_keep_stage_storage_ephemeral(
    execution: DockerExecution,
) -> None:
    policy = execution.policy
    online_agent = execution._container_options(
        name="stage", network="none", workspace_mib=37, include_gateway_host="172.20.0.2"
    )
    gateway = execution._container_options(name="gateway", gateway=True, image=policy.gateway_image)
    offline = execution._container_options(name="offline", workspace_mib=37)

    def option(args: list[str], name: str) -> str:
        return args[args.index(name) + 1]

    def tmp_mib(args: list[str]) -> int:
        value = next(
            args[index + 1]
            for index, item in enumerate(args[:-1])
            if item == "--tmpfs" and args[index + 1].startswith("/tmp:")
        )
        size = next(part[5:-1] for part in value.split(",") if part.startswith("size="))
        return int(size)

    total_cpu = int(policy.cpus * 100_000)
    gateway_cpu = max(1000, min(10_000, total_cpu // 10))
    agent_memory = int(option(online_agent, "--memory")) // MIB
    gateway_memory = int(option(gateway, "--memory")) // MIB
    agent_cpu = int(option(online_agent, "--cpu-quota"))
    allocated_gateway_cpu = int(option(gateway, "--cpu-quota"))
    agent_pids = int(option(online_agent, "--pids-limit"))
    gateway_pids = int(option(gateway, "--pids-limit"))
    agent_tmp = tmp_mib(online_agent)
    gateway_tmp = tmp_mib(gateway)

    assert gateway_memory == 256
    assert gateway_cpu == allocated_gateway_cpu
    assert gateway_pids == 32
    assert gateway_tmp == 16
    assert agent_memory == policy.memory_mib - gateway_memory
    assert agent_cpu == total_cpu - allocated_gateway_cpu
    assert agent_pids == policy.pids_limit - gateway_pids
    assert agent_tmp == policy.tmp_limit_mib - gateway_tmp
    assert agent_memory + gateway_memory == policy.memory_mib
    assert agent_cpu + allocated_gateway_cpu == total_cpu
    assert agent_pids + gateway_pids == policy.pids_limit
    assert agent_tmp + gateway_tmp == policy.tmp_limit_mib

    assert option(offline, "--memory") == str(policy.memory_mib * MIB)
    assert option(offline, "--memory-swap") == option(offline, "--memory")
    assert option(offline, "--cpu-quota") == str(total_cpu)
    assert option(offline, "--pids-limit") == str(policy.pids_limit)
    assert tmp_mib(offline) == policy.tmp_limit_mib
    assert option(online_agent, "--memory-swap") == option(online_agent, "--memory")
    assert option(online_agent, "--network") == "none"
    tmpfs = [
        online_agent[index + 1]
        for index, value in enumerate(online_agent[:-1])
        if value == "--tmpfs"
    ]
    assert any(item.startswith("/work:rw,nosuid,nodev,exec,size=37m,") for item in tmpfs)
    assert "--read-only" in online_agent
    assert "--cap-drop" in online_agent and option(online_agent, "--cap-drop") == "ALL"
    assert "--mount" not in online_agent and "-v" not in online_agent
    assert "mergerail-ai:172.20.0.2" in online_agent
    labels = [
        online_agent[index + 1]
        for index, value in enumerate(online_agent[:-1])
        if value == "--label"
    ]
    assert "com.mergerail.managed=true" in labels
    assert f"com.mergerail.repo={execution._repo_id}" in labels
    assert f"com.mergerail.instance={execution._instance_id}" in labels


def test_online_limits_reject_a_valid_policy_without_room_for_the_gateway(
    tmp_path: Path,
) -> None:
    policy = ExecutionPolicy(
        image=IMAGE,
        memory_mib=256,
        pids_limit=16,
        workspace_limit_mib=1,
        tmp_limit_mib=1,
        max_bundle_mib=1,
    )
    policy.validate()
    execution = DockerExecution(policy, tmp_path / "project", tmp_path / "state")

    with pytest.raises(DockerExecutionError, match="cannot fit the online agent and gateway"):
        execution._online_resource_limits()


def test_gateway_container_does_not_receive_a_workspace_mount(execution: DockerExecution) -> None:
    args = execution._container_options(
        name="gateway", gateway=True, image=execution.policy.gateway_image
    )
    tmpfs = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--tmpfs"]
    assert all(not item.startswith("/work:") for item in tmpfs)
    assert any(item.startswith("/tmp:") for item in tmpfs)


def test_artifact_size_and_total_cache_quotas_are_enforced(tmp_path: Path) -> None:
    limited = DockerExecution(
        ExecutionPolicy(image=IMAGE, cache_limit_mib=1), tmp_path / "project", tmp_path / "state"
    )
    task_dir = limited._task_dir()

    with pytest.raises(DockerExecutionError, match="configured byte limit"):
        limited._store_artifact(task_dir, "too-large.bin", b"x" * 5, limit=4)
    with pytest.raises(DockerExecutionError, match="cache limit reached"):
        limited._store_artifact(task_dir, "over-cache.bin", b"x" * (MIB + 1), limit=2 * MIB)
    assert not (task_dir / "too-large.bin").exists()
    assert not (task_dir / "over-cache.bin").exists()


def _symlink_or_skip(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")


def test_artifact_cache_refuses_a_symlink_anywhere_in_the_cache(
    execution: DockerExecution, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"valuable")
    _symlink_or_skip(execution._cache_dir / "alias", outside)

    with pytest.raises(DockerExecutionError, match="unexpected symlink"):
        execution._cache_used_bytes()


def test_promoting_an_artifact_refuses_a_symlink_target(
    execution: DockerExecution, tmp_path: Path
) -> None:
    task_dir = execution._task_dir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve")
    _symlink_or_skip(task_dir / "artifact.tar", outside)
    source = tmp_path / "source.tar"
    source.write_bytes(b"new")

    with pytest.raises(DockerExecutionError, match="target must not be a symlink"):
        execution._promote_artifact(task_dir, "artifact.tar", source, limit=100)
    assert outside.read_bytes() == b"preserve"
    assert source.exists()


def test_recovery_removes_only_managed_resources_for_this_repository(
    execution: DockerExecution, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_id = execution._repo_id
    calls: list[tuple[str, ...]] = []
    containers = {
        "old-owned": {
            "Config": {
                "Labels": {
                    "com.mergerail.managed": "true",
                    "com.mergerail.repo": repo_id,
                    "com.mergerail.instance": "previous-instance",
                    "com.mergerail.owner": execution._owner_id,
                }
            }
        },
        "other-repo": {
            "Config": {
                "Labels": {
                    "com.mergerail.managed": "true",
                    "com.mergerail.repo": "another-repository",
                    "com.mergerail.instance": "another-instance",
                }
            }
        },
        "unmanaged": {"Config": {"Labels": {"com.mergerail.repo": repo_id}}},
    }
    networks = {
        "old-owned-net": {
            "Labels": {
                "com.mergerail.managed": "true",
                "com.mergerail.repo": repo_id,
                "com.mergerail.instance": "previous-instance",
                "com.mergerail.owner": execution._owner_id,
            }
        },
        "other-repo-net": {
            "Labels": {
                "com.mergerail.managed": "true",
                "com.mergerail.repo": "another-repository",
                "com.mergerail.instance": "another-instance",
            }
        },
    }

    def run(*args: str, **_kwargs: Any) -> str:
        calls.append(args)
        if args[:3] == ("ps", "--all", "--quiet"):
            # Docker's label filter is represented by this fixture; inspection
            # still verifies ownership before cleanup.
            return "old-owned\nother-repo\nunmanaged"
        if args[:3] == ("network", "ls", "--quiet"):
            return "old-owned-net\nother-repo-net"
        return ""

    def inspect(*args: str, **_kwargs: Any) -> list[dict[str, Any]]:
        if args[0] == "inspect":
            return [containers[args[1]]]
        if args[:2] == ("network", "inspect"):
            return [networks[args[2]]]
        raise AssertionError(args)

    monkeypatch.setattr(execution, "_docker", run)
    monkeypatch.setattr(execution, "_docker_json", inspect)

    result = execution._reconcile_resources()

    removals = [call for call in calls if call[:2] == ("rm", "--force")]
    network_removals = [call for call in calls if call[:2] == ("network", "rm")]
    assert removals == [("rm", "--force", "old-owned")]
    assert network_removals == [("network", "rm", "old-owned-net")]
    assert result["recovery"]["containers_removed"] == 1
    assert result["recovery"]["networks_removed"] == 1


class _FakeProcess:
    def __init__(self, output: bytes, *, stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(output)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"not-json\n", "malformed JSON"),
        (b"[]\n", "non-object JSON"),
        (b'{"type":"unexpected"}\n', "unknown frame"),
    ],
)
def test_worker_rejects_malformed_or_unknown_protocol_frames(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    message: str,
) -> None:
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: _FakeProcess(output))
    monkeypatch.setattr(execution, "_docker", lambda *_args, **_kwargs: "")

    with pytest.raises(DockerExecutionError, match=message):
        execution._worker("stage", {"mode": "probe"})


def test_worker_requires_a_result_frame_even_after_clean_exit(
    execution: DockerExecution, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: _FakeProcess(b""))

    with pytest.raises(DockerExecutionError, match="failed: worker exited with status 0"):
        execution._worker("stage", {"mode": "probe"})


class _BlockingStdout:
    def __init__(self, stopped: threading.Event) -> None:
        self.stopped = stopped

    def readline(self, _limit: int) -> bytes:
        self.stopped.wait(timeout=2)
        return b""


class _CancellableProcess:
    def __init__(self, stopped: threading.Event) -> None:
        self.stopped = stopped
        self.stdin = io.BytesIO()
        self.stdout = _BlockingStdout(stopped)
        self.stderr = io.BytesIO()
        self.returncode = -9

    def poll(self) -> int | None:
        return self.returncode if self.stopped.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        if not self.stopped.wait(timeout):
            raise TimeoutError("fake worker was not stopped")
        return self.returncode


def test_worker_cancellation_preserves_the_last_durable_checkpoint(
    execution: DockerExecution, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = threading.Event()
    process = _CancellableProcess(stopped)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(execution, "_docker", lambda *_args, **_kwargs: stopped.set())

    with pytest.raises(
        DockerExecutionError,
        match="cancelled; the last durable checkpoint is preserved",
    ):
        execution._worker("stage", {"mode": "turn"}, cancelled=lambda: True, timeout=10)


def test_reviewer_turn_resumes_session_then_close_prevents_more_turns(
    execution: DockerExecution, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha = "a" * 40
    result_sha = "b" * 40
    execution._preflight_result = {"ready": True}
    execution._base_sha = base_sha
    execution._branch = "mergerail-candidate/review"
    monkeypatch.setattr(execution.worktree, "head", lambda: result_sha)
    monkeypatch.setattr(execution, "probe", lambda name: BackendInfo(name, True))

    class Lease:
        def release(self) -> None:
            pass

    monkeypatch.setattr(execution, "_with_resource_lease", lambda: Lease())
    monkeypatch.setattr(execution, "_start_stage", lambda *_args, **_kwargs: ("stage", b"", {}))
    monkeypatch.setattr(execution, "_prepare_stage", lambda *_args, **_kwargs: None)
    requests: list[dict[str, Any]] = []

    def worker(_container: str, request: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        requests.append(request)
        return {"reply": {"text": "reviewed", "session_id": "continued-session"}}

    monkeypatch.setattr(execution, "_worker", worker)
    removed: list[str] = []

    def remove_container(name: str) -> None:
        removed.append(name)
        execution._active_container = None

    monkeypatch.setattr(execution, "_remove_container", remove_container)
    session = docker.DockerAgentSession(
        execution,
        "review-bot",
        SessionSpec(role="reviewer", cwd=Path("/work/repo"), resume_session_id="prior-session"),
        NullEventSink(),
    )

    reply = session.ask(TurnRequest("Review this change"))

    assert reply.text == "reviewed"
    assert session.session_id == "continued-session"
    assert requests[0]["resume_session_id"] == "prior-session"
    assert removed == ["stage"]
    session.close()
    assert session.session_id is None

    def fail_closed_turn(*_args: Any) -> Any:
        pytest.fail("closed session ran a turn")

    monkeypatch.setattr(execution, "_agent_turn", fail_closed_turn)
    closed = session.ask(TurnRequest("another turn"))
    assert closed.is_error
    assert "closed" in closed.text


def test_selected_free_model_metadata_is_filtered_before_forwarding(
    execution: DockerExecution, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    model_dir = tmp_path / ".cache" / "opencode"
    model_dir.mkdir(parents=True)
    entry = {
        "name": "n" * 250,
        "cost": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "secret": 9},
        "limit": {"context": 32000, "output": 4000, "unsupported": 3, "bad": True},
        "tool_call": True,
        "reasoning": "unexpected",
        "attachment": False,
        "temperature": True,
        "modalities": {
            "input": ["text", 1, "image"],
            "output": ["text"],
            "internal": ["secret"],
        },
        "provider": {"api_key": "discard-me"},
    }
    (model_dir / "models.json").write_text(
        json.dumps({"opencode": {"models": {"space-bunny-free": entry}}}), encoding="utf-8"
    )

    definition = execution._selected_model_definition()

    assert set(definition) == {
        "name",
        "cost",
        "limit",
        "tool_call",
        "attachment",
        "temperature",
        "modalities",
    }
    assert len(definition["name"]) == 200
    assert definition["cost"] == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    assert definition["limit"] == {"context": 32000, "output": 4000}
    assert definition["modalities"] == {"input": ["text", "image"], "output": ["text"]}


@pytest.mark.parametrize("price", [0.01, True, "0"])
def test_selected_model_must_be_proved_free(
    execution: DockerExecution, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, price: object
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    model_dir = tmp_path / ".cache" / "opencode"
    model_dir.mkdir(parents=True)
    (model_dir / "models.json").write_text(
        json.dumps(
            {"opencode": {"models": {"space-bunny-free": {"cost": {"input": price, "output": 0}}}}}
        ),
        encoding="utf-8",
    )

    with pytest.raises(DockerExecutionError, match="not proved free"):
        execution._selected_model_definition()
