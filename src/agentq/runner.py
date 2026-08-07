"""The loop: claim a task, fix it, check it, review it, land it.

One worktree, two long-lived agents, tasks worked one at a time. The order of
the steps is the whole design:

1. the shared worktree is reset onto a fresh branch cut from the base — the
   agents never edit the checkout the running app imports from;
2. a **fixer** agent makes the change and commits it — or answers in words,
   when the task turns out to be a question;
3. the runner itself runs the project's checks;
4. a **reviewer** agent, in the same worktree but forbidden from editing
   anything, reads the diff *and the check results*, and approves or rejects;
5. a rejection — or a failing check, which never reaches the reviewer — goes
   back to the fixer *in the same session*, up to ``max_rounds`` times;
6. an approval lands: merged into the base branch, or opened as a pull request.

Nothing here is retried harder than that. A task that cannot be made to pass in
three rounds is a task that needed a person in round one.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

from . import checks, delivery, log, prompts
from .audit import AuditLog
from .backends import (
    AgentReply,
    AgentSession,
    BackendInfo,
    BackendRegistry,
    EventSink,
    TurnRequest,
)
from .backends.policy import strict_security_gaps
from .config import Config
from .detect import Check
from .fronts.base import Front, StreamEvent
from .gitctl import Worktree, attempt_branch, git, is_repo, repo_root
from .lease import RunnerBusy, RunnerLease
from .share import Share
from .streaming import normalize_agent_event
from .supervisor import Supervisor
from .tasks import Status, Task, TaskStore


class _RunnerEventSink(EventSink):
    def __init__(self, runner: Runner, backend: str, role: str) -> None:
        self.runner = runner
        self.backend = backend
        self.role = role

    def emit(self, event: Mapping[str, Any]) -> None:
        self.runner._agent_event(self.backend, self.role, event)


class Runner:
    """Everything wired together. ``Runner().run()`` is a working installation."""

    def __init__(
        self,
        config: Config | None = None,
        front: Front | str | None = None,
        *,
        supervise: bool = True,
        backends: BackendRegistry | None = None,
        share: Share | None = None,
    ) -> None:
        if config is None:
            start = Path.cwd()
            if not is_repo(start):
                raise SystemExit(f"agentq: {start} is not inside a git repository")
            config = Config.load(repo_root(start))
        self.config = config
        self.store = TaskStore(self.config.queue_path)
        self.front = self._resolve_front(front)
        self.supervisor = Supervisor(
            self.config.process if supervise else [], self.config.root, self.config.process_log
        )
        self.share = share
        self.worktree = Worktree(self.config.root, self.config.worktree)
        self.backends = backends or self.config.backend_registry()
        self.fixer_backend = self._resolve_backend(self.config.fixer.backend)
        self.reviewer_backend = self._resolve_backend(self.config.reviewer.backend)
        self._validate_security()
        self.fixer: AgentSession | None = None
        self.reviewer: AgentSession | None = None
        self._active_task: Task | None = None
        #: What actually runs per round; ``run`` prunes it against the base.
        self.active_checks: list[Check] = list(self.config.checks)
        self.allowed_check_failures: frozenset[str] = frozenset()
        self.audit = AuditLog(self.config.audit_path)
        self.lease = RunnerLease(self.config.state_dir / "runner.lock")
        self.stopping = False

    def _validate_security(self) -> None:
        if not self.config.strict_security:
            return
        for role, backend in (
            ("fixer", self.fixer_backend),
            ("reviewer", self.reviewer_backend),
        ):
            info = self.backends.probe(backend)
            assert isinstance(info, BackendInfo)
            gaps = strict_security_gaps(role, info.capabilities)
            if gaps:
                raise SystemExit(
                    f"agentq: strict security refuses {role}={backend}: missing "
                    + ", ".join(gaps)
                )

    def _make_agent(
        self,
        backend: str,
        role: str,
        *,
        read_only: bool = False,
        system_prompt: str = "",
    ) -> AgentSession:
        return self.backends.open_session(
            backend,
            self.config.session_spec(
                role,
                self.worktree.path,
                read_only=read_only,
                system_prompt=system_prompt,
            ),
            _RunnerEventSink(self, backend, role),
        )

    def _agent_event(self, backend: str, role: str, event: Mapping[str, Any]) -> None:
        task = self._active_task
        if task is None:
            return
        for update in normalize_agent_event(backend, role, event):
            self._stream(task, update)

    def _stream(self, task: Task, event: StreamEvent) -> None:
        try:
            self.front.stream(task, event)
        except Exception as exc:
            log.warn("runner.stream_failed", task=task.id, error=log.clip(exc, 200))

    def _agents(self) -> tuple[AgentSession, AgentSession]:
        """Open role sessions after the shared worktree exists, then reuse them."""
        if self.fixer is None:
            self.fixer = self._make_agent(
                self.fixer_backend,
                "fixer",
                system_prompt=prompts.fixer_system(self.config.conventions),
            )
        if self.reviewer is None:
            reviewer_info = self.backends.probe(self.reviewer_backend)
            assert isinstance(reviewer_info, BackendInfo)
            self.reviewer = self._make_agent(
                self.reviewer_backend,
                "reviewer",
                read_only=True,
                system_prompt=prompts.reviewer_system(
                    self.config.conventions,
                    structured=reviewer_info.capabilities.structured_output,
                ),
            )
        return self.fixer, self.reviewer

    def _resolve_backend(self, requested: str) -> str:
        choices = self.config.backend_order if requested in ("", "auto") else [requested]
        reasons: list[str] = []
        for name in choices:
            try:
                info = self.backends.probe(name)
            except LookupError as exc:
                reasons.append(str(exc))
                continue
            assert isinstance(info, BackendInfo)
            if info.available:
                return name
            reasons.append(f"{name}: {info.reason or 'not available'}")
        raise SystemExit("agentq: no usable agent backend (" + "; ".join(reasons) + ")")

    def _resolve_front(self, front: Front | str | None) -> Front:
        from .fronts import make_front

        if isinstance(front, Front):
            return front
        return make_front(front or "folder", self.config, self.store)

    # --- lifecycle -------------------------------------------------------

    def run(self, *, once: bool = False, until: int | None = None) -> None:
        """Work the queue. ``once`` stops after one task; ``until`` after that task."""
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            run_id = self.lease.acquire()
        except RunnerBusy as exc:
            raise SystemExit(f"agentq: {exc}") from exc
        self.store.claimant = run_id
        self.front.store.claimant = run_id
        try:
            log.info(
                "runner.started",
                root=self.config.root,
                base=self.config.base_branch,
                front=self.front.name,
                checks=",".join(check.name for check in self.config.checks) or "none",
                delivery=self.config.delivery,
            )
            self.audit.emit(
                "runner.started",
                root=str(self.config.root),
                base=self.config.base_branch,
                fixer=self.fixer_backend,
                reviewer=self.reviewer_backend,
                run_id=run_id,
            )
            self._recover_agent_tasks()
            self.active_checks = self._baseline()
            self.front.start()
            try:
                if self.share is not None:
                    self.share.start(self.front)
                self.supervisor.start()
                self._recover_deliveries()
                self._loop(once=once, until=until)
            finally:
                if self.share is not None:
                    self.share.stop()
                self.front.stop()
                self.supervisor.stop()
                if self.fixer is not None:
                    self.fixer.close()
                if self.reviewer is not None:
                    self.reviewer.close()
                self.audit.emit("runner.stopped", run_id=run_id)
        finally:
            self.lease.release()

    def _baseline(self) -> list[Check]:
        """The configured checks, minus the ones that already fail on the base.

        A check that fails before any agent has touched anything would fail
        every task through no fault of the task — and the fixer would burn its
        rounds repairing a suite it did not break.
        """
        config = self.config
        self.allowed_check_failures = frozenset()
        if not config.checks or not config.baseline_checks:
            return list(config.checks)
        log.info("checks.baseline", count=len(config.checks))
        branch = f"{config.branch_prefix}/baseline"
        self.worktree.reset(branch, config.base_branch, recreate=True)
        try:
            healthy, failing = checks.baseline(config.checks, self.worktree.path)
        finally:
            self.worktree.detach(config.base_branch)
            git("branch", "-D", branch, cwd=config.root, check=False)
        self.audit.emit(
            "checks.baseline",
            mode=config.baseline_mode,
            healthy=[check.name for check in healthy],
            failing=[check.name for check in failing],
        )
        if failing:
            reason = (
                "they remain visible as known failures for this session"
                if config.baseline_mode == "compare"
                else "they fail on the clean base — fix them and restart agentq"
            )
            log.warn(
                "checks.failing_on_base",
                checks=",".join(check.name for check in failing),
                reason=reason,
            )
        if config.baseline_mode == "strict" and failing:
            names = ", ".join(check.name for check in failing)
            raise SystemExit(f"agentq: baseline checks fail in strict mode: {names}")
        if config.baseline_mode == "compare":
            self.allowed_check_failures = frozenset(check.name for check in failing)
            return list(config.checks)
        return healthy

    def _recover_deliveries(self) -> None:
        for task in self.store.delivery_tasks():
            log.info(
                "delivery.recovering",
                task=task.id,
                branch=task.delivery.branch,
                stage=task.delivery.stage,
            )
            self.land(
                task,
                task.delivery.branch,
                task.note,
                "",
                task.cost_usd,
            )

    def _recover_agent_tasks(self) -> None:
        for task in self.store.recover_orphans():
            log.warn("runner.task_recovered", task=task.id, branches=task.previous_branches)
            self.audit.emit(
                "task.recovered",
                task=task.id,
                previous_branches=task.previous_branches,
            )

    def _loop(self, *, once: bool, until: int | None) -> None:
        while not self.stopping:
            self.supervisor.supervise()
            pending_delivery = self.store.delivery_tasks()
            if pending_delivery:
                task = pending_delivery[0]
                self.land(
                    task,
                    task.delivery.branch,
                    task.note,
                    "",
                    task.cost_usd,
                )
                continue
            queued = self.front.next_task()
            if queued is None:
                if once or (until is not None and self._settled(until)):
                    log.info("runner.queue_empty")
                    return
                time.sleep(self.config.poll_seconds)
                continue
            task = queued
            try:
                self.handle(task)
            except Exception as exc:  # one bad task must not end the loop
                log.exception("runner.task_crashed", task=task.id)
                self.finish(task.id, Status.FAILED, f"the runner crashed: {log.clip(exc, 300)}")
            finally:
                self._active_task = None
                self._close_non_conversational_sessions()
            if once or task.id == until:
                return

    def _settled(self, task_id: int) -> bool:
        """Whether the task we were asked to wait for is already off the queue."""
        waited = self.store.get(task_id)
        return waited is None or not waited.is_open

    def _close_non_conversational_sessions(self) -> None:
        for attribute in ("fixer", "reviewer"):
            session = getattr(self, attribute)
            if session is not None and not session.capabilities.conversations:
                session.close()
                setattr(self, attribute, None)

    # --- one task --------------------------------------------------------

    def handle(self, task: Task) -> None:
        config = self.config
        base = config.base_branch
        branch = attempt_branch(config.branch_prefix, task.id, task.attempts)
        self._active_task = task
        self._stream(task, StreamEvent("runner", "reset"))
        log.info("runner.task_start", task=task.id, branch=branch, attempt=task.attempts)
        self.audit.emit("task.started", task=task.id, branch=branch, attempt=task.attempts)

        base_sha = git("rev-parse", base, cwd=config.root)
        self.worktree.reset(branch, base)
        fixer, reviewer = self._agents()
        current = self.store.update(task.id, branch=branch, note="", status=Status.RUNNING)
        self._stream(task, StreamEvent("runner", "status", "fixing"))
        self._stream(current or task, StreamEvent("fixer", "status", "working"))
        self.report(current or task, "started", "")

        spent = 0.0
        summary = ""
        objection = ""
        approved = False
        for round_no in range(1, config.max_rounds + 1):
            if self._over_budget(spent):
                self.finish(
                    task.id,
                    Status.BLOCKED,
                    f"the ${config.max_usd:.2f} budget is spent and the work is not approved; "
                    f"the branch `{branch}` is intact",
                    cost=spent,
                )
                return
            prompt = (
                prompts.fix(task, branch, base, self.supervisor.error_digest())
                if round_no == 1
                else prompts.revise(objection)
            )
            remaining = self._remaining(spent)
            self._stream(task, StreamEvent("fixer", "status", f"round {round_no}"))
            reply = self._ask(
                fixer,
                task,
                TurnRequest(prompt, max_cost_usd=remaining if remaining > 0 else None)
            )
            if reply is None:
                return
            self._audit_reply(task.id, round_no, "fixer", self.fixer_backend, reply)
            if reply.is_error:
                self._stream(task, StreamEvent("fixer", "error", reply.text))
                spent += reply.cost_usd or 0.0
                self.finish(
                    task.id,
                    Status.FAILED,
                    f"the fixer failed: {log.clip(reply.text, 500)}",
                    cost=spent,
                )
                return
            self._stream(task, StreamEvent("fixer", "result", self._reply_text(reply)))
            turn_cost = self._turn_cost(task.id, self.fixer_backend, reply, spent)
            if turn_cost is None:
                return
            spent += turn_cost
            summary = log.clip(reply.text, 600) or summary

            answer = prompts.answer_of(reply.text)
            if answer is not None and self.worktree.head() == base_sha:
                # The task was a question, or was already true. The reply is
                # the deliverable; scratch files, if any, die with the reset.
                log.info("runner.answered", task=task.id, round=round_no)
                self.finish(task.id, Status.DONE, answer, cost=spent)
                return
            self.commit_leftovers(task)

            if self.worktree.head() == base_sha:
                objection = "You committed nothing. The branch is still at the base commit."
                log.warn("runner.empty_branch", task=task.id, round=round_no)
                continue

            self._stream(task, StreamEvent("runner", "status", "running checks"))
            passed, report = checks.run(
                self.active_checks,
                self.worktree.path,
                allowed_failures=self.allowed_check_failures,
                cancelled=lambda: self._cancel_requested(task.id),
            )
            if self._cancel_requested(task.id):
                self._cancel_task(task)
                return
            self.audit.emit(
                "checks.completed",
                task=task.id,
                round=round_no,
                passed=passed,
                allowed_failures=sorted(self.allowed_check_failures),
            )
            if not passed:
                objection = f"The checks do not pass. Fix them first.\n{report}"
                log.warn("runner.checks_failed", task=task.id, round=round_no)
                continue

            self.store.update(task.id, status=Status.REVIEW, note=summary)
            self._stream(task, StreamEvent("runner", "status", "reviewing"))
            self._stream(task, StreamEvent("reviewer", "status", f"round {round_no}"))
            diff, stat = self._change_context(base)
            remaining = self._remaining(spent)
            verdict = self._ask(
                reviewer,
                task,
                TurnRequest(
                    prompts.review(task, branch, base, report, diff, stat),
                    schema=prompts.REVIEW_SCHEMA,
                    max_cost_usd=remaining if remaining > 0 else None,
                )
            )
            if verdict is None:
                return
            self._audit_reply(task.id, round_no, "reviewer", self.reviewer_backend, verdict)
            if verdict.is_error:
                self._stream(task, StreamEvent("reviewer", "error", verdict.text))
                spent += verdict.cost_usd or 0.0
                self.finish(
                    task.id,
                    Status.FAILED,
                    f"the reviewer failed: {log.clip(verdict.text, 500)}",
                    cost=spent,
                )
                return
            self._stream(task, StreamEvent("reviewer", "result", self._reply_text(verdict)))
            turn_cost = self._turn_cost(task.id, self.reviewer_backend, verdict, spent)
            if turn_cost is None:
                return
            spent += turn_cost
            decision, objection = self._decision_of(verdict)
            self.audit.emit(
                "review.completed", task=task.id, round=round_no, approved=decision
            )
            log.info("runner.reviewed", task=task.id, round=round_no, approved=decision)
            if decision is True:
                approved = True
                break
            if decision is None:
                # No verdict at all: treat it as a rejection, because the
                # alternative is landing on the strength of a truncated reply.
                objection = "You did not end your reply with a VERDICT line. " + objection

        if not approved:
            self.finish(
                task.id,
                Status.FAILED,
                objection or "the reviewer never approved it",
                cost=spent,
            )
            log.warn("runner.task_failed", task=task.id, branch=branch)
            return

        approved_task = self.store.approve(
            task.id,
            branch=branch,
            commit=self.worktree.head(),
            requested_mode=config.delivery,
            base_branch=base,
            summary=summary,
            review=objection,
        )
        if approved_task is None:
            self.finish(task.id, Status.FAILED, "the approved task disappeared", cost=spent)
            return
        self._stream(task, StreamEvent("runner", "status", "delivering"))
        self.land(approved_task, branch, summary, objection, spent)

    def _over_budget(self, spent: float) -> bool:
        return self.config.max_usd > 0 and spent >= self.config.max_usd

    def _ask(
        self, session: AgentSession, task: Task, request: TurnRequest
    ) -> AgentReply | None:
        if self._cancel_requested(task.id):
            self._cancel_task(task)
            return None
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentq-turn") as pool:
            future = pool.submit(session.ask, request)
            while True:
                try:
                    reply = future.result(timeout=0.2)
                    break
                except FutureTimeout:
                    if self._cancel_requested(task.id):
                        session.cancel()
        if self._cancel_requested(task.id):
            self._cancel_task(task)
            return None
        return reply

    def _cancel_requested(self, task_id: int) -> bool:
        current = self.store.get(task_id)
        return current is None or current.status == Status.CANCELLING

    def _cancel_task(self, task: Task) -> None:
        current = self.store.get(task.id)
        if current is None or current.status != Status.CANCELLING:
            return
        try:
            self.commit_leftovers(current)
        except Exception as exc:
            self.stopping = True
            self.finish(
                task.id,
                Status.BLOCKED,
                "cancellation stopped the agent, but partial work could not be preserved: "
                f"{log.clip(exc, 300)}",
            )
            return
        cancelled = self.store.complete_cancel(
            task.id,
            f"cancelled; partial work is preserved on `{current.branch}`"
            if current.branch
            else "cancelled",
        )
        if cancelled is None:
            return
        self.audit.emit("task.cancelled", task=task.id, branch=current.branch)
        self._stream(cancelled, StreamEvent("runner", "status", "cancelled"))
        self.report(cancelled, "cancelled", cancelled.note)

    def _remaining(self, spent: float) -> float:
        """What the next call may spend. Zero means no cap was configured."""
        if self.config.max_usd <= 0:
            return 0.0
        return max(self.config.max_usd - spent, 0.01)

    def _turn_cost(
        self, task_id: int, backend: str, reply: AgentReply, spent: float
    ) -> float | None:
        if self.config.max_usd > 0 and reply.cost_usd is None:
            self.finish(
                task_id,
                Status.BLOCKED,
                f"backend '{backend}' did not report exact USD cost; "
                "the configured budget cannot be enforced",
                cost=spent,
            )
            return None
        return reply.cost_usd or 0.0

    def _audit_reply(
        self, task_id: int, round_no: int, role: str, backend: str, reply: AgentReply
    ) -> None:
        usage = reply.usage
        agent = self.config.reviewer if role == "reviewer" else self.config.fixer
        self.audit.emit(
            "agent.turn",
            task=task_id,
            round=round_no,
            role=role,
            backend=backend,
            model=agent.model,
            error=reply.is_error,
            seconds=round(reply.seconds, 3),
            cost_usd=reply.cost_usd,
            context_tokens=reply.context_tokens,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            session_id=reply.session_id,
        )

    def _decision_of(self, verdict: AgentReply) -> tuple[bool | None, str]:
        """The reviewer's answer: structured when the CLI enforced the shape,
        the VERDICT line otherwise."""
        structured = verdict.structured or {}
        answer = str(structured.get("verdict") or "").strip().upper()
        if answer in ("APPROVE", "REJECT"):
            objection = str(structured.get("objection") or "") or verdict.text
            return answer == "APPROVE", log.clip(objection, 1200)
        return prompts.verdict_of(verdict.text), log.clip(verdict.text, 1200)

    @staticmethod
    def _reply_text(reply: AgentReply) -> str:
        if reply.text:
            return reply.text
        return json.dumps(reply.structured, ensure_ascii=False) if reply.structured else ""

    def _change_context(self, base: str) -> tuple[str, str]:
        """The diff and its stat, for the review prompt to embed or point at."""
        stat = git("diff", "--stat", f"{base}...HEAD", cwd=self.worktree.path, check=False)
        diff = git("diff", f"{base}...HEAD", cwd=self.worktree.path, check=False)
        return diff, stat

    def land(self, task: Task, branch: str, summary: str, review: str, spent: float) -> None:
        """Merge or open a pull request, and tell whoever asked."""
        try:
            resolved = task.delivery.resolved_mode or delivery.resolve_mode(
                self.config.root, task.delivery.requested_mode or self.config.delivery
            )
        except ValueError as exc:
            blocked = self.store.block_delivery(
                task.id, stage="preflight", code="invalid_mode", message=str(exc)
            )
            if blocked is not None:
                self.store.update(task.id, cost_usd=round(spent, 4))
                self.report(blocked, "blocked", self._delivery_failure(blocked, str(exc)))
            return
        current = self.store.start_delivery(task.id, resolved)
        if current is None:
            self.finish(task.id, Status.FAILED, "the approved delivery record is incomplete")
            return
        self.audit.emit("delivery.started", task=task.id, mode=resolved)
        landed = delivery.recover_delivery(
            self.config.root,
            current,
            summary,
            review,
            on_stage=lambda stage: self._record_delivery_stage(task.id, stage),
            validate_merge=self._validate_merge,
            integration_path=self.config.state_dir / "integration",
        )
        if not landed.ok:
            blocked = self.store.block_delivery(
                task.id,
                stage=landed.stage or "preflight",
                code="delivery_failed",
                message=landed.reason,
            )
            if blocked is not None:
                self.store.update(task.id, cost_usd=round(spent, 4))
                self.audit.emit(
                    "delivery.blocked",
                    task=task.id,
                    stage=landed.stage or "preflight",
                    reason=landed.reason,
                )
                self.report(
                    blocked,
                    "blocked",
                    self._delivery_failure(blocked, landed.reason),
                )
            return

        completed = self.store.complete_delivery(
            task.id, landed.outcome, url=landed.url
        )
        if completed is None:
            self.finish(task.id, Status.FAILED, "delivery succeeded but its task disappeared")
            return
        self.audit.emit(
            "delivery.completed", task=task.id, outcome=landed.outcome, url=landed.url or None
        )
        if landed.outcome == delivery.LOCAL_MERGE:
            # The branch is checked out here, so let go of it before deleting.
            self.worktree.detach(self.config.base_branch)
            git("branch", "-d", branch, cwd=self.config.root, check=False)
            if self.supervisor.enabled:
                log.info("runner.restarting_process")
                self.supervisor.restart()

        note = (
            f"{summary}\n\nMerged locally into `{self.config.base_branch}`; "
            "the remote repository was not updated."
            if landed.outcome == delivery.LOCAL_MERGE
            else f"{summary}\n\nPull request opened: {landed.url}"
        )
        self.finish(task.id, Status.DONE, note, cost=spent)

    @staticmethod
    def _delivery_failure(task: Task, reason: str) -> str:
        return (
            "The work was approved but not delivered.\n"
            f"Branch: `{task.delivery.branch}`\n"
            f"Commit: `{task.delivery.commit}`\n"
            f"Stage: `{task.delivery.stage or 'preflight'}`\n"
            f"Reason: {reason}\n"
            f"Retry only delivery with `agentq retry-delivery {task.id}`."
        )

    def _record_delivery_stage(self, task_id: int, stage: str) -> None:
        self.store.update_delivery_stage(task_id, stage)
        self.audit.emit("delivery.stage", task=task_id, stage=stage)

    def _validate_merge(self, path: Path) -> tuple[bool, str]:
        passed, report = checks.run(
            self.active_checks,
            path,
            allowed_failures=self.allowed_check_failures,
        )
        self.audit.emit("delivery.integration_checks", passed=passed)
        return passed, report

    def commit_leftovers(self, task: Task) -> None:
        """Commit what the agent forgot to.

        It is told to commit, and usually does. When it does not, the work is
        one reset away from being gone — an extra commit with an honest message
        is the cheaper mistake.
        """
        if not self.worktree.is_dirty():
            return
        log.warn("runner.uncommitted_leftovers", task=task.id)
        self.worktree.commit_all(f"agentq #{task.id}: uncommitted leftovers from the agent")

    # --- telling whoever asked ------------------------------------------

    def finish(self, task_id: int, status: str, note: str, *, cost: float | None = None) -> None:
        changes: dict[str, object] = {
            "status": status,
            "note": note,
            "claimed_by": "",
            "claimed_at": "",
        }
        if cost is not None:
            changes["cost_usd"] = round(cost, 4)
        task = self.store.update(task_id, **changes)
        if task is None:  # deleted from under us
            return
        log.info("runner.task_finished", task=task_id, status=status, cost=cost and round(cost, 3))
        self.audit.emit("task.finished", task=task_id, status=status, cost_usd=cost, note=note)
        event = {Status.DONE: "done", Status.BLOCKED: "blocked"}.get(status, "failed")
        self.report(task, event, note)

    def report(self, task: Task, event: str, text: str) -> None:
        try:
            self.front.report(task, event, text)
        except Exception as exc:  # a courtesy must never take the runner down
            log.warn("runner.report_failed", task=task.id, error=log.clip(exc, 200))


__all__ = ["Runner"]
