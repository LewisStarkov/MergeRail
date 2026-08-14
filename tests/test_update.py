from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mergerail import update


def completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_latest_release_uses_the_highest_stable_semver(monkeypatch: pytest.MonkeyPatch) -> None:
    output = (
        "a\trefs/tags/v1.9.0\n"
        "b\trefs/tags/v1.10.0\n"
        "c\trefs/tags/v2.0.0-rc1\n"
        "d\trefs/tags/not-a-version\n"
    )
    monkeypatch.setattr(update, "_run", lambda *args, **kwargs: completed(output))

    assert update.latest_release("https://example.test/mergerail.git") == "v1.10.0"


def test_install_release_uses_uv_and_the_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return completed()

    monkeypatch.setattr("mergerail.update.shutil.which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(update, "_run", run)

    update.install_release("v1.2.3", "https://example.test/mergerail.git")

    assert calls == [
        [
            "/usr/bin/uv",
            "tool",
            "install",
            "--force",
            "git+https://example.test/mergerail.git@v1.2.3",
        ]
    ]


def test_auto_update_checks_only_once_per_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "update.json"
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(state))
    monkeypatch.setattr("mergerail.update.time.time", lambda: 100_000.0)
    monkeypatch.setattr(update, "latest_release", lambda: "v0.1.0")

    assert update.auto_update() is None
    assert json.loads(state.read_text(encoding="utf-8"))["latest"] == "v0.1.0"

    monkeypatch.setattr(
        update,
        "latest_release",
        lambda: pytest.fail("the cached check should have been used"),
    )
    assert update.auto_update() is None


def test_auto_update_installs_a_new_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "update.json"
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(state))
    monkeypatch.setattr(update, "latest_release", lambda: "v0.2.0")
    installed: list[str] = []
    monkeypatch.setattr(update, "install_release", installed.append)

    assert update.auto_update() == "v0.2.0"
    assert installed == ["v0.2.0"]
    assert json.loads(state.read_text(encoding="utf-8"))["installed"] == "v0.2.0"


def test_uvx_bootstrap_relaunches_an_already_installed_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "update.json"
    state.write_text(
        json.dumps(
            {"checked_at": 100_000.0, "latest": "v0.2.0", "installed": "v0.2.0"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(state))
    monkeypatch.setattr("mergerail.update.time.time", lambda: 100_001.0)
    monkeypatch.setattr(
        update,
        "latest_release",
        lambda: pytest.fail("the cached check should have been used"),
    )

    assert update.auto_update() == "v0.2.0"


def test_manual_check_does_not_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(update, "latest_release", lambda: "v0.2.0")
    monkeypatch.setattr(
        update, "install_release", lambda tag: pytest.fail(f"installed {tag}")
    )

    assert update.update(check_only=True) == 0
    assert "v0.2.0 is available" in capsys.readouterr().out
