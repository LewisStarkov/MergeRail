"""Git synchronization across the Docker boundary.

The host only creates bundles from its own object database and imports one
named, validated commit. Project checkouts and their tar archives are handled
inside disposable containers by :mod:`mergerail.execution.worker`.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


class SyncError(RuntimeError):
    """A repository snapshot could not be safely frozen or imported."""


_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_RESULT_REF = "refs/heads/mergerail-result"
_HOST_GIT_OUTPUT_LIMIT = 8 * 1024 * 1024


def _git_environment() -> dict[str, str]:
    env = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_EXTERNAL_DIFF",
        "GIT_DIFF_OPTS",
        "GIT_CONFIG",
    ):
        env.pop(name, None)
    return env


def _disable_local_drivers(root: Path) -> list[str]:
    """Override every repo-local external filter, diff, and merge driver."""

    try:
        returncode, stdout, _stderr = _run_bounded(
            [
                "git",
                "config",
                "--includes",
                "--null",
                "--name-only",
                "--get-regexp",
                r"^(filter|diff|merge)\..*\.",
            ],
            cwd=root,
            env=_git_environment(),
            timeout=10,
            output_limit=256 * 1024,
        )
    except (OSError, subprocess.SubprocessError, SyncError) as error:
        raise SyncError(f"cannot inspect local Git driver settings: {error}") from error
    if returncode not in {0, 1}:
        raise SyncError("cannot inspect local Git driver settings")
    config_names: set[str] = set()
    for raw_name in stdout.split(b"\0"):
        if not raw_name:
            continue
        try:
            name = raw_name.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise SyncError("local Git driver name is not valid UTF-8") from error
        section, separator, rest = name.partition(".")
        driver, final_separator, key = rest.rpartition(".")
        if (
            section.lower() not in {"filter", "diff", "merge"}
            or not separator
            or not final_separator
        ):
            raise SyncError("local Git driver setting has an unsupported name")
        if (
            not driver
            or "=" in driver
            or any(ord(char) < 32 or ord(char) == 127 for char in driver)
        ):
            raise SyncError("local Git driver setting has an unsafe subsection")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", key):
            raise SyncError("local Git driver setting has an unsupported key")
        config_names.add(f"{section.lower()}\0{driver}\0{key.lower()}")
    overrides: list[str] = []
    drivers: dict[str, set[str]] = {section: set() for section in ("filter", "diff", "merge")}
    for name in config_names:
        section, driver, _key = name.split("\0")
        drivers[section].add(driver)

    def option(section: str, driver: str, key: str, value: str) -> tuple[str, str]:
        # `git -c` parses quotes as part of the subsection. Its key parser
        # already uses the first/last period as delimiters, so preserve the
        # actual subsection bytes here, including spaces and Unicode.
        return "-c", f"{section}.{driver}.{key}={value}"

    for driver in sorted(drivers["filter"]):
        for key in ("clean", "smudge", "process"):
            overrides.extend(option("filter", driver, key, ""))
        overrides.extend(option("filter", driver, "required", "false"))
    for driver in sorted(drivers["diff"]):
        for key in ("command", "textconv"):
            overrides.extend(option("diff", driver, key, ""))
    for driver in sorted(drivers["merge"]):
        for key in ("driver", "recursive"):
            overrides.extend(option("merge", driver, key, ""))
    return overrides


def host_git_command(root: Path, *args: str) -> tuple[list[str], dict[str, str]]:
    """Build a sanitized Git argv/environment pair for host delivery code.

    This helper is intentionally public for the Docker-aware delivery path.
    Callers still need to choose operations which never check out project files
    on the host.
    """

    command = [
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        f"core.attributesFile={os.devnull}",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "credential.helper=",
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.safecrlf=false",
        "-c",
        "diff.external=",
        *_disable_local_drivers(root),
        *args,
    ]
    return command, _git_environment()


def _run_bounded(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    output_limit: int = _HOST_GIT_OUTPUT_LIMIT,
) -> tuple[int, bytes, bytes]:
    """Capture both pipes with a hard byte cap and terminate on timeout."""

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise SyncError(f"cannot run host git: {error}") from error
    assert process.stdout is not None and process.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    exceeded = threading.Event()
    total_lock = threading.Lock()
    total = 0

    def drain(name: str, pipe: BinaryIO) -> None:
        nonlocal total
        while block := pipe.read(64 * 1024):
            with total_lock:
                remaining = max(0, output_limit - total)
                if remaining:
                    buffers[name].extend(block[:remaining])
                    total += min(len(block), remaining)
                if len(block) > remaining:
                    exceeded.set()
                    with suppress(OSError):
                        process.kill()

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        for reader in readers:
            reader.join(timeout=2)
        raise SyncError("host Git command timed out") from error
    for reader in readers:
        reader.join(timeout=2)
    if any(reader.is_alive() for reader in readers):
        process.kill()
        raise SyncError("host Git output stream did not close")
    if exceeded.is_set():
        raise SyncError(f"host Git output exceeded the {_HOST_GIT_OUTPUT_LIMIT}-byte limit")
    return process.returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])


def host_git(root: Path, *args: str, check: bool = True, timeout: float = 120) -> str:
    """Run Git without user/system hooks, filters, pagers, or shell expansion."""

    command, env = host_git_command(root, *args)
    try:
        returncode, stdout, stderr = _run_bounded(command, cwd=root, env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise SyncError(f"cannot run host git: {error}") from error
    if check and returncode != 0:
        detail = (stderr or stdout).decode(errors="replace").strip()[-2000:]
        raise SyncError(f"git {' '.join(args)} failed: {detail or returncode}")
    return stdout.decode(errors="replace").strip()


def resolve_commit(root: Path, revision: str) -> str:
    """Resolve a local commit name once and return its immutable object id."""

    if not revision or revision.startswith("-") or "\x00" in revision:
        raise SyncError("base must name a local Git commit")
    sha = host_git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if not _OID.fullmatch(sha):
        raise SyncError("Git returned an invalid commit id")
    return sha


def create_bundle(root: Path, revisions: tuple[str, ...], *, max_bytes: int) -> bytes:
    """Create a bounded bundle to stdout, containing only the named revisions."""

    if not revisions or any(not _OID.fullmatch(item) for item in revisions):
        raise SyncError("bundle revisions must be resolved commit ids")
    token = uuid4().hex
    refs = [f"refs/mergerail-docker/{token}/{index}" for index in range(len(revisions))]
    zero = "0" * len(revisions[0])
    created: list[str] = []
    process: subprocess.Popen[bytes] | None = None
    try:
        for ref, sha in zip(refs, revisions, strict=True):
            host_git(root, "update-ref", ref, sha, zero)
            created.append(ref)
        command = [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "credential.helper=",
            "bundle",
            "create",
            "-",
            *refs,
        ]
        process = subprocess.Popen(
            command,
            cwd=root,
            env=_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        data = bytearray()
        stderr = bytearray()
        overflow = threading.Event()

        def drain_error() -> None:
            assert process is not None and process.stderr is not None
            while block := process.stderr.read(8192):
                if len(stderr) < 16 * 1024:
                    stderr.extend(block[: 16 * 1024 - len(stderr)])

        error_reader = threading.Thread(target=drain_error, daemon=True)
        error_reader.start()
        while block := process.stdout.read(min(1024 * 1024, max_bytes + 1 - len(data))):
            data.extend(block)
            if len(data) > max_bytes:
                overflow.set()
                process.kill()
                break
        process.stdout.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            raise SyncError("Git bundle creation did not stop after output was bounded") from error
        error_reader.join(timeout=2)
        if error_reader.is_alive():
            raise SyncError("Git bundle diagnostic stream did not close")
        if overflow.is_set():
            raise SyncError(f"repository snapshot exceeds the {max_bytes}-byte bundle limit")
        if process.returncode != 0:
            raise SyncError(
                f"git bundle create failed: {bytes(stderr).decode(errors='replace')[-2000:]}"
            )
        raw = bytes(data)
        if not raw.startswith((b"# v2 git bundle\n", b"# v3 git bundle\n")):
            raise SyncError("Git produced an invalid bundle header")
        return raw
    except OSError as error:
        raise SyncError(f"cannot create Git bundle: {error}") from error
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        for ref in created:
            host_git(root, "update-ref", "-d", ref, check=False)


def bundle_refs(bundle: bytes) -> dict[str, str]:
    """Return advertised object ids and refs from a bounded Git bundle header."""

    header, separator, _pack = bundle[: 1024 * 1024].partition(b"\n\n")
    if not separator or not header.startswith((b"# v2 git bundle\n", b"# v3 git bundle\n")):
        raise SyncError("Git bundle header is missing or too large")
    found: dict[str, str] = {}
    for line in header.splitlines()[1:]:
        if not line or line.startswith(b"-"):
            continue
        fields = line.split(b" ", 1)
        if len(fields) != 2:
            raise SyncError("Git bundle has a malformed advertised ref")
        try:
            sha, ref = (part.decode("ascii") for part in fields)
        except UnicodeDecodeError as error:
            raise SyncError("Git bundle ref is not ASCII") from error
        if not _OID.fullmatch(sha) or not ref.startswith("refs/mergerail-docker/"):
            raise SyncError("Git bundle contains an unexpected advertised ref")
        if sha in found:
            continue
        found[sha] = ref
    return found


def validate_bundle_header(bundle: bytes, expected_sha: str) -> None:
    """Check a single advertised ref before Git sees an imported bundle."""

    if not _OID.fullmatch(expected_sha):
        raise SyncError("expected result is not a commit id")
    header, separator, _pack = bundle[: 1024 * 1024].partition(b"\n\n")
    if not separator or not header.startswith((b"# v2 git bundle\n", b"# v3 git bundle\n")):
        raise SyncError("result bundle header is missing or too large")
    lines = header.splitlines()[1:]
    advertised: list[tuple[str, str]] = []
    for line in lines:
        if not line:
            continue
        if line.startswith(b"-"):
            raise SyncError("result bundle must not depend on prerequisite commits")
        fields = line.split(b" ", 1)
        if len(fields) != 2:
            raise SyncError("result bundle has a malformed advertised ref")
        try:
            oid, ref = (item.decode("ascii") for item in fields)
        except UnicodeDecodeError as error:
            raise SyncError("result bundle ref is not ASCII") from error
        advertised.append((oid, ref))
    if advertised != [(expected_sha, _RESULT_REF)]:
        raise SyncError("result bundle must advertise only the expected MergeRail ref")


def import_result_bundle(
    root: Path,
    bundle: bytes,
    *,
    expected_sha: str,
    base_sha: str,
    branch: str,
    max_bytes: int = 256 * 1024 * 1024,
) -> str:
    """Import exactly one sandbox commit and advance its controlled ref by CAS."""

    validate_bundle_header(bundle, expected_sha)
    if len(bundle) > max_bytes:
        raise SyncError("result bundle exceeds its configured byte limit")
    if not _OID.fullmatch(base_sha):
        raise SyncError("base commit id is invalid")
    checked = host_git(root, "check-ref-format", "--branch", branch, check=False)
    if not checked:
        raise SyncError("sandbox result branch name is invalid")
    ref = f"refs/heads/{branch}"
    _header, separator, pack = bundle.partition(b"\n\n")
    if not separator or not pack.startswith(b"PACK"):
        raise SyncError("result bundle pack data is missing or malformed")
    command, env = host_git_command(
        root,
        "-c",
        "transfer.fsckObjects=true",
        "index-pack",
        "--stdin",
        "--fsck-objects",
        "--strict",
    )
    try:
        indexed = subprocess.run(
            command,
            cwd=root,
            env=env,
            input=pack,
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SyncError(f"cannot import sandbox Git pack: {error}") from error
    if indexed.returncode != 0:
        detail = indexed.stderr.decode(errors="replace").strip()[-1600:]
        raise SyncError(
            f"sandbox Git pack failed strict validation: {detail or indexed.returncode}"
        )
    fetched = host_git(root, "rev-parse", "--verify", f"{expected_sha}^{{commit}}", check=False)
    if fetched != expected_sha:
        raise SyncError("imported bundle did not resolve to the expected commit")
    if not is_ancestor(root, base_sha, expected_sha):
        raise SyncError("sandbox result is not descended from the frozen base")
    current = host_git(root, "rev-parse", "--verify", ref, check=False)
    if current and not is_ancestor(root, current, expected_sha):
        raise SyncError("sandbox result does not contain the current durable checkpoint")
    zero = "0" * len(expected_sha)
    expected_old = current or zero
    try:
        host_git(root, "update-ref", ref, expected_sha, expected_old)
    except SyncError as error:
        raise SyncError("result branch changed while the sandbox commit was imported") from error
    return expected_sha


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    if not _OID.fullmatch(ancestor) or not _OID.fullmatch(descendant):
        return False
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        cwd=root,
        env=_git_environment(),
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def update_ref_cas(root: Path, branch: str, new_sha: str, old_sha: str | None) -> None:
    """Atomically update a local branch only if its prior value is unchanged."""

    if not _OID.fullmatch(new_sha):
        raise SyncError("new ref target is not a commit id")
    if host_git(root, "check-ref-format", "--branch", branch, check=False) == "":
        raise SyncError("branch name is invalid")
    ref = f"refs/heads/{branch}"
    old = old_sha or ("0" * len(new_sha))
    if not _OID.fullmatch(old):
        raise SyncError("expected old ref target is invalid")
    host_git(root, "update-ref", ref, new_sha, old)


__all__ = [
    "SyncError",
    "bundle_refs",
    "create_bundle",
    "host_git",
    "host_git_command",
    "import_result_bundle",
    "is_ancestor",
    "resolve_commit",
    "update_ref_cas",
    "validate_bundle_header",
]
