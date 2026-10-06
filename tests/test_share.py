from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mergerail.fronts.web import WebFront
from mergerail.share import NgrokTunnel
from mergerail.tasks import TaskStore


class Process:
    def __init__(self, code: int | None = None) -> None:
        self.code = code
        self.waited = False

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        self.code = 0
        return 0


def test_ngrok_starts_with_generated_web_login_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = Process()
    command: list[str] = []

    def fake_spawn(argv: list[str], **_: Any) -> Any:
        command.extend(argv)
        tunnel._log_path.write_text(
            json.dumps(
                {
                    "msg": "started tunnel",
                    "name": tunnel._name,
                    "url": "https://demo.ngrok.app",
                }
            ),
            encoding="utf-8",
        )
        return process

    tunnel = NgrokTunnel(tmp_path, tmp_path / ".mergerail")
    monkeypatch.setattr("mergerail.share.shutil.which", lambda _: "/bin/ngrok")
    monkeypatch.setattr("mergerail.share.spawn", fake_spawn)
    stopped: list[Any] = []
    monkeypatch.setattr("mergerail.share.terminate_tree", stopped.append)

    front = WebFront(TaskStore(tmp_path / "tasks.json"), port=8788)
    assert tunnel.start(front) == "https://demo.ngrok.app"
    assert command[:3] == ["/bin/ngrok", "http", "http://127.0.0.1:8788"]
    assert "--basic-auth" not in command
    assert tunnel.username == "mergerail"
    assert len(tunnel.password) >= 8
    token = front.authenticate(tunnel.username, tunnel.password)
    assert token is not None
    assert front.authenticated(f"mergerail_session={token}")

    tunnel.stop()
    assert stopped == [process]
    assert process.waited


def test_ngrok_policy_replaces_generated_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "policy.yml"
    policy.write_text("on_http_request: []\n", encoding="utf-8")
    process = Process()
    command: list[str] = []
    tunnel = NgrokTunnel(tmp_path, tmp_path / ".mergerail", policy=policy)
    monkeypatch.setattr("mergerail.share.shutil.which", lambda _: "/bin/ngrok")

    def fake_spawn(argv: list[str], **_: Any) -> Any:
        command.extend(argv)
        tunnel._log_path.write_text(
            json.dumps(
                {
                    "msg": "started tunnel",
                    "name": tunnel._name,
                    "url": "https://demo.ngrok.app",
                }
            ),
            encoding="utf-8",
        )
        return process

    monkeypatch.setattr("mergerail.share.spawn", fake_spawn)
    monkeypatch.setattr("mergerail.share.terminate_tree", lambda _: None)

    front = WebFront(TaskStore(tmp_path / "tasks.json"), port=8788)
    tunnel.start(front)
    tunnel.stop()
    assert "--traffic-policy-file" in command
    assert "--basic-auth" not in command
    assert not front.auth_enabled


def test_ngrok_missing_binary_and_nonlocal_web_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mergerail.share.shutil.which", lambda _: None)
    local = WebFront(TaskStore(tmp_path / "tasks.json"))
    with pytest.raises(SystemExit, match="ngrok is not installed"):
        NgrokTunnel(tmp_path, tmp_path / ".mergerail").start(local)

    public = WebFront(TaskStore(tmp_path / "tasks.json"), host="0.0.0.0")
    with pytest.raises(SystemExit, match="bind to localhost"):
        NgrokTunnel(tmp_path, tmp_path / ".mergerail").start(public)


def test_ngrok_preserves_operator_auth_and_uses_private_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "ngrok.yml"
    config.write_text("version: 3\n")
    monkeypatch.setenv("MERGERAIL_NGROK_CONFIG", str(config))
    tunnel = NgrokTunnel(tmp_path, tmp_path / ".mergerail")
    process = Process()
    monkeypatch.setattr("mergerail.share.shutil.which", lambda _: "/bin/ngrok")
    command: list[str] = []

    def fake_spawn(argv: list[str], **_: Any) -> Any:
        command.extend(argv)
        tunnel._log_path.write_text(json.dumps({"name": tunnel._name,
                                               "url": "https://demo.ngrok.app"}))
        return process

    monkeypatch.setattr("mergerail.share.spawn", fake_spawn)
    monkeypatch.setattr("mergerail.share.terminate_tree", lambda _: None)
    front = WebFront(TaskStore(tmp_path / "tasks.json"))
    front.enable_auth("owner", "persistent-private-password")
    tunnel.start(front)
    assert not tunnel.password and not tunnel.username
    assert front.authenticate("owner", "persistent-private-password")
    assert command[command.index("--config") + 1] == str(config)
    assert "--inspect=false" in command
    tunnel.stop()
