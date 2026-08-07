"""Normalize provider events into the small live-update vocabulary fronts use."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .fronts.base import StreamEvent


def normalize_agent_event(
    backend: str, role: str, event: Mapping[str, Any]
) -> list[StreamEvent]:
    kind = str(event.get("type") or "").replace("-", "_").replace(".", "_")
    if kind in {"error", "turn_failed", "session_error"}:
        message = _error_text(event)
        return [StreamEvent(role, "error", message)] if message else []

    if backend == "claude" and kind == "assistant":
        claude_message = event.get("message")
        blocks = (
            claude_message.get("content", [])
            if isinstance(claude_message, Mapping)
            else []
        )
        updates: list[StreamEvent] = []
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text" and (text := _string(block.get("text"))):
                updates.append(StreamEvent(role, "text", text))
            elif block_type == "tool_use":
                updates.append(StreamEvent(role, "tool", _tool_text(block)))
        return updates

    payload = event.get("part")
    source = payload if isinstance(payload, Mapping) else event

    if kind in {"text", "text_delta", "assistant_message"}:
        text = _string(source.get("text") or source.get("content"))
        return [StreamEvent(role, "text", text)] if text else []

    if backend == "codex" and kind in {"item_started", "item_completed"}:
        item = event.get("item")
        if not isinstance(item, Mapping):
            return []
        item_type = str(item.get("type") or "")
        if item_type == "agent_message":
            text = _string(item.get("text") or item.get("content"))
            return [StreamEvent(role, "text", text)] if text else []
        if kind == "item_started":
            return [StreamEvent(role, "tool", _tool_text(item))]
        return []

    if kind in {"tool", "tool_use", "tool_call", "mcp_tool_call"}:
        return [StreamEvent(role, "tool", _tool_text(source))]
    return []


def _tool_text(event: Mapping[str, Any]) -> str:
    name = _string(
        event.get("name")
        or event.get("tool")
        or event.get("type")
        or event.get("server")
    ) or "tool"
    values: list[object] = [event.get("input"), event.get("arguments"), event.get("state")]
    hint = ""
    for value in values:
        if not isinstance(value, Mapping):
            continue
        nested = value.get("input")
        candidate = nested if isinstance(nested, Mapping) else value
        for key in ("command", "file_path", "path", "query", "pattern", "description"):
            if text := _string(candidate.get(key)):
                hint = text
                break
        if hint:
            break
    if not hint:
        hint = _string(event.get("command") or event.get("path"))
    rendered = f"{name} · {hint}" if hint else name
    return rendered if len(rendered) <= 240 else rendered[:237] + "..."


def _error_text(event: Mapping[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, Mapping):
        return _string(error.get("message") or error.get("code"))
    return _string(event.get("message") or error)


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


__all__ = ["normalize_agent_event"]
