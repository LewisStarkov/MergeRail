from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mergerail.backends import codex
from mergerail.backends.base import SessionSpec, TurnRequest
from mergerail.backends.process import JsonlProcessResult


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


def test_codex_skip_permission_disables_sandbox_only_for_fixer(repo: Path) -> None:
    fixer = codex.CodexBackend(("fake-codex",)).open_session(
        SessionSpec("fixer", repo, permission="skip")
    )
    fixer_command = fixer.command("inspect")
    assert "--dangerously-bypass-approvals-and-sandbox" in fixer_command
    assert "--sandbox" not in fixer_command

    reviewer = codex.CodexBackend(("fake-codex",)).open_session(
        SessionSpec("reviewer", repo, permission="skip")
    )
    reviewer_command = reviewer.command("inspect")
    assert "--dangerously-bypass-approvals-and-sandbox" not in reviewer_command
    assert reviewer_command[reviewer_command.index("--sandbox") + 1] == "read-only"


def test_codex_resumes_an_initial_session_without_repeating_the_system_prompt(
    repo: Path, monkeypatch: Any
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> JsonlProcessResult:
        del kwargs
        commands.append(command)
        return result(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "continued"},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 5}},
        )

    monkeypatch.setattr(codex, "run_jsonl", fake_run)
    session = codex.CodexBackend(("fake-codex",)).open_session(
        SessionSpec(
            "fixer",
            repo,
            system_prompt="standing rules",
            resume_session_id="thread-old",
        )
    )

    reply = session.ask(TurnRequest("continue"))

    assert commands[0][-3:] == ["resume", "thread-old", "continue"]
    assert "standing rules" not in commands[0]
    assert reply.session_id == "thread-old"


def test_codex_rotation_returns_no_resumable_session(repo: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        codex,
        "run_jsonl",
        lambda *args, **kwargs: result(
            {"type": "thread.started", "thread_id": "thread-7"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 20}},
        ),
    )
    session = codex.CodexBackend(("fake-codex",)).open_session(
        SessionSpec("fixer", repo, context_limit=20)
    )

    reply = session.ask(TurnRequest("work"))

    assert reply.session_id is None
    assert session.session_id is None


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
