"""Policies derived from backend capability declarations."""

from __future__ import annotations

from .base import BackendCapabilities


def strict_security_gaps(role: str, capabilities: BackendCapabilities) -> tuple[str, ...]:
    gaps: list[str] = []
    if role == "reviewer" and not capabilities.native_read_only:
        gaps.append("native read-only")
    if not capabilities.native_push_denial:
        gaps.append("native push denial")
    return tuple(gaps)


__all__ = ["strict_security_gaps"]
