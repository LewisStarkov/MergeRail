"""Export only short-lived gateway credentials from the trusted host's Codex login."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
import time
from pathlib import Path

from .execution.codex_auth import CodexAuthError, gateway_credentials


def export_credentials(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CodexAuthError("Credential export directory must be private and owned by this user")
    if os.environ.get("MERGERAIL_CODEX_GATEWAY_CREDENTIALS_FILE"):
        raise CodexAuthError("The host credential broker must read the original Codex login")
    record: dict[str, str | float] = dict(gateway_credentials("chatgpt"))
    record["expires_at"] = time.time() + 120
    descriptor, name = tempfile.mkstemp(prefix=".credentials-", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "credentials.json")
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            export_credentials(args.directory)
        except CodexAuthError:
            # Do not log SDK errors or credentials, and fail closed on stale exports.
            (args.directory / "credentials.json").unlink(missing_ok=True)
            if not args.loop:
                raise SystemExit(
                    "Codex login unavailable; run codex login on the trusted host"
                ) from None
        if not args.loop:
            return
        time.sleep(15)


if __name__ == "__main__":
    main()
