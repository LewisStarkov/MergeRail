"""Additional worker failure and dispatch coverage without a Docker daemon."""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mergerail.execution import worker


class WorkerPaths:
    def __init__(self, root: Path) -> None:
        self.repo = root / "work" / "repo"
        self.home = root / "work" / "home"
        self.snapshot = root / "tmp" / "snapshot.bundle"
        self.workspace = root / "tmp" / "workspace.tar"
        self.result = root / "tmp" / "result.bundle"
        self.session = root / "tmp" / "home.tar"


@pytest.fixture
def worker_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkerPaths:
    paths = WorkerPaths(tmp_path / "sandbox")
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


class _Output:
    def __init__(self) -> None:
        self.value = bytearray()

    def write(self, data: bytes) -> int:
        self.value.extend(data)
        return len(data)

    def flush(self) -> None:
        pass


def _console(
    monkeypatch: pytest.MonkeyPatch, request: dict[str, Any]
) -> tuple[_Output, io.StringIO]:
    output = _Output()
    errors = io.StringIO()
    streams = SimpleNamespace(
        stdin=SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())),
        stdout=SimpleNamespace(buffer=output),
        stderr=errors,
    )
    monkeypatch.setattr(worker, "sys", streams)
    monkeypatch.setattr(os, "environ", {})
    return output, errors


def test_workspace_restore_rejects_malformed_archive_and_preserves_existing_files(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "malformed.tar"
    archive.write_bytes(b"this is not a tar archive")
    destination = tmp_path / "checkout"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")

    with pytest.raises((worker.WorkerError, tarfile.TarError)):
        worker._safe_extract_workspace(archive, destination, limit_bytes=4096)

    assert sentinel.read_text(encoding="utf-8") == "original"
    assert list(destination.iterdir()) == [sentinel]


@pytest.mark.parametrize("name", ["../escape", "/absolute", "nested/../../escape"])
def test_workspace_restore_rejects_unsafe_tar_paths_before_writing(
    tmp_path: Path, name: str
) -> None:
    archive = tmp_path / "paths.tar"
    _tar(archive, [(name, b"attacker data")])
    destination = tmp_path / "checkout"
    outside = tmp_path / "escape"

    with pytest.raises(worker.WorkerError, match="unsafe path"):
        worker._safe_extract_workspace(archive, destination, limit_bytes=4096)

    assert not outside.exists()
    assert not destination.exists()


@pytest.mark.parametrize(
    ("protected", "excluded", "message"),
    [
        (".mergerail", (), "protected controller paths are malformed"),
        ([".mergerail", 3], (), "protected controller paths are malformed"),
        ([], ".opencode", "excluded archive paths are malformed"),
        ([], [".opencode", None], "excluded archive paths are malformed"),
    ],
)
def test_workspace_restore_rejects_malformed_path_policy(
    tmp_path: Path, protected: object, excluded: object, message: str
) -> None:
    archive = tmp_path / "empty.tar"
    _tar(archive, [("ordinary.txt", b"safe")])

    with pytest.raises(worker.WorkerError, match=message):
        worker._safe_extract_workspace(
            archive,
            tmp_path / "checkout",
            limit_bytes=4096,
            protected_paths=protected,
            excluded_paths=excluded,
        )


@pytest.mark.parametrize(
    ("name", "target", "protected"),
    [
        ("nested/.Git/config", b"not metadata", []),
        ("folder/link", "../.GIT/config", []),
        ("folder/link", "../.mergerail/tasks.json", [".mergerail"]),
        ("folder/link", "C:/outside", []),
        ("folder/link", "..\\outside", []),
    ],
)
def test_workspace_restore_rejects_git_state_and_aliased_symlink_targets(
    tmp_path: Path, name: str, target: bytes | str, protected: list[str]
) -> None:
    archive = tmp_path / "unsafe-links.tar"
    _tar(archive, [(name, target)])
    destination = tmp_path / "checkout"
    outside = tmp_path / "outside"
    outside.write_text("preserve", encoding="utf-8")

    with pytest.raises(worker.WorkerError):
        worker._safe_extract_workspace(
            archive,
            destination,
            limit_bytes=4096,
            protected_paths=protected,
        )

    assert outside.read_text(encoding="utf-8") == "preserve"
    assert not (destination / "folder" / "link").exists()


def test_workspace_restore_will_not_follow_a_preexisting_parent_symlink(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "parent-link.tar"
    _tar(archive, [("nested/new.txt", b"must not escape")])
    destination = tmp_path / "checkout"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")
    (destination / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(worker.WorkerError, match="escapes the checkout"):
        worker._safe_extract_workspace(archive, destination, limit_bytes=4096)

    assert sentinel.read_text(encoding="utf-8") == "original"
    assert not (outside / "new.txt").exists()


def test_workspace_restore_rejects_file_colliding_with_existing_directory(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "collision.tar"
    _tar(archive, [("nested", b"not a directory")])
    destination = tmp_path / "checkout"
    (destination / "nested").mkdir(parents=True)
    sentinel = destination / "nested" / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")

    with pytest.raises(worker.WorkerError, match="collides with a directory"):
        worker._safe_extract_workspace(archive, destination, limit_bytes=4096)

    assert sentinel.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("failure", ["missing", "oversize", "malformed"])
def test_restore_home_rejects_unusable_session_archive(
    worker_paths: WorkerPaths, failure: str
) -> None:
    if failure == "oversize":
        _tar(worker_paths.session, [("session.json", b"payload")])
        maximum = 1
    elif failure == "malformed":
        worker_paths.session.parent.mkdir(parents=True, exist_ok=True)
        worker_paths.session.write_bytes(b"not a tar")
        maximum = 4096
    else:
        maximum = 4096

    with pytest.raises((worker.WorkerError, tarfile.TarError)):
        worker._restore_home({"restore_home": True, "role": "fixer", "max_bundle_bytes": maximum})

    assert worker_paths.home.is_dir()
    assert not (worker_paths.home / "session.json").exists()


def test_restore_home_skips_creation_for_idle_reviewer_but_restores_valid_session(
    worker_paths: WorkerPaths,
) -> None:
    worker._restore_home({"restore_home": False, "role": "reviewer"})
    assert not worker_paths.home.exists()

    _tar(
        worker_paths.session,
        [("task.json", b"task state"), (".Opencode/private.json", b"host config")],
    )
    worker._restore_home({"restore_home": True, "role": "reviewer", "max_bundle_bytes": 65_536})

    assert (worker_paths.home / "task.json").read_bytes() == b"task state"
    assert not (worker_paths.home / ".Opencode").exists()


def test_prepare_owner_requires_designated_uid_before_changing_permissions(
    worker_paths: WorkerPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_paths.repo.mkdir(parents=True)
    original_mode = worker_paths.repo.stat().st_mode
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    with pytest.raises(worker.WorkerError, match="designated non-root UID"):
        worker._prepare_owner("fixer", read_only=True)

    assert worker_paths.repo.stat().st_mode == original_mode


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are required")
def test_prepare_owner_readonly_role_removes_write_bits_without_touching_home(
    worker_paths: WorkerPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_paths.repo.mkdir(parents=True)
    (worker_paths.repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(os, "geteuid", lambda: 65534, raising=False)

    worker._prepare_owner("reviewer", read_only=True)

    assert worker_paths.repo.stat().st_mode & 0o222 == 0
    assert (worker_paths.repo / "source.py").stat().st_mode & 0o222 == 0
    assert not worker_paths.home.exists()


def test_archive_member_count_is_checked_before_iterating_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeArchive:
        def __enter__(self) -> FakeArchive:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def getmembers(self) -> list[tarfile.TarInfo]:
            return [tarfile.TarInfo("unused")] * 100_001

    monkeypatch.setattr(tarfile, "open", lambda *_args, **_kwargs: FakeArchive())

    with pytest.raises(worker.WorkerError, match="too many entries"):
        worker._archive_members(tmp_path / "not-opened.tar", limit_bytes=1024)


def test_archive_member_negative_size_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    member = tarfile.TarInfo("negative")
    member.size = -1

    class FakeArchive:
        def __enter__(self) -> FakeArchive:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def getmembers(self) -> list[tarfile.TarInfo]:
            return [member]

    monkeypatch.setattr(tarfile, "open", lambda *_args, **_kwargs: FakeArchive())

    with pytest.raises(worker.WorkerError, match="negative file size"):
        worker._archive_members(tmp_path / "not-opened.tar", limit_bytes=1024)


@pytest.mark.parametrize(
    ("mode", "patches", "payload", "expected"),
    [
        (
            "probe",
            {"_probe": {"probe_id": "fixture"}},
            {"backend": "fixture"},
            {"probe_id": "fixture"},
        ),
        ("checks", {"_run_checks": {"check_id": "fixture"}}, {}, {"check_id": "fixture"}),
        (
            "preflight",
            {"_preflight_probe": {"preflight_id": "fixture"}},
            {},
            {"preflight_id": "fixture"},
        ),
        ("recover", {"_recover": {"recovery_id": "fixture"}}, {}, {"recovery_id": "fixture"}),
        (
            "merge_candidate",
            {"_merge_candidate": {"candidate_id": "fixture"}},
            {},
            {"candidate_id": "fixture"},
        ),
    ],
)
def test_main_dispatches_successful_worker_modes_and_frames_distinct_results(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    patches: dict[str, dict[str, str]],
    payload: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    for name, result in patches.items():
        monkeypatch.setattr(worker, name, lambda _request, result=result: result)
    output, _errors = _console(monkeypatch, {"mode": mode, **payload})

    assert worker.main() == 0

    frames = [json.loads(line) for line in output.value.splitlines()]
    frame_result: dict[str, Any] = {"probe": expected} if mode == "probe" else expected
    assert frames == [{"type": "result", **frame_result}]


def test_main_prepare_and_commit_dispatch_emit_the_verified_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sha = "a" * 40
    monkeypatch.setattr(worker, "_reset_from_bundle", lambda _request: (sha, sha))
    monkeypatch.setattr(worker, "_validate_tree", lambda *_args: None)
    monkeypatch.setattr(worker, "_restore_home", lambda _request: None)
    monkeypatch.setattr(worker, "_prepare_owner", lambda *_args: None)
    output, _errors = _console(monkeypatch, {"mode": "prepare", "base_sha": sha})

    assert worker.main() == 0
    assert json.loads(output.value) == {"type": "result", "base_sha": sha, "head": sha}

    monkeypatch.setattr(worker, "_commit_workspace", lambda *_args: sha)
    monkeypatch.setattr(worker, "_write_result_bundle", lambda _head: None)
    output, _errors = _console(monkeypatch, {"mode": "commit", "base_sha": sha})

    assert worker.main() == 0
    assert json.loads(output.value) == {"type": "result", "head": sha}


def test_export_main_quiesces_then_streams_a_bounded_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "answer.txt").write_text("safe artifact", encoding="utf-8")
    output = _Output()
    errors = io.StringIO()
    monkeypatch.setattr(
        worker,
        "sys",
        SimpleNamespace(stdout=SimpleNamespace(buffer=output), stderr=errors),
    )
    stopped: list[bool] = []
    monkeypatch.setattr(worker, "_quiesce_same_uid", lambda: stopped.append(True))

    assert worker.export_main([str(source), "65536", "1024"]) == 0

    assert stopped == [True]
    with tarfile.open(fileobj=io.BytesIO(bytes(output.value)), mode="r:") as archive:
        member = archive.getmember("answer.txt")
        payload = archive.extractfile(member)
        assert payload is not None and payload.read() == b"safe artifact"
    assert errors.getvalue() == ""


def test_export_main_reports_invalid_arguments_without_quiescing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors = io.StringIO()
    monkeypatch.setattr(worker, "sys", SimpleNamespace(stderr=errors))
    monkeypatch.setattr(
        worker,
        "_quiesce_same_uid",
        lambda: pytest.fail("invalid export request must fail before quiescing"),
    )

    assert worker.export_main(["only-one-argument"]) == 1
    assert "requires source and byte limits" in errors.getvalue()
