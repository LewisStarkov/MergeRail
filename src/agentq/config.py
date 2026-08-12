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

import json
import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import AgentOptions
from .backends import BackendRegistry, SessionSpec, default_registry
from .backends.external import ExternalBackend
from .delivery import AUTO
from .detect import Check, convention_files, detect_checks
from .gitctl import detect_base_branch

CONFIG_NAME = "agentq.toml"
EXTERNAL_BACKEND_PROTOCOL = "agentq-jsonl-v1"


def _env(name: str) -> str:
    return os.environ.get(f"AGENTQ_{name.upper()}", "").strip()


@dataclass(slots=True)
class AgentConfig:
    backend: str = "auto"
    model: str | None = None
    effort: str = ""
    permission: str = "safe"
    timeout: int = 3600
    context_limit: int = 160_000
    settings: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ProjectContext:
    environment: str = "local"
    work_mode: str = "development"
    summary: str = ""
    external_actions: str = "forbid"
    constraints: list[str] = field(default_factory=list)


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
    context_limit: int = 160_000
    agent_timeout: int = 3600
    permission: str = "acceptEdits"
    model: str | None = "sonnet"
    effort: str = ""
    #: The whole task's budget in dollars, all rounds of both agents. 0 = none.
    max_usd: float = 0.0
    branch_prefix: str = "agentq"
    #: Which Claude settings the agents load. ``project`` keeps the operator's
    #: own MCP servers and plugins out of the agents' context; ``all`` loads
    #: everything the interactive CLI would.
    agent_settings: str = "project"
    #: Run the checks once on the clean base before applying ``baseline_mode``.
    baseline_checks: bool = True
    baseline_mode: str = "exclude"
    strict_security: bool = False
    #: A process to run and restart when work lands. Empty means none.
    process: list[str] = field(default_factory=list)
    #: Front-specific sections of the config file, verbatim.
    fronts: dict[str, dict[str, Any]] = field(default_factory=dict)
    backend_order: list[str] = field(default_factory=lambda: ["claude", "codex", "opencode"])
    fixer: AgentConfig = field(default_factory=AgentConfig)
    reviewer: AgentConfig = field(default_factory=lambda: AgentConfig(permission="review"))
    external_backends: dict[str, list[str]] = field(default_factory=dict)
    project: ProjectContext = field(default_factory=ProjectContext)

    @property
    def queue_path(self) -> Path:
        return self.state_dir / "tasks.json"

    @property
    def audit_path(self) -> Path:
        return self.state_dir / "events.jsonl"

    @property
    def process_log(self) -> Path:
        return self.state_dir / "process.log"

    def agent_options(self) -> AgentOptions:
        return AgentOptions(
            permission=self.permission,
            model=self.model,
            effort=self.effort,
            timeout=self.agent_timeout,
            context_limit=self.context_limit,
            setting_sources="" if self.agent_settings in ("", "all") else self.agent_settings,
        )

    def session_spec(
        self,
        role: str,
        cwd: Path,
        *,
        system_prompt: str = "",
        read_only: bool = False,
        resume_session_id: str | None = None,
    ) -> SessionSpec:
        selected = self.reviewer if role == "reviewer" else self.fixer
        return SessionSpec(
            role=role,
            cwd=cwd,
            system_prompt=system_prompt,
            read_only=read_only,
            model=selected.model,
            effort=selected.effort,
            permission=selected.permission,
            timeout=selected.timeout,
            context_limit=selected.context_limit,
            settings=selected.settings,
            resume_session_id=resume_session_id,
        )

    def front(self, name: str) -> dict[str, Any]:
        return self.fronts.get(name, {})

    def backend_registry(self) -> BackendRegistry:
        registry = default_registry()
        for name, command in self.external_backends.items():
            registry.register_external(ExternalBackend(name, command), replace=True)
        return registry

    # --- loading ---------------------------------------------------------

    @classmethod
    def load(cls, root: Path) -> Config:
        raw = read_config_file(root / CONFIG_NAME)
        # The environment first, like everywhere else: it is the machine's own
        # answer, and the machine knows best where its state may live.
        state_dir = Path(_env("state_dir") or str(raw.get("state_dir") or "") or root / ".agentq")
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
                if isinstance(value, dict)
                and key not in {"agents", "backends", "checks", "project"}
            },
        )
        _apply(config, raw)
        _apply_project(config, raw)
        _apply_agents(config, raw)
        _apply_backends(config, raw)
        _apply_env(config)
        if config.delivery not in {"auto", "local", "merge", "pr"}:
            raise ValueError(f"unknown delivery mode: {config.delivery!r}")
        if config.baseline_mode not in {"exclude", "compare", "strict"}:
            raise ValueError(f"unknown baseline mode: {config.baseline_mode!r}")
        if config.project.environment not in {"local", "staging", "production"}:
            raise ValueError(f"unknown project environment: {config.project.environment!r}")
        if config.project.work_mode not in {"development", "maintenance", "incident"}:
            raise ValueError(f"unknown project work mode: {config.project.work_mode!r}")
        if config.project.external_actions not in {"forbid", "ask"}:
            raise ValueError(
                f"unknown external actions policy: {config.project.external_actions!r}"
            )
        return config


def read_config_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"cannot parse {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
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


def _as_bool(value: str) -> bool:
    return value.strip().lower() not in ("0", "false", "no", "off")


def _apply(config: Config, raw: dict[str, Any]) -> None:
    """Scalar settings from the file, when present and of the right shape."""
    for key, cast in (
        ("delivery", str),
        ("poll_seconds", float),
        ("max_rounds", int),
        ("context_limit", int),
        ("agent_timeout", int),
        ("permission", str),
        ("effort", str),
        ("max_usd", float),
        ("branch_prefix", str),
        ("agent_settings", str),
        ("baseline_checks", bool),
        ("baseline_mode", str),
        ("strict_security", bool),
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


def _apply_project(config: Config, raw: dict[str, Any]) -> None:
    project = raw.get("project")
    section = project if isinstance(project, dict) else {}
    for key in ("environment", "work_mode", "summary", "external_actions"):
        value = section.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            setattr(config.project, key, str(value).strip())
    constraints = section.get("constraints")
    if isinstance(constraints, list):
        config.project.constraints = [text for item in constraints if (text := str(item).strip())]


def _apply_agents(config: Config, raw: dict[str, Any]) -> None:
    agents = raw.get("agents")
    section = agents if isinstance(agents, dict) else {}
    order = section.get("backend_order")
    if isinstance(order, list) and order:
        config.backend_order = [str(item) for item in order]

    legacy: dict[str, object] = {}
    if "model" in raw:
        legacy["model"] = str(raw["model"]) or None
    for old, new in (
        ("effort", "effort"),
        ("permission", "permission"),
        ("agent_timeout", "timeout"),
        ("context_limit", "context_limit"),
    ):
        if old in raw:
            legacy[new] = raw[old]
    if "agent_settings" in raw:
        legacy["setting_sources"] = raw["agent_settings"]

    for role, target in (("fixer", config.fixer), ("reviewer", config.reviewer)):
        values = dict(legacy)
        configured = section.get(role)
        if isinstance(configured, dict):
            values.update(configured)
        _apply_agent(target, values)


def _apply_agent(target: AgentConfig, values: dict[str, object]) -> None:
    for key in ("backend", "effort", "permission"):
        value = values.get(key)
        if value is not None:
            setattr(target, key, str(value))
    if "model" in values:
        target.model = str(values["model"]) if values["model"] else None
    for key in ("timeout", "context_limit"):
        value = values.get(key)
        if value is not None:
            setattr(target, key, int(str(value)))
    sources = values.get("setting_sources")
    if sources is not None:
        target.settings["setting_sources"] = "" if str(sources) in ("", "all") else str(sources)
    settings = values.get("settings")
    if isinstance(settings, dict):
        target.settings.update({str(key): value for key, value in settings.items()})


def _apply_backends(config: Config, raw: dict[str, Any]) -> None:
    backends = raw.get("backends")
    if not isinstance(backends, dict):
        return
    for name, settings in backends.items():
        if not isinstance(settings, dict):
            continue
        protocol = settings.get("protocol")
        if protocol != EXTERNAL_BACKEND_PROTOCOL:
            raise ValueError(
                f"external backend {name!r} requires "
                f'protocol = "{EXTERNAL_BACKEND_PROTOCOL}"; got {protocol!r}'
            )
        command = settings.get("command")
        if isinstance(command, str):
            argv = shlex.split(command)
        elif isinstance(command, list):
            argv = [str(part) for part in command]
        else:
            argv = []
        if not argv:
            raise ValueError(f"external backend {name!r} requires a non-empty command")
        config.external_backends[str(name)] = argv


def _apply_env(config: Config) -> None:
    """The machine's own answers, which override the committed ones."""
    for key, cast in (
        ("base_branch", str),
        ("delivery", str),
        ("permission", str),
        ("effort", str),
        ("branch_prefix", str),
        ("agent_settings", str),
        ("poll_seconds", float),
        ("max_rounds", int),
        ("context_limit", int),
        ("agent_timeout", int),
        ("max_usd", float),
        ("baseline_checks", _as_bool),
        ("baseline_mode", str),
        ("strict_security", _as_bool),
    ):
        value = _env(key)
        if value:
            setattr(config, key, cast(value))
    if _env("model"):
        config.model = _env("model")
    if _env("process"):
        config.process = shlex.split(_env("process"))
    for key in ("environment", "work_mode", "summary", "external_actions"):
        value = _env("project_" + key)
        if value:
            setattr(config.project, key, value)
    for role, target in (("fixer", config.fixer), ("reviewer", config.reviewer)):
        prefix = f"{role}_"
        values: dict[str, object] = {}
        for key in ("backend", "model", "effort", "permission", "timeout", "context_limit"):
            value = _env(prefix + key)
            if value:
                values[key] = value
        _apply_agent(target, values)


def render_config(config: Config) -> str:
    """The detected settings, as a file a person can edit."""
    lines = [
        "# agentq — written by `agentq init` from what this repository looks like.",
        "# Everything here is optional; delete a line to go back to the detected value.",
        "",
        f'base_branch = "{config.base_branch}"',
        "",
        "# auto: open a PR when origin exists, otherwise merge locally.",
        "# local / pr force one strategy; merge is a deprecated alias for local.",
        f'delivery = "{config.delivery}"',
        "",
        f"max_rounds = {config.max_rounds}          # fixer <-> reviewer rounds before giving up",
        "",
        "# A process to run and restart whenever work lands. Optional.",
        f'process = "{" ".join(config.process)}"' if config.process else '# process = ""',
        "",
        "# exclude skips checks already failing on the base; compare still runs",
        "# them as known failures; strict refuses to start until every check passes.",
        f"baseline_checks = {str(config.baseline_checks).lower()}",
        f'baseline_mode = "{config.baseline_mode}"',
        f"strict_security = {str(config.strict_security).lower()}",
        "",
        "# max_usd = 0.0    # requires exact USD reporting from both selected backends",
        "",
        "[project]",
        "# This context is included in both agents' standing instructions.",
        f"environment = {json.dumps(config.project.environment, ensure_ascii=False)}",
        f"work_mode = {json.dumps(config.project.work_mode, ensure_ascii=False)}",
        f"summary = {json.dumps(config.project.summary, ensure_ascii=False)}",
        f"external_actions = {json.dumps(config.project.external_actions, ensure_ascii=False)}",
        "constraints = ["
        + ", ".join(json.dumps(item, ensure_ascii=False) for item in config.project.constraints)
        + "]",
        "",
        "[agents]",
        "backend_order = [" + ", ".join(f'"{name}"' for name in config.backend_order) + "]",
        "",
        "[agents.fixer]",
        f'backend = "{config.fixer.backend}"',
        f'model = "{config.fixer.model or ""}"',
        f'effort = "{config.fixer.effort}"',
        f'permission = "{config.fixer.permission}"',
        "",
        "[agents.reviewer]",
        f'backend = "{config.reviewer.backend}"',
        f'model = "{config.reviewer.model or ""}"',
        f'effort = "{config.reviewer.effort}"',
        'permission = "review"',
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
        "# [web]",
        '# host = "127.0.0.1"',
        "# port = 8788",
        '# username = "agentq"   # use AGENTQ_WEB_PASSWORD for the secret',
        "# session_ttl = 28800",
        "# max_sse_clients = 16",
        "",
    ]
    return "\n".join(lines)


def write_config(config: Config) -> Path:
    """Atomically persist the resolved project configuration."""
    from .tasks import write_atomic

    target = config.root / CONFIG_NAME
    write_atomic(target, render_config(config))
    return target


def ensure_state_ignored(root: Path) -> bool:
    """Add the runtime state directory to ``.gitignore`` once."""
    from .tasks import write_atomic

    path = root / ".gitignore"
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    if any(line.strip().rstrip("/") == ".agentq" for line in current.splitlines()):
        return False
    lead = "" if not current or current.endswith("\n") else "\n"
    write_atomic(path, f"{current}{lead}.agentq/\n")
    return True


__all__ = [
    "CONFIG_NAME",
    "AgentConfig",
    "Config",
    "ProjectContext",
    "ensure_state_ignored",
    "read_config_file",
    "render_config",
    "write_config",
]
