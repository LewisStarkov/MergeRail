"""What a front is: two methods, and nothing else.

This is the whole reason the interface swaps in one line. A front takes work in
and reports back — Telegram, a folder, a web form, a GitHub issue label, Slack.
Everything a front needs to answer the right person is carried on the task
itself (``source`` and ``origin``), so the runner never learns what a chat id is.

The default implementation is store-backed because every front so far wants the
same queue semantics: durable, claimable, readable while the app is down. A
front that has its own queue (an issue tracker, say) overrides ``next_task``.
"""

from __future__ import annotations

from ..tasks import Task, TaskStore

#: What the runner announces. A front may render these however it likes.
EVENTS = ("started", "done", "failed", "blocked")


class Front:
    """Base class and the contract: implement ``report``, get everything else."""

    #: Goes onto every task this front creates.
    name = "front"

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Begin accepting work. Called once, before the loop."""

    def stop(self) -> None:
        """Stop accepting work. Called once, on the way out."""

    # --- the contract ----------------------------------------------------

    def next_task(self) -> Task | None:
        """Claim the next piece of work, or ``None`` if there is none."""
        return self.store.take_next()

    def report(self, task: Task, event: str, text: str) -> None:
        """Tell whoever asked what happened. Never fatal — this is a courtesy."""


__all__ = ["EVENTS", "Front"]
