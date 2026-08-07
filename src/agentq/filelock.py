"""Small cross-platform blocking file lock used around durable state writes."""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[str]) -> None:
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue

    def _unlock(handle: IO[str]) -> None:
        handle.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_file(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("\n")
            handle.flush()
        _lock(handle)
        try:
            yield
        finally:
            _unlock(handle)


__all__ = ["exclusive_file"]
