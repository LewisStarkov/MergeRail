"""The loop: claim a task, fix it, check it, review it, land it.

One worktree, two long-lived agents, tasks worked one at a time. The order of
the steps is the whole design:

1. the shared worktree is reset onto a fresh branch cut from the base — the
   agents never edit the checkout the running app imports from;
2. a **fixer** agent makes the change and commits it;
3. the runner itself runs the project's checks;
4. a **reviewer** agent, in the same worktree but forbidden from editing
   anything, reads the diff *and the check results*, and answers APPROVE or
   REJECT;
5. a rejection — or a failing check, which never reaches the reviewer — goes
   back to the fixer *in the same session*, up to ``max_rounds`` times;
6. an approval lands: merged into the base branch, or opened as a pull request.

Nothing here is retried harder than that. A task that cannot be made to pass in
three rounds is a task that needed a person in round one.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import checks, delivery, log, prompts
from .agent import ClaudeAgent
from .config import Config
from .fronts.base import Front
from .gitctl import Worktree, git, repo_root
from .supervisor import Supervisor
from .tasks import Status, Task, TaskStore


class Runner:
    """Everything wired together. ``Runner().run()`` is a working installation."""

    def __init__(
        self,
        config: Config | None = None,
        front: Front | str | None = None,
        *,
        supervise: bool = True,
    ) -> None:
        self.config = config or Config.load(repo_root(Path.cwd()))
        self.store = TaskStore(self.config.queue_path)
        self.front = self._resolve_front(front)
        self.supervisor = Supervisor(
            self.config.process if supervise else [], self.config.root, self.config.process_log
        )
        self.worktree = Worktree(self.config.root, self.config.worktree)
        options = self.config.agent_options()
        self.fixer = ClaudeAgent("fixer", self.worktree.path, options)
        self.reviewer = ClaudeAgent("reviewer", self.worktree.path, options, read_only=True)
        self.stopping = False

    def _resolve_front(self, front: Front | str | None) -> Front:
        from .fronts import make_front

        if isinstance(front, Front):
            return front
        return make_front(front or "folder", self.config, self.store)

    # --- lifecycle -------------------------------------------------------

    def run(self, *, once: bool = False) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "runner.started",
            root=self.config.root,
            base=self.config.base_branch,
            front=self.front.name,
            checks=",".join(check.name for check in self.config.checks) or "none",
            delivery=self.config.delivery,
        )
        self.front.start()
        self.supervisor.start()
        try:
            self._loop(once=once)
        finally:
            self.front.stop()
            self.supervisor.stop()

    def _loop(self, *, once: bool) -> None:
        while not self.stopping:
            self.supervisor.supervise()
            task = self.front.next_task()
            if task is None:
                if once:
                    log.info("runner.queue_empty")
                    return
                time.sleep(self.config.poll_seconds)
                continue
            try:
                self.handle(task)
            except Exception as exc:  # one bad task must not end the loop
                log.exception("runner.task_crashed", task=task.id)
                self.finish(task.id, Status.FAILED, f"the runner crashed: {log.clip(exc, 300)}")
            if once:
                return

    # --- one task --------------------------------------------------------

    def handle(self, task: Task) -> None:
        config = self.config
        base = config.base_branch
        branch = f"{config.branch_prefix}/{task.id}"
        log.info("runner.task_start", task=task.id, branch=branch, attempt=task.attempts)

        base_sha = git("rev-parse", base, cwd=config.root)
        self.worktree.reset(branch, base)
        current = self.store.update(task.id, branch=branch, note="", status=Status.RUNNING)
        self.report(current or task, "started", "")

        summary = ""
        objection = ""
        report = ""
        approved = False
        for round_no in range(1, config.max_rounds + 1):
            prompt = (
                prompts.fix(task, branch, base, config.conventions, self.supervisor.error_digest())
                if round_no == 1
                else prompts.revise(objection)
            )
            reply = self.fixer.ask(prompt)
            summary = log.clip(reply.text, 600) or summary
            self.commit_leftovers(task)

            if self.worktree.head() == base_sha:
                objection = "You committed nothing. The branch is still at the base commit."
                log.warn("runner.empty_branch", task=task.id, round=round_no)
                continue

            passed, report = checks.run(config.checks, self.worktree.path)
            if not passed:
                objection = f"The checks do not pass. Fix them first.\n{report}"
                log.warn("runner.checks_failed", task=task.id, round=round_no)
                continue

            self.store.update(task.id, status=Status.REVIEW, note=summary)
            verdict = self.reviewer.ask(
                prompts.review(task, branch, base, config.conventions, report)
            )
            decision = prompts.verdict_of(verdict.text)
            objection = log.clip(verdict.text, 1200)
            log.info("runner.reviewed", task=task.id, round=round_no, approved=decision)
            if decision is True:
                approved = True
                break
            if decision is None:
                # No verdict line at all: treat it as a rejection, because the
                # alternative is landing on the strength of a truncated reply.
                objection = "You did not end your reply with a VERDICT line. " + objection

        if not approved:
            self.finish(task.id, Status.FAILED, objection or "the reviewer never approved it")
            log.warn("runner.task_failed", task=task.id, branch=branch)
            return

        self.land(task, branch, summary, objection)

    def land(self, task: Task, branch: str, summary: str, review: str) -> None:
        """Merge or open a pull request, and tell whoever asked."""
        base = self.config.base_branch
        landed = delivery.land(
            self.config.root, task, branch, base, self.config.delivery, summary, review
        )
        if not landed.ok:
            self.finish(task.id, Status.BLOCKED, landed.reason)
            return

        if landed.kind == delivery.MERGE:
            # The branch is checked out here, so let go of it before deleting.
            self.worktree.detach(base)
            git("branch", "-d", branch, cwd=self.config.root, check=False)
            if self.supervisor.enabled:
                log.info("runner.restarting_process")
                self.supervisor.restart()

        self.store.update(task.id, url=landed.url)
        note = summary if landed.kind == delivery.MERGE else f"{summary}\n\nPull request opened."
        self.finish(task.id, Status.DONE, note)

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

    def finish(self, task_id: int, status: str, note: str) -> None:
        task = self.store.update(task_id, status=status, note=note)
        if task is None:  # deleted from under us
            return
        log.info("runner.task_finished", task=task_id, status=status)
        event = {Status.DONE: "done", Status.BLOCKED: "blocked"}.get(status, "failed")
        self.report(task, event, note)

    def report(self, task: Task, event: str, text: str) -> None:
        try:
            self.front.report(task, event, text)
        except Exception as exc:  # a courtesy must never take the runner down
            log.warn("runner.report_failed", task=task.id, error=log.clip(exc, 200))


__all__ = ["Runner"]
