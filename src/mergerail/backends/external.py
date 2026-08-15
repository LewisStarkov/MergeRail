"""Long-lived external backend protocol over stdin/stdout JSONL."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from importlib import metadata
from pathlib import Path
from typing import IO, Any

from .. import procs
from .base import (
    AgentReply,
    BackendCapabilities,
    BackendInfo,
    EventSink,
    NullEventSink,
    SessionSpec,
    TurnRequest,
    Usage,
)

PROTOCOL_VERSION = 1
DEFAULT_MAX_FRAME_BYTES = 4 * 1024 * 1024


class ProtocolError(RuntimeError):
    """The driver violated ``mergerail-jsonl-v1``."""


class ExternalBackend:
    """An explicitly configured executable implementing ``mergerail-jsonl-v1``."""

    def __init__(
        self,
        name: str,
        command: Sequence[str],
        *,
        timeout: float = 10.0,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        if not name:
            raise ValueError("external backend name cannot be empty")
        if not command:
            raise ValueError("external backend command cannot be empty")
        self.name = name
        self.command = tuple(command)
        self.timeout = timeout
        self.max_frame_bytes = max_frame_bytes

    def probe(self) -> BackendInfo:
        executable = self.command[0]
        available = bool(shutil.which(executable))
        if not available:
            path = Path(executable)
            available = path.is_file() and os.access(path, os.X_OK)
        reason = "" if available else f"executable not found: {executable}"
        return BackendInfo(
            self.name,
            available,
            capabilities=BackendCapabilities(conversations=False),
            reason=reason,
        )

    def open_session(self, spec: SessionSpec, events: EventSink | None = None) -> ExternalSession:
        return ExternalSession(
            self.command,
            spec,
            events or NullEventSink(),
            handshake_timeout=self.timeout,
            max_frame_bytes=self.max_frame_bytes,
        )


class ExternalSession:
    """One driver process and one correlated role session."""

    def __init__(
        self,
        command: Sequence[str],
        spec: SessionSpec,
        events: EventSink,
        *,
        handshake_timeout: float,
        max_frame_bytes: int,
    ) -> None:
        self.spec = spec
        self.events = events
        self._max_frame_bytes = max_frame_bytes
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        self._closed = False
        self._request_number = 0
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._process = _spawn(command, spec.cwd)
        self._stdout_reader = threading.Thread(
            target=_read_lines, args=(self._process.stdout, self._lines), daemon=True
        )
        self._stderr_reader = threading.Thread(
            target=_drain, args=(self._process.stderr, self._stderr), daemon=True
        )
        self._stdout_reader.start()
        self._stderr_reader.start()
        try:
            self._capabilities = self._handshake(handshake_timeout)
            self.session_id = self._open(handshake_timeout)
        except BaseException:
            self._stop()
            raise

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def ask(self, request: TurnRequest) -> AgentReply:
        started = time.monotonic()
        with self._lock:
            if self._closed:
                return self._failure("external driver session is closed", started)
            request_id = self._next_request_id()
            frame: dict[str, Any] = {
                "type": "turn",
                "request_id": request_id,
                "session_id": self.session_id,
                "prompt": request.prompt,
                "schema": request.schema,
            }
            if request.max_cost_usd is not None:
                frame["max_cost_usd"] = request.max_cost_usd
            try:
                self._send(frame)
                result = self._wait_for_result(request_id, self.spec.timeout)
            except (ProtocolError, TimeoutError, OSError) as error:
                self._stop()
                return self._failure(str(error), started)
        return _reply_from_result(result, time.monotonic() - started, self.session_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            with suppress(OSError):
                self._send(
                    {
                        "type": "close_session",
                        "request_id": self._next_request_id(),
                        "session_id": self.session_id,
                    }
                )
            self._stop()

    def cancel(self) -> None:
        self._stop()

    def _handshake(self, timeout: float) -> BackendCapabilities:
        self._send(
            {
                "type": "hello",
                "protocol": PROTOCOL_VERSION,
                "mergerail_version": _mergerail_version(),
            }
        )
        event = self._read_frame(timeout)
        if event.get("type") != "hello":
            raise ProtocolError("external driver did not answer with a hello frame")
        if event.get("protocol") != PROTOCOL_VERSION:
            raise ProtocolError(
                f"external driver protocol mismatch: expected {PROTOCOL_VERSION}, "
                f"got {event.get('protocol')!r}"
            )
        capabilities = event.get("capabilities")
        if not isinstance(capabilities, dict):
            raise ProtocolError("external driver hello is missing capabilities")
        return _capabilities(capabilities)

    def _open(self, timeout: float) -> str:
        request_id = self._next_request_id()
        self._send(
            {
                "type": "open_session",
                "request_id": request_id,
                "role": self.spec.role,
                "cwd": str(self.spec.cwd),
                "policy": (
                    "read-only"
                    if self.spec.read_only or self.spec.role == "reviewer"
                    else "workspace-write"
                ),
                "system_prompt": self.spec.system_prompt,
                "model": self.spec.model,
                "effort": self.spec.effort,
                "settings": dict(self.spec.settings),
            }
        )
        deadline = time.monotonic() + timeout
        while True:
            event = self._read_frame(max(0.0, deadline - time.monotonic()))
            event_request = event.get("request_id")
            if event_request != request_id:
                if event_request is None and event.get("type") in {
                    "error",
                    "result",
                    "session",
                    "session_opened",
                }:
                    raise ProtocolError("external driver response is missing request_id")
                self.events.emit(event)
                continue
            if event.get("type") == "error":
                raise ProtocolError(_result_error(event))
            if event.get("type") not in {"session", "session_opened", "result"}:
                self.events.emit(event)
                continue
            session_id = _string(event.get("session_id"))
            if not session_id:
                raise ProtocolError("external driver did not return a session_id")
            return session_id

    def _wait_for_result(self, request_id: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            event = self._read_frame(max(0.0, deadline - time.monotonic()))
            event_request = event.get("request_id")
            if event_request != request_id:
                if event_request is None and event.get("type") in {"error", "result"}:
                    raise ProtocolError("external driver response is missing request_id")
                self.events.emit(event)
                continue
            if event.get("type") == "error":
                return {
                    "type": "result",
                    "request_id": request_id,
                    "text": _result_error(event),
                    "is_error": True,
                }
            if event.get("type") == "result":
                return event
            self.events.emit(event)

    def _send(self, frame: Mapping[str, object]) -> None:
        if self._process.poll() is not None:
            raise OSError(self._process_error("external driver exited"))
        stream = self._process.stdin
        if stream is None:
            raise OSError("external driver stdin is unavailable")
        line = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        stream.write(line + "\n")
        stream.flush()

    def _read_frame(self, timeout: float) -> dict[str, Any]:
        if timeout <= 0:
            raise TimeoutError("external driver response timed out")
        try:
            raw = self._lines.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("external driver response timed out") from error
        if raw is None:
            raise ProtocolError(self._process_error("external driver closed stdout"))
        if len(raw.encode("utf-8")) > self._max_frame_bytes:
            raise ProtocolError("external driver frame exceeds the configured size limit")
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ProtocolError(f"external driver emitted malformed JSON: {error.msg}") from error
        if not isinstance(event, dict):
            raise ProtocolError("external driver frame must be a JSON object")
        return event

    def _next_request_id(self) -> str:
        self._request_number += 1
        return str(self._request_number)

    def _failure(self, message: str, started: float) -> AgentReply:
        return AgentReply(
            text=message,
            is_error=True,
            cost_usd=None,
            context_tokens=0,
            seconds=time.monotonic() - started,
            session_id=getattr(self, "session_id", None),
        )

    def _process_error(self, fallback: str) -> str:
        detail = "".join(self._stderr).strip()
        return detail or fallback

    def _stop(self) -> None:
        with self._stop_lock:
            if self._closed:
                return
            self._closed = True
            if self._process.poll() is None:
                procs.terminate_tree(self._process)
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    procs.kill_tree(self._process)
                    self._process.wait(timeout=2)
            self._stdout_reader.join(timeout=1)
            self._stderr_reader.join(timeout=1)


def _spawn(command: Sequence[str], cwd: Path) -> subprocess.Popen[str]:
    options: dict[str, Any] = {
        "cwd": cwd,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "bufsize": 1,
    }
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(list(command), **options)


def _read_lines(stream: IO[str] | None, into: queue.Queue[str | None]) -> None:
    if stream is None:
        into.put(None)
        return
    try:
        for line in stream:
            into.put(line.rstrip("\r\n"))
    finally:
        into.put(None)


def _drain(stream: IO[str] | None, into: list[str]) -> None:
    if stream is None:
        return
    for line in stream:
        into.append(line)


def _capabilities(raw: dict[str, Any]) -> BackendCapabilities:
    fields = BackendCapabilities.__dataclass_fields__
    values: dict[str, bool] = {}
    for name in fields:
        value = raw.get(name, False)
        if not isinstance(value, bool):
            raise ProtocolError(f"external capability {name!r} must be boolean")
        values[name] = value
    return BackendCapabilities(**values)


def _reply_from_result(event: dict[str, Any], seconds: float, session_id: str) -> AgentReply:
    usage = _usage(event.get("usage"))
    structured = event.get("structured") or event.get("structured_output")
    return AgentReply(
        text=_string(event.get("text") or event.get("result")),
        is_error=bool(event.get("is_error")),
        cost_usd=_number(event.get("cost_usd")),
        context_tokens=(
            _integer(event.get("context_tokens"))
            or (usage.context_tokens if usage is not None else 0)
        ),
        seconds=seconds,
        structured=structured if isinstance(structured, dict) else None,
        session_id=_string(event.get("session_id")) or session_id,
        usage=usage,
    )


def _usage(value: object) -> Usage | None:
    if not isinstance(value, dict):
        return None
    return Usage(
        input_tokens=_integer(value.get("input_tokens")),
        output_tokens=_integer(value.get("output_tokens")),
        cache_creation_input_tokens=_integer(value.get("cache_creation_input_tokens")),
        cache_read_input_tokens=_integer(value.get("cache_read_input_tokens")),
    )


def _result_error(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        return _string(error.get("message") or error.get("code")) or "external driver failed"
    return _string(event.get("message") or error) or "external driver failed"


def _mergerail_version() -> str:
    try:
        return metadata.version("mergerail")
    except metadata.PackageNotFoundError:
        return "0.1.1"


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "ExternalBackend",
    "ExternalSession",
    "ProtocolError",
]
