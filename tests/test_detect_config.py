from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentq.config import Config, render_config
from agentq.detect import convention_files, detect_checks
from agentq.fronts import make_front
from agentq.fronts.web import WebFront
from agentq.tasks import TaskStore


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


def test_a_tool_mentioned_in_a_comment_is_not_a_dependency(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(
        "# we might adopt ruff and mypy someday\n[project]\nname='x'\n", encoding="utf-8"
    )
    assert detect_checks(repo) == []


def test_a_tests_directory_alone_does_not_summon_pytest(repo: Path) -> None:
    # A check for a tool that is not a dependency fails on every task.
    (repo / "tests").mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert detect_checks(repo) == []


def test_a_pytest_plugin_implies_pytest(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(
        "[project]\nname='x'\n[dependency-groups]\ndev=['pytest-asyncio>=0.23']\n",
        encoding="utf-8",
    )
    assert [check.name for check in detect_checks(repo)] == ["pytest"]


def test_a_tool_table_counts_as_declared(repo: Path) -> None:
    (repo / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    assert [check.name for check in detect_checks(repo)] == ["ruff"]


def test_jest_gets_no_flag_it_would_refuse(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentq.detect.shutil.which", lambda name: f"/usr/bin/{name}")
    (repo / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "^29"}}),
        encoding="utf-8",
    )
    (repo / "package-lock.json").write_text("{}", encoding="utf-8")
    (checks,) = detect_checks(repo)
    assert checks.command == ["npm", "run", "test"]


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


def test_web_front_loads_auth_and_resource_limits(repo: Path) -> None:
    (repo / "agentq.toml").write_text(
        "[web]\n"
        'host = "0.0.0.0"\n'
        'username = "agentq"\n'
        'password = "secret"\n'
        "session_ttl = 60\n"
        "max_sse_clients = 3\n",
        encoding="utf-8",
    )
    config = Config.load(repo)
    front = make_front("web", config, TaskStore(config.queue_path))
    assert isinstance(front, WebFront)
    assert front.auth_enabled
    assert front.session_ttl == 60
    assert front.max_sse_clients == 3


def test_environment_wins_over_the_file(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "agentq.toml").write_text('delivery = "pr"\n', encoding="utf-8")
    monkeypatch.setenv("AGENTQ_DELIVERY", "merge")
    monkeypatch.setenv("AGENTQ_MODEL", "opus")
    config = Config.load(repo)
    assert config.delivery == "merge"
    assert config.model == "opus"


def test_agent_sections_and_external_backend_are_loaded(repo: Path) -> None:
    (repo / "agentq.toml").write_text(
        "\n".join(
            [
                "[agents]",
                'backend_order = ["codex", "gemini", "opencode"]',
                "[agents.fixer]",
                'backend = "codex"',
                'model = "gpt-test"',
                'effort = "high"',
                'permission = "safe"',
                "timeout = 900",
                "context_limit = 120000",
                "[agents.fixer.settings]",
                'profile = "project"',
                "[agents.reviewer]",
                'backend = "gemini"',
                'model = "gemini-test"',
                'permission = "review"',
                "timeout = 300",
                "[backends.gemini]",
                'protocol = "agentq-jsonl-v1"',
                f"command = {json.dumps([sys.executable, '-m', 'gemini_driver'])}",
            ]
        ),
        encoding="utf-8",
    )

    config = Config.load(repo)
    assert config.backend_order == ["codex", "gemini", "opencode"]
    assert config.fixer.backend == "codex"
    assert config.fixer.settings == {"profile": "project"}
    assert config.reviewer.backend == "gemini"
    assert config.external_backends["gemini"] == [sys.executable, "-m", "gemini_driver"]
    assert "gemini" in config.backend_registry().names()

    fixer = config.session_spec("fixer", repo)
    reviewer = config.session_spec("reviewer", repo, read_only=True)
    assert (fixer.model, fixer.effort, fixer.timeout, fixer.context_limit) == (
        "gpt-test",
        "high",
        900,
        120000,
    )
    assert fixer.settings == {"profile": "project"}
    assert reviewer.model == "gemini-test"
    assert reviewer.timeout == 300
    assert reviewer.read_only


@pytest.mark.parametrize("protocol", [None, "agentq-jsonl-v2"])
def test_external_backend_requires_the_jsonl_v1_protocol(
    repo: Path, protocol: str | None
) -> None:
    lines = ["[backends.gemini]"]
    if protocol is not None:
        lines.append(f'protocol = "{protocol}"')
    lines.append(f"command = {json.dumps([sys.executable, '-m', 'gemini_driver'])}")
    (repo / "agentq.toml").write_text("\n".join(lines), encoding="utf-8")

    with pytest.raises(ValueError, match=r"gemini.*agentq-jsonl-v1"):
        Config.load(repo)


def test_external_backend_requires_a_command(repo: Path) -> None:
    (repo / "agentq.toml").write_text(
        '[backends.gemini]\nprotocol = "agentq-jsonl-v1"\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match=r"gemini.*non-empty command"):
        Config.load(repo)


def test_unknown_delivery_mode_is_rejected(repo: Path) -> None:
    (repo / "agentq.toml").write_text('delivery = "surprise"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unknown delivery mode"):
        Config.load(repo)


def test_malformed_config_is_not_silently_ignored(repo: Path) -> None:
    path = repo / "agentq.toml"
    path.write_text("delivery = [", encoding="utf-8")
    with pytest.raises(ValueError, match=r"cannot parse .*agentq\.toml"):
        Config.load(repo)


def test_role_specific_agent_environment_overrides_the_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "agentq.toml").write_text(
        "[agents.fixer]\nbackend = 'claude'\n[agents.reviewer]\nbackend = 'claude'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTQ_FIXER_BACKEND", "codex")
    monkeypatch.setenv("AGENTQ_FIXER_MODEL", "gpt-test")
    monkeypatch.setenv("AGENTQ_FIXER_TIMEOUT", "123")
    monkeypatch.setenv("AGENTQ_REVIEWER_BACKEND", "opencode")
    monkeypatch.setenv("AGENTQ_REVIEWER_PERMISSION", "review")

    config = Config.load(repo)
    assert config.fixer.backend == "codex"
    assert config.fixer.model == "gpt-test"
    assert config.fixer.timeout == 123
    assert config.reviewer.backend == "opencode"
    assert config.reviewer.permission == "review"


def test_state_dir_env_wins_over_the_file_too(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "agentq.toml").write_text('state_dir = "from-file"\n', encoding="utf-8")
    monkeypatch.setenv("AGENTQ_STATE_DIR", "from-env")
    assert Config.load(repo).state_dir == repo / "from-env"


def test_baseline_checks_reads_a_boolean_from_the_environment(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTQ_BASELINE_CHECKS", "false")
    assert Config.load(repo).baseline_checks is False


def test_baseline_mode_and_strict_security_are_configurable(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "agentq.toml").write_text(
        'baseline_mode = "compare"\nstrict_security = true\n', encoding="utf-8"
    )
    config = Config.load(repo)
    assert config.baseline_mode == "compare"
    assert config.strict_security

    monkeypatch.setenv("AGENTQ_BASELINE_MODE", "strict")
    assert Config.load(repo).baseline_mode == "strict"


def test_project_context_is_loaded_validated_and_overridden(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "agentq.toml").write_text(
        "[project]\n"
        'environment = "staging"\n'
        'work_mode = "maintenance"\n'
        'summary = "prepare the release"\n'
        'external_actions = "ask"\n'
        'constraints = ["no schema changes"]\n',
        encoding="utf-8",
    )
    config = Config.load(repo)
    assert config.project.environment == "staging"
    assert config.project.work_mode == "maintenance"
    assert config.project.summary == "prepare the release"
    assert config.project.constraints == ["no schema changes"]

    monkeypatch.setenv("AGENTQ_PROJECT_ENVIRONMENT", "production")
    monkeypatch.setenv("AGENTQ_PROJECT_WORK_MODE", "incident")
    overridden = Config.load(repo)
    assert overridden.project.environment == "production"
    assert overridden.project.work_mode == "incident"


def test_unknown_project_context_value_is_rejected(repo: Path) -> None:
    (repo / "agentq.toml").write_text(
        '[project]\nenvironment = "somewhere"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown project environment"):
        Config.load(repo)


def test_unknown_baseline_mode_is_rejected(repo: Path) -> None:
    (repo / "agentq.toml").write_text('baseline_mode = "guess"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown baseline mode"):
        Config.load(repo)


def test_agent_settings_reach_the_agent_options(repo: Path) -> None:
    config = Config.load(repo)
    assert config.agent_options().setting_sources == "project"
    config.agent_settings = "all"
    assert config.agent_options().setting_sources == ""


def test_rendered_config_is_loadable(repo: Path, tmp_path: Path) -> None:
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n[tool.ruff]\n", encoding="utf-8")
    config = Config.load(repo)
    config.backend_order = ["codex", "opencode"]
    config.fixer.backend = "codex"
    config.fixer.model = "gpt-test"
    config.fixer.effort = "high"
    config.reviewer.backend = "opencode"
    config.reviewer.model = "openai/reviewer"
    config.process = ["uv", "run", "app"]
    config.baseline_checks = False
    config.baseline_mode = "compare"
    config.strict_security = True
    config.project.environment = "production"
    config.project.work_mode = "maintenance"
    config.project.summary = 'release "blue" safely'
    config.project.external_actions = "ask"
    config.project.constraints = ["preserve data", "no downtime"]
    (repo / "agentq.toml").write_text(render_config(config), encoding="utf-8")
    again = Config.load(repo)
    assert again.base_branch == config.base_branch
    assert [check.command for check in again.checks] == [check.command for check in config.checks]
    assert again.backend_order == ["codex", "opencode"]
    assert (again.fixer.backend, again.fixer.model, again.fixer.effort) == (
        "codex",
        "gpt-test",
        "high",
    )
    assert (again.reviewer.backend, again.reviewer.model) == (
        "opencode",
        "openai/reviewer",
    )
    assert again.process == ["uv", "run", "app"]
    assert again.baseline_checks is False
    assert again.baseline_mode == "compare"
    assert again.strict_security
    assert again.project.environment == "production"
    assert again.project.work_mode == "maintenance"
    assert again.project.summary == 'release "blue" safely'
    assert again.project.external_actions == "ask"
    assert again.project.constraints == ["preserve data", "no downtime"]
