from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mergerail.backends import opencode
from mergerail.backends.base import SessionSpec, TurnRequest
from mergerail.backends.process import JsonlProcessResult


def test_opencode_builds_resume_model_agent_and_permission_options(repo: Path) -> None:
    session = opencode.OpenCodeBackend(("fake-opencode",)).open_session(
        SessionSpec(
            "reviewer",
            repo,
            read_only=True,
            model="openai/test",
            effort="high",
            settings={"agent": "audit"},
        )
    )
    session.session_id = "ses_1"
    command = session.command("review")
    assert command[:4] == ["fake-opencode", "run", "--format", "json"]
    assert command[command.index("--session") + 1] == "ses_1"
    assert command[command.index("--model") + 1] == "openai/test"
    assert command[command.index("--variant") + 1] == "high"
    assert command[command.index("--agent") + 1] == "audit"

    permission = json.loads(session.environment()["OPENCODE_PERMISSION"])
    assert permission["edit"] == "deny"
    assert permission["task"] == "deny"
    assert permission["bash"]["git push *"] == "deny"


def test_opencode_parses_cumulative_text_usage_and_exact_cost(repo: Path, monkeypatch: Any) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> JsonlProcessResult:
        commands.append(command)
        callback = kwargs["on_event"]
        events = (
            {"type": "text", "sessionID": "ses_9", "part": {"id": "p1", "text": "hel"}},
            {
                "type": "text",
                "sessionID": "ses_9",
                "part": {"id": "p1", "text": "hello"},
            },
            {
                "type": "step_finish",
                "sessionID": "ses_9",
                "part": {
                    "cost": 0.125,
                    "tokens": {
                        "input": 20,
                        "output": 5,
                        "cache": {"read": 4, "write": 2},
                    },
                },
            },
        )
        for event in events:
            callback(event)
        return JsonlProcessResult(events, "", 0, False, 0.5)

    monkeypatch.setattr(opencode, "run_jsonl", fake_run)
    session = opencode.OpenCodeBackend(("fake-opencode",)).open_session(
        SessionSpec("fixer", repo, system_prompt="rules")
    )
    reply = session.ask(TurnRequest("work"))

    assert commands[0][-1] == "rules\n\nwork"
    assert reply.text == "hello"
    assert reply.session_id == "ses_9"
    assert reply.cost_usd == 0.125
    assert reply.context_tokens == 26
    assert reply.usage is not None and reply.usage.output_tokens == 5
    assert not reply.is_error


def test_opencode_resumes_an_initial_session_without_repeating_the_system_prompt(
    repo: Path, monkeypatch: Any
) -> None:
    commands: list[list[str]] = []
    events = ({"type": "text", "sessionID": "ses_old", "part": {"text": "continued"}},)

    def fake_run(command: list[str], **kwargs: Any) -> JsonlProcessResult:
        del kwargs
        commands.append(command)
        return JsonlProcessResult(events, "", 0, False, 0.1)

    monkeypatch.setattr(opencode, "run_jsonl", fake_run)
    session = opencode.OpenCodeBackend(("fake-opencode",)).open_session(
        SessionSpec(
            "fixer",
            repo,
            system_prompt="standing rules",
            resume_session_id="ses_old",
        )
    )

    reply = session.ask(TurnRequest("continue"))

    assert commands[0][commands[0].index("--session") + 1] == "ses_old"
    assert commands[0][-1] == "continue"
    assert "standing rules" not in commands[0]
    assert reply.session_id == "ses_old"


def test_opencode_does_not_mutate_the_process_environment(repo: Path) -> None:
    session = opencode.OpenCodeBackend(("fake-opencode",)).open_session(SessionSpec("fixer", repo))
    before = dict(os.environ)
    child = session.environment()
    assert child is not os.environ
    assert dict(os.environ) == before


def test_opencode_rotates_a_session_at_the_context_limit(repo: Path, monkeypatch: Any) -> None:
    events = (
        {"type": "text", "sessionID": "ses_9", "part": {"text": "done"}},
        {"type": "step_finish", "part": {"tokens": {"input": 20, "output": 5}}},
    )
    monkeypatch.setattr(
        opencode,
        "run_jsonl",
        lambda *args, **kwargs: JsonlProcessResult(events, "", 0, False, 0.1),
    )
    session = opencode.OpenCodeBackend(("fake-opencode",)).open_session(
        SessionSpec("fixer", repo, context_limit=20)
    )
    reply = session.ask(TurnRequest("work"))
    assert reply.session_id is None
    assert session.session_id is None
