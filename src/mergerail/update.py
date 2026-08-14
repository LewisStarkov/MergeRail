"""Install stable MergeRail releases directly from the Git repository."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__

REPOSITORY = "https://github.com/LewisStarkov/MergeRail.git"
CHECK_INTERVAL = 24 * 60 * 60
_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_FALSE = {"0", "false", "no", "off"}


class UpdateError(RuntimeError):
    pass


def latest_release(repository: str | None = None) -> str | None:
    result = _run(
        ["git", "ls-remote", "--tags", "--refs", _repository(repository)], timeout=15
    )
    tags = [
        match.group(0)
        for line in result.stdout.splitlines()
        if (match := _TAG.fullmatch(line.partition("refs/tags/")[2]))
    ]
    return max(tags, key=_version) if tags else None


def is_newer(tag: str, current: str = __version__) -> bool:
    match = _TAG.fullmatch(tag)
    if match is None:
        raise UpdateError(f"invalid release tag: {tag}")
    current_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", current)
    if current_match is None:
        raise UpdateError(f"invalid installed version: {current}")
    return tuple(map(int, match.groups())) > tuple(map(int, current_match.groups()))


def install_release(tag: str, repository: str | None = None) -> None:
    if _TAG.fullmatch(tag) is None:
        raise UpdateError(f"invalid release tag: {tag}")
    uv = shutil.which("uv")
    if uv is None:
        raise UpdateError("uv is required; install it from https://docs.astral.sh/uv/")
    source = _repository(repository)
    if not source.startswith("git+"):
        source = f"git+{source}"
    _run([uv, "tool", "install", "--force", f"{source}@{tag}"], timeout=300)


def update(*, check_only: bool = False) -> int:
    try:
        tag = latest_release()
        now = time.time()
        if tag is None:
            _write_state(now, tag)
            print("mergerail: the repository has no stable release tags yet")
        elif not is_newer(tag):
            _write_state(now, tag)
            print(f"mergerail {__version__} is up to date ({tag})")
        elif check_only:
            _write_state(now, tag)
            print(f"mergerail {tag} is available (installed: {__version__})")
        else:
            install_release(tag)
            _write_state(now, tag, installed=tag)
            print(f"mergerail: installed {tag}; the next invocation will use it")
        return 0
    except UpdateError as error:
        print(f"mergerail: update failed: {error}", file=sys.stderr)
        return 1


def auto_update() -> str | None:
    if not _auto_update_enabled() or os.environ.get("MERGERAIL_UPDATE_RELAUNCHED"):
        return None
    now = time.time()
    state = _read_state()
    if now - _last_checked(state) < CHECK_INTERVAL:
        latest = state.get("latest")
        if (
            isinstance(latest, str)
            and latest == state.get("installed")
            and _TAG.fullmatch(latest)
            and is_newer(latest)
        ):
            return latest
        return None
    try:
        tag = latest_release()
        if tag is None or not is_newer(tag):
            _write_state(now, tag)
            return None
        install_release(tag)
        _write_state(now, tag, installed=tag)
        print(f"mergerail: installed {tag}; restarting with the new version")
        return tag
    except UpdateError as error:
        _write_state(now, None)
        print(f"mergerail: automatic update failed: {error}", file=sys.stderr)
        return None


def relaunch(arguments: list[str]) -> int:
    uv = shutil.which("uv")
    if uv is None:
        raise UpdateError("uv disappeared after installing the update")
    tools = Path(_run([uv, "tool", "dir", "--bin"], timeout=30).stdout.strip())
    executable = tools / ("mergerail.exe" if sys.platform == "win32" else "mergerail")
    if not executable.is_file():
        raise UpdateError(f"updated executable was not found in {tools}")
    environment = os.environ.copy()
    environment["MERGERAIL_UPDATE_RELAUNCHED"] = "1"
    try:
        return subprocess.run([str(executable), *arguments], env=environment).returncode
    except OSError as error:
        raise UpdateError(str(error)) from error


def _repository(repository: str | None = None) -> str:
    value = repository or os.environ.get("MERGERAIL_UPDATE_REPOSITORY", REPOSITORY)
    return value.removeprefix("git+")


def _version(tag: str) -> tuple[int, int, int]:
    match = _TAG.fullmatch(tag)
    assert match is not None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(str(error)) from error
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        raise UpdateError(detail[-1] if detail else f"command exited with {result.returncode}")
    return result


def _auto_update_enabled() -> bool:
    return os.environ.get("MERGERAIL_AUTO_UPDATE", "1").strip().lower() not in _FALSE


def _state_path() -> Path:
    if custom := os.environ.get("MERGERAIL_UPDATE_STATE"):
        return Path(custom).expanduser()
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "mergerail" / "update.json"


def _read_state() -> dict[str, object]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _last_checked(state: dict[str, object] | None = None) -> float:
    checked_at = (state or _read_state()).get("checked_at", 0)
    return float(checked_at) if isinstance(checked_at, int | float) else 0.0


def _write_state(
    checked_at: float, latest: str | None, *, installed: str | None = None
) -> None:
    path = _state_path()
    try:
        state = _read_state()
        state.update({"checked_at": checked_at, "latest": latest})
        if installed is not None:
            state["installed"] = installed
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state) + "\n", encoding="utf-8")
    except OSError:
        pass
