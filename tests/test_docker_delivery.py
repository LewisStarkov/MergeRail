from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mergerail.execution.delivery import (
    _apply_candidate,
    _record_ref_locks,
    _recover_apply,
    _transaction,
)
from tests.conftest import run

_git_version = re.search(r"(\d+)\.(\d+)", run("--version", cwd=Path.cwd()))
pytestmark = pytest.mark.skipif(
    _git_version is None or tuple(map(int, _git_version.groups())) < (2, 48),
    reason="Docker checkout transactions require host Git 2.48 or newer",
)


def candidate(repo: Path) -> tuple[str, str]:
    base = run("rev-parse", "HEAD", cwd=repo)
    run("checkout", "-qb", "candidate", cwd=repo)
    (repo / "new.txt").write_text("result\n")
    run("add", "new.txt", cwd=repo)
    run("commit", "-qm", "candidate", cwd=repo)
    tip = run("rev-parse", "HEAD", cwd=repo)
    run("checkout", "-q", "main", cwd=repo)
    return base, tip


def test_apply_locks_head_and_index_and_advances_exact_sha(repo: Path) -> None:
    base, tip = candidate(repo)
    hook = repo / ".git" / "hooks" / "reference-transaction"
    hook.write_text('#!/bin/sh\ntouch "$PWD/host-hook-ran"\n')
    hook.chmod(0o755)
    _apply_candidate(repo, repo / ".mergerail", 1, "refs/heads/main", base, tip)
    assert run("rev-parse", "HEAD", cwd=repo) == tip
    assert (repo / "new.txt").read_text() == "result\n"
    assert run("diff", "--name-only", "HEAD", cwd=repo) == ""
    assert not (repo / "host-hook-ran").exists()
    assert not (repo / ".git/index.lock").exists()


def test_moved_branch_is_refused_before_checkout_changes(repo: Path) -> None:
    base, tip = candidate(repo)
    run("commit", "--allow-empty", "-qm", "base moved", cwd=repo)
    moved = run("rev-parse", "HEAD", cwd=repo)
    with pytest.raises(RuntimeError, match="changed"):
        _apply_candidate(repo, repo / ".mergerail", 1, "refs/heads/main", base, tip)
    assert run("rev-parse", "HEAD", cwd=repo) == moved
    assert not (repo / "new.txt").exists()


def test_checkout_on_other_branch_is_refused(repo: Path) -> None:
    base, tip = candidate(repo)
    run("checkout", "-qb", "user-work", cwd=repo)
    with pytest.raises(RuntimeError, match="changed"):
        _apply_candidate(repo, repo / ".mergerail", 1, "refs/heads/main", base, tip)
    assert run("branch", "--show-current", cwd=repo) == "user-work"
    assert not (repo / "new.txt").exists()


def test_existing_git_lock_is_preserved(repo: Path) -> None:
    base, tip = candidate(repo)
    lock = repo / ".git/index.lock"
    lock.write_text("user lock")
    with pytest.raises(FileExistsError):
        _apply_candidate(repo, repo / ".mergerail", 1, "refs/heads/main", base, tip)
    assert lock.read_text() == "user lock"


def test_untracked_collision_is_preserved(repo: Path) -> None:
    base, tip = candidate(repo)
    (repo / "new.txt").write_text("user file\n")
    state = repo / ".mergerail"
    with pytest.raises(RuntimeError, match="checkout refused"):
        _apply_candidate(repo, state, 1, "refs/heads/main", base, tip)
    assert (repo / "new.txt").read_text() == "user file\n"
    assert run("rev-parse", "HEAD", cwd=repo) == base
    _recover_apply(repo, state, 1)
    assert not (repo / ".git/index.lock").exists()


def test_interrupted_applied_index_can_finish_reference_transaction(repo: Path) -> None:
    base, tip = candidate(repo)
    state = repo / ".mergerail"
    state.mkdir()
    index = repo / ".git/index"
    temporary = index.with_name("mergerail-index-recovery")
    run("read-tree", "-u", "-m", base, tip, cwd=repo)
    index.with_name("index.lock").write_text("mergerail docker delivery 1\n")
    record = {
        "task": 1,
        "base_ref": "refs/heads/main",
        "base_sha": base,
        "candidate": tip,
        "index": str(index),
        "temporary": str(temporary),
    }
    (state / "docker-delivery-apply.json").write_text(json.dumps(record))
    _recover_apply(repo, state, 1)
    assert run("rev-parse", "HEAD", cwd=repo) == tip
    assert (repo / "new.txt").read_text() == "result\n"
    assert not index.with_name("index.lock").exists()
    assert not (state / "docker-delivery-apply.json").exists()


def test_interrupted_prepared_transaction_releases_only_recorded_locks(repo: Path) -> None:
    base, tip = candidate(repo)
    state = repo / ".mergerail"
    state.mkdir()
    index = repo / ".git/index"
    temporary = index.with_name("mergerail-index-recovery")
    temporary.write_bytes(index.read_bytes())
    index.with_name("index.lock").write_text("mergerail docker delivery 1\n")
    transaction = _transaction(repo, "refs/heads/main", base, tip)
    identities = _record_ref_locks(repo, "refs/heads/main")
    assert identities
    transaction.kill()
    transaction.communicate(timeout=5)
    record = {
        "task": 1,
        "base_ref": "refs/heads/main",
        "base_sha": base,
        "candidate": tip,
        "index": str(index),
        "temporary": str(temporary),
        "ref_locks": identities,
    }
    (state / "docker-delivery-apply.json").write_text(json.dumps(record))
    _recover_apply(repo, state, 1)
    assert run("rev-parse", "HEAD", cwd=repo) == base
    assert all(not Path(path).exists() for path in identities)
    assert not index.with_name("index.lock").exists()


def test_recovery_preserves_unknown_reference_lock(repo: Path) -> None:
    base, tip = candidate(repo)
    state = repo / ".mergerail"
    state.mkdir()
    index = repo / ".git/index"
    index.with_name("index.lock").write_text("mergerail docker delivery 1\n")
    unknown = repo / ".git/refs/heads/main.lock"
    unknown.write_text("another process\n")
    record = {
        "task": 1,
        "base_ref": "refs/heads/main",
        "base_sha": base,
        "candidate": tip,
        "index": str(index),
        "temporary": str(index.with_name("mergerail-index-recovery")),
        "ref_locks": {},
    }
    journal = state / "docker-delivery-apply.json"
    journal.write_text(json.dumps(record))
    with pytest.raises(RuntimeError, match="unknown ownership"):
        _recover_apply(repo, state, 1)
    assert unknown.read_text() == "another process\n"
    assert journal.exists()
    assert index.with_name("index.lock").exists()


def test_reftable_storage_is_refused_before_reference_prepare(tmp_path: Path) -> None:
    repository = tmp_path / "reftable"
    repository.mkdir()
    run("init", "-b", "main", "--ref-format=reftable", cwd=repository)
    run("config", "user.name", "Test", cwd=repository)
    run("config", "user.email", "test@example.invalid", cwd=repository)
    run("commit", "--allow-empty", "-qm", "base", cwd=repository)
    base = run("rev-parse", "HEAD", cwd=repository)
    with pytest.raises(RuntimeError, match="files reference storage"):
        _transaction(repository, "refs/heads/main", base, base)
    assert run("rev-parse", "HEAD", cwd=repository) == base
