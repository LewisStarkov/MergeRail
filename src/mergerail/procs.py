"""Child processes that own a whole tree, and how to stop all of it.

``uv run python -m app`` is two processes: the wrapper and the interpreter it
execs. Terminating the wrapper leaves the real one running, and a restart then
brings up a second copy. The same goes for an agent that spawned a test runner.
So every child started here becomes the root of its own tree — a session on
POSIX, a process group on Windows — and termination is aimed at the tree.

POSIX gets the usual pair: SIGTERM to the group, then SIGKILL. Windows has no
SIGTERM; the polite request is a Ctrl-Break to the group, and the hammer is
``taskkill /T``, which walks the tree by parent ids.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


class ProcessController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._process: subprocess.Popen[str] | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def reset(self) -> None:
        self._cancelled.clear()

    def attach(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._process = process
            cancelled = self._cancelled.is_set()
        if cancelled:
            self._terminate(process)

    def detach(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            process = self._process
        if process is not None:
            self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        terminate_tree(process)
        killer = threading.Timer(2, kill_tree, args=(process,))
        killer.daemon = True
        killer.start()


def spawn(
    command: list[str],
    *,
    cwd: Path,
    stdout: int,
    stderr: int,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    """Start ``command`` as the root of its own process tree."""
    if sys.platform == "win32":
        return subprocess.Popen(
            command,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            text=True,
            bufsize=1,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(
        command,
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,
    )


def terminate_tree(process: subprocess.Popen[str]) -> None:
    """Ask the whole tree to stop. The polite half; :func:`kill_tree` is the other."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            os.kill(process.pid, signal.CTRL_BREAK_EVENT)
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):  # pragma: no cover - race with exit
        process.terminate()


def kill_tree(process: subprocess.Popen[str]) -> None:
    """Stop the whole tree now, no questions asked."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, check=False
        )
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover - race with exit
        process.kill()


__all__ = ["ProcessController", "kill_tree", "spawn", "terminate_tree"]
