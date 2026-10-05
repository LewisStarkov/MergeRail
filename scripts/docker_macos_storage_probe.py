"""Synthetic macOS hard-quota experiment, not a production storage backend.

Creates one 64 MiB journaled, case-sensitive disk image, binds only its verified
mount, fills it from a constrained container, then verifies persistence after
detach/reattach. No host project commands, secrets, sudo, or global settings.
Uncertain cleanup retains the image. Exit 0 proves only this experiment.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

IMAGE = "postgres@sha256:47f917f7409eacd22fc5dfb1dee634e1b55cf0c01d1a7eb701be2227a03e0641"
LABEL = "io.mergerail.macos-storage-probe"
CAPACITY = 64 * 1024 * 1024


def command(
    *argv: str, timeout: int = 30, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(argv, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result


def attached(image: Path) -> list[dict[str, Any]]:
    info = plistlib.loads(command("hdiutil", "info", "-plist").stdout)
    return [entry for entry in info.get("images", [])
            if Path(entry.get("image-path", "")).resolve() == image.resolve()]


def experiment() -> dict[str, Any]:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("this probe has only been prepared for macOS arm64")
    if os.environ.get("DOCKER_HOST"):
        raise RuntimeError("DOCKER_HOST override is unsupported")
    context = json.loads(command("docker", "context", "inspect").stdout)[0]
    if not context["Endpoints"]["docker"]["Host"].startswith("unix://"):
        raise RuntimeError("local Docker context required")
    engine = json.loads(command("docker", "info", "--format", "{{json .}}").stdout)
    metadata = json.loads(command("docker", "image", "inspect", IMAGE).stdout)[0]
    if (engine["OSType"] != "linux" or engine["CgroupVersion"] != "2"
            or engine["Architecture"] not in {"arm64", "aarch64"}
            or metadata["Os"] != "linux" or metadata["Architecture"] != "arm64"):
        raise RuntimeError("native Linux arm64 image/engine and cgroup v2 required")
    for key in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"):
        if not engine.get(key):
            raise RuntimeError(f"missing engine capability: {key}")
    base = Path.home() / "Library/Caches/mergerail-macos-storage-probe"
    if base.is_symlink():
        raise RuntimeError("storage probe base must not be a symlink")
    base.mkdir(parents=True, exist_ok=True)
    if any(base.glob("probe-*/quota.dmg")):
        raise RuntimeError("a prior storage probe is retained; refusing another allocation")
    if shutil.disk_usage(base).free < CAPACITY * 4:
        raise RuntimeError("insufficient free disk space for the bounded probe")
    directory = Path(tempfile.mkdtemp(prefix="probe-", dir=base))
    owner = "mergerail-quota-" + uuid.uuid4().hex[:16]
    token = uuid.uuid4().hex
    (directory / "owner").write_text(owner)
    image = directory / "quota.dmg"
    containers: list[str] = []
    mount: Path | None = None
    report = {"schema": 1, "release_ready": False, "owner": owner,
              "host": "macOS arm64", "engine": engine["ServerVersion"],
              "capacity_bytes": CAPACITY, "image": IMAGE, "probes": [],
              "cleanup": {"verified": False, "errors": []}}
    started = time.monotonic()

    def verify_mount(destination: Path) -> None:
        entries = attached(image)
        points = [e.get("mount-point") for item in entries
                  for e in item.get("system-entities", [])]
        if str(destination) not in points or destination.is_symlink():
            raise RuntimeError("owned disk image mount identity was lost")
        if destination.stat().st_dev == directory.stat().st_dev:
            raise RuntimeError("bind source is on the host filesystem, not the disk image")
        stats = os.statvfs(destination)
        capacity = stats.f_blocks * stats.f_frsize
        if not CAPACITY - 1024 * 1024 <= capacity <= CAPACITY:
            raise RuntimeError(f"unexpected mounted capacity: {capacity}")

    def attach(number: int) -> Path:
        destination = directory / f"mount-{number}"
        destination.mkdir()
        response = command("hdiutil", "attach", str(image), "-nobrowse", "-noautoopen",
                           "-mountpoint", str(destination), "-plist", timeout=60)
        entities = plistlib.loads(response.stdout)["system-entities"]
        matches = [e for e in entities if e.get("mount-point") == str(destination)]
        if len(matches) != 1 or not matches[0].get("dev-entry"):
            raise RuntimeError("hdiutil did not confirm the exact owned mountpoint")
        verify_mount(destination)
        return destination

    def detach() -> None:
        for entry in attached(image):
            entities = entry.get("system-entities", [])
            devices = [e["dev-entry"] for e in entities if e.get("dev-entry")]
            if not devices:
                raise RuntimeError("cannot identify owned attached disk device")
            command("hdiutil", "detach", devices[0], timeout=60)
        if attached(image):
            raise RuntimeError("owned image remains attached")

    def run(script: str) -> dict[str, Any]:
        assert mount is not None
        verify_mount(mount)
        if (mount / "token").read_text() != token:
            raise RuntimeError("disk token changed before bind")
        name = f"{owner}-{len(containers)}"
        containers.append(name)
        args = ["docker", "create", "--name", name, "--label", f"{LABEL}={owner}",
                "--pull=never", "--network=none", "--read-only", "--user=65534:65534",
                "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--cpus=0.5",
                "--memory=64m", "--memory-swap=64m", "--pids-limit=32", "--shm-size=1m",
                "--stop-signal=SIGTERM", "--log-driver=local", "--log-opt=max-size=1m",
                "--log-opt=max-file=1", "--log-opt=compress=false",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=8m,mode=1777",
                "--entrypoint=/usr/bin/env", "--mount",
                f"type=bind,src={mount},dst=/workspace"]
        for path in metadata["Config"].get("Volumes") or {}:
            args.extend(["--tmpfs", f"{path}:ro,noexec,nosuid,nodev,size=1m"])
        args.extend([metadata["Id"], "-i", "PATH=/usr/bin:/bin", "HOME=/tmp",
                     "/bin/sh", "-c", script])
        command(*args)
        configuration = json.loads(command("docker", "inspect", name).stdout)[0]
        if any(m["Type"] == "volume" for m in configuration["Mounts"]):
            raise RuntimeError("unbounded Docker volume appeared in the probe")
        binds = [m for m in configuration["Mounts"] if m["Type"] == "bind"]
        if (len(binds) != 1 or binds[0]["Source"] != str(mount)
                or binds[0]["Destination"] != "/workspace"):
            raise RuntimeError("unexpected bind mounts")
        before = time.monotonic()
        result = command("docker", "start", "--attach", name, timeout=90, check=False)
        state = json.loads(command("docker", "inspect", name).stdout)[0]["State"]
        record = {"seconds": round(time.monotonic() - before, 3),
                  "stdout": result.stdout.decode(errors="replace"),
                  "stderr": result.stderr.decode(errors="replace"),
                  "exit_code": state["ExitCode"], "oom_killed": state["OOMKilled"]}
        report["probes"].append(record)
        if result.returncode or state["Running"] or state["ExitCode"] or state["OOMKilled"]:
            raise RuntimeError("bounded storage container failed; see probe output")
        command("docker", "rm", name)
        return record

    try:
        command("hdiutil", "create", "-size", "64m", "-fs", "Case-sensitive Journaled HFS+",
                "-type", "UDIF", "-volname", "MergeRailQuotaProbe", "-nospotlight",
                str(image), timeout=60)
        if image.stat().st_size != CAPACITY:
            raise RuntimeError("disk image is not fixed at the requested size")
        mount = attach(1)
        mount.chmod(0o777)
        (mount / "token").write_text(token)
        run("""set -eu
test "$(id -u)" = 65534
test "$(cat /sys/fs/cgroup/memory.max)" = 67108864
test "$(cat /sys/fs/cgroup/memory.swap.max)" = 0
test "$(cat /sys/fs/cgroup/pids.max)" = 32
test "$(cat /sys/fs/cgroup/cpu.max)" = '50000 100000'
test ! -e /var/run/docker.sock
cd /workspace
touch case Case
test "$(find . -maxdepth 1 -name '*ase' | wc -l)" = 2
if dd if=/dev/zero of=fill bs=1048576 count=72 2>/tmp/dd-error; then exit 20; fi
cat /tmp/dd-error
grep -q 'No space left on device' /tmp/dd-error
written=$(stat -c %s fill)
echo written_bytes=$written
test "$written" -le 67108864
test "$written" -ge 33554432
rm fill
cp token marker
sync
cat /sys/fs/cgroup/memory.peak
""")
        if image.stat().st_size != CAPACITY:
            raise RuntimeError("disk image grew beyond the hard bound")
        report["image_allocated_bytes"] = image.stat().st_blocks * 512
        if (mount / "marker").read_text() != token:
            raise RuntimeError("container marker missing from the image")
        detach()
        mount = attach(2)
        reread = run("set -eu; test ! -e /workspace/fill; cat /workspace/marker")
        if reread["stdout"] != token:
            raise RuntimeError("marker did not survive detach and reattach")
        report["quota_and_persistence_verified"] = True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    finally:
        errors = report["cleanup"]["errors"]
        for name in containers:
            try:
                found = command("docker", "inspect", name, check=False)
                if found.returncode:
                    if b"no such" not in found.stderr.lower():
                        raise RuntimeError("container absence cannot be verified: " + name)
                    continue
                data = json.loads(found.stdout)[0]
                if data["Config"]["Labels"].get(LABEL) != owner:
                    raise RuntimeError("refusing removal of an unowned container: " + name)
                command("docker", "rm", "--force", name)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                errors.append(str(error))
        try:
            if errors:
                raise RuntimeError("image retained because container cleanup is uncertain")
            detach()
            if (directory / "owner").read_text() != owner:
                raise RuntimeError("run directory ownership changed")
            if any(child.is_mount() for child in directory.iterdir()):
                raise RuntimeError("run directory still contains a mount")
            shutil.rmtree(directory)
            report["cleanup"]["verified"] = True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(str(error))
            report["cleanup"]["retained_path"] = str(directory)
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = experiment()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report = {"release_ready": False, "error": str(error)}
    payload = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.write_text(payload)
    print(payload, end="")
    passed = report.get("quota_and_persistence_verified") and report["cleanup"]["verified"]
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
