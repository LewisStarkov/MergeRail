"""Rejected Git transfers must preserve refs and the operator's checkout."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from mergerail.execution import sync
from tests.conftest import run


@pytest.mark.parametrize("revision", ["", "--all", "HEAD\x00other"])
def test_resolve_rejects_option_and_nul_injection(repo: Path, revision: str) -> None:
    with pytest.raises(sync.SyncError, match="local Git commit"):
        sync.resolve_commit(repo, revision)


@pytest.mark.parametrize("revisions", [(), ("HEAD",), ("a" * 40, "bad")])
def test_snapshot_requires_resolved_commits(repo: Path, revisions: tuple[str, ...]) -> None:
    before = run("show-ref", cwd=repo)
    with pytest.raises(sync.SyncError, match="resolved commit ids"):
        sync.create_bundle(repo, revisions, max_bytes=1024)
    assert run("show-ref", cwd=repo) == before


def test_oversized_snapshot_removes_temporary_refs(repo: Path) -> None:
    sha = sync.resolve_commit(repo, "HEAD")
    before = run("show-ref", cwd=repo)
    with pytest.raises(sync.SyncError, match="snapshot exceeds"):
        sync.create_bundle(repo, (sha,), max_bytes=16)
    assert run("show-ref", cwd=repo) == before
    assert run("status", "--porcelain", cwd=repo) == ""


@pytest.mark.parametrize(
    ("header", "message"),
    [
        (b"not a bundle\n\nPACK", "header is missing"),
        (b"# v2 git bundle\nmalformed\n\nPACK", "malformed advertised"),
        (b"# v2 git bundle\n" + b"a" * 40 + b" refs/heads/other\n\nPACK", "unexpected"),
        (b"# v2 git bundle\n" + b"a" * 40 + b" refs/mergerail-docker/\xff\n\nPACK", "ASCII"),
    ],
)
def test_input_bundle_rejects_unrecognized_header(header: bytes, message: str) -> None:
    with pytest.raises(sync.SyncError, match=message):
        sync.bundle_refs(header)


def test_input_bundle_deduplicates_same_commit_and_skips_prerequisites() -> None:
    sha = "a" * 40
    header = (
        f"# v2 git bundle\n-{sha} prerequisite\n"
        f"{sha} refs/mergerail-docker/first\n{sha} refs/mergerail-docker/second\n\nPACK"
    ).encode()
    assert sync.bundle_refs(header) == {sha: "refs/mergerail-docker/first"}


@pytest.mark.parametrize(
    ("header", "expected", "message"),
    [
        (b"", "bad", "not a commit id"),
        (b"bad\n\nPACK", "a" * 40, "header is missing"),
        (b"# v2 git bundle\nmalformed\n\nPACK", "a" * 40, "malformed advertised"),
        (b"# v2 git bundle\n\xff refs/heads/mergerail-result\n\nPACK", "a" * 40, "ASCII"),
    ],
)
def test_result_bundle_rejects_malformed_header(header: bytes, expected: str, message: str) -> None:
    with pytest.raises(sync.SyncError, match=message):
        sync.validate_bundle_header(header, expected)


def _bundle(repo: Path, sha: str, target: Path) -> bytes:
    run("update-ref", "refs/heads/mergerail-result", sha, cwd=repo)
    run("bundle", "create", str(target), "refs/heads/mergerail-result", cwd=repo)
    return target.read_bytes()


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("too-large", "byte limit"),
        ("bad-base", "base commit id"),
        ("bad-branch", "branch name"),
        ("bad-pack", "pack data"),
        ("corrupt-pack", "strict validation"),
    ],
)
def test_rejected_result_never_creates_delivery_ref(
    repo: Path, tmp_path: Path, bad: str, message: str
) -> None:
    sha = sync.resolve_commit(repo, "HEAD")
    bundle = _bundle(repo, sha, tmp_path / "result.bundle")
    limit = 1 if bad == "too-large" else len(bundle)
    branch = "bad..branch" if bad == "bad-branch" else "mergerail-candidate/rejected"
    if bad in {"bad-pack", "corrupt-pack"}:
        header = bundle.partition(b"\n\n")[0]
        bundle = header + b"\n\n" + (b"missing" if bad == "bad-pack" else b"PACKcorrupt")
    before = run("show-ref", cwd=repo)
    with pytest.raises(sync.SyncError, match=message):
        sync.import_result_bundle(
            repo,
            bundle,
            expected_sha=sha,
            base_sha="bad" if bad == "bad-base" else sha,
            branch=branch,
            max_bytes=limit,
        )
    assert run("show-ref", cwd=repo) == before
    assert (repo / "README.md").read_text() == "# project\n"


def test_result_cannot_replace_a_newer_durable_checkpoint(repo: Path, tmp_path: Path) -> None:
    base = sync.resolve_commit(repo, "HEAD")
    (repo / "README.md").write_text("first checkpoint\n")
    run("commit", "-qam", "first", cwd=repo)
    first = sync.resolve_commit(repo, "HEAD")
    old_bundle = _bundle(repo, first, tmp_path / "old.bundle")
    (repo / "README.md").write_text("newer checkpoint\n")
    run("commit", "-qam", "second", cwd=repo)
    second = sync.resolve_commit(repo, "HEAD")
    sync.update_ref_cas(repo, "mergerail-candidate/durable", second, None)
    with pytest.raises(sync.SyncError, match="current durable checkpoint"):
        sync.import_result_bundle(
            repo,
            old_bundle,
            expected_sha=first,
            base_sha=base,
            branch="mergerail-candidate/durable",
        )
    assert sync.resolve_commit(repo, "mergerail-candidate/durable") == second
    assert (repo / "README.md").read_text() == "newer checkpoint\n"


def test_result_requires_frozen_base_ancestry(repo: Path, tmp_path: Path) -> None:
    base = sync.resolve_commit(repo, "HEAD")
    run("checkout", "--orphan", "unrelated", cwd=repo)
    run("commit", "-qm", "unrelated root", cwd=repo)
    unrelated = sync.resolve_commit(repo, "HEAD")
    bundle = _bundle(repo, unrelated, tmp_path / "unrelated.bundle")
    with pytest.raises(sync.SyncError, match="frozen base"):
        sync.import_result_bundle(
            repo, bundle, expected_sha=unrelated, base_sha=base, branch="candidate"
        )
    assert run("branch", "--list", "candidate", cwd=repo) == ""


@pytest.mark.parametrize(
    ("branch", "new", "old", "message"),
    [
        ("candidate", "bad", None, "new ref target"),
        ("bad..branch", "a" * 40, None, "branch name"),
        ("candidate", "a" * 40, "bad", "expected old ref"),
    ],
)
def test_cas_validates_every_ref_component(
    repo: Path, branch: str, new: str, old: str | None, message: str
) -> None:
    with pytest.raises(sync.SyncError, match=message):
        sync.update_ref_cas(repo, branch, new, old)
    assert run("branch", "--list", "candidate", cwd=repo) == ""
    assert not sync.is_ancestor(repo, "invalid", new)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        (b"\xff", "UTF-8"),
        (b"core.unexpected.key", "unsupported name"),
        (b"filter..clean", "unsafe subsection"),
        (b"filter.name=override.clean", "unsafe subsection"),
        (b"filter.name\nnewline.clean", "unsafe subsection"),
        (b"filter.name.bad_key", "unsupported key"),
    ],
)
def test_local_driver_names_cannot_inject_git_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: bytes, message: str
) -> None:
    monkeypatch.setattr(sync, "_run_bounded", lambda *_args, **_kwargs: (0, name + b"\0", b""))
    with pytest.raises(sync.SyncError, match=message):
        sync.host_git_command(tmp_path, "status")


@pytest.mark.parametrize("failure", ["status", "exception"])
def test_cannot_inspect_drivers_fails_before_git_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def inspect(*_args: Any, **_kwargs: Any) -> tuple[int, bytes, bytes]:
        if failure == "exception":
            raise OSError("fixture inspection error")
        return 2, b"", b"invalid configuration"

    monkeypatch.setattr(sync, "_run_bounded", inspect)
    with pytest.raises(sync.SyncError, match="cannot inspect"):
        sync.host_git(tmp_path, "status")


def test_bounded_git_stops_child_on_deadline(tmp_path: Path) -> None:
    with pytest.raises(sync.SyncError, match="timed out"):
        sync._run_bounded(
            [sys.executable, "-I", "-c", "import time;time.sleep(30)"],
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=0.1,
        )


def test_bounded_git_reports_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(sync.SyncError, match="cannot run host git"):
        sync._run_bounded(
            [str(tmp_path / "missing")], cwd=tmp_path, env=dict(os.environ), timeout=1
        )


def test_git_failure_preserves_stderr_and_unchecked_reads(repo: Path) -> None:
    with pytest.raises(sync.SyncError, match="failed:"):
        sync.host_git(repo, "show", "missing-ref")
    assert sync.host_git(repo, "show", "missing-ref", check=False) == ""


def test_git_environment_drops_all_inherited_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_EXEC_PATH", "/fixture/untrusted")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "injected")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/fixture/objects")
    environment = sync._git_environment()
    assert "GIT_EXEC_PATH" not in environment
    assert "GIT_CONFIG_PARAMETERS" not in environment
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in environment
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
