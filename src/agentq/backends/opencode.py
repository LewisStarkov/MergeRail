"""OpenCode CLI backend using ``opencode run --format json``."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from typing import Any

from .base import (
    AgentReply,
    BackendCapabilities,
    BackendInfo,
    EventSink,
    NullEventSink,
    SessionSpec,
    TurnRequest,
    Usage,
)
from .process import JsonlProcessResult, ProcessController, run_jsonl

CAPABILITIES = BackendCapabilities(
    conversations=True,
    native_resume=True,
    streaming=True,
    structured_output=False,
    usage_reporting=True,
    context_reporting=False,
    exact_cost_reporting=True,
    native_budget_limit=False,
    native_read_only=True,
    native_push_denial=True,
)


class OpenCodeBackend:
    name = "opencode"

    def __init__(self, command: Sequence[str] = ("opencode",)) -> None:
        self._command = tuple(command)

    def probe(self) -> BackendInfo:
        try:
            result = subprocess.run(
                [*self._command, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return BackendInfo(self.name, False, capabilities=CAPABILITIES, reason=str(error))
        version = (result.stdout or result.stderr).strip() or None
        reason = "" if result.returncode == 0 else (version or "version probe failed")
        return BackendInfo(
            self.name,
            result.returncode == 0,
            version=version,
            capabilities=CAPABILITIES,
            reason=reason,
        )

    def open_session(self, spec: SessionSpec, events: EventSink | None = None) -> OpenCodeSession:
        return OpenCodeSession(self._command, spec, events or NullEventSink())


class OpenCodeSession:
    """A logical session resumed by id across one-shot OpenCode processes."""

    def __init__(self, command: Sequence[str], spec: SessionSpec, events: EventSink) -> None:
        self._executable = tuple(command)
        self.spec = spec
        self.events = events
        self.session_id: str | None = None
        self._controller = ProcessController()

    @property
    def capabilities(self) -> BackendCapabilities:
        return CAPABILITIES

    def command(self, prompt: str) -> list[str]:
        args = [
            *self._executable,
            "run",
            "--format",
            "json",
            "--dir",
            str(self.spec.cwd),
            "--auto",
        ]
        if self.session_id:
            args += ["--session", self.session_id]
        if self.spec.model:
            args += ["--model", self.spec.model]
        if self.spec.effort:
            args += ["--variant", self.spec.effort]
        configured_agent = self.spec.settings.get("agent")
        if isinstance(configured_agent, str) and configured_agent:
            args += ["--agent", configured_agent]
        args.append(prompt)
        return args

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        read_only = self.spec.read_only or self.spec.role == "reviewer"
        permissions: dict[str, object] = {
            "bash": {"*": "allow", "git push": "deny", "git push *": "deny"},
            "edit": "deny" if read_only else "allow",
            "external_directory": "deny",
        }
        if read_only:
            # A reviewer must not delegate an edit to a more permissive subagent.
            permissions["task"] = "deny"
        env["OPENCODE_PERMISSION"] = json.dumps(permissions, separators=(",", ":"))
        return env

    def ask(self, request: TurnRequest) -> AgentReply:
        prompt = request.prompt
        if self.spec.system_prompt and self.session_id is None:
            prompt = f"{self.spec.system_prompt}\n\n{prompt}"
        result = run_jsonl(
            self.command(prompt),
            cwd=self.spec.cwd,
            timeout=self.spec.timeout,
            env=self.environment(),
            on_event=self.events.emit,
            controller=self._controller,
        )
        return self._reply(result, wants_structured=request.schema is not None)

    def close(self) -> None:
        self.session_id = None

    def cancel(self) -> None:
        self._controller.cancel()

    def _reply(self, result: JsonlProcessResult, *, wants_structured: bool) -> AgentReply:
        texts: dict[str, str] = {}
        anonymous_text: list[str] = []
        errors: list[str] = []
        total_cost = 0.0
        saw_cost = False
        input_tokens = 0
        output_tokens = 0
        cache_read = 0
        cache_write = 0
        saw_usage = False

        for event in result.events:
            session_id = _string(event.get("sessionID") or event.get("session_id"))
            if session_id:
                self.session_id = session_id
            event_type = str(event.get("type") or "").replace("-", "_")
            part = event.get("part")
            payload = part if isinstance(part, dict) else event
            if event_type == "text":
                value = _string(payload.get("text"))
                identifier = _string(payload.get("id"))
                if identifier:
                    texts[identifier] = value
                elif value:
                    anonymous_text.append(value)
            elif event_type == "step_finish":
                tokens = payload.get("tokens")
                if isinstance(tokens, dict):
                    saw_usage = True
                    input_tokens += _integer(tokens.get("input")) or 0
                    output_tokens += _integer(tokens.get("output")) or 0
                    cache = tokens.get("cache")
                    if isinstance(cache, dict):
                        cache_read += _integer(cache.get("read")) or 0
                        cache_write += _integer(cache.get("write")) or 0
                cost = payload.get("cost")
                if isinstance(cost, int | float) and not isinstance(cost, bool):
                    saw_cost = True
                    total_cost += float(cost)
            elif event_type in {"error", "session_error"}:
                errors.append(_error_text(event))

        if result.timed_out:
            errors.append(f"opencode timed out after {self.spec.timeout} seconds")
        if result.cancelled:
            errors.append("opencode turn was cancelled")
        if result.returncode != 0:
            errors.append(result.stderr.strip() or f"opencode exited with {result.returncode}")

        text = "".join(anonymous_text) + "".join(texts.values())
        if not text and not errors:
            errors.append(result.stderr.strip() or "opencode produced no text result")
        usage = (
            Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_write,
                cache_read_input_tokens=cache_read,
            )
            if saw_usage
            else None
        )
        structured = _structured(text) if wants_structured else None
        context = usage.context_tokens if usage is not None else 0
        reply_session = self.session_id
        if context >= self.spec.context_limit:
            self.session_id = None
        reply_text = text
        if errors:
            error_text = "\n".join(message for message in errors if message)
            reply_text = f"{text}\n{error_text}".strip()
        return AgentReply(
            text=reply_text,
            is_error=bool(errors),
            cost_usd=total_cost if saw_cost else None,
            context_tokens=context,
            seconds=result.seconds,
            structured=structured,
            session_id=reply_session,
            usage=usage,
        )


def _structured(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _error_text(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        return _string(error.get("message") or error.get("name")) or "opencode failed"
    return _string(event.get("message") or error) or "opencode failed"


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


__all__ = ["OpenCodeBackend", "OpenCodeSession"]
