"""What a front is: two required methods and one optional live-update hook.

This is the whole reason the interface swaps in one line. A front takes work in
and reports back — Telegram, a folder, a web form, a GitHub issue label, Slack.
Everything a front needs to answer the right person is carried on the task
itself (``source`` and ``origin``), so the runner never learns what a chat id is.

The default implementation is store-backed because every front so far wants the
same queue semantics: durable, claimable, readable while the app is down. A
front that has its own queue (an issue tracker, say) overrides ``next_task``.

A third-party front is a subclass named as ``mypackage.fronts:SlackFront`` in
the config (or ``--front``); it is constructed as ``Class(store, config)``, so
its ``__init__`` must take the :class:`~agentq.tasks.TaskStore` and the
:class:`~agentq.config.Config` — everything else it needs (a token, a URL)
belongs in its own section of ``agentq.toml``, reachable via ``config.front``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..tasks import Task, TaskStore

#: What the runner announces. A front may render these however it likes.
EVENTS = ("started", "done", "failed", "blocked", "cancelled")


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One provider-neutral live update for a task."""

    role: str
    kind: str
    text: str = ""


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

    def stream(self, task: Task, event: StreamEvent) -> None:
        """Publish transient progress when the front supports live updates."""


__all__ = ["EVENTS", "Front", "StreamEvent"]
