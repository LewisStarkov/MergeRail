"""Exercise worker snapshot transitions against bounded real Git bundles."""

from __future__ import annotations

import io
import os
import shlex
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from mergerail.execution import sync, worker
from tests.conftest import run


@dataclass(frozen=True)
class WorkerPaths:
    repo: Path
    home: Path
    snapshot: Path
    workspace: Path
    result: Path
    session: Path


@pytest.fixture
def worker_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkerPaths:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    sandbox = tmp_path / "sandbox"
    paths = WorkerPaths(
        repo=sandbox / "work" / "repo",
        home=sandbox / "work" / "home",
        snapshot=sandbox / "tmp" / "snapshot.bundle",
        workspace=sandbox / "tmp" / "workspace.tar",
        result=sandbox / "tmp" / "result.bundle",
        session=sandbox / "tmp" / "home.tar",
    )
    fixed_paths = {
        "/tmp/snapshot.bundle": paths.snapshot,
        "/tmp/workspace.tar": paths.workspace,
        "/tmp/result.bundle": paths.result,
        "/tmp/home.tar": paths.session,
    }
    path_type = Path

    def sandbox_path(value: os.PathLike[str] | str) -> Path:
        raw = os.fspath(value)
        if raw in fixed_paths:
            return fixed_paths[raw]
        return path_type(value)

    monkeypatch.setattr(worker, "Path", sandbox_path)
    monkeypatch.setattr(worker, "REPO", paths.repo)
    monkeypatch.setattr(worker, "HOME", paths.home)
    return paths


def _commit_file(repo: Path, name: str, contents: str, message: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    run("add", "-A", cwd=repo)
    run("commit", "-qm", message, cwd=repo)
    return run("rev-parse", "HEAD", cwd=repo)


def _request(
    worker_paths: WorkerPaths,
    repo: Path,
    base: str,
    result: str,
    *,
    allow_diverged: bool = False,
    protected_paths: list[str] | None = None,
) -> dict[str, object]:
    bundle = sync.create_bundle(repo, (base, result), max_bytes=8 * 1024 * 1024)
    worker_paths.snapshot.parent.mkdir(parents=True, exist_ok=True)
    worker_paths.snapshot.write_bytes(bundle)
    return {
        "base_sha": base,
        "result_sha": result,
        "branch": "mergerail-candidate/test",
        "bundle_refs": sync.bundle_refs(bundle),
        "max_bundle_bytes": 8 * 1024 * 1024,
        "max_workspace_bytes": 8 * 1024 * 1024,
        "allow_diverged": allow_diverged,
        "protected_paths": protected_paths or [],
    }


def _tar(path: Path, entries: list[tuple[str, bytes | str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as archive:
        for name, value in entries:
            member = tarfile.TarInfo(name)
            if isinstance(value, str):
                member.type = tarfile.SYMTYPE
                member.linkname = value
                archive.addfile(member)
            else:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))


@pytest.mark.skipif(os.name == "nt", reason="worker preparation requires POSIX user IDs")
def test_snapshot_reset_and_prepare_use_only_named_commits(
    repo: Path, worker_paths: WorkerPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = run("rev-parse", "HEAD", cwd=repo)
    result = _commit_file(repo, "src/app.py", "value = 2\n", "candidate")
    request = _request(worker_paths, repo, base, result)

    assert worker._reset_from_bundle(request) == (base, result)
    assert run("branch", "--show-current", cwd=worker_paths.repo) == "mergerail-candidate/test"
    assert run("rev-parse", "HEAD", cwd=worker_paths.repo) == result
    assert (
        worker._git(worker_paths.repo, "config", "--local", "--get", "core.hooksPath", check=False)
        == ""
    )

    monkeypatch.setattr(os, "geteuid", lambda: 65534, raising=False)
    worker._prepare_owner("fixer", read_only=False)
    assert worker_paths.home.is_dir()
    assert os.access(worker_paths.repo, os.W_OK)


def test_restore_home_keeps_task_files_but_excludes_opencode_config(
    worker_paths: WorkerPaths,
) -> None:
    _tar(
        worker_paths.session,
        [
            ("session.json", b'{"turn": 3}'),
            (".opencode/opencode.json", b'{"plugin": ["unexpected"]}'),
            (".codex/auth.json", b'{"tokens": "unexpected"}'),
            (".codex/config.toml", b'model_provider="unexpected"'),
            (".codex/sessions/thread.jsonl", b'{"turn": 3}'),
        ],
    )

    worker._restore_home({"restore_home": True, "role": "fixer", "max_bundle_bytes": 64 * 1024})

    assert (worker_paths.home / "session.json").read_bytes() == b'{"turn": 3}'
    assert not (worker_paths.home / ".opencode").exists()
    assert not (worker_paths.home / ".codex/auth.json").exists()
    assert not (worker_paths.home / ".codex/config.toml").exists()
    assert (worker_paths.home / ".codex/sessions/thread.jsonl").read_bytes() == b'{"turn": 3}'


def test_restore_home_rejects_outward_symlink(worker_paths: WorkerPaths) -> None:
    _tar(worker_paths.session, [("links/out", "../../outside")])

    with pytest.raises(worker.WorkerError, match="symlink"):
        worker._restore_home({"restore_home": True, "role": "fixer", "max_bundle_bytes": 64 * 1024})
    assert not (worker_paths.home.parent.parent / "outside").exists()


@pytest.mark.skipif(os.name == "nt", reason="Git filter and hook fixtures use a POSIX shell")
def test_checkpoint_discards_forged_filter_and_hook_configuration(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / ".gitattributes").write_text("payload filter=fixture\n", encoding="utf-8")
    base = _commit_file(repo, "payload", "base\n", "base with filter attribute")
    marker = tmp_path / "filter-ran"
    script = (
        "import pathlib,sys; "
        f"pathlib.Path({str(marker)!r}).write_text('ran'); "
        "sys.stdout.buffer.write(sys.stdin.buffer.read())"
    )
    filter_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    run("config", "filter.fixture.clean", filter_command, cwd=repo)
    run("config", "filter.fixture.smudge", filter_command, cwd=repo)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\n", encoding="utf-8")
    hook.chmod(0o755)
    monkeypatch.setattr(worker, "REPO", repo)
    (repo / "payload").write_text("changed\n", encoding="utf-8")

    head = worker._commit_workspace(base)

    assert head != base
    assert run("show", f"{head}:payload", cwd=repo) == "changed"
    assert not marker.exists()
    assert "filter.fixture.clean" not in run("config", "--local", "--list", cwd=repo)
    assert not hook.exists()


def test_recovery_exports_checkpoint_and_ignores_git_metadata_from_archive(
    repo: Path, worker_paths: WorkerPaths
) -> None:
    base = run("rev-parse", "HEAD", cwd=repo)
    request = _request(worker_paths, repo, base, base, protected_paths=[".mergerail"])
    _tar(
        worker_paths.workspace,
        [
            ("README.md", b"# recovered\n"),
            (".git/config", b"[core]\n hooksPath = /tmp/untrusted-hooks\n"),
            (".git/hooks/pre-commit", b"#!/bin/sh\ntouch never-run\n"),
        ],
    )

    recovered = worker._recover(request)

    head = str(recovered["head"])
    assert worker._git(worker_paths.repo, "show", f"{head}:README.md") == "# recovered"
    assert worker._git(worker_paths.repo, "rev-parse", "HEAD") == head
    sync.validate_bundle_header(worker_paths.result.read_bytes(), head)
    assert "untrusted-hooks" not in (worker_paths.repo / ".git" / "config").read_text()
    assert not (worker_paths.repo / "never-run").exists()


def test_recovery_rejects_merge_rail_state_before_export(
    repo: Path, worker_paths: WorkerPaths
) -> None:
    base = run("rev-parse", "HEAD", cwd=repo)
    request = _request(worker_paths, repo, base, base, protected_paths=[".mergerail"])
    _tar(worker_paths.workspace, [(".mergerail/tasks.json", b"[]")])

    with pytest.raises(worker.WorkerError, match="controller state"):
        worker._recover(request)

    assert not worker_paths.result.exists()
    assert run("rev-parse", "HEAD", cwd=repo) == base


@pytest.mark.parametrize("mutation", ["change", "delete", "rename"])
def test_checkpoint_cannot_disable_or_replace_operator_config(
    repo: Path, worker_paths: WorkerPaths, mutation: str
) -> None:
    base = _commit_file(repo, "mergerail.toml", '[execution]\nbackend = "docker"\n', "policy")
    worker._reset_from_bundle(_request(worker_paths, repo, base, base))
    config = worker_paths.repo / "mergerail.toml"
    if mutation == "change":
        config.write_text("# execution disabled\n")
    elif mutation == "delete":
        config.unlink()
    else:
        config.rename(worker_paths.repo / "renamed.toml")
    with pytest.raises(worker.WorkerError, match="operator-owned"):
        worker._commit_workspace(base)
    assert run("show", f"{base}:mergerail.toml", cwd=repo) == '[execution]\nbackend = "docker"'


@pytest.mark.parametrize("name", ["mergerail.toml", "MERGERAIL.TOML"])
def test_checkpoint_cannot_introduce_operator_config(
    repo: Path, worker_paths: WorkerPaths, name: str
) -> None:
    base = run("rev-parse", "HEAD", cwd=repo)
    worker._reset_from_bundle(_request(worker_paths, repo, base, base))
    (worker_paths.repo / name).write_text("# injected config\n")
    with pytest.raises(worker.WorkerError, match="operator-owned"):
        worker._commit_workspace(base)


@pytest.mark.parametrize("mode", ["100755", "120000"])
def test_config_guard_rejects_changed_git_mode_or_symlink_type(
    repo: Path, worker_paths: WorkerPaths, mode: str
) -> None:
    base = _commit_file(repo, "mergerail.toml", "# operator settings\n", "config")
    worker._reset_from_bundle(_request(worker_paths, repo, base, base))
    blob = run("rev-parse", "HEAD:mergerail.toml", cwd=worker_paths.repo)
    run("update-index", "--cacheinfo", mode, blob, "mergerail.toml", cwd=worker_paths.repo)
    run("commit", "-qm", "change config entry", cwd=worker_paths.repo)
    result = run("rev-parse", "HEAD", cwd=worker_paths.repo)
    with pytest.raises(worker.WorkerError, match="operator-owned"):
        worker._validate_operator_config(base, result)


def test_fast_forward_candidate_exports_the_approved_sha(
    repo: Path, worker_paths: WorkerPaths
) -> None:
    base = run("rev-parse", "HEAD", cwd=repo)
    result = _commit_file(repo, "fix.py", "ok = True\n", "approved")
    request = _request(worker_paths, repo, base, result, allow_diverged=True)

    candidate = worker._merge_candidate(request)

    assert candidate["head"] == result
    sync.validate_bundle_header(worker_paths.result.read_bytes(), result)


def test_advanced_base_candidate_has_both_requested_parents(
    repo: Path, worker_paths: WorkerPaths
) -> None:
    common = _commit_file(repo, "mergerail.toml", "max_rounds = 2\n", "operator config")
    run("checkout", "-b", "agent", common, cwd=repo)
    result = _commit_file(repo, "agent.txt", "agent\n", "agent change")
    run("checkout", "-b", "advanced", common, cwd=repo)
    (repo / "mergerail.toml").write_text("max_rounds = 4\n")
    base = _commit_file(repo, "base.txt", "base\n", "advanced base")
    request = _request(worker_paths, repo, base, result, allow_diverged=True)

    candidate = worker._merge_candidate(request)

    head = str(candidate["head"])
    parents = set(worker._git(worker_paths.repo, "show", "-s", "--format=%P", head).split())
    assert parents == {base, result}
    assert worker._git(worker_paths.repo, "show", f"{head}:agent.txt") == "agent"
    assert worker._git(worker_paths.repo, "show", f"{head}:base.txt") == "base"
    assert worker._git(worker_paths.repo, "show", f"{head}:mergerail.toml") == "max_rounds = 4"
    sync.validate_bundle_header(worker_paths.result.read_bytes(), head)


def test_conflicting_candidate_fails_without_exporting_partial_merge(
    repo: Path, worker_paths: WorkerPaths
) -> None:
    common = run("rev-parse", "HEAD", cwd=repo)
    (repo / "shared.txt").write_text("common\n", encoding="utf-8")
    run("add", "shared.txt", cwd=repo)
    run("commit", "-qm", "shared base", cwd=repo)
    common = run("rev-parse", "HEAD", cwd=repo)
    run("checkout", "-b", "agent", common, cwd=repo)
    result = _commit_file(repo, "shared.txt", "agent\n", "agent edit")
    run("checkout", "-b", "advanced", common, cwd=repo)
    base = _commit_file(repo, "shared.txt", "base\n", "base edit")
    request = _request(worker_paths, repo, base, result, allow_diverged=True)

    with pytest.raises(worker.WorkerError, match="sandbox merge failed"):
        worker._merge_candidate(request)

    assert worker._git(worker_paths.repo, "rev-parse", "HEAD") == base
    assert not worker._git(worker_paths.repo, "status", "--porcelain")
    assert not worker_paths.result.exists()
