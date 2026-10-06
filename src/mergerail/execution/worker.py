"""Trusted, short-lived worker process inside a MergeRail container.

The protocol is one bounded JSON request on stdin followed by bounded JSONL
events and one result on stdout. This file and the rest of ``mergerail`` are
copied into a root-owned runtime ZIP; the project checkout is never on the
Python import path.
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import unicodedata
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, cast

REPO = Path("/work/repo")
HOME = Path("/work/home")
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 100_000
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_WINDOWS_RESERVED = re.compile(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?\Z")


class WorkerError(RuntimeError):
    pass


class _CallbackEventSink:
    def __init__(self, callback: Any) -> None:
        self.callback = callback

    def emit(self, event: Any) -> None:
        self.callback(event)


def _emit(value: dict[str, Any]) -> None:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        raise WorkerError("worker response exceeds the frame limit")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def _clean_environment(*, role: str = "", gateway: bool = False) -> None:
    """Drop image/user inherited variables and set a small predictable env."""

    role_home = HOME
    os.environ.clear()
    os.environ.update(
        {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(role_home),
            "TMPDIR": "/tmp",
            "TMP": "/tmp",
            "TEMP": "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_ATTR_NOSYSTEM": "1",
            "PIP_NO_INDEX": "1",
            "UV_OFFLINE": "1",
            "npm_config_offline": "true",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
    )
    if role:
        os.environ["MERGERAIL_AGENT_ROLE"] = role
    if gateway:
        os.environ["XDG_CONFIG_HOME"] = str(HOME / ".config")
        os.environ["XDG_CACHE_HOME"] = str(HOME / ".cache")
        os.environ["XDG_DATA_HOME"] = str(HOME / ".local" / "share")


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "credential.helper=",
        *args,
    ]
    result = subprocess.run(
        command,
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise WorkerError(f"git {' '.join(args)} failed: {detail or result.returncode}")
    return result.stdout.strip()


def _check_oid(value: object, label: str) -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise WorkerError(f"{label} is not a valid Git commit id")
    return value


def _check_branch(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("-") or "\x00" in value:
        raise WorkerError("branch name is invalid")
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", value],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise WorkerError("branch name is invalid")
    return value


def _fetch_bundle(bundle_path: Path, refs: object, wanted: set[str]) -> None:
    if not isinstance(refs, dict):
        raise WorkerError("snapshot bundle refs are missing")
    mapped: dict[str, str] = {}
    for sha, ref in refs.items():
        if not _OID.fullmatch(str(sha)) or not isinstance(ref, str):
            raise WorkerError("snapshot bundle ref is malformed")
        if not ref.startswith("refs/mergerail-docker/"):
            raise WorkerError("snapshot bundle contains an unexpected ref")
        mapped[str(sha)] = ref
    if set(mapped) != wanted:
        raise WorkerError("snapshot bundle does not contain exactly the requested commits")
    for sha in sorted(wanted):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(REPO),
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "--no-tags",
                str(bundle_path),
                mapped[sha],
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise WorkerError(
                f"cannot restore Git snapshot: {(result.stderr or result.stdout).strip()[-1200:]}"
            )
        fetched = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if fetched.returncode != 0 or fetched.stdout.strip() != sha:
            raise WorkerError("snapshot bundle ref did not resolve to its declared commit")


def _reset_from_bundle(request: dict[str, Any]) -> tuple[str, str]:
    base = _check_oid(request.get("base_sha"), "base")
    result = _check_oid(request.get("result_sha", base), "result")
    branch = _check_branch(request.get("branch", "mergerail"))
    bundle = Path("/tmp/snapshot.bundle")
    if not bundle.is_file() or bundle.stat().st_size > int(request.get("max_bundle_bytes", 0)):
        raise WorkerError("snapshot bundle is missing or exceeds its size limit")
    if REPO.exists():
        shutil.rmtree(REPO)
    REPO.parent.mkdir(parents=True, exist_ok=True)
    REPO.mkdir(parents=True)
    init = subprocess.run(["git", "init", str(REPO)], capture_output=True, text=True, check=False)
    if init.returncode != 0:
        raise WorkerError(f"cannot initialize workspace Git repo: {init.stderr.strip()[-1200:]}")
    _fetch_bundle(bundle, request.get("bundle_refs"), {base, result})
    # Git uses the exit code for --is-ancestor, so check it directly.
    ancestry = subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", base, result],
        capture_output=True,
        check=False,
    )
    if ancestry.returncode != 0 and not request.get("allow_diverged"):
        raise WorkerError("snapshot result does not descend from its base")
    _git(REPO, "checkout", "--detach", result)
    _git(REPO, "checkout", "-b", branch, result)
    config = REPO / ".git" / "config"
    config.write_text(
        "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n"
        "\tbare = false\n\tlogallrefupdates = true\n",
        encoding="utf-8",
    )
    hooks = REPO / ".git" / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks)
    hooks.mkdir(mode=0o700)
    return base, result


def _archive_members(archive_path: Path, *, limit_bytes: int) -> list[tarfile.TarInfo]:
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            members = archive.getmembers()
    except (OSError, tarfile.TarError) as error:
        raise WorkerError(f"workspace archive is invalid: {error}") from error
    if len(members) > 100_000:
        raise WorkerError("workspace archive has too many entries")
    total = 0
    for member in members:
        total += max(member.size, 0)
        if total > limit_bytes:
            raise WorkerError("workspace archive expands beyond its size limit")
        path = PurePosixPath(member.name)
        if member.size < 0:
            raise WorkerError("workspace archive has a negative file size")
        if (
            path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts)
        ) and member.name not in {".", "./"}:
            raise WorkerError("workspace archive contains an unsafe path")
    return members


def _safe_extract_workspace(
    archive_path: Path,
    destination: Path,
    *,
    limit_bytes: int,
    protected_paths: object = (),
    excluded_paths: object = (),
) -> None:
    """Extract an opaque Docker copy archive without following archive paths."""

    if isinstance(protected_paths, list) and all(isinstance(item, str) for item in protected_paths):
        protected_values = cast(list[str], protected_paths)
    elif protected_paths == ():
        protected_values = []
    else:
        raise WorkerError("protected controller paths are malformed")
    protected = [
        tuple(
            unicodedata.normalize("NFC", part).rstrip(" .").casefold()
            for part in item.split("/")
            if part
        )
        for item in protected_values
        if item
    ]
    if not isinstance(excluded_paths, tuple | list) or any(
        not isinstance(item, str) for item in excluded_paths
    ):
        raise WorkerError("excluded archive paths are malformed")
    excluded = [
        tuple(
            unicodedata.normalize("NFC", part).rstrip(" .").casefold()
            for part in item.split("/")
            if part
        )
        for item in excluded_paths
        if item
    ]

    def canonical_parts(parts: Iterable[object]) -> tuple[str, ...]:
        return tuple(
            unicodedata.normalize("NFC", str(part)).rstrip(" .").casefold() for part in parts
        )

    def overlaps(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
        return left[: len(right)] == right or right[: len(left)] == left

    def resolve_link(relative: str, link: str) -> tuple[str, ...]:
        stack = list(PurePosixPath(relative).parts[:-1])
        for part in PurePosixPath(link).parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not stack:
                    raise WorkerError("workspace archive contains an outward symlink")
                stack.pop()
                continue
            stack.append(part)
        resolved = canonical_parts(stack)
        if not resolved or any(part == ".git" for part in resolved):
            raise WorkerError("workspace archive symlink targets Git metadata")
        if any(overlaps(resolved, item) for item in protected):
            raise WorkerError("workspace archive symlink targets MergeRail controller state")
        return resolved

    with tarfile.open(archive_path, mode="r:*") as archive:
        members = _archive_members(archive_path, limit_bytes=limit_bytes)
        names = [PurePosixPath(item.name) for item in members if item.name not in {".", "./"}]
        if names and all(
            len(item.parts) >= 2 and item.parts[0] == destination.name for item in names
        ):
            strip_root = destination.name
        else:
            strip_root = ""
        regular: list[tuple[tarfile.TarInfo, Path, str]] = []
        links: list[tuple[tarfile.TarInfo, Path, str]] = []
        directories: list[tuple[tarfile.TarInfo, Path, str]] = []
        seen: set[str] = set()
        for member in members:
            if member.name in {".", "./"}:
                continue
            path = PurePosixPath(member.name)
            parts = path.parts[1:] if strip_root and path.parts[0] == strip_root else path.parts
            if not parts:
                continue
            relative = PurePosixPath(*parts)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise WorkerError("workspace archive contains an unsafe path")
            # Git metadata belongs to the trusted snapshot bundle, never to the
            # agent's tar stream. Ignore even malicious .git symlinks/aliases.
            folded_parts = canonical_parts(relative.parts)
            if any(folded_parts[: len(item)] == item for item in excluded):
                continue
            if any(part == ".git" for part in folded_parts):
                if folded_parts[0] == ".git":
                    continue
                raise WorkerError("workspace archive contains a nested Git metadata alias")
            if any(overlaps(folded_parts, item) for item in protected):
                raise WorkerError("workspace archive contains MergeRail controller state")
            key = unicodedata.normalize("NFC", relative.as_posix()).casefold()
            if key in seen:
                raise WorkerError("workspace archive contains duplicate normalized paths")
            seen.add(key)
            target = destination.joinpath(*relative.parts)
            if not target.resolve(strict=False).is_relative_to(destination.resolve()):
                raise WorkerError("workspace archive escapes the checkout")
            if member.isdir():
                directories.append((member, target, relative.as_posix()))
            elif member.isreg():
                regular.append((member, target, relative.as_posix()))
            elif member.issym():
                links.append((member, target, relative.as_posix()))
            else:
                raise WorkerError("workspace archive contains a hard link or special file")

        destination.mkdir(parents=True, exist_ok=True)
        for _member, target, _relative in sorted(directories, key=lambda item: len(item[1].parts)):
            if target.exists() and target.is_symlink():
                raise WorkerError("workspace archive directory collides with a symlink")
            target.mkdir(parents=True, exist_ok=True, mode=0o755)
        for member, target, _relative in regular:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            parent = target.parent
            while parent != destination:
                if parent.is_symlink() or not parent.is_dir():
                    raise WorkerError("workspace archive traverses a non-directory")
                parent = parent.parent
            if target.is_symlink() or target.is_dir():
                raise WorkerError("workspace archive file collides with a directory or symlink")
            source = archive.extractfile(member)
            if source is None:
                raise WorkerError("workspace archive contains an unreadable file")
            data = source.read(member.size + 1)
            if len(data) != member.size:
                raise WorkerError("workspace archive file size does not match its header")
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
        # Symlinks are created only after every regular path was extracted.
        for member, target, relative_name in links:
            link = member.linkname
            if not link or link.startswith(("/", "\\")) or "\\" in link or ":" in link:
                raise WorkerError("workspace archive contains an absolute or aliased symlink")
            resolve_link(relative_name, link)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if target.exists() or target.is_symlink():
                raise WorkerError("workspace archive symlink collides with an existing path")
            os.symlink(link, target)


def _prepare_owner(role: str, read_only: bool) -> None:
    if sys.platform == "win32":
        raise WorkerError("workspace preparation requires a POSIX container")
    if os.geteuid() != 65534:
        raise WorkerError("workspace preparation must run as the designated non-root UID")
    if role != "reviewer":
        HOME.mkdir(parents=True, exist_ok=True)
    if read_only:
        for path in [REPO, *REPO.rglob("*")]:
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode & 0o555)
    else:
        REPO.chmod(stat.S_IMODE(REPO.stat().st_mode) | 0o700)


def _restore_home(request: dict[str, Any]) -> None:
    if not request.get("restore_home"):
        if request.get("role") != "reviewer":
            HOME.mkdir(parents=True, exist_ok=True)
        return
    HOME.mkdir(parents=True, exist_ok=True)
    archive = Path("/tmp/home.tar")
    if not archive.is_file() or archive.stat().st_size > int(request.get("max_bundle_bytes", 0)):
        raise WorkerError("session archive is missing or exceeds its size limit")
    _safe_extract_workspace(
        archive,
        HOME,
        limit_bytes=int(request["max_bundle_bytes"]),
        excluded_paths=(".opencode", ".codex/auth.json", ".codex/config.toml"),
    )


def _sanitize_git_config() -> None:
    git_dir = REPO / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise WorkerError("workspace Git metadata was replaced")
    config = git_dir / "config"
    if config.is_symlink():
        config.unlink()
    config.write_text(
        "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n"
        "\tbare = false\n\tlogallrefupdates = true\n",
        encoding="utf-8",
    )
    hooks = git_dir / "hooks"
    if hooks.exists() or hooks.is_symlink():
        if hooks.is_symlink():
            hooks.unlink()
        else:
            shutil.rmtree(hooks)
    hooks.mkdir(mode=0o700)


def _validate_tree(sha: str, protected_paths: object = ()) -> None:
    """Reject cross-platform aliases, submodules, LFS pointers and escaping links."""

    raw = subprocess.run(
        ["git", "-C", str(REPO), "ls-tree", "-r", "-z", "--full-tree", sha],
        capture_output=True,
        check=False,
    )
    if raw.returncode != 0:
        raise WorkerError("cannot inspect sandbox result tree")
    seen_components: dict[str, str] = {}
    if not isinstance(protected_paths, list) or any(
        not isinstance(item, str) for item in protected_paths
    ):
        if protected_paths != ():
            raise WorkerError("protected controller paths are malformed")
        protected_paths = []
    protected = [
        tuple(unicodedata.normalize("NFC", item).casefold().split("/"))
        for item in protected_paths
        if item
    ]
    entries = raw.stdout.split(b"\0")
    for record in entries:
        if not record:
            continue
        metadata, separator, name_bytes = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise WorkerError("sandbox result tree has malformed entries")
        mode, kind, oid = fields
        try:
            name = name_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise WorkerError("sandbox result contains a non-UTF-8 path") from error
        path = PurePosixPath(name)
        components = path.parts
        if (
            path.is_absolute()
            or not components
            or any(part in {"", ".", ".."} for part in components)
        ):
            raise WorkerError(f"sandbox result contains an unsafe path: {name!r}")
        folded_components = tuple(
            unicodedata.normalize("NFC", item).casefold() for item in components
        )
        for protected_components in protected:
            if (
                folded_components[: len(protected_components)] == protected_components
                or protected_components[: len(folded_components)] == folded_components
            ):
                raise WorkerError("sandbox result contains MergeRail controller state")
        normalized = unicodedata.normalize("NFC", name)
        original_parts = PurePosixPath(name).parts
        normalized_parts = PurePosixPath(normalized).parts
        for index, _component in enumerate(normalized_parts):
            folded = "/".join(part.casefold() for part in normalized_parts[: index + 1])
            original = "/".join(original_parts[: index + 1])
            prior = seen_components.get(folded)
            if prior is not None and prior != original:
                raise WorkerError(
                    f"sandbox result has case-folding path collision: {prior!r}, {original!r}"
                )
            seen_components[folded] = original
        for component in components:
            if component.rstrip(" .").casefold() == ".git":
                raise WorkerError("sandbox result contains a .git path alias")
            if component.endswith((".", " ")) or ":" in component or "\\" in component:
                raise WorkerError("sandbox result contains a Windows or HFS path alias")
            if _WINDOWS_RESERVED.fullmatch(component):
                raise WorkerError("sandbox result contains a reserved Windows path")
            if any(ord(char) < 32 or ord(char) == 127 for char in component):
                raise WorkerError("sandbox result contains a control character in a path")
        if mode == b"160000" or kind == b"commit":
            raise WorkerError("sandbox result contains a Git submodule")
        if mode == b"120000":
            target_result = subprocess.run(
                ["git", "-C", str(REPO), "cat-file", "blob", oid.decode("ascii")],
                capture_output=True,
                check=False,
            )
            if target_result.returncode != 0:
                raise WorkerError("cannot inspect sandbox symlink")
            target = target_result.stdout.decode("utf-8", errors="surrogateescape")
            if target.startswith(("/", "\\")) or "\\" in target or ":" in target:
                raise WorkerError("sandbox result contains an outward symlink")
            resolved_parts = list(components[:-1])
            for part in PurePosixPath(target).parts:
                if part == "..":
                    if not resolved_parts:
                        raise WorkerError("sandbox result contains an outward symlink")
                    resolved_parts.pop()
                elif part not in {"", "."}:
                    resolved_parts.append(part)
            folded_target = tuple(
                unicodedata.normalize("NFC", part).rstrip(" .").casefold()
                for part in resolved_parts
            )
            if not folded_target or any(part == ".git" for part in folded_target):
                raise WorkerError("sandbox symlink targets Git metadata")
            if any(
                folded_target[: len(protected_path)] == protected_path
                or protected_path[: len(folded_target)] == folded_target
                for protected_path in protected
            ):
                raise WorkerError("sandbox symlink targets MergeRail controller state")
        elif mode in {b"100644", b"100755"}:
            size = int(
                subprocess.check_output(
                    ["git", "-C", str(REPO), "cat-file", "-s", oid.decode("ascii")],
                    text=True,
                ).strip()
            )
            if size <= 1024:
                contents = subprocess.run(
                    ["git", "-C", str(REPO), "cat-file", "blob", oid.decode("ascii")],
                    capture_output=True,
                    check=False,
                ).stdout
                if contents.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                    raise WorkerError("sandbox result contains an unresolved Git LFS pointer")
        else:
            raise WorkerError("sandbox result contains an unsupported file type")


def _validate_operator_config(base: str, result: str) -> None:
    def entries(sha: str) -> list[str]:
        return [
            entry
            for entry in _git(REPO, "ls-tree", "-z", sha).split("\0")
            if entry.partition("\t")[2].casefold() == "mergerail.toml"
        ]

    if entries(base) != entries(result):
        raise WorkerError("sandbox result changes operator-owned mergerail.toml")


def _commit_workspace(base: str, protected_paths: object = ()) -> str:
    _sanitize_git_config()
    current = _git(REPO, "rev-parse", "HEAD")
    ancestry = subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", base, current],
        capture_output=True,
        check=False,
    )
    if ancestry.returncode != 0:
        raise WorkerError("agent changed the workspace to a commit outside the frozen base")
    status = _git(REPO, "status", "--porcelain", "--untracked-files=all")
    if status:
        _git(REPO, "add", "-A")
        _sanitize_git_config()
        commit = subprocess.run(
            [
                "git",
                "-C",
                str(REPO),
                "-c",
                "user.name=MergeRail sandbox",
                "-c",
                "user.email=mergerail@sandbox.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--no-gpg-sign",
                "-m",
                "MergeRail sandbox checkpoint",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if commit.returncode != 0:
            raise WorkerError(
                "cannot checkpoint sandbox changes: "
                f"{(commit.stderr or commit.stdout).strip()[-1200:]}"
            )
    current = _git(REPO, "rev-parse", "HEAD")
    ancestry = subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", base, current],
        capture_output=True,
        check=False,
    )
    if ancestry.returncode != 0:
        raise WorkerError("sandbox checkpoint is not descended from the frozen base")
    _validate_tree(current, protected_paths)
    _validate_operator_config(base, current)
    fsck = subprocess.run(
        [
            "git",
            "-C",
            str(REPO),
            "-c",
            "transfer.fsckObjects=true",
            "fsck",
            "--strict",
            "--no-reflogs",
            current,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if fsck.returncode != 0:
        raise WorkerError(
            f"sandbox Git validation failed: {(fsck.stderr or fsck.stdout).strip()[-1200:]}"
        )
    return current


def _write_result_bundle(sha: str) -> None:
    repo = REPO
    _git(repo, "update-ref", "refs/heads/mergerail-result", sha)
    path = Path("/tmp/result.bundle")
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "bundle",
            "create",
            str(path),
            "refs/heads/mergerail-result",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise WorkerError(
            f"cannot export sandbox checkpoint: {(result.stderr or result.stdout).strip()[-1200:]}"
        )
    path.chmod(0o400)


def _reply_dict(reply: Any) -> dict[str, Any]:
    from dataclasses import asdict

    usage = reply.usage
    return {
        "text": reply.text,
        "is_error": reply.is_error,
        "cost_usd": reply.cost_usd,
        "context_tokens": reply.context_tokens,
        "seconds": reply.seconds,
        "structured": reply.structured,
        "session_id": reply.session_id,
        "diagnostics": [asdict(value) for value in reply.diagnostics],
        "usage": None
        if usage is None
        else {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
        },
    }


def _turn(request: dict[str, Any]) -> dict[str, Any]:
    from mergerail.backends import default_registry
    from mergerail.backends.base import SessionSpec, TurnRequest
    from mergerail.backends.external import ExternalBackend

    role = request.get("role")
    if role not in {"fixer", "reviewer"}:
        raise WorkerError("agent role is invalid")
    name = str(request.get("backend", "opencode")).strip().lower()
    if name == "codex" and not request.get("gateway"):
        raise WorkerError("Docker Codex requires its credential gateway")
    _restore_home(request)
    _clean_environment(role=str(role), gateway=bool(request.get("gateway")))
    if bool(request.get("gateway")):
        try:
            address = ipaddress.IPv4Address(request.get("gateway_host", ""))
        except ipaddress.AddressValueError as error:
            raise WorkerError("AI gateway must have a literal private IPv4 address") from error
        if (
            not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
        ):
            raise WorkerError("AI gateway must have a literal private IPv4 address")
        gateway_host = str(address)
    if bool(request.get("gateway")) and name == "opencode":
        selected_model = str(request.get("model", "opencode/space-bunny-free"))
        provider, separator, model_id = selected_model.partition("/")
        if provider != "opencode" or not separator or not model_id:
            raise WorkerError("Docker AI requires the selected OpenCode Zen model")
        config = {
            "model": selected_model,
            "share": "disabled",
            "plugin": [],
            "plugins": [],
            "mcp": {},
            "enabled_providers": ["opencode"],
            "autoupdate": False,
            "provider": {
                "opencode": {
                    "options": {"baseURL": f"http://{gateway_host}:8765/zen/v1"},
                    "models": {model_id: request.get("model_definition", {})},
                }
            },
        }
        # Runtime config has higher precedence than project settings; the CLI
        # cannot redirect model requests away from the fixed local gateway.
        os.environ["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, separators=(",", ":"))
        os.environ["XDG_CONFIG_HOME"] = "/tmp/mergerail-opencode-config"
        os.environ["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
        os.environ["OPENCODE_DISABLE_PRUNE"] = "true"
        os.environ["OPENCODE_AUTO_SHARE"] = "false"
        os.environ["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        os.environ["OPENCODE_PURE"] = "1"
        os.environ["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "1"
    settings_value = request.get("settings")
    settings = cast(dict[str, object], settings_value) if isinstance(settings_value, dict) else {}
    spec = SessionSpec(
        role=str(role),
        cwd=REPO,
        system_prompt=str(request.get("system_prompt", "")),
        read_only=bool(request.get("read_only")) or role == "reviewer",
        model=request.get("model") if isinstance(request.get("model"), str) else None,
        effort=str(request.get("effort", "")),
        permission="review" if role == "reviewer" else "safe",
        timeout=min(max(int(request.get("timeout", 3600)), 1), 3600),
        context_limit=min(max(int(request.get("context_limit", 160_000)), 1), 1_000_000),
        settings=settings,
        resume_session_id=request.get("resume_session_id")
        if isinstance(request.get("resume_session_id"), str)
        else None,
    )
    registry = default_registry()
    if name == "codex":
        from mergerail.backends.codex import CodexBackend

        prefix = "/backend-api/codex" if request.get("codex_auth") == "chatgpt" else "/v1"
        codex_home = HOME / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        os.environ["CODEX_HOME"] = str(codex_home)
        command = [
            "codex",
            "--config",
            'model_provider="mergerail"',
            "--config",
            'model_providers.mergerail.name="openai"',
            "--config",
            f'model_providers.mergerail.base_url="http://{gateway_host}:8765{prefix}"',
            "--config",
            'model_providers.mergerail.wire_api="responses"',
            "--config",
            "model_providers.mergerail.requires_openai_auth=false",
            "--config",
            "model_providers.mergerail.supports_websockets=false",
            "--config",
            'cli_auth_credentials_store="ephemeral"',
            "--config",
            'web_search="disabled"',
            "--config",
            "mcp_servers={}",
            "--config",
            'projects./work/repo.trust_level="untrusted"',
            "--config",
            "features.plugins=false",
            "--config",
            "features.hooks=false",
        ]
        # Reviewers use a different UID from the source owner. A read-only
        # fixer still needs Codex's native sandbox: its UID owns the source.
        registry.register(
            CodexBackend(command, sandboxed_externally=role == "reviewer" or not spec.read_only),
            replace=True,
        )
    external = request.get("external_backends")
    if isinstance(external, dict):
        for ext_name, command in external.items():
            if isinstance(ext_name, str) and ext_name.strip().lower() in {
                "opencode",
                "claude",
                "codex",
            }:
                raise WorkerError("external backends cannot replace built-in Docker providers")
            if (
                isinstance(ext_name, str)
                and isinstance(command, list)
                and command
                and all(isinstance(part, str) for part in command)
            ):
                registry.register_external(ExternalBackend(ext_name, command), replace=True)
    if name == "claude":
        raise WorkerError(
            f"Docker backend {name!r} is unsupported until provider authentication is validated"
        )
    events = 0

    def emit(event: dict[str, Any]) -> None:
        nonlocal events
        events += 1
        if events > MAX_EVENTS:
            raise WorkerError("agent emitted too many streaming events")
        _emit({"type": "event", "event": event})

    session = registry.open_session(name, spec, _CallbackEventSink(emit))
    try:
        reply = session.ask(TurnRequest(str(request.get("prompt", "")), request.get("schema")))
    finally:
        with suppress(Exception):
            session.close()
    result = _reply_dict(reply)
    return result


def _probe(request: dict[str, Any]) -> dict[str, Any]:
    from mergerail.backends import default_registry
    from mergerail.backends.external import ExternalBackend

    name = str(request.get("backend", "opencode")).strip().lower()
    if name == "claude":
        return {
            "name": name,
            "available": False,
            "reason": (
                f"Docker backend {name!r} is unsupported until provider authentication is validated"
            ),
        }
    registry = default_registry()
    external = request.get("external_backends")
    if isinstance(external, dict):
        for ext_name, command in external.items():
            if isinstance(ext_name, str) and ext_name.strip().lower() in {
                "opencode",
                "claude",
                "codex",
            }:
                raise WorkerError("external backends cannot replace built-in Docker providers")
            if (
                isinstance(ext_name, str)
                and isinstance(command, list)
                and command
                and all(isinstance(part, str) for part in command)
            ):
                registry.register_external(ExternalBackend(ext_name, command), replace=True)
    info = registry.probe(name)
    if isinstance(info, dict):
        raise WorkerError("backend registry returned multiple probes for one backend")
    return {
        "name": info.name,
        "available": info.available,
        "version": info.version,
        "reason": info.reason,
        "capabilities": {
            field: getattr(info.capabilities, field)
            for field in info.capabilities.__dataclass_fields__
        },
    }


def _run_checks(request: dict[str, Any]) -> dict[str, Any]:
    from mergerail.detect import Check

    checks = request.get("checks", [])
    if not isinstance(checks, list):
        raise WorkerError("checks must be a list")
    limit = min(max(int(request.get("log_limit_bytes", 1_048_576)), 1024), 100 * 1024 * 1024)
    deadline_seconds = min(max(int(request.get("timeout", 1800)), 1), 1800)
    results: list[dict[str, Any]] = []
    all_passed = True
    for item in checks:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise WorkerError("check entry is malformed")
        command = item.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or "\x00" in part for part in command)
        ):
            raise WorkerError("check command must be a non-empty argv list")
        check = Check(item["name"], list(command))
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                check.command,
                cwd=REPO,
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as error:
            results.append({"name": check.name, "passed": False, "output": str(error)})
            all_passed = False
            continue
        output = bytearray()
        assert process.stdout is not None
        too_large = False
        while True:
            ready, _write, _error = select.select([process.stdout], [], [], 0.2)
            if not ready:
                if process.poll() is not None:
                    break
                if time.monotonic() - started > deadline_seconds:
                    process.kill()
                    break
                continue
            chunk = cast(io.BufferedReader, process.stdout).read1(65536)
            if not chunk:
                if process.poll() is not None:
                    break
                continue
            remaining = limit + 1 - len(output)
            output.extend(chunk[:remaining])
            if len(output) > limit:
                too_large = True
                process.kill()
                break
            if time.monotonic() - started > deadline_seconds:
                process.kill()
                break
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if too_large:
            report = f"output exceeded {limit} bytes"
            passed = False
        elif time.monotonic() - started > deadline_seconds:
            report = f"timed out after {deadline_seconds}s"
            passed = False
        else:
            report = output.decode("utf-8", errors="replace")
            passed = process.returncode == 0
        all_passed = all_passed and passed
        results.append({"name": check.name, "passed": passed, "output": report[-limit:]})
    summary = (
        "\n".join(
            f"{item['name']}: {'PASS' if item['passed'] else 'FAIL'} — "
            f"{(item['output'].splitlines()[-1] if item['output'].splitlines() else '')[:200]}"
            for item in results
        )
        or "(no checks configured for this project)"
    )
    details = "\n".join(
        f"{item['name']}: FAIL\n{item['output'][-1500:]}" for item in results if not item["passed"]
    )
    return {
        "passed": all_passed,
        "report": summary + ("\n" + details if details else ""),
        "results": results,
    }


def _preflight_probe(request: dict[str, Any]) -> dict[str, Any]:
    limits = request.get("limits")
    if not isinstance(limits, dict):
        raise WorkerError("preflight limits are missing")
    expected = {
        "memory": str(int(limits["memory_bytes"])),
        "pids": str(int(limits["pids_limit"])),
        "swap": "0",
    }
    observed: dict[str, str] = {}
    for key, path in (
        ("memory", Path("/sys/fs/cgroup/memory.max")),
        ("pids", Path("/sys/fs/cgroup/pids.max")),
        ("swap", Path("/sys/fs/cgroup/memory.swap.max")),
    ):
        if not path.is_file():
            raise WorkerError(f"cgroup v2 {key} limit is unavailable")
        observed[key] = path.read_text(encoding="ascii").strip()
        if observed[key] != expected[key]:
            raise WorkerError(
                f"cgroup v2 {key} limit mismatch ({observed[key]} != {expected[key]})"
            )
    cpu_file = Path("/sys/fs/cgroup/cpu.max")
    if not cpu_file.is_file():
        raise WorkerError("cgroup v2 CPU limit is unavailable")
    quota, period = cpu_file.read_text(encoding="ascii").split()
    expected_quota = int(float(limits["cpus"]) * int(period))
    if quota == "max" or int(quota) != expected_quota:
        raise WorkerError("cgroup v2 CPU limit does not match the requested quota")
    observed["cpu"] = f"{quota} {period}"
    enospc = False
    try:
        with Path("/work/fill").open("wb") as output:
            while True:
                output.write(b"x" * 65536)
    except OSError as error:
        if error.errno != 28:
            raise WorkerError(f"workspace tmpfs probe failed before ENOSPC: {error}") from error
        enospc = True
    if not enospc:
        raise WorkerError("workspace tmpfs did not return ENOSPC at its bound")
    observed["tmpfs_enospc"] = "verified"
    return {"ok": True, "cgroup": observed}


def _recover(request: dict[str, Any]) -> dict[str, Any]:
    base, _result = _reset_from_bundle(request)
    workspace_tar = Path("/tmp/workspace.tar")
    if not workspace_tar.is_file() or workspace_tar.stat().st_size > int(
        request["max_bundle_bytes"]
    ):
        raise WorkerError("workspace archive is missing or exceeds its size limit")
    # The Git database and config came from the trusted frozen bundle. Replace
    # its source tree from the quiesced archive while retaining that .git dir.
    for entry in REPO.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_symlink() or not entry.is_dir():
            entry.unlink()
        else:
            shutil.rmtree(entry)
    _safe_extract_workspace(
        workspace_tar,
        REPO,
        limit_bytes=int(request["max_workspace_bytes"]),
        protected_paths=request.get("protected_paths", []),
    )
    _sanitize_git_config()
    head = _commit_workspace(base, request.get("protected_paths", []))
    _write_result_bundle(head)
    return {"head": head}


def _same_uid_processes(uid: int, own_pid: int) -> list[tuple[int, str]]:
    peers: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == own_pid:
            continue
        try:
            status = (entry / "status").read_text(encoding="ascii")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        state = ""
        uids: tuple[int, ...] = ()
        for line in status.splitlines():
            if line.startswith("State:"):
                values = line.split()
                state = values[1] if len(values) > 1 else ""
            elif line.startswith("Uid:"):
                values = line.split()[1:]
                try:
                    uids = tuple(int(value) for value in values)
                except ValueError as error:
                    raise WorkerError(
                        "cannot verify process credentials before artifact export"
                    ) from error
                break
        if uid not in uids:
            continue
        if not uids or any(value != uid for value in uids[:3]):
            raise WorkerError(
                "artifact export found a process with mixed saved or effective credentials"
            )
        peers.append((pid, state))
    return peers


def _quiesce_same_uid() -> None:
    """Stop and terminate every untrusted peer before reading mutable tmpfs."""

    if sys.platform == "win32":
        raise WorkerError("artifact export requires a POSIX container")
    uid = os.geteuid()
    own_pid = os.getpid()
    deadline = time.monotonic() + 5
    empty_scans = 0
    while time.monotonic() < deadline:
        peers = [
            (pid, state)
            for pid, state in _same_uid_processes(uid, own_pid)
            if state not in {"Z", "X"}
        ]
        for pid, _state in peers:
            try:
                os.kill(pid, signal.SIGSTOP)
            except ProcessLookupError:
                continue
            except PermissionError as error:
                raise WorkerError(
                    "cannot stop an untrusted process before artifact export"
                ) from error
        time.sleep(0.03)
        stopped = [
            (pid, state)
            for pid, state in _same_uid_processes(uid, own_pid)
            if state not in {"Z", "X"}
        ]
        if any(state not in {"T", "t", "Z", "X"} for _pid, state in stopped):
            continue
        for pid, _state in stopped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as error:
                raise WorkerError(
                    "cannot terminate an untrusted process before artifact export"
                ) from error
        time.sleep(0.03)
        remaining = [
            (pid, state)
            for pid, state in _same_uid_processes(uid, own_pid)
            if state not in {"Z", "X"}
        ]
        if not remaining:
            empty_scans += 1
            if empty_scans >= 2:
                return
        else:
            empty_scans = 0
    raise WorkerError("untrusted processes did not quiesce before artifact export")


class _LimitedWriter(io.BufferedIOBase):
    def __init__(self, stream: io.BufferedIOBase, limit: int) -> None:
        super().__init__()
        self.stream = stream
        self.limit = limit
        self.written = 0

    def write(self, value: Any) -> int:
        if self.written + len(value) > self.limit:
            raise WorkerError("artifact archive exceeds its byte limit")
        count = self.stream.write(value)
        if count != len(value):
            raise WorkerError("artifact export stream stopped unexpectedly")
        self.written += count
        return count

    def flush(self) -> None:
        self.stream.flush()

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False


def _export_tree(source: Path, *, maximum: int, expanded_maximum: int) -> None:
    if not source.is_dir() or source.is_symlink():
        raise WorkerError("artifact export source is not a directory")
    output = _LimitedWriter(cast(io.BufferedIOBase, sys.stdout.buffer), maximum)
    files_bytes = 0
    members_count = 0
    with tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as archive:

        def add(path: Path, relative: str) -> None:
            nonlocal files_bytes, members_count
            info = path.lstat()
            members_count += 1
            if members_count > 100_000:
                raise WorkerError("artifact contains too many entries")
            item = tarfile.TarInfo(relative or ".")
            item.mode = stat.S_IMODE(info.st_mode)
            item.uid = 0
            item.gid = 0
            item.mtime = int(info.st_mtime)
            if stat.S_ISDIR(info.st_mode):
                item.type = tarfile.DIRTYPE
                item.size = 0
                archive.addfile(item)
                with os.scandir(path) as children:
                    names = sorted(child.name for child in children)
                for name in names:
                    if not relative and name == ".git":
                        continue
                    child_relative = f"{relative}/{name}" if relative else name
                    add(path / name, child_relative)
                return
            if stat.S_ISREG(info.st_mode):
                files_bytes += info.st_size
                if files_bytes > expanded_maximum:
                    raise WorkerError("artifact expands beyond the workspace byte limit")
                item.type = tarfile.REGTYPE
                item.size = info.st_size
                with path.open("rb") as handle:
                    archive.addfile(item, handle)
                return
            if stat.S_ISLNK(info.st_mode):
                item.type = tarfile.SYMTYPE
                item.linkname = os.readlink(path)
                item.size = 0
                archive.addfile(item)
                return
            raise WorkerError("artifact contains a hard link or special file")

        add(source, "")
    output.flush()


def export_main(arguments: list[str]) -> int:
    """Quiesce same-UID children and stream one opaque directory tar to stdout."""

    try:
        if len(arguments) != 3:
            raise WorkerError("artifact export requires source and byte limits")
        source = Path(arguments[0])
        maximum = int(arguments[1])
        expanded_maximum = int(arguments[2])
        if maximum < 1 or expanded_maximum < 1:
            raise WorkerError("artifact export limits must be positive")
        _quiesce_same_uid()
        _export_tree(source, maximum=maximum, expanded_maximum=expanded_maximum)
        return 0
    except BaseException as error:
        print(f"artifact export: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 1


def _merge_candidate(request: dict[str, Any]) -> dict[str, Any]:
    base, result = _reset_from_bundle(request)
    _sanitize_git_config()
    branch = _check_branch(request.get("branch"))
    if result == base:
        head = base
    else:
        forward = subprocess.run(
            ["git", "-C", str(REPO), "merge-base", "--is-ancestor", base, result],
            capture_output=True,
            check=False,
        )
        _git(REPO, "checkout", "-B", branch, result if forward.returncode == 0 else base)
        if forward.returncode == 0:
            head = result
        else:
            common = subprocess.run(
                ["git", "-C", str(REPO), "merge-base", base, result],
                capture_output=True,
                text=True,
                check=False,
            )
            if common.returncode != 0:
                raise WorkerError("sandbox integration commits have no common ancestor")
            merge = subprocess.run(
                [
                    "git",
                    "-C",
                    str(REPO),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "user.name=MergeRail integration",
                    "-c",
                    "user.email=mergerail@sandbox.invalid",
                    "merge",
                    "--no-commit",
                    "--no-ff",
                    result,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if merge.returncode != 0:
                _git(REPO, "merge", "--abort", check=False)
                raise WorkerError(
                    f"sandbox merge failed: {(merge.stderr or merge.stdout).strip()[-1500:]}"
                )
            commit = subprocess.run(
                [
                    "git",
                    "-C",
                    str(REPO),
                    "-c",
                    "user.name=MergeRail integration",
                    "-c",
                    "user.email=mergerail@sandbox.invalid",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    "MergeRail isolated integration candidate",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if commit.returncode != 0:
                raise WorkerError(
                    "cannot commit integration candidate: "
                    f"{(commit.stderr or commit.stdout).strip()[-1200:]}"
                )
            head = _git(REPO, "rev-parse", "HEAD")
    for ancestor in (base, result):
        check = subprocess.run(
            ["git", "-C", str(REPO), "merge-base", "--is-ancestor", ancestor, head],
            capture_output=True,
            check=False,
        )
        if check.returncode != 0:
            raise WorkerError("sandbox candidate does not contain both requested commits")
    _validate_tree(head, request.get("protected_paths", []))
    _validate_operator_config(base, head)
    _write_result_bundle(head)
    return {"head": head}


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        _emit({"type": "error", "error": "worker request exceeds the frame limit"})
        return 2
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise WorkerError("worker request must be a JSON object")
        mode = request.get("mode")
        _clean_environment()
        if mode == "prepare":
            base, result = _reset_from_bundle(request)
            _validate_tree(result, request.get("protected_paths", []))
            _restore_home(request)
            _prepare_owner(str(request.get("role", "fixer")), bool(request.get("read_only")))
            _emit({"type": "result", "base_sha": base, "head": result})
            return 0
        if mode == "turn":
            _emit({"type": "result", "reply": _turn(request)})
            return 0
        if mode == "probe":
            _emit({"type": "result", "probe": _probe(request)})
            return 0
        if mode == "checks":
            _emit({"type": "result", **_run_checks(request)})
            return 0
        if mode == "preflight":
            _emit({"type": "result", **_preflight_probe(request)})
            return 0
        if mode == "commit":
            base = _check_oid(request.get("base_sha"), "base")
            head = _commit_workspace(base, request.get("protected_paths", []))
            _write_result_bundle(head)
            _emit({"type": "result", "head": head})
            return 0
        if mode == "recover":
            _emit({"type": "result", **_recover(request)})
            return 0
        if mode == "merge_candidate":
            _emit({"type": "result", **_merge_candidate(request)})
            return 0
        raise WorkerError(f"unsupported worker mode: {mode!r}")
    except BaseException as error:
        try:
            _emit({"type": "error", "error": f"{type(error).__name__}: {error}"})
        except Exception:
            print(f"worker: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 1


__all__ = ["WorkerError", "export_main", "main"]
