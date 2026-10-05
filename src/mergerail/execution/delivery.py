"""Deliver only the immutable tree checked and merged inside Docker."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import delivery
from ..delivery import Landing, StageSink
from ..detect import Check
from ..tasks import Task, write_atomic
from .sync import host_git, host_git_command

if TYPE_CHECKING:
    from .docker import DockerExecution


def recover_delivery(
    execution: DockerExecution,
    root: Path,
    task: Task,
    checks: list[Check],
    allowed_failures: frozenset[str],
    *,
    on_stage: StageSink | None = None,
) -> Landing:
    record = task.delivery
    if not record.branch or not record.commit or not record.base_branch:
        return Landing(False, "", "delivery record is incomplete", stage="preflight")
    mode = record.resolved_mode or delivery.resolve_mode(root, record.requested_mode)
    if mode == delivery.PR:
        return delivery.open_pull_request(
            root,
            task,
            record.branch,
            record.base_branch,
            delivery.pr_body(task, record.summary, record.review),
            commit=record.commit,
            on_stage=on_stage,
            sandboxed=True,
        )
    try:
        base_ref = f"refs/heads/{record.base_branch}"
        if host_git(root, "symbolic-ref", "--quiet", "HEAD") != base_ref:
            raise RuntimeError(f"checkout must be on {record.base_branch}")
        _recover_apply(root, execution.state_dir, task.id)
        base_sha = host_git(root, "rev-parse", "--verify", base_ref)
        if host_git(root, "merge-base", record.commit, base_sha) == record.commit:
            return Landing(True, delivery.MERGE, stage="complete", outcome=delivery.LOCAL_MERGE)
        if host_git(root, "diff", "--name-only", "HEAD"):
            raise RuntimeError("working checkout has uncommitted changes; result branch is intact")
        if on_stage:
            on_stage("integration")
        candidate = execution.merge_candidate(base_sha, record.commit, record.branch)
        if on_stage:
            on_stage("integration_checks")
        passed, report = execution.run_checks(checks, candidate, allowed_failures=allowed_failures)
        if not passed:
            return Landing(
                False, "", f"integration checks failed: {report}", stage="integration_checks"
            )
        if on_stage:
            on_stage("merge")
        _apply_candidate(root, execution.state_dir, task.id, base_ref, base_sha, candidate)
        return Landing(True, delivery.MERGE, stage="complete", outcome=delivery.LOCAL_MERGE)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        return Landing(False, "", str(error), stage="merge")


def _transaction(
    root: Path, base_ref: str, base_sha: str, candidate: str
) -> subprocess.Popen[bytes]:
    if host_git(root, "rev-parse", "--show-ref-format") != "files":
        raise RuntimeError("Docker delivery requires Git's files reference storage")
    host_git(root, "check-ref-format", base_ref)
    if not all(
        re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", sha) for sha in (base_sha, candidate)
    ):
        raise ValueError("delivery requires exact commit hashes")
    command, env = host_git_command(root, "update-ref", "--stdin")
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    # Updating HEAD locks both the symbolic reference and its referent.
    # Verify the branch while those prepared locks prevent a checkout change.
    process.stdin.write(f"start\nupdate HEAD {candidate} {base_sha}\nprepare\n".encode("ascii"))
    process.stdin.flush()
    watchdog = threading.Timer(30, process.kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        if (
            process.stdout.readline().strip() != b"start: ok"
            or process.stdout.readline().strip() != b"prepare: ok"
        ):
            _, stderr = process.communicate(timeout=5)
            raise RuntimeError(
                "checkout or base changed: " + stderr.decode(errors="replace").strip()
            )
        head_lock = _ref_lock_paths(root, base_ref)[0]
        if host_git(root, "symbolic-ref", "--quiet", "HEAD") != base_ref or not head_lock.exists():
            raise RuntimeError("checkout or base changed during reference preparation")
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    finally:
        watchdog.cancel()
    return process


def _apply_candidate(
    root: Path,
    state_dir: Path,
    task_id: int,
    base_ref: str,
    base_sha: str,
    candidate: str,
) -> None:
    index = Path(host_git(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    lock = index.with_name(index.name + ".lock")
    journal = state_dir / "docker-delivery-apply.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    process = None
    temporary = None
    applied = False
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(f"mergerail docker delivery {task_id}\n")
            handle.flush()
            os.fsync(handle.fileno())
        fd, name = tempfile.mkstemp(prefix="mergerail-index-", dir=index.parent)
        os.close(fd)
        temporary = Path(name)
        shutil.copyfile(index, temporary)
        record: dict[str, Any] = {
            "task": task_id,
            "base_ref": base_ref,
            "base_sha": base_sha,
            "candidate": candidate,
            "index": str(index),
            "temporary": str(temporary),
        }
        write_atomic(journal, json.dumps(record))
        process = _transaction(root, base_ref, base_sha, candidate)
        record["ref_locks"] = _record_ref_locks(root, base_ref)
        write_atomic(journal, json.dumps(record))
        if host_git(root, "diff", "--name-only", "HEAD"):
            raise RuntimeError("checkout changed during validation")
        command, env = host_git_command(root, "read-tree", "-u", "-m", base_sha, candidate)
        env["GIT_INDEX_FILE"] = str(temporary)
        applied = True
        outcome = subprocess.run(
            command, cwd=root, env=env, capture_output=True, text=True, timeout=60
        )
        if outcome.returncode:
            raise RuntimeError("checkout refused: " + (outcome.stderr or outcome.stdout).strip())
        os.replace(temporary, index)
        stdout, stderr = process.communicate(b"commit\n", timeout=30)
        if process.returncode or b"commit: ok" not in stdout:
            raise RuntimeError(
                "reference commit failed; saved delivery journal requires recovery: "
                + stderr.decode(errors="replace").strip()
            )
        applied = False
        journal.unlink(missing_ok=True)
    finally:
        if process is not None and process.poll() is None:
            with contextlib.suppress(subprocess.SubprocessError, OSError):
                process.communicate(b"abort\n", timeout=5)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        if not applied:
            lock.unlink(missing_ok=True)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)


def _recover_apply(root: Path, state_dir: Path, task_id: int) -> None:
    journal = state_dir / "docker-delivery-apply.json"
    if not journal.exists():
        return
    record = json.loads(journal.read_text())
    if record.get("task") != task_id:
        raise RuntimeError("a different Docker delivery has an unfinished checkout journal")
    index = Path(host_git(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    if record.get("index") != str(index):
        raise RuntimeError("delivery recovery index does not match this repository")
    lock = index.with_name(index.name + ".lock")
    if lock.read_text() != f"mergerail docker delivery {task_id}\n":
        raise RuntimeError("delivery recovery does not own the index lock")
    candidate = str(record["candidate"])
    base_sha = str(record["base_sha"])
    base_ref = str(record["base_ref"])
    _release_recorded_ref_locks(root, base_ref, record.get("ref_locks", {}))
    if host_git(root, "symbolic-ref", "HEAD") != base_ref:
        raise RuntimeError("checkout moved after interrupted delivery; inspect the saved journal")
    current = host_git(root, "rev-parse", base_ref)
    if current not in {base_sha, candidate}:
        raise RuntimeError("base moved after interrupted delivery; result branch is intact")
    temporary = Path(str(record["temporary"]))
    if temporary.parent != index.parent or not temporary.name.startswith("mergerail-index-"):
        raise RuntimeError("delivery recovery has an invalid temporary index")
    candidate_index = next(
        (
            path
            for path in (index, temporary)
            if path.exists() and _index_matches(root, path, candidate)
        ),
        None,
    )
    if candidate_index is None:
        if current == base_sha and _index_matches(root, index, base_sha):
            lock.unlink()
            temporary.unlink(missing_ok=True)
            journal.unlink()
            return
        raise RuntimeError(
            "interrupted checkout has local changes; inspect the saved journal before retrying"
        )
    if candidate_index == temporary:
        os.replace(temporary, index)
    if current == base_sha:
        process = _transaction(root, base_ref, base_sha, candidate)
        stdout, stderr = process.communicate(b"commit\n", timeout=30)
        if process.returncode or b"commit: ok" not in stdout:
            raise RuntimeError(
                "cannot recover delivery reference: " + stderr.decode(errors="replace").strip()
            )
    lock.unlink()
    temporary.unlink(missing_ok=True)
    journal.unlink()


def _index_matches(root: Path, index: Path, sha: str) -> bool:
    for args in (("diff", "--cached", "--name-only", sha), ("diff", "--name-only")):
        command, env = host_git_command(root, *args)
        env["GIT_INDEX_FILE"] = str(index)
        result = subprocess.run(
            command, cwd=root, env=env, capture_output=True, text=True, timeout=30
        )
        if result.returncode or result.stdout.strip():
            return False
    return True


def _ref_lock_paths(root: Path, base_ref: str) -> list[Path]:
    return [
        Path(host_git(root, "rev-parse", "--path-format=absolute", "--git-path", name))
        for name in ("HEAD.lock", base_ref + ".lock")
    ]


def _lock_identity(path: Path) -> dict[str, int | str]:
    state = path.lstat()
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("reference lock is not a regular file")
    return {
        "device": state.st_dev,
        "inode": state.st_ino,
        "mtime": state.st_mtime_ns,
        "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _record_ref_locks(root: Path, base_ref: str) -> dict[str, dict[str, int | str]]:
    return {
        str(path): _lock_identity(path) for path in _ref_lock_paths(root, base_ref) if path.exists()
    }


def _release_recorded_ref_locks(root: Path, base_ref: str, recorded: object) -> None:
    if not isinstance(recorded, dict):
        raise RuntimeError("invalid delivery reference-lock journal")
    for path in _ref_lock_paths(root, base_ref):
        if path.exists():
            if recorded.get(str(path)) != _lock_identity(path):
                raise RuntimeError(
                    "a reference lock has unknown ownership; inspect the saved journal"
                )
            path.unlink()
