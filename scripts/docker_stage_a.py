"""Bounded Stage A experiments; never enables MergeRail's protected mode.

Requires an already installed, trusted Linux image with /bin/sh and coreutils.
No pulls, builds, project commands, credentials, bind mounts, or persistent volumes.
Exit 2 means the release gate remains blocked, even when individual probes pass.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=30,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result


def experiment(image: str, *, stress: bool = False) -> dict[str, Any]:
    if os.environ.get("DOCKER_HOST"):
        raise RuntimeError("DOCKER_HOST override is unsupported; select a local context")
    context = json.loads(docker("context", "inspect").stdout)[0]
    endpoint = context["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith(("unix://", "npipe://")):
        raise RuntimeError("only a local Docker context is allowed")
    info = json.loads(docker("info", "--format", "{{json .}}").stdout)
    if info["OSType"] != "linux" or info["CgroupVersion"] != "2":
        raise RuntimeError("these experiments require Linux containers and cgroup v2")
    for capability in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"):
        if not info.get(capability):
            raise RuntimeError(f"engine does not advertise {capability}")
    metadata = json.loads(docker("image", "inspect", image).stdout)[0]
    aliases = {"aarch64": "arm64", "x86_64": "amd64"}
    arch = aliases.get(info["Architecture"], info["Architecture"])
    host_arch = aliases.get(platform.machine(), platform.machine())
    if metadata["Os"] != "linux" or metadata["Architecture"] != arch or arch != host_arch:
        raise RuntimeError("native matching host/engine/image architecture required")
    image_id = metadata["Id"]
    owner = "mergerail-stage-a-" + uuid.uuid4().hex
    report: dict[str, Any] = {
        "schema": 1,
        "release_ready": False,
        "host": platform.system(),
        "architecture": arch,
        "engine": info["ServerVersion"],
        "storage_driver": info["Driver"],
        "engine_memory_bytes": info["MemTotal"],
        "engine_cpu_count": info["NCPU"],
        "image_id": image_id,
        "image_digests": metadata.get("RepoDigests", []),
        "owner": owner,
        "probes": [],
        "unverified": [
            "persistent workspace/cache hard quota and crash recovery",
            "AI authorization and controlled egress",
            "CLI -> tests -> reviewer sequence and immutable Git delivery",
            "memory/PID stress enforcement and production eco budget",
            "idle lifecycle of the production executor",
            "cold pull, peak Docker VM RAM, native baseline and warm task overhead",
        ],
    }
    names: list[str] = []
    volumes: list[str] = []

    def create(script: str, *extra: str) -> tuple[str, subprocess.CompletedProcess[str]]:
        name = f"{owner}-{len(names)}"
        names.append(name)
        args = [
            "create", "--pull=never", "--name", name,
            "--label", f"io.mergerail.stage-a={owner}",
            "--network=none", "--read-only", "--user=65534:65534",
            "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
            "--cpus=0.5", "--memory=64m", "--memory-swap=64m", "--pids-limit=32",
            "--shm-size=1m", "--stop-signal=SIGTERM", "--log-driver=local",
            "--log-opt=max-size=1m", "--log-opt=max-file=1", "--log-opt=compress=false",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=8m,mode=1777",
            "--entrypoint=/usr/bin/env",
        ]
        # Shadow image-declared volumes; otherwise Docker silently allocates them.
        for path in metadata["Config"].get("Volumes") or {}:
            if path != "/tmp":
                args.extend(["--tmpfs", f"{path}:ro,noexec,nosuid,nodev,size=1m"])
        args.extend([
            *extra, image_id, "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/tmp",
            "/bin/sh", "-c", script,
        ])
        return name, docker(*args, check=False)

    def probe(title: str, script: str, *, exit_code: int = 0, oom: bool = False) -> None:
        started = time.monotonic()
        name, created = create(script)
        if created.returncode:
            raise RuntimeError(created.stderr.strip())
        result = docker("start", "--attach", name, check=False)
        state = json.loads(docker("inspect", name).stdout)[0]
        report["probes"].append({
            "name": title,
            "passed": (
                result.returncode == exit_code
                and state["State"]["ExitCode"] == exit_code
                and state["State"]["OOMKilled"] == oom
            ),
            "seconds": round(time.monotonic() - started, 3),
            "stdout": result.stdout.strip(), "stderr": result.stderr.strip(),
            "exit_code": state["State"]["ExitCode"],
            "oom_killed": state["State"]["OOMKilled"],
        })
        docker("rm", name)

    try:
        probe("effective_kernel_limits", """
set -eu
test "$(cat /sys/fs/cgroup/memory.max)" = 67108864
test "$(cat /sys/fs/cgroup/memory.swap.max)" = 0
test "$(cat /sys/fs/cgroup/pids.max)" = 32
test "$(cat /sys/fs/cgroup/cpu.max)" = '50000 100000'
test "$(id -u)" = 65534
grep -E '^(CapEff|NoNewPrivs|Seccomp):' /proc/self/status
grep -q '^CapEff:[[:space:]]*0000000000000000$' /proc/self/status
grep -q '^NoNewPrivs:[[:space:]]*1$' /proc/self/status
grep -q '^Seccomp:[[:space:]]*2$' /proc/self/status
""")
        with tempfile.TemporaryDirectory(prefix="mergerail-stage-a-") as temporary:
            sentinel = Path(temporary) / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            # The generated path contains no shell metacharacters except possible quotes.
            quoted = "'" + str(sentinel).replace("'", "'\\''") + "'"
            probe("no_host_mount_or_active_external_interface", f"""
set -eu
test ! -e {quoted}
test ! -e /var/run/docker.sock
test ! -e /run/host-services/ssh-auth.sock
cat /proc/net/route
test "$(wc -l < /proc/net/route)" = 1
for device in /sys/class/net/*; do
    test -d "$device" || continue
    test "$(basename "$device")" = lo && continue
    flags=$(cat "$device/flags")
    echo "$(basename "$device") flags=$flags"
    test "$((flags & 1))" = 0
done
ln -s {quoted} /tmp/escape
if echo modified > /tmp/escape; then exit 1; fi
if touch /etc/mergerail-stage-a-sentinel; then exit 1; fi
""")
            if sentinel.read_text(encoding="utf-8") != "unchanged":
                raise RuntimeError("host sentinel was changed")
        probe("tmpfs_enospc_not_persistent_quota", """
set -eu
if dd if=/dev/zero of=/tmp/fill bs=1048576 count=9 2>/dev/shm/dd-error; then exit 1; fi
cat /dev/shm/dd-error
grep -q 'No space left on device' /dev/shm/dd-error
test "$(stat -c %s /tmp/fill)" -le 8388608
stat -c 'written_bytes=%s' /tmp/fill
""")
        probe("cpu_throttled_under_load", """
set -eu
(while :; do :; done) & a=$!
(while :; do :; done) & b=$!
sleep 3
kill "$a" "$b"
wait "$a" 2>/dev/null || :
wait "$b" 2>/dev/null || :
cat /sys/fs/cgroup/cpu.stat
awk '$1 == "nr_throttled" { if ($2 > 0) ok=1 } END { exit !ok }' /sys/fs/cgroup/cpu.stat
cat /sys/fs/cgroup/memory.peak
""")
        if stress:
            probe(
                "memory_oom_enforced",
                "exec python3 -I -c 'x=bytearray(128*1024*1024); print(len(x))'",
                exit_code=137, oom=True,
            )
            probe("pid_limit_enforced", """
exec python3 -I - <<'PY'
import errno
import json
import os
import time

children = []
try:
    for _ in range(40):
        try:
            pid = os.fork()
        except OSError as error:
            events = open('/sys/fs/cgroup/pids.events').read()
            print(json.dumps({'errno': error.errno, 'children': len(children), 'events': events}))
            assert error.errno == errno.EAGAIN and len(children) < 40
            assert int(events.split()[1]) > 0
            break
        if pid == 0:
            time.sleep(15)
            os._exit(0)
        children.append(pid)
    else:
        raise RuntimeError('PID limit not enforced')
finally:
    for pid in children:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
PY
""")
            if all(p["passed"] for p in report["probes"][-2:]):
                report["unverified"].remove(
                    "memory/PID stress enforcement and production eco budget"
                )
                report["unverified"].append("production eco budget (stress uses 64 MiB / 32 PIDs)")
        name, created = create("trap '' TERM INT; sleep 300 & wait")
        if created.returncode:
            raise RuntimeError(created.stderr.strip())
        docker("start", name)
        before = docker("top", name, "-eo", "pid,ppid,comm").stdout
        started = time.monotonic()
        docker("stop", "--time=1", name)
        state = json.loads(docker("inspect", name).stdout)[0]["State"]
        top = docker("top", name, check=False)
        report["probes"].append({
            "name": "cancel_whole_container",
            "passed": not state["Running"] and top.returncode != 0,
            "seconds": round(time.monotonic() - started, 3),
            "processes_before_stop": before.strip(), "state_after_stop": state,
        })
        docker("rm", name)
        name, quota = create("exit 0", "--storage-opt=size=16m")
        report["storage_opt_probe"] = {
            "accepted": quota.returncode == 0,
            "error": quota.stderr.strip(),
            "persistent_quota_verified": False,
        }
        # Creation alone does not prove enforcement, even if the driver accepts it.
        if quota.returncode == 0:
            docker("rm", name)
        volume = f"{owner}-quota"
        volumes.append(volume)
        quota = docker(
            "volume", "create", "--driver=local", "--label", f"io.mergerail.stage-a={owner}",
            "--opt=size=16m", volume, check=False,
        )
        report["local_volume_size_option"] = {
            "accepted": quota.returncode == 0, "error": quota.stderr.strip(),
            "persistent_quota_verified": False,
        }
        if quota.returncode == 0:
            docker("volume", "rm", volume)
    finally:
        for name in names:
            found = docker("inspect", name, check=False)
            if found.returncode:
                if "no such" not in found.stderr.lower():
                    raise RuntimeError(f"cleanup unverifiable for {name}: {found.stderr.strip()}")
                continue
            labels = json.loads(found.stdout)[0]["Config"].get("Labels") or {}
            if labels.get("io.mergerail.stage-a") != owner:
                raise RuntimeError(f"refusing cleanup of container without ownership label: {name}")
            docker("rm", "--force", name)
        for volume in volumes:
            found = docker("volume", "inspect", volume, check=False)
            if found.returncode:
                if "no such" not in found.stderr.lower():
                    raise RuntimeError(f"cleanup unverifiable for {volume}: {found.stderr.strip()}")
                continue
            labels = json.loads(found.stdout)[0].get("Labels") or {}
            if labels.get("io.mergerail.stage-a") != owner:
                raise RuntimeError(f"refusing cleanup of volume without ownership label: {volume}")
            docker("volume", "rm", volume)
    remaining = docker("ps", "-aq", "--filter", f"label=io.mergerail.stage-a={owner}").stdout
    remaining_volumes = docker(
        "volume", "ls", "-q", "--filter", f"label=io.mergerail.stage-a={owner}",
    ).stdout
    report["cleanup_verified"] = not remaining.strip() and not remaining_volumes.strip()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="already installed trusted probe image")
    parser.add_argument("--stress", action="store_true", help="test OOM/PIDs; image needs python3")
    args = parser.parse_args()
    try:
        report = experiment(args.image, stress=args.stress)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        print(json.dumps({"release_ready": False, "error": str(error)}, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    if not report["cleanup_verified"] or any(not p["passed"] for p in report["probes"]):
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
