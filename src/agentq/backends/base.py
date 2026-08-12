"""Provider-neutral contracts for command-line coding agents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Features a backend can actually guarantee, not merely emulate in a prompt."""

    conversations: bool = True
    native_resume: bool = False
    streaming: bool = False
    structured_output: bool = False
    usage_reporting: bool = False
    context_reporting: bool = False
    exact_cost_reporting: bool = False
    native_budget_limit: bool = False
    native_read_only: bool = False
    native_push_denial: bool = False
    attachments: bool = False


@dataclass(frozen=True, slots=True)
class BackendInfo:
    """The result of probing a backend without opening a session."""

    name: str
    available: bool
    version: str | None = None
    capabilities: BackendCapabilities = field(default_factory=BackendCapabilities)
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """Everything common to a role's long-lived agent session."""

    role: str
    cwd: Path
    system_prompt: str = ""
    read_only: bool = False
    model: str | None = None
    effort: str = ""
    permission: str = "safe"
    timeout: int = 3600
    context_limit: int = 160_000
    settings: Mapping[str, object] = field(default_factory=dict)
    resume_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """One prompt, plus the optional guarantees requested for its response."""

    prompt: str
    schema: dict[str, Any] | None = None
    max_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    """Provider-reported token counts; unknown values stay unknown."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    @property
    def context_tokens(self) -> int:
        return sum(
            value
            for value in (
                self.input_tokens,
                self.cache_creation_input_tokens,
                self.cache_read_input_tokens,
            )
            if isinstance(value, int) and value >= 0
        )


@dataclass(slots=True)
class AgentReply:
    """The normalized final result of one agent turn.

    The first six fields retain the original ``agentq.agent.AgentReply``
    constructor, so existing embedders can adopt the backend API gradually.
    """

    text: str
    is_error: bool
    cost_usd: float | None
    context_tokens: int
    seconds: float
    structured: dict[str, Any] | None = None
    session_id: str | None = None
    usage: Usage | None = None


@runtime_checkable
class EventSink(Protocol):
    """Receives normalized or provider-native streaming events."""

    def emit(self, event: Mapping[str, Any]) -> None: ...


class NullEventSink:
    """Default event sink for callers that only need the final reply."""

    def emit(self, event: Mapping[str, Any]) -> None:
        del event


@runtime_checkable
class AgentSession(Protocol):
    @property
    def capabilities(self) -> BackendCapabilities: ...

    def ask(self, request: TurnRequest) -> AgentReply: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class AgentBackend(Protocol):
    name: str

    def probe(self) -> BackendInfo: ...

    def open_session(self, spec: SessionSpec, events: EventSink | None = None) -> AgentSession: ...


__all__ = [
    "AgentBackend",
    "AgentReply",
    "AgentSession",
    "BackendCapabilities",
    "BackendInfo",
    "EventSink",
    "NullEventSink",
    "SessionSpec",
    "TurnRequest",
    "Usage",
]
