"""Bounded Stage A network-isolation probe; it never enables MergeRail's protected mode.

A stdlib host controller drives fixed synthetic Python fixture containers on a uniquely
labelled internal Docker bridge with an isolated IPv4 gateway and IPv6 disabled, using an
already installed Linux image passed by immutable digest. It proves container-to-container
reachability, the absence of an IPv4 default route, refusal of public, cloud-metadata and
host sentinel destinations, and refusal of external DNS resolution. No pulls, builds, bind
mounts, credentials, host sockets, project commands, or fallback to an ordinary bridge or to
host execution.
Exit 2 means every check passed but the release gate stays blocked; 1 means an error, an
unverifiable cleanup, or a failed check. 0 is never returned.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import socket
import subprocess
import threading
import time
import uuid
from typing import Any

DEFAULT_IMAGE = "python@sha256:e2a5fce94bd761967528a12f16d707c2613e1522f3f2d77fa45766f45962547f"
DEFAULT_PYTHON = "/usr/local/bin/python3"
LABEL = "io.mergerail.network-stage-a"
GATEWAY_MODE = "isolated"
FIXTURE_PORT = 8080
FIXTURE_MARKER = "MERGERAIL_STAGE_A_NETWORK_FIXTURE_LISTENING"
SENTINEL_HOST = "host.docker.internal"
DNS_SERVER = "127.0.0.1"
DNS_OPTIONS = ("timeout:1", "attempts:1")
SENTINEL_HOST_ENTRY = f"{SENTINEL_HOST}:host-gateway"
FIXTURE_BODY = b"MERGERAIL_STAGE_A_NETWORK_FIXTURE_OK\n"
FIXTURE_RESPONSE = (
    b"HTTP/1.0 200 OK\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Length: " + str(len(FIXTURE_BODY)).encode("ascii") + b"\r\n"
    b"Connection: close\r\n"
    b"\r\n" + FIXTURE_BODY
)
HARDENING = (
    "--pull=never",
    "--read-only",
    "--user=65534:65534",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges:true",
    "--cpus=0.5",
    "--memory=64m",
    "--memory-swap=64m",
    "--pids-limit=32",
    "--shm-size=1m",
    "--log-driver=local",
    "--log-opt=max-size=1m",
    "--log-opt=max-file=1",
    "--log-opt=compress=false",
    "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=8m,mode=1777",
    "--stop-signal=SIGTERM",
)
RECORDED_LIMITS = {
    "Memory": 67108864,
    "MemorySwap": 67108864,
    "NanoCpus": 500000000,
    "PidsLimit": 32,
    "ShmSize": 1048576,
    "ReadonlyRootfs": True,
    "CapDrop": ["ALL"],
}
RECORDED_TMPFS = {"rw", "noexec", "nosuid", "nodev"}
RECORDED_TMPFS_SIZE = {"8388608", "8m"}
UNVERIFIED = [
    "controlled AI egress gateway, redirect checks and provider authorization",
    "loopback-only publication of preview ports",
    "remote Docker contexts, several engines and parallel heavy stages",
    "Linux and Windows/WSL hosts, amd64 images and CPU/RAM/PID stress enforcement",
    "project CLI -> tests -> reviewer sequence and Git bundle delivery",
    "execution/docker.py implementation; protected mode is not started",
]

FIXTURE_TEMPLATE = r'''
import base64, json, signal, socket, time

RESPONSE = base64.b64decode("FIXTURE_RESPONSE_PLACEHOLDER")
LIFETIME = 120.0
MAX_REQUESTS = 4
LISTENING = "MERGERAIL_STAGE_A_NETWORK_FIXTURE_LISTENING"
stopping = []


def stop(_signum, _frame):
    stopping.append(True)


def process():
    fields = {}
    with open("/proc/self/status", encoding="ascii") as handle:
        for line in handle:
            key, _, value = line.partition(":")
            if key in ("Uid", "CapEff", "NoNewPrivs", "Seccomp"):
                fields[key] = value.split()
    return fields


def ipv4_routes():
    with open("/proc/net/route", encoding="ascii") as handle:
        lines = handle.read().splitlines()
    return [line.split() for line in lines[1:]]


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", 8080))
server.listen(8)
server.settimeout(0.5)
print(LISTENING, flush=True)
served = 0
failures = 0
accept_error = None
deadline = time.monotonic() + LIFETIME
while not stopping and served < MAX_REQUESTS and time.monotonic() < deadline:
    try:
        connection = server.accept()[0]
    except socket.timeout:
        continue
    except OSError as failure:
        accept_error = type(failure).__name__ + ": " + str(failure)
        break
    with connection:
        connection.settimeout(5.0)
        try:
            connection.recv(4096)
            connection.sendall(RESPONSE)
            served += 1
        except OSError:
            failures += 1
print(json.dumps({
    "served": served,
    "failures": failures,
    "accept_error": accept_error,
    "stopped": bool(stopping),
    "default_route": any(row[1] == "00000000" for row in ipv4_routes()),
    "process": process(),
}), flush=True)
'''

CLIENT_SOURCE = r'''
import base64, json, socket, sys, threading, time

FIXTURE = (sys.argv[1], int(sys.argv[2]))
SENTINEL = (sys.argv[3], int(sys.argv[4]))
PUBLIC = ("1.1.1.1", 443)
METADATA = ("169.254.169.254", 80)
EXTERNAL_NAME = "opencode.ai"
TIMEOUT = 3.0
ATTEMPTS = 5
DNS_TIMEOUT = 6.0


def process():
    fields = {}
    with open("/proc/self/status", encoding="ascii") as handle:
        for line in handle:
            key, _, value = line.partition(":")
            if key in ("Uid", "CapEff", "NoNewPrivs", "Seccomp"):
                fields[key] = value.split()
    return fields


def ipv4_routes():
    with open("/proc/net/route", encoding="ascii") as handle:
        lines = handle.read().splitlines()
    return [line.split() for line in lines[1:]]


def resolve(host):
    try:
        found = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as failure:
        return None, type(failure).__name__ + ": " + str(failure)
    addresses = sorted({item[4][0] for item in found})
    return (addresses[0] if addresses else None), None


def tcp(address, port, read=0):
    outcome = {"connected": False, "error": None, "received": None, "seconds": 0.0}
    started = time.monotonic()
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(TIMEOUT)
    try:
        connection.connect((address, port))
        outcome["connected"] = True
        connection.sendall(b"GET / HTTP/1.0\r\n\r\n")
        if read:
            chunks = []
            total = 0
            while total < read:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            outcome["received"] = base64.b64encode(b"".join(chunks)).decode("ascii")
    except OSError as failure:
        outcome["error"] = type(failure).__name__ + ": " + str(failure)
    finally:
        connection.close()
        outcome["seconds"] = round(time.monotonic() - started, 3)
    return outcome


def denied(name, port):
    target = {"target": name, "port": port}
    address, error = resolve(name)
    target["resolved"] = address
    target["resolution_error"] = error
    if address is None:
        target["blocked"] = True
        target["blocked_by"] = "name_unresolved"
        return target
    outcome = tcp(address, port)
    outcome["address"] = address
    target.update(outcome)
    target["blocked"] = not outcome["connected"]
    target["blocked_by"] = "connect_failed" if target["blocked"] else "connected"
    return target


def external_dns():
    result = {"name": EXTERNAL_NAME, "resolved": None, "error": None, "timed_out": False}
    finished = threading.Event()
    outcome = {}

    def resolve_name():
        address, error = resolve(EXTERNAL_NAME)
        outcome["address"] = address
        outcome["error"] = error
        finished.set()

    started = time.monotonic()
    threading.Thread(target=resolve_name, daemon=True).start()
    finished.wait(DNS_TIMEOUT)
    result["seconds"] = round(time.monotonic() - started, 3)
    result["timed_out"] = not finished.is_set()
    result["resolved"] = outcome.get("address")
    result["error"] = outcome.get("error")
    result["blocked"] = finished.is_set() and outcome.get("error") is not None
    if result["timed_out"]:
        result["blocked_by"] = "resolver_timeout"
    else:
        result["blocked_by"] = "name_unresolved" if result["blocked"] else "resolved"
    return result


report = {"process": process()}
report["routes"] = ipv4_routes()
report["ipv4_default_route"] = any(row[1] == "00000000" for row in report["routes"])
report["fixture"] = {"attempts": []}
for index in range(ATTEMPTS):
    address, error = resolve(FIXTURE[0])
    attempt = {"attempt": index + 1, "resolved": address, "resolution_error": error}
    if address is not None:
        attempt.update(tcp(address, FIXTURE[1], read=4096))
    report["fixture"]["attempts"].append(attempt)
    if attempt.get("connected"):
        break
    time.sleep(0.5)
report["fixture"]["address"] = FIXTURE[0]
report["fixture"]["port"] = FIXTURE[1]
report["public"] = denied(*PUBLIC)
report["metadata"] = denied(*METADATA)
report["sentinel"] = denied(SENTINEL[0], SENTINEL[1])
report["external_dns"] = external_dns()
with open("/etc/resolv.conf", encoding="ascii") as handle:
    report["nameservers"] = handle.read().split()
print(json.dumps(report), flush=True)
'''


def docker(*args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        reason = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"docker {' '.join(args[:2])}: {reason}")
    return result


class Sentinel:
    """Ephemeral host listener created by this run; the only host service ever contacted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("0.0.0.0", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.2)
        self.port = self._socket.getsockname()[1]
        self.accepted = 0
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                connection = self._socket.accept()[0]
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self.accepted += 1
            connection.close()

    def control(self) -> dict[str, Any]:
        """Positive control, so a negative container result cannot be a broken listener."""
        with self._lock:
            before = self.accepted
        error = None
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=2.0) as connection:
                connection.sendall(b"stage-a-control\n")
        except OSError as failure:
            error = f"{type(failure).__name__}: {failure}"
        deadline = time.monotonic() + 5.0
        while error is None and self.accepted == before and time.monotonic() < deadline:
            time.sleep(0.05)
        reachable = error is None and self.accepted > before
        self._reset()
        return {"bind": "0.0.0.0", "port": self.port, "reachable": reachable, "error": error}

    def _reset(self) -> None:
        with self._lock:
            self.accepted = 0

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._socket.close()


def fixture_source() -> str:
    encoded = base64.b64encode(FIXTURE_RESPONSE).decode("ascii")
    return FIXTURE_TEMPLATE.replace("FIXTURE_RESPONSE_PLACEHOLDER", encoded)


def verify_environment(image: str) -> dict[str, Any]:
    if os.environ.get("DOCKER_HOST"):
        raise RuntimeError("DOCKER_HOST override is unsupported; select a local context")
    context = json.loads(docker("context", "inspect").stdout)[0]
    endpoint = context["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith(("unix://", "npipe://")):
        raise RuntimeError("only a local Docker context is allowed")
    info = json.loads(docker("info", "--format", "{{json .}}").stdout)
    if info["OSType"] != "linux" or info["CgroupVersion"] != "2":
        raise RuntimeError("this probe requires Linux containers and cgroup v2")
    # BridgeNfIptables was removed from GET /info in API 1.50. Prove the
    # isolated gateway with the real reachability checks below instead.
    if int(info["ServerVersion"].split(".")[0]) < 28:
        raise RuntimeError("isolated gateway probe requires Docker Engine 28 or newer")
    for capability in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"):
        if not info.get(capability):
            raise RuntimeError(f"engine does not advertise {capability}")
    metadata = json.loads(docker("image", "inspect", image).stdout)[0]
    aliases = {"aarch64": "arm64", "x86_64": "amd64"}
    arch = aliases.get(info["Architecture"], info["Architecture"])
    host_arch = aliases.get(platform.machine(), platform.machine())
    if metadata["Os"] != "linux" or metadata["Architecture"] != arch or arch != host_arch:
        raise RuntimeError("native matching host/engine/image architecture required")
    digest = image.partition("@")[2]
    if not digest.startswith("sha256:"):
        raise RuntimeError("the probe image must be pinned by digest")
    if not any(entry.endswith(digest) for entry in metadata.get("RepoDigests") or []):
        raise RuntimeError(f"image {image} is not present locally by digest")
    if metadata["Config"].get("Volumes"):
        raise RuntimeError("the pinned image declares volumes; this probe expects none")
    return {
        "host": platform.system(),
        "architecture": arch,
        "engine": info["ServerVersion"],
        "context": context["Name"],
        "endpoint": endpoint,
        "storage_driver": info["Driver"],
        "image": {
            "reference": image,
            "id": metadata["Id"],
            "repo_digests": metadata.get("RepoDigests") or [],
            "os": metadata["Os"],
            "architecture": metadata["Architecture"],
        },
    }


def hardened(fields: dict[str, Any]) -> bool:
    return (
        fields.get("Uid", [])[:1] == ["65534"]
        and fields.get("CapEff") == ["0000000000000000"]
        and fields.get("NoNewPrivs") == ["1"]
        and fields.get("Seccomp") == ["2"]
    )


def recorded_limits(config: dict[str, Any]) -> bool:
    if not all(config.get(key) == value for key, value in RECORDED_LIMITS.items()):
        return False
    if (config.get("LogConfig") or {}).get("Type") != "local":
        return False
    if not any(
        entry.split(":")[0] == "no-new-privileges" for entry in (config.get("SecurityOpt") or [])
    ):
        return False
    raw = (config.get("Tmpfs") or {}).get("/tmp", "")
    options = dict(
        item.split("=", 1) if "=" in item else (item, "") for item in raw.split(",") if item
    )
    return set(options) >= RECORDED_TMPFS and options.get("size") in RECORDED_TMPFS_SIZE


def recorded_dns(config: dict[str, Any]) -> bool:
    return (
        config.get("Dns") == [DNS_SERVER]
        and config.get("DnsOptions") == list(DNS_OPTIONS)
        and SENTINEL_HOST_ENTRY in (config.get("ExtraHosts") or [])
    )


def absent(stderr: str) -> bool:
    lowered = stderr.lower()
    return "no such" in lowered or "not found" in lowered


def parse_json_line(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def wait_for_marker(name: str, marker: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        output = docker("logs", name, check=False)
        if marker in output.stdout or marker in output.stderr:
            return True
        if output.returncode or time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def tail(text: str, limit: int = 400) -> str:
    return text.strip()[-limit:]


def probe(image: str, python_bin: str) -> dict[str, Any]:
    started = time.monotonic()
    owner = "mergerail-network-stage-a-" + uuid.uuid4().hex
    network_name = f"{owner}-net"
    report: dict[str, Any] = {
        "schema": 1,
        "probe": "docker_network_stage_a",
        "release_ready": False,
        "requested_image": image,
        "owner": owner,
        "network_name": network_name,
        "checks": [],
        "phases": {},
        "cleanup": {"containers": [], "network_removed": False, "verified": False, "errors": []},
        "unverified": UNVERIFIED,
    }
    containers: list[str] = []
    networks: list[str] = []
    sentinel: Sentinel | None = None

    def check(name: str, passed: bool, details: dict[str, Any]) -> None:
        report["checks"].append({"name": name, "passed": bool(passed), "details": details})

    def create(
        name: str,
        source: str,
        network: str,
        arguments: tuple[str, ...] = (),
        *extra: str,
    ) -> None:
        containers.append(name)
        docker(
            "create", "--name", name, "--label", f"{LABEL}={owner}", "--network", network,
            *HARDENING, *extra, f"--entrypoint={python_bin}",
            report["image"]["id"], "-I", "-B", "-c", source, *arguments,
        )

    try:
        mark = time.monotonic()
        report.update(verify_environment(image))
        sentinel = Sentinel()
        report["sentinel"] = sentinel.control()
        if not report["sentinel"]["reachable"]:
            raise RuntimeError("the created host sentinel is unreachable; denial checks invalid")
        preflight_name = f"{owner}-preflight"
        # A host-only loopback check cannot prove Docker's host-gateway mapping.
        # This fixed trusted control contacts only the listener we just created.
        create(
            preflight_name,
            "import json,socket,sys; "
            "address=socket.gethostbyname(sys.argv[1]); "
            "connection=socket.create_connection((address,int(sys.argv[2])),timeout=3); "
            "connection.sendall(b'mergerail-positive-control'); connection.close(); "
            "print(json.dumps({'host_address':address}))",
            "bridge",
            (SENTINEL_HOST, str(sentinel.port)),
            f"--add-host={SENTINEL_HOST_ENTRY}",
        )
        preflight = docker("start", "--attach", preflight_name, timeout=60)
        report["interpreter"] = {
            "path": python_bin,
            "output": tail(preflight.stdout, 200),
            "stderr": tail(preflight.stderr, 200),
        }
        if preflight.returncode:
            raise RuntimeError(f"image interpreter {python_bin} unusable: {tail(preflight.stderr)}")
        positive = json.loads(preflight.stdout)
        deadline = time.monotonic() + 2
        while sentinel.accepted == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        if sentinel.accepted == 0:
            raise RuntimeError("host-gateway positive control did not reach our listener")
        report["host_gateway_positive_control"] = positive
        sentinel._reset()
        report["phases"]["preflight_seconds"] = round(time.monotonic() - mark, 3)

        mark = time.monotonic()
        created = docker(
            "network", "create", "--driver", "bridge", "--internal", "--ipv6=false",
            "--opt", f"com.docker.network.bridge.gateway_mode_ipv4={GATEWAY_MODE}",
            "--opt", "com.docker.network.bridge.enable_ipv6=false",
            "--label", f"{LABEL}={owner}", network_name, check=False,
        )
        if created.returncode:
            raise RuntimeError(
                "an internal bridge with an isolated IPv4 gateway is unavailable; "
                f"no fallback to an ordinary bridge: {tail(created.stderr or created.stdout)}"
            )
        networks.append(network_name)
        described = json.loads(docker("network", "inspect", network_name).stdout)[0]
        options = described.get("Options") or {}
        subnets = (described.get("IPAM") or {}).get("Config") or []
        check(
            "network_isolated_and_unlabeled_gateway",
            described.get("Internal") is True
            and described.get("EnableIPv6") is False
            and options.get("com.docker.network.bridge.gateway_mode_ipv4") == GATEWAY_MODE
            and options.get("com.docker.network.bridge.enable_ipv6") == "false"
            and (described.get("Labels") or {}).get(LABEL) == owner
            and bool(subnets),
            {
                "internal": described.get("Internal"),
                "enable_ipv6": described.get("EnableIPv6"),
                "options": options,
                "labels": described.get("Labels"),
                "subnets": subnets,
            },
        )
        report["phases"]["network_create_seconds"] = round(time.monotonic() - mark, 3)

        mark = time.monotonic()
        create(
            f"{owner}-fixture", fixture_source(), network_name, (),
            f"--dns={DNS_SERVER}", *(f"--dns-option={option}" for option in DNS_OPTIONS),
        )
        docker("start", f"{owner}-fixture")
        ready = wait_for_marker(f"{owner}-fixture", FIXTURE_MARKER, 60.0)
        report["phases"]["fixture_start_seconds"] = round(time.monotonic() - mark, 3)
        if not ready:
            logs = tail(docker("logs", f"{owner}-fixture").stdout)
            raise RuntimeError(f"fixture did not report readiness: {logs}")
        fixture_address = json.loads(docker("inspect", f"{owner}-fixture").stdout)[0][
            "NetworkSettings"
        ]["Networks"][network_name]["IPAddress"]
        if not fixture_address:
            raise RuntimeError("fixture container received no address on the isolated network")

        mark = time.monotonic()
        create(
            f"{owner}-client",
            CLIENT_SOURCE,
            network_name,
            (fixture_address, str(FIXTURE_PORT), SENTINEL_HOST, str(sentinel.port)),
            f"--dns={DNS_SERVER}",
            *(f"--dns-option={option}" for option in DNS_OPTIONS),
            f"--add-host={SENTINEL_HOST_ENTRY}",
        )
        attached = docker("start", "--attach", f"{owner}-client", check=False, timeout=120)
        report["phases"]["client_seconds"] = round(time.monotonic() - mark, 3)
        if attached.returncode:
            reason = tail(attached.stderr or attached.stdout)
            raise RuntimeError(f"client container failed: {reason}")
        client = parse_json_line(attached.stdout)
        if client is None:
            raise RuntimeError(f"client produced no JSON report: {tail(attached.stdout)}")
        report["client"] = client

        mark = time.monotonic()
        docker("stop", "--time=5", f"{owner}-fixture", check=False)
        fixture = parse_json_line(docker("logs", f"{owner}-fixture").stdout) or {}
        report["phases"]["fixture_stop_seconds"] = round(time.monotonic() - mark, 3)
        if not fixture:
            raise RuntimeError("fixture produced no final report; its lifetime is not provable")

        received = b""
        attempts = [item for item in client["fixture"]["attempts"] if item.get("connected")]
        if attempts and attempts[-1].get("received"):
            received = base64.b64decode(attempts[-1]["received"])
        check(
            "fixture_serves_exact_fixed_response",
            received == FIXTURE_RESPONSE,
            {
                "address": client["fixture"]["address"],
                "port": client["fixture"]["port"],
                "attempts": len(client["fixture"]["attempts"]),
                "received_bytes": len(received),
                "received_sha256": hashlib.sha256(received).hexdigest(),
                "expected_bytes": len(FIXTURE_RESPONSE),
                "expected_sha256": hashlib.sha256(FIXTURE_RESPONSE).hexdigest(),
                "served": fixture.get("served"),
            },
        )
        check(
            "client_has_no_ipv4_default_route",
            client.get("ipv4_default_route") is False,
            {"routes": client.get("routes")},
        )
        check("public_endpoint_blocked", client["public"]["blocked"], client["public"])
        check("cloud_metadata_blocked", client["metadata"]["blocked"], client["metadata"])
        check(
            "host_sentinel_unreachable",
            client["sentinel"]["blocked"]
            and client["sentinel"].get("resolved") == positive["host_address"]
            and client["sentinel"].get("blocked_by") == "connect_failed"
            and sentinel.accepted == 0,
            {"client": client["sentinel"], "host_sentinel_accepted": sentinel.accepted},
        )
        check("external_dns_blocked", client["external_dns"]["blocked"], client["external_dns"])
        check(
            "containers_hardened",
            hardened(client.get("process", {})) and hardened(fixture.get("process", {})),
            {"client": client.get("process"), "fixture": fixture.get("process")},
        )
        check(
            "fixture_network_has_no_default_route",
            fixture.get("default_route") is False and (fixture.get("served") or 0) >= 1,
            {"default_route": fixture.get("default_route"), "served": fixture.get("served"),
             "failures": fixture.get("failures"), "accept_error": fixture.get("accept_error")},
        )

        states = {
            name: json.loads(docker("inspect", name).stdout)[0]
            for name in (f"{owner}-fixture", f"{owner}-client")
        }
        check(
            "no_published_ports_and_single_network",
            all(
                not (state["HostConfig"].get("PortBindings") or {})
                and state["HostConfig"].get("NetworkMode") == network_name
                and list(state["NetworkSettings"]["Networks"]) == [network_name]
                and (state["NetworkSettings"].get("Ports") or {}) == {}
                for state in states.values()
            ),
            {
                name: {
                    "network_mode": state["HostConfig"].get("NetworkMode"),
                    "port_bindings": state["HostConfig"].get("PortBindings"),
                    "published": state["NetworkSettings"].get("Ports"),
                    "networks": {
                        key: {
                            "ip_address": value.get("IPAddress"),
                            "gateway": value.get("Gateway"),
                        }
                        for key, value in state["NetworkSettings"]["Networks"].items()
                    },
                }
                for name, state in states.items()
            },
        )
        check(
            "resource_bounds_recorded_by_engine",
            all(recorded_limits(state["HostConfig"]) for state in states.values()),
            {name: state["HostConfig"] for name, state in states.items()},
        )
        check(
            "client_dns_pinned_and_sentinel_pinned",
            recorded_dns(states[f"{owner}-client"]["HostConfig"]),
            {
                "dns": states[f"{owner}-client"]["HostConfig"].get("Dns"),
                "dns_options": states[f"{owner}-client"]["HostConfig"].get("DnsOptions"),
                "extra_hosts": states[f"{owner}-client"]["HostConfig"].get("ExtraHosts"),
                "nameservers": client.get("nameservers"),
            },
        )
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, LookupError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        mark = time.monotonic()
        try:
            for name in containers:
                found = docker("inspect", name, check=False)
                if found.returncode:
                    if not absent(found.stderr):
                        report["cleanup"]["errors"].append(f"{name}: {tail(found.stderr, 200)}")
                    continue
                labels = json.loads(found.stdout)[0]["Config"].get("Labels") or {}
                if labels.get(LABEL) != owner:
                    report["cleanup"]["errors"].append(f"refusing unlabelled container {name}")
                    continue
                removed = docker("rm", "--force", name, check=False)
                if removed.returncode:
                    report["cleanup"]["errors"].append(f"{name}: {tail(removed.stderr, 200)}")
                else:
                    report["cleanup"]["containers"].append(name)
            for item in networks:
                found = docker("network", "inspect", item, check=False)
                if found.returncode:
                    if not absent(found.stderr):
                        report["cleanup"]["errors"].append(f"{item}: {tail(found.stderr, 200)}")
                    continue
                labels = json.loads(found.stdout)[0].get("Labels") or {}
                if labels.get(LABEL) != owner:
                    report["cleanup"]["errors"].append(f"refusing unlabelled network {item}")
                    continue
                removed = docker("network", "rm", item, check=False)
                if removed.returncode:
                    report["cleanup"]["errors"].append(f"{item}: {tail(removed.stderr, 200)}")
                else:
                    report["cleanup"]["network_removed"] = True
            if sentinel is not None:
                sentinel.close()
                report.setdefault("sentinel", {})["closed"] = True
            filter_arg = f"label={LABEL}={owner}"
            left = docker("ps", "-aq", "--filter", filter_arg, check=False)
            networks_left = docker("network", "ls", "-q", "--filter", filter_arg, check=False)
            if left.returncode or networks_left.returncode:
                report["cleanup"]["errors"].append("residual resources could not be listed")
            report["cleanup"]["verified"] = (
                not report["cleanup"]["errors"]
                and not left.stdout.strip()
                and not networks_left.stdout.strip()
            )
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as cleanup_error:
            report["cleanup"]["errors"].append(f"cleanup failed: {cleanup_error}")
        report["phases"]["cleanup_seconds"] = round(time.monotonic() - mark, 3)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Docker network isolation probe.")
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="already installed trusted Python image, pinned by digest",
    )
    parser.add_argument(
        "--python-bin",
        default=DEFAULT_PYTHON,
        help="absolute interpreter path inside the pinned image",
    )
    args = parser.parse_args()
    report = probe(args.image, args.python_bin)
    print(json.dumps(report, indent=2))
    if report.get("error") or not report["cleanup"]["verified"] or not report["checks"]:
        return 1
    if any(not entry["passed"] for entry in report["checks"]):
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
