"""Settings, in the order of who knows best.

Detected first, because the repository already contains the answers. Then
``agentq.toml``, which is where a wrong guess gets corrected once. Then the
environment, for the things that differ between machines and must not be
committed. Nothing is required: running the tool in a repository with no config
at all is the case this file exists to make work.

``init`` writes the detected values out so there is something to edit rather
than a manual to read.
"""

from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import AgentOptions
from .delivery import AUTO
from .detect import Check, convention_files, detect_checks
from .gitctl import detect_base_branch

CONFIG_NAME = "agentq.toml"


def _env(name: str) -> str:
    return os.environ.get(f"AGENTQ_{name.upper()}", "").strip()


@dataclass(slots=True)
class Config:
    """Everything the runner needs, resolved."""

    root: Path
    base_branch: str
    checks: list[Check]
    conventions: list[str]
    state_dir: Path
    worktree: Path
    delivery: str = AUTO
    poll_seconds: float = 5.0
    max_rounds: int = 3
    context_limit: int = 300_000
    agent_timeout: int = 3600
    permission: str = "acceptEdits"
    model: str | None = "sonnet"
    max_usd: float = 0.0
    branch_prefix: str = "agentq"
    #: A process to run and restart when work lands. Empty means none.
    process: list[str] = field(default_factory=list)
    #: Front-specific sections of the config file, verbatim.
    fronts: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def queue_path(self) -> Path:
        return self.state_dir / "tasks.json"

    @property
    def process_log(self) -> Path:
        return self.state_dir / "process.log"

    def agent_options(self) -> AgentOptions:
        return AgentOptions(
            permission=self.permission,
            model=self.model,
            max_usd=self.max_usd,
            timeout=self.agent_timeout,
            context_limit=self.context_limit,
        )

    def front(self, name: str) -> dict[str, Any]:
        return self.fronts.get(name, {})

    # --- loading ---------------------------------------------------------

    @classmethod
    def load(cls, root: Path) -> Config:
        raw = read_config_file(root / CONFIG_NAME)
        state_dir = Path(raw.get("state_dir") or _env("state_dir") or root / ".agentq")
        if not state_dir.is_absolute():
            state_dir = root / state_dir

        config = cls(
            root=root,
            base_branch=str(raw.get("base_branch") or "") or detect_base_branch(root),
            checks=_checks_from(raw) or detect_checks(root),
            conventions=convention_files(root),
            state_dir=state_dir,
            worktree=state_dir / "worktree",
            fronts={
                key: value
                for key, value in raw.items()
                if isinstance(value, dict) and key not in {"checks"}
            },
        )
        _apply(config, raw)
        _apply_env(config)
        return config


def read_config_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _checks_from(raw: dict[str, Any]) -> list[Check]:
    """``checks = [{name = "test", command = "npm test"}]`` — string or list."""
    listed = raw.get("checks")
    if not isinstance(listed, list):
        return []
    found: list[Check] = []
    for item in listed:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        argv = shlex.split(command) if isinstance(command, str) else list(command or [])
        if argv:
            found.append(Check(str(item.get("name") or argv[0]), [str(part) for part in argv]))
    return found


def _apply(config: Config, raw: dict[str, Any]) -> None:
    """Scalar settings from the file, when present and of the right shape."""
    for key, cast in (
        ("delivery", str),
        ("poll_seconds", float),
        ("max_rounds", int),
        ("context_limit", int),
        ("agent_timeout", int),
        ("permission", str),
        ("max_usd", float),
        ("branch_prefix", str),
    ):
        value = raw.get(key)
        if value is not None and not isinstance(value, dict):
            setattr(config, key, cast(value))
    if "model" in raw:
        config.model = str(raw["model"]) or None
    process = raw.get("process")
    if isinstance(process, str):
        config.process = shlex.split(process)
    elif isinstance(process, list):
        config.process = [str(part) for part in process]


def _apply_env(config: Config) -> None:
    """The machine's own answers, which override the committed ones."""
    for key, cast in (
        ("base_branch", str),
        ("delivery", str),
        ("permission", str),
        ("branch_prefix", str),
        ("poll_seconds", float),
        ("max_rounds", int),
        ("context_limit", int),
        ("agent_timeout", int),
        ("max_usd", float),
    ):
        value = _env(key)
        if value:
            setattr(config, key, cast(value))
    if _env("model"):
        config.model = _env("model")
    if _env("process"):
        config.process = shlex.split(_env("process"))


def render_config(config: Config) -> str:
    """The detected settings, as a file a person can edit."""
    lines = [
        "# agentq — written by `agentq init` from what this repository looks like.",
        "# Everything here is optional; delete a line to go back to the detected value.",
        "",
        f'base_branch = "{config.base_branch}"',
        "",
        "# auto: merge if the base branch allows it, otherwise open a pull request.",
        "# merge / pr force one or the other.",
        f'delivery = "{config.delivery}"',
        "",
        f"max_rounds = {config.max_rounds}          # fixer <-> reviewer rounds before giving up",
        f'model = "{config.model or "sonnet"}"',
        "",
        "# acceptEdits keeps the agent inside the permission system.",
        '# "skip" passes --dangerously-skip-permissions: faster, and only sane',
        "# because the agent works in a throwaway worktree.",
        f'permission = "{config.permission}"',
        "",
        "# A process to run and restart whenever work lands. Optional.",
        f'process = "{" ".join(config.process)}"' if config.process else '# process = ""',
        "",
        "# Detected from this repository. Order matters; the first failure stops the round.",
    ]
    for check in config.checks:
        lines.append("[[checks]]")
        lines.append(f'name = "{check.name}"')
        lines.append(f'command = "{" ".join(check.command)}"')
        lines.append("")
    if not config.checks:
        lines.append("# (none found — add [[checks]] entries with name and command)")
        lines.append("")
    lines += [
        "# [telegram]",
        '# token = ""      # or AGENTQ_TELEGRAM_TOKEN in the environment',
        "# admins = []     # numeric ids; empty means the first /start claims the bot",
        "",
    ]
    return "\n".join(lines)


__all__ = ["CONFIG_NAME", "Config", "read_config_file", "render_config"]
