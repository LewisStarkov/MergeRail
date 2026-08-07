"""Claude Code backend and its backwards-compatible ``ClaudeAgent`` facade."""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import log, procs
from .base import (
    AgentReply,
    AgentSession,
    BackendCapabilities,
    BackendInfo,
    EventSink,
    NullEventSink,
    SessionSpec,
    TurnRequest,
    Usage,
)
from .process import ProcessController, parse_event, run_jsonl

ALLOWED_TOOLS = ("Bash", "Edit", "Write", "Read", "Glob", "Grep", "MultiEdit", "TodoWrite")
READ_ONLY_DENIED = ("Edit", "Write", "MultiEdit", "NotebookEdit")
PUSH_DENIED = "Bash(git push:*)"


@functools.cache
def cli_supports(flag: str) -> bool:
    """Whether the installed Claude Code understands ``flag``."""
    try:
        result = subprocess.run(
            ["claude", "--help"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return flag in result.stdout


@dataclass(frozen=True, slots=True)
class AgentOptions:
    """Legacy Claude-specific options retained for source compatibility."""

    permission: str = "acceptEdits"
    model: str | None = None
    effort: str = ""
    timeout: int = 3600
    context_limit: int = 160_000
    setting_sources: str = "project"


class _ClaudeConversation:
    def __init__(
        self,
        spec: SessionSpec,
        *,
        setting_sources: str = "project",
        events: EventSink | None = None,
    ) -> None:
        self.role = spec.role
        self.cwd = spec.cwd
        self.spec = spec
        self.setting_sources = setting_sources
        self.events = events or NullEventSink()
        self.session_id: str | None = None
        self.context_tokens = 0
        self._controller = ProcessController()

    @property
    def capabilities(self) -> BackendCapabilities:
        return claude_capabilities()

    def _command(self, request: TurnRequest) -> list[str]:
        args = [
            "claude",
            "-p",
            request.prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self.spec.system_prompt:
            args += ["--append-system-prompt", self.spec.system_prompt]
        if self.spec.permission == "skip":
            args.append("--dangerously-skip-permissions")
        else:
            permission = self.spec.permission
            if permission in {"safe", "review"}:
                permission = "acceptEdits"
            args += ["--permission-mode", permission]
            args += ["--allowed-tools", *ALLOWED_TOOLS]
        denied = [PUSH_DENIED, *(READ_ONLY_DENIED if self.spec.read_only else ())]
        args += ["--disallowed-tools", ",".join(denied)]
        if self.setting_sources and cli_supports("--setting-sources"):
            args += ["--setting-sources", self.setting_sources]
        if self.spec.effort and cli_supports("--effort"):
            args += ["--effort", self.spec.effort]
        if self.spec.model:
            args += ["--model", self.spec.model]
        if request.schema and cli_supports("--json-schema"):
            args += ["--json-schema", json.dumps(request.schema)]
        if request.max_cost_usd is not None and request.max_cost_usd > 0:
            args += ["--max-budget-usd", str(round(request.max_cost_usd, 4))]
        if self.session_id:
            args += ["--resume", self.session_id]
        return args

    def _ask(self, request: TurnRequest) -> AgentReply:
        log.info(
            "agent.start",
            backend="claude",
            role=self.role,
            resumed=bool(self.session_id),
            chars=len(request.prompt),
        )
        result = run_jsonl(
            self._command(request),
            cwd=self.cwd,
            timeout=self.spec.timeout,
            on_event=self._on_event,
            controller=self._controller,
        )
        final: dict[str, Any] | None = None
        turn_usage: dict[str, Any] = {}
        for event in result.events:
            if event.get("type") == "assistant":
                turn_usage = usage_of(event) or turn_usage
            elif event.get("type") == "result":
                final = event
        if result.timed_out:
            log.error("agent.timeout", backend="claude", role=self.role, seconds=self.spec.timeout)
        if final is None:
            log.error(
                "agent.no_result", backend="claude", role=self.role, code=result.returncode
            )
            reason = log.clip(result.stderr, 500) or "the agent produced no result"
            return AgentReply(reason, True, None, 0, result.seconds)
        return self._absorb(final, turn_usage, result.seconds)

    def _absorb(
        self, result: dict[str, Any], turn_usage: dict[str, Any], seconds: float
    ) -> AgentReply:
        usage = usage_from(turn_usage)
        context = usage.context_tokens or self.context_tokens
        self.context_tokens = context
        self.session_id = str(result.get("session_id") or "") or self.session_id
        structured = result.get("structured_output")
        raw_cost = result.get("total_cost_usd")
        cost = float(raw_cost) if isinstance(raw_cost, int | float) else None
        reply = AgentReply(
            text=str(result.get("result") or ""),
            is_error=bool(result.get("is_error")),
            cost_usd=cost,
            context_tokens=context,
            seconds=seconds,
            structured=structured if isinstance(structured, dict) else None,
            session_id=self.session_id,
            usage=usage if turn_usage else None,
        )
        log.info(
            "agent.done",
            backend="claude",
            role=self.role,
            context=context,
            cost=round(cost, 3) if cost is not None else None,
            seconds=round(seconds),
        )
        if context >= self.spec.context_limit:
            log.warn("agent.rotated", backend="claude", role=self.role, context=context)
            self.session_id = None
        return reply

    def _on_event(self, event: dict[str, Any]) -> None:
        self.events.emit(event)
        if event.get("type") == "assistant":
            self._echo(event)

    def _echo(self, event: dict[str, Any]) -> None:
        message = event.get("message")
        blocks = message.get("content", []) if isinstance(message, dict) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                log.raw(f"{self.role} │ {log.clip(str(block['text']), 300)}")
            elif block.get("type") == "tool_use":
                log.raw(f"{self.role} │ → {block.get('name')} {tool_hint(block)}")

    def _kill(self, process: subprocess.Popen[str]) -> None:  # pragma: no cover
        """Compatibility hook for callers that previously owned the watchdog."""
        log.error("agent.timeout", backend="claude", role=self.role, seconds=self.spec.timeout)
        procs.kill_tree(process)

    def close(self) -> None:
        """Claude's print-mode process ends after every turn; only its id persists."""

    def cancel(self) -> None:
        self._controller.cancel()


class ClaudeSession(_ClaudeConversation):
    """Provider-neutral Claude session returned by :class:`ClaudeBackend`."""

    def ask(self, request: TurnRequest) -> AgentReply:
        return self._ask(request)


class ClaudeAgent(_ClaudeConversation):
    """Original string-based API, kept while callers migrate to ``TurnRequest``."""

    def __init__(
        self,
        role: str,
        cwd: Path,
        options: AgentOptions,
        *,
        read_only: bool = False,
        system_prompt: str = "",
    ) -> None:
        self.options = options
        super().__init__(
            SessionSpec(
                role=role,
                cwd=cwd,
                system_prompt=system_prompt,
                read_only=read_only,
                model=options.model,
                effort=options.effort,
                permission=options.permission,
                timeout=options.timeout,
                context_limit=options.context_limit,
            ),
            setting_sources=options.setting_sources,
        )
        self.read_only = read_only
        self.system_prompt = system_prompt

    def command(
        self, prompt: str, *, schema: dict[str, Any] | None = None, max_usd: float = 0.0
    ) -> list[str]:
        return self._command(TurnRequest(prompt, schema, max_usd or None))

    def ask(
        self, prompt: str, *, schema: dict[str, Any] | None = None, max_usd: float = 0.0
    ) -> AgentReply:
        return self._ask(TurnRequest(prompt, schema, max_usd or None))


class ClaudeBackend:
    name = "claude"

    def probe(self) -> BackendInfo:
        executable = shutil.which("claude")
        if executable is None:
            return BackendInfo(
                self.name, False, capabilities=claude_capabilities(), reason="not found"
            )
        version: str | None = None
        try:
            checked = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            version = (checked.stdout or checked.stderr).strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
        return BackendInfo(self.name, True, version, claude_capabilities())

    def open_session(
        self, spec: SessionSpec, events: EventSink | None = None
    ) -> AgentSession:
        setting_sources = spec.settings.get("setting_sources", "project")
        return ClaudeSession(
            spec,
            setting_sources=str(setting_sources) if setting_sources is not None else "",
            events=events,
        )


def claude_capabilities() -> BackendCapabilities:
    return BackendCapabilities(
        native_resume=True,
        streaming=True,
        structured_output=cli_supports("--json-schema"),
        usage_reporting=True,
        context_reporting=True,
        exact_cost_reporting=True,
        native_budget_limit=True,
        native_read_only=True,
        native_push_denial=True,
    )


def usage_of(event: dict[str, Any]) -> dict[str, Any]:
    message = event.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    return usage if isinstance(usage, dict) else {}


def usage_from(raw: dict[str, Any]) -> Usage:
    def count(key: str) -> int | None:
        value = raw.get(key)
        return value if isinstance(value, int) and value >= 0 else None

    return Usage(
        input_tokens=count("input_tokens"),
        output_tokens=count("output_tokens"),
        cache_creation_input_tokens=count("cache_creation_input_tokens"),
        cache_read_input_tokens=count("cache_read_input_tokens"),
    )


def context_size(usage: dict[str, Any]) -> int:
    return usage_from(usage).context_tokens


def tool_hint(block: dict[str, Any]) -> str:
    raw = block.get("input")
    payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
    for key in ("command", "file_path", "pattern", "path", "description"):
        value = payload.get(key)
        if value:
            return log.clip(str(value), 120)
    return ""


__all__ = [
    "ALLOWED_TOOLS",
    "PUSH_DENIED",
    "READ_ONLY_DENIED",
    "AgentOptions",
    "ClaudeAgent",
    "ClaudeBackend",
    "ClaudeSession",
    "claude_capabilities",
    "cli_supports",
    "context_size",
    "parse_event",
    "tool_hint",
    "usage_of",
]
