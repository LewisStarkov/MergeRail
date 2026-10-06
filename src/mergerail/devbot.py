"""Docker-only Web front and immutable outbox for the Rivals dev deployment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Config
from .execution.sync import create_bundle, host_git
from .fronts.web import WebFront
from .tasks import Status, Task, TaskStore, write_atomic

MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
JOB_KEY = re.compile(r"[0-9]+-[0-9]+-[a-f0-9]{40}\Z")
RUNTIME_DIRS = ("bot/", "core/", "webapp/", "miniapp/", "admin/", "tests/")


def check_scope(root: Path, base: str, sha: str) -> None:
    """Infrastructure, dependencies, schema and release scope require operator review."""
    changed = host_git(root, "diff", "--name-only", "--no-renames", base, sha).splitlines()
    denied = [name for name in changed if not name.startswith(RUNTIME_DIRS)]
    # Manifests and config under a runtime directory are also executable build policy.
    denied += [
        name
        for name in changed
        if Path(name).name in {"package.json", "package-lock.json", "Dockerfile"}
        or Path(name).name.startswith(".env")
        or (
            name.startswith(("miniapp/public/", "admin/public/"))
            and Path(name).suffix in {".js", ".html"}
        )
    ]
    # Existing content tags hash bytes. Automatic tasks must preserve regular-file
    # metadata, so cached images cannot silently differ from the approved tree.
    for entry in host_git(root, "diff", "--raw", "--no-renames", base, sha).splitlines():
        metadata, name = entry.split("\t", 1)
        before, after = metadata.split()[:2]
        before = before.lstrip(":")
        if (
            "120000" in {before, after}
            or (before != after and before != "000000" and after != "000000")
            or (before == "000000" and after != "100644")
        ):
            denied.append(name)
    if denied:
        raise ValueError(
            "automatic DevBot release scope refused: " + ", ".join(sorted(set(denied)))
        )


def read_record(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise ValueError("invalid deployment record")
        raw = handle.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("deployment record exceeds its size limit")
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("deployment record must be an object")
    return value


class DevBotWebFront(WebFront):
    """No deploy credentials or host commands: publish reviewed objects only."""

    def __init__(self, store: TaskStore, config: Config) -> None:
        if (
            config.execution is None
            or config.execution.release_scope != "rivals-dev"
            or config.delivery != "local"
        ):
            raise ValueError("DevBot requires mandatory Docker execution and local delivery")
        if config.base_branch != "main" or config.baseline_mode != "strict" or not config.checks:
            raise ValueError("DevBot requires main and strict checks")
        if any(
            agent.backend not in {"opencode", "codex"} for agent in (config.fixer, config.reviewer)
        ):
            raise ValueError("DevBot online execution supports OpenCode and Codex only")
        super().__init__(store, host="127.0.0.1", port=8788)
        self.root = config.root
        self.state_dir = config.state_dir
        self.outbox = Path(os.environ["MERGERAIL_DEVBOT_OUTBOX"])
        self.outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.outbox.is_symlink():
            raise ValueError("deployment outbox must not be a symlink")
        password = Path(os.environ["MERGERAIL_WEB_PASSWORD_FILE"]).read_text().strip()
        if len(password) < 24:
            raise ValueError("DevBot web password must contain at least 24 characters")
        self.enable_auth("mergerail", password)
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None

    def start(self) -> None:
        # Complete a crash between durable DONE and publishing its request.
        for task in reversed(self.store.load()):
            if task.status == Status.DONE:
                try:
                    self.publish(task)
                except (OSError, ValueError, RuntimeError) as error:
                    self.store.append_message(task.id, f"DevBot not queued: {error}", role="system")
        super().start()
        self._watch_stop.clear()

        def watch() -> None:
            previous = ""
            while not self._watch_stop.wait(2):
                current = repr(
                    [
                        (p.name, p.stat().st_mtime_ns)
                        for p in sorted(self.outbox.glob("*/status.json"))
                    ]
                )
                if current != previous:
                    previous = current
                    self._changed()

        self._watch_thread = threading.Thread(target=watch, daemon=True)
        self._watch_thread.start()

    def stop(self) -> None:
        self._watch_stop.set()
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=3)
        super().stop()

    @staticmethod
    def key(task: Task) -> str:
        return f"{task.id}-{task.attempts}-{task.approved_sha}"

    def publish(self, task: Task) -> None:
        if not task.approved_sha or task.delivery.outcome != "local_merge":
            return
        if task.execution.get("backend") != "docker" or not task.execution.get("validated"):
            raise ValueError("deployment requires validated Docker execution")
        sha = task.approved_sha
        base = str(task.execution.get("base_sha") or "")
        if not re.fullmatch(r"[a-f0-9]{40}", sha) or not re.fullmatch(r"[a-f0-9]{40}", base):
            raise ValueError("deployment requires immutable base and approved SHAs")
        key = self.key(task)
        directory = self.outbox / key
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("deployment job must not be a symlink")
        request = directory / "request.json"
        if request.exists():
            self.clean_completed(task)
            return
        if host_git(self.root, "rev-parse", f"{base}^{{tree}}") == host_git(
            self.root, "rev-parse", f"{sha}^{{tree}}"
        ):
            return
        check_scope(self.root, base, sha)
        # Local integration must not substitute a different, unreviewed tree.
        if task.execution.get("delivered_sha") != sha:
            raise ValueError("approved SHA differs from the delivered result; deploy refused")
        used = sum(p.stat().st_size for p in self.outbox.glob("*/result.bundle"))
        if used + MAX_ARTIFACT_BYTES > 4096 * 1024 * 1024:
            raise ValueError("deployment outbox cache limit reached; archive completed artifacts")
        bundle = create_bundle(self.root, (sha,), max_bytes=MAX_ARTIFACT_BYTES)
        artifact = directory / "result.bundle"
        temporary = directory / f"result.{uuid4().hex}.tmp"
        with temporary.open("xb") as handle:
            handle.write(bundle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(artifact)
        write_atomic(
            request,
            json.dumps(
                {
                    "task_id": task.id,
                    "attempt": task.attempts,
                    "sha": sha,
                    "base_sha": base,
                    "artifact_id": hashlib.sha256(bundle).hexdigest(),
                    "environment": "rivals-dev",
                    "created_at": task.updated_at,
                    "policy_digest": task.execution.get("policy_digest"),
                }
            ),
        )
        self.clean_completed(task)

    def clean_completed(self, task: Task) -> None:
        cache = self.state_dir / "docker-execution" / "tasks" / str(task.id)
        if cache.is_dir() and not cache.is_symlink():
            for path in cache.iterdir():
                if (
                    (
                        path.name == "fixer-home.tar"
                        or re.fullmatch(r"recovery-[a-f0-9]{32}\.workspace\.tar", path.name)
                    )
                    and path.is_file()
                    and not path.is_symlink()
                ):
                    path.unlink()
        completed = []
        used = sum(p.stat().st_size for p in self.outbox.glob("*/result.bundle"))
        for path in self.outbox.glob("*/status.json"):
            if read_record(path).get("status") in {"succeeded", "superseded"}:
                artifact = path.parent / "result.bundle"
                if artifact.is_file() and not artifact.is_symlink():
                    completed.append(artifact)
        for artifact in sorted(completed, key=lambda p: p.stat().st_mtime):
            if used <= 2048 * 1024 * 1024 and len(completed) <= 20:
                break
            used -= artifact.stat().st_size
            artifact.unlink()
            completed.remove(artifact)

    def report(self, task: Task, event: str, text: str) -> None:
        if event == "done":
            try:
                self.publish(task)
            except (OSError, ValueError, RuntimeError) as error:
                self.store.append_message(task.id, f"DevBot not queued: {error}", role="system")
        super().report(task, event, text)

    def tasks_payload(self, *, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        payload = super().tasks_payload(offset=offset, limit=limit)
        for row in payload["tasks"]:
            key = f"{row['id']}-{row['attempts']}-{row['approved_sha']}"
            if not JOB_KEY.fullmatch(key):
                continue
            directory = self.outbox / key
            if directory.is_symlink():
                continue
            if (directory / "request.json").exists():
                row["deployment"] = {"status": "queued", **read_record(directory / "request.json")}
            if (directory / "status.json").exists():
                row["deployment"] = read_record(directory / "status.json")
        return payload

    def act(self, task_id: int, verb: str) -> tuple[int, dict[str, Any]]:
        if verb != "retry-deploy":
            return super().act(task_id, verb)
        task = self.store.get(task_id)
        if task is None:
            return 404, {"error": "task not found"}
        directory = self.outbox / self.key(task)
        if not (directory / "status.json").is_file():
            return 409, {"error": "no saved deployment"}
        if read_record(directory / "status.json").get("status") != "failed":
            return 409, {"error": "only a failed deployment can be retried"}
        write_atomic(directory / "retry.json", json.dumps({"id": uuid4().hex}))
        self._changed()
        return 200, {"ok": True}
