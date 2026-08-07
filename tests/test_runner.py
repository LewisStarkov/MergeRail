"""The loop itself, with the agents replaced by scripts.

Every test here runs against a real git repository and a real worktree; only
the two ``claude`` subprocesses are faked. What is being tested is the wiring:
who gets asked what, in which order, and what lands where.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentq.backends import (
    AgentReply,
    BackendCapabilities,
    BackendInfo,
    BackendRegistry,
    EventSink,
    SessionSpec,
    TurnRequest,
)
from agentq.config import Config
from agentq.detect import Check
from agentq.fronts.base import Front, StreamEvent
from agentq.runner import Runner
from agentq.tasks import Status, Task, TaskStore

APPROVE = "VERDICT: APPROVE"
REJECT = "VERDICT: REJECT"

Turn = Callable[[str], AgentReply]


def reply(
    text: str,
    *,
    cost: float | None = 0.0,
    error: bool = False,
    structured: dict[str, Any] | None = None,
) -> AgentReply:
    return AgentReply(
        text=text,
        is_error=error,
        cost_usd=cost,
        context_tokens=0,
        seconds=0.0,
        structured=structured,
    )


def says(text: str, **kwargs: Any) -> Turn:
    return lambda prompt: reply(text, **kwargs)


class FakeAgent:
    """Stands in for ClaudeAgent: a queue of scripted turns, in order."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.turns: list[Turn] = []
        self.prompts: list[str] = []
        self.events: EventSink | None = None
        self.cancelled = threading.Event()

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(structured_output=True)

    def ask(self, request: TurnRequest) -> AgentReply:
        self.prompts.append(request.prompt)
        assert self.turns, f"the {self.role} was asked more often than scripted"
        if self.events is not None:
            self.events.emit({"type": "text_delta", "text": f"{self.role} is working"})
        return self.turns.pop(0)(request.prompt)

    def close(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancelled.set()


class FakeBackend:
    name = "fake"

    def __init__(self, agents: dict[str, FakeAgent]) -> None:
        self.agents = agents

    def probe(self) -> BackendInfo:
        return BackendInfo(
            self.name,
            True,
            capabilities=BackendCapabilities(structured_output=True),
        )

    def open_session(self, spec: SessionSpec, events: EventSink | None = None) -> FakeAgent:
        assert spec.cwd.exists()
        agent = self.agents[spec.role]
        agent.events = events
        return agent


class RecordingFront(Front):
    def __init__(self, store: TaskStore) -> None:
        super().__init__(store)
        self.streamed: list[tuple[int, StreamEvent]] = []

    def stream(self, task: Task, event: StreamEvent) -> None:
        self.streamed.append((task.id, event))


class RecordingShare:
    def __init__(self) -> None:
        self.events: list[str] = []

    def start(self, front: Front) -> str:
        self.events.append(f"start:{front.name}")
        return "https://demo.ngrok.app"

    def stop(self) -> None:
        self.events.append("stop")


@pytest.fixture
def rig(repo: Path) -> tuple[Runner, FakeAgent, FakeAgent]:
    fakes = {"fixer": FakeAgent("fixer"), "reviewer": FakeAgent("reviewer")}
    config = Config.load(repo)
    config.fixer.backend = "fake"
    config.reviewer.backend = "fake"
    config.checks = []
    config.baseline_checks = False
    runner = Runner(
        config,
        RecordingFront(TaskStore(config.queue_path)),
        supervise=False,
        backends=BackendRegistry([FakeBackend(fakes)]),
    )
    return runner, fakes["fixer"], fakes["reviewer"]


def committing(
    runner: Runner, filename: str, text: str = "committed it", cost: float | None = 0.0
) -> Turn:
    """A fixer turn that actually commits a file, like the real one would."""

    def turn(prompt: str) -> AgentReply:
        (runner.worktree.path / filename).write_text("made", encoding="utf-8")
        runner.worktree.commit_all(f"add {filename}")
        return reply(text, cost=cost)

    return turn


def worked(runner: Runner, text: str) -> Task:
    task = runner.store.add(text)
    claimed = runner.store.take_next()
    assert claimed is not None
    runner.handle(claimed)
    final = runner.store.get(task.id)
    assert final is not None
    return final


# --- the happy path ------------------------------------------------------


def test_fixed_reviewed_and_merged(
    repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, reviewer = rig
    fixer.turns = [committing(runner, "one.txt")]
    reviewer.turns = [says(APPROVE)]

    final = worked(runner, "create one.txt")
    assert final.status == Status.DONE
    assert (repo / "one.txt").exists()  # merged into the base checkout
    assert "committed it" in final.note
    assert isinstance(runner.front, RecordingFront)
    assert any(
        event.role == "fixer" and event.kind == "text" for _, event in runner.front.streamed
    )
    assert any(
        event.role == "reviewer" and event.kind == "result"
        for _, event in runner.front.streamed
    )
    assert {event["event"] for event in runner.audit.read(task=final.id)} >= {
        "task.started",
        "agent.turn",
        "review.completed",
        "task.finished",
    }


def test_strict_security_rejects_a_backend_without_native_guarantees(repo: Path) -> None:
    fakes = {"fixer": FakeAgent("fixer"), "reviewer": FakeAgent("reviewer")}
    config = Config.load(repo)
    config.fixer.backend = "fake"
    config.reviewer.backend = "fake"
    config.strict_security = True
    with pytest.raises(SystemExit, match="strict security refuses fixer=fake"):
        Runner(
            config,
            RecordingFront(TaskStore(config.queue_path)),
            backends=BackendRegistry([FakeBackend(fakes)]),
        )


def test_a_rejection_goes_back_to_the_fixer_in_the_same_session(
    repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, reviewer = rig
    fixer.turns = [committing(runner, "one.txt"), committing(runner, "two.txt", "fixed it")]
    reviewer.turns = [says(f"the test is loosened\n{REJECT}"), says(APPROVE)]

    final = worked(runner, "do the thing")
    assert final.status == Status.DONE
    # The objection became the fixer's next instruction, verbatim.
    assert "the test is loosened" in fixer.prompts[1]


def test_a_structured_verdict_outranks_the_text(
    repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, reviewer = rig
    fixer.turns = [committing(runner, "one.txt"), committing(runner, "two.txt")]
    reviewer.turns = [
        says("prose without a verdict", structured={"verdict": "REJECT", "objection": "narrow it"}),
        says("", structured={"verdict": "APPROVE"}),
    ]

    final = worked(runner, "do the thing")
    assert final.status == Status.DONE
    assert "narrow it" in fixer.prompts[1]


# --- the checks gate -----------------------------------------------------


def test_compare_baseline_runs_known_failures_without_blocking_tasks(
    rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, _fixer, _reviewer = rig
    runner.config.baseline_checks = True
    runner.config.baseline_mode = "compare"
    runner.config.checks = [
        Check("known", [sys.executable, "-c", "raise SystemExit(1)"]),
        Check("healthy", [sys.executable, "-c", "raise SystemExit(0)"]),
    ]

    active = runner._baseline()
    assert [check.name for check in active] == ["known", "healthy"]
    assert runner.allowed_check_failures == frozenset({"known"})


def test_strict_baseline_refuses_to_start_with_a_known_failure(
    rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, _fixer, _reviewer = rig
    runner.config.baseline_checks = True
    runner.config.baseline_mode = "strict"
    runner.config.checks = [Check("broken", [sys.executable, "-c", "raise SystemExit(1)"])]
    with pytest.raises(SystemExit, match="baseline checks fail in strict mode: broken"):
        runner._baseline()


def test_failing_checks_return_to_the_fixer_without_a_review(
    repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, reviewer = rig
    probe = "import pathlib, sys; sys.exit(0 if pathlib.Path('ok').exists() else 1)"
    runner.active_checks = [Check("gate", [sys.executable, "-c", probe])]
    fixer.turns = [committing(runner, "one.txt"), committing(runner, "ok")]
    reviewer.turns = [says(APPROVE)]

    final = worked(runner, "do the thing")
    assert final.status == Status.DONE
    assert "checks do not pass" in fixer.prompts[1]
    assert len(reviewer.prompts) == 1  # the failing round never reached review


def test_committing_nothing_is_an_objection(
    repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, reviewer = rig
    runner.config.max_rounds = 1
    fixer.turns = [says("I looked around")]

    final = worked(runner, "do the thing")
    assert final.status == Status.FAILED
    assert "committed nothing" in final.note
    assert reviewer.prompts == []


# --- ways out ------------------------------------------------------------


def test_an_answer_is_a_result_not_a_failure(
    repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, reviewer = rig
    fixer.turns = [says("ANSWER: the bot is silent because the token expired")]

    final = worked(runner, "why is the bot silent?")
    assert final.status == Status.DONE
    assert final.note == "the bot is silent because the token expired"
    assert reviewer.prompts == []  # nothing to review


def test_an_agent_error_fails_fast_instead_of_burning_rounds(
    repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, _reviewer = rig
    fixer.turns = [says("billing hard limit reached", error=True)]

    final = worked(runner, "do the thing")
    assert final.status == Status.FAILED
    assert "billing hard limit" in final.note
    assert len(fixer.prompts) == 1  # not retried


def test_the_budget_is_a_ceiling_for_the_whole_task(
    repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, reviewer = rig
    runner.config.max_usd = 0.05
    fixer.turns = [committing(runner, "one.txt", cost=0.04)]
    reviewer.turns = [says(REJECT, cost=0.03)]

    final = worked(runner, "do the thing")
    assert final.status == Status.BLOCKED
    assert "budget" in final.note
    assert final.cost_usd == pytest.approx(0.07)
    assert len(fixer.prompts) == 1  # round two never started


def test_a_budget_blocks_if_runtime_cost_is_missing(
    rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, reviewer = rig
    runner.config.max_usd = 1.0
    fixer.turns = [committing(runner, "one.txt", cost=None)]

    final = worked(runner, "do the thing")
    assert final.status == Status.BLOCKED
    assert "did not report exact USD cost" in final.note
    assert reviewer.prompts == []


def test_an_unmergeable_landing_is_blocked_with_the_branch_intact(
    repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, reviewer = rig
    fixer.turns = [committing(runner, "one.txt")]
    reviewer.turns = [says(APPROVE)]
    # Somebody is mid-thought in the base checkout; no remote for the fallback.
    (repo / "README.md").write_text("half-written\n", encoding="utf-8")

    final = worked(runner, "do the thing")
    assert final.status == Status.BLOCKED
    assert final.branch == f"agentq/{final.id}/a1"


# --- run(until=...) ------------------------------------------------------


def test_share_follows_the_runner_lifecycle(rig: tuple[Runner, FakeAgent, FakeAgent]) -> None:
    runner, _fixer, _reviewer = rig
    share = RecordingShare()
    runner.share = share

    runner.run(once=True)

    assert share.events == ["start:front", "stop"]


def test_run_until_stops_after_the_named_task(
    repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, _reviewer = rig
    fixer.turns = [says("ANSWER: yes")]
    first = runner.store.add("a question")
    runner.store.add("a second task nobody scripted")

    runner.run(until=first.id)
    done = runner.store.get(first.id)
    waiting = runner.store.get(first.id + 1)
    assert done is not None and done.status == Status.DONE
    assert waiting is not None and waiting.status == Status.NEW


def test_run_recovers_review_work_on_a_new_attempt(
    rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, _reviewer = rig
    task = runner.store.add("answer this")
    runner.store.update(
        task.id,
        status=Status.REVIEW,
        branch="agentq/1/a1",
        claimed_by="dead-runner",
    )
    fixer.turns = [says("ANSWER: recovered")]

    runner.run(once=True)

    recovered = runner.store.get(task.id)
    assert recovered is not None
    assert recovered.status == Status.DONE
    assert recovered.attempts == 1
    assert recovered.previous_branches == ["agentq/1/a1"]
    assert recovered.claimed_by == ""
    assert any(event["event"] == "task.recovered" for event in runner.audit.read(task=task.id))


def test_active_turn_can_be_cancelled_and_preserves_the_branch(
    rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, fixer, _reviewer = rig
    started = threading.Event()

    def wait_for_cancel(prompt: str) -> AgentReply:
        del prompt
        (runner.worktree.path / "partial.txt").write_text("partial", encoding="utf-8")
        started.set()
        assert fixer.cancelled.wait(timeout=5)
        return reply("cancelled", error=True)

    fixer.turns = [wait_for_cancel]
    task = runner.store.add("long task")
    thread = threading.Thread(target=runner.run, kwargs={"once": True})
    thread.start()
    assert started.wait(timeout=5)
    assert runner.store.request_cancel(task.id) is not None
    thread.join(timeout=10)
    assert not thread.is_alive()

    cancelled = runner.store.get(task.id)
    assert cancelled is not None
    assert cancelled.status == Status.CANCELLED
    assert cancelled.branch == "agentq/1/a1"
    assert (runner.worktree.path / "partial.txt").exists()
    assert any(event["event"] == "task.cancelled" for event in runner.audit.read(task=task.id))
