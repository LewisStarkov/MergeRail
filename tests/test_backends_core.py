from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from mergerail.backends.base import (
    AgentReply,
    BackendCapabilities,
    BackendInfo,
    SessionSpec,
    TurnRequest,
    Usage,
)
from mergerail.backends.claude import AgentOptions, ClaudeAgent, ClaudeBackend, ClaudeSession
from mergerail.backends.process import (
    JsonlProcessResult,
    ProcessController,
    parse_event,
    run_jsonl,
)
from mergerail.backends.registry import BackendRegistry, BackendRegistryError, default_registry


class FakeBackend:
    name = "fake"

    def probe(self) -> BackendInfo:
        return BackendInfo(self.name, True, "1", BackendCapabilities())

    def open_session(self, spec: SessionSpec, events: object = None) -> ClaudeSession:
        del events
        return ClaudeSession(spec)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))


def test_usage_keeps_unknown_counts_and_measures_context() -> None:
    usage = Usage(input_tokens=5, cache_creation_input_tokens=3, cache_read_input_tokens=2)
    assert usage.context_tokens == 10
    assert usage.output_tokens is None


def test_registry_is_explicit_and_case_insensitive(tmp_path: Path) -> None:
    registry = BackendRegistry([FakeBackend()])
    assert registry.names() == ("fake",)
    assert registry.get("FAKE").probe().available
    assert registry.open_session("fake", SessionSpec("fixer", tmp_path)).capabilities
    with pytest.raises(ValueError, match="already registered"):
        registry.register_external(FakeBackend())
    with pytest.raises(BackendRegistryError, match="unknown backend"):
        registry.get("missing")


def test_default_registry_contains_the_builtin_backends() -> None:
    assert default_registry().names() == ("claude", "codex", "opencode")


def test_claude_maps_neutral_permissions_and_enforces_review_denials(tmp_path: Path) -> None:
    session = ClaudeSession(SessionSpec("reviewer", tmp_path, read_only=True, permission="review"))
    command = session._command(TurnRequest("review"))
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert "Edit" in command[command.index("--disallowed-tools") + 1]
    assert "git push" in command[command.index("--disallowed-tools") + 1]


def test_claude_passes_supported_schema_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mergerail.backends.claude.cli_supports", lambda flag: True)
    session = ClaudeSession(SessionSpec("reviewer", tmp_path))
    command = session._command(
        TurnRequest("review", schema={"type": "object"}, max_cost_usd=0.12567)
    )
    assert command[command.index("--json-schema") + 1] == '{"type": "object"}'
    assert command[command.index("--max-budget-usd") + 1] == "0.1257"


def test_claude_backend_returns_a_protocol_session(tmp_path: Path) -> None:
    opened = ClaudeBackend().open_session(SessionSpec("fixer", tmp_path))
    assert isinstance(opened, ClaudeSession)
    assert opened.capabilities.native_resume


def test_claude_absorbs_session_usage_cost_and_schema(tmp_path: Path) -> None:
    session = ClaudeSession(SessionSpec("fixer", tmp_path, context_limit=100))
    reply = session._absorb(
        {
            "session_id": "thread-1",
            "result": "done",
            "total_cost_usd": 0.12,
            "structured_output": {"verdict": "APPROVE"},
        },
        {"input_tokens": 20, "cache_read_input_tokens": 5, "output_tokens": 2},
        1.5,
    )
    assert reply == AgentReply(
        "done",
        False,
        0.12,
        25,
        1.5,
        {"verdict": "APPROVE"},
        "thread-1",
        Usage(input_tokens=20, output_tokens=2, cache_read_input_tokens=5),
    )


def test_claude_streams_events_and_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = RecordingSink()
    session = ClaudeSession(SessionSpec("fixer", tmp_path), events=sink)
    seen_commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> JsonlProcessResult:
        seen_commands.append(command)
        events = (
            {"type": "assistant", "message": {"usage": {"input_tokens": 8}}},
            {"type": "result", "session_id": "thread-1", "result": "ok"},
        )
        callback = kwargs["on_event"]
        for event in events:
            callback(event)
        return JsonlProcessResult(events, "", 0, False, 0.01)

    monkeypatch.setattr("mergerail.backends.claude.run_jsonl", fake_run)
    assert session.ask(TurnRequest("first")).text == "ok"
    assert session.ask(TurnRequest("second")).text == "ok"
    assert "--resume" not in seen_commands[0]
    assert seen_commands[1][-2:] == ["--resume", "thread-1"]
    assert [event["type"] for event in sink.events] == [
        "assistant",
        "result",
        "assistant",
        "result",
    ]


def test_claude_resumes_an_initial_session_without_repeating_the_system_prompt(
    tmp_path: Path,
) -> None:
    session = ClaudeSession(
        SessionSpec(
            "fixer",
            tmp_path,
            system_prompt="standing rules",
            resume_session_id="thread-old",
        )
    )

    command = session._command(TurnRequest("continue"))

    assert command[:3] == ["claude", "-p", "continue"]
    assert command[-2:] == ["--resume", "thread-old"]
    assert "--append-system-prompt" not in command


def test_claude_rotation_returns_no_resumable_session(tmp_path: Path) -> None:
    session = ClaudeSession(SessionSpec("fixer", tmp_path, context_limit=20))

    reply = session._absorb(
        {"session_id": "thread-1", "result": "done"},
        {"input_tokens": 20},
        0.1,
    )

    assert reply.session_id is None
    assert session.session_id is None


def test_legacy_claude_agent_api_still_builds_the_same_request(tmp_path: Path) -> None:
    legacy = ClaudeAgent("fixer", tmp_path, AgentOptions(permission="safe"))
    assert legacy.command("hello")[:3] == ["claude", "-p", "hello"]
    legacy.session_id = "old"
    assert legacy.command("again")[-2:] == ["--resume", "old"]


def test_jsonl_process_ignores_junk_and_reports_timeout(tmp_path: Path) -> None:
    script = (
        "import time; print('junk', flush=True); "
        'print(\'{"type":"ready"}\', flush=True); time.sleep(60)'
    )
    result = run_jsonl([sys.executable, "-c", script], cwd=tmp_path, timeout=0.2)
    assert result.timed_out
    assert result.events == ({"type": "ready"},)
    assert result.returncode != 0


def test_jsonl_process_can_be_cancelled_with_its_tree(tmp_path: Path) -> None:
    controller = ProcessController()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_jsonl,
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            timeout=30,
            controller=controller,
        )
        time.sleep(0.1)
        controller.cancel()
        result = future.result(timeout=5)
    assert result.cancelled
    assert result.returncode != 0


def test_parse_event_only_accepts_json_objects() -> None:
    assert parse_event("not json") is None
    assert parse_event("[]") is None
    assert parse_event('{"type":"result"}') == {"type": "result"}


def test_no_result_preserves_stderr_and_unknown_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = ClaudeSession(SessionSpec("fixer", tmp_path))
    failed = JsonlProcessResult((), "bad flag\n", 2, False, 0.1)
    monkeypatch.setattr("mergerail.backends.claude.run_jsonl", lambda *args, **kwargs: failed)
    reply = session.ask(TurnRequest("work"))
    assert reply.is_error
    assert reply.text == "bad flag"
    assert reply.cost_usd is None
