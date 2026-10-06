from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

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
    assert command[:3] == ["fake-codex", "--config", 'model_reasoning_effort="high"']
    assert "--config" not in command[command.index("exec") :]
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


TRANSPORT = {
    "type": "error",
    "message": (
        "Reconnecting... 1/5: stream disconnected before completion: "
        "stream closed before response.completed"
    ),
}
MESSAGE = {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
COMPLETED = {"type": "turn.completed"}


@pytest.mark.parametrize(
    ("events", "code", "error", "kind"),
    [
        ([TRANSPORT, MESSAGE, COMPLETED], 0, False, "none"),
        ([MESSAGE, COMPLETED, TRANSPORT], 0, True, "transport"),
        ([MESSAGE, TRANSPORT], 1, True, "transport"),
        (
            [MESSAGE, {"type": "turn.failed", "error": {"message": TRANSPORT["message"]}}],
            1,
            True,
            "transport",
        ),
        (
            [{"type": "error", "message": "bad credentials"}, MESSAGE, COMPLETED],
            0,
            True,
            "terminal",
        ),
        (
            [MESSAGE, COMPLETED, {"type": "turn.failed", "error": {"message": "denied"}}],
            1,
            True,
            "terminal",
        ),
        ([MESSAGE, COMPLETED], 1, True, "exit"),
        (
            [MESSAGE, {"type": "event_msg", "payload": {"type": "task_complete"}}],
            0,
            True,
            "incomplete",
        ),
        ([COMPLETED], 0, True, "incomplete"),
        ([MESSAGE, COMPLETED, {"type": "turn.started"}], 0, True, "incomplete"),
        (
            [
                {
                    "type": "turn.failed",
                    "error": {
                        "message": (
                            "stream disconnected before completion: "
                            "Incomplete response returned, reason: content_filter"
                        )
                    },
                }
            ],
            1,
            True,
            "terminal",
        ),
    ],
)
def test_codex_transport_completion_order(
    repo: Path, events: list[dict[str, Any]], code: int, error: bool, kind: str
) -> None:
    session = codex.CodexBackend().open_session(SessionSpec("fixer", repo))
    reply = session._reply(result(*events, returncode=code), expects_structured=False)
    assert reply.is_error is error
    assert reply.diagnostics[0].error_type == kind
    assert reply.diagnostics[0].exit_code == code


@pytest.mark.parametrize(
    ("timed_out", "cancelled", "kind"), [(True, False, "timeout"), (False, True, "cancelled")]
)
def test_codex_timeout_and_cancel_never_recover(
    repo: Path, timed_out: bool, cancelled: bool, kind: str
) -> None:
    session = codex.CodexBackend().open_session(SessionSpec("fixer", repo))
    raw = JsonlProcessResult((TRANSPORT,), "", -9, timed_out, 2, cancelled)
    reply = session._reply(raw, expects_structured=False)
    assert reply.is_error
    assert reply.diagnostics[0].error_type == kind


def test_codex_diagnostics_never_contain_error_payloads(repo: Path) -> None:
    from dataclasses import asdict

    from mergerail.backends.base import TurnDiagnostics

    secret = "Bearer secret-token"
    session = codex.CodexBackend().open_session(SessionSpec("fixer", repo))
    reply = session._reply(
        result(
            {"type": "error", "message": TRANSPORT["message"] + secret},
            {"type": secret},
            stderr=secret,
            returncode=1,
        ),
        expects_structured=False,
    )
    document = asdict(reply.diagnostics[0])
    assert secret not in json.dumps(document)
    assert TurnDiagnostics.from_dict(document) == reply.diagnostics[0]
    document["close_reason"] = secret
    assert TurnDiagnostics.from_dict(document) is None
