"""Codex CLI backend using its non-interactive JSONL interface."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
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
    structured_output=True,
    usage_reporting=True,
    context_reporting=True,
    exact_cost_reporting=False,
    native_budget_limit=False,
    native_read_only=True,
    native_push_denial=False,
)


class CodexBackend:
    """Open provider-neutral sessions backed by ``codex exec``."""

    name = "codex"

    def __init__(self, command: Sequence[str] = ("codex",)) -> None:
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

    def open_session(self, spec: SessionSpec, events: EventSink | None = None) -> CodexSession:
        return CodexSession(self._command, spec, events or NullEventSink())


class CodexSession:
    """One resumable Codex thread."""

    def __init__(self, command: Sequence[str], spec: SessionSpec, events: EventSink) -> None:
        self._executable = tuple(command)
        self.spec = spec
        self.events = events
        self.session_id: str | None = None
        self._controller = ProcessController()

    @property
    def capabilities(self) -> BackendCapabilities:
        return CAPABILITIES

    def command(self, prompt: str, *, schema_path: Path | None = None) -> list[str]:
        sandbox = (
            "read-only"
            if self.spec.read_only or self.spec.role == "reviewer"
            else "workspace-write"
        )
        args = [
            *self._executable,
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "--cd",
            str(self.spec.cwd),
        ]
        if self.spec.model:
            args += ["--model", self.spec.model]
        if self.spec.effort:
            # This is a command-line config override, not a user config edit.
            effort = self.spec.effort.replace("\\", "\\\\").replace('"', '\\"')
            args += ["--config", f'model_reasoning_effort="{effort}"']
        if schema_path is not None:
            args += ["--output-schema", str(schema_path)]
        if self.session_id:
            args += ["resume", self.session_id, prompt]
        else:
            args.append(prompt)
        return args

    def ask(self, request: TurnRequest) -> AgentReply:
        prompt = request.prompt
        if self.spec.system_prompt and self.session_id is None:
            prompt = f"{self.spec.system_prompt}\n\n{prompt}"

        if request.schema is None:
            result = self._run(self.command(prompt))
        else:
            with tempfile.TemporaryDirectory(prefix="agentq-codex-") as directory:
                schema_path = Path(directory) / "output-schema.json"
                schema_path.write_text(json.dumps(request.schema), encoding="utf-8")
                result = self._run(self.command(prompt, schema_path=schema_path))
        return self._reply(result, expects_structured=request.schema is not None)

    def close(self) -> None:
        self.session_id = None

    def cancel(self) -> None:
        self._controller.cancel()

    def _run(self, command: list[str]) -> JsonlProcessResult:
        return run_jsonl(
            command,
            cwd=self.spec.cwd,
            timeout=self.spec.timeout,
            on_event=self.events.emit,
            controller=self._controller,
        )

    def _reply(self, result: JsonlProcessResult, *, expects_structured: bool) -> AgentReply:
        text = ""
        error_messages: list[str] = []
        usage: Usage | None = None
        saw_completion = False

        for event in result.events:
            event_type = str(event.get("type") or "")
            if event_type == "thread.started":
                thread_id = _string(event.get("thread_id") or event.get("threadId"))
                if thread_id:
                    self.session_id = thread_id
            elif event_type == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    message = _string(item.get("text") or item.get("content"))
                    if message:
                        text = message
            elif event_type == "turn.completed":
                saw_completion = True
                usage = _usage(event.get("usage")) or usage
            elif event_type in {"turn.failed", "error"}:
                error_messages.append(_error_text(event))

        if result.timed_out:
            error_messages.append(f"codex timed out after {self.spec.timeout} seconds")
        if result.cancelled:
            error_messages.append("codex turn was cancelled")
        if result.returncode != 0:
            error_messages.append(result.stderr.strip() or f"codex exited with {result.returncode}")
        if not saw_completion and not error_messages:
            error_messages.append(result.stderr.strip() or "codex produced no completed turn")
        if saw_completion and not text and not error_messages:
            error_messages.append("codex completed without an agent message")

        structured = _structured(text) if expects_structured else None
        context = usage.context_tokens if usage is not None else 0
        reply_session = self.session_id
        if context >= self.spec.context_limit:
            self.session_id = None
        reply_text = text
        if error_messages:
            error_text = "\n".join(message for message in error_messages if message)
            reply_text = f"{text}\n{error_text}".strip()
        return AgentReply(
            text=reply_text,
            is_error=bool(error_messages),
            cost_usd=None,
            context_tokens=context,
            seconds=result.seconds,
            structured=structured,
            session_id=reply_session,
            usage=usage,
        )


def _usage(value: object) -> Usage | None:
    if not isinstance(value, dict):
        return None
    return Usage(
        input_tokens=_integer(value.get("input_tokens")),
        output_tokens=_integer(value.get("output_tokens")),
        cache_read_input_tokens=_integer(
            value.get("cached_input_tokens") or value.get("cache_read_input_tokens")
        ),
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
        return _string(error.get("message") or error.get("code")) or "codex failed"
    return _string(event.get("message") or error) or "codex failed"


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


__all__ = ["CodexBackend", "CodexSession"]
