"""Credential selection and the Codex worker's Docker-only configuration."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from mergerail.backends.base import AgentReply, SessionSpec
from mergerail.backends.codex import CodexBackend
from mergerail.execution import codex_auth, worker
from mergerail.execution.docker import DockerExecution
from mergerail.execution.policy import ExecutionPolicy

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Docker supports Linux/macOS")


@pytest.fixture
def auth_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MERGERAIL_CODEX_ALLOW_API_BILLING", raising=False)
    return tmp_path


def test_subscription_auth_never_forwards_refresh_or_id_tokens(auth_home: Path) -> None:
    (auth_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": "unused-api-secret",
                "tokens": {
                    "access_token": "access-secret",
                    "account_id": "account",
                    "refresh_token": "refresh-secret",
                    "id_token": "id-secret",
                },
            }
        )
    )
    assert codex_auth.gateway_credentials("chatgpt") == {
        "authorization": "Bearer access-secret",
        "chatgpt-account-id": "account",
    }


def test_api_key_requires_explicit_billing_mode(
    auth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "api-secret")
    with pytest.raises(codex_auth.CodexAuthError, match="codex login"):
        codex_auth.gateway_credentials("chatgpt")
    with pytest.raises(codex_auth.CodexAuthError, match="ALLOW_API_BILLING"):
        codex_auth.gateway_credentials("api")
    monkeypatch.setenv("MERGERAIL_CODEX_ALLOW_API_BILLING", "1")
    assert codex_auth.gateway_credentials("api") == {"authorization": "Bearer api-secret"}


@pytest.mark.parametrize("value", ["bad\r\nInjected:value", "", None, 17, "x" * 32769])
def test_invalid_credentials_are_rejected_without_echoing(auth_home: Path, value: Any) -> None:
    (auth_home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {"access_token": value, "account_id": "account"},
            }
        )
    )
    with pytest.raises(codex_auth.CodexAuthError, match="invalid credential") as error:
        codex_auth.gateway_credentials("chatgpt")
    assert "Injected" not in str(error.value)


def test_auth_cache_symlinks_and_oversized_files_are_refused(auth_home: Path) -> None:
    target = auth_home / "other"
    target.write_text("secret")
    path = auth_home / "auth.json"
    path.symlink_to(target)
    with pytest.raises(codex_auth.CodexAuthError, match="unavailable"):
        codex_auth.gateway_credentials("chatgpt")
    path.unlink()
    path.write_bytes(b"x" * 65537)
    with pytest.raises(codex_auth.CodexAuthError, match="size limit"):
        codex_auth.gateway_credentials("chatgpt")


def test_codex_auth_policy_does_not_accept_arbitrary_providers() -> None:
    with pytest.raises(ValueError, match="codex_auth"):
        ExecutionPolicy(image="sha256:" + "a" * 64, codex_auth="http://host").validate()


def test_default_auth_preserves_pre_codex_delivery_policy_digest() -> None:
    policy = ExecutionPolicy(image="sha256:" + "a" * 64)
    legacy = asdict(policy)
    del legacy["codex_auth"]
    assert policy.digest == hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    assert replace(policy, codex_auth="api").digest != policy.digest


@pytest.mark.parametrize("role", ["fixer", "reviewer"])
def test_docker_codex_uses_external_isolation_for_both_roles(tmp_path: Path, role: str) -> None:
    command = (
        CodexBackend(sandboxed_externally=True)
        .open_session(
            SessionSpec(role, tmp_path, read_only=role == "reviewer"),
        )
        .command("inspect")
    )
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert "--sandbox" not in command


def test_worker_requires_gateway_for_codex() -> None:
    with pytest.raises(worker.WorkerError, match="credential gateway"):
        worker._turn({"backend": "codex", "role": "fixer"})


def _token(expiry: float) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return "header." + encoded + ".signature"


def test_expiring_auth_uses_host_account_refresh_and_reloads_cache(
    auth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = auth_home / "auth.json"
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": _token(time.time() - 1),
                    "account_id": "old-account",
                },
            }
        )
    )
    refreshed = _token(time.time() + 3600)
    calls: list[Path] = []

    def refresh(home: Path) -> None:
        calls.append(home)
        path.write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "access_token": refreshed,
                        "account_id": "new-account",
                        "refresh_token": "never-forward",
                    },
                }
            )
        )

    monkeypatch.setattr(codex_auth, "_refresh_with_codex", refresh)
    assert codex_auth.gateway_credentials("chatgpt") == {
        "authorization": "Bearer " + refreshed,
        "chatgpt-account-id": "new-account",
    }
    assert calls == [auth_home]


def test_failed_rotation_does_not_reuse_expired_auth(
    auth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (auth_home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": _token(time.time() - 1),
                    "account_id": "account",
                }
            }
        )
    )
    monkeypatch.setattr(codex_auth, "_refresh_with_codex", lambda home: None)
    with pytest.raises(codex_auth.CodexAuthError, match="expired"):
        codex_auth.gateway_credentials("chatgpt")


@pytest.mark.parametrize(
    ("mode", "prefix"),
    [
        ("chatgpt", "/backend-api/codex"),
        ("api", "/v1"),
    ],
)
@pytest.mark.parametrize(
    ("role", "read_only", "external"),
    [
        ("fixer", False, True),
        ("fixer", True, False),
        ("reviewer", True, True),
    ],
)
def test_worker_configures_only_local_codex_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    prefix: str,
    role: str,
    read_only: bool,
    external: bool,
) -> None:
    import mergerail.backends

    commands: list[tuple[str, ...]] = []

    class Registry:
        def register(self, backend: Any, *, replace: bool) -> None:
            commands.append(backend._command)
            assert backend._sandboxed_externally is external

        def open_session(self, *args: Any) -> Any:
            class Session:
                def ask(self, request: Any) -> AgentReply:
                    return AgentReply("done", False, 0.0, 0, 0.0)

                def close(self) -> None:
                    pass

            return Session()

    monkeypatch.setattr(mergerail.backends, "default_registry", Registry)
    monkeypatch.setattr(worker, "_restore_home", lambda request: None)
    monkeypatch.setattr(worker, "HOME", tmp_path)
    monkeypatch.setattr(os, "environ", {"OPENAI_API_KEY": "host-secret"})
    worker._turn(
        {
            "backend": "codex",
            "role": role,
            "read_only": read_only,
            "gateway": True,
            "gateway_host": "172.20.0.2",
            "codex_auth": mode,
        }
    )
    assert len(commands) == 1
    assert f'model_providers.mergerail.base_url="http://172.20.0.2:8765{prefix}"' in commands[0]
    assert "model_providers.mergerail.requires_openai_auth=false" in commands[0]
    assert 'projects./work/repo.trust_level="untrusted"' in commands[0]
    assert "features.plugins=false" in commands[0]
    assert "features.hooks=false" in commands[0]
    assert os.environ["CODEX_HOME"] == str(tmp_path / ".codex")
    assert "OPENAI_API_KEY" not in os.environ
    assert "host-secret" not in str(commands)


@pytest.mark.parametrize("failure", [None, "rpc-error", "malformed"])
def test_host_refresh_rpc_is_bounded_and_opens_no_coding_thread(
    auth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    executable = auth_home / "codex"
    script = """
import json, os, sys, pathlib
home = pathlib.Path(os.environ["CODEX_HOME"])
assert "OPENAI_API_KEY" not in os.environ
assert "CODEX_ACCESS_TOKEN" not in os.environ
assert "CODEX_REFRESH_TOKEN_URL_OVERRIDE" not in os.environ
assert pathlib.Path.cwd() != home
assert "features.plugins=false" in sys.argv and "features.hooks=false" in sys.argv
requests = []
for line in sys.stdin:
    request = json.loads(line)
    requests.append(request)
    (home / "requests.json").write_text(json.dumps(requests))
    if request["method"] == "initialize":
        print(json.dumps({"id": 1, "result": {}}), flush=True)
    elif request["method"] == "account/read":
        assert request["params"] == {"refreshToken": True}
        failure = __FAILURE__
        if failure == "rpc-error":
            print(json.dumps({"id": 2, "error": {"message": "private-secret"}}), flush=True)
        elif failure == "malformed":
            print("malformed private-secret", flush=True)
        else:
            print(json.dumps({"id": 2, "result": {"account": {"type": "chatgpt"}}}), flush=True)
"""
    executable.write_text(f"#!{sys.executable}\n" + script.replace("__FAILURE__", repr(failure)))
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(auth_home))
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "unexpected-identity")
    monkeypatch.setenv("CODEX_REFRESH_TOKEN_URL_OVERRIDE", "https://attacker.example/token")
    if failure:
        with pytest.raises(codex_auth.CodexAuthError) as error:
            codex_auth._refresh_with_codex(auth_home)
        assert "private-secret" not in str(error.value)
    else:
        codex_auth._refresh_with_codex(auth_home)
    requests = json.loads((auth_home / "requests.json").read_text())
    assert [request["method"] for request in requests] == [
        "initialize",
        "initialized",
        "account/read",
    ]


def test_controller_sends_credentials_only_to_source_free_gateway(
    auth_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (auth_home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "fixture-access-secret",
                    "account_id": "fixture-account",
                    "refresh_token": "fixture-refresh-secret",
                }
            }
        )
    )
    execution = DockerExecution(
        ExecutionPolicy(image="sha256:" + "a" * 64),
        auth_home / "repo",
        auth_home / "state",
    )
    copied: dict[str, bytes] = {}
    commands: list[tuple[str, ...]] = []

    def create(**kwargs: Any) -> str:
        assert kwargs["gateway"] is True
        return "gateway-only"

    def copy(container: str, path: str, payload: bytes, **kwargs: Any) -> None:
        assert container == "gateway-only"
        copied[path] = payload

    monkeypatch.setattr(execution, "_create_container", create)
    monkeypatch.setattr(execution, "_runtime_zip", lambda: b"runtime")
    monkeypatch.setattr(execution, "_copy_into", copy)
    monkeypatch.setattr(execution, "_docker", lambda *args, **kwargs: commands.append(args))
    monkeypatch.setattr(
        execution,
        "_inspect_container",
        lambda name: {
            "NetworkSettings": {"Networks": {"private": {"IPAddress": "172.20.0.2"}}},
        },
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"", b"")
    )
    assert execution._start_gateway("private", backend="codex") == ("gateway-only", "172.20.0.2")
    assert json.loads(copied["/tmp/codex-credentials.json"]) == {
        "authorization": "Bearer fixture-access-secret",
        "chatgpt-account-id": "fixture-account",
    }
    assert "fixture-access-secret" not in str(commands)
    assert "fixture-refresh-secret" not in str(copied)
    assert "fixture-access-secret" not in str(execution.metadata)
    assert "chatgpt.com" in str(commands) and "/backend-api/codex" in str(commands)
