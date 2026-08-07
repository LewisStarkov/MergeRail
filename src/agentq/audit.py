"""Append-only operational history for a runner."""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .filelock import exclusive_file

AUDIT_SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUPS = 3


class AuditLog:
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backups: int = DEFAULT_BACKUPS,
    ) -> None:
        self.path = path
        self.max_bytes = max(max_bytes, 0)
        self.backups = max(backups, 0)
        self._lock = threading.Lock()

    def emit(self, event: str, *, task: int | None = None, **fields: object) -> None:
        record: dict[str, object] = {
            "schema": AUDIT_SCHEMA_VERSION,
            "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event,
        }
        if task is not None:
            record["task"] = task
        record.update({key: value for key, value in fields.items() if value is not None})
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with exclusive_file(self.path.with_name(self.path.name + ".lock")):
                self._rotate(len((line + "\n").encode("utf-8")))
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")

    def read(self, *, task: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        records: deque[dict[str, Any]] = deque(maxlen=limit)
        paths = [
            self.path.with_name(f"{self.path.name}.{index}")
            for index in range(self.backups, 0, -1)
        ]
        paths.append(self.path)
        if not any(path.exists() for path in paths):
            return []
        with self._lock, exclusive_file(self.path.with_name(self.path.name + ".lock")):
            for path in paths:
                try:
                    handle = path.open(encoding="utf-8")
                except FileNotFoundError:
                    continue
                with handle:
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(record, dict) or (
                            task is not None and record.get("task") != task
                        ):
                            continue
                        records.append(record)
        return list(records)

    def _rotate(self, incoming_bytes: int) -> None:
        if self.max_bytes <= 0 or not self.path.exists():
            return
        if self.path.stat().st_size + incoming_bytes <= self.max_bytes:
            return
        if self.backups <= 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                os.replace(source, self.path.with_name(f"{self.path.name}.{index + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))


__all__ = ["AUDIT_SCHEMA_VERSION", "AuditLog"]
