from __future__ import annotations

from pathlib import Path

from agentq.agent import AgentOptions, ClaudeAgent, context_size, parse_event, tool_hint
from agentq.prompts import fix, review, verdict_of
from agentq.tasks import Task


def agent(**options: object) -> ClaudeAgent:
    return ClaudeAgent("fixer", Path("/tmp"), AgentOptions(**options))  # type: ignore[arg-type]


def test_context_counts_fresh_written_and_cached_tokens() -> None:
    usage = {
        "input_tokens": 100,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 5,
        "output_tokens": 999,
    }
    assert context_size(usage) == 125


def test_context_ignores_nonsense() -> None:
    assert context_size({"input_tokens": "many"}) == 0


def test_a_full_session_is_dropped_rather_than_resumed() -> None:
    one = agent(context_limit=1000)
    one.session_id = "abc"
    one._absorb({"session_id": "abc", "result": "done"}, {"input_tokens": 1500}, 1.0)
    assert one.session_id is None
    assert one.context_tokens == 1500


def test_a_session_with_room_is_kept() -> None:
    one = agent(context_limit=1000)
    one._absorb({"session_id": "abc", "result": "done"}, {"input_tokens": 100}, 1.0)
    assert one.session_id == "abc"


def test_a_missing_usage_block_does_not_shrink_the_measurement() -> None:
    one = agent(context_limit=1000)
    one.context_tokens = 800
    one._absorb({"session_id": "abc"}, {}, 1.0)
    assert one.context_tokens == 800


def test_the_reviewer_cannot_edit() -> None:
    reviewer = ClaudeAgent("reviewer", Path("/tmp"), AgentOptions(), read_only=True)
    command = reviewer.command("hi")
    assert "--disallowed-tools" in command
    assert "--dangerously-skip-permissions" not in command


def test_skip_permissions_is_opt_in() -> None:
    assert "--dangerously-skip-permissions" in agent(permission="skip").command("hi")
    assert "--permission-mode" in agent(permission="acceptEdits").command("hi")


def test_resume_is_passed_once_there_is_a_session() -> None:
    one = agent()
    assert "--resume" not in one.command("hi")
    one.session_id = "abc"
    assert one.command("hi")[-2:] == ["--resume", "abc"]


def test_parse_event_survives_junk_lines() -> None:
    assert parse_event("") is None
    assert parse_event("not json") is None
    assert parse_event('{"type": "result"}') == {"type": "result"}


def test_tool_hint_picks_the_telling_field() -> None:
    assert tool_hint({"input": {"file_path": "a/b.py"}}) == "a/b.py"
    assert tool_hint({"input": {}}) == ""


def test_the_last_verdict_wins() -> None:
    # The instructions quote both words, and reasoning aloud must not decide it.
    text = "I could say VERDICT: APPROVE here but no.\nVERDICT: REJECT"
    assert verdict_of(text) is False
    assert verdict_of("VERDICT: approve") is True
    assert verdict_of("I liked it") is None


def test_prompts_carry_the_repository_rules_by_reference() -> None:
    task = Task(id=1, text="fix it")
    body = fix(task, "agentq/1", "main", ["CLAUDE.md"], "")
    assert "CLAUDE.md" in body
    assert "agentq/1" in body
    assert "Do not push" in body

    verdict = review(task, "agentq/1", "main", [], "ruff: PASS")
    assert "VERDICT: APPROVE" in verdict
    assert "ruff: PASS" in verdict
