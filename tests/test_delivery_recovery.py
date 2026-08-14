from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import mergerail.delivery as delivery
from mergerail.gitctl import Worktree, git
from mergerail.tasks import DeliveryRecord, Task


def approved_task(*, commit: str, branch: str = "mergerail/1/a1", mode: str = "local") -> Task:
    return Task(
        id=1,
        text="ship it",
        branch=branch,
        approved_sha=commit,
        delivery=DeliveryRecord(
            requested_mode=mode,
            resolved_mode=mode,
            status="running",
            stage="merge",
            base_branch="main",
            branch=branch,
            commit=commit,
            attempts=1,
        ),
    )


def test_local_delivery_recovers_after_merge_completed(repo: Path, tmp_path: Path) -> None:
    worktree = Worktree(repo, tmp_path / "wt")
    worktree.reset("mergerail/1/a1", "main")
    (worktree.path / "one.txt").write_text("done", encoding="utf-8")
    worktree.commit_all("work")
    approved = worktree.head()
    first = delivery.merge_into_base(repo, "mergerail/1/a1", "main", commit=approved)
    assert first.ok

    # Simulate cleanup plus a crash before TaskStore.complete_delivery(). The
    # immutable SHA, not the task branch, is enough to recognize success.
    worktree.detach("main")
    git("branch", "-D", "mergerail/1/a1", cwd=repo)
    recovered = delivery.recover_delivery(repo, approved_task(commit=approved))
    assert recovered.ok
    assert recovered.outcome == delivery.LOCAL_MERGE


def test_pr_recovery_accepts_an_existing_pr_without_repush(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git("remote", "add", "origin", "https://example.invalid/project.git", cwd=repo)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/gh")
    approved = git("rev-parse", "HEAD", cwd=repo)
    monkeypatch.setattr(
        delivery,
        "_existing_pr",
        lambda _root, _branch: ("https://example.test/pr/1", approved),
    )
    task = approved_task(commit=approved, mode="pr")

    recovered = delivery.recover_delivery(repo, task)
    assert recovered.ok
    assert recovered.url == "https://example.test/pr/1"
    assert recovered.outcome == delivery.PULL_REQUEST


def test_pr_recovery_blocks_if_the_remote_head_moved(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git("remote", "add", "origin", "https://example.invalid/project.git", cwd=repo)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        delivery,
        "_existing_pr",
        lambda _root, _branch: ("https://example.test/pr/1", "different-sha"),
    )
    approved = git("rev-parse", "HEAD", cwd=repo)

    recovered = delivery.recover_delivery(repo, approved_task(commit=approved, mode="pr"))
    assert not recovered.ok
    assert recovered.stage == "preflight"
    assert "moved after approval" in recovered.reason


def test_auto_with_origin_does_not_silently_merge_when_gh_is_missing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git("remote", "add", "origin", "https://example.invalid/project.git", cwd=repo)
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert delivery.resolve_mode(repo, delivery.AUTO) == delivery.PR
    landed = delivery.land(
        repo,
        Task(id=1, text="x"),
        "main",
        "main",
        delivery.AUTO,
        "summary",
        "review",
        commit=git("rev-parse", "HEAD", cwd=repo),
    )
    assert landed.ok is False
    assert landed.stage == "preflight"
    assert "gh" in landed.reason


def test_pull_request_pushes_then_creates_with_persisted_stages(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages: list[str] = []
    calls: list[list[str]] = []
    monkeypatch.setattr(delivery, "can_open_pr", lambda _root: True)
    monkeypatch.setattr(delivery, "commit_exists", lambda _root, _target: True)
    monkeypatch.setattr(delivery, "ref_exists", lambda _root, _ref: False)
    monkeypatch.setattr(delivery, "_existing_pr", lambda _root, _branch: ("", ""))

    def run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        stdout = "https://example.test/pr/7\n" if command[:3] == ["gh", "pr", "create"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", run_command)
    landed = delivery.open_pull_request(
        repo,
        Task(id=7, text="ship safely"),
        "mergerail/7/a1",
        "main",
        "reviewed",
        commit="approved-sha",
        on_stage=stages.append,
    )

    assert landed.ok and landed.url == "https://example.test/pr/7"
    assert stages == ["push", "create_pr"]
    assert calls[0][:4] == ["git", "push", "--set-upstream", "origin"]
    assert calls[1][:3] == ["gh", "pr", "create"]


def test_pull_request_reports_a_failed_push(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delivery, "can_open_pr", lambda _root: True)
    monkeypatch.setattr(delivery, "commit_exists", lambda _root, _target: True)
    monkeypatch.setattr(delivery, "ref_exists", lambda _root, _ref: False)
    monkeypatch.setattr(delivery, "_existing_pr", lambda _root, _branch: ("", ""))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="remote denied the push"
        ),
    )

    landed = delivery.open_pull_request(
        repo,
        Task(id=8, text="ship"),
        "mergerail/8/a1",
        "main",
        "reviewed",
        commit="approved-sha",
    )

    assert not landed.ok and landed.stage == "push"
    assert "remote denied" in landed.reason


def test_pull_request_recovers_if_pr_appears_after_push(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delivery, "can_open_pr", lambda _root: True)
    monkeypatch.setattr(delivery, "commit_exists", lambda _root, _target: True)
    monkeypatch.setattr(delivery, "ref_exists", lambda _root, _ref: False)
    responses = iter([("", ""), ("https://example.test/pr/8", "approved-sha")])
    monkeypatch.setattr(delivery, "_existing_pr", lambda _root, _branch: next(responses))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout="", stderr=""
        ),
    )

    landed = delivery.open_pull_request(
        repo,
        Task(id=8, text="ship"),
        "mergerail/8/a1",
        "main",
        "reviewed",
        commit="approved-sha",
    )

    assert landed.ok and landed.url == "https://example.test/pr/8"


def test_pull_request_treats_create_race_as_success(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delivery, "can_open_pr", lambda _root: True)
    monkeypatch.setattr(delivery, "commit_exists", lambda _root, _target: True)
    monkeypatch.setattr(delivery, "ref_exists", lambda _root, _ref: False)
    responses = iter(
        [
            ("", ""),
            ("", ""),
            ("https://example.test/pr/9", "approved-sha"),
        ]
    )
    monkeypatch.setattr(delivery, "_existing_pr", lambda _root, _branch: next(responses))

    def run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="already exists")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run_command)
    landed = delivery.open_pull_request(
        repo,
        Task(id=9, text="ship"),
        "mergerail/9/a1",
        "main",
        "reviewed",
        commit="approved-sha",
    )

    assert landed.ok and landed.url == "https://example.test/pr/9"


def test_delivery_preflight_rejects_invalid_inputs(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown delivery mode"):
        delivery.resolve_mode(repo, "teleport")

    worktree = Worktree(repo, tmp_path / "wt-preflight")
    worktree.reset("mergerail/9/a1", "main")
    (worktree.path / "change.txt").write_text("change", encoding="utf-8")
    worktree.commit_all("change")
    missing_path = delivery.merge_into_base(
        repo,
        "mergerail/9/a1",
        "main",
        commit=worktree.head(),
        validate=lambda _path: (True, "green"),
    )
    assert not missing_path.ok and missing_path.stage == "preflight"
    assert "integration path" in missing_path.reason

    incomplete = Task(id=9, text="incomplete")
    recovered = delivery.recover_delivery(repo, incomplete)
    assert not recovered.ok and "missing branch" in recovered.reason
