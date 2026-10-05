"""Transport and preflight boundary checks that do not require Docker Engine."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mergerail.execution import docker as docker_module
from mergerail.execution import gateway
from mergerail.execution.docker import MIB, DockerExecution, DockerExecutionError
from mergerail.execution.policy import ExecutionPolicy

IMAGE = "mergerail-runtime@sha256:" + "a" * 64
NATIVE_POPEN = subprocess.Popen
NATIVE_RUN = subprocess.run


@pytest.fixture
def execution(tmp_path: Path) -> DockerExecution:
    return DockerExecution(ExecutionPolicy(image=IMAGE), tmp_path / "project", tmp_path / "state")


def _install_static_export_process(
    monkeypatch: pytest.MonkeyPatch,
    payload: Path,
    *,
    returncode: int = 0,
    stderr_text: str = "",
) -> list[list[str]]:
    commands: list[list[str]] = []

    def start_static_process(
        command: list[str], *, stdout: Any, stderr: Any
    ) -> subprocess.Popen[bytes]:
        # Observe, but never execute, the Docker command/bootstrap. The child
        # below is a fixed Python reader for a test-owned temporary file.
        commands.append(command)
        assert command[:3] == ["docker", "exec", "-i"]
        assert command[9] == docker_module._EXPORT_BOOTSTRAP
        helper = (
            "import pathlib,sys;"
            "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes());"
            "sys.stderr.write(sys.argv[2]);"
            "raise SystemExit(int(sys.argv[3]))"
        )
        return NATIVE_POPEN(
            [sys.executable, "-I", "-c", helper, str(payload), stderr_text, str(returncode)],
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(subprocess, "Popen", start_static_process)
    return commands


def test_capture_tar_streams_only_a_bounded_static_export_and_keeps_bytes(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = tmp_path / "fixture.tar"
    expected = b"bounded test archive\x00" * 128
    payload.write_bytes(expected)
    commands = _install_static_export_process(monkeypatch, payload)

    artifact = execution._capture_tar("stage-1", "/work/repo", purpose="workspace")

    assert artifact.read_bytes() == expected
    assert len(commands) == 1
    assert commands[0][5] == "stage-1"
    assert commands[0][-3] == "/work/repo"
    assert int(commands[0][-2]) >= len(expected)
    if os.name != "nt":
        assert artifact.stat().st_mode & 0o777 == 0o600


def test_failed_capture_removes_partial_and_preserves_previous_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = DockerExecution(
        ExecutionPolicy(image=IMAGE, cache_limit_mib=1),
        tmp_path / "project",
        tmp_path / "state",
    )
    previous = execution._task_dir() / "recovery-last-good.workspace.tar"
    previous.write_bytes(b"previous unverified checkpoint")
    payload = tmp_path / "failed.tar"
    payload.write_bytes(b"partial replacement")
    _install_static_export_process(
        monkeypatch,
        payload,
        returncode=23,
        stderr_text="fixture export failed",
    )

    with pytest.raises(DockerExecutionError, match="fixture export failed"):
        execution._capture_tar("stage-1", "/work/repo", purpose="workspace")

    assert previous.read_bytes() == b"previous unverified checkpoint"
    assert list(execution._cache_dir.glob("workspace.*.partial")) == []


def test_capture_tar_stops_an_oversized_export_without_leaving_an_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = DockerExecution(
        ExecutionPolicy(image=IMAGE, cache_limit_mib=1),
        tmp_path / "project",
        tmp_path / "state",
    )
    maximum = MIB - 64 * 1024
    payload = tmp_path / "oversized.tar"
    payload.write_bytes(b"x" * (maximum + 1))
    killed: list[tuple[str, ...]] = []

    def docker(*args: str, **_kwargs: Any) -> str:
        killed.append(args)
        return ""

    monkeypatch.setattr(
        execution,
        "_docker",
        docker,
    )
    _install_static_export_process(monkeypatch, payload)

    with pytest.raises(DockerExecutionError, match="exceeded the configured size limit"):
        execution._capture_tar("stage-1", "/work/repo", purpose="workspace")

    assert ("kill", "stage-1") in killed
    assert list(execution._cache_dir.glob("workspace.*.partial")) == []


def test_network_creation_rejects_nonisolated_result_and_removes_owned_network(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_calls: list[tuple[str, ...]] = []
    inspect_count = 0

    def docker_json(*args: str, **_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal inspect_count
        assert args[:2] == ("network", "inspect")
        inspect_count += 1
        if inspect_count == 1:
            return [{"Internal": False, "EnableIPv6": False, "Options": {}}]
        return [
            {
                "Labels": {
                    "com.mergerail.managed": "true",
                    "com.mergerail.repo": execution._repo_id,
                    "com.mergerail.instance": execution._instance_id,
                }
            }
        ]

    monkeypatch.setattr(execution, "_docker_json", docker_json)

    def docker(*args: str, **_kwargs: Any) -> str:
        docker_calls.append(args)
        return ""

    monkeypatch.setattr(
        execution,
        "_docker",
        docker,
    )

    with pytest.raises(DockerExecutionError, match="internal IPv4-only AI network"):
        execution._create_network()

    assert inspect_count == 2
    assert any(call[:2] == ("network", "create") and "--internal" in call for call in docker_calls)
    assert any(call[:2] == ("network", "rm") for call in docker_calls)
    assert execution._networks == set()


@pytest.mark.parametrize(
    ("unsafe_setting", "message"),
    [
        ("named_mount", "host or named-volume mount"),
        ("capabilities", "drop all capabilities or enable NNP"),
        ("no_nnp", "drop all capabilities or enable NNP"),
    ],
)
def test_active_probe_refuses_unsafe_engine_inspection_before_worker(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_setting: str,
    message: str,
) -> None:
    policy = execution.policy
    name = "mergerail-preflight-fixed"
    record: dict[str, Any] = {
        "Config": {
            "Labels": {
                "com.mergerail.managed": "true",
                "com.mergerail.repo": execution._repo_id,
                "com.mergerail.instance": execution._instance_id,
            }
        },
        "HostConfig": {
            "Memory": policy.memory_mib * MIB,
            "MemorySwap": policy.memory_mib * MIB,
            "CpuQuota": int(policy.cpus * 100_000),
            "CpuPeriod": 100_000,
            "PidsLimit": policy.pids_limit,
            "ReadonlyRootfs": True,
            "NetworkMode": "none",
            "Binds": [],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Tmpfs": {
                "/work": "rw,nosuid,nodev,exec,size=1m,mode=1777",
                "/tmp": f"rw,nosuid,nodev,noexec,size={policy.tmp_limit_mib}m,mode=1777",
            },
        },
        "Mounts": [],
    }
    if unsafe_setting == "named_mount":
        record["Mounts"] = [{"Type": "volume", "Source": "untrusted", "Destination": "/work"}]
    elif unsafe_setting == "capabilities":
        record["HostConfig"]["CapDrop"] = []
    else:
        record["HostConfig"]["SecurityOpt"] = ["seccomp=builtin"]

    docker_calls: list[tuple[str, ...]] = []

    def docker(*args: str, **_kwargs: Any) -> str:
        docker_calls.append(args)
        return ""

    monkeypatch.setattr(execution, "_new_name", lambda _prefix: name)
    monkeypatch.setattr(
        execution,
        "_docker",
        docker,
    )
    monkeypatch.setattr(execution, "_inspect_container", lambda _name: record)
    monkeypatch.setattr(
        execution,
        "_copy_into",
        lambda *_args, **_kwargs: pytest.fail("worker runtime was copied after unsafe inspect"),
    )
    monkeypatch.setattr(
        execution,
        "_worker",
        lambda *_args, **_kwargs: pytest.fail("worker ran after unsafe inspect"),
    )

    with pytest.raises(DockerExecutionError, match=message):
        execution._active_preflight_probe()

    assert docker_calls[0][0] == "create"
    assert docker_calls[1] == ("start", name)
    assert docker_calls[-1] == ("rm", "--force", name)
    assert name not in execution._containers


def _gateway_boundaries(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
    *,
    readiness: Any,
) -> tuple[list[tuple[str, ...]], list[str]]:
    commands: list[tuple[str, ...]] = []
    removed: list[str] = []

    def docker(*args: str, **_kwargs: Any) -> str:
        commands.append(args)
        return ""

    monkeypatch.setattr(execution, "_create_container", lambda **_kwargs: "gateway-stage")
    monkeypatch.setattr(execution, "_runtime_zip", lambda: b"fixture-runtime")
    monkeypatch.setattr(execution, "_copy_into", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        execution,
        "_inspect_container",
        lambda _name: {
            "NetworkSettings": {"Networks": {"ai-private": {"IPAddress": "172.30.0.3"}}}
        },
    )
    monkeypatch.setattr(
        execution,
        "_docker",
        docker,
    )
    monkeypatch.setattr(execution, "_remove_container", removed.append)
    monkeypatch.setattr(subprocess, "run", readiness)
    return commands, removed


def test_gateway_bootstrap_environment_passes_the_consumer_proxy_contract(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ready(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    commands, removed = _gateway_boundaries(execution, monkeypatch, readiness=ready)

    assert execution._start_gateway("ai-private") == ("gateway-stage", "172.30.0.3")
    startup = next(call for call in commands if call[:2] == ("exec", "-d"))
    code = startup[startup.index("-c") + 1]
    import_boundary = ";from mergerail.execution.gateway import main;"
    assert import_boundary in code
    environment_prefix = code.split(import_boundary, maxsplit=1)[0]
    report = environment_prefix + ";import json;print(json.dumps(dict(os.environ)))"
    child = NATIVE_RUN(
        [sys.executable, "-I", "-c", report],
        capture_output=True,
        check=True,
        text=True,
        timeout=15,
    )
    environment = json.loads(child.stdout)
    gateway.check_proxy_environment(environment)

    assert "NO_PROXY" not in environment
    assert "no_proxy" not in environment
    assert startup[:6] == ("exec", "-d", "--user", "65534:0", "gateway-stage", "python3")
    assert commands.count(("network", "connect", "bridge", "gateway-stage")) == 1
    assert removed == []


@pytest.mark.parametrize("timeout", [False, True], ids=["nonzero-readiness", "timeout"])
def test_gateway_readiness_failure_removes_the_started_sidecar(
    execution: DockerExecution,
    monkeypatch: pytest.MonkeyPatch,
    timeout: bool,
) -> None:
    def readiness(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        if timeout:
            raise subprocess.TimeoutExpired(["docker", "exec"], 35)
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"probe failed")

    commands, removed = _gateway_boundaries(execution, monkeypatch, readiness=readiness)

    message = "readiness probe timed out" if timeout else "gateway did not become ready"
    with pytest.raises(DockerExecutionError, match=message):
        execution._start_gateway("ai-private")

    assert any(call[:2] == ("exec", "-d") for call in commands)
    assert removed == ["gateway-stage"]
