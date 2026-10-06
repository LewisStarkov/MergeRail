"""Trusted host-side adapter for the existing Rivals CPD development wrapper."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import re
import resource
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from .devbot import JOB_KEY, MAX_ARTIFACT_BYTES, check_scope, read_record
from .devbot_build import BUILD_CONTAINER, ENGINE, prepare_images
from .execution.delivery import _apply_candidate, _recover_apply
from .execution.sync import host_git, is_ancestor
from .lease import RunnerLease
from .tasks import now_iso, write_atomic

DEV_URL = "https://dev.rivals.baby"
CHECKOUT = Path("/Users/lama/.local/share/mergerail-devbot/deployer/checkout")


def health(sha: str) -> bool:
    try:
        request = urllib.request.Request(DEV_URL + "/health", headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return bool(json.load(response) == {"status": "ok", "revision": sha})
    except (OSError, ValueError):
        return False


class DevBotDeployer:
    def __init__(self, outbox: Path, state: Path, root: Path = CHECKOUT) -> None:
        self.outbox = outbox.resolve()
        self.state = state.resolve()
        self.root = root.resolve()
        if self.root != CHECKOUT:
            raise ValueError("DevBot deployment requires its verified automation checkout on main")
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lease = RunnerLease(self.state / "deployer.lock")

    def remote(self, script: str) -> str:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=15",
                "projects-main",
                "bash",
                "-s",
            ],
            input=script,
            capture_output=True,
            text=True,
            timeout=600 if "docker load -i" in script else 60,
        )
        if result.returncode:
            raise RuntimeError("DevBot SSH verification failed: " + result.stderr[-500:])
        return result.stdout.strip()

    def verified(self) -> str:
        sha = self.remote("set -eu\ncat /opt/rivals-dev-config/verified-commit\n")
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ValueError("DevBot has no valid verified SHA")
        return sha

    def healthy_release(self, sha: str) -> bool:
        # Legacy releases exposed their content tag; new releases expose the SHA.
        revision = self.remote(
            "set -eu\n"
            f'awk -F= \'$1 == "RIVALS_RELEASE_SHA" {{sha=$2}} '
            f'$1 == "APP_TAG" {{tag=$2}} END {{print sha ? sha : tag}}\' '
            f"/opt/rivals-dev-releases/{sha}/.release.env\n"
        )
        if not re.fullmatch(r"[a-f0-9]{20}|[a-f0-9]{40}", revision):
            raise ValueError("invalid release revision metadata")
        return health(revision)

    def write_status(self, directory: Path, request: dict[str, Any], **values: Any) -> None:
        record = {**request, **values, "updated_at": now_iso(), "url": DEV_URL}
        write_atomic(directory / "status.json", json.dumps(record))

    def approval(self, request: dict[str, Any]) -> None:
        saved = self.state / f"approval-{request['task_id']}-{request['attempt']}.json"
        if saved.exists():
            approval = read_record(saved)
        else:
            task_id = int(request["task_id"])
            code = (
                "import json; from pathlib import Path; from mergerail.tasks import TaskStore; "
                f"task=TaskStore(Path('/state/tasks.json')).get({task_id}); "
                "print(json.dumps(task.to_dict() if task else {}))"
            )
            result = subprocess.run(
                [
                    "docker",
                    "--context",
                    "colima-mergerail-devbot",
                    "exec",
                    "mergerail-devbot-controller-1",
                    "python3",
                    "-c",
                    code,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            approval = json.loads(result.stdout)
        execution = approval.get("execution", {})
        if (
            approval.get("status") != "done"
            or approval.get("approved_sha") != request["sha"]
            or approval.get("attempts") != request["attempt"]
            or approval.get("delivery", {}).get("outcome") != "local_merge"
            or execution.get("backend") != "docker"
            or not execution.get("validated")
            or execution.get("delivered_sha") != request["sha"]
            or execution.get("base_sha") != request["base_sha"]
            or execution.get("policy_digest") != request["policy_digest"]
        ):
            raise ValueError("outbox result does not match the trusted runner approval")
        if not saved.exists():
            write_atomic(saved, json.dumps(approval))

    def validate(self, directory: Path) -> dict[str, Any]:
        if directory.is_symlink() or not JOB_KEY.fullmatch(directory.name):
            raise ValueError("invalid deployment directory")
        request = read_record(directory / "request.json")
        sha = request.get("sha", "")
        base = request.get("base_sha", "")
        if not all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{40}", v) for v in (sha, base)):
            raise ValueError("invalid immutable deployment SHAs")
        if directory.name != f"{request.get('task_id')}-{request.get('attempt')}-{sha}":
            raise ValueError("deployment task identity mismatch")
        if request.get("environment") != "rivals-dev":
            raise ValueError("automatic deployment is restricted to rivals-dev")
        artifact = directory / "result.bundle"
        if artifact.is_symlink() or not 0 < artifact.stat().st_size <= MAX_ARTIFACT_BYTES:
            raise ValueError("invalid deployment artifact")
        with artifact.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != request.get("artifact_id"):
            raise ValueError("deployment artifact checksum mismatch")
        if host_git(self.root, "branch", "--show-current") != "main":
            raise ValueError("deployment checkout must remain on main")
        refs = host_git(self.root, "bundle", "list-heads", str(artifact)).splitlines()
        if len(refs) != 1 or refs[0].split()[0] != sha:
            raise ValueError("bundle does not contain exactly the approved result ref")
        host_git(self.root, "bundle", "verify", str(artifact))
        source_ref = refs[0].split()[1]
        if not source_ref.startswith("refs/mergerail-docker/"):
            raise ValueError("unexpected bundle ref")
        host_git(
            self.root,
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            str(artifact),
            f"{source_ref}:refs/mergerail-devbot/{directory.name}",
        )
        self.approval(request)
        check_scope(self.root, base, sha)
        if not is_ancestor(self.root, base, sha):
            raise ValueError("approved result does not descend from its recorded base")
        return request

    def run_wrapper(self, command: list[str], log: Path, timeout: float | None = None) -> int:
        # CPD owns its remote deployment lock. Do not kill it and blindly roll back.
        # Logs stay private and are bounded by consuming stdout, not an unbounded file.
        with log.open("ab") as handle:
            log.chmod(0o600)
            process = subprocess.Popen(
                command,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                preexec_fn=lambda: resource.setrlimit(
                    resource.RLIMIT_FSIZE, (4 * 1024**3, 4 * 1024**3)
                ),
                env={**os.environ, "CPD_HOST": "projects-main", "CPD_GIT_REMOTE": "origin"},
            )

            def expire() -> None:
                try:
                    subprocess.run(
                        [*ENGINE, "stop", "-t", "1", BUILD_CONTAINER],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                    )
                finally:
                    process.kill()

            timer = threading.Timer(timeout, expire) if timeout else None
            if timer:
                timer.daemon = True
                timer.start()
            assert process.stdout is not None
            output = process.stdout
            written = handle.tell()
            for block in iter(lambda: output.read(65536), b""):
                room = 5 * 1024 * 1024 - written
                if room > 0:
                    handle.write(block[:room])
                    written += min(room, len(block))
                    handle.flush()
            try:
                return process.wait()
            finally:
                if timer:
                    timer.cancel()

    def run_rollout(self, command: list[str], log: Path, deadline: float = 1800) -> int:
        # A stalled rollout has unknown external state. Keep its SSH process and
        # log drain alive; block the queue instead of interrupting remote services.
        result: list[int | Exception] = []

        def execute() -> None:
            try:
                result.append(self.run_wrapper(command, log))
            except Exception as error:
                result.append(error)

        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        worker.join(deadline)
        if worker.is_alive():
            raise RuntimeError("rollout deadline exceeded; process left active for inspection")
        value = result[0]
        if isinstance(value, Exception):
            raise value
        return value

    def rollback(self, previous: str, expected_current: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{40}", previous):
            raise ValueError("invalid previous release SHA")
        # The existing rollback command verifies schema and database identity and
        # refuses migration-in-progress. No database downgrade or restore occurs.
        status = self.run_rollout(
            [
                "bash",
                "scripts/rollback.sh",
                "development",
                previous,
                "--expected-current-sha",
                expected_current,
            ],
            self.state / "rollback.log",
        )
        if status or self.verified() != previous or not self.healthy_release(previous):
            raise RuntimeError("previous release recovery needs operator inspection")

    def deploy(self, directory: Path) -> None:
        status_path = directory / "status.json"
        old = read_record(status_path) if status_path.exists() else {}
        retry_path = directory / "retry.json"
        retry_id = str(read_record(retry_path).get("id", "")) if retry_path.exists() else ""
        if old.get("status") == "blocked":
            raise RuntimeError("operator inspection required: " + str(old.get("error")))
        if old.get("status") in {"succeeded", "superseded"}:
            return
        if old.get("status") == "failed" and retry_id == old.get("retry_id", ""):
            return
        request = self.validate(directory)
        sha = str(request["sha"])
        _recover_apply(self.root, self.state, int(request["task_id"]))
        current = host_git(self.root, "rev-parse", "main")
        if current != sha and is_ancestor(self.root, sha, current):
            self.write_status(
                directory,
                request,
                status="superseded",
                retry_id=retry_id,
                error="a newer result is already on main",
            )
            return
        if current != sha and current != request["base_sha"]:
            raise ValueError("main changed since review; preserved result requires a new review")
        previous = self.verified()
        if previous != sha and is_ancestor(self.root, sha, previous):
            self.write_status(
                directory,
                request,
                status="superseded",
                retry_id=retry_id,
                error="a newer result is already verified on DevBot",
            )
            return
        if not is_ancestor(self.root, previous, sha):
            raise ValueError("verified DevBot release is outside the reviewed ancestry")
        if self.verified() == sha and health(sha):
            self.write_status(
                directory, request, status="succeeded", previous_sha=previous, retry_id=retry_id
            )
            return
        if old.get("status") == "running":
            # An interrupted adapter must wait for the SSH rollout process to exit.
            self.remote("set -eu\nflock -n /opt/rivals-dev-config/deploy.lock true\n")
        if current != sha:
            _apply_candidate(
                self.root, self.state, int(request["task_id"]), "refs/heads/main", current, sha
            )
        self.write_status(
            directory, request, status="building", previous_sha=previous, retry_id=retry_id
        )
        prepare_images(self.root, self.state, sha, self.remote, self.run_wrapper)
        self.write_status(
            directory, request, status="running", previous_sha=previous, retry_id=retry_id
        )
        code = self.run_rollout(
            [
                "bash",
                "scripts/cpd-dev.sh",
                "--deploy-head",
                "--allow-dirty",
                "--prebuilt-only",
                "--expected-previous-sha",
                previous,
                "--expected-sha",
                sha,
            ],
            self.state / "deploy.log",
        )
        if code == 0 and self.verified() == sha and health(sha):
            self.write_status(
                directory, request, status="succeeded", previous_sha=previous, retry_id=retry_id
            )
            return
        if code == 42:
            self.write_status(
                directory,
                request,
                status="superseded",
                retry_id=retry_id,
                error="DevBot changed before the deployment lock was acquired",
            )
            return
        actual = self.remote(
            "set -eu\ncat /opt/rivals-dev-config/verified-commit\n"
            "readlink /opt/rivals-dev\n"
            "docker ps --filter label=com.docker.compose.project=rivals-dev "
            "--format '{{.Names}} {{.Image}} {{.Status}}'\n"
        )
        if actual.splitlines()[0] not in {previous, sha}:
            self.write_status(
                directory,
                request,
                status="blocked",
                previous_sha=previous,
                retry_id=retry_id,
                error="DevBot changed externally; rollback refused",
                server_state=actual,
            )
            raise RuntimeError("DevBot external state changed; queue blocked")
        reason = f"CPD/health verification failed (exit {code}); see private deploy.log"
        # A failed public check can leave candidate containers active even when
        # the canonical symlink still points at the old release.
        try:
            self.rollback(previous, actual.splitlines()[0])
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            self.write_status(
                directory,
                request,
                status="blocked",
                previous_sha=previous,
                retry_id=retry_id,
                error=f"{reason}; recovery refused: {error}",
                server_state=actual,
            )
            raise
        self.write_status(
            directory,
            request,
            status="failed",
            previous_sha=previous,
            retry_id=retry_id,
            error=reason + "; previous release restored",
        )

    def tick(self) -> None:
        directories = [p for p in self.outbox.iterdir() if JOB_KEY.fullmatch(p.name)]
        for directory in sorted(
            directories, key=lambda p: tuple(int(v) for v in p.name.split("-")[:2])
        ):
            if (
                not directory.is_dir()
                or directory.is_symlink()
                or not JOB_KEY.fullmatch(directory.name)
            ):
                continue
            if not (directory / "request.json").is_file():
                continue
            try:
                self.deploy(directory)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                request = read_record(directory / "request.json")
                old = (
                    read_record(directory / "status.json")
                    if (directory / "status.json").exists()
                    else {}
                )
                if old.get("status") in {"running", "blocked"}:
                    # Unknown external state blocks the queue, including newer tasks.
                    if old.get("status") == "running":
                        self.write_status(
                            directory,
                            request,
                            status="blocked",
                            error=str(error)[:600],
                            previous_sha=old.get("previous_sha"),
                            retry_id=old.get("retry_id", ""),
                        )
                    raise RuntimeError(f"deployment recovery blocked: {error}") from error
                retry = directory / "retry.json"
                retry_id = str(read_record(retry).get("id", "")) if retry.exists() else ""
                self.write_status(
                    directory, request, status="failed", error=str(error)[:600], retry_id=retry_id
                )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outbox", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    deployer = DevBotDeployer(args.outbox, args.state)
    logger = logging.getLogger("mergerail.devbot-deploy")
    logger.addHandler(
        logging.handlers.RotatingFileHandler(
            args.state / "service.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=2,
        )
    )
    deployer.lease.acquire()
    try:
        while True:
            try:
                deployer.tick()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                logger.exception("deployment queue blocked; saved result retained")
                if args.once:
                    raise
            if args.once:
                return
            time.sleep(3)
    finally:
        deployer.lease.release()


if __name__ == "__main__":
    main()
