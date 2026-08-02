from __future__ import annotations

from pathlib import Path

from agentq.checks import run as run_checks
from agentq.delivery import Landing, land, merge_into_base, pr_body
from agentq.detect import Check
from agentq.gitctl import Worktree, current_branch, detect_base_branch, git, remote_url
from agentq.tasks import Task
from tests.conftest import run


def test_base_branch_prefers_main(repo: Path) -> None:
    assert detect_base_branch(repo) == "main"


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


def test_merge_refuses_when_the_checkout_is_elsewhere(repo: Path) -> None:
    run("checkout", "-qb", "somewhere-else", cwd=repo)
    landed = merge_into_base(repo, "agentq/1", "main")
    assert landed.ok is False
    assert "somewhere-else" in landed.reason
    assert current_branch(repo) == "somewhere-else"


def test_without_a_remote_a_pr_is_impossible_and_says_so(repo: Path, tmp_path: Path) -> None:
    assert remote_url(repo) == ""
    worktree = Worktree(repo, tmp_path / "wt")
    worktree.reset("agentq/1", "main")
    (worktree.path / "one.txt").write_text("done", encoding="utf-8")
    worktree.commit_all("work")
    run("checkout", "-qb", "elsewhere", cwd=repo)

    # auto: the merge is impossible here, and so is the fallback — the caller
    # must be told rather than left believing it landed.
    landed = land(repo, Task(id=1, text="x"), "agentq/1", "main", "auto", "summary", "review")
    assert landed == Landing(False, "", landed.reason)
    assert "pull request" in landed.reason


def test_pr_body_carries_the_task_and_the_review() -> None:
    body = pr_body(Task(id=7, text="fix the header"), "moved the div", "VERDICT: APPROVE")
    assert "#7" in body and "fix the header" in body
    assert "moved the div" in body and "APPROVE" in body


def test_checks_report_the_failure_they_saw(repo: Path) -> None:
    passed, report = run_checks([Check("yes", ["true"]), Check("no", ["false"])], repo)
    assert passed is False
    assert "yes: PASS" in report and "no: FAIL" in report


def test_a_missing_tool_is_a_failure_not_a_skip(repo: Path) -> None:
    passed, report = run_checks([Check("ghost", ["definitely-not-installed"])], repo)
    assert passed is False
    assert "not found" in report


def test_no_checks_is_not_a_failure(repo: Path) -> None:
    passed, report = run_checks([], repo)
    assert passed is True
    assert "no checks" in report
