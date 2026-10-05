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

from mergerail.backends import (
    AgentReply,
    BackendCapabilities,
    BackendInfo,
    BackendRegistry,
    EventSink,
    SessionSpec,
    TurnRequest,
)
from mergerail.config import Config
from mergerail.detect import Check
from mergerail.fronts.base import Front, StreamEvent
from mergerail.lease import RunnerLease
from mergerail.runner import Runner
from mergerail.tasks import Status, Task, TaskStore
from mergerail.update import ReleaseCandidate

APPROVE = "VERDICT: APPROVE"
REJECT = "VERDICT: REJECT"
RUNTIME_CANDIDATE = ReleaseCandidate(
    "v0.2.0", "1" * 40, "https://example.test/mergerail.git"
)

Turn = Callable[[str], AgentReply]


def reply(
    text: str,
    *,
    cost: float | None = 0.0,
    error: bool = False,
    structured: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> AgentReply:
    return AgentReply(
        text=text,
        is_error=error,
        cost_usd=cost,
        context_tokens=0,
        seconds=0.0,
        structured=structured,
        session_id=session_id,
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
        self.native_resume = False

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            conversations=True,
            native_resume=self.native_resume,
            structured_output=True,
        )

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
        self.specs: list[SessionSpec] = []

    def probe(self) -> BackendInfo:
        return BackendInfo(
            self.name,
            True,
            capabilities=BackendCapabilities(structured_output=True),
        )

    def open_session(self, spec: SessionSpec, events: EventSink | None = None) -> FakeAgent:
        assert spec.cwd.exists()
        self.specs.append(spec)
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


def test_fixed_reviewed_and_merged(repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]) -> None:
    runner, fixer, reviewer = rig
    fixer.turns = [committing(runner, "one.txt")]
    reviewer.turns = [says(APPROVE)]

    final = worked(runner, "create one.txt")
    assert final.status == Status.DONE
    assert (repo / "one.txt").exists()  # merged into the base checkout
    assert "committed it" in final.note
    assert isinstance(runner.front, RecordingFront)
    assert any(event.role == "fixer" and event.kind == "text" for _, event in runner.front.streamed)
    assert any(
        event.role == "reviewer" and event.kind == "result" for _, event in runner.front.streamed
    )
    assert {event["event"] for event in runner.audit.read(task=final.id)} >= {
        "task.started",
        "agent.turn",
        "review.completed",
        "task.finished",
    }


def test_pending_task_messages_reach_the_fixer_and_are_answered(
    rig: tuple[Runner, FakeAgent, FakeAgent],
) -> None:
    runner, fixer, _reviewer = rig
    task = runner.store.add("why is it broken?")
    message = runner.store.append_message(task.id, "also check the timeout")
    assert message is not None
    fixer.turns = [says("ANSWER: the timeout is too short", session_id="thread-1")]

    claimed = runner.store.take_next()
    assert claimed is not None
    runner.handle(claimed)

    final = runner.store.get(task.id)
    thread = runner.store.messages(task.id)
    assert final is not None and final.status == Status.DONE
    assert "also check the timeout" in fixer.prompts[0]
    assert thread[0].status == "answered"
    assert thread[-1].role == "assistant"
    assert final.sessions["fixer"].session_id == "thread-1"


def test_follow_up_reuses_the_task_session_and_durable_thread(
    rig: tuple[Runner, FakeAgent, FakeAgent],
) -> None:
    runner, fixer, _reviewer = rig
    fixer.turns = [says("ANSWER: first answer", session_id="thread-1")]
    first = worked(runner, "explain it")
    runner._close_agent_sessions()
    message = runner.store.append_message(first.id, "and what about retries?")
    assert message is not None
    assert runner.store.retry_task(first.id) is not None
    fixer.turns = [says("ANSWER: retries are bounded", session_id="thread-1")]

    claimed = runner.store.take_next()
    assert claimed is not None
    runner.handle(claimed)

    final = runner.store.get(first.id)
    backend = runner.backends.get("fake")
    assert isinstance(backend, FakeBackend)
    assert final is not None and len(final.runs) == 1
    assert "first answer" in fixer.prompts[-1]
    assert "what about retries" in fixer.prompts[-1]
    fixer_specs = [spec for spec in backend.specs if spec.role == "fixer"]
    assert fixer_specs[-1].resume_session_id == "thread-1"


def test_sessions_are_never_reused_across_tasks(rig: tuple[Runner, FakeAgent, FakeAgent]) -> None:
    runner, fixer, _reviewer = rig
    fixer.turns = [
        says("ANSWER: first", session_id="thread-1"),
        says("ANSWER: second", session_id="thread-2"),
    ]

    worked(runner, "first question")
    worked(runner, "second question")

    backend = runner.backends.get("fake")
    assert isinstance(backend, FakeBackend)
    fixer_specs = [spec for spec in backend.specs if spec.role == "fixer"]
    assert len(fixer_specs) == 2
    assert fixer_specs[0].resume_session_id is None
    assert fixer_specs[1].resume_session_id is None


def test_invalid_resumed_session_falls_back_to_the_durable_context(
    rig: tuple[Runner, FakeAgent, FakeAgent],
) -> None:
    runner, fixer, _reviewer = rig
    task = runner.store.add("explain it")
    runner.store.save_session(task.id, "fixer", backend="fake", session_id="expired")
    runner.store.append_message(task.id, "include the edge case")
    fixer.native_resume = True
    fixer.turns = [
        says("session not found", error=True),
        says("ANSWER: recovered", session_id="fresh"),
    ]

    claimed = runner.store.take_next()
    assert claimed is not None
    runner.handle(claimed)

    final = runner.store.get(task.id)
    assert final is not None and final.status == Status.DONE
    assert len(fixer.prompts) == 2
    assert "include the edge case" in fixer.prompts[1]
    assert final.sessions["fixer"].session_id == "fresh"
    assert any(
        event["event"] == "task.session_fallback" for event in runner.audit.read(task=task.id)
    )


def test_message_during_review_invalidates_the_verdict(
    rig: tuple[Runner, FakeAgent, FakeAgent],
) -> None:
    runner, fixer, reviewer = rig
    fixer.turns = [
        committing(runner, "one.txt"),
        committing(runner, "two.txt", "included the follow-up"),
    ]

    def approve_after_message(prompt: str) -> AgentReply:
        del prompt
        active = runner._active_task
        assert active is not None
        runner.store.append_message(active.id, "also create two.txt")
        return reply(APPROVE)

    reviewer.turns = [approve_after_message, says(APPROVE)]

    final = worked(runner, "create one.txt")

    assert final.status == Status.DONE
    assert "also create two.txt" in fixer.prompts[1]
    assert (runner.config.root / "two.txt").exists()
    assert any(event["event"] == "review.invalidated" for event in runner.audit.read(task=final.id))


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


def test_setup_persists_context_and_switches_roles_without_restart(
    repo: Path, rig: tuple[Runner, FakeAgent, FakeAgent]
) -> None:
    runner, _, _ = rig
    stages: list[str] = []

    status, response = runner.apply_setup(
        {
            "agent": "fake",
            "environment": "production",
            "work_mode": "maintenance",
            "summary": "prepare the release",
            "external_actions": "ask",
            "constraints": ["preserve the API"],
        },
        lambda stage, message: stages.append(stage),
    )

    assert status == 200 and response["ok"] is True
    assert stages == ["validating", "saving", "activating", "complete"]
    assert runner.fixer_backend == runner.reviewer_backend == "fake"
    assert runner.config.project.environment == "production"
    assert set(runner.setup_snapshot()["values"]) == {
        "agent",
        "environment",
        "summary",
        "external_actions",
    }
    saved = (repo / "mergerail.toml").read_text(encoding="utf-8")
    assert 'summary = "prepare the release"' in saved
    assert 'constraints = ["preserve the API"]' in saved


def test_setup_refuses_to_reconfigure_an_active_task(
    rig: tuple[Runner, FakeAgent, FakeAgent],
) -> None:
    runner, _, _ = rig
    runner._active_task = Task(id=1, text="busy")
    status, response = runner.apply_setup({}, lambda stage, message: None)
    assert status == 409
    assert "active task" in response["error"]


def test_interactive_front_can_recover_an_invalid_backend(repo: Path) -> None:
    fakes = {"fixer": FakeAgent("fixer"), "reviewer": FakeAgent("reviewer")}
    config = Config.load(repo)
    config.fixer.backend = "missing"
    config.reviewer.backend = "missing"
    runner = Runner(
        config,
        RecordingFront(TaskStore(config.queue_path)),
        supervise=False,
        backends=BackendRegistry([FakeBackend(fakes)]),
        allow_setup=True,
    )
    assert runner.fixer_backend == runner.reviewer_backend == ""

    status, _ = runner.apply_setup({"agent": "fake"}, lambda stage, message: None)
    assert status == 200
    assert runner.fixer_backend == runner.reviewer_backend == "fake"


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
    rig: tuple[Runner, FakeAgent, FakeAgent],
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
    rig: tuple[Runner, FakeAgent, FakeAgent],
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
    rig: tuple[Runner, FakeAgent, FakeAgent],
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
    assert final.branch == f"mergerail/{final.id}/a1"


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


def test_runtime_update_waits_for_a_task_then_claims_no_more_work(
    rig: tuple[Runner, FakeAgent, FakeAgent],
) -> None:
    runner, fixer, _reviewer = rig
    fixer.turns = [says("ANSWER: finished safely")]
    first = runner.store.add("finish this first")
    second = runner.store.add("leave this queued")
    probes = iter([None, RUNTIME_CANDIDATE])
    runner.update_probe = lambda: next(probes)

    assert runner.run() == RUNTIME_CANDIDATE

    finished = runner.store.get(first.id)
    waiting = runner.store.get(second.id)
    assert finished is not None and finished.status == Status.DONE
    assert waiting is not None and waiting.status == Status.NEW
    assert waiting.claimed_by == ""


def test_runtime_update_follows_pending_delivery_handling(
    rig: tuple[Runner, FakeAgent, FakeAgent], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _fixer, _reviewer = rig
    pending = Task(id=7, text="deliver me")
    deliveries = iter([[pending], []])
    events: list[str] = []
    monkeypatch.setattr(runner.store, "delivery_tasks", lambda: next(deliveries))
    monkeypatch.setattr(runner, "land", lambda *args: events.append("delivery"))

    def probe() -> ReleaseCandidate:
        events.append("probe")
        return RUNTIME_CANDIDATE

    runner.update_probe = probe
    monkeypatch.setattr(
        runner.front,
        "next_task",
        lambda: pytest.fail("a task was claimed after finding an update"),
    )

    assert runner._loop(once=False, until=None) == RUNTIME_CANDIDATE
    assert events == ["delivery", "probe"]


def test_runtime_candidate_returns_only_after_cleanup_and_lease_release(
    rig: tuple[Runner, FakeAgent, FakeAgent], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _fixer, _reviewer = rig
    events: list[str] = []

    def probe() -> ReleaseCandidate:
        events.append("probe")
        return RUNTIME_CANDIDATE

    runner.update_probe = probe
    monkeypatch.setattr(runner.front, "start", lambda: events.append("front.start"))
    monkeypatch.setattr(runner.front, "stop", lambda: events.append("front.stop"))
    monkeypatch.setattr(runner.supervisor, "start", lambda: events.append("process.start"))
    monkeypatch.setattr(runner.supervisor, "stop", lambda: events.append("process.stop"))

    assert runner.run() == RUNTIME_CANDIDATE

    assert events == ["front.start", "process.start", "probe", "front.stop", "process.stop"]
    replacement = RunnerLease(runner.lease.path)
    replacement.acquire()
    replacement.release()


def test_run_recovers_review_work_on_a_new_attempt(
    rig: tuple[Runner, FakeAgent, FakeAgent],
) -> None:
    runner, fixer, _reviewer = rig
    task = runner.store.add("answer this")
    runner.store.update(
        task.id,
        status=Status.REVIEW,
        branch="mergerail/1/a1",
        claimed_by="dead-runner",
    )
    fixer.turns = [says("ANSWER: recovered")]

    runner.run(once=True)

    recovered = runner.store.get(task.id)
    assert recovered is not None
    assert recovered.status == Status.DONE
    assert recovered.attempts == 1
    assert recovered.previous_branches == ["mergerail/1/a1"]
    assert recovered.claimed_by == ""
    assert any(event["event"] == "task.recovered" for event in runner.audit.read(task=task.id))


def test_active_turn_can_be_cancelled_and_preserves_the_branch(
    rig: tuple[Runner, FakeAgent, FakeAgent],
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
    assert cancelled.branch == "mergerail/1/a1"
    assert (runner.worktree.path / "partial.txt").exists()
    assert any(event["event"] == "task.cancelled" for event in runner.audit.read(task=task.id))


def test_docker_delivery_cannot_fall_back_to_local_checks(
    rig: tuple[Runner, FakeAgent, FakeAgent], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, _ = rig
    task = runner.store.add("saved Docker approval")
    runner.store.update(task.id, execution={"backend": "docker", "policy_digest": "saved"})
    runner.store.approve(
        task.id, branch="candidate", commit="a" * 40, base_branch="main",
        requested_mode="local", summary="fixed", review="approved",
    )
    events: list[str] = []

    def baseline() -> list[Check]:
        events.append("host baseline")
        return []

    monkeypatch.setattr(runner, "_baseline", baseline)
    with pytest.raises(SystemExit, match="original execution policy"):
        runner.run(once=True)
    assert events == []
    assert runner.store.get(task.id) is not None


def test_cancelled_backend_exception_keeps_cancelled_status(
    rig: tuple[Runner, FakeAgent, FakeAgent],
) -> None:
    runner, fixer, _ = rig
    task = runner.store.add("cancel a running turn")

    def cancel_then_raise(prompt: str) -> AgentReply:
        runner.store.request_cancel(task.id)
        raise RuntimeError("stage was killed")

    fixer.turns = [cancel_then_raise]
    claimed = runner.store.take_next()
    assert claimed is not None
    runner.handle(claimed)
    result = runner.store.get(task.id)
    assert result is not None and result.status == Status.CANCELLED
