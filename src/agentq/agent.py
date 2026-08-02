"""Claude Code as a subprocess, with a conversation that survives between rounds.

Two things here are not obvious and are the reason this module exists at all.

**The session is resumed.** Every ``claude -p`` call after the first passes
``--resume``. A fixer that has already read the codebase does not read it again
for the next task; it walks in knowing where things are. That is most of what
makes the second task cheaper than the first.

**The session is measured honestly.** The ``result`` event's usage is the sum
over every turn of the run, so a thirty-turn task reports millions of tokens and
would rotate a session nowhere near full. What actually matters is the *last
assistant turn's* usage: what that turn had in front of it is how long the
conversation now is. When that number approaches the session ceiling the id is
dropped and the next call starts fresh. Warm until it cannot be, then honestly
cold.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import log

#: Tools the agents get when the permission mode is not ``skip``.
ALLOWED_TOOLS = ("Bash", "Edit", "Write", "Read", "Glob", "Grep", "MultiEdit", "TodoWrite")

#: The reviewer reads and runs; it does not write. Enforced by the CLI rather
#: than by the prompt, because a prompt is a request and this is a rule.
READ_ONLY_DENIED = "Edit,Write,MultiEdit,NotebookEdit"


@dataclass(slots=True)
class AgentReply:
    text: str
    is_error: bool
    cost_usd: float
    context_tokens: int
    seconds: float


@dataclass(frozen=True, slots=True)
class AgentOptions:
    """Everything the CLI invocation needs that is not the prompt."""

    permission: str = "acceptEdits"
    model: str | None = None
    max_usd: float = 0.0
    timeout: int = 3600
    context_limit: int = 300_000


class ClaudeAgent:
    """One role, one directory, one conversation for as long as it fits."""

    def __init__(
        self, role: str, cwd: Path, options: AgentOptions, *, read_only: bool = False
    ) -> None:
        self.role = role
        self.cwd = cwd
        self.options = options
        self.read_only = read_only
        self.session_id: str | None = None
        self.context_tokens = 0

    def command(self, prompt: str) -> list[str]:
        args = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
        if self.options.permission == "skip":
            args.append("--dangerously-skip-permissions")
        else:
            args += ["--permission-mode", self.options.permission]
            args += ["--allowed-tools", *ALLOWED_TOOLS]
        if self.read_only:
            args += ["--disallowed-tools", READ_ONLY_DENIED]
        if self.options.model:
            args += ["--model", self.options.model]
        if self.options.max_usd > 0:
            args += ["--max-budget-usd", str(self.options.max_usd)]
        if self.session_id:
            args += ["--resume", self.session_id]
        return args

    def ask(self, prompt: str) -> AgentReply:
        started = time.monotonic()
        log.info("agent.start", role=self.role, resumed=bool(self.session_id), chars=len(prompt))
        process = subprocess.Popen(
            self.command(prompt),
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        watchdog = threading.Timer(self.options.timeout, self._kill, args=(process,))
        watchdog.start()
        result: dict[str, Any] | None = None
        turn_usage: dict[str, Any] = {}
        try:
            stream = process.stdout
            assert stream is not None
            for raw in stream:
                event = parse_event(raw)
                if event is None:
                    continue
                if event.get("type") == "assistant":
                    self._echo(event)
                    turn_usage = usage_of(event) or turn_usage
                elif event.get("type") == "result":
                    result = event
            process.wait()
        finally:
            watchdog.cancel()

        stderr = (process.stderr.read() if process.stderr else "") or ""
        seconds = time.monotonic() - started
        if result is None:
            # The CLI died before it emitted a result event: a bad flag, a
            # session that could not be resumed, or the watchdog.
            log.error("agent.no_result", role=self.role, code=process.returncode)
            reason = log.clip(stderr, 500) or "the agent produced no result"
            return AgentReply(reason, True, 0.0, 0, seconds)
        return self._absorb(result, turn_usage, seconds)

    def _absorb(
        self, result: dict[str, Any], turn_usage: dict[str, Any], seconds: float
    ) -> AgentReply:
        # A missing usage block is not evidence that the session shrank, so the
        # previous measurement stands.
        context = context_size(turn_usage) or self.context_tokens
        self.context_tokens = context
        self.session_id = str(result.get("session_id") or "") or self.session_id
        reply = AgentReply(
            text=str(result.get("result") or ""),
            is_error=bool(result.get("is_error")),
            cost_usd=float(result.get("total_cost_usd") or 0.0),
            context_tokens=context,
            seconds=seconds,
        )
        log.info(
            "agent.done",
            role=self.role,
            context=context,
            cost=round(reply.cost_usd, 3),
            seconds=round(seconds),
        )
        if context >= self.options.context_limit:
            # The next round would not fit. Say so rather than letting the CLI
            # fail mid-task on a resume that cannot be loaded.
            log.warn("agent.rotated", role=self.role, context=context)
            self.session_id = None
        return reply

    def _echo(self, event: dict[str, Any]) -> None:
        """Real time: what the agent is saying, and what it just reached for."""
        message = event.get("message")
        blocks = message.get("content", []) if isinstance(message, dict) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                log.raw(f"{self.role} │ {log.clip(str(block['text']), 300)}")
            elif block.get("type") == "tool_use":
                log.raw(f"{self.role} │ → {block.get('name')} {tool_hint(block)}")

    def _kill(self, process: subprocess.Popen[str]) -> None:  # pragma: no cover - timeout path
        log.error("agent.timeout", role=self.role, seconds=self.options.timeout)
        process.kill()


def parse_event(raw: str) -> dict[str, Any] | None:
    line = raw.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def usage_of(event: dict[str, Any]) -> dict[str, Any]:
    """The ``usage`` block of one assistant turn, if it carries one."""
    message = event.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    return usage if isinstance(usage, dict) else {}


def context_size(usage: dict[str, Any]) -> int:
    """How many tokens a turn had in front of it: fresh, cache-written, cached.

    All three are context. Splitting them is a billing question; the session
    ceiling counts them together.
    """
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
    return total


def tool_hint(block: dict[str, Any]) -> str:
    """The one field of a tool call that tells you what is going on."""
    raw = block.get("input")
    payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
    for key in ("command", "file_path", "pattern", "path", "description"):
        value = payload.get(key)
        if value:
            return log.clip(str(value), 120)
    return ""


__all__ = [
    "ALLOWED_TOOLS",
    "READ_ONLY_DENIED",
    "AgentOptions",
    "AgentReply",
    "ClaudeAgent",
    "context_size",
    "parse_event",
    "tool_hint",
    "usage_of",
]
