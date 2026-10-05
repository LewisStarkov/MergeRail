"""Backend-registry adapter for disposable Docker sessions."""

from __future__ import annotations

from pathlib import Path

from ..backends.base import AgentSession, BackendInfo, EventSink, NullEventSink, SessionSpec
from .docker import DockerAgentSession, DockerExecution, DockerExecutionError


class DockerBackend:
    """Expose one named in-container backend through the normal registry API."""

    def __init__(self, execution: DockerExecution, name: str) -> None:
        self.execution = execution
        self.name = name.strip().lower()

    def probe(self) -> BackendInfo:
        return self.execution.probe(self.name)

    def open_session(self, spec: SessionSpec, events: EventSink | None = None) -> AgentSession:
        if Path(spec.cwd) != Path("/work/repo"):
            raise DockerExecutionError(
                "Docker backend sessions must use the virtual /work/repo checkout"
            )
        info = self.probe()
        if not info.available:
            detail = f": {info.reason}" if info.reason else ""
            raise DockerExecutionError(f"Docker backend {self.name!r} is unavailable{detail}")
        return DockerAgentSession(self.execution, self.name, spec, events or NullEventSink())


__all__ = ["DockerBackend"]
