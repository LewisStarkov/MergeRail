from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentq.config import Config, render_config
from agentq.detect import convention_files, detect_checks


def test_python_project_is_detected(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(
        "[project]\nname='x'\n[dependency-groups]\ndev=['ruff','mypy','pytest']\n", encoding="utf-8"
    )
    names = [check.name for check in detect_checks(repo)]
    assert names == ["ruff", "mypy", "pytest"]


def test_node_scripts_become_checks(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentq.detect.shutil.which", lambda name: f"/usr/bin/{name}")
    (repo / "package.json").write_text(
        json.dumps({"scripts": {"lint": "eslint .", "test": "vitest", "dev": "next dev"}}),
        encoding="utf-8",
    )
    (repo / "package-lock.json").write_text("{}", encoding="utf-8")
    checks = detect_checks(repo)
    assert [check.name for check in checks] == ["lint", "test"]
    # A watcher would hold the queue open forever, so the run flag goes in.
    assert checks[1].command == ["npm", "run", "test", "--", "--run"]


def test_a_project_with_nothing_gets_no_checks(repo: Path) -> None:
    assert detect_checks(repo) == []


def test_conventions_are_the_files_that_exist(repo: Path) -> None:
    (repo / "CLAUDE.md").write_text("rules", encoding="utf-8")
    assert convention_files(repo) == ["CLAUDE.md", "README.md"]


def test_config_defaults_to_the_repository(repo: Path) -> None:
    config = Config.load(repo)
    assert config.base_branch == "main"
    assert config.state_dir == repo / ".agentq"
    assert config.queue_path == repo / ".agentq" / "tasks.json"
    assert config.delivery == "auto"


def test_config_file_overrides_detection(repo: Path) -> None:
    (repo / "agentq.toml").write_text(
        '\n'.join(
            [
                'base_branch = "trunk"',
                'delivery = "pr"',
                "max_rounds = 5",
                'process = "python -m app"',
                "[[checks]]",
                'name = "custom"',
                'command = "make verify"',
                "[telegram]",
                'token = "from-file"',
            ]
        ),
        encoding="utf-8",
    )
    config = Config.load(repo)
    assert config.base_branch == "trunk"
    assert config.delivery == "pr"
    assert config.max_rounds == 5
    assert config.process == ["python", "-m", "app"]
    assert [check.command for check in config.checks] == [["make", "verify"]]
    assert config.front("telegram")["token"] == "from-file"


def test_environment_wins_over_the_file(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "agentq.toml").write_text('delivery = "pr"\n', encoding="utf-8")
    monkeypatch.setenv("AGENTQ_DELIVERY", "merge")
    monkeypatch.setenv("AGENTQ_MODEL", "opus")
    config = Config.load(repo)
    assert config.delivery == "merge"
    assert config.model == "opus"


def test_rendered_config_is_loadable(repo: Path, tmp_path: Path) -> None:
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n[tool.ruff]\n", encoding="utf-8")
    config = Config.load(repo)
    (repo / "agentq.toml").write_text(render_config(config), encoding="utf-8")
    again = Config.load(repo)
    assert again.base_branch == config.base_branch
    assert [check.command for check in again.checks] == [check.command for check in config.checks]
