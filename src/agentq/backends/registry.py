"""Backend discovery without provider knowledge in the runner."""

from __future__ import annotations

import importlib
from collections.abc import Iterable

from .base import AgentBackend, AgentSession, BackendInfo, EventSink, SessionSpec

_BUILTINS = (
    ("agentq.backends.claude", "ClaudeBackend"),
    ("agentq.backends.codex", "CodexBackend"),
    ("agentq.backends.opencode", "OpenCodeBackend"),
)


class BackendRegistryError(LookupError):
    pass


class BackendRegistry:
    """A small explicit registry; importing a package never launches a tool."""

    def __init__(self, backends: Iterable[AgentBackend] = ()) -> None:
        self._backends: dict[str, AgentBackend] = {}
        for backend in backends:
            self.register(backend)

    def register(self, backend: AgentBackend, *, replace: bool = False) -> None:
        name = backend.name.strip().lower()
        if not name:
            raise ValueError("backend name cannot be empty")
        if name in self._backends and not replace:
            raise ValueError(f"backend already registered: {name}")
        self._backends[name] = backend

    def register_external(self, backend: AgentBackend, *, replace: bool = False) -> None:
        """Register an operator-configured driver; external commands are never inferred."""
        self.register(backend, replace=replace)

    def get(self, name: str) -> AgentBackend:
        key = name.strip().lower()
        try:
            return self._backends[key]
        except KeyError as error:
            choices = ", ".join(self.names()) or "none"
            raise BackendRegistryError(
                f"unknown backend {name!r}; registered backends: {choices}"
            ) from error

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._backends))

    def probe(self, name: str | None = None) -> BackendInfo | dict[str, BackendInfo]:
        if name is not None:
            return self.get(name).probe()
        return {key: backend.probe() for key, backend in sorted(self._backends.items())}

    def open_session(
        self,
        name: str,
        spec: SessionSpec,
        events: EventSink | None = None,
    ) -> AgentSession:
        return self.get(name).open_session(spec, events)


def default_registry() -> BackendRegistry:
    """Build the built-in registry, tolerating optional backend modules."""
    registry = BackendRegistry()
    for module_name, class_name in _BUILTINS:
        try:
            module = importlib.import_module(module_name)
            backend_type = getattr(module, class_name)
            backend = backend_type()
        except (ImportError, AttributeError):
            continue
        registry.register(backend)
    return registry


__all__ = ["BackendRegistry", "BackendRegistryError", "default_registry"]
