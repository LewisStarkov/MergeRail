"""Install stable MergeRail releases directly from the Git repository."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn, Protocol

from . import __version__
from .filelock import exclusive_file
from .tasks import write_atomic

REPOSITORY = "https://github.com/LewisStarkov/MergeRail.git"
CHECK_INTERVAL = 24 * 60 * 60
_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_REVISION = re.compile(r"[0-9a-fA-F]{40,64}")
_FALSE = {"0", "false", "no", "off"}
_RELAUNCH_SENTINEL = "MERGERAIL_UPDATE_RELAUNCHED"
_runtime_cooldown: dict[str, float] = {}


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    tag: str
    revision: str
    repository: str


class Candidate(Protocol):
    @property
    def tag(self) -> str: ...

    @property
    def revision(self) -> str: ...

    @property
    def repository(self) -> str: ...


def latest_release(repository: str | None = None) -> str | None:
    candidate = _discover_release(repository)
    return candidate.tag if candidate is not None else None


def is_newer(tag: str, current: str = __version__) -> bool:
    match = _TAG.fullmatch(tag)
    if match is None:
        raise UpdateError(f"invalid release tag: {tag}")
    current_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", current)
    if current_match is None:
        raise UpdateError(f"invalid installed version: {current}")
    return tuple(map(int, match.groups())) > tuple(map(int, current_match.groups()))


def install_release(
    tag: str, repository: str | None = None, *, revision: str | None = None
) -> None:
    if _TAG.fullmatch(tag) is None:
        raise UpdateError(f"invalid release tag: {tag}")
    reference = revision or tag
    if revision is not None and _REVISION.fullmatch(revision) is None:
        raise UpdateError(f"invalid release revision: {revision}")
    source = _repository(repository)
    try:
        with exclusive_file(_install_lock_path()):
            _install_release_unlocked(tag, source, reference)
    except OSError as error:
        raise UpdateError(str(error)) from error


def _install_release_unlocked(tag: str, repository: str, reference: str) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise UpdateError("uv is required; install it from https://docs.astral.sh/uv/")
    source = repository
    if not source.startswith("git+"):
        source = f"git+{source}"
    _run(
        [uv, "--no-config", "tool", "install", "--force", f"{source}@{reference}"],
        timeout=300,
        cwd=_trusted_cwd(),
    )


def update(*, check_only: bool = False) -> int:
    try:
        repository = _repository()
        candidate = _discover_release(repository)
        now = time.time()
        if candidate is None:
            _write_state(now, None, repository=repository)
            print("mergerail: the repository has no stable release tags yet")
        elif not is_newer(candidate.tag):
            _write_state(now, candidate, repository=repository)
            print(f"mergerail {__version__} is up to date ({candidate.tag})")
        elif check_only:
            _write_state(now, candidate, repository=repository)
            print(f"mergerail {candidate.tag} is available (installed: {__version__})")
        else:
            _install_release(candidate)
            _write_state(now, candidate, installed=candidate, repository=repository)
            print(f"mergerail: installed {candidate.tag}; the next invocation will use it")
        return 0
    except UpdateError as error:
        print(f"mergerail: update failed: {error}", file=sys.stderr)
        return 1


def auto_update() -> str | None:
    relaunched = os.environ.pop(_RELAUNCH_SENTINEL, None)
    if not _auto_update_enabled() or relaunched:
        return None
    repository = _repository()
    now = time.time()
    state = _read_state()
    if now - _last_checked(state, repository) < CHECK_INTERVAL:
        installed = _state_candidate(state, "installed")
        if installed is not None and installed.repository == repository and is_newer(installed.tag):
            return installed.tag
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
        candidate = _discover_release(repository)
        if candidate is None or not is_newer(candidate.tag):
            _write_state(now, candidate, repository=repository)
            return None
        _install_release(candidate)
        _write_state(now, candidate, installed=candidate, repository=repository)
        print(f"mergerail: installed {candidate.tag}; restarting with the new version")
        return candidate.tag
    except UpdateError as error:
        _write_state(now, None, repository=repository)
        print(f"mergerail: automatic update failed: {error}", file=sys.stderr)
        return None


def runtime_candidate() -> ReleaseCandidate | None:
    """Return a newer release without installing it, at most once per interval."""
    if not _auto_update_enabled():
        return None
    repository = _repository()
    now = time.time()
    state = _read_state()
    installed = _state_candidate(state, "installed")
    if installed is not None and installed.repository == repository and is_newer(installed.tag):
        return installed
    monotonic_now = time.monotonic()
    if monotonic_now - _runtime_cooldown.get(repository, float("-inf")) < CHECK_INTERVAL:
        return None
    if now - _last_checked(state, repository) < CHECK_INTERVAL:
        return None
    try:
        candidate = _discover_release(repository)
        if not _write_state(now, candidate, repository=repository):
            _runtime_cooldown[repository] = monotonic_now
        return candidate if candidate is not None and is_newer(candidate.tag) else None
    except UpdateError as error:
        if not _write_state(now, None, repository=repository):
            _runtime_cooldown[repository] = monotonic_now
        print(f"mergerail: runtime update check failed: {error}", file=sys.stderr)
        return None


def install_candidate(candidate: Candidate) -> bool:
    """Install a candidate found by the running service after it has stopped."""
    pinned = ReleaseCandidate(
        candidate.tag,
        candidate.revision.lower(),
        _repository(candidate.repository),
    )
    now = time.time()
    persistence_error: OSError | None = None
    try:
        with exclusive_file(_install_lock_path()), exclusive_file(_state_lock_path()):
            state = _read_state()
            if _state_candidate(state, "installed") == pinned:
                print(f"mergerail: {pinned.tag} is already installed; restarting")
                return True
            _install_release_unlocked(pinned.tag, pinned.repository, pinned.revision)
            try:
                _write_state_unlocked(
                    now,
                    pinned,
                    installed=pinned,
                    repository=pinned.repository,
                )
            except OSError as error:
                persistence_error = error
        print(f"mergerail: installed {pinned.tag}; restarting with the new version")
        if persistence_error is not None:
            print(
                f"mergerail: update state could not be saved: {persistence_error}",
                file=sys.stderr,
            )
        return True
    except (OSError, UpdateError) as error:
        _write_state(now, pinned, repository=pinned.repository)
        print(f"mergerail: runtime update failed: {error}", file=sys.stderr)
        return False


def relaunch(arguments: list[str]) -> NoReturn:
    uv = shutil.which("uv")
    if uv is None:
        raise UpdateError("uv disappeared after installing the update")
    tools = Path(
        _run(
            [uv, "--no-config", "tool", "dir", "--bin"],
            timeout=30,
            cwd=_trusted_cwd(),
        ).stdout.strip()
    )
    executable = tools / ("mergerail.exe" if sys.platform == "win32" else "mergerail")
    if not executable.is_file():
        raise UpdateError(f"updated executable was not found in {tools}")
    environment = os.environ.copy()
    environment[_RELAUNCH_SENTINEL] = "1"
    try:
        if sys.platform == "win32":
            subprocess.Popen([str(executable), *arguments], env=environment)
            raise SystemExit(0)
        os.execve(str(executable), [str(executable), *arguments], environment)
    except OSError as error:
        raise UpdateError(str(error)) from error


def _repository(repository: str | None = None) -> str:
    value = repository or os.environ.get("MERGERAIL_UPDATE_REPOSITORY", REPOSITORY)
    return value.removeprefix("git+")


def _discover_release(repository: str | None = None) -> ReleaseCandidate | None:
    source = _repository(repository)
    result = _run(
        ["git", "ls-remote", "--tags", source], timeout=15, cwd=_trusted_cwd()
    )
    revisions: dict[str, str] = {}
    for line in result.stdout.splitlines():
        revision, separator, ref = line.partition("\t")
        if separator and _REVISION.fullmatch(revision):
            revisions[ref] = revision.lower()
    tags = [
        match.group(0)
        for ref in revisions
        if (match := _TAG.fullmatch(ref.removeprefix("refs/tags/")))
    ]
    if not tags:
        return None
    tag = max(tags, key=_version)
    ref = f"refs/tags/{tag}"
    revision = revisions.get(f"{ref}^{{}}", revisions[ref])
    return ReleaseCandidate(tag, revision, source)


def _install_release(candidate: ReleaseCandidate) -> None:
    install_release(
        candidate.tag,
        candidate.repository,
        revision=candidate.revision,
    )


def _trusted_cwd() -> Path:
    path = _user_update_root() / "subprocess"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise UpdateError(f"cannot create trusted update directory {path}: {error}") from error
    return path


def _version(tag: str) -> tuple[int, int, int]:
    match = _TAG.fullmatch(tag)
    assert match is not None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _run(
    command: list[str], *, timeout: int, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
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
    return _user_update_root() / "update.json"


def _read_state() -> dict[str, object]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _last_checked(state: dict[str, object], repository: str) -> float:
    stored_repository = state.get("repository")
    if stored_repository is not None and stored_repository != repository:
        return 0.0
    checked_at = state.get("checked_at", 0)
    if not isinstance(checked_at, int | float):
        return 0.0
    value = float(checked_at)
    return value if math.isfinite(value) else 0.0


def _write_state(
    checked_at: float,
    latest: ReleaseCandidate | None,
    *,
    installed: ReleaseCandidate | None = None,
    repository: str,
) -> bool:
    try:
        with exclusive_file(_state_lock_path()):
            _write_state_unlocked(
                checked_at,
                latest,
                installed=installed,
                repository=repository,
            )
        return True
    except OSError:
        return False


def _write_state_unlocked(
    checked_at: float,
    latest: ReleaseCandidate | None,
    *,
    installed: ReleaseCandidate | None = None,
    repository: str,
) -> None:
    state = _read_state()
    state.update(
        {
            "checked_at": checked_at,
            "latest": latest.tag if latest is not None else None,
            "latest_candidate": asdict(latest) if latest is not None else None,
            "repository": repository,
        }
    )
    if installed is not None:
        state.update(
            {
                "installed": installed.tag,
                "installed_candidate": asdict(installed),
            }
        )
    write_atomic(_state_path(), json.dumps(state) + "\n")


def _state_candidate(state: dict[str, object], name: str) -> ReleaseCandidate | None:
    raw = state.get(f"{name}_candidate")
    if not isinstance(raw, dict):
        return None
    tag = raw.get("tag")
    revision = raw.get("revision")
    repository = raw.get("repository")
    if (
        not isinstance(tag, str)
        or _TAG.fullmatch(tag) is None
        or not isinstance(revision, str)
        or _REVISION.fullmatch(revision) is None
        or not isinstance(repository, str)
        or not repository
    ):
        return None
    return ReleaseCandidate(tag, revision.lower(), _repository(repository))


def _state_lock_path() -> Path:
    path = _state_path()
    return path.with_name(f"{path.name}.lock")


def _install_lock_path() -> Path:
    if custom := os.environ.get("MERGERAIL_UPDATE_LOCK"):
        return Path(custom).expanduser()
    return _user_update_root() / "install.lock"


def _user_update_root() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "mergerail"
