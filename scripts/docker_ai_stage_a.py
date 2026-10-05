"""Real keyless OpenCode request through the Stage A fixed-origin gateway.

Uses the installed default model catalogue entry without --model or changes to
the host configuration. No credential store is read or mounted. This proves only
the current free-provider path, not production execution or OAuth support.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

PYTHON = "python@sha256:e2a5fce94bd761967528a12f16d707c2613e1522f3f2d77fa45766f45962547f"
OPENCODE = (
    "ghcr.io/anomalyco/opencode@sha256:"
    "d654ecb68ae52ae3abcc56a2a07ff25647e33c38d5a7fc880c5ee20ee54c7d30"
)
LABEL = "io.mergerail.ai-stage-a"


def docker(*args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["docker", *args], capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result


def experiment() -> dict[str, Any]:
    if os.environ.get("DOCKER_HOST"):
        raise ValueError("DOCKER_HOST override is unsupported")
    context = json.loads(docker("context", "inspect").stdout)[0]
    if not context["Endpoints"]["docker"]["Host"].startswith("unix://"):
        raise ValueError("local Docker context required")
    info = json.loads(docker("info", "--format", "{{json .}}").stdout)
    if info["Architecture"] not in {"aarch64", "arm64"} or info["CgroupVersion"] != "2":
        raise ValueError("native arm64 Linux cgroup v2 engine required")
    for reference in (PYTHON, OPENCODE):
        image = json.loads(docker("image", "inspect", reference).stdout)[0]
        if (image["Architecture"] != "arm64" or image["Os"] != "linux"
                or image["Config"].get("Volumes")):
            raise ValueError("unexpected probe image architecture or declared volume")
    home = Path.home()
    model = json.loads((home / ".config/opencode/opencode.json").read_text())["model"]
    provider, model_id = model.split("/", 1)
    if provider != "opencode":
        raise ValueError("only the current OpenCode Zen keyless path is under test")
    catalogue = json.loads((home / ".cache/opencode/models.json").read_text())[provider]
    selected = catalogue["models"][model_id]
    if selected["cost"]["input"] != 0 or selected["cost"]["output"] != 0:
        raise ValueError("configured model is not free; refusing to substitute another model")
    clean_catalogue = {key: catalogue[key] for key in ("id", "name", "env", "npm", "api")}
    clean_catalogue["models"] = {model_id: selected}
    payload = json.dumps({provider: clean_catalogue}).encode()
    if len(payload) > 1024 * 1024:
        raise ValueError("model catalogue exceeds probe input budget")
    owner = "mergerail-ai-stage-a-" + uuid.uuid4().hex[:12]
    networks: list[str] = []
    containers: list[str] = []
    report = {"schema": 1, "release_ready": False, "owner": owner, "model": model,
              "credentials": "none", "cleanup": {"verified": False, "errors": []}}
    started = time.monotonic()

    def create(name: str, network: str, image: str, cpu: str, memory: str, pids: str,
               temporary: str, argv: list[str], extra: list[str] | None = None) -> None:
        containers.append(name)
        docker("create", "--name", name, "--label", f"{LABEL}={owner}", "--pull=never",
               "--network", network, "--read-only", "--user=65534:65534", "--cap-drop=ALL",
               "--security-opt=no-new-privileges:true", "--cpus", cpu, "--memory", memory,
               "--memory-swap", memory, "--pids-limit", pids, "--shm-size=1m",
               "--tmpfs", f"/tmp:rw,nosuid,nodev,size={temporary},mode=1777",
               "--log-driver=local", "--log-opt=max-size=1m", "--log-opt=max-file=1",
               "--log-opt=compress=false", "--entrypoint=/usr/bin/env",
               *(extra or []), image, "-i", "PATH=/usr/local/bin:/usr/bin:/bin", *argv)

    try:
        internal, external = owner + "-internal", owner + "-external"
        docker("network", "create", "--internal", "--ipv6=false", "--opt",
               "com.docker.network.bridge.gateway_mode_ipv4=isolated",
               "--label", f"{LABEL}={owner}", internal)
        networks.append(internal)
        docker("network", "create", "--ipv6=false", "--label", f"{LABEL}={owner}", external)
        networks.append(external)
        net = json.loads(docker("network", "inspect", internal).stdout)[0]
        subnet = net["IPAM"]["Config"][0]["Subnet"]
        address = str(ipaddress.ip_network(subnet).network_address + 2)
        gateway = owner + "-gateway"
        code = (Path(__file__).parent / "docker_ai_gateway.py").read_text()
        create(gateway, internal, PYTHON, "0.1", "128m", "32", "8m",
               ["/usr/local/bin/python3", "-I", "-B", "-c", code,
                "--upstream-host", "opencode.ai", "--upstream-prefix", "/zen/v1",
                "--bind", address, "--port", "8080"],
               ["--ip", address, "--sysctl=net.ipv4.ip_forward=0"])
        docker("network", "connect", external, gateway)
        docker("start", gateway)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            logs = docker("logs", gateway, check=False)
            if b"listening on" in logs.stderr:
                break
            state = json.loads(docker("inspect", gateway).stdout)[0]["State"]
            if not state["Running"]:
                raise RuntimeError(
                    "gateway startup failed: " + logs.stderr.decode(errors="replace")
                )
            time.sleep(0.5)
        else:
            raise RuntimeError("gateway did not become ready within its startup deadline")
        client = owner + "-client"
        config = {"snapshot": False, "share": "disabled", "plugin": [], "mcp": {},
                  "permission": {"*": "deny"},
                  "provider": {provider: {"options": {"baseURL": f"http://{address}:8080/zen/v1"}}}}
        env = {"HOME": "/tmp/home", "XDG_DATA_HOME": "/tmp/data", "XDG_CACHE_HOME": "/tmp/cache",
               "XDG_CONFIG_HOME": "/tmp/config", "XDG_STATE_HOME": "/tmp/state", "TMPDIR": "/tmp",
               "OPENCODE_CONFIG_DIR": "/tmp/config", "OPENCODE_MODELS_PATH": "/tmp/models.json",
               "OPENCODE_CONFIG_CONTENT": json.dumps(config), "OPENCODE_PURE": "1",
               "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1",
               "OPENCODE_DISABLE_MODELS_FETCH": "1",
               "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "1"}
        script = """set -eu
cat > /tmp/models.json
mkdir -p /tmp/work
cd /tmp/work
opencode run --format json 'Reply exactly MERGERAIL_STAGE_A_OK. Do not use tools.'
cat /sys/fs/cgroup/memory.peak
"""
        create(client, internal, OPENCODE, "1", "2048m", "128", "128m",
               [*[f"{key}={value}" for key, value in env.items()], "/bin/sh", "-c", script],
               ["--interactive", "--dns=127.0.0.1", "--dns-option=timeout:1",
                "--dns-option=attempts:1"])

        def log_bound() -> None:
            if sys.platform == "win32":
                raise RuntimeError("this probe requires a POSIX host")
            resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))

        before = time.monotonic()
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            outcome = subprocess.run(["docker", "start", "--attach", "--interactive", client],
                                     input=payload, stdout=stdout, stderr=stderr, timeout=180,
                                     preexec_fn=log_bound)
            stdout.seek(0)
            stderr.seek(0)
            report["client_stdout"] = stdout.read().decode(errors="replace")
            report["client_stderr"] = stderr.read().decode(errors="replace")
            report["exit_code"] = outcome.returncode
        report["request_seconds"] = round(time.monotonic() - before, 3)
        report["authorized_response"] = outcome.returncode == 0 and any(
            json.loads(line).get("type") == "text"
            and "MERGERAIL_STAGE_A_OK" in json.loads(line).get("part", {}).get("text", "")
            for line in report["client_stdout"].splitlines() if line.startswith("{"))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    finally:
        for name in reversed(containers):
            try:
                found = docker("inspect", name, check=False)
                if found.returncode:
                    if b"no such" not in found.stderr.lower():
                        raise RuntimeError("container absence unverifiable: " + name)
                    continue
                data = json.loads(found.stdout)[0]
                if data["Config"]["Labels"].get(LABEL) != owner:
                    raise RuntimeError("container ownership mismatch")
                if name.endswith("-gateway"):
                    logs = docker("logs", name, check=False)
                    report["gateway_logs"] = logs.stderr.decode(errors="replace")
                docker("rm", "--force", name)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                report["cleanup"]["errors"].append(str(error))
        for name in reversed(networks):
            try:
                data = json.loads(docker("network", "inspect", name).stdout)[0]
                if data["Labels"].get(LABEL) != owner:
                    raise RuntimeError("network ownership mismatch")
                docker("network", "rm", name)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                report["cleanup"]["errors"].append(str(error))
        report["cleanup"]["verified"] = not report["cleanup"]["errors"]
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = experiment()
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report.get("authorized_response") and report["cleanup"]["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
