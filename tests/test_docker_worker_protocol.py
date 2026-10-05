"""Cover the worker RPC and export boundaries without starting Docker or CLIs."""

from __future__ import annotations

import errno
import io
import json
import os
import signal
import sys
import tarfile
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mergerail.backends import BackendCapabilities, BackendInfo
from mergerail.backends.base import AgentReply, EventSink, SessionSpec, TurnRequest, Usage
from mergerail.execution import worker


class _OutputBuffer:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, value: bytes) -> int:
        self.data.extend(value)
        return len(value)

    def flush(self) -> None:
        pass

    def getvalue(self) -> bytes:
        return bytes(self.data)


class _Console:
    def __init__(self, request: bytes) -> None:
        self.input = io.BytesIO(request)
        self.output = _OutputBuffer()
        self.stderr = io.StringIO()
        self.streams = SimpleNamespace(
            stdin=SimpleNamespace(buffer=self.input),
            stdout=SimpleNamespace(buffer=self.output),
            stderr=self.stderr,
        )


def _install_console(monkeypatch: pytest.MonkeyPatch, request: bytes) -> _Console:
    console = _Console(request)
    monkeypatch.setattr(worker, "sys", console.streams)
    # Never expose or mutate the user's process environment in worker tests.
    monkeypatch.setattr(os, "environ", {})
    return console


class _Session:
    def __init__(
        self,
        events: EventSink | None,
        *,
        event_frames: list[Mapping[str, Any]] | None = None,
        fail: bool = False,
    ) -> None:
        self.events = events
        self.event_frames = event_frames or []
        self.fail = fail
        self.closed = False
        self.request: TurnRequest | None = None

    def ask(self, request: TurnRequest) -> AgentReply:
        self.request = request
        if self.events is not None:
            for event in self.event_frames:
                self.events.emit(event)
        if self.fail:
            raise RuntimeError("fixture backend failed")
        return AgentReply(
            text="review complete",
            is_error=False,
            cost_usd=0.0,
            context_tokens=7,
            seconds=1.25,
            structured={"ok": True},
            session_id="fixture-session",
            usage=Usage(input_tokens=4, output_tokens=3),
        )

    def close(self) -> None:
        self.closed = True


class _Registry:
    def __init__(
        self,
        *,
        events: list[Mapping[str, Any]] | None = None,
        fail: bool = False,
    ) -> None:
        self.events = events or []
        self.fail = fail
        self.registered: list[Any] = []
        self.name: str | None = None
        self.spec: SessionSpec | None = None
        self.session: _Session | None = None

    def register_external(self, backend: Any, *, replace: bool = False) -> None:
        del replace
        self.registered.append(backend)

    def open_session(
        self,
        name: str,
        spec: SessionSpec,
        events: EventSink | None = None,
    ) -> _Session:
        self.name = name
        self.spec = spec
        self.session = _Session(events, event_frames=self.events, fail=self.fail)
        return self.session

    def probe(self, name: str | None = None) -> BackendInfo:
        return BackendInfo(
            name or "fixture",
            True,
            version="fixture-1",
            capabilities=BackendCapabilities(conversations=False, streaming=True),
            reason="available in fixture",
        )


def _patch_registry(monkeypatch: pytest.MonkeyPatch, registry: _Registry) -> None:
    import mergerail.backends as backends

    monkeypatch.setattr(backends, "default_registry", lambda: registry)


@pytest.mark.parametrize(
    ("raw", "limit", "expected_code"),
    [
        (b"{", None, 1),
        (b"[]", None, 1),
        (b'{"mode":"not-a-mode"}', None, 1),
        (b"x" * 1024, 32, 2),
    ],
)
def test_main_bounds_and_reports_invalid_rpc_frames(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    limit: int | None,
    expected_code: int,
) -> None:
    if limit is not None:
        monkeypatch.setattr(worker, "MAX_REQUEST_BYTES", limit)
    console = _install_console(monkeypatch, raw)

    assert worker.main() == expected_code

    lines = console.output.getvalue().splitlines()
    assert len(lines) == 1
    assert len(lines[0]) <= worker.MAX_FRAME_BYTES
    frame = json.loads(lines[0])
    assert frame["type"] == "error"
    assert isinstance(frame["error"], str)
    if limit is not None:
        assert console.input.tell() == limit + 1


def test_turn_uses_fixed_gateway_and_reviewer_contract_without_host_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _Registry(events=[{"kind": "delta", "text": "checking"}])
    _patch_registry(monkeypatch, registry)
    monkeypatch.setattr(worker, "_restore_home", lambda _request: None)
    isolated_environment = {
        "HOME": "/fixture/home",
        "PATH": "/fixture/bin",
        "OPENAI_API_KEY": "fixture-only-secret",
        "OPENCODE_CONFIG_CONTENT": "host configuration must be discarded",
    }
    request = {
        "mode": "turn",
        "role": "reviewer",
        "backend": "opencode",
        "gateway": True,
        "gateway_host": "172.20.0.2",
        "model": "opencode/fixture-free",
        "model_definition": {"name": "Fixture Free", "tool_call": True},
        "prompt": "Review this fixture change",
        "schema": {"type": "object"},
        "external_backends": {"fixture": ["/fixture/bin/driver", "--offline"]},
    }
    console = _install_console(monkeypatch, json.dumps(request).encode())
    # Use a fixture-owned environment; no process environment is inspected or cleared.
    monkeypatch.setattr(os, "environ", isolated_environment)

    assert worker.main() == 0

    assert registry.name == "opencode"
    assert registry.spec is not None
    assert registry.spec.cwd == Path("/work/repo")
    assert registry.spec.role == "reviewer"
    assert registry.spec.read_only is True
    assert registry.spec.permission == "review"
    assert registry.session is not None and registry.session.closed
    assert registry.session.request is not None
    assert registry.session.request.prompt == "Review this fixture change"
    assert len(registry.registered) == 1
    assert registry.registered[0].command == ("/fixture/bin/driver", "--offline")

    config = json.loads(isolated_environment["OPENCODE_CONFIG_CONTENT"])
    assert config["model"] == "opencode/fixture-free"
    assert config["provider"]["opencode"]["options"]["baseURL"] == ("http://172.20.0.2:8765/zen/v1")
    assert config["plugin"] == [] and config["plugins"] == [] and config["mcp"] == {}
    assert isolated_environment["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert isolated_environment["OPENCODE_PURE"] == "1"
    assert isolated_environment["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
    assert "OPENAI_API_KEY" not in isolated_environment
    assert "host configuration must be discarded" not in isolated_environment.values()

    frames = [json.loads(line) for line in console.output.getvalue().splitlines()]
    assert frames[0] == {"type": "event", "event": {"kind": "delta", "text": "checking"}}
    assert frames[-1]["type"] == "result"
    reply = frames[-1]["reply"]
    assert reply["text"] == "review complete"
    assert reply["usage"] == {
        "input_tokens": 4,
        "output_tokens": 3,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
    }


@pytest.mark.parametrize("gateway_host", [None, "not-an-ip", "127.0.0.1", "0.0.0.0", "::1"])
def test_turn_refuses_missing_or_non_private_ipv4_gateway(
    monkeypatch: pytest.MonkeyPatch,
    gateway_host: str | None,
) -> None:
    monkeypatch.setattr(worker, "_restore_home", lambda _request: None)
    monkeypatch.setattr(os, "environ", {})
    request: dict[str, Any] = {
        "role": "fixer",
        "backend": "opencode",
        "gateway": True,
        "model": "opencode/fixture-free",
    }
    if gateway_host is not None:
        request["gateway_host"] = gateway_host

    with pytest.raises(worker.WorkerError, match="literal private IPv4"):
        worker._turn(request)


@pytest.mark.parametrize("failure", ["backend", "event-limit"])
def test_turn_closes_session_when_backend_or_event_limit_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    event_frames: list[Mapping[str, Any]] = [
        {"kind": "delta", "text": str(index)} for index in range(2)
    ]
    registry = _Registry(events=event_frames, fail=failure == "backend")
    _patch_registry(monkeypatch, registry)
    monkeypatch.setattr(worker, "_restore_home", lambda _request: None)
    monkeypatch.setattr(os, "environ", {})
    if failure == "event-limit":
        monkeypatch.setattr(worker, "MAX_EVENTS", 1)
    console = _install_console(
        monkeypatch,
        json.dumps({"mode": "turn", "role": "fixer", "backend": "opencode"}).encode(),
    )

    assert worker.main() == 1

    assert registry.session is not None and registry.session.closed
    frames = [json.loads(line) for line in console.output.getvalue().splitlines()]
    assert frames[-1]["type"] == "error"
    if failure == "event-limit":
        assert "too many streaming events" in frames[-1]["error"]
        assert sum(frame["type"] == "event" for frame in frames) == 1
    else:
        assert "fixture backend failed" in frames[-1]["error"]


def test_probe_normalizes_backend_info_and_registers_explicit_external_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _Registry()
    _patch_registry(monkeypatch, registry)

    result = worker._probe(
        {
            "backend": "fixture",
            "external_backends": {"fixture": ["/fixture/bin/driver", "--probe-only"]},
        }
    )

    assert result == {
        "name": "fixture",
        "available": True,
        "version": "fixture-1",
        "reason": "available in fixture",
        "capabilities": {
            "conversations": False,
            "native_resume": False,
            "streaming": True,
            "structured_output": False,
            "usage_reporting": False,
            "context_reporting": False,
            "exact_cost_reporting": False,
            "native_budget_limit": False,
            "native_read_only": False,
            "native_push_denial": False,
            "attachments": False,
        },
    }
    assert len(registry.registered) == 1
    assert registry.registered[0].command == ("/fixture/bin/driver", "--probe-only")


def test_probe_normalizes_builtin_backend_info(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _Registry()
    _patch_registry(monkeypatch, registry)

    result = worker._probe({"backend": "opencode"})

    assert result["name"] == "opencode"
    assert result["available"] is True
    assert result["version"] == "fixture-1"
    assert result["capabilities"]["streaming"] is True
    assert result["capabilities"]["conversations"] is False
    assert not registry.registered


def test_probe_marks_unsupported_provider_without_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    import mergerail.backends as backends

    monkeypatch.setattr(
        backends,
        "default_registry",
        lambda: pytest.fail("unsupported provider must not be probed"),
    )

    result = worker._probe({"backend": "claude"})

    assert result["available"] is False
    assert "unsupported" in result["reason"]


def _capture_export(monkeypatch: pytest.MonkeyPatch) -> _OutputBuffer:
    output = _OutputBuffer()
    streams = SimpleNamespace(
        stdout=SimpleNamespace(buffer=output),
        stderr=io.StringIO(),
    )
    monkeypatch.setattr(worker, "sys", streams)
    return output


def test_export_tree_writes_regular_files_and_symlinks_without_git_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "module.py").write_text("answer = 42\n", encoding="utf-8")
    (source / "module-link").symlink_to("module.py")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("private git state", encoding="utf-8")
    output = _capture_export(monkeypatch)

    worker._export_tree(source, maximum=64 * 1024, expanded_maximum=1024)

    with tarfile.open(fileobj=io.BytesIO(output.getvalue()), mode="r:") as archive:
        members = {member.name: member for member in archive.getmembers()}
        assert members["module.py"].isreg()
        payload = archive.extractfile("module.py")
        assert payload is not None
        assert payload.read() == b"answer = 42\n"
        assert members["module-link"].issym()
        assert members["module-link"].linkname == "module.py"
        assert not any(name == ".git" or name.startswith(".git/") for name in members)


def test_exported_outward_link_is_not_followed_and_restore_rejects_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("never archive this content", encoding="utf-8")
    (source / "escape").symlink_to(outside)
    output = _capture_export(monkeypatch)

    worker._export_tree(source, maximum=64 * 1024, expanded_maximum=1024)

    payload = output.getvalue()
    assert b"never archive this content" not in payload
    archive_path = tmp_path / "export.tar"
    archive_path.write_bytes(payload)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        assert archive.getmember("escape").issym()
    with pytest.raises(worker.WorkerError, match="absolute or aliased symlink"):
        worker._safe_extract_workspace(archive_path, tmp_path / "restore", limit_bytes=64 * 1024)
    assert outside.read_text(encoding="utf-8") == "never archive this content"


def test_export_tree_enforces_archive_and_expanded_byte_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "payload").write_bytes(b"x" * 1024)
    _capture_export(monkeypatch)
    with pytest.raises(worker.WorkerError, match="expands beyond"):
        worker._export_tree(source, maximum=64 * 1024, expanded_maximum=100)

    _capture_export(monkeypatch)
    with pytest.raises(worker.WorkerError, match="archive exceeds"):
        worker._export_tree(source, maximum=512, expanded_maximum=64 * 1024)


def test_export_tree_rejects_special_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        pytest.skip("FIFO creation is not portable to Windows")
    source = tmp_path / "workspace"
    source.mkdir()
    os.mkfifo(source / "pipe")
    _capture_export(monkeypatch)

    with pytest.raises(worker.WorkerError, match="special file"):
        worker._export_tree(source, maximum=64 * 1024, expanded_maximum=1024)


def test_same_uid_enumeration_excludes_init_and_own_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = tmp_path / "proc"
    for pid, uid, state in (("1", 0, "S"), ("4242", 65534, "S"), ("9001", 65534, "R")):
        entry = proc / pid
        entry.mkdir(parents=True)
        (entry / "status").write_text(
            f"Name:\tfixture\nState:\t{state} (fixture)\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n",
            encoding="ascii",
        )
    path_type = Path
    monkeypatch.setattr(
        worker,
        "Path",
        lambda value: proc if os.fspath(value) == "/proc" else path_type(value),
    )

    assert worker._same_uid_processes(65534, 4242) == [(9001, "R")]


def test_quiesce_stops_then_kills_only_active_peers_and_tolerates_zombies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "win32":
        pytest.skip("process-state semantics require a POSIX host")
    snapshots = iter(
        [
            [(9001, "S"), (9002, "Z"), (9003, "X")],
            [(9001, "T"), (9002, "Z"), (9003, "X")],
            [(9002, "Z"), (9003, "X")],
            [(9002, "Z"), (9003, "X")],
            [(9002, "Z"), (9003, "X")],
            [(9002, "Z"), (9003, "X")],
        ]
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "geteuid", lambda: 65534, raising=False)
    monkeypatch.setattr(os, "getpid", lambda: 4242)
    monkeypatch.setattr(os, "kill", lambda pid, signum: signals.append((pid, signum)))
    monkeypatch.setattr(worker, "_same_uid_processes", lambda _uid, _pid: next(snapshots, []))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    worker._quiesce_same_uid()

    assert signals == [(9001, signal.SIGSTOP), (9001, signal.SIGKILL)]
    assert all(pid not in {1, 4242} for pid, _signum in signals)
    assert 9002 not in {pid for pid, _signum in signals}
    assert 9003 not in {pid for pid, _signum in signals}


def test_quiesce_fails_closed_when_peer_does_not_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "win32":
        pytest.skip("process-state semantics require a POSIX host")
    signals: list[tuple[int, int]] = []
    times = iter([0.0, 0.0, 6.0])
    monkeypatch.setattr(os, "geteuid", lambda: 65534, raising=False)
    monkeypatch.setattr(os, "getpid", lambda: 4242)
    monkeypatch.setattr(os, "kill", lambda pid, signum: signals.append((pid, signum)))
    monkeypatch.setattr(worker, "_same_uid_processes", lambda _uid, _pid: [(9001, "S")])
    monkeypatch.setattr(time, "monotonic", lambda: next(times))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(worker.WorkerError, match="did not quiesce"):
        worker._quiesce_same_uid()

    assert signals == [(9001, signal.SIGSTOP)]


class _NoSpacePath:
    def __init__(self) -> None:
        self.opened = False

    def open(self, _mode: str) -> None:
        self.opened = True
        raise OSError(errno.ENOSPC, "fixture tmpfs limit")


def _fake_preflight_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    memory: str = "2097152",
    pids: str = "64",
) -> _NoSpacePath:
    cgroup_values = {
        "/sys/fs/cgroup/memory.max": memory,
        "/sys/fs/cgroup/pids.max": pids,
        "/sys/fs/cgroup/memory.swap.max": "0",
        "/sys/fs/cgroup/cpu.max": "50000 100000",
    }
    mappings: dict[str, Path] = {}
    for name, value in cgroup_values.items():
        destination = tmp_path / name.rsplit("/", 1)[-1]
        destination.write_text(value, encoding="ascii")
        mappings[name] = destination
    fill = _NoSpacePath()
    path_type = Path

    def sandbox_path(value: os.PathLike[str] | str) -> Any:
        raw = os.fspath(value)
        if raw == "/work/fill":
            return fill
        return mappings.get(raw, path_type(value))

    monkeypatch.setattr(worker, "Path", sandbox_path)
    return fill


def test_preflight_checks_fake_cgroup_limits_and_only_accepts_enospc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fill = _fake_preflight_paths(tmp_path, monkeypatch)

    result = worker._preflight_probe(
        {"limits": {"memory_bytes": 2_097_152, "pids_limit": 64, "cpus": 0.5}}
    )

    assert result == {
        "ok": True,
        "cgroup": {
            "memory": "2097152",
            "pids": "64",
            "swap": "0",
            "cpu": "50000 100000",
            "tmpfs_enospc": "verified",
        },
    }
    assert fill.opened


def test_preflight_rejects_cgroup_mismatch_before_tmpfs_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fill = _fake_preflight_paths(tmp_path, monkeypatch, pids="max")

    with pytest.raises(worker.WorkerError, match="pids limit mismatch"):
        worker._preflight_probe(
            {"limits": {"memory_bytes": 2_097_152, "pids_limit": 64, "cpus": 0.5}}
        )
    assert not fill.opened
