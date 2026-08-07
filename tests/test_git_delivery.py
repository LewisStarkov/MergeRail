from __future__ import annotations

import sys
import threading
from pathlib import Path

from agentq.checks import baseline
from agentq.checks import run as run_checks
from agentq.delivery import LOCAL, Landing, land, merge_into_base, pr_body, resolve_mode
from agentq.detect import Check
from agentq.gitctl import GitError, Worktree, current_branch, detect_base_branch, git, remote_url
from agentq.tasks import Task
from tests.conftest import run

PASSING = Check("yes", [sys.executable, "-c", "pass"])
FAILING = Check("no", [sys.executable, "-c", "print('boom'); raise SystemExit(1)"])


def test_base_branch_prefers_main(repo: Path) -> None:
    assert detect_base_branch(repo) == "main"


def test_base_branch_honours_what_the_clone_recorded(repo: Path) -> None:
    # A repository whose life happens on `develop` says so in origin/HEAD.
    run("checkout", "-qb", "develop", cwd=repo)
    run("checkout", "-q", "main", cwd=repo)
    run("remote", "add", "origin", "https://example.invalid/x.git", cwd=repo)
    run("update-ref", "refs/remotes/origin/develop", "HEAD", cwd=repo)
    run("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop", cwd=repo)
    assert detect_base_branch(repo) == "develop"


def test_a_worktree_is_reset_between_tasks(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree(repo, tmp_path / "wt")
    worktree.reset("agentq/1", "main")
    (worktree.path / "one.txt").write_text("first task", encoding="utf-8")
    worktree.commit_all("first")
    first_head = worktree.head()

    worktree.reset("agentq/2", "main")
    assert worktree.head() != first_head
    assert not (worktree.path / "one.txt").exists()
    # The first task's work survives on its own branch.
    assert git("rev-parse", "agentq/1", cwd=repo) == first_head


def test_a_worktree_never_resets_an_existing_task_branch(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree(repo, tmp_path / "wt")
    worktree.reset("agentq/1/a1", "main")
    (worktree.path / "approved.txt").write_text("keep me", encoding="utf-8")
    worktree.commit_all("approved")
    approved = worktree.head()

    try:
        worktree.reset("agentq/1/a1", "main")
    except GitError as exc:
        assert "refusing to reset" in str(exc)
    else:
        raise AssertionError("existing branches must be protected")
    assert git("rev-parse", "agentq/1/a1", cwd=repo) == approved


def test_leftovers_are_visible_as_dirt(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree(repo, tmp_path / "wt")
    worktree.reset("agentq/1", "main")
    assert worktree.is_dirty() is False
    (worktree.path / "x.txt").write_text("x", encoding="utf-8")
    assert worktree.is_dirty() is True


def test_merge_fast_forwards_into_the_base(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree(repo, tmp_path / "wt")
    worktree.reset("agentq/1", "main")
    (worktree.path / "one.txt").write_text("done", encoding="utf-8")
    worktree.commit_all("work")

    landed = merge_into_base(repo, "agentq/1", "main")
    assert landed.ok and landed.kind == "merge"
    assert (repo / "one.txt").exists()


def test_validated_merge_checks_the_combined_tree_before_moving_base(
    repo: Path, tmp_path: Path
) -> None:
    run("config", "user.email", "t@example.com", cwd=repo)
    run("config", "user.name", "test", cwd=repo)
    worktree = Worktree(repo, tmp_path / "wt")
    worktree.reset("agentq/1", "main")
    (worktree.path / "agent.txt").write_text("agent", encoding="utf-8")
    worktree.commit_all("agent work")
    approved = worktree.head()

    (repo / "base.txt").write_text("base", encoding="utf-8")
    run("add", "-A", cwd=repo)
    run("commit", "-qm", "base moved", cwd=repo)
    seen: list[set[str]] = []

    def validate(path: Path) -> tuple[bool, str]:
        seen.append({item.name for item in path.iterdir()})
        return True, "green"

    landed = merge_into_base(
        repo,
        "agentq/1",
        "main",
        commit=approved,
        validate=validate,
        integration_path=tmp_path / "integration",
    )
    assert landed.ok
    assert seen and {"agent.txt", "base.txt"} <= seen[0]
    assert (repo / "agent.txt").exists()


def test_failed_integration_checks_leave_base_untouched(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree(repo, tmp_path / "wt")
    worktree.reset("agentq/1", "main")
    (worktree.path / "agent.txt").write_text("agent", encoding="utf-8")
    worktree.commit_all("agent work")
    before = git("rev-parse", "main", cwd=repo)

    landed = merge_into_base(
        repo,
        "agentq/1",
        "main",
        validate=lambda _path: (False, "combined suite failed"),
        integration_path=tmp_path / "integration",
    )
    assert not landed.ok
    assert landed.stage == "integration_checks"
    assert git("rev-parse", "main", cwd=repo) == before
    assert not (repo / "agent.txt").exists()
    assert not (tmp_path / "integration").exists()


def test_merge_refuses_when_the_checkout_is_elsewhere(repo: Path) -> None:
    run("checkout", "-qb", "somewhere-else", cwd=repo)
    landed = merge_into_base(repo, "agentq/1", "main")
    assert landed.ok is False
    assert "somewhere-else" in landed.reason
    assert current_branch(repo) == "somewhere-else"


def test_merge_refuses_a_dirty_checkout(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree(repo, tmp_path / "wt")
    worktree.reset("agentq/1", "main")
    (worktree.path / "one.txt").write_text("done", encoding="utf-8")
    worktree.commit_all("work")
    # Somebody is mid-thought in the main checkout; their work is theirs.
    (repo / "README.md").write_text("half-written\n", encoding="utf-8")

    landed = merge_into_base(repo, "agentq/1", "main")
    assert landed.ok is False
    assert "uncommitted" in landed.reason
    assert (repo / "README.md").read_text(encoding="utf-8") == "half-written\n"


def test_without_a_remote_a_pr_is_impossible_and_says_so(repo: Path, tmp_path: Path) -> None:
    assert remote_url(repo) == ""
    worktree = Worktree(repo, tmp_path / "wt")
    worktree.reset("agentq/1", "main")
    (worktree.path / "one.txt").write_text("done", encoding="utf-8")
    worktree.commit_all("work")
    run("checkout", "-qb", "elsewhere", cwd=repo)

    # Explicit PR mode reports its missing prerequisite.
    landed = land(repo, Task(id=1, text="x"), "agentq/1", "main", "auto", "summary", "review")
    assert landed == Landing(False, "", landed.reason, stage="preflight")
    assert "elsewhere" in landed.reason


def test_auto_resolves_to_local_without_pr_capabilities(repo: Path) -> None:
    assert resolve_mode(repo, "auto") == LOCAL


def test_pr_body_carries_the_task_and_the_review() -> None:
    body = pr_body(Task(id=7, text="fix the header"), "moved the div", "VERDICT: APPROVE")
    assert "#7" in body and "fix the header" in body
    assert "moved the div" in body and "APPROVE" in body


def test_checks_report_the_failure_they_saw(repo: Path) -> None:
    passed, report = run_checks([PASSING, FAILING], repo)
    assert passed is False
    assert "yes: PASS" in report and "no: FAIL" in report
    assert "boom" in report


def test_baseline_splits_the_healthy_from_the_already_broken(repo: Path) -> None:
    healthy, failing = baseline([PASSING, FAILING], repo)
    assert healthy == [PASSING]
    assert failing == [FAILING]


def test_known_baseline_failures_still_run_without_failing_the_round(repo: Path) -> None:
    passed, report = run_checks(
        [FAILING], repo, allowed_failures=frozenset({FAILING.name})
    )
    assert passed
    assert "KNOWN FAIL" in report


def test_a_missing_tool_is_a_failure_not_a_skip(repo: Path) -> None:
    passed, report = run_checks([Check("ghost", ["definitely-not-installed"])], repo)
    assert passed is False
    assert "not found" in report


def test_checks_can_be_cancelled(repo: Path) -> None:
    stop = threading.Event()
    timer = threading.Timer(0.1, stop.set)
    timer.start()
    try:
        passed, report = run_checks(
            [Check("slow", [sys.executable, "-c", "import time; time.sleep(60)"])],
            repo,
            cancelled=stop.is_set,
        )
    finally:
        timer.cancel()
    assert not passed
    assert "cancelled" in report


def test_no_checks_is_not_a_failure(repo: Path) -> None:
    passed, report = run_checks([], repo)
    assert passed is True
    assert "no checks" in report
