from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mergerail.codex_credentials import export_credentials
from mergerail.execution.codex_auth import CodexAuthError, gateway_credentials


def test_export_contains_only_gateway_access_and_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "login"
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "access-secret",
                    "account_id": "account",
                    "refresh_token": "refresh-secret",
                    "id_token": "id-secret",
                }
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("MERGERAIL_CODEX_GATEWAY_CREDENTIALS_FILE", raising=False)
    directory = tmp_path / "gateway"
    export_credentials(directory)
    path = directory / "credentials.json"
    record = json.loads(path.read_text())
    assert set(record) == {"authorization", "chatgpt-account-id", "expires_at"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert "refresh-secret" not in path.read_text()
    assert "id-secret" not in path.read_text()
    monkeypatch.setenv("MERGERAIL_CODEX_GATEWAY_CREDENTIALS_FILE", str(path))
    assert gateway_credentials("chatgpt") == {
        "authorization": "Bearer access-secret",
        "chatgpt-account-id": "account",
    }
    with pytest.raises(CodexAuthError, match="ChatGPT"):
        gateway_credentials("api")
    record["expires_at"] = time.time() - 1
    path.write_text(json.dumps(record))
    with pytest.raises(CodexAuthError, match="expired"):
        gateway_credentials("chatgpt")


@pytest.mark.parametrize("failure", ["symlink", "mode", "owner", "oversized", "extra", "future"])
def test_bad_exports_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "credentials.json"
    record: dict[str, object] = {
        "authorization": "Bearer secret",
        "chatgpt-account-id": "account",
        "expires_at": time.time() + 120,
    }
    if failure == "extra":
        record["refresh_token"] = "never-accepted"
    if failure == "future":
        record["expires_at"] = time.time() + 3600
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    if failure == "symlink":
        other = tmp_path / "other"
        path.rename(other)
        path.symlink_to(other)
    elif failure == "mode":
        path.chmod(0o644)
    elif failure == "owner":
        monkeypatch.setenv("MERGERAIL_CODEX_GATEWAY_CREDENTIALS_UID", str(os.getuid() + 1))
    elif failure == "oversized":
        path.write_bytes(b"x" * 65537)
    monkeypatch.setenv("MERGERAIL_CODEX_GATEWAY_CREDENTIALS_FILE", str(path))
    with pytest.raises(CodexAuthError):
        gateway_credentials("chatgpt")


@pytest.mark.skipif(sys.platform == "win32", reason="The service supervisor uses Bash")
@pytest.mark.parametrize("broker_fails", [False, True])
def test_broker_restarts_without_interrupting_deployment(broker_fails: bool) -> None:
    source = (Path(__file__).resolve().parents[1] / "ops/devbot/run.sh").read_text()
    supervisor = "cleanup() {" + source.split("cleanup() {", 1)[1]
    broker = "/usr/bin/true" if broker_fails else "/bin/sleep 60"
    script = (
        "set -Eeuo pipefail\n"
        "sleep() { command sleep 0.02; }\n"
        f"{broker} & credential_pid=$!\n"
        "/bin/sleep 0.2 & deployment_pid=$!\n"
        'start_credentials() { /bin/sleep 60 & credential_pid=$!; '
        'echo "restart $credential_pid"; }\n'
        'echo "$credential_pid $deployment_pid"\n' + supervisor
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert ("restart" in result.stdout) == broker_fails
    for pid in result.stdout.replace("restart", "").split():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)
