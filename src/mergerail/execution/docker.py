"""Disposable Docker execution for isolated MergeRail stages."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import stat
import subprocess
import threading
import time
import zipfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from .. import log
from ..backends import (
    AgentReply,
    BackendCapabilities,
    BackendInfo,
    BackendRegistry,
    EventSink,
    SessionSpec,
    TurnDiagnostics,
    TurnRequest,
    Usage,
)
from ..detect import Check
from ..lease import RunnerBusy, RunnerLease
from .policy import ExecutionPolicy
from .sync import (
    SyncError,
    bundle_refs,
    create_bundle,
    host_git,
    import_result_bundle,
    is_ancestor,
    resolve_commit,
    update_ref_cas,
)

MIB = 1024 * 1024
_SAFE_TASK_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_DOCKER_BOOTSTRAP = (
    "import os,sys;os.environ.clear();"
    "os.environ.update({'PATH':'/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',"
    "'HOME':'/work/home','TMPDIR':'/tmp','TMP':'/tmp','TEMP':'/tmp',"
    "'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null',"
    "'GIT_TERMINAL_PROMPT':'0','GIT_LFS_SKIP_SMUDGE':'1','GIT_ATTR_NOSYSTEM':'1'});"
    "sys.path.insert(0,'/tmp/mergerail-runtime.zip');"
    "from mergerail.execution.worker import main;raise SystemExit(main())"
)
_SLEEP_BOOTSTRAP = (
    "import os,time;os.environ.clear();"
    "os.environ.update({'PATH':'/usr/local/bin:/usr/bin:/bin'});time.sleep(86400)"
)
_EXPORT_BOOTSTRAP = (
    "import os,sys;os.environ.clear();"
    "os.environ.update({'PATH':'/usr/local/bin:/usr/bin:/bin'});"
    "sys.path.insert(0,'/tmp/mergerail-runtime.zip');"
    "from mergerail.execution.worker import export_main;"
    "raise SystemExit(export_main(sys.argv[1:]))"
)
_RECEIVE_BOOTSTRAP = (
    "import os,sys;"
    "p=sys.argv[1];"
    "fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600);"
    "f=os.fdopen(fd,'wb');"
    "n=0;limit=int(sys.argv[2]);"
    "\nwhile True:\n"
    " b=sys.stdin.buffer.read(min(1048576,limit+1-n))\n"
    " if not b: break\n"
    " n+=len(b)\n"
    " if n>limit: raise SystemExit('input exceeds limit')\n"
    " f.write(b)\n"
    "f.flush();os.fsync(f.fileno());f.close();os.chmod(p,0o444)"
)


class DockerExecutionError(RuntimeError):
    """Docker execution failed; host fallback is never permitted."""


class DockerExecution:
    """Own Docker stages, their network, and bounded Git synchronization."""

    def __init__(
        self,
        policy: ExecutionPolicy,
        root: Path,
        state_dir: Path,
        external_backends: dict[str, list[str]] | None = None,
    ) -> None:
        self.policy = policy
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.external_backends = {
            str(name).strip().lower(): [str(part) for part in command]
            for name, command in (external_backends or {}).items()
        }
        self.policy.validate()
        self._repo_id = hashlib.sha256(os.fsencode(self.root)).hexdigest()[:24]
        resource_dir = Path.home().resolve() / ".cache" / "mergerail"
        resource_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        resource_stat = resource_dir.stat()
        owner_identity = f"{resource_dir}:{resource_stat.st_dev}:{resource_stat.st_ino}"
        self._owner_id = hashlib.sha256(os.fsencode(owner_identity)).hexdigest()[:24]
        self._resource_lease_path = resource_dir / "docker-resource.lock"
        self._instance_id = uuid4().hex
        self._cache_dir = self.state_dir / "docker-execution"
        self._containers: set[str] = set()
        self._networks: set[str] = set()
        self._active_container: str | None = None
        self._active_lock = threading.RLock()
        self._preflight_result: dict[str, Any] | None = None
        self._probes: dict[str, BackendInfo] = {}
        self._task_id = "unscoped"
        self._base_sha = ""
        self._branch = ""
        self._head_sha = ""
        self._last_recovery: dict[str, Any] = {"status": "none"}
        self._metadata: dict[str, Any] = {
            "backend": "docker",
            "policy_digest": self.policy.digest,
            "image": self.policy.image,
            "validated": False,
            "base_sha": None,
            "result_sha": None,
            "phase": "created",
            "recovery": self._last_recovery,
        }
        self.worktree = DockerWorktree(self)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        if self._cache_dir.is_symlink():
            raise DockerExecutionError("Docker artifact cache directory must not be a symlink")
        self._cache_dir.chmod(0o700)
        self._write_metadata()

    @property
    def metadata(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(json.dumps(self._metadata)))

    @property
    def image(self) -> str:
        return self.policy.image

    def set_task_id(self, task_id: object) -> None:
        value = str(task_id)
        if not _SAFE_TASK_ID.fullmatch(value) or value in {".", ".."}:
            raise DockerExecutionError("task id contains unsafe characters for Docker artifacts")
        if self._task_id != value:
            self._metadata.pop("codex_gateway_diagnostics", None)
            self._metadata.pop("codex_cli_diagnostics", None)
        self._task_id = value
        self._write_metadata()

    def _task_dir(self, task_id: str | None = None) -> Path:
        key = task_id or self._task_id
        if not _SAFE_TASK_ID.fullmatch(key):
            raise DockerExecutionError("task id is unsafe for Docker artifact storage")
        tasks = self._cache_dir / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        if tasks.is_symlink():
            raise DockerExecutionError("task artifact root must not be a symlink")
        tasks.chmod(0o700)
        path = tasks / key
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise DockerExecutionError("task artifact directory must not be a symlink")
        path.chmod(0o700)
        return path

    def _write_metadata(self) -> None:
        self._metadata.update(
            {
                "backend": "docker",
                "policy_digest": self.policy.digest,
                "image": self.policy.image,
                "validated": bool(self._metadata.get("validated", False)),
                "base_sha": self._base_sha or None,
                "result_sha": self._head_sha or None,
                "branch": self._branch or None,
                "task_id": self._task_id,
                "phase": self._metadata.get("phase", "created"),
                "recovery": self._last_recovery,
            }
        )
        target = self._cache_dir / "metadata.json"
        temporary = target.with_name(f"metadata.{uuid4().hex}.tmp")
        payload = json.dumps(self._metadata, sort_keys=True, separators=(",", ":")).encode()
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(target)

    def _phase(self, phase: str, **values: Any) -> None:
        self._metadata["phase"] = phase
        self._metadata.update(values)
        self._write_metadata()

    def _docker(self, *args: str, check: bool = True, timeout: float = 30) -> str:
        try:
            result = subprocess.run(
                ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DockerExecutionError(f"cannot run Docker CLI: {error}") from error
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-2500:]
            raise DockerExecutionError(
                f"docker {' '.join(args[:3])} failed: {detail or result.returncode}"
            )
        return result.stdout.strip()

    def _docker_json(self, *args: str, timeout: float = 30) -> Any:
        raw = self._docker(*args, timeout=timeout)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise DockerExecutionError("Docker returned malformed JSON diagnostics") from error

    def _context_host(self) -> str:
        if platform.system() not in {"Linux", "Darwin"}:
            raise DockerExecutionError(
                "Docker execution is supported only on native Linux and macOS hosts"
            )
        if os.environ.get("DOCKER_HOST", "").strip():
            raise DockerExecutionError(
                "DOCKER_HOST is set; Docker execution requires a local context"
            )
        if os.environ.get("DOCKER_TLS_VERIFY") or os.environ.get("DOCKER_CERT_PATH"):
            raise DockerExecutionError("Docker TLS environment overrides are unsupported")
        context = os.environ.get("DOCKER_CONTEXT", "").strip()
        if not context:
            context = self._docker("context", "show")
        details = self._docker_json("context", "inspect", context)
        if not isinstance(details, list) or not details or not isinstance(details[0], dict):
            raise DockerExecutionError("cannot inspect the selected Docker context")
        endpoint = details[0].get("Endpoints", {}).get("docker", {}).get("Host")
        if not isinstance(endpoint, str):
            raise DockerExecutionError("Docker context has no daemon endpoint")
        if endpoint.startswith("unix://") and platform.system() in {"Linux", "Darwin"}:
            return endpoint
        raise DockerExecutionError(
            f"Docker context endpoint {endpoint!r} is not a supported local Unix socket"
        )

    @staticmethod
    def _canonical_arch(value: object) -> str:
        name = str(value or "").lower()
        return {
            "x86_64": "amd64",
            "amd64": "amd64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }.get(name, name)

    def _inspect_image(self, image: str) -> dict[str, Any]:
        found = self._docker_json("image", "inspect", image)
        if not isinstance(found, list) or not found or not isinstance(found[0], dict):
            raise DockerExecutionError(f"pinned image is not present locally: {image}")
        record = found[0]
        reference = image.rsplit("@", 1)[-1]
        digest = reference.removeprefix("sha256:")
        repo_digests = record.get("RepoDigests") or []
        identity_matches = record.get("Id") == f"sha256:{digest}"
        identity_matches |= any(str(item).endswith(f"@sha256:{digest}") for item in repo_digests)
        if not identity_matches:
            raise DockerExecutionError(
                f"Docker image identity does not match the pinned digest: {image}"
            )
        if record.get("Os") != "linux":
            raise DockerExecutionError(f"Docker image must target Linux: {image}")
        if self._canonical_arch(record.get("Architecture")) != self._canonical_arch(
            platform.machine()
        ):
            raise DockerExecutionError(
                "Docker image architecture does not match this machine; "
                "emulated images are not supported"
            )
        config_value = record.get("Config")
        config = config_value if isinstance(config_value, dict) else {}
        volumes = config.get("Volumes")
        if volumes:
            raise DockerExecutionError(
                "pinned Docker images with declared volumes are not supported"
            )
        env_value = config.get("Env")
        env = env_value if isinstance(env_value, list) else []
        unsafe_names = [
            str(item).split("=", 1)[0]
            for item in env
            if re.search(
                r"(?i)(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|PROXY)",
                str(item).split("=", 1)[0],
            )
            and "=" in str(item)
            and str(item).split("=", 1)[1]
        ]
        if unsafe_names:
            raise DockerExecutionError(
                "pinned image defines credential or proxy environment variables: "
                + ", ".join(sorted(unsafe_names))
            )
        return record

    def _runtime_zip(self) -> bytes:
        source = Path(__file__).resolve().parents[2]
        if source.is_relative_to(self.root):
            raise DockerExecutionError(
                "MergeRail controller source is inside the target repository; "
                "install it outside the project before Docker execution"
            )
        package = source / "mergerail"
        if not package.is_dir():
            raise DockerExecutionError(
                "cannot locate the trusted installed MergeRail package source"
            )
        output = io.BytesIO()
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for path in sorted(package.rglob("*.py")):
                if "__pycache__" in path.parts or path.is_symlink():
                    continue
                archive.write(path, path.relative_to(source).as_posix())
        result = output.getvalue()
        if not result or len(result) > 8 * MIB:
            raise DockerExecutionError("trusted MergeRail runtime ZIP is empty or exceeds 8 MiB")
        return result

    def _resource_labels(self, kind: str) -> list[str]:
        return [
            "--label",
            "com.mergerail.managed=true",
            "--label",
            f"com.mergerail.repo={self._repo_id}",
            "--label",
            f"com.mergerail.owner={self._owner_id}",
            "--label",
            f"com.mergerail.instance={self._instance_id}",
            "--label",
            f"com.mergerail.kind={kind}",
        ]

    def _new_name(self, prefix: str) -> str:
        return f"mergerail-{prefix}-{uuid4().hex[:16]}"

    def _create_network(self) -> str:
        name = self._new_name("net")
        self._docker(
            "network",
            "create",
            "--driver",
            "bridge",
            "--internal",
            "--ipv6=false",
            "--opt",
            "com.docker.network.bridge.gateway_mode_ipv4=isolated",
            *self._resource_labels("network"),
            name,
        )
        self._networks.add(name)
        inspected = self._docker_json("network", "inspect", name)[0]
        options = inspected.get("Options") or {}
        if (
            not inspected.get("Internal")
            or inspected.get("EnableIPv6")
            or options.get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"
        ):
            self._remove_network(name)
            raise DockerExecutionError("Docker did not create an internal IPv4-only AI network")
        return name

    def _container_options(
        self,
        *,
        name: str,
        network: str = "none",
        workspace_mib: int | None = None,
        include_gateway_host: str = "",
        image: str | None = None,
        gateway: bool = False,
    ) -> list[str]:
        workspace = workspace_mib or self.policy.workspace_limit_mib
        memory_bytes = self.policy.memory_mib * MIB
        cpus = int(self.policy.cpus * 100_000)
        pids = self.policy.pids_limit
        temporary_mib = self.policy.tmp_limit_mib
        if gateway or include_gateway_host:
            allocation = self._online_resource_limits()["gateway" if gateway else "agent"]
            memory_bytes = allocation["memory_mib"] * MIB
            cpus = allocation["cpu_quota"]
            pids = allocation["pids"]
            temporary_mib = allocation["tmp_mib"]
        command = [
            "create",
            "--name",
            name,
            *self._resource_labels("container"),
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--memory",
            str(memory_bytes),
            "--memory-swap",
            str(memory_bytes),
            "--cpu-period",
            "100000",
            "--cpu-quota",
            str(cpus),
            "--pids-limit",
            str(pids),
            "--shm-size",
            "16m",
            "--log-driver",
            "json-file",
            "--log-opt",
            f"max-size={self.policy.log_limit_mib}m",
            "--log-opt",
            "max-file=1",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={temporary_mib}m,mode=1777",
            "--user",
            "0:0",
        ]
        if not gateway:
            command += [
                "--tmpfs",
                f"/work:rw,nosuid,nodev,exec,size={workspace}m,mode=1777",
            ]
        if include_gateway_host:
            command += ["--dns", "127.0.0.1", "--add-host", f"mergerail-ai:{include_gateway_host}"]
        image_name = image or self.policy.image
        return [*command, "--entrypoint", "python3", image_name, "-I", "-c", _SLEEP_BOOTSTRAP]

    def _online_resource_limits(self) -> dict[str, dict[str, int]]:
        total_cpu = int(self.policy.cpus * 100_000)
        gateway = {
            "memory_mib": 256,
            "cpu_quota": max(1000, min(10_000, total_cpu // 10)),
            "pids": 32,
            "tmp_mib": 16,
        }
        agent = {
            "memory_mib": self.policy.memory_mib - gateway["memory_mib"],
            "cpu_quota": total_cpu - gateway["cpu_quota"],
            "pids": self.policy.pids_limit - gateway["pids"],
            "tmp_mib": self.policy.tmp_limit_mib - gateway["tmp_mib"],
        }
        if (
            agent["memory_mib"] < 256
            or agent["memory_mib"] <= self.policy.workspace_limit_mib + agent["tmp_mib"]
            or agent["cpu_quota"] < 1000
            or agent["pids"] < 16
            or agent["tmp_mib"] < 1
        ):
            raise DockerExecutionError("execution limits cannot fit the online agent and gateway")
        allocations = {"agent": agent, "gateway": gateway}
        self._metadata["online_resource_limits"] = allocations
        return allocations

    def _create_container(
        self,
        *,
        network: str = "none",
        workspace_mib: int | None = None,
        gateway_host: str = "",
        image: str | None = None,
        gateway: bool = False,
    ) -> str:
        name = self._new_name("gateway" if gateway else "stage")
        self._docker(
            *self._container_options(
                name=name,
                network=network,
                workspace_mib=workspace_mib,
                include_gateway_host=gateway_host,
                image=image,
                gateway=gateway,
            )
        )
        self._containers.add(name)
        try:
            self._docker("start", name)
        except BaseException:
            self._remove_container(name)
            raise
        return name

    def _inspect_container(self, name: str) -> dict[str, Any]:
        result = self._docker_json("inspect", name)
        if not isinstance(result, list) or not result or not isinstance(result[0], dict):
            raise DockerExecutionError(f"cannot inspect Docker container {name}")
        return result[0]

    def _owned(self, record: dict[str, Any]) -> bool:
        labels = record.get("Config", {}).get("Labels", {})
        if not isinstance(labels, dict):
            labels = record.get("Labels", {})
        return bool(
            labels.get("com.mergerail.managed") == "true"
            and labels.get("com.mergerail.repo") == self._repo_id
            and labels.get("com.mergerail.instance") == self._instance_id
        )

    def _remove_container(self, name: str, *, force: bool = True) -> None:
        try:
            record = self._inspect_container(name)
            if not self._owned(record):
                raise DockerExecutionError(f"refusing to remove unowned Docker container {name}")
            self._docker("rm", "--force" if force else "--volumes", name, check=False, timeout=60)
        except DockerExecutionError:
            pass
        self._containers.discard(name)
        with self._active_lock:
            if self._active_container == name:
                self._active_container = None

    def _remove_network(self, name: str) -> None:
        try:
            found = self._docker_json("network", "inspect", name)
            if found and isinstance(found[0], dict):
                labels = found[0].get("Labels") or {}
                if (
                    labels.get("com.mergerail.managed") != "true"
                    or labels.get("com.mergerail.repo") != self._repo_id
                    or labels.get("com.mergerail.instance") != self._instance_id
                ):
                    raise DockerExecutionError(f"refusing to remove unowned Docker network {name}")
                self._docker("network", "rm", name, check=False)
        except DockerExecutionError:
            pass
        self._networks.discard(name)

    def _copy_into(self, container: str, path: str, data: bytes, *, maximum: int) -> None:
        if len(data) > maximum:
            raise DockerExecutionError(f"Docker input {path} exceeds its configured byte limit")
        if not re.fullmatch(r"/tmp/[A-Za-z0-9_.-]+", path):
            raise DockerExecutionError("internal Docker copy path is invalid")
        script = _RECEIVE_BOOTSTRAP
        try:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    "--user",
                    "0:0",
                    container,
                    "python3",
                    "-I",
                    "-c",
                    script,
                    path,
                    str(maximum),
                ],
                input=data,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DockerExecutionError(f"cannot copy bounded data into Docker: {error}") from error
        if result.returncode != 0:
            raise DockerExecutionError(
                "Docker could not receive a bounded snapshot: "
                + result.stderr.decode(errors="replace")[-1200:]
            )

    def _worker_command(self, container: str, *, uid: int = 0) -> list[str]:
        return [
            "docker",
            "exec",
            "-i",
            "--user",
            f"{uid}:{uid}",
            container,
            "python3",
            "-I",
            "-c",
            _DOCKER_BOOTSTRAP,
        ]

    def _worker(
        self,
        container: str,
        request: dict[str, Any],
        *,
        uid: int = 0,
        timeout: float = 120,
        cancelled: Callable[[], bool] | None = None,
        events: EventSink | None = None,
        stage_label: str = "Docker worker",
    ) -> dict[str, Any]:
        frame = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
        if len(frame) > 16 * MIB:
            raise DockerExecutionError("Docker worker request exceeds the 16 MiB frame limit")
        command = self._worker_command(container, uid=uid)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise DockerExecutionError(f"cannot start {stage_label}: {error}") from error
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(frame)
        process.stdin.close()
        stderr_parts: list[bytes] = []

        def drain_stderr() -> None:
            assert process.stderr is not None
            total = 0
            while block := process.stderr.read(65536):
                room = self.policy.log_limit_mib * MIB - total
                if room > 0:
                    stderr_parts.append(block[:room])
                    total += min(len(block), room)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        timed_out = threading.Event()
        cancelled_event = threading.Event()

        def stop() -> None:
            self._docker("kill", container, check=False, timeout=10)

        def watch() -> None:
            while process.poll() is None:
                if cancelled is not None:
                    try:
                        if cancelled():
                            cancelled_event.set()
                            stop()
                            return
                    except Exception:
                        pass
                time.sleep(0.1)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()

        def expire() -> None:
            timed_out.set()
            stop()

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        total_output = 0
        result: dict[str, Any] | None = None
        try:
            while True:
                line = process.stdout.readline(4 * MIB + 2)
                if not line:
                    break
                total_output += len(line)
                if len(line) > 4 * MIB or total_output > self.policy.log_limit_mib * MIB:
                    stop()
                    raise DockerExecutionError(
                        f"{stage_label} output exceeded its configured limit"
                    )
                if not line.endswith(b"\n"):
                    stop()
                    raise DockerExecutionError(f"{stage_label} emitted an oversized JSONL frame")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as error:
                    stop()
                    raise DockerExecutionError(f"{stage_label} emitted malformed JSON") from error
                if not isinstance(event, dict):
                    stop()
                    raise DockerExecutionError(f"{stage_label} emitted a non-object JSON frame")
                kind = event.get("type")
                if kind == "event":
                    if events is not None and isinstance(event.get("event"), dict):
                        with suppress(Exception):
                            events.emit(event["event"])
                elif kind == "result":
                    result = event
                elif kind == "error":
                    raise DockerExecutionError(
                        f"{stage_label} failed: {event.get('error', 'unknown worker error')}"
                    )
                else:
                    raise DockerExecutionError(f"{stage_label} emitted an unknown frame")
            process.wait(timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            stop()
            raise DockerExecutionError(f"{stage_label} stopped unexpectedly: {error}") from error
        finally:
            timer.cancel()
            stderr_thread.join(timeout=2)
        stderr = b"".join(stderr_parts).decode(errors="replace").strip()
        if cancelled_event.is_set():
            raise DockerExecutionError(
                "Docker stage was cancelled; the last durable checkpoint is preserved"
            )
        if timed_out.is_set():
            raise DockerExecutionError(
                f"{stage_label} timed out; the last durable checkpoint is preserved"
            )
        if process.returncode != 0 or result is None:
            detail = stderr[-1800:] or f"worker exited with status {process.returncode}"
            raise DockerExecutionError(f"{stage_label} failed: {detail}")
        return result

    def _with_resource_lease(self) -> RunnerLease:
        lease = RunnerLease(self._resource_lease_path)
        try:
            lease.acquire()
        except RunnerBusy as error:
            raise DockerExecutionError(
                f"another MergeRail Docker stage holds the user resource lease: {error}"
            ) from error
        try:
            running = self._docker(
                "ps", "--quiet", "--filter", "label=com.mergerail.managed=true"
            ).splitlines()
            own_orphans = False
            for container in running:
                record = self._inspect_container(container)
                labels = record.get("Config", {}).get("Labels") or {}
                if (
                    labels.get("com.mergerail.managed") == "true"
                    and labels.get("com.mergerail.repo") != self._repo_id
                ):
                    raise DockerExecutionError(
                        "another repository has an orphaned MergeRail Docker container; "
                        "recover that repository before starting a new stage"
                    )
                if labels.get("com.mergerail.managed") == "true":
                    if labels.get("com.mergerail.owner") != self._owner_id:
                        raise DockerExecutionError(
                            "another user or an unknown owner has a MergeRail Docker container; "
                            "new stages are blocked without removing it"
                        )
                    own_orphans = True
            if own_orphans:
                self._reconcile_resources()
                if self._docker(
                    "ps", "--quiet", "--filter", "label=com.mergerail.managed=true"
                ).strip():
                    raise DockerExecutionError(
                        "orphaned MergeRail Docker containers remain active; "
                        "new stages are blocked until cleanup succeeds"
                    )
        except BaseException:
            lease.release()
            raise
        return lease

    def _workspace_bundle(self, base_sha: str, result_sha: str) -> tuple[bytes, dict[str, str]]:
        maximum = self.policy.max_bundle_mib * MIB
        try:
            bundle = create_bundle(
                self.root, tuple(dict.fromkeys((base_sha, result_sha))), max_bytes=maximum
            )
            refs = bundle_refs(bundle)
        except SyncError as error:
            raise DockerExecutionError(f"cannot freeze committed Git snapshot: {error}") from error
        if set(refs) != set(dict.fromkeys((base_sha, result_sha))):
            raise DockerExecutionError("frozen Git bundle omitted a requested commit")
        return bundle, refs

    def _snapshot_request(
        self, base_sha: str, result_sha: str, bundle: bytes, refs: dict[str, str], branch: str
    ) -> dict[str, Any]:
        return {
            "mode": "prepare",
            "base_sha": base_sha,
            "result_sha": result_sha,
            "branch": branch,
            "bundle_refs": refs,
            "max_bundle_bytes": self.policy.max_bundle_mib * MIB,
            "protected_paths": self._protected_paths(),
        }

    def _protected_paths(self) -> list[str]:
        try:
            relative = self.state_dir.relative_to(self.root).as_posix()
        except ValueError:
            return []
        if relative in {"", "."}:
            raise DockerExecutionError(
                "Docker controller state directory cannot be the project root"
            )
        return [relative]

    def _start_stage(
        self,
        base_sha: str,
        result_sha: str,
        branch: str,
        *,
        network: str = "none",
        gateway_host: str = "",
        workspace_mib: int | None = None,
    ) -> tuple[str, bytes, dict[str, str]]:
        bundle, refs = self._workspace_bundle(base_sha, result_sha)
        container = self._create_container(
            network=network,
            workspace_mib=workspace_mib,
            gateway_host=gateway_host,
        )
        with self._active_lock:
            self._active_container = container
        try:
            self._copy_into(
                container, "/tmp/mergerail-runtime.zip", self._runtime_zip(), maximum=8 * MIB
            )
            self._copy_into(
                container,
                "/tmp/snapshot.bundle",
                bundle,
                maximum=self.policy.max_bundle_mib * MIB,
            )
        except BaseException:
            self._remove_container(container)
            raise
        return container, bundle, refs

    def _prepare_stage(
        self,
        container: str,
        base_sha: str,
        result_sha: str,
        refs: dict[str, str],
        *,
        branch: str,
        role: str = "fixer",
        read_only: bool = False,
        home_archive: bytes | None = None,
    ) -> None:
        restore_home = home_archive is not None
        if home_archive is not None:
            self._copy_into(
                container,
                "/tmp/home.tar",
                home_archive,
                maximum=self.policy.max_bundle_mib * MIB,
            )
        result = self._worker(
            container,
            self._snapshot_request(base_sha, result_sha, b"", refs, branch)
            | {
                "mode": "prepare",
                "role": role,
                "read_only": read_only,
                "restore_home": restore_home,
            },
            uid=65534,
            stage_label="Docker workspace preparation",
        )
        if result.get("head") != result_sha:
            raise DockerExecutionError("Docker workspace restored a different Git checkpoint")
        if role == "reviewer":
            created = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "65533:65533",
                    container,
                    "python3",
                    "-I",
                    "-c",
                    "import os;"
                    "os.makedirs('/work/home',exist_ok=True);"
                    "os.chmod('/work/home',0o700)",
                ],
                capture_output=True,
                timeout=15,
                check=False,
            )
            if created.returncode != 0:
                raise DockerExecutionError(
                    "cannot initialize the isolated reviewer home: "
                    + created.stderr.decode(errors="replace")[-1000:]
                )

    def _capture_tar(self, container: str, source: str, *, purpose: str) -> Path:
        if source not in {"/work/repo", "/work/home"}:
            raise DockerExecutionError("Docker artifact source is outside the task tmpfs")
        available = self.policy.cache_limit_mib * MIB - self._cache_used_bytes() - 64 * 1024
        if available < 10 * 1024:
            raise DockerExecutionError(
                "Docker artifact cache has no reserved space for a safe checkpoint"
            )
        maximum = min(self.policy.max_bundle_mib * MIB, available)
        target = self._cache_dir / f"{purpose}.{uuid4().hex}.partial"
        command = [
            "docker",
            "exec",
            "-i",
            "--user",
            "65534:65534",
            container,
            "python3",
            "-I",
            "-c",
            _EXPORT_BOOTSTRAP,
            source,
            str(maximum),
            str(self.policy.workspace_limit_mib * MIB),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise DockerExecutionError(
                f"cannot start the quiesced Docker {purpose} export: {error}"
            ) from error
        assert process.stdout is not None
        size = 0
        limit_hit = threading.Event()
        expired = threading.Event()
        stderr_parts: list[bytes] = []

        def drain_stderr() -> None:
            assert process.stderr is not None
            stderr_parts.append(process.stderr.read(8192))

        def kill_container() -> None:
            expired.set()
            self._docker("kill", container, check=False, timeout=10)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        timer = threading.Timer(180, kill_container)
        timer.daemon = True
        timer.start()
        with target.open("xb") as output:
            target.chmod(0o600)
            while block := process.stdout.read(1024 * 1024):
                size += len(block)
                if size > maximum:
                    limit_hit.set()
                    process.kill()
                    self._docker("kill", container, check=False, timeout=10)
                    break
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            self._docker("kill", container, check=False, timeout=10)
            process.wait(timeout=10)
            expired.set()
        finally:
            timer.cancel()
            stderr_thread.join(timeout=2)
        stderr = b"".join(stderr_parts)
        if limit_hit.is_set():
            target.unlink(missing_ok=True)
            raise DockerExecutionError(
                f"Docker {purpose} export exceeded the configured size limit"
            )
        if expired.is_set():
            target.unlink(missing_ok=True)
            raise DockerExecutionError(
                f"Docker {purpose} export timed out; the last checkpoint is preserved"
            )
        if process.returncode != 0:
            target.unlink(missing_ok=True)
            raise DockerExecutionError(
                f"cannot export Docker {purpose}: {stderr.decode(errors='replace')[-1200:]}"
            )
        return target

    def _cache_used_bytes(self) -> int:
        used = 0
        for path in self._cache_dir.rglob("*"):
            if path.is_symlink():
                raise DockerExecutionError("Docker artifact cache contains an unexpected symlink")
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as error:
                raise DockerExecutionError(
                    f"cannot inspect Docker artifact cache: {error}"
                ) from error
            if stat.S_ISREG(info.st_mode):
                used += info.st_size
        return used

    def _promote_artifact(self, task_dir: Path, name: str, source: Path, *, limit: int) -> Path:
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise DockerExecutionError(f"Docker artifact {name} exceeds its configured byte limit")
        target = task_dir / name
        if target.is_symlink():
            raise DockerExecutionError("Docker artifact target must not be a symlink")
        previous = target.stat().st_size if target.exists() else 0
        used = self._cache_used_bytes()
        if used - previous > self.policy.cache_limit_mib * MIB:
            raise DockerExecutionError(
                "Docker artifact cache limit reached; remove old task-only artifacts"
            )
        source.replace(target)
        target.chmod(0o600)
        return target

    def _copy_file_into(self, container: str, path: str, source: Path, *, maximum: int) -> None:
        if not re.fullmatch(r"/tmp/[A-Za-z0-9_.-]+", path):
            raise DockerExecutionError("internal Docker copy path is invalid")
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise DockerExecutionError("Docker artifact input is not a bounded regular file")
        command = [
            "docker",
            "exec",
            "-i",
            "--user",
            "0:0",
            container,
            "python3",
            "-I",
            "-c",
            _RECEIVE_BOOTSTRAP,
            path,
            str(maximum),
        ]
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            assert process.stdin is not None
            total = 0
            with source.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    total += len(block)
                    if total > maximum:
                        process.kill()
                        raise DockerExecutionError(
                            "Docker artifact input exceeds its configured byte limit"
                        )
                    process.stdin.write(block)
            process.stdin.close()
            process.wait(timeout=120)
            detail = process.stderr.read(4096) if process.stderr is not None else b""
        except (OSError, subprocess.SubprocessError) as error:
            if "process" in locals() and process.poll() is None:
                process.kill()
                process.wait()
            raise DockerExecutionError(
                f"cannot send a bounded artifact into Docker: {error}"
            ) from error
        except DockerExecutionError:
            if "process" in locals() and process.poll() is None:
                process.kill()
                process.wait()
            raise
        if total != info.st_size or process.returncode != 0:
            raise DockerExecutionError(
                "Docker could not receive a bounded artifact: "
                + detail.decode(errors="replace")[-1200:]
            )

    def _read_container_file(self, container: str, path: str, maximum: int) -> bytes:
        if path != "/tmp/result.bundle":
            raise DockerExecutionError("Docker result file path is invalid")
        script = (
            "import os,sys;"
            "p=sys.argv[1];limit=int(sys.argv[2]);"
            "f=open(p,'rb');n=0;"
            "\nwhile True:\n"
            " b=f.read(min(1048576,limit+1-n))\n"
            " if not b: break\n"
            " n+=len(b)\n"
            " if n>limit: raise SystemExit('result exceeds limit')\n"
            " sys.stdout.buffer.write(b)\n"
            "sys.stdout.buffer.flush()"
        )
        command = [
            "docker",
            "exec",
            "--user",
            "65534:65534",
            container,
            "python3",
            "-I",
            "-c",
            script,
            path,
            str(maximum),
        ]
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as error:
            raise DockerExecutionError(f"cannot read Docker result: {error}") from error
        assert process.stdout is not None
        data = bytearray()
        stderr_parts: list[bytes] = []

        def drain_stderr() -> None:
            assert process.stderr is not None
            stderr_parts.append(process.stderr.read(8192))

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        while block := process.stdout.read(1024 * 1024):
            if len(data) + len(block) > maximum:
                process.kill()
                self._docker("kill", container, check=False, timeout=10)
                process.wait(timeout=10)
                stderr_thread.join(timeout=2)
                raise DockerExecutionError(
                    "Docker result bundle exceeded its configured byte limit"
                )
            data.extend(block)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired as error:
            process.kill()
            self._docker("kill", container, check=False, timeout=10)
            process.wait(timeout=10)
            raise DockerExecutionError("Docker result bundle read timed out") from error
        stderr_thread.join(timeout=2)
        if process.returncode != 0:
            detail = b"".join(stderr_parts).decode(errors="replace")[-1200:]
            raise DockerExecutionError(f"cannot read Docker result bundle: {detail}")
        return bytes(data)

    def _store_artifact(self, task_dir: Path, name: str, data: bytes, *, limit: int) -> Path:
        if len(data) > limit:
            raise DockerExecutionError(f"Docker artifact {name} exceeds its configured byte limit")
        used = self._cache_used_bytes()
        target = task_dir / name
        if used + len(data) > self.policy.cache_limit_mib * MIB:
            raise DockerExecutionError(
                "Docker artifact cache limit reached; remove old task-only artifacts"
            )
        temporary = task_dir / f"{name}.{uuid4().hex}.tmp"
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        return target

    def _agent_turn(
        self, session: DockerAgentSession, request: TurnRequest, *, deadline: float | None = None
    ) -> AgentReply:
        if deadline is not None and (time.monotonic() >= deadline or session._cancelled.is_set()):
            raise DockerExecutionError(
                "Codex turn deadline exhausted or cancelled; checkpoint preserved"
            )
        role = session.spec.role
        if role not in {"fixer", "reviewer"}:
            raise DockerExecutionError(f"unsupported Docker agent role: {role!r}")
        self._ensure_preflight()
        base_sha = self._base_sha
        result_sha = self.worktree.head()
        if not base_sha or not result_sha:
            raise DockerExecutionError("reset the Docker worktree before opening an agent session")
        gateway = session.backend_name in {"opencode", "codex"}
        model_definition = (
            self._selected_model_definition() if session.backend_name == "opencode" else {}
        )
        session_file = self._task_dir() / "fixer-home.tar"
        home_archive: bytes | None = None
        if role == "fixer" and session_file.exists():
            if session_file.stat().st_size > self.policy.max_bundle_mib * MIB:
                raise DockerExecutionError(
                    "saved fixer session archive exceeds the configured limit"
                )
            home_archive = session_file.read_bytes()
        lease = self._with_resource_lease()
        network = ""
        gateway_container = ""
        container = ""
        raw_reply: dict[str, Any] | None = None
        pending_workspace: Path | None = None
        pending_home: Path | None = None
        pending_archive: Path | None = None
        try:
            if gateway:
                network = self._create_network()
                if session.backend_name == "codex":
                    gateway_container, gateway_ip = self._start_gateway(network, backend="codex")
                else:
                    gateway_container, gateway_ip = self._start_gateway(network)
            else:
                gateway_ip = ""
            self._phase(
                "agent-starting", role=role, agent_backend=session.backend_name, base_sha=base_sha
            )
            container, _bundle, refs = self._start_stage(
                base_sha,
                result_sha,
                session.branch,
                network=network or "none",
                gateway_host=gateway_ip,
            )
            if deadline is not None and (
                time.monotonic() >= deadline or session._cancelled.is_set()
            ):
                raise DockerExecutionError(
                    "Codex turn deadline exhausted or cancelled; checkpoint preserved"
                )
            self._prepare_stage(
                container,
                base_sha,
                result_sha,
                refs,
                branch=session.branch,
                role=role,
                read_only=role == "reviewer" or session.spec.read_only,
                home_archive=home_archive,
            )
            turn_timeout = session.spec.timeout
            if deadline is not None:
                turn_timeout = min(turn_timeout, int(deadline - time.monotonic()))
                if turn_timeout < 1 or session._cancelled.is_set():
                    raise DockerExecutionError(
                        "Codex turn deadline exhausted or cancelled; checkpoint preserved"
                    )
            worker_request = {
                "mode": "turn",
                "backend": session.backend_name,
                "external_backends": self.external_backends,
                "role": role,
                "base_sha": base_sha,
                "prompt": request.prompt,
                "schema": request.schema,
                "system_prompt": session.spec.system_prompt,
                "read_only": role == "reviewer" or session.spec.read_only,
                "model": self.policy.opencode_model
                if session.backend_name == "opencode"
                else session.spec.model,
                "model_definition": model_definition,
                "effort": session.spec.effort,
                "settings": dict(session.spec.settings),
                "timeout": turn_timeout,
                "context_limit": session.spec.context_limit,
                "resume_session_id": session.session_id,
                "gateway": gateway,
                "gateway_host": gateway_ip,
                "codex_auth": self.policy.codex_auth,
                "max_bundle_bytes": self.policy.max_bundle_mib * MIB,
            }
            timeout = max(30, float(turn_timeout) + 30)
            with self._active_lock:
                self._active_container = container
            result = self._worker(
                container,
                worker_request,
                uid=65533 if role == "reviewer" else 65534,
                timeout=timeout,
                cancelled=lambda: session._cancelled.is_set(),
                events=session.events,
                stage_label=f"Docker {session.backend_name} {role} turn",
            )
            raw_reply = result.get("reply")
            if not isinstance(raw_reply, dict):
                raise DockerExecutionError("Docker agent returned no normalized reply")
            if session.backend_name == "codex":
                previous = self._metadata.get("codex_cli_diagnostics", [])
                diagnostics = [
                    asdict(value) for value in self._reply_from_dict(raw_reply).diagnostics
                ]
                self._metadata["codex_cli_diagnostics"] = (previous + diagnostics)[-16:]
            if session.backend_name == "codex" and gateway_container:
                with suppress(DockerExecutionError, ValueError):
                    payload = self._docker(
                        "exec",
                        "--user",
                        "0:0",
                        gateway_container,
                        "python3",
                        "-I",
                        "-c",
                        "from pathlib import Path; "
                        "p=Path('/tmp/mergerail-gateway-diagnostics.jsonl'); "
                        "print(p.open('rb').read(16384).decode() if p.exists() else '')",
                        timeout=5,
                    )
                    diagnostics = []
                    for line in payload.splitlines()[-16:]:
                        record = json.loads(line)
                        if (
                            isinstance(record, dict)
                            and record.get("close_reason")
                            in (
                                "upstream_eof",
                                "transport_error",
                                "timeout",
                                "size_limit",
                                "upstream_http_error",
                            )
                            and type(record.get("status")) is int
                            and type(record.get("bytes")) is int
                        ):
                            diagnostics.append(
                                {
                                    "close_reason": record["close_reason"],
                                    "status": record["status"],
                                    "bytes": record["bytes"],
                                }
                            )
                    previous = self._metadata.get("codex_gateway_diagnostics", [])
                    self._metadata["codex_gateway_diagnostics"] = (previous + diagnostics)[-32:]
            if role == "fixer":
                # The trusted exporter stops and kills every untrusted peer
                # before streaming either mutable tmpfs tree.
                self._phase("capturing-checkpoint", role=role, base_sha=base_sha)
                pending_workspace = self._capture_tar(container, "/work/repo", purpose="workspace")
                task_dir = self._task_dir()
                pending_archive = self._promote_artifact(
                    task_dir,
                    f"recovery-{uuid4().hex}.workspace.tar",
                    pending_workspace,
                    limit=self.policy.max_bundle_mib * MIB,
                )
                pending_workspace = None
                self._last_recovery = {
                    "status": "unverified",
                    "artifact": str(pending_archive),
                    "reason": "checkpoint archive captured; sandbox validation has not completed",
                }
                self._phase("recovering-checkpoint", role=role, base_sha=base_sha)
                pending_home = self._capture_tar(container, "/work/home", purpose="fixer-home")
                self._remove_container(container)
                container = ""
                with self._active_lock:
                    self._active_container = None
                if gateway_container:
                    self._remove_container(gateway_container)
                    gateway_container = ""
                if network:
                    self._remove_network(network)
                    network = ""
                new_head = self._recover_workspace(
                    base_sha,
                    result_sha,
                    session.branch,
                    pending_archive,
                )
                self._head_sha = new_head
                self._metadata["agent_result_sha"] = new_head
                self._promote_artifact(
                    task_dir,
                    "fixer-home.tar",
                    pending_home,
                    limit=self.policy.max_bundle_mib * MIB,
                )
                pending_home = None
                pending_archive.unlink(missing_ok=True)
                pending_archive = None
                self._last_recovery = {"status": "verified", "result_sha": new_head}
                self._phase("checkpoint-verified", role=role, result_sha=new_head)
                session.session_id = (
                    raw_reply.get("session_id")
                    if isinstance(raw_reply.get("session_id"), str)
                    else None
                )
            else:
                session.session_id = (
                    raw_reply.get("session_id")
                    if isinstance(raw_reply.get("session_id"), str)
                    else None
                )
                self._phase("reviewer-complete", role=role, result_sha=result_sha)
            return self._reply_from_dict(raw_reply)
        except (DockerExecutionError, SyncError):
            self._last_recovery = {
                "status": "unverified"
                if pending_archive is not None
                else "last-checkpoint-preserved",
                "artifact": str(pending_archive) if pending_archive is not None else None,
                "reason": "current stage did not produce an imported, validated checkpoint",
            }
            self._phase("failed", role=role, recovery=self._last_recovery)
            raise
        finally:
            for path in (pending_workspace, pending_home):
                if path is not None:
                    path.unlink(missing_ok=True)
            if container:
                self._remove_container(container)
            if gateway_container:
                self._remove_container(gateway_container)
            if network:
                self._remove_network(network)
            lease.release()

    def _recover_workspace(
        self, base_sha: str, result_sha: str, branch: str, workspace_tar: Path
    ) -> str:
        bundle, refs = self._workspace_bundle(base_sha, result_sha)
        container = self._create_container()
        with self._active_lock:
            self._active_container = container
        try:
            self._copy_into(
                container, "/tmp/mergerail-runtime.zip", self._runtime_zip(), maximum=8 * MIB
            )
            self._copy_into(
                container, "/tmp/snapshot.bundle", bundle, maximum=self.policy.max_bundle_mib * MIB
            )
            self._copy_file_into(
                container,
                "/tmp/workspace.tar",
                workspace_tar,
                maximum=self.policy.max_bundle_mib * MIB,
            )
            result = self._worker(
                container,
                {
                    "mode": "recover",
                    "base_sha": base_sha,
                    "result_sha": result_sha,
                    "branch": branch,
                    "bundle_refs": refs,
                    "max_bundle_bytes": self.policy.max_bundle_mib * MIB,
                    "max_workspace_bytes": self.policy.workspace_limit_mib * MIB,
                    "protected_paths": self._protected_paths(),
                },
                uid=65534,
                timeout=180,
                stage_label="Docker offline checkpoint validation",
            )
            new_head = result.get("head")
            if not isinstance(new_head, str) or not re.fullmatch(
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})", new_head
            ):
                raise DockerExecutionError(
                    "offline sandbox returned a checkpoint outside the frozen base"
                )
            bundle_bytes = self._read_container_file(
                container, "/tmp/result.bundle", self.policy.max_bundle_mib * MIB
            )
            imported = import_result_bundle(
                self.root,
                bundle_bytes,
                expected_sha=new_head,
                base_sha=base_sha,
                branch=branch,
                max_bytes=self.policy.max_bundle_mib * MIB,
            )
            return imported
        except SyncError as error:
            raise DockerExecutionError(f"sandbox checkpoint import failed: {error}") from error
        finally:
            self._remove_container(container)

    def _start_gateway(self, network: str, *, backend: str = "opencode") -> tuple[str, str]:
        host, prefix = self.policy.upstream_host, self.policy.upstream_prefix
        credentials: dict[str, str] | None = None
        if backend == "codex":
            from .codex_auth import CodexAuthError, gateway_credentials

            try:
                credentials = gateway_credentials(self.policy.codex_auth)
            except CodexAuthError as error:
                raise DockerExecutionError(str(error)) from None
            host, prefix = (
                ("chatgpt.com", "/backend-api/codex")
                if self.policy.codex_auth == "chatgpt"
                else ("api.openai.com", "/v1")
            )
        gateway = self._create_container(
            network=network,
            image=self.policy.gateway_image,
            gateway=True,
        )
        try:
            self._copy_into(
                gateway, "/tmp/mergerail-runtime.zip", self._runtime_zip(), maximum=8 * MIB
            )
            credential_args: list[str] = []
            if credentials is not None:
                # Only this source-free sidecar receives credentials. stdin is
                # bounded; tokens never enter argv, image env, or task archives.
                self._copy_into(
                    gateway,
                    "/tmp/codex-credentials.json",
                    json.dumps(credentials).encode(),
                    maximum=65536,
                )
                credential_args = ["--credentials-file", "/tmp/codex-credentials.json"]
            self._docker("network", "connect", "bridge", gateway, timeout=30)
            inspected = self._inspect_container(gateway)
            endpoint = inspected.get("NetworkSettings", {}).get("Networks", {}).get(network, {})
            address = endpoint.get("IPAddress") if isinstance(endpoint, dict) else ""
            if not isinstance(address, str) or not address:
                raise DockerExecutionError("cannot determine the private AI sidecar address")
            code = (
                "import os,sys;os.environ.clear();"
                "os.environ.update({'PATH':'/usr/local/bin:/usr/bin:/bin',"
                "'HOME':'/tmp'});sys.path.insert(0,'/tmp/mergerail-runtime.zip');"
                "from mergerail.execution.gateway import main;"
                f"raise SystemExit(main(['--upstream-host',{host!r},"
                f"'--upstream-prefix',{prefix!r},'--bind',{address!r},'--port','8765']"
                f"+{credential_args!r}))"
            )
            self._docker(
                "exec",
                "-d",
                "--user",
                "65534:0",
                gateway,
                "python3",
                "-I",
                "-c",
                code,
                timeout=20,
            )
            readiness = (
                "import socket,sys,time;deadline=time.monotonic()+25\n"
                "while time.monotonic()<deadline:\n"
                " try:\n"
                "  s=socket.create_connection((sys.argv[1],8765),1);s.close();sys.exit(0)\n"
                " except OSError: time.sleep(.1)\n"
                "raise SystemExit('gateway did not become ready')"
            )
            try:
                probe = subprocess.run(
                    [
                        "docker",
                        "exec",
                        gateway,
                        "python3",
                        "-I",
                        "-c",
                        readiness,
                        address,
                    ],
                    capture_output=True,
                    timeout=35,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise DockerExecutionError("AI gateway readiness probe timed out") from error
            if probe.returncode == 0:
                return gateway, address
            raise DockerExecutionError("AI egress gateway did not become ready")
        except BaseException:
            self._remove_container(gateway)
            raise

    def _selected_model_definition(self) -> dict[str, Any]:
        provider, slash, model_id = self.policy.opencode_model.partition("/")
        if not slash or provider != "opencode" or not model_id:
            raise DockerExecutionError(
                "Docker execution only accepts a selected OpenCode Zen model"
            )
        path = Path.home() / ".cache" / "opencode" / "models.json"
        try:
            if path.stat().st_size > 16 * MIB:
                raise DockerExecutionError("OpenCode model catalogue exceeds the 16 MiB read limit")
            catalogue = json.loads(path.read_text(encoding="utf-8"))
        except DockerExecutionError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DockerExecutionError(
                f"cannot verify the selected free OpenCode model: {error}"
            ) from error
        provider_entry = catalogue.get(provider) if isinstance(catalogue, dict) else None
        models = provider_entry.get("models") if isinstance(provider_entry, dict) else None
        entry = models.get(model_id) if isinstance(models, dict) else None
        if not isinstance(entry, dict):
            raise DockerExecutionError(
                f"selected model {self.policy.opencode_model!r} is absent from "
                "~/.cache/opencode/models.json"
            )
        costs = entry.get("cost")
        if not isinstance(costs, dict):
            raise DockerExecutionError("selected OpenCode model has no cost metadata")
        for key in ("input", "output"):
            value = costs.get(key)
            if isinstance(value, bool) or not isinstance(value, int | float) or value != 0:
                raise DockerExecutionError(
                    "selected OpenCode model is not proved free in the model catalogue"
                )
        for key in ("cache_read", "cache_write"):
            if key in costs:
                value = costs[key]
                if isinstance(value, bool) or not isinstance(value, int | float) or value != 0:
                    raise DockerExecutionError("selected OpenCode model has a nonzero cache cost")
        definition: dict[str, Any] = {
            "name": str(entry.get("name") or model_id)[:200],
            "cost": {key: 0 for key in ("input", "output", "cache_read", "cache_write")},
        }
        limit = entry.get("limit")
        if isinstance(limit, dict):
            definition["limit"] = {
                key: int(limit[key])
                for key in ("context", "output")
                if isinstance(limit.get(key), int) and not isinstance(limit.get(key), bool)
            }
        for key in ("tool_call", "reasoning", "attachment", "temperature"):
            if isinstance(entry.get(key), bool):
                definition[key] = entry[key]
        modalities = entry.get("modalities")
        if isinstance(modalities, dict):
            definition["modalities"] = {
                key: [item for item in values if isinstance(item, str)][:8]
                for key, values in modalities.items()
                if key in {"input", "output"} and isinstance(values, list)
            }
        return definition

    def _ensure_preflight(self) -> dict[str, Any]:
        return self._preflight_result or self.preflight()

    def preflight(self) -> dict[str, Any]:
        if self._preflight_result is not None:
            return dict(self._preflight_result)
        try:
            self.policy.validate()
            self._context_host()
            host_version = host_git(self.root, "--version")
            match = re.search(r"\b(\d+)\.(\d+)(?:\.(\d+))?", host_version)
            if not match or (int(match.group(1)), int(match.group(2))) < (2, 48):
                raise DockerExecutionError(
                    "host Git 2.48 or newer is required for atomic Docker delivery"
                )
            ref_format = host_git(self.root, "rev-parse", "--show-ref-format")
            if ref_format != "files":
                raise DockerExecutionError(
                    "host Git must use files ref storage for recoverable atomic delivery"
                )
            info = self._docker_json("info", "--format", "{{json .}}", timeout=30)
            if not isinstance(info, dict):
                raise DockerExecutionError("Docker daemon information is malformed")
            if info.get("OSType") != "linux":
                raise DockerExecutionError("Docker daemon must run Linux containers")
            version_match = re.match(r"(\d+)\.(\d+)", str(info.get("ServerVersion") or ""))
            if not version_match or int(version_match.group(1)) < 28:
                raise DockerExecutionError(
                    "Docker Engine 28 or newer is required for the isolated gateway bridge"
                )
            if str(info.get("CgroupVersion")) != "2":
                raise DockerExecutionError("Docker daemon must use cgroup v2")
            daemon_arch = self._canonical_arch(info.get("Architecture"))
            if daemon_arch != self._canonical_arch(platform.machine()):
                raise DockerExecutionError(
                    "Docker daemon architecture is not native to this machine"
                )
            daemon_cpus = int(info.get("NCPU") or 0)
            daemon_memory = int(info.get("MemTotal") or 0)
            if daemon_cpus < 1 or daemon_memory < self.policy.memory_mib * MIB:
                raise DockerExecutionError(
                    "Docker daemon does not have the configured CPU or memory capacity"
                )
            security = " ".join(str(item) for item in info.get("SecurityOptions") or [])
            if "seccomp" not in security and "name=seccomp" not in security:
                raise DockerExecutionError("Docker daemon does not report seccomp support")
            image = self._inspect_image(self.policy.image)
            gateway_image = self._inspect_image(self.policy.gateway_image)
            runtime = self._runtime_zip()
            lease = self._with_resource_lease()
            diagnostics: dict[str, Any] = {
                "docker_context": os.environ.get("DOCKER_CONTEXT") or "default",
                "daemon_os": info.get("OSType"),
                "daemon_architecture": info.get("Architecture"),
                "cgroup_version": info.get("CgroupVersion"),
                "engine_version": info.get("ServerVersion"),
                "image_id": image.get("Id"),
                "gateway_image_id": gateway_image.get("Id"),
                "runtime_bytes": len(runtime),
                "resource_limits": {
                    "cpus": self.policy.cpus,
                    "memory_mib": self.policy.memory_mib,
                    "workspace_mib": self.policy.workspace_limit_mib,
                    "tmp_mib": self.policy.tmp_limit_mib,
                    "pids": self.policy.pids_limit,
                },
            }
            self._phase("preflight", diagnostics=diagnostics)
            self._active_preflight_probe()
            self._preflight_result = diagnostics
            self._metadata["preflight"] = diagnostics
            self._metadata["validated"] = True
            self._phase("ready")
            return dict(diagnostics)
        except ValueError as error:
            raise DockerExecutionError(f"invalid Docker execution policy: {error}") from error
        except SyncError as error:
            raise DockerExecutionError(f"host Git preflight failed: {error}") from error
        finally:
            lease_obj = locals().get("lease")
            if isinstance(lease_obj, RunnerLease):
                lease_obj.release()

    def _active_preflight_probe(self) -> None:
        name = self._new_name("preflight")
        command = self._container_options(name=name, workspace_mib=1)
        # A small /work mount produces ENOSPC quickly; inspect verifies the
        # effective security and resource settings before the active probe.
        self._docker(*command)
        self._containers.add(name)
        self._docker("start", name)
        try:
            record = self._inspect_container(name)
            host = record.get("HostConfig", {})
            memory = self.policy.memory_mib * MIB
            expected = {
                "Memory": memory,
                "MemorySwap": memory,
                "CpuQuota": int(self.policy.cpus * 100_000),
                "CpuPeriod": 100_000,
                "PidsLimit": self.policy.pids_limit,
                "ReadonlyRootfs": True,
                "NetworkMode": "none",
            }
            for field, value in expected.items():
                if host.get(field) != value:
                    raise DockerExecutionError(
                        f"Docker did not apply the requested {field} limit "
                        f"({host.get(field)!r} != {value!r})"
                    )
            if host.get("Binds") or record.get("Mounts"):
                mounts = record.get("Mounts") or []
                if any(item.get("Type") != "tmpfs" for item in mounts):
                    raise DockerExecutionError(
                        "Docker stage unexpectedly has a host or named-volume mount"
                    )
            caps = host.get("CapDrop") or []
            security = host.get("SecurityOpt") or []
            if "ALL" not in caps or not any("no-new-privileges" in str(item) for item in security):
                raise DockerExecutionError(
                    "Docker stage did not drop all capabilities or enable NNP"
                )
            tmpfs = host.get("Tmpfs") or {}
            if "/work" not in tmpfs or "/tmp" not in tmpfs:
                raise DockerExecutionError(
                    "Docker stage did not receive the bounded workspace and tmp tmpfs mounts"
                )
            expected_tmpfs = {
                "/work": "rw,nosuid,nodev,exec,size=1m,mode=1777",
                "/tmp": f"rw,nosuid,nodev,noexec,size={self.policy.tmp_limit_mib}m,mode=1777",
            }
            if any(tmpfs.get(path) != value for path, value in expected_tmpfs.items()):
                raise DockerExecutionError(
                    "Docker stage tmpfs settings do not match the requested limits"
                )
            self._copy_into(
                name, "/tmp/mergerail-runtime.zip", self._runtime_zip(), maximum=8 * MIB
            )
            result = self._worker(
                name,
                {
                    "mode": "preflight",
                    "limits": {
                        "memory_bytes": memory,
                        "pids_limit": self.policy.pids_limit,
                        "cpus": self.policy.cpus,
                    },
                },
                timeout=30,
                stage_label="Docker cgroup and tmpfs probe",
            )
            if result.get("ok") is not True:
                raise DockerExecutionError("Docker resource probe did not pass")
            self._phase("preflight-cancel-probe")
            process = subprocess.Popen(
                ["docker", "exec", name, "python3", "-I", "-c", "import time;time.sleep(30)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.2)
            self._docker("kill", name, check=False, timeout=10)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            status = self._inspect_container(name).get("State", {})
            if status.get("Running"):
                raise DockerExecutionError("Docker did not stop an active stage when cancelled")
        finally:
            self._remove_container(name)

    def _probe_backend(self, name: str) -> BackendInfo:
        key = name.strip().lower()
        if key in self._probes:
            return self._probes[key]
        if key == "claude":
            info = BackendInfo(
                key,
                False,
                capabilities=BackendCapabilities(),
                reason=(
                    f"Docker backend {key!r} is unsupported until provider "
                    "authentication is validated"
                ),
            )
            self._probes[key] = info
            return info
        try:
            self._ensure_preflight()
            base = self._base_sha
            sha = base or resolve_commit(self.root, "HEAD")
            if not sha:
                raise DockerExecutionError("backend probe requires a committed repository base")
            bundle, refs = self._workspace_bundle(sha, sha)
            lease = self._with_resource_lease()
            container = ""
            try:
                container = self._create_container()
                self._copy_into(
                    container, "/tmp/mergerail-runtime.zip", self._runtime_zip(), maximum=8 * MIB
                )
                self._copy_into(
                    container,
                    "/tmp/snapshot.bundle",
                    bundle,
                    maximum=self.policy.max_bundle_mib * MIB,
                )
                self._worker(
                    container,
                    self._snapshot_request(sha, sha, bundle, refs, "mergerail/probe")
                    | {"mode": "prepare", "role": "fixer", "read_only": True},
                    uid=65534,
                    stage_label="Docker backend probe preparation",
                )
                result = self._worker(
                    container,
                    {"mode": "probe", "backend": key, "external_backends": self.external_backends},
                    uid=65534,
                    timeout=45,
                    stage_label=f"Docker {key} probe",
                )
                raw = result.get("probe") or {}
                caps_raw = raw.get("capabilities") or {}
                capabilities = BackendCapabilities(
                    **{
                        field: bool(caps_raw.get(field, getattr(BackendCapabilities(), field)))
                        for field in BackendCapabilities.__dataclass_fields__
                    }
                )
                info = BackendInfo(
                    name=key,
                    available=bool(raw.get("available")),
                    version=raw.get("version") if isinstance(raw.get("version"), str) else None,
                    capabilities=capabilities,
                    reason=str(raw.get("reason") or ""),
                )
            finally:
                if container:
                    self._remove_container(container)
                lease.release()
        except DockerExecutionError as error:
            info = BackendInfo(key, False, reason=str(error))
        self._probes[key] = info
        return info

    def probe(self, name: str) -> BackendInfo:
        key = name.strip().lower()
        if key == "opencode":
            try:
                self._selected_model_definition()
            except DockerExecutionError as error:
                return BackendInfo(key, False, reason=str(error))
        if key == "codex":
            from .codex_auth import CodexAuthError, gateway_credentials

            try:
                gateway_credentials(self.policy.codex_auth)
            except CodexAuthError as error:
                return BackendInfo(key, False, reason=str(error))
        return self._probe_backend(key)

    def registry(self) -> BackendRegistry:
        from .backends import DockerBackend

        names = {"opencode", "claude", "codex", *self.external_backends}
        reserved = {
            name
            for name in self.external_backends
            if name.strip().lower() in {"opencode", "claude", "codex"}
        }
        if reserved:
            raise DockerExecutionError(
                "external backend names may not replace built-in Docker providers: "
                + ", ".join(sorted(reserved))
            )
        registry = BackendRegistry()
        for name in sorted(names):
            registry.register(DockerBackend(self, name))
        return registry

    def _run_checks(
        self,
        checks: list[Check],
        sha: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._ensure_preflight()
        commit = resolve_commit(self.root, sha)
        bundle, refs = self._workspace_bundle(commit, commit)
        lease = self._with_resource_lease()
        container = ""
        try:
            container = self._create_container()
            self._copy_into(
                container, "/tmp/mergerail-runtime.zip", self._runtime_zip(), maximum=8 * MIB
            )
            self._copy_into(
                container, "/tmp/snapshot.bundle", bundle, maximum=self.policy.max_bundle_mib * MIB
            )
            self._worker(
                container,
                self._snapshot_request(commit, commit, bundle, refs, "mergerail/checks")
                | {"mode": "prepare", "role": "fixer", "read_only": False},
                uid=65534,
                stage_label="Docker check workspace preparation",
            )
            self._phase("checks-running", result_sha=commit)
            result = self._worker(
                container,
                {
                    "mode": "checks",
                    "checks": [{"name": item.name, "command": item.command} for item in checks],
                    "timeout": 1800,
                    "log_limit_bytes": self.policy.log_limit_mib * MIB,
                },
                uid=65534,
                timeout=max(30, 1800 * max(1, len(checks)) + 30),
                cancelled=cancelled,
                stage_label="Docker project checks",
            )
            return result
        finally:
            if container:
                self._remove_container(container)
            lease.release()

    def run_checks(
        self,
        checks: list[Check],
        sha: str,
        allowed_failures: frozenset[str] = frozenset(),
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[bool, str]:
        if self.policy.release_scope == "rivals-dev":
            from ..devbot import check_scope

            try:
                check_scope(self.root, resolve_commit(self.root, "main"), sha)
            except (ValueError, RuntimeError) as error:
                return False, str(error)
        try:
            result = self._run_checks(checks, sha, cancelled=cancelled)
        except DockerExecutionError as error:
            return False, str(error)
        records = result.get("results", [])
        names = {item.name for item in checks}
        passed = bool(result.get("passed")) or all(
            record.get("passed") or record.get("name") in allowed_failures for record in records
        )
        lines: list[str] = []
        for record in records:
            name = str(record.get("name") or "check")
            okay = bool(record.get("passed"))
            allowed = not okay and name in allowed_failures
            status = "PASS" if okay else "KNOWN FAIL" if allowed else "FAIL"
            output = str(record.get("output") or "")
            tail = output.splitlines()[-1] if output.splitlines() else ""
            lines.append(f"{name}: {status} — {tail[:200]}")
            if not okay and not allowed:
                lines.append(output[-1500:])
        missing = names - {str(item.get("name")) for item in records}
        if missing:
            passed = False
            lines.extend(f"{name}: FAIL — check did not run" for name in sorted(missing))
        return passed, "\n".join(lines) or "(no checks configured for this project)"

    def baseline(self, checks: list[Check], sha: str) -> tuple[list[Check], list[Check]]:
        result = self._run_checks(checks, sha)
        by_name = {
            str(item.get("name")): bool(item.get("passed")) for item in result.get("results", [])
        }
        healthy = [item for item in checks if by_name.get(item.name, False)]
        failing = [item for item in checks if not by_name.get(item.name, False)]
        if failing:
            log.warn("checks.docker_baseline_failed", report=str(result.get("report", ""))[-3000:])
        return healthy, failing

    def change_context(self, base: str) -> tuple[str, str]:
        if not self._branch:
            raise DockerExecutionError("Docker worktree has not been reset")
        head = self.worktree.head()
        base_sha = resolve_commit(self.root, base)
        if is_ancestor(self.root, head, base_sha):
            return "", ""
        diff_args = ["diff", "--no-ext-diff", "--no-textconv", f"{base_sha}...{head}"]
        stat = host_git(self.root, *diff_args[:1], "--stat", *diff_args[1:])
        diff = host_git(self.root, *diff_args)
        return diff, stat

    def merge_candidate(self, base_sha: str, result_sha: str, branch: str) -> str:
        self._ensure_preflight()
        base = resolve_commit(self.root, base_sha)
        result = resolve_commit(self.root, result_sha)
        lease = self._with_resource_lease()
        container = ""
        try:
            bundle, refs = self._workspace_bundle(base, result)
            container = self._create_container()
            self._copy_into(
                container, "/tmp/mergerail-runtime.zip", self._runtime_zip(), maximum=8 * MIB
            )
            self._copy_into(
                container, "/tmp/snapshot.bundle", bundle, maximum=self.policy.max_bundle_mib * MIB
            )
            returned = self._worker(
                container,
                {
                    "mode": "merge_candidate",
                    "base_sha": base,
                    "result_sha": result,
                    "allow_diverged": True,
                    "branch": branch,
                    "bundle_refs": refs,
                    "max_bundle_bytes": self.policy.max_bundle_mib * MIB,
                    "protected_paths": self._protected_paths(),
                },
                uid=65534,
                timeout=180,
                stage_label="Docker isolated integration merge",
            )
            candidate = returned.get("head")
            if not isinstance(candidate, str) or not re.fullmatch(
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})", candidate
            ):
                raise DockerExecutionError("sandbox merge returned an invalid candidate commit id")
            result_bundle = self._read_container_file(
                container, "/tmp/result.bundle", self.policy.max_bundle_mib * MIB
            )
            candidate_branch = f"mergerail-candidate/{self._task_id}-{uuid4().hex}"
            imported = import_result_bundle(
                self.root,
                result_bundle,
                expected_sha=candidate,
                base_sha=base,
                branch=candidate_branch,
                max_bytes=self.policy.max_bundle_mib * MIB,
            )
            if not is_ancestor(self.root, result, imported):
                raise DockerExecutionError(
                    "sandbox candidate does not contain the approved agent result"
                )
            self._metadata["agent_result_sha"] = result
            self._metadata["candidate_sha"] = imported
            self._metadata["candidate_branch"] = candidate_branch
            self._phase(
                "candidate-verified", candidate_sha=imported, candidate_branch=candidate_branch
            )
            return imported
        except SyncError as error:
            raise DockerExecutionError(f"sandbox merge import failed: {error}") from error
        finally:
            if container:
                self._remove_container(container)
            lease.release()

    def recover(self) -> dict[str, Any]:
        """Remove prior labeled resources and retain interrupted archives untrusted."""

        self._context_host()
        lease = self._with_resource_lease()
        try:
            return self._reconcile_resources()
        finally:
            lease.release()

    def _reconcile_resources(self) -> dict[str, Any]:

        self._phase("reconciling")
        containers = self._docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=com.mergerail.managed=true",
            "--filter",
            f"label=com.mergerail.repo={self._repo_id}",
            "--filter",
            f"label=com.mergerail.owner={self._owner_id}",
            check=False,
        ).splitlines()
        removed = 0
        for container_id in containers:
            try:
                record = self._inspect_container(container_id)
            except DockerExecutionError:
                continue
            labels = record.get("Config", {}).get("Labels", {})
            if (
                labels.get("com.mergerail.managed") == "true"
                and labels.get("com.mergerail.repo") == self._repo_id
                and labels.get("com.mergerail.owner") == self._owner_id
            ):
                self._docker("rm", "--force", container_id, check=False, timeout=30)
                removed += 1
        networks = self._docker(
            "network",
            "ls",
            "--quiet",
            "--filter",
            "label=com.mergerail.managed=true",
            "--filter",
            f"label=com.mergerail.repo={self._repo_id}",
            "--filter",
            f"label=com.mergerail.owner={self._owner_id}",
            check=False,
        ).splitlines()
        removed_networks = 0
        for network_id in networks:
            try:
                record = self._docker_json("network", "inspect", network_id)[0]
            except (DockerExecutionError, IndexError):
                continue
            labels = record.get("Labels") or {}
            if (
                labels.get("com.mergerail.managed") == "true"
                and labels.get("com.mergerail.repo") == self._repo_id
                and labels.get("com.mergerail.owner") == self._owner_id
            ):
                self._docker("network", "rm", network_id, check=False)
                removed_networks += 1
        pending = sorted(
            str(path) for path in (self._cache_dir / "tasks").glob("*/recovery-*.workspace.tar")
        )
        self._last_recovery = {
            "status": "unverified-artifacts-preserved" if pending else "resources-reconciled",
            "containers_removed": removed,
            "networks_removed": removed_networks,
            "pending_artifacts": pending,
            "auto_delivery": False,
        }
        self._phase("reconciled", recovery=self._last_recovery)
        return self.metadata

    def close(self) -> None:
        """Stop only resources owned by this executor; keep durable task files."""

        for container in tuple(self._containers):
            self._remove_container(container)
        for network in tuple(self._networks):
            self._remove_network(network)
        self._active_container = None
        self._phase("closed")

    @staticmethod
    def _reply_from_dict(raw: dict[str, Any]) -> AgentReply:
        usage_data = raw.get("usage")
        usage = None
        if isinstance(usage_data, dict):
            usage = Usage(
                input_tokens=usage_data.get("input_tokens"),
                output_tokens=usage_data.get("output_tokens"),
                cache_creation_input_tokens=usage_data.get("cache_creation_input_tokens"),
                cache_read_input_tokens=usage_data.get("cache_read_input_tokens"),
            )
        return AgentReply(
            text=str(raw.get("text") or ""),
            is_error=bool(raw.get("is_error")),
            cost_usd=raw.get("cost_usd") if isinstance(raw.get("cost_usd"), int | float) else None,
            context_tokens=int(raw.get("context_tokens") or 0),
            seconds=float(raw.get("seconds") or 0.0),
            structured=raw.get("structured") if isinstance(raw.get("structured"), dict) else None,
            session_id=raw.get("session_id") if isinstance(raw.get("session_id"), str) else None,
            usage=usage,
            diagnostics=tuple(
                diagnostic
                for value in raw.get("diagnostics", [])[:2]
                if (diagnostic := TurnDiagnostics.from_dict(value)) is not None
            )
            if isinstance(raw.get("diagnostics"), list)
            else (),
        )


class DockerWorktree:
    """Host Git branch state with a fixed virtual checkout path."""

    def __init__(self, execution: DockerExecution) -> None:
        self.execution = execution
        self.root = execution.root
        self.path = Path("/work/repo")

    def reset(self, branch: str, base: str, *, recreate: bool = False) -> None:
        execution = self.execution
        execution._ensure_preflight()
        if host_git(self.root, "check-ref-format", "--branch", branch, check=False) == "":
            raise DockerExecutionError(f"invalid Docker worktree branch name: {branch!r}")
        current = host_git(self.root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if current == branch:
            raise DockerExecutionError(
                "refusing to reset the branch currently checked out on the host"
            )
        base_sha = resolve_commit(self.root, base)
        ref = f"refs/heads/{branch}"
        existing = host_git(self.root, "rev-parse", "--verify", ref, check=False)
        if existing and not recreate:
            raise DockerExecutionError(
                f"refusing to reset existing Docker attempt branch {branch!r}"
            )
        if existing:
            if not is_ancestor(self.root, existing, base_sha) and not is_ancestor(
                self.root, base_sha, existing
            ):
                raise DockerExecutionError(
                    "existing Docker branch and requested base have diverged"
                )
            update_ref_cas(self.root, branch, base_sha, existing)
        else:
            update_ref_cas(self.root, branch, base_sha, None)
        # Freeze committed history only. Host working-tree and ignored files
        # never enter the bundle.
        bundle, refs = execution._workspace_bundle(base_sha, base_sha)
        execution._base_sha = base_sha
        execution._branch = branch
        execution._head_sha = base_sha
        execution._metadata["branch"] = branch
        execution._last_recovery = {"status": "clean-base", "base_sha": base_sha}
        execution._phase("worktree-reset", base_sha=base_sha, result_sha=base_sha)
        lease = execution._with_resource_lease()
        container = ""
        try:
            container = execution._create_container()
            execution._copy_into(
                container, "/tmp/mergerail-runtime.zip", execution._runtime_zip(), maximum=8 * MIB
            )
            execution._copy_into(
                container,
                "/tmp/snapshot.bundle",
                bundle,
                maximum=execution.policy.max_bundle_mib * MIB,
            )
            result = execution._worker(
                container,
                execution._snapshot_request(base_sha, base_sha, bundle, refs, branch)
                | {"mode": "prepare", "role": "fixer", "read_only": False},
                uid=65534,
                stage_label="Docker worktree reset",
            )
            if result.get("head") != base_sha:
                raise DockerExecutionError("Docker worktree reset did not restore the frozen base")
        finally:
            if container:
                execution._remove_container(container)
            lease.release()

    def head(self) -> str:
        if not self.execution._branch:
            raise DockerExecutionError("Docker worktree has not been reset")
        result = host_git(
            self.root,
            "rev-parse",
            "--verify",
            f"refs/heads/{self.execution._branch}^{{commit}}",
            check=False,
        )
        return result or self.execution._head_sha

    def is_dirty(self) -> bool:
        return False

    def commit_all(self, message: str) -> None:
        del message
        # Every fixer checkpoint is committed in the networkless recovery
        # sandbox before it becomes visible on the host branch.
        return

    def detach(self, base: str) -> None:
        # The host branch is owned by the controller's delivery transaction.
        # Detaching a virtual checkout must not rewrite the durable reviewed SHA.
        del base


class DockerAgentSession:
    def __init__(
        self,
        execution: DockerExecution,
        backend_name: str,
        spec: SessionSpec,
        events: EventSink,
    ) -> None:
        self.execution = execution
        self.backend_name = backend_name
        self.spec = spec
        self.events = events
        self.session_id = spec.resume_session_id
        self.branch = execution._branch
        self._cancelled = threading.Event()
        self._closed = False
        self._capabilities = execution.probe(backend_name).capabilities

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def ask(self, request: TurnRequest) -> AgentReply:
        if self._closed:
            return AgentReply("Docker agent session is closed", True, None, 0, 0.0)
        self._cancelled.clear()
        if self.backend_name != "codex":
            return self.execution._agent_turn(self, request)
        deadline = time.monotonic() + self.spec.timeout
        reply = self.execution._agent_turn(self, request, deadline=deadline)
        if (
            self.spec.role != "fixer"
            or not reply.is_error
            or not self.session_id
            or not reply.diagnostics
            or not (
                reply.diagnostics[-1].error_type == "transport"
                or (
                    reply.diagnostics[-1].error_type == "incomplete"
                    and reply.diagnostics[-1].close_reason == "missing_completion"
                    and reply.diagnostics[-1].exit_code == 0
                )
            )
            or self._cancelled.is_set()
            or deadline - time.monotonic() < 1
            or self.execution._last_recovery.get("status") != "verified"
            or not (self.execution._task_dir() / "fixer-home.tar").is_file()
        ):
            return reply
        self.execution._phase(
            "resuming-checkpoint", role="fixer", reason=reply.diagnostics[-1].close_reason
        )
        resumed = self.execution._agent_turn(
            self,
            TurnRequest(
                "The previous CLI stream disconnected. Your verified Git checkpoint and native "
                "session have been restored. Continue the same task from the existing changes; "
                "do not reset or discard them. Confirm completion, or finish any remaining work. "
                "MergeRail will run its checks and independent review afterwards.\n\n"
                + request.prompt,
                schema=request.schema,
                max_cost_usd=request.max_cost_usd,
            ),
            deadline=deadline,
        )
        return replace(
            resumed,
            seconds=reply.seconds + resumed.seconds,
            diagnostics=reply.diagnostics + resumed.diagnostics,
        )

    def cancel(self) -> None:
        self._cancelled.set()
        container = self.execution._active_container
        if container:
            self.execution._docker("kill", container, check=False, timeout=10)

    def close(self) -> None:
        self._closed = True
        self.session_id = None


__all__ = ["DockerExecution", "DockerExecutionError", "DockerWorktree"]
