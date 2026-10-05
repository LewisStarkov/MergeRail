"""Operator-owned Docker settings. A project snapshot cannot change these."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from typing import Any

PINNED_IMAGE = re.compile(r"(?:[a-zA-Z0-9._:/-]+@sha256:|sha256:)[a-f0-9]{64}\Z")
GATEWAY_IMAGE = "python@sha256:e2a5fce94bd761967528a12f16d707c2613e1522f3f2d77fa45766f45962547f"


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    backend: str = "docker"
    required: bool = True
    profile: str = "eco"
    image: str = ""
    gateway_image: str = GATEWAY_IMAGE
    cpus: float = 1.0
    memory_mib: int = 2048
    pids_limit: int = 128
    workspace_limit_mib: int = 512
    tmp_limit_mib: int = 128
    cache_limit_mib: int = 4096
    max_bundle_mib: int = 256
    log_limit_mib: int = 20
    idle_stop_seconds: int = 60
    upstream_host: str = "opencode.ai"
    upstream_prefix: str = "/zen/v1"
    opencode_model: str = "opencode/space-bunny-free"

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def validate(self) -> None:
        if self.backend != "docker" or self.required is not True or self.profile != "eco":
            raise ValueError("execution requires backend='docker', required=true, profile='eco'")
        for name in ("image", "gateway_image"):
            value = getattr(self, name)
            if not isinstance(value, str) or not PINNED_IMAGE.fullmatch(value):
                raise ValueError(f"execution.{name} must be pinned by sha256 digest")
        if isinstance(self.cpus, bool) or not isinstance(self.cpus, int | float):
            raise ValueError("execution.cpus must be a finite positive number")
        if not math.isfinite(self.cpus) or not 0 < self.cpus <= 16:
            raise ValueError("execution.cpus must be between 0 and 16")
        for name in (
            "memory_mib",
            "pids_limit",
            "workspace_limit_mib",
            "tmp_limit_mib",
            "cache_limit_mib",
            "max_bundle_mib",
            "log_limit_mib",
            "idle_stop_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65536:
                raise ValueError(f"execution.{name} must be an integer from 1 to 65536")
        if self.memory_mib < 256 or self.pids_limit < 16:
            raise ValueError("execution needs at least 256 MiB RAM and 16 PIDs")
        if self.workspace_limit_mib + self.tmp_limit_mib >= self.memory_mib:
            raise ValueError("execution tmpfs limits must leave RAM for running commands")
        if self.max_bundle_mib > self.workspace_limit_mib:
            raise ValueError("execution.max_bundle_mib must fit the workspace limit")
        if self.idle_stop_seconds > 60:
            raise ValueError("eco idle_stop_seconds must be at most 60")
        if self.upstream_host != "opencode.ai" or self.upstream_prefix != "/zen/v1":
            raise ValueError("Docker AI currently supports the fixed OpenCode Zen origin only")
        if not isinstance(self.opencode_model, str) or not re.fullmatch(
            r"opencode/[a-zA-Z0-9._-]{1,128}", self.opencode_model
        ):
            raise ValueError("execution.opencode_model must name an OpenCode Zen model")


def load_policy(raw: dict[str, Any]) -> ExecutionPolicy | None:
    requested = os.environ.get("MERGERAIL_EXECUTION_BACKEND", "").strip()
    required = os.environ.get("MERGERAIL_EXECUTION_REQUIRED", "").strip()
    image = os.environ.get("MERGERAIL_EXECUTION_IMAGE", "").strip()
    if "execution" not in raw and not requested and not required and not image:
        return None
    section = raw.get("execution", {})
    if not isinstance(section, dict):
        raise ValueError("execution must be a TOML table; host fallback is forbidden")
    values = dict(section)
    sync = values.pop("sync", {})
    if not isinstance(sync, dict) or set(sync) - {"mode", "include_uncommitted", "max_bundle_mib"}:
        raise ValueError("unsupported execution.sync settings")
    if (
        sync.get("mode", "git-bundle") != "git-bundle"
        or sync.get("include_uncommitted", False) is not False
    ):
        raise ValueError("execution supports committed Git bundles only")
    if "max_bundle_mib" in sync:
        values["max_bundle_mib"] = sync["max_bundle_mib"]
    if requested:
        values["backend"] = requested
    if required:
        if required.lower() not in {"true", "1"}:
            raise ValueError(
                "MERGERAIL_EXECUTION_REQUIRED must be true; host fallback is forbidden"
            )
        values["required"] = True
    if image:
        values["image"] = image
    unknown = set(values) - ExecutionPolicy.__dataclass_fields__.keys()
    if unknown:
        raise ValueError("unknown execution settings: " + ", ".join(sorted(unknown)))
    policy = ExecutionPolicy(**values)
    policy.validate()
    return policy
