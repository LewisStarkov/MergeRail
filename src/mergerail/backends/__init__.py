"""Backend-neutral agent sessions and built-in CLI adapters."""

from __future__ import annotations

from .base import (
    AgentBackend,
    AgentReply,
    AgentSession,
    BackendCapabilities,
    BackendInfo,
    EventSink,
    NullEventSink,
    SessionSpec,
    TurnDiagnostics,
    TurnRequest,
    Usage,
)
from .claude import AgentOptions, ClaudeAgent, ClaudeBackend, ClaudeSession
from .codex import CodexBackend, CodexSession
from .external import ExternalBackend, ExternalSession, ProtocolError
from .opencode import OpenCodeBackend, OpenCodeSession
from .registry import BackendRegistry, BackendRegistryError, default_registry

__all__ = [
    "AgentBackend",
    "AgentOptions",
    "AgentReply",
    "AgentSession",
    "BackendCapabilities",
    "BackendInfo",
    "BackendRegistry",
    "BackendRegistryError",
    "ClaudeAgent",
    "ClaudeBackend",
    "ClaudeSession",
    "CodexBackend",
    "CodexSession",
    "EventSink",
    "ExternalBackend",
    "ExternalSession",
    "NullEventSink",
    "OpenCodeBackend",
    "OpenCodeSession",
    "ProtocolError",
    "SessionSpec",
    "TurnDiagnostics",
    "TurnRequest",
    "Usage",
    "default_registry",
]
