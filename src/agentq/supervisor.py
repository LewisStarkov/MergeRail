"""An optional child process, kept alive and restarted when work lands.

This exists because the most useful place to run this tool is next to the thing
it is fixing. If the runner owns the process, then "merged" and "restarted" are
one step, and the last few error lines from that process are available to hand
to the agent as context — nine times out of ten the task *is* the exception the
service has been logging for the last five minutes.

The child is started as the root of its own process tree and stopped as a
tree — see :mod:`agentq.procs` for why, and for how that works per platform.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections import deque
from pathlib import Path

from . import log, procs

#: Output lines worth keeping for an agent to read.
ERROR_LINE = re.compile(r"\b(ERROR|CRITICAL|Traceback|Exception|FATAL)\b")


class Supervisor:
    """Starts it, tails it, restarts it — and kills the whole group."""

    def __init__(self, command: list[str], cwd: Path, log_path: Path) -> None:
        self.command = command
        self.cwd = cwd
        self.log_path = log_path
        self.process: subprocess.Popen[str] | None = None
        self.recent_errors: deque[str] = deque(maxlen=40)

    @property
    def enabled(self) -> bool:
        return bool(self.command)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if not self.enabled or self.alive:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.process = procs.spawn(
            self.command,
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        threading.Thread(target=self._drain, args=(self.process,), daemon=True).start()
        log.info("process.started", pid=self.process.pid, log=self.log_path)

    def _drain(self, process: subprocess.Popen[str]) -> None:
        """One line at a time, to the log file and to our own stdout."""
        stream = process.stdout
        if stream is None:  # pragma: no cover - stdout is always a pipe here
            return
        with self.log_path.open("a", encoding="utf-8") as sink:
            for raw in stream:
                line = raw.rstrip("\n")
                sink.write(line + "\n")
                sink.flush()
                log.raw(f"app │ {line}")
                if ERROR_LINE.search(line):
                    self.recent_errors.append(line)

    def stop(self, timeout: float = 15.0) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        procs.terminate_tree(process)
        try:
            process.wait(timeout)
        except subprocess.TimeoutExpired:
            log.warn("process.kill")
            procs.kill_tree(process)
            process.wait(5)
        log.info("process.stopped")

    def restart(self) -> None:
        self.stop()
        self.start()

    def supervise(self) -> None:
        """Bring it back if it fell over on its own."""
        if self.process is not None and self.process.poll() is not None:
            log.warn("process.died", code=self.process.returncode)
            self.process = None
            self.start()

    def error_digest(self, limit: int = 15) -> str:
        lines = list(self.recent_errors)[-limit:]
        return "\n".join(lines) if lines else ""


__all__ = ["ERROR_LINE", "Supervisor"]
