"""What this project is, without being told.

The point of the whole tool is that you run it in a repository and it works. So
nothing here asks: the base branch comes from the refs, and the checks come from
whatever manifest is lying in the root. A wrong guess is cheap — it lands in
``agentq.toml`` on first run, where you can fix it once.

A check is only proposed if the thing that would run it exists on this machine.
An agent that has to read ``command not found`` on every round is an agent
paying tokens for your missing toolchain.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Check:
    """One deterministic verdict on the working tree."""

    name: str
    command: list[str]


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _python_prefix(root: Path) -> list[str]:
    """How this project runs its own tools."""
    if (root / "uv.lock").exists() and shutil.which("uv"):
        return ["uv", "run"]
    if (root / "poetry.lock").exists() and shutil.which("poetry"):
        return ["poetry", "run"]
    return []


def _requirement_name(spec: str) -> str:
    """``mypy>=1.11; python_version > '3.10'`` → ``mypy``."""
    return re.split(r"[\[\]<>=!~;@\s]", spec.strip(), maxsplit=1)[0].lower()


def _python_dependency_names(data: dict[str, Any]) -> set[str]:
    """Every distribution named anywhere pyproject declares dependencies."""
    listed: list[Any] = []
    project = data.get("project")
    if isinstance(project, dict):
        direct = project.get("dependencies")
        if isinstance(direct, list):
            listed += direct
        extras = project.get("optional-dependencies")
        if isinstance(extras, dict):
            for group in extras.values():
                if isinstance(group, list):
                    listed += group
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, list):
                listed += group
    return {_requirement_name(str(item)) for item in listed if isinstance(item, str)}


def _python_checks(root: Path) -> list[Check]:
    """The tools this project actually depends on — not merely mentions.

    A substring scan of the manifest would propose ``ruff`` because a comment
    says the word, and ``pytest`` because a ``tests/`` directory exists — and a
    check for a tool that is not a dependency fails on every task, through no
    fault of the task. So the manifest is parsed: a tool counts when it is a
    dependency (a ``pytest-*`` plugin implies pytest), has a ``[tool.*]``
    table, or has a config file of its own in the root.
    """
    try:
        data = tomllib.loads(_text(root / "pyproject.toml"))
    except tomllib.TOMLDecodeError:
        data = {}
    names = _python_dependency_names(data)
    tables = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    prefix = _python_prefix(root)
    found: list[Check] = []

    def declared(tool: str, *files: str) -> bool:
        depended = any(name == tool or name.startswith(f"{tool}-") for name in names)
        configured = isinstance(tables, dict) and tool in tables
        return depended or configured or any((root / name).exists() for name in files)

    if declared("ruff", "ruff.toml", ".ruff.toml"):
        found.append(Check("ruff", [*prefix, "ruff", "check", "."]))
    if declared("mypy", "mypy.ini", ".mypy.ini"):
        found.append(Check("mypy", [*prefix, "mypy", "."]))
    if declared("pytest", "pytest.ini"):
        found.append(Check("pytest", [*prefix, "pytest", "-q"]))
    return found


def _node_manager(root: Path) -> str:
    for lockfile, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("package-lock.json", "npm"),
    ):
        if (root / lockfile).exists() and shutil.which(manager):
            return manager
    return "npm" if shutil.which("npm") else ""


def _node_checks(root: Path) -> list[Check]:
    """Whatever the project already calls lint, typecheck and test.

    Only scripts that exist are proposed, and only the ones that end: a ``test``
    script that opens a watcher would hang the runner, so vitest — whose default
    is to watch — gets its ``--run`` flag. Other runners get no flag at all;
    jest, for one, refuses arguments it does not know.
    """
    try:
        manifest = json.loads(_text(root / "package.json") or "{}")
    except json.JSONDecodeError:
        return []
    if not isinstance(manifest, dict):
        return []
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return []
    manager = _node_manager(root)
    if not manager:
        return []
    dependencies: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        section = manifest.get(key)
        if isinstance(section, dict):
            dependencies |= set(section)

    found: list[Check] = []
    for name in ("lint", "typecheck", "type-check", "tsc", "test"):
        if name not in scripts:
            continue
        command = [manager, "run", name] if manager != "yarn" else [manager, name]
        uses_vitest = "vitest" in dependencies or "vitest" in str(scripts.get("test") or "")
        if name == "test" and uses_vitest:
            # npm needs the separator so the flag reaches the script rather
            # than the package manager.
            command += ["--", "--run"] if manager == "npm" else ["--run"]
        found.append(Check(name, command))
    return found


def _rust_checks(root: Path) -> list[Check]:
    if not (root / "Cargo.toml").exists() or not shutil.which("cargo"):
        return []
    return [
        Check("clippy", ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"]),
        Check("test", ["cargo", "test"]),
    ]


def _go_checks(root: Path) -> list[Check]:
    if not (root / "go.mod").exists() or not shutil.which("go"):
        return []
    return [Check("vet", ["go", "vet", "./..."]), Check("test", ["go", "test", "./..."])]


def detect_checks(root: Path) -> list[Check]:
    """Every check this repository already knows how to run.

    A polyglot repository gets all of them, which is right: a Next.js front and
    a Python API in one tree both have to survive the change.
    """
    found: list[Check] = []
    for probe in (_python_checks, _node_checks, _rust_checks, _go_checks):
        found.extend(probe(root))
    return found


def convention_files(root: Path) -> list[str]:
    """The files this repository uses to state its own rules.

    These are why the prompts carry no project-specific instructions: the
    project already wrote them down, and the agent reads them itself.
    """
    names = ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", "README.md")
    return [name for name in names if (root / name).is_file()]


__all__ = ["Check", "convention_files", "detect_checks"]
