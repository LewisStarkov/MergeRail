from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from mergerail.devbot_build import ENGINE, prepare_images
from tests.conftest import run


@pytest.mark.parametrize("import_fails", [False, True])
def test_build_exports_approved_tree_and_never_working_secrets(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, import_fails: bool
) -> None:
    for name in (
        "Dockerfile",
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        "alembic.ini",
        "Caddyfile",
        "compose.prod.yml",
        "scripts/backup_db.sh",
        "bot/value.py",
        "core/value.py",
        "webapp/value.py",
        "ops/value.py",
        "migrations/value.py",
        "miniapp/value.ts",
        "admin/value.ts",
    ):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("approved\n")
    run("add", ".", cwd=repo)
    run("commit", "-qm", "checked source", cwd=repo)
    sha = run("rev-parse", "HEAD", cwd=repo)
    (repo / ".env").write_text("secret sentinel")
    (repo / "bot/value.py").write_text("unreviewed change")
    state = tmp_path / "build-state"
    state.mkdir()
    commands: list[list[str]] = []
    remotes: list[str] = []
    actual_run = subprocess.run
    actual_output = subprocess.check_output

    def fake_process(command: list[str], **kwargs: Any) -> Any:
        if command[:3] == ENGINE:
            return subprocess.CompletedProcess(command, 0)
        return actual_run(command, **kwargs)

    def fake_output(command: list[str], **kwargs: Any) -> Any:
        if command[:3] == ENGINE:
            return "sha256:" + "a" * 64 + "\n"
        return actual_output(command, **kwargs)

    def wrapper(command: list[str], _: Path, timeout: float | None) -> int:
        commands.append(command)
        assert timeout is not None and timeout <= 1800
        if "--target" in command:
            context = state / f"build-{sha}"
            assert (context / "bot/value.py").read_text() == "approved\n"
            assert not (context / ".env").exists()
        if "save" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"immutable image")
        return 0

    def remote(command: str) -> str:
        remotes.append(command)
        if "echo cached" in command:
            return "missing" if "rivals-dev-app:" in command else "cached"
        if "docker load" in command and import_fails:
            raise RuntimeError("image verification failed")
        return ""

    monkeypatch.setattr("mergerail.devbot_build.subprocess.run", fake_process)
    monkeypatch.setattr("mergerail.devbot_build.subprocess.check_output", fake_output)
    if import_fails:
        with pytest.raises(RuntimeError, match="image verification failed"):
            prepare_images(repo, state, sha, remote, wrapper)
    else:
        prepare_images(repo, state, sha, remote, wrapper)
    context = state / f"build-{sha}"
    assert not context.exists()
    build = next(command for command in commands if "--target" in command)
    assert build[build.index("--platform") + 1] == "linux/amd64"
    assert "--secret" not in build and "--ssh" not in build
    assert any("--pids-limit=256" in command for command in commands)
    assert any("sha256sum -c" in command and "docker load" in command for command in remotes)
    assert not (state / "image.tar").exists()
    assert any("image" in command and "rm" in command for command in commands)
    assert any("-delete" in command for command in remotes)
