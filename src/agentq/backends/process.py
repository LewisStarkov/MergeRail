"""Shared process-tree and JSONL plumbing for CLI backends."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from .. import procs

JsonEvent = dict[str, Any]
EventCallback = Callable[[JsonEvent], None]


@dataclass(frozen=True, slots=True)
class JsonlProcessResult:
    events: tuple[JsonEvent, ...]
    stderr: str
    returncode: int
    timed_out: bool
    seconds: float
    cancelled: bool = False


ProcessController = procs.ProcessController


def parse_event(raw: str) -> JsonEvent | None:
    """Parse one JSONL object, ignoring blank, malformed, and non-object lines."""
    line = raw.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def run_jsonl(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    on_event: EventCallback | None = None,
    controller: ProcessController | None = None,
) -> JsonlProcessResult:
    """Run a JSONL CLI, streaming objects while safely owning its process tree."""
    started = time.monotonic()
    control = controller or ProcessController()
    control.reset()
    process = procs.spawn(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    control.attach(process)
    stderr_lines: list[str] = []
    drainer = threading.Thread(
        target=drain_text, args=(process.stderr, stderr_lines), daemon=True
    )
    drainer.start()
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        procs.kill_tree(process)

    watchdog = threading.Timer(max(timeout, 0.0), expire)
    watchdog.start()
    events: list[JsonEvent] = []
    try:
        stream = process.stdout
        assert stream is not None
        for raw in stream:
            event = parse_event(raw)
            if event is None:
                continue
            events.append(event)
            if on_event is not None:
                on_event(event)
        process.wait()
    finally:
        watchdog.cancel()
        drainer.join(timeout=5)
        control.detach(process)

    return JsonlProcessResult(
        events=tuple(events),
        stderr="".join(stderr_lines),
        returncode=int(process.returncode or 0),
        timed_out=timed_out.is_set(),
        seconds=time.monotonic() - started,
        cancelled=control.cancelled,
    )


def drain_text(stream: IO[str] | None, into: list[str]) -> None:
    """Drain a text pipe so a noisy child cannot deadlock on stderr."""
    if stream is None:  # pragma: no cover - backend processes always request a pipe
        return
    for line in stream:
        into.append(line)


__all__ = [
    "EventCallback",
    "JsonEvent",
    "JsonlProcessResult",
    "ProcessController",
    "drain_text",
    "parse_event",
    "run_jsonl",
]
