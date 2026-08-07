"""Where approved work goes: into the base branch, or into a pull request.

Nothing here is forced and nothing silently changes strategy. ``auto`` resolves
once from repository capabilities, then the selected merge or PR operation is
idempotent enough to recover after a process crash.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import log
from .gitctl import commit_exists, current_branch, git, is_ancestor, ref_exists, remote_url
from .tasks import Task

#: How a change is allowed to land.
MERGE = "merge"
LOCAL = "local"
PR = "pr"
AUTO = "auto"

LOCAL_MERGE = "local_merge"
PULL_REQUEST = "pull_request"

StageSink = Callable[[str], None]
MergeValidator = Callable[[Path], tuple[bool, str]]


@dataclass(frozen=True, slots=True)
class Landing:
    ok: bool
    #: ``merge``, ``pr`` or ``""`` when nothing worked.
    kind: str
    reason: str = ""
    url: str = ""
    stage: str = ""
    outcome: str = ""


def can_open_pr(root: Path) -> bool:
    return bool(remote_url(root)) and shutil.which("gh") is not None


def resolve_mode(root: Path, mode: str) -> str:
    """Resolve a delivery strategy exactly once, with no later fallback."""
    if mode == AUTO:
        # A configured remote means delivery is expected to leave the machine.
        # Missing ``gh`` is therefore a visible PR preflight error, not
        # permission to modify the local base branch instead.
        return PR if remote_url(root) else LOCAL
    if mode == MERGE:  # compatibility with v1 configuration
        return LOCAL
    if mode in (LOCAL, PR):
        return mode
    raise ValueError(f"unknown delivery mode: {mode}")


def merge_into_base(
    root: Path,
    branch: str,
    base: str,
    *,
    commit: str = "",
    on_stage: StageSink | None = None,
    validate: MergeValidator | None = None,
    integration_path: Path | None = None,
) -> Landing:
    """Fast-forward if we can, a real merge if the base moved, else give up."""
    target = commit or branch
    if commit and not commit_exists(root, target):
        return Landing(False, "", f"approved commit '{target}' does not exist", stage="preflight")
    if commit and is_ancestor(root, target, base):
        return Landing(True, MERGE, stage="complete", outcome=LOCAL_MERGE)
    checked_out = current_branch(root)
    if checked_out != base:
        return Landing(
            False,
            "",
            f"the working checkout is on '{checked_out}', not '{base}'",
            stage="preflight",
        )
    # A merge into a dirty checkout can tangle somebody's half-written work
    # into the landing. Untracked files are fine — git refuses collisions on
    # its own — but modified tracked files mean a person is mid-thought here.
    status = git("status", "--porcelain", cwd=root, check=False)
    dirty = [line for line in status.splitlines() if line and not line.startswith("??")]
    if dirty:
        return Landing(
            False,
            "",
            f"the working checkout has {len(dirty)} uncommitted change(s)",
            stage="preflight",
        )
    if not commit_exists(root, target):
        return Landing(False, "", f"approved commit '{target}' does not exist", stage="preflight")
    if is_ancestor(root, target, base):
        return Landing(True, MERGE, stage="complete", outcome=LOCAL_MERGE)
    if validate is not None:
        if integration_path is None:
            return Landing(False, "", "integration path is required", stage="preflight")
        return _validated_merge(
            root,
            branch,
            base,
            target,
            integration_path,
            validate,
            on_stage,
        )
    return _merge_current(root, branch, base, target, on_stage)


def _merge_current(
    root: Path,
    branch: str,
    base: str,
    target: str,
    on_stage: StageSink | None,
) -> Landing:
    refusal = "git merge refused"
    if on_stage:
        on_stage("merge")
    for strategy in (["--ff-only"], ["--no-ff", "-m", f"Merge {branch}"]):
        result = subprocess.run(
            ["git", "merge", *strategy, target], cwd=root, capture_output=True, text=True
        )
        if result.returncode == 0:
            if not is_ancestor(root, target, base):
                return Landing(
                    False,
                    "",
                    "merge completed but the approved commit is not in the base branch",
                    stage="merge",
                )
            log.info("delivery.merged", branch=branch, strategy=strategy[0])
            return Landing(True, MERGE, stage="complete", outcome=LOCAL_MERGE)
        refusal = f"git merge {strategy[0]}: {result.stderr or result.stdout}"
        git("merge", "--abort", cwd=root, check=False)
    return Landing(False, "", log.clip(refusal, 400), stage="merge")


def _validated_merge(
    root: Path,
    branch: str,
    base: str,
    target: str,
    path: Path,
    validate: MergeValidator,
    on_stage: StageSink | None,
) -> Landing:
    base_sha = git("rev-parse", base, cwd=root)
    git("worktree", "remove", "--force", str(path), cwd=root, check=False)
    shutil.rmtree(path, ignore_errors=True)
    git("worktree", "prune", cwd=root, check=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    added = subprocess.run(
        ["git", "worktree", "add", "--detach", str(path), base_sha],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        return Landing(
            False,
            "",
            log.clip(f"git worktree add: {added.stderr or added.stdout}", 400),
            stage="integration",
        )
    try:
        if on_stage:
            on_stage("integration")
        if is_ancestor(root, base_sha, target):
            git("checkout", "--detach", target, cwd=path)
            integrated = target
        else:
            merged = subprocess.run(
                ["git", "merge", "--no-ff", "-m", f"Merge {branch}", target],
                cwd=path,
                capture_output=True,
                text=True,
            )
            if merged.returncode != 0:
                git("merge", "--abort", cwd=path, check=False)
                return Landing(
                    False,
                    "",
                    log.clip(f"git merge --no-ff: {merged.stderr or merged.stdout}", 400),
                    stage="integration",
                )
            integrated = git("rev-parse", "HEAD", cwd=path)

        passed, report = validate(path)
        if not passed:
            return Landing(
                False,
                "",
                f"integration checks failed: {log.clip(report, 1200)}",
                stage="integration_checks",
            )
        if current_branch(root) != base or git("rev-parse", base, cwd=root) != base_sha:
            return Landing(
                False,
                "",
                f"base branch '{base}' moved during validation",
                stage="merge",
            )
        status = git("status", "--porcelain", cwd=root, check=False)
        if any(line and not line.startswith("??") for line in status.splitlines()):
            return Landing(
                False,
                "",
                "the working checkout changed during validation",
                stage="merge",
            )
        if on_stage:
            on_stage("merge")
        merged = subprocess.run(
            ["git", "merge", "--ff-only", integrated],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if merged.returncode != 0:
            return Landing(
                False,
                "",
                log.clip(f"git merge --ff-only: {merged.stderr or merged.stdout}", 400),
                stage="merge",
            )
        if not is_ancestor(root, target, base):
            return Landing(
                False,
                "",
                "merge completed but the approved commit is not in the base branch",
                stage="merge",
            )
        log.info("delivery.merged", branch=branch, strategy="validated")
        return Landing(True, MERGE, stage="complete", outcome=LOCAL_MERGE)
    finally:
        git("worktree", "remove", "--force", str(path), cwd=root, check=False)
        shutil.rmtree(path, ignore_errors=True)


def _existing_pr(root: Path, branch: str) -> tuple[str, str]:
    found = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            branch,
            "--json",
            "url,headRefOid",
            "--jq",
            "[.url,.headRefOid] | @tsv",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if found.returncode != 0:
        return "", ""
    url, _, commit = found.stdout.strip().partition("\t")
    return url, commit


def open_pull_request(
    root: Path,
    task: Task,
    branch: str,
    base: str,
    body: str,
    *,
    commit: str = "",
    on_stage: StageSink | None = None,
) -> Landing:
    """Push the branch and open a PR with the reviewer's own words in it."""
    if not can_open_pr(root):
        missing = "no 'origin' remote" if not remote_url(root) else "the 'gh' CLI is not installed"
        return Landing(False, "", f"cannot open a pull request: {missing}", stage="preflight")

    target = commit or branch
    if not commit_exists(root, target):
        return Landing(False, "", f"approved commit '{target}' does not exist", stage="preflight")
    if commit and ref_exists(root, f"refs/heads/{branch}"):
        branch_head = git("rev-parse", branch, cwd=root, check=False)
        if branch_head != commit:
            return Landing(
                False,
                "",
                f"branch '{branch}' moved after approval ({branch_head} != {commit})",
                stage="preflight",
            )

    existing_url, existing_commit = _existing_pr(root, branch)
    if existing_url:
        if commit and existing_commit != commit:
            return Landing(
                False,
                "",
                "pull request head moved after approval "
                f"({existing_commit or 'unknown'} != {commit})",
                stage="preflight",
            )
        return Landing(True, PR, url=existing_url, stage="complete", outcome=PULL_REQUEST)

    if on_stage:
        on_stage("push")
    pushed = subprocess.run(
        ["git", "push", "--set-upstream", "origin", f"{target}:refs/heads/{branch}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if pushed.returncode != 0:
        return Landing(
            False,
            "",
            log.clip(f"git push: {pushed.stderr or pushed.stdout}", 400),
            stage="push",
        )

    existing_url, existing_commit = _existing_pr(root, branch)
    if existing_url:
        if commit and existing_commit != commit:
            return Landing(
                False,
                "",
                "pull request head does not match the approved commit "
                f"({existing_commit} != {commit})",
                stage="push",
            )
        log.info("delivery.pr_updated", branch=branch, url=existing_url)
        return Landing(True, PR, url=existing_url, stage="complete", outcome=PULL_REQUEST)

    if on_stage:
        on_stage("create_pr")
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
            url, pr_commit = _existing_pr(root, branch)
            if commit and pr_commit != commit:
                return Landing(
                    False,
                    "",
                    f"existing pull request head does not match approval ({pr_commit} != {commit})",
                    stage="create_pr",
                )
            log.info("delivery.pr_updated", branch=branch, url=url)
            return Landing(True, PR, url=url, stage="complete", outcome=PULL_REQUEST)
        return Landing(False, "", log.clip(f"gh pr create: {text}", 400), stage="create_pr")

    url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
    log.info("delivery.pr_opened", branch=branch, url=url)
    return Landing(True, PR, url=url, stage="complete", outcome=PULL_REQUEST)


def pr_body(task: Task, summary: str, review: str) -> str:
    """What a human needs to decide, in the order they need it."""
    return (
        f"**Task #{task.id}** — as written:\n\n> {task.text or '(no text)'}\n\n"
        f"## What changed\n\n{summary or '(no summary)'}\n\n"
        f"## Review\n\nAn adversarial reviewer read this diff with the check results in hand "
        f"and approved it:\n\n{review or '(no review recorded)'}\n\n"
        f"---\nOpened by agentq — the checks already passed locally on this commit."
    )


def land(
    root: Path,
    task: Task,
    branch: str,
    base: str,
    mode: str,
    summary: str,
    review: str,
    *,
    commit: str = "",
    on_stage: StageSink | None = None,
    validate_merge: MergeValidator | None = None,
    integration_path: Path | None = None,
) -> Landing:
    """Deliver through one deterministic strategy; never hide a fallback."""
    resolved = resolve_mode(root, mode)
    if resolved == LOCAL:
        return merge_into_base(
            root,
            branch,
            base,
            commit=commit,
            on_stage=on_stage,
            validate=validate_merge,
            integration_path=integration_path,
        )
    return open_pull_request(
        root,
        task,
        branch,
        base,
        pr_body(task, summary, review),
        commit=commit,
        on_stage=on_stage,
    )


def recover_delivery(
    root: Path,
    task: Task,
    summary: str = "",
    review: str = "",
    *,
    on_stage: StageSink | None = None,
    validate_merge: MergeValidator | None = None,
    integration_path: Path | None = None,
) -> Landing:
    """Resume the persisted operation after a runner restart."""
    record = task.delivery
    if not record.branch or not record.commit or not record.base_branch:
        return Landing(
            False,
            "",
            "delivery record is missing branch, commit, or base branch",
            stage="preflight",
        )
    mode = record.resolved_mode or record.requested_mode
    return land(
        root,
        task,
        record.branch,
        record.base_branch,
        mode,
        summary or record.summary,
        review or record.review,
        commit=record.commit,
        on_stage=on_stage,
        validate_merge=validate_merge,
        integration_path=integration_path,
    )


__all__ = [
    "AUTO",
    "LOCAL",
    "LOCAL_MERGE",
    "MERGE",
    "PR",
    "PULL_REQUEST",
    "Landing",
    "MergeValidator",
    "StageSink",
    "can_open_pr",
    "land",
    "merge_into_base",
    "open_pull_request",
    "pr_body",
    "recover_delivery",
    "resolve_mode",
]
