"""The queue — one JSON file, written by two processes.

A front (Telegram, a folder, whatever you plug in) appends to it; the runner
claims tasks from it and writes outcomes back. Neither owns the file, so every
mutation is a read-modify-write under an ``flock`` on a sidecar lock file, and
the result lands through a temp file and ``os.replace``. A runner killed
mid-write cannot leave the front with half a JSON document, and two runners
cannot hand the same task to two agents.

**Why a file and not a database.** The runner merges branches and restarts the
supervised process, so it has to keep working while that process — and anything
it owns — is down. A file also stays legible: you can read the queue, or fix it,
with an editor.

Attachments are not inlined. A screenshot is written next to the JSON under
``media/`` and the task keeps the absolute path, because the thing that reads it
is an agent in a worktree where nothing else about this directory exists.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Bumped only if the on-disk shape changes incompatibly. Readers tolerate
#: missing and unknown keys, so adding a field does not need a bump.
SCHEMA_VERSION = 1


class Status:
    """Where a task is. The runner drives every transition after ``NEW``."""

    #: Written by a front, waiting to be claimed.
    NEW = "new"
    #: An agent is working on it.
    RUNNING = "running"
    #: The work is committed; a second agent is reading it.
    REVIEW = "review"
    #: Reviewed and landed — merged, or opened as a pull request.
    DONE = "done"
    #: The agent gave up, or the reviewer refused it too many times.
    FAILED = "failed"
    #: Approved but not landed — the branch is waiting for a human.
    BLOCKED = "blocked"
    #: Closed by hand.
    CLOSED = "closed"


#: Statuses the runner will not touch again.
TERMINAL: frozenset[str] = frozenset({Status.DONE, Status.FAILED, Status.BLOCKED, Status.CLOSED})

#: Rendered next to each row, so a list of ten reads at a glance.
ICONS: dict[str, str] = {
    Status.NEW: "🕓",
    Status.RUNNING: "🚧",
    Status.REVIEW: "🔍",
    Status.DONE: "✅",
    Status.FAILED: "❌",
    Status.BLOCKED: "⚠️",
    Status.CLOSED: "🗄",
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class Task:
    """One line of work, as a person wrote it, plus everything since."""

    id: int
    text: str
    status: str = Status.NEW
    #: Absolute path to an attached screenshot or document, if there was one.
    file: str | None = None
    created_at: str = ""
    updated_at: str = ""
    #: Which front took it in, and whatever that front needs to answer back —
    #: a chat id, an issue number, a filename. Opaque to the runner.
    source: str = ""
    origin: dict[str, str] = field(default_factory=dict)
    author: str = ""
    #: Filled in by the runner.
    branch: str | None = None
    attempts: int = 0
    #: Where it landed, when that has a URL: the pull request.
    url: str = ""
    #: The last thing that happened, in one line: an agent's summary, a
    #: reviewer's objection, the reason a merge could not go through.
    note: str = ""

    @property
    def icon(self) -> str:
        return ICONS.get(self.status, "•")

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL

    @property
    def title(self) -> str:
        """First line, for a PR title or a list row."""
        return (self.text.strip().splitlines() or [""])[0]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Task:
        """Tolerant on purpose: a file written by an older build still loads."""
        known = {item.name for item in fields(cls)}
        data = {key: value for key, value in raw.items() if key in known}
        data.setdefault("id", 0)
        data.setdefault("text", "")
        return cls(**data)


class TaskStore:
    """The JSON file, and the only sanctioned way to change it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.media_dir = path.parent / "media"

    # --- reading ---------------------------------------------------------

    def load(self) -> list[Task]:
        """Every task, newest first. A missing or corrupt file reads as empty.

        Corrupt rather than raising because the caller is usually a front
        answering a person: an unreadable queue should say "no tasks", not take
        the command down until someone opens a terminal.
        """
        return sorted(self._read(), key=lambda task: task.id, reverse=True)

    def get(self, task_id: int) -> Task | None:
        return next((task for task in self._read() if task.id == task_id), None)

    def open_tasks(self) -> list[Task]:
        return [task for task in self.load() if task.is_open]

    # --- writing ---------------------------------------------------------

    def add(
        self,
        text: str,
        *,
        source: str = "",
        origin: dict[str, str] | None = None,
        author: str = "",
        file: str | None = None,
    ) -> Task:
        """Append a task and hand back the stored copy, id included."""
        with self._locked():
            tasks = self._read()
            stamp = now_iso()
            task = Task(
                id=max((item.id for item in tasks), default=0) + 1,
                text=text.strip(),
                file=file,
                created_at=stamp,
                updated_at=stamp,
                source=source,
                origin=dict(origin or {}),
                author=author,
            )
            tasks.append(task)
            self._write(tasks)
            return task

    def update(self, task_id: int, **changes: Any) -> Task | None:
        """Set fields on one task under the lock. Unknown keys are ignored."""
        known = {item.name for item in fields(Task)} - {"id"}
        with self._locked():
            tasks = self._read()
            for task in tasks:
                if task.id != task_id:
                    continue
                for key, value in changes.items():
                    if key in known:
                        setattr(task, key, value)
                task.updated_at = now_iso()
                self._write(tasks)
                return task
            return None

    def take_next(self) -> Task | None:
        """Claim the oldest ``NEW`` task, flipping it to ``RUNNING`` atomically.

        The claim and the read happen inside one lock so a second runner — or a
        second pass of the same one — cannot hand the same task to a second
        agent. That is the whole reason this is not ``load()`` plus ``update()``.
        """
        with self._locked():
            tasks = self._read()
            waiting = sorted(
                (task for task in tasks if task.status == Status.NEW), key=lambda task: task.id
            )
            if not waiting:
                return None
            task = waiting[0]
            task.status = Status.RUNNING
            task.attempts += 1
            task.updated_at = now_iso()
            self._write(tasks)
            return task

    def remove(self, task_id: int) -> bool:
        with self._locked():
            tasks = self._read()
            kept = [task for task in tasks if task.id != task_id]
            if len(kept) == len(tasks):
                return False
            self._write(kept)
            return True

    def media_path(self, task_id: int, suffix: str) -> Path:
        """Where an attachment for ``task_id`` goes. Creates the directory."""
        self.media_dir.mkdir(parents=True, exist_ok=True)
        safe = suffix if suffix.startswith(".") and len(suffix) <= 10 else ".bin"
        return self.media_dir / f"task-{task_id:04d}{safe}"

    # --- plumbing --------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Exclusive across processes for the length of one read-modify-write.

        The lock lives on a sidecar file rather than on the queue itself: the
        write replaces that inode, and a lock held on a replaced inode protects
        nothing.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        with lock.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> list[Task]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        return [Task.from_dict(item) for item in items if isinstance(item, dict)]

    def _write(self, tasks: list[Task]) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "tasks": [task.to_dict() for task in sorted(tasks, key=lambda task: task.id)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Not a context manager: the file has to outlive the ``with`` below so
        # ``os.replace`` can move it into place, which is the atomicity.
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
            "w",
            dir=self.path.parent,
            prefix=self.path.name,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        )
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise


__all__ = ["ICONS", "SCHEMA_VERSION", "TERMINAL", "Status", "Task", "TaskStore", "now_iso"]
