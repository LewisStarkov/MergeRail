"""The host applies a sandbox candidate only after integration checks pass."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from mergerail import delivery
from mergerail.detect import Check
from mergerail.execution import delivery as docker_delivery
from mergerail.execution.docker import DockerExecution
from mergerail.tasks import DeliveryRecord, Task
from tests.conftest import run
from tests.test_docker_delivery import candidate
from tests.test_docker_delivery import pytestmark as pytestmark


class Sandbox:
    def __init__(self, state_dir: Path, candidate_sha: str, *, passed: bool = True) -> None:
        self.state_dir = state_dir
        self.candidate_sha = candidate_sha
        self.passed = passed
        self.merges: list[tuple[str, str, str]] = []
        self.checks: list[tuple[list[Check], str, frozenset[str]]] = []

    def merge_candidate(self, base: str, approved: str, branch: str) -> str:
        self.merges.append((base, approved, branch))
        return self.candidate_sha

    def run_checks(
        self, checks: list[Check], sha: str, *, allowed_failures: frozenset[str]
    ) -> tuple[bool, str]:
        self.checks.append((checks, sha, allowed_failures))
        return self.passed, "sandbox integration report"


def approved(tip: str) -> Task:
    return Task(
        id=1,
        text="fix project",
        delivery=DeliveryRecord(
            branch="candidate",
            commit=tip,
            base_branch="main",
            requested_mode="local",
            resolved_mode="merge",
        ),
    )


@pytest.mark.parametrize("passed", [True, False])
def test_delivery_checks_the_exact_candidate_before_host_checkout(repo: Path, passed: bool) -> None:
    base, tip = candidate(repo)
    sandbox = Sandbox(repo / ".mergerail", tip, passed=passed)
    stages: list[str] = []
    checks = [Check("fixture checks", ["operator-installed-tool"])]
    allowed = frozenset({"known baseline failure"})

    result = docker_delivery.recover_delivery(
        cast(DockerExecution, sandbox),
        repo,
        approved(tip),
        checks,
        allowed,
        on_stage=stages.append,
    )

    assert sandbox.merges == [(base, tip, "candidate")]
    assert sandbox.checks == [(checks, tip, allowed)]
    assert result.ok is passed
    if passed:
        assert stages == ["integration", "integration_checks", "merge"]
        assert run("rev-parse", "HEAD", cwd=repo) == tip
        assert (repo / "new.txt").read_text() == "result\n"
    else:
        assert stages == ["integration", "integration_checks"]
        assert result.stage == "integration_checks"
        assert "sandbox integration report" in result.reason
        assert run("rev-parse", "HEAD", cwd=repo) == base
        assert not (repo / "new.txt").exists()
    assert run("rev-parse", "candidate", cwd=repo) == tip


def test_retry_of_already_delivered_commit_is_idempotent(repo: Path) -> None:
    _base, tip = candidate(repo)
    run("merge", "--ff-only", "candidate", cwd=repo)
    sandbox = Sandbox(repo / ".mergerail", tip)

    result = docker_delivery.recover_delivery(
        cast(DockerExecution, sandbox), repo, approved(tip), [], frozenset()
    )

    assert result.ok and result.outcome == delivery.LOCAL_MERGE
    assert not sandbox.merges and not sandbox.checks
    assert run("rev-parse", "HEAD", cwd=repo) == tip


@pytest.mark.parametrize("blocked", ["dirty", "other-branch", "missing-record"])
def test_delivery_preconditions_preserve_checkout_and_skip_sandbox(
    repo: Path, blocked: str
) -> None:
    base, tip = candidate(repo)
    task = approved(tip)
    if blocked == "dirty":
        (repo / "README.md").write_text("operator edits\n")
    elif blocked == "other-branch":
        run("checkout", "-qb", "operator-work", cwd=repo)
    else:
        task.delivery.commit = ""
    sandbox = Sandbox(repo / ".mergerail", tip)

    result = docker_delivery.recover_delivery(
        cast(DockerExecution, sandbox), repo, task, [], frozenset()
    )

    assert not result.ok
    assert not sandbox.merges and not sandbox.checks
    assert run("rev-parse", "HEAD", cwd=repo) == base
    assert not (repo / "new.txt").exists()
    if blocked == "dirty":
        assert (repo / "README.md").read_text() == "operator edits\n"


def test_pr_delivery_forwards_exact_approval_with_host_hook_protection(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base, tip = candidate(repo)
    task = approved(tip)
    task.delivery.resolved_mode = "pr"
    sandbox = Sandbox(repo / ".mergerail", tip)
    calls: list[dict[str, Any]] = []

    def open_pr(*_args: Any, **kwargs: Any) -> delivery.Landing:
        calls.append(kwargs)
        return delivery.Landing(True, "pr", url="https://example.invalid/pr/1")

    monkeypatch.setattr(delivery, "open_pull_request", open_pr)
    result = docker_delivery.recover_delivery(
        cast(DockerExecution, sandbox), repo, task, [], frozenset()
    )

    assert result.ok and result.url == "https://example.invalid/pr/1"
    assert calls[0]["commit"] == tip
    assert calls[0]["sandboxed"] is True
    assert not sandbox.merges and not sandbox.checks
