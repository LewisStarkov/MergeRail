"""Run selected Python tests in a bounded, networkless Stage A container.

Only explicit source files and installed pure-Python pytest modules are copied
over stdin. No host bind mounts, dependency installation, or host test execution.
This development harness is not MergeRail's production executor.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import resource
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

IMAGE = (
    "ghcr.io/anomalyco/build/base@sha256:"
    "f36badd57c0aad8a168e4265a67d9e0dcb3f07a13189df637ae70d3656ddbf72"
)
LABEL = "io.mergerail.stage-a-tests"
LIMIT = 32 * 1024 * 1024
BOOTSTRAP = """
import base64, json, os, pathlib, sys
payload = json.loads(sys.stdin.buffer.read(32*1024*1024+1))
root = pathlib.Path('/tmp/check')
for name, encoded in payload['files'].items():
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise RuntimeError('unsafe input path')
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(base64.b64decode(encoded, validate=True))
os.chdir(root)
sys.path[:0] = [str(root/'deps'), str(root/'src'), str(root)]
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
import pytest
result = pytest.main(['-q', '-o', 'addopts=', '-p', 'no:cacheprovider', *payload['tests']])
print('cgroup_memory_peak=' + pathlib.Path('/sys/fs/cgroup/memory.peak').read_text().strip())
raise SystemExit(result)
"""


def run(root: Path, tests: list[str], site: Path) -> dict[str, Any]:
    files: dict[str, str] = {}
    total = 0

    def add(path: Path, name: str) -> None:
        nonlocal total
        content = path.read_bytes()
        total += len(content)
        if total > LIMIT // 2:
            raise ValueError("source transfer exceeds 16 MiB")
        files[name] = base64.b64encode(content).decode()

    for folder in ("src", "scripts", "tests"):
        for path in sorted((root / folder).rglob("*.py")):
            add(path, path.relative_to(root).as_posix())
    for path in sorted((root / "src/mergerail/fronts/web_assets").rglob("*")):
        if path.is_file() and path.suffix in {".html", ".js", ".css", ".json"}:
            add(path, path.relative_to(root).as_posix())
    for package in ("pytest", "_pytest", "pluggy", "iniconfig", "packaging", "pygments"):
        directory = site / package
        if not directory.is_dir():
            raise ValueError(f"missing installed pure-Python test dependency: {directory}")
        for path in sorted(directory.rglob("*.py")):
            add(path, "deps/" + path.relative_to(site).as_posix())
    add(site / "py.py", "deps/py.py")
    inputs = {"files": files, "tests": tests}
    payload = json.dumps(inputs).encode()
    if len(payload) > LIMIT:
        raise ValueError("encoded source transfer exceeds 32 MiB")
    if os.environ.get("DOCKER_HOST"):
        raise ValueError("DOCKER_HOST override is unsupported")
    context = json.loads(subprocess.check_output(["docker", "context", "inspect"], timeout=30))[0]
    if not context["Endpoints"]["docker"]["Host"].startswith("unix://"):
        raise ValueError("local Docker context required")
    inspected = subprocess.check_output(["docker", "image", "inspect", IMAGE], timeout=30)
    metadata = json.loads(inspected)[0]
    if metadata["Architecture"] != "arm64" or metadata["Config"].get("Volumes"):
        raise ValueError("harness requires the pinned native arm64 image without volumes")
    name = "mergerail-stage-a-tests-" + uuid.uuid4().hex[:12]
    argv = ["docker", "create", "--interactive", "--name", name, "--label", f"{LABEL}={name}",
            "--pull=never", "--read-only", "--network=none", "--user=65534:65534",
            "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--cpus=1",
            "--memory=512m", "--memory-swap=512m", "--pids-limit=128", "--shm-size=1m",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=128m,mode=1777",
            "--log-driver=local", "--log-opt=max-size=10m", "--log-opt=max-file=1",
            "--log-opt=compress=false", "--entrypoint=/usr/bin/env", metadata["Id"],
            "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1",
            "/usr/bin/python3", "-I", "-B", "-c", BOOTSTRAP]
    started = time.monotonic()
    report = {"image": IMAGE, "source_bytes": total, "transfer_bytes": len(payload),
              "tests": tests, "container": name, "cleanup_verified": False}

    def bound_cli_output() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))

    try:
        subprocess.run(argv, capture_output=True, check=True, timeout=30)
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            result = subprocess.run(["docker", "start", "--attach", "--interactive", name],
                                    input=payload, stdout=stdout, stderr=stderr, timeout=180,
                                    preexec_fn=bound_cli_output)
            stdout.seek(0)
            stderr.seek(0)
            report.update(
                exit_code=result.returncode, stdout=stdout.read().decode(errors="replace"),
                stderr=stderr.read().decode(errors="replace"),
            )
        inspected = subprocess.check_output(["docker", "inspect", name], timeout=30)
        state = json.loads(inspected)[0]["State"]
        report["container_state"] = state
    finally:
        found = subprocess.run(["docker", "inspect", name], capture_output=True, timeout=30)
        if found.returncode == 0:
            metadata = json.loads(found.stdout)[0]
            if metadata["Config"]["Labels"].get(LABEL) != name:
                raise RuntimeError("refusing cleanup without matching ownership")
            subprocess.run(
                ["docker", "rm", "--force", name], capture_output=True, check=True, timeout=30
            )
            report["cleanup_verified"] = True
        elif b"no such" in found.stderr.lower():
            report["cleanup_verified"] = True
        else:
            raise RuntimeError("container cleanup could not be verified")
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("tests", nargs="+")
    args = parser.parse_args()
    report = run(args.root.resolve(), args.tests, args.site.resolve())
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(report.get("stdout", "")[-16000:])
    print(report.get("stderr", "")[-4000:])
    print(json.dumps({k: v for k, v in report.items() if k not in {"stdout", "stderr"}}))
    return 0 if report["exit_code"] == 0 and report["cleanup_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
