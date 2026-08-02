"""Where approved work goes: into the base branch, or into a pull request.

Nothing here is forced. The base branch is what the running deployment builds
from, and a conflict resolved by a script at three in the morning is how a
payment handler ends up half-rewritten. A merge that cannot be made cleanly is
not retried harder — it becomes a pull request, and if that is impossible too,
it is left as a branch with an explanation.

That fallback is why ``auto`` is the default and why the setting matters less
than it looks: a protected base branch refuses the merge, the change arrives as
a PR, and the repository's own policy has decided the question without anyone
configuring anything.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import log
from .gitctl import current_branch, git, remote_url
from .tasks import Task

#: How a change is allowed to land.
MERGE = "merge"
PR = "pr"
AUTO = "auto"


@dataclass(frozen=True, slots=True)
class Landing:
    ok: bool
    #: ``merge``, ``pr`` or ``""`` when nothing worked.
    kind: str
    reason: str = ""
    url: str = ""


def can_open_pr(root: Path) -> bool:
    return bool(remote_url(root)) and shutil.which("gh") is not None


def merge_into_base(root: Path, branch: str, base: str) -> Landing:
    """Fast-forward if we can, a real merge if the base moved, else give up."""
    checked_out = current_branch(root)
    if checked_out != base:
        return Landing(False, "", f"the working checkout is on '{checked_out}', not '{base}'")
    refusal = "git merge refused"
    for strategy in (["--ff-only"], ["--no-ff", "-m", f"Merge {branch}"]):
        result = subprocess.run(
            ["git", "merge", *strategy, branch], cwd=root, capture_output=True, text=True
        )
        if result.returncode == 0:
            log.info("delivery.merged", branch=branch, strategy=strategy[0])
            return Landing(True, MERGE)
        refusal = f"git merge {strategy[0]}: {result.stderr or result.stdout}"
        git("merge", "--abort", cwd=root, check=False)
    return Landing(False, "", log.clip(refusal, 400))


def open_pull_request(root: Path, task: Task, branch: str, base: str, body: str) -> Landing:
    """Push the branch and open a PR with the reviewer's own words in it."""
    if not can_open_pr(root):
        missing = "no 'origin' remote" if not remote_url(root) else "the 'gh' CLI is not installed"
        return Landing(False, "", f"cannot open a pull request: {missing}")

    pushed = subprocess.run(
        ["git", "push", "--set-upstream", "origin", branch, "--force-with-lease"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if pushed.returncode != 0:
        return Landing(False, "", log.clip(f"git push: {pushed.stderr or pushed.stdout}", 400))

    created = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            log.clip(task.title or f"agentq #{task.id}", 120),
            "--body",
            body,
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        text = created.stderr or created.stdout
        # An existing PR for this branch is a success: the branch was updated
        # by the push, which is what the second round was for.
        if "already exists" in text:
            url = subprocess.run(
                ["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"],
                cwd=root,
                capture_output=True,
                text=True,
            ).stdout.strip()
            log.info("delivery.pr_updated", branch=branch, url=url)
            return Landing(True, PR, url=url)
        return Landing(False, "", log.clip(f"gh pr create: {text}", 400))

    url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
    log.info("delivery.pr_opened", branch=branch, url=url)
    return Landing(True, PR, url=url)


def pr_body(task: Task, summary: str, review: str) -> str:
    """What a human needs to decide, in the order they need it."""
    return (
        f"**Task #{task.id}** — as written:\n\n> {task.text or '(no text)'}\n\n"
        f"## What changed\n\n{summary or '(no summary)'}\n\n"
        f"## Review\n\nAn adversarial reviewer read this diff with the check results in hand "
        f"and approved it:\n\n{review or '(no review recorded)'}\n\n"
        f"---\nOpened by [agentq](https://github.com/) — the checks below already passed "
        f"locally on this commit."
    )


def land(
    root: Path, task: Task, branch: str, base: str, mode: str, summary: str, review: str
) -> Landing:
    """Merge, open a PR, or explain why neither happened."""
    if mode in (MERGE, AUTO):
        landed = merge_into_base(root, branch, base)
        if landed.ok:
            return landed
        if mode == MERGE:
            return landed
        log.warn("delivery.merge_refused", reason=log.clip(landed.reason, 120))
    return open_pull_request(root, task, branch, base, pr_body(task, summary, review))


__all__ = [
    "AUTO",
    "MERGE",
    "PR",
    "Landing",
    "can_open_pr",
    "land",
    "merge_into_base",
    "open_pull_request",
    "pr_body",
]
