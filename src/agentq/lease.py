"""Exclusive ownership of the repository's AgentQ runner."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from uuid import uuid4


class RunnerBusy(RuntimeError):
    pass


if sys.platform == "win32":
    import msvcrt

    _LOCK_OFFSET = 1 << 20

    def _try_lock(handle: IO[str]) -> bool:
        handle.seek(_LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[str]) -> None:
        handle.seek(_LOCK_OFFSET)
        with contextlib.suppress(OSError):
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[str]) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RunnerLease:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.run_id = ""
        self._handle: IO[str] | None = None

    def acquire(self) -> str:
        if self._handle is not None:
            return self.run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("\n")
            handle.flush()
        if not _try_lock(handle):
            owner = self._owner(handle)
            handle.close()
            detail = f" ({owner})" if owner else ""
            raise RunnerBusy(f"another agentq runner owns {self.path}{detail}")

        self.run_id = uuid4().hex
        record = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        handle.seek(0)
        handle.truncate()
        json.dump(record, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self.run_id

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        _unlock(handle)
        handle.close()

    @staticmethod
    def _owner(handle: IO[str]) -> str:
        try:
            handle.seek(0)
            raw: Any = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(raw, dict):
            return ""
        bits = []
        if raw.get("pid"):
            bits.append(f"pid {raw['pid']}")
        if raw.get("host"):
            bits.append(f"host {raw['host']}")
        if raw.get("started_at"):
            bits.append(f"since {raw['started_at']}")
        return ", ".join(bits)


__all__ = ["RunnerBusy", "RunnerLease"]
