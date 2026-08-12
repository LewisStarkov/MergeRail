"""The queue — one JSON file, written by two processes.

A front (Telegram, a folder, whatever you plug in) appends to it; the runner
claims tasks from it and writes outcomes back. Neither owns the file, so every
mutation is a read-modify-write under an OS file lock (``flock``; ``msvcrt`` on
Windows) held on a sidecar lock file, and the result lands through a temp file
and ``os.replace``. A runner killed
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

import json
import os
import shutil
import tempfile
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .filelock import exclusive_file

#: Version 4 adds durable agent sessions and an execution history. Older
#: versions remain readable and are backed up before their first version 4
#: write.
SCHEMA_VERSION = 4


class QueueCorruptError(RuntimeError):
    pass


class Status:
    """Where a task is. The runner drives every transition after ``NEW``."""

    #: Written by a front, waiting to be claimed.
    NEW = "new"
    #: An agent is working on it.
    RUNNING = "running"
    #: The work is committed; a second agent is reading it.
    REVIEW = "review"
    #: Review passed and the exact commit is durable, waiting to be delivered.
    APPROVED = "approved"
    #: A merge/push/PR operation is in progress and can be recovered.
    DELIVERING = "delivering"
    #: A person requested cancellation; the runner is stopping the active process.
    CANCELLING = "cancelling"
    #: Cancelled by a person, with any partial branch preserved.
    CANCELLED = "cancelled"
    #: Reviewed and landed — merged, or opened as a pull request.
    DONE = "done"
    #: The agent gave up, or the reviewer refused it too many times.
    FAILED = "failed"
    #: Approved but not landed — the branch is waiting for a human.
    BLOCKED = "blocked"
    #: Closed by hand.
    CLOSED = "closed"


#: Statuses the runner will not touch again.
TERMINAL: frozenset[str] = frozenset(
    {Status.DONE, Status.FAILED, Status.BLOCKED, Status.CANCELLED, Status.CLOSED}
)

#: Rendered next to each row, so a list of ten reads at a glance.
ICONS: dict[str, str] = {
    Status.NEW: "🕓",
    Status.RUNNING: "🚧",
    Status.REVIEW: "🔍",
    Status.APPROVED: "🟢",
    Status.DELIVERING: "🚚",
    Status.CANCELLING: "⏳",
    Status.CANCELLED: "🚫",
    Status.DONE: "✅",
    Status.FAILED: "❌",
    Status.BLOCKED: "⚠️",
    Status.CLOSED: "🗄",
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class DeliveryError:
    """One failed delivery step; earlier errors are retained for diagnosis."""

    stage: str
    code: str
    message: str
    occurred_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DeliveryError:
        known = {item.name for item in fields(cls)}
        data = {key: value for key, value in raw.items() if key in known}
        return cls(
            stage=str(data.get("stage", "")),
            code=str(data.get("code", "delivery_failed")),
            message=str(data.get("message", "")),
            occurred_at=str(data.get("occurred_at", "")),
        )


@dataclass(slots=True)
class DeliveryRecord:
    """Durable, provider-neutral progress for delivering an approved commit."""

    requested_mode: str = "auto"
    resolved_mode: str = ""
    status: str = "none"
    stage: str = ""
    outcome: str = ""
    base_branch: str = ""
    branch: str = ""
    commit: str = ""
    summary: str = ""
    review: str = ""
    attempts: int = 0
    url: str = ""
    errors: list[DeliveryError] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> DeliveryRecord:
        if not isinstance(raw, dict):
            return cls()
        known = {item.name for item in fields(cls)} - {"errors"}
        data = {key: value for key, value in raw.items() if key in known}
        raw_errors = raw.get("errors", [])
        errors = (
            [DeliveryError.from_dict(item) for item in raw_errors if isinstance(item, dict)]
            if isinstance(raw_errors, list)
            else []
        )
        return cls(**data, errors=errors)


@dataclass(slots=True)
class TaskMessage:
    """One folded message from a task's append-only thread journal."""

    id: int
    text: str
    role: str = "user"
    mode: str = "instruction"
    author: str = ""
    file: str | None = None
    status: str = "stored"
    idempotency_key: str = ""
    attempt: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TaskMessage:
        known = {item.name for item in fields(cls)}
        data = {key: value for key, value in raw.items() if key in known}
        data["id"] = _safe_int(data.get("id", 0))
        data["text"] = str(data.get("text", ""))
        data["role"] = str(data.get("role", "user"))
        data["mode"] = str(data.get("mode", "instruction"))
        data["author"] = str(data.get("author", ""))
        data["status"] = str(data.get("status", "stored"))
        data["idempotency_key"] = str(data.get("idempotency_key", ""))
        data["attempt"] = _safe_int(data.get("attempt", 0))
        data["created_at"] = str(data.get("created_at", ""))
        data["updated_at"] = str(data.get("updated_at", ""))
        file = data.get("file")
        data["file"] = str(file) if file is not None else None
        return cls(**data)


@dataclass(slots=True)
class TaskSession:
    """A resumable backend session owned by one task and agent role."""

    role: str
    backend: str
    session_id: str
    context_tokens: int = 0
    updated_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TaskSession:
        return cls(
            role=str(raw.get("role", "")),
            backend=str(raw.get("backend", "")),
            session_id=str(raw.get("session_id", "")),
            context_tokens=_safe_int(raw.get("context_tokens", 0)),
            updated_at=str(raw.get("updated_at", "")),
        )


@dataclass(slots=True)
class TaskRun:
    """Immutable snapshot of one completed execution before a follow-up run."""

    attempt: int
    status: str
    branch: str | None = None
    approved_sha: str = ""
    delivery: DeliveryRecord = field(default_factory=DeliveryRecord)
    note: str = ""
    url: str = ""
    cost_usd: float = 0.0
    started_at: str = ""
    finished_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TaskRun:
        known = {item.name for item in fields(cls)} - {"delivery"}
        data = {key: value for key, value in raw.items() if key in known}
        data["attempt"] = _safe_int(data.get("attempt", 0))
        data["status"] = str(data.get("status", ""))
        data["cost_usd"] = _safe_float(data.get("cost_usd", 0.0))
        return cls(**data, delivery=DeliveryRecord.from_dict(raw.get("delivery")))


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
    #: Attempts never reuse a branch. Old refs remain visible and recoverable.
    previous_branches: list[str] = field(default_factory=list)
    attempts: int = 0
    claimed_by: str = ""
    claimed_at: str = ""
    #: The immutable commit accepted by the reviewer.
    approved_sha: str = ""
    delivery: DeliveryRecord = field(default_factory=DeliveryRecord)
    #: A v1 BLOCKED task had no structured delivery record. Keep that fact
    #: explicit so a migration never pretends it is safe to retry delivery.
    legacy_blocked: bool = False
    #: Where it landed, when that has a URL: the pull request.
    url: str = ""
    #: The last thing that happened, in one line: an agent's summary, a
    #: reviewer's objection, the reason a merge could not go through.
    note: str = ""
    #: What the agents spent on this task, in dollars, across every round.
    cost_usd: float = 0.0
    #: Resumable backend sessions, keyed by agent role (fixer, reviewer, ...).
    sessions: dict[str, TaskSession] = field(default_factory=dict)
    #: Previous terminal executions, retained when the task is retried.
    runs: list[TaskRun] = field(default_factory=list)

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
    def from_dict(cls, raw: dict[str, Any], *, schema_version: int = SCHEMA_VERSION) -> Task:
        """Tolerant on purpose: a file written by an older build still loads."""
        known = {item.name for item in fields(cls)} - {"delivery", "sessions", "runs"}
        data = {key: value for key, value in raw.items() if key in known}
        data.setdefault("id", 0)
        data.setdefault("text", "")
        delivery = DeliveryRecord.from_dict(raw.get("delivery"))
        raw_sessions = raw.get("sessions", {})
        sessions: dict[str, TaskSession] = {}
        if isinstance(raw_sessions, dict):
            for role, item in raw_sessions.items():
                if not isinstance(item, dict):
                    continue
                session = TaskSession.from_dict({**item, "role": role})
                if session.role:
                    sessions[session.role] = session
        elif isinstance(raw_sessions, list):
            for item in raw_sessions:
                if not isinstance(item, dict):
                    continue
                session = TaskSession.from_dict(item)
                if session.role:
                    sessions[session.role] = session
        raw_runs = raw.get("runs", [])
        runs = (
            [TaskRun.from_dict(item) for item in raw_runs if isinstance(item, dict)]
            if isinstance(raw_runs, list)
            else []
        )
        if schema_version < 2 and data.get("status") == Status.BLOCKED:
            data["legacy_blocked"] = True
        return cls(**data, delivery=delivery, sessions=sessions, runs=runs)


class TaskStore:
    """The JSON file, and the only sanctioned way to change it."""

    def __init__(self, path: Path, *, claimant: str = "") -> None:
        self.path = path
        self.media_dir = path.parent / "media"
        self.threads_dir = path.parent / "threads"
        self.archive_path = path.with_name(f"{path.stem}.archive.jsonl")
        self.claimant = claimant

    # --- reading ---------------------------------------------------------

    def load(self) -> list[Task]:
        """Every task, newest first. A missing file reads as empty."""
        return sorted(self._read(), key=lambda task: task.id, reverse=True)

    def get(self, task_id: int) -> Task | None:
        return next((task for task in self._read() if task.id == task_id), None)

    def open_tasks(self) -> list[Task]:
        return [task for task in self.load() if task.is_open]

    # --- task threads ----------------------------------------------------

    def append_message(
        self,
        task_id: int,
        text: str,
        *,
        role: str = "user",
        mode: str = "instruction",
        author: str = "",
        file: str | None = None,
        idempotency_key: str = "",
        status: str | None = None,
    ) -> TaskMessage | None:
        """Append a message, returning the existing one for a duplicate key."""
        task = self.get(task_id)
        if task is None:
            return None
        path = self._thread_path(task_id)
        with self._thread_locked(task_id):
            current = self._read_thread(path)
            if idempotency_key:
                duplicate = next(
                    (item for item in current if item.idempotency_key == idempotency_key),
                    None,
                )
                if duplicate is not None:
                    return duplicate
            stamp = now_iso()
            message = TaskMessage(
                id=max((item.id for item in current), default=0) + 1,
                text=text.strip(),
                role=role,
                mode=mode,
                author=author,
                file=file,
                status=(
                    status
                    if status is not None
                    else "pending"
                    if role == "user" and mode == "instruction"
                    else "stored"
                ),
                idempotency_key=idempotency_key,
                attempt=task.attempts,
                created_at=stamp,
                updated_at=stamp,
            )
            self._append_thread_event(path, {"event": "message", "message": message.to_dict()})
            return message

    def messages(self, task_id: int, *, after: int = 0, limit: int = 100) -> list[TaskMessage]:
        """Read the folded thread in message-id order."""
        if limit <= 0:
            return []
        with self._thread_locked(task_id):
            messages = self._read_thread(self._thread_path(task_id))
        return [item for item in messages if item.id > after][:limit]

    def message_stats(self, task_id: int) -> dict[str, int | str]:
        messages = self.messages(task_id, limit=2**31 - 1)
        try:
            revision = self._thread_path(task_id).stat().st_size
        except OSError:
            revision = 0
        return {
            "message_count": len(messages),
            "pending_message_count": sum(item.status == "pending" for item in messages),
            "last_message_at": max(
                (item.updated_at or item.created_at for item in messages), default=""
            ),
            "message_revision": revision,
        }

    def claim_pending_messages(self, task_id: int) -> list[TaskMessage]:
        """Atomically move every pending instruction to ``processing``."""
        path = self._thread_path(task_id)
        with self._thread_locked(task_id):
            messages = self._read_thread(path)
            pending = [item for item in messages if item.status == "pending"]
            if not pending:
                return []
            stamp = now_iso()
            for message in pending:
                self._append_thread_event(
                    path,
                    {
                        "event": "update",
                        "message_id": message.id,
                        "changes": {"status": "processing", "updated_at": stamp},
                    },
                )
                message.status = "processing"
                message.updated_at = stamp
            return pending

    def update_message(self, task_id: int, message_id: int, **changes: Any) -> TaskMessage | None:
        """Append an update event and return the newly folded message."""
        immutable = {"id", "created_at", "idempotency_key"}
        known = {item.name for item in fields(TaskMessage)} - immutable
        accepted = {key: value for key, value in changes.items() if key in known}
        path = self._thread_path(task_id)
        with self._thread_locked(task_id):
            messages = self._read_thread(path)
            message = next((item for item in messages if item.id == message_id), None)
            if message is None:
                return None
            stamp = now_iso()
            accepted["updated_at"] = stamp
            self._append_thread_event(
                path,
                {"event": "update", "message_id": message_id, "changes": accepted},
            )
            for key, value in accepted.items():
                setattr(message, key, value)
            return message

    def recover_processing_messages(self) -> int:
        """Return interrupted message claims to pending after runner startup."""
        recovered = 0
        try:
            paths = sorted(self.threads_dir.glob("task-*.jsonl"))
        except OSError:
            return 0
        for path in paths:
            try:
                task_id = int(path.stem.removeprefix("task-"))
            except ValueError:
                continue
            with self._thread_locked(task_id):
                messages = self._read_thread(path)
                processing = [item for item in messages if item.status == "processing"]
                if not processing:
                    continue
                stamp = now_iso()
                for message in processing:
                    self._append_thread_event(
                        path,
                        {
                            "event": "update",
                            "message_id": message.id,
                            "changes": {"status": "pending", "updated_at": stamp},
                        },
                    )
                recovered += len(processing)
        return recovered

    def message_media_path(self, task_id: int, message_id: int, suffix: str) -> Path:
        """A collision-free attachment path for one thread message."""
        directory = self.media_dir / f"task-{task_id:04d}"
        directory.mkdir(parents=True, exist_ok=True)
        safe = (
            suffix
            if suffix.startswith(".") and suffix[1:].isalnum() and len(suffix) <= 10
            else ".bin"
        )
        return directory / f"message-{message_id:06d}{safe}"

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

    def save_session(
        self,
        task_id: int,
        role: str | TaskSession,
        *,
        backend: str = "",
        session_id: str = "",
        context_tokens: int = 0,
    ) -> TaskSession | None:
        """Persist a task-scoped backend session under its agent role."""
        if isinstance(role, TaskSession):
            session = TaskSession(
                role=role.role,
                backend=role.backend,
                session_id=role.session_id,
                context_tokens=role.context_tokens,
                updated_at=now_iso(),
            )
        else:
            session = TaskSession(
                role=role,
                backend=backend,
                session_id=session_id,
                context_tokens=context_tokens,
                updated_at=now_iso(),
            )
        if not session.role:
            raise ValueError("a task session requires a role")
        with self._locked():
            tasks = self._read()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None:
                return None
            task.sessions[session.role] = session
            task.updated_at = session.updated_at
            self._write(tasks)
            return session

    def clear_session(self, task_id: int, role: str) -> bool:
        """Forget one resumable session without changing the task thread."""
        with self._locked():
            tasks = self._read()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None or role not in task.sessions:
                return False
            del task.sessions[role]
            task.updated_at = now_iso()
            self._write(tasks)
            return True

    def approve(
        self,
        task_id: int,
        *,
        branch: str,
        commit: str,
        requested_mode: str,
        base_branch: str,
        summary: str = "",
        review: str = "",
    ) -> Task | None:
        """Persist review approval before any operation can change the repo."""
        if not branch or not commit:
            raise ValueError("approved work requires both a branch and a commit")
        with self._locked():
            tasks = self._read()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None:
                return None
            task.branch = branch
            task.approved_sha = commit
            task.status = Status.APPROVED
            task.delivery = DeliveryRecord(
                requested_mode=requested_mode,
                status="pending",
                base_branch=base_branch,
                branch=branch,
                commit=commit,
                summary=summary,
                review=review,
            )
            task.updated_at = now_iso()
            self._write(tasks)
            return task

    def start_delivery(self, task_id: int, resolved_mode: str) -> Task | None:
        """Claim an approved delivery, recording the chosen strategy once."""
        with self._locked():
            tasks = self._read()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None or task.status not in (Status.APPROVED, Status.DELIVERING):
                return None
            record = task.delivery
            if not record.commit or not record.branch:
                return None
            if record.resolved_mode and record.resolved_mode != resolved_mode:
                raise ValueError(
                    f"delivery already resolved as {record.resolved_mode}, not {resolved_mode}"
                )
            record.resolved_mode = resolved_mode
            record.status = "running"
            record.stage = "preflight"
            record.attempts += 1
            task.status = Status.DELIVERING
            task.updated_at = now_iso()
            self._write(tasks)
            return task

    def complete_delivery(self, task_id: int, outcome: str, *, url: str = "") -> Task | None:
        with self._locked():
            tasks = self._read()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None:
                return None
            task.delivery.status = "succeeded"
            task.delivery.stage = "complete"
            task.delivery.outcome = outcome
            task.delivery.url = url
            task.url = url
            task.status = Status.DONE
            task.claimed_by = ""
            task.claimed_at = ""
            task.updated_at = now_iso()
            self._write(tasks)
            return task

    def update_delivery_stage(self, task_id: int, stage: str) -> Task | None:
        """Checkpoint a side-effecting step before it starts."""
        with self._locked():
            tasks = self._read()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None or task.status != Status.DELIVERING:
                return None
            task.delivery.stage = stage
            task.updated_at = now_iso()
            self._write(tasks)
            return task

    def block_delivery(self, task_id: int, *, stage: str, code: str, message: str) -> Task | None:
        with self._locked():
            tasks = self._read()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None:
                return None
            task.delivery.status = "blocked"
            task.delivery.stage = stage
            task.delivery.errors.append(
                DeliveryError(stage=stage, code=code, message=message, occurred_at=now_iso())
            )
            task.note = message
            task.status = Status.BLOCKED
            task.claimed_by = ""
            task.claimed_at = ""
            task.updated_at = now_iso()
            self._write(tasks)
            return task

    def retry_delivery(self, task_id: int) -> Task | None:
        """Requeue delivery only; fixer/reviewer attempt counters are untouched."""
        with self._locked():
            tasks = self._read()
            task = next((item for item in tasks if item.id == task_id), None)
            if (
                task is None
                or task.status != Status.BLOCKED
                or task.legacy_blocked
                or not task.delivery.commit
                or not task.delivery.branch
            ):
                return None
            task.delivery.status = "pending"
            task.delivery.stage = ""
            task.status = Status.APPROVED
            task.updated_at = now_iso()
            self._write(tasks)
            return task

    def retry_task(self, task_id: int) -> Task | None:
        """Requeue agent work without moving or deleting the old branch."""
        with self._locked():
            tasks = self._read()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None or task.status not in TERMINAL:
                return None
            stamp = now_iso()
            task.runs.append(
                TaskRun(
                    attempt=task.attempts,
                    status=task.status,
                    branch=task.branch,
                    approved_sha=task.approved_sha,
                    delivery=DeliveryRecord.from_dict(asdict(task.delivery)),
                    note=task.note,
                    url=task.url,
                    cost_usd=task.cost_usd,
                    started_at=task.claimed_at or task.created_at,
                    finished_at=task.updated_at or stamp,
                )
            )
            if task.branch and task.branch not in task.previous_branches:
                task.previous_branches.append(task.branch)
            task.branch = None
            task.approved_sha = ""
            task.delivery = DeliveryRecord()
            task.legacy_blocked = False
            task.url = ""
            task.cost_usd = 0.0
            task.status = Status.NEW
            task.note = ""
            task.claimed_by = ""
            task.claimed_at = ""
            task.updated_at = stamp
            self._write(tasks)
            return task

    def request_cancel(self, task_id: int) -> Task | None:
        with self._locked():
            tasks = self._read()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None:
                return None
            if task.status == Status.NEW:
                task.status = Status.CANCELLED
                task.note = "cancelled before it started"
                task.claimed_by = ""
                task.claimed_at = ""
            elif task.status in (Status.RUNNING, Status.REVIEW):
                task.status = Status.CANCELLING
                task.note = "cancellation requested"
            elif task.status != Status.CANCELLING:
                return None
            task.updated_at = now_iso()
            self._write(tasks)
            return task

    def complete_cancel(self, task_id: int, note: str) -> Task | None:
        with self._locked():
            tasks = self._read()
            task = next((item for item in tasks if item.id == task_id), None)
            if task is None or task.status != Status.CANCELLING:
                return None
            task.status = Status.CANCELLED
            task.note = note
            task.claimed_by = ""
            task.claimed_at = ""
            task.updated_at = now_iso()
            self._write(tasks)
            return task

    def delivery_tasks(self) -> list[Task]:
        """Recoverable work, oldest first, for runner startup recovery."""
        return sorted(
            (task for task in self._read() if task.status in (Status.APPROVED, Status.DELIVERING)),
            key=lambda task: task.id,
        )

    def recover_orphans(self) -> list[Task]:
        """Requeue agent work left behind by a runner that no longer owns the lease."""
        recovered: list[Task] = []
        with self._locked():
            tasks = self._read()
            for task in tasks:
                if task.status not in (Status.RUNNING, Status.REVIEW):
                    continue
                if task.branch and task.branch not in task.previous_branches:
                    task.previous_branches.append(task.branch)
                task.branch = None
                task.approved_sha = ""
                task.delivery = DeliveryRecord()
                task.status = Status.NEW
                task.note = "recovered after the previous runner stopped"
                task.claimed_by = ""
                task.claimed_at = ""
                task.updated_at = now_iso()
                recovered.append(task)
            if recovered:
                self._write(tasks)
        return recovered

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
            task.claimed_by = self.claimant
            task.claimed_at = now_iso()
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

    def archive(self, *, keep: int = 100) -> int:
        """Move old terminal tasks out of the hot queue, retaining the newest ``keep``."""
        keep = max(keep, 0)
        with self._locked():
            tasks = self._read()
            terminal = sorted(
                (task for task in tasks if task.status in TERMINAL),
                key=lambda task: task.id,
                reverse=True,
            )
            moving_ids = {task.id for task in terminal[keep:]}
            if not moving_ids:
                return 0
            archived_ids = self._archived_ids()
            fresh = sorted(
                (item for item in tasks if item.id in moving_ids and item.id not in archived_ids),
                key=lambda item: item.id,
            )
            if fresh:
                self.archive_path.parent.mkdir(parents=True, exist_ok=True)
                with self.archive_path.open("a", encoding="utf-8") as handle:
                    for task in fresh:
                        record = {
                            "schema": SCHEMA_VERSION,
                            "archived_at": now_iso(),
                            "task": task.to_dict(),
                        }
                        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                        handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            self._write([task for task in tasks if task.id not in moving_ids])
            return len(moving_ids)

    def archived(self, *, limit: int = 100) -> list[Task]:
        if limit <= 0:
            return []
        records: deque[Task] = deque(maxlen=limit)
        try:
            handle = self.archive_path.open(encoding="utf-8")
        except FileNotFoundError:
            return []
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw = record.get("task") if isinstance(record, dict) else None
                if isinstance(raw, dict):
                    records.append(Task.from_dict(raw))
        return list(reversed(records))

    def _archived_ids(self) -> set[int]:
        ids: set[int] = set()
        try:
            handle = self.archive_path.open(encoding="utf-8")
        except FileNotFoundError:
            return ids
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw = record.get("task") if isinstance(record, dict) else None
                if isinstance(raw, dict) and isinstance(raw.get("id"), int):
                    ids.add(raw["id"])
        return ids

    def media_path(self, task_id: int, suffix: str) -> Path:
        """Where an attachment for ``task_id`` goes. Creates the directory."""
        self.media_dir.mkdir(parents=True, exist_ok=True)
        safe = suffix if suffix.startswith(".") and len(suffix) <= 10 else ".bin"
        return self.media_dir / f"task-{task_id:04d}{safe}"

    # --- plumbing --------------------------------------------------------

    def _thread_path(self, task_id: int) -> Path:
        return self.threads_dir / f"task-{task_id:04d}.jsonl"

    @contextmanager
    def _thread_locked(self, task_id: int) -> Iterator[None]:
        """Serialize reads and appends for one thread across processes."""
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        path = self._thread_path(task_id)
        with exclusive_file(path.with_name(path.name + ".lock")):
            yield

    def _read_thread(self, path: Path) -> list[TaskMessage]:
        """Fold message and update events, ignoring torn/unknown records."""
        folded: dict[int, TaskMessage] = {}
        try:
            handle = path.open(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise QueueCorruptError(f"cannot read task thread {path}: {exc}") from exc
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                event = record.get("event")
                if event == "message":
                    raw = record.get("message")
                    if not isinstance(raw, dict):
                        continue
                    try:
                        message = TaskMessage.from_dict(raw)
                    except (TypeError, ValueError):
                        continue
                    if message.id > 0:
                        folded[message.id] = message
                elif event == "update":
                    try:
                        message_id = int(record.get("message_id", 0))
                    except (TypeError, ValueError):
                        continue
                    target = folded.get(message_id)
                    changes = record.get("changes")
                    if target is None or not isinstance(changes, dict):
                        continue
                    mutable = {item.name for item in fields(TaskMessage)} - {
                        "id",
                        "created_at",
                        "idempotency_key",
                    }
                    for key, value in changes.items():
                        if key in mutable:
                            setattr(target, key, value)
        return [folded[item_id] for item_id in sorted(folded)]

    def _append_thread_event(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            with path.open("ab+") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() > 0:
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        handle.write(b"\n")
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise QueueCorruptError(f"cannot append task thread {path}: {exc}") from exc

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Exclusive across processes for the length of one read-modify-write.

        The lock lives on a sidecar file rather than on the queue itself: the
        write replaces that inode, and a lock held on a replaced inode protects
        nothing.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        with exclusive_file(lock):
            yield

    def _read(self) -> list[Task]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as exc:
            raise self._corrupt(f"invalid JSON: {exc}") from exc
        except OSError as exc:
            raise QueueCorruptError(f"cannot read queue {self.path}: {exc}") from exc
        version = raw.get("version", 1) if isinstance(raw, dict) else 1
        version = version if isinstance(version, int) else 1
        items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise self._corrupt("the root does not contain a task list")
        if any(not isinstance(item, dict) for item in items):
            raise self._corrupt("the task list contains a non-object entry")
        try:
            return [Task.from_dict(item, schema_version=version) for item in items]
        except (TypeError, ValueError) as exc:
            raise self._corrupt(f"invalid task record: {exc}") from exc

    def _corrupt(self, reason: str) -> QueueCorruptError:
        backup: Path | None = None
        try:
            modified = datetime.fromtimestamp(self.path.stat().st_mtime, UTC)
            stamp = modified.strftime("%Y%m%dT%H%M%SZ")
            backup = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
            if not backup.exists():
                shutil.copy2(self.path, backup)
        except OSError:
            backup = None
        preserved = f"; preserved at {backup}" if backup is not None else ""
        return QueueCorruptError(f"queue {self.path} is corrupt: {reason}{preserved}")

    def _write(self, tasks: list[Task]) -> None:
        self._backup_legacy()
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "tasks": [task.to_dict() for task in sorted(tasks, key=lambda task: task.id)],
        }
        write_atomic(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    def _backup_legacy(self) -> None:
        """Keep the original older-schema queue once before migration."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        version = raw.get("version", 1) if isinstance(raw, dict) else 1
        version = version if isinstance(version, int) else 1
        backup = self.path.with_name(f"{self.path.stem}.v{version}.backup{self.path.suffix}")
        if version < SCHEMA_VERSION and not backup.exists():
            shutil.copy2(self.path, backup)


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` so a reader never sees half of it, whatever kills us."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Not a context manager: the file has to outlive the ``with`` below so
    # ``os.replace`` can move it into place, which is the atomicity.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        "w",
        dir=path.parent,
        prefix=path.name,
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


__all__ = [
    "ICONS",
    "SCHEMA_VERSION",
    "TERMINAL",
    "DeliveryError",
    "DeliveryRecord",
    "QueueCorruptError",
    "Status",
    "Task",
    "TaskMessage",
    "TaskRun",
    "TaskSession",
    "TaskStore",
    "now_iso",
    "write_atomic",
]
