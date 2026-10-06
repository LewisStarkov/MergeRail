"""Read host Codex authentication for the trusted gateway, never for a task."""

from __future__ import annotations

import base64
import json
import os
import selectors
import stat
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class CodexAuthError(ValueError):
    pass


def _header(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 32768:
        raise CodexAuthError("Codex authentication contains an invalid credential")
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise CodexAuthError("Codex authentication contains an invalid credential")
    return value


def _read_auth(home: Path) -> dict[str, Any]:
    path = home / "auth.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise CodexAuthError("Codex auth.json must be a regular file owned by this user")
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise CodexAuthError("Codex auth.json exceeds its size limit")
        auth = json.loads(raw)
    except (OSError, ValueError) as error:
        if isinstance(error, CodexAuthError):
            raise
        raise CodexAuthError(
            "Codex authentication is unavailable; run 'codex login' on the host"
        ) from None
    if not isinstance(auth, dict):
        raise CodexAuthError("Codex auth.json is malformed; run 'codex login' on the host")
    return auth


def _needs_refresh(auth: dict[str, Any]) -> bool:
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        return False
    token = _header(tokens.get("access_token"))
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expiry = claims.get("exp")
        if isinstance(expiry, int | float) and not isinstance(expiry, bool):
            return expiry <= time.time() + 300
    except (ValueError, IndexError, AttributeError):
        pass
    refreshed = auth.get("last_refresh")
    if isinstance(refreshed, str):
        try:
            return datetime.fromisoformat(refreshed) < datetime.now(UTC) - timedelta(days=8)
        except (ValueError, TypeError):
            pass
    return False


def _refresh_with_codex(home: Path) -> None:
    """Use Codex's own account RPC for rotation and shared-cache persistence."""

    environment = {
        name: os.environ[name]
        for name in (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "CODEX_CA_CERTIFICATE",
        )
        if name in os.environ
    }
    environment["CODEX_HOME"] = str(home.resolve())
    process: subprocess.Popen[bytes] | None = None
    try:
        # No thread or turn is opened, and no repository is passed to the host
        # CLI. The only RPC after initialization is the authentication refresh.
        with tempfile.TemporaryDirectory(prefix="mergerail-codex-auth-") as cwd:
            process = subprocess.Popen(
                [
                    "codex",
                    "--config",
                    "features.plugins=false",
                    "--config",
                    "features.hooks=false",
                    "--config",
                    'cli_auth_credentials_store="file"',
                    "app-server",
                ],
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert process.stdin is not None and process.stdout is not None
            deadline = time.monotonic() + 30
            buffered = bytearray()
            total = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                requests = [
                    {
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {"name": "mergerail_auth", "version": "1"},
                        },
                    },
                    {"id": 2, "method": "account/read", "params": {"refreshToken": True}},
                ]
                for request in requests:
                    process.stdin.write(json.dumps(request).encode() + b"\n")
                    process.stdin.flush()
                    while True:
                        if not selector.select(max(0, deadline - time.monotonic())):
                            raise CodexAuthError("Codex authentication refresh timed out")
                        block = os.read(process.stdout.fileno(), 4096)
                        total += len(block)
                        if not block or total > 65536:
                            raise CodexAuthError("Codex authentication refresh failed")
                        buffered.extend(block)
                        matched = False
                        while b"\n" in buffered:
                            line, _, remainder = buffered.partition(b"\n")
                            buffered = bytearray(remainder)
                            frame = json.loads(line)
                            if isinstance(frame, dict) and frame.get("id") == request["id"]:
                                if "error" in frame or "result" not in frame:
                                    raise CodexAuthError("Codex authentication refresh failed")
                                matched = True
                                break
                        if matched:
                            break
                    if request["id"] == 1:
                        process.stdin.write(b'{"method":"initialized"}\n')
                        process.stdin.flush()
                process.stdin.close()
                process.wait(timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, CodexAuthError):
            raise
        raise CodexAuthError(
            "Codex authentication refresh failed; run 'codex login' on the host"
        ) from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()


def gateway_credentials(mode: str) -> dict[str, str]:
    """Select subscription auth by default; API billing requires host opt-in."""

    if mode not in {"chatgpt", "api"}:
        raise CodexAuthError("execution.codex_auth must be 'chatgpt' or 'api'")
    if os.name == "nt":
        raise CodexAuthError("Docker Codex authentication requires Linux or macOS")
    exported = os.environ.get("MERGERAIL_CODEX_GATEWAY_CREDENTIALS_FILE")
    if exported:
        if mode != "chatgpt":
            raise CodexAuthError("Exported gateway credentials support ChatGPT authentication only")
        return _exported_credentials(Path(exported))
    if mode == "api" and os.environ.get("MERGERAIL_CODEX_ALLOW_API_BILLING") != "1":
        raise CodexAuthError(
            "API billing requires MERGERAIL_CODEX_ALLOW_API_BILLING=1 on the controller host"
        )
    if mode == "api" and os.environ.get("OPENAI_API_KEY"):
        return {"authorization": "Bearer " + _header(os.environ["OPENAI_API_KEY"])}
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    auth = _read_auth(home)
    if mode == "api":
        if not auth.get("OPENAI_API_KEY"):
            raise CodexAuthError(
                "API authentication requires OPENAI_API_KEY or 'codex login --with-api-key'"
            )
        return {"authorization": "Bearer " + _header(auth["OPENAI_API_KEY"])}
    tokens = auth.get("tokens")
    if auth.get("auth_mode") not in {None, "chatgpt"} or not isinstance(tokens, dict):
        raise CodexAuthError("ChatGPT authentication requires 'codex login' on the host")
    if _needs_refresh(auth):
        _refresh_with_codex(home)
        auth = _read_auth(home)
        tokens = auth.get("tokens")
        if auth.get("auth_mode") not in {None, "chatgpt"} or not isinstance(tokens, dict):
            raise CodexAuthError("ChatGPT login changed during authentication refresh")
        if _needs_refresh(auth):
            raise CodexAuthError("Codex authentication has expired; run 'codex login' on the host")
    return {
        "authorization": "Bearer " + _header(tokens.get("access_token")),
        "chatgpt-account-id": _header(tokens.get("account_id")),
    }


def _exported_credentials(path: Path) -> dict[str, str]:
    """Read a short-lived, operator-owned export without touching a login cache."""

    try:
        owner = int(os.environ.get("MERGERAIL_CODEX_GATEWAY_CREDENTIALS_UID", str(os.getuid())))
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != owner
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise CodexAuthError("Gateway credential export must be private and operator-owned")
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise CodexAuthError("Gateway credential export exceeds its size limit")
        record = json.loads(raw)
        if not isinstance(record, dict) or set(record) != {
            "authorization",
            "chatgpt-account-id",
            "expires_at",
        }:
            raise CodexAuthError("Gateway credential export is malformed")
        expiry = record["expires_at"]
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, int | float)
            or not time.time() < expiry <= time.time() + 180
        ):
            raise CodexAuthError(
                "Gateway credential export expired; check the host credential broker"
            )
        authorization = record["authorization"]
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise CodexAuthError("Gateway credential export is malformed")
        return {
            "authorization": "Bearer " + _header(authorization[7:]),
            "chatgpt-account-id": _header(record["chatgpt-account-id"]),
        }
    except (OSError, ValueError, TypeError) as error:
        if isinstance(error, CodexAuthError):
            raise
        raise CodexAuthError("Gateway credential export is unavailable") from None
