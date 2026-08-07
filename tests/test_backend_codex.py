from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentq.backends import codex
from agentq.backends.base import SessionSpec, TurnRequest
from agentq.backends.process import JsonlProcessResult


def result(*events: dict[str, Any], returncode: int = 0, stderr: str = "") -> JsonlProcessResult:
    return JsonlProcessResult(tuple(events), stderr, returncode, False, 1.25)


def test_codex_maps_roles_models_effort_and_resume(repo: Path) -> None:
    session = codex.CodexBackend(("fake-codex",)).open_session(
        SessionSpec(
            "reviewer",
            repo,
            read_only=True,
            model="gpt-test",
            effort="high",
        )
    )
    command = session.command("inspect")
    assert command[:3] == ["fake-codex", "exec", "--json"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--model") + 1] == "gpt-test"
    assert 'model_reasoning_effort="high"' in command

    session.session_id = "thread-1"
    resumed = session.command("again")
    assert resumed[-3:] == ["resume", "thread-1", "again"]


def test_codex_uses_a_temporary_schema_and_normalizes_events(repo: Path, monkeypatch: Any) -> None:
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        controller: object | None = None,
    ) -> JsonlProcessResult:
        del cwd, timeout, env, controller
        commands.append(command)
        schema_path = Path(command[command.index("--output-schema") + 1])
        assert json.loads(schema_path.read_text(encoding="utf-8")) == {"type": "object"}
        events = (
            {"type": "thread.started", "thread_id": "thread-7"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"verdict":"approve"}'},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 40, "cached_input_tokens": 10, "output_tokens": 4},
            },
        )
        if on_event is not None:
            for event in events:
                on_event(event)
        return result(*events)

    monkeypatch.setattr(codex, "run_jsonl", fake_run)
    session = codex.CodexBackend(("fake-codex",)).open_session(
        SessionSpec("reviewer", repo, system_prompt="standing rules", read_only=True)
    )
    reply = session.ask(TurnRequest("review", schema={"type": "object"}))

    assert reply.text == '{"verdict":"approve"}'
    assert reply.structured == {"verdict": "approve"}
    assert reply.session_id == "thread-7"
    assert reply.context_tokens == 50
    assert reply.cost_usd is None
    assert not reply.is_error
    assert "standing rules" in commands[0][-1]


def test_codex_reports_a_failed_turn_without_claiming_zero_cost(
    repo: Path, monkeypatch: Any
) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> JsonlProcessResult:
        del args, kwargs
        return result({"type": "error", "message": "bad credentials"}, returncode=1)

    monkeypatch.setattr(codex, "run_jsonl", fake_run)
    session = codex.CodexBackend(("fake-codex",)).open_session(SessionSpec("fixer", repo))
    reply = session.ask(TurnRequest("fix"))
    assert reply.is_error
    assert "bad credentials" in reply.text
    assert reply.cost_usd is None
