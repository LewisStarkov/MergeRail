"""A front made of files, for everywhere a chat does not reach.

Drop a ``.md`` or ``.txt`` file into ``.agentq/inbox/`` and it becomes a task;
the outcome is written to ``.agentq/outbox/<id>.md``. That is enough to drive
this tool from cron, from CI, from a web form that writes a file, or from a
person with an editor — and it is the front that needs no credentials at all,
which makes it the honest default.
"""

from __future__ import annotations

from pathlib import Path

from .. import log
from ..tasks import Task, TaskStore
from .base import Front

SUFFIXES = (".md", ".txt")


class FolderFront(Front):
    name = "folder"

    def __init__(self, store: TaskStore, state_dir: Path) -> None:
        super().__init__(store)
        self.inbox = state_dir / "inbox"
        self.outbox = state_dir / "outbox"

    def start(self) -> None:
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)
        log.info("folder.watching", inbox=self.inbox)

    def next_task(self) -> Task | None:
        """Sweep the inbox into the queue, then claim from it as usual."""
        for path in sorted(self.inbox.iterdir()) if self.inbox.is_dir() else []:
            if not path.is_file() or path.suffix.lower() not in SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError as exc:  # pragma: no cover - unreadable file
                log.warn("folder.unreadable", path=path, error=exc)
                continue
            task = self.store.add(text, source=self.name, origin={"file": path.name})
            path.unlink(missing_ok=True)
            log.info("folder.accepted", task=task.id, file=path.name)
        return super().next_task()

    def report(self, task: Task, event: str, text: str) -> None:
        self.outbox.mkdir(parents=True, exist_ok=True)
        target = self.outbox / f"{task.id:04d}-{event}.md"
        body = f"# Task #{task.id} — {event}\n\n> {task.text}\n\n{text}\n"
        if task.url:
            body += f"\n{task.url}\n"
        target.write_text(body, encoding="utf-8")


__all__ = ["SUFFIXES", "FolderFront"]
