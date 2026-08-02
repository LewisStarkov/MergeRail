"""A real git repository per test, because the interesting parts are git."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def run(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=test", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    run("init", "-q", "-b", "main", cwd=root)
    (root / "README.md").write_text("# project\n", encoding="utf-8")
    run("add", "-A", cwd=root)
    run("commit", "-qm", "init", cwd=root)
    return root
