"""Compatibility imports for the original Claude-only agent API.

New integrations should import the provider-neutral contracts from
``mergerail.backends`` and obtain sessions through a backend registry.
"""

from __future__ import annotations

from .backends.base import AgentReply
from .backends.claude import (
    ALLOWED_TOOLS,
    PUSH_DENIED,
    READ_ONLY_DENIED,
    AgentOptions,
    ClaudeAgent,
    cli_supports,
    context_size,
    parse_event,
    tool_hint,
    usage_of,
)

__all__ = [
    "ALLOWED_TOOLS",
    "PUSH_DENIED",
    "READ_ONLY_DENIED",
    "AgentOptions",
    "AgentReply",
    "ClaudeAgent",
    "cli_supports",
    "context_size",
    "parse_event",
    "tool_hint",
    "usage_of",
]
