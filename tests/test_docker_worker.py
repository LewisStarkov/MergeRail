"""Exercise the recovery boundary with real Git objects and hostile archives."""

from __future__ import annotations

import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

from mergerail.execution import worker
from tests.conftest import run


def archive(tmp_path: Path, entries: list[tuple[str, bytes | str | None]]) -> Path:
    path = tmp_path / "snapshot.tar"
    with tarfile.open(path, "w") as stream:
        for name, value in entries:
            item = tarfile.TarInfo(name)
            if value is None:
                item.type = tarfile.DIRTYPE
            elif isinstance(value, str):
                item.type = tarfile.SYMTYPE
                item.linkname = value
            else:
                item.size = len(value)
                item.mode = 0o755
            stream.addfile(item, io.BytesIO(value) if isinstance(value, bytes) else None)
    return path


def test_archive_preserves_bytes_modes_and_inward_links(tmp_path: Path) -> None:
    source = archive(tmp_path, [("dir", None), ("dir/tool", b"body"), ("link", "dir/tool")])
    target = tmp_path / "repo"
    worker._safe_extract_workspace(source, target, limit_bytes=100)
    assert (target / "dir/tool").read_bytes() == b"body"
    if sys.platform != "win32":
        assert os.access(target / "dir/tool", os.X_OK)
    assert (target / "link").is_symlink()
    assert os.readlink(target / "link") == "dir/tool"
    if sys.platform != "win32":
        assert (target / "link").read_bytes() == b"body"


@pytest.mark.parametrize("name", ["../outside", "/outside", "dir/../../outside"])
def test_archive_refuses_traversal(tmp_path: Path, name: str) -> None:
    source = archive(tmp_path, [(name, b"unsafe")])
    with pytest.raises(worker.WorkerError, match="unsafe path"):
        worker._safe_extract_workspace(source, tmp_path / "repo", limit_bytes=100)
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("target", ["/etc/passwd", "../outside", "C:outside", "..\\outside"])
def test_archive_refuses_escaping_links(tmp_path: Path, target: str) -> None:
    source = archive(tmp_path, [("link", target)])
    with pytest.raises(worker.WorkerError, match="symlink"):
        worker._safe_extract_workspace(source, tmp_path / "repo", limit_bytes=100)


def test_archive_cannot_supply_git_metadata(tmp_path: Path) -> None:
    source = archive(tmp_path, [(".git/config", b"malicious"), ("source", b"safe")])
    target = tmp_path / "repo"
    worker._safe_extract_workspace(source, target, limit_bytes=100)
    assert not (target / ".git").exists()
    assert (target / "source").read_bytes() == b"safe"


@pytest.mark.parametrize(
    "entries",
    [
        [("a", b"a"), ("a", b"b")],
        [("A", b"a"), ("a", b"b")],
        [("é", b"a"), ("e\u0301", b"b")],
    ],
)
def test_archive_refuses_duplicate_normalized_paths(
    tmp_path: Path, entries: list[tuple[str, bytes | str | None]]
) -> None:
    source = archive(tmp_path, entries)
    with pytest.raises(worker.WorkerError, match="duplicate normalized"):
        worker._safe_extract_workspace(source, tmp_path / "repo", limit_bytes=100)


def test_archive_expansion_is_bounded(tmp_path: Path) -> None:
    source = archive(tmp_path, [("large", b"a" * 101)])
    with pytest.raises(worker.WorkerError, match="expands"):
        worker._safe_extract_workspace(source, tmp_path / "repo", limit_bytes=100)


def test_archive_rejects_hardlinks_and_devices(tmp_path: Path) -> None:
    for kind in (tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
        source = tmp_path / "special.tar"
        with tarfile.open(source, "w") as stream:
            item = tarfile.TarInfo("special")
            item.type = kind
            item.linkname = "source"
            stream.addfile(item)
        with pytest.raises(worker.WorkerError, match="hard link or special"):
            worker._safe_extract_workspace(source, tmp_path / "repo", limit_bytes=100)


def test_archive_does_not_follow_preexisting_symlink(tmp_path: Path) -> None:
    source = archive(tmp_path, [("alias/file", b"unsafe")])
    target = tmp_path / "repo"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises(worker.WorkerError, match="escapes"):
        worker._safe_extract_workspace(source, target, limit_bytes=100)
    assert not (outside / "file").exists()


@pytest.fixture
def sandbox(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(worker, "REPO", repo)
    return repo


def commit_file(repo: Path, name: str, value: str = "body") -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    run("add", "-f", "--", name, cwd=repo)
    run("commit", "-qm", "fixture", cwd=repo)
    return run("rev-parse", "HEAD", cwd=repo)


def commit_index_entry(repo: Path, name: str, value: str, mode: str = "100644") -> str:
    source = repo.parent / "git-blob-input"
    source.write_text(value)
    blob = run("hash-object", "-w", str(source), cwd=repo)
    # Build the hostile object directly: Windows Git refuses these paths in its index.
    tree_input = repo.parent / "git-tree-input"
    tree_input.write_bytes(mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(blob))
    tree = run("hash-object", "-t", "tree", "-w", str(tree_input), cwd=repo)
    return run("commit-tree", tree, "-p", "HEAD", "-m", "fixture tree", cwd=repo)


def test_tree_accepts_regular_source_and_internal_symlink(sandbox: Path) -> None:
    commit_file(sandbox, "source/tool")
    (sandbox / "link").symlink_to("source/tool")
    run("add", "link", cwd=sandbox)
    run("commit", "-qm", "link", cwd=sandbox)
    worker._validate_tree(run("rev-parse", "HEAD", cwd=sandbox), [".mergerail"])


@pytest.mark.parametrize("name", ["NUL.txt", "name.", "name:stream", "bad\\name", "bad\x01name"])
def test_tree_rejects_unsafe_checkout_paths(sandbox: Path, name: str) -> None:
    sha = commit_index_entry(sandbox, name, "unsafe path")
    with pytest.raises(worker.WorkerError, match="path"):
        worker._validate_tree(sha)


def test_tree_rejects_controller_state(sandbox: Path) -> None:
    sha = commit_file(sandbox, ".mergerail/tasks.json", "[]")
    with pytest.raises(worker.WorkerError, match="controller state"):
        worker._validate_tree(sha, [".mergerail"])


def test_tree_rejects_lfs_pointer(sandbox: Path) -> None:
    sha = commit_file(
        sandbox, "blob", "version https://git-lfs.github.com/spec/v1\noid sha256:abc\n"
    )
    with pytest.raises(worker.WorkerError, match="LFS pointer"):
        worker._validate_tree(sha)


def test_tree_rejects_submodule(sandbox: Path) -> None:
    sha = run("rev-parse", "HEAD", cwd=sandbox)
    run("update-index", "--add", "--cacheinfo", f"160000,{sha},module", cwd=sandbox)
    run("commit", "-qm", "gitlink", cwd=sandbox)
    with pytest.raises(worker.WorkerError, match="submodule"):
        worker._validate_tree(run("rev-parse", "HEAD", cwd=sandbox))


@pytest.mark.parametrize("target", ["/etc/passwd", "../outside", "dir/../../outside"])
def test_tree_rejects_escaping_source_link(sandbox: Path, target: str) -> None:
    sha = commit_index_entry(sandbox, "link", target, "120000")
    with pytest.raises(worker.WorkerError, match="outward symlink"):
        worker._validate_tree(sha)


def test_checkpoint_sanitizes_git_config_before_committing(sandbox: Path) -> None:
    base = run("rev-parse", "HEAD", cwd=sandbox)
    hook = sandbox / ".git/hooks/pre-commit"
    hook.write_text('#!/bin/sh\ntouch "$PWD/unsafe-hook"\n')
    hook.chmod(0o755)
    run("config", "core.fsmonitor", "touch unsafe-monitor", cwd=sandbox)
    (sandbox / "source").write_text("fix")
    sha = worker._commit_workspace(base, [".mergerail"])
    assert sha != base
    assert run("show", f"{sha}:source", cwd=sandbox) == "fix"
    assert not (sandbox / "unsafe-hook").exists()
    assert not (sandbox / "unsafe-monitor").exists()
    assert "fsmonitor" not in (sandbox / ".git/config").read_text()


@pytest.mark.parametrize(
    "checks",
    [
        None,
        [None],
        [{"name": 1}],
        [{"name": "bad", "command": []}],
        [{"name": "bad", "command": ["x\x00y"]}],
    ],
)
def test_check_protocol_rejects_malformed_commands(checks: object) -> None:
    with pytest.raises(worker.WorkerError):
        worker._run_checks({"checks": checks})


@pytest.mark.skipif(os.name == "nt", reason="worker checks use Linux pipe polling")
def test_checks_report_success_failure_and_missing_executable(sandbox: Path) -> None:
    result = worker._run_checks(
        {
            "checks": [
                {"name": "pass", "command": [sys.executable, "-c", "print('checked')"]},
                {"name": "fail", "command": [sys.executable, "-c", "raise SystemExit(7)"]},
                {"name": "missing", "command": ["/mergerail-uninstalled-test-command"]},
            ]
        }
    )
    assert not result["passed"]
    records = {record["name"]: record for record in result["results"]}
    assert records["pass"]["passed"] and "checked" in records["pass"]["output"]
    assert not records["fail"]["passed"] and not records["missing"]["passed"]


@pytest.mark.skipif(os.name == "nt", reason="worker checks use Linux pipe polling")
def test_check_output_and_deadline_are_bounded(sandbox: Path) -> None:
    result = worker._run_checks(
        {
            "checks": [
                {"name": "large", "command": [sys.executable, "-c", "print('x'*10000)"]},
                {
                    "name": "timeout",
                    "command": [sys.executable, "-c", "import time; time.sleep(5)"],
                },
            ],
            "log_limit_bytes": 1024,
            "timeout": 1,
        }
    )
    assert not result["passed"]
    records = {record["name"]: record for record in result["results"]}
    assert "exceeded" in records["large"]["output"]
    assert "timed out" in records["timeout"]["output"]


@pytest.mark.parametrize(
    "target", [".git/config", ".GIT/config", "dir/../.git/config", ".mergerail/tasks.json"]
)
def test_tree_rejects_symlink_into_controller_metadata(sandbox: Path, target: str) -> None:
    sha = commit_index_entry(sandbox, "link", target, "120000")
    with pytest.raises(worker.WorkerError, match=r"(?:metadata|controller|protected|Git|\.git)"):
        worker._validate_tree(sha, [".mergerail"])
