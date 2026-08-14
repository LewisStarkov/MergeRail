"""A third-party front, as small as the contract allows — for the loader test."""

from __future__ import annotations

from mergerail.config import Config
from mergerail.fronts.base import Front
from mergerail.tasks import Task, TaskStore


class EchoFront(Front):
    name = "echo"

    def __init__(self, store: TaskStore, config: Config) -> None:
        super().__init__(store)
        self.config = config
        self.reported: list[tuple[int, str]] = []

    def report(self, task: Task, event: str, text: str) -> None:
        self.reported.append((task.id, event))
