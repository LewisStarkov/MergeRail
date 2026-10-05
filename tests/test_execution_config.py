"""Unsupported isolation settings must fail before host detection and probes."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest

from mergerail.config import Config, render_config
from mergerail.detect import Check
from mergerail.execution.policy import ExecutionPolicy, load_policy

EXECUTION_ENV = ("MERGERAIL_EXECUTION_BACKEND", "MERGERAIL_EXECUTION_REQUIRED")

#: Everything that would learn something about the host, or start something on it.
HOST_CONTACT = (
    "detect_base_branch",
    "detect_checks",
    "convention_files",
    "default_registry",
    "ExternalBackend",
)

EXECUTION_TABLES = [
    pytest.param("[execution]\n", id="empty-table"),
    pytest.param('[execution]\nbackend = "docker"\n', id="backend-docker"),
    pytest.param('[execution]\nbackend = "local"\n', id="backend-local"),
    pytest.param("[execution]\nrequired = false\n", id="required-false"),
    pytest.param(
        '[execution]\nbackend = "docker"\nrequired = true\nimage = "mergerail:0.1"\n',
        id="complete-request",
    ),
    pytest.param(
        '[execution]\nbackend = "docker"\n[execution.sandbox]\nmemory = "1g"\n',
        id="sub-table",
    ),
    pytest.param('[execution.image]\nname = "base"\n', id="nested-only"),
    pytest.param("[[execution]]\nbackend = 'docker'\n", id="array-of-tables"),
    pytest.param('execution = "docker"\n', id="bare-string"),
    pytest.param("execution = 7\n", id="malformed-number"),
    pytest.param("execution = true\n", id="malformed-bool"),
    pytest.param('execution = { backend = "docker" }\n', id="inline-table"),
    pytest.param(
        '[execution]\nbackend = "docker"\n[web]\nhost = "0.0.0.0"\n',
        id="beside-a-real-front",
    ),
    pytest.param(
        'delivery = "pr"\n[execution]\nbackend = "docker"\n[agents]\nbackend_order = ["codex"]\n',
        id="beside-a-real-agents-table",
    ),
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A plain directory: nothing here is a git repository, and nothing runs."""
    return tmp_path


@pytest.fixture(autouse=True)
def clean_execution_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's shell is not part of the test."""
    for name in EXECUTION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MERGERAIL_EXECUTION_IMAGE", raising=False)


@pytest.fixture
def sealed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Tripwires on everything that touches the host, plus a record of the hits."""
    seen: list[str] = []

    def tripwire(name: str) -> Callable[..., NoReturn]:
        def fail(*args: object, **kwargs: object) -> NoReturn:
            seen.append(name)
            raise AssertionError(f"{name}() was reached before the load refused")

        return fail

    for name in HOST_CONTACT:
        monkeypatch.setattr(f"mergerail.config.{name}", tripwire(name))
    return seen


@pytest.fixture
def detected(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Detection, recorded rather than performed."""
    seen: list[str] = []

    def probe(name: str, found: Any) -> Callable[[Path], Any]:
        def run(_root: Path) -> Any:
            seen.append(name)
            return found

        return run

    monkeypatch.setattr("mergerail.config.detect_base_branch", probe("detect_base_branch", "trunk"))
    monkeypatch.setattr(
        "mergerail.config.detect_checks", probe("detect_checks", [Check("test", ["true"])])
    )
    monkeypatch.setattr(
        "mergerail.config.convention_files", probe("convention_files", ["README.md"])
    )
    return seen


def refuse(root: Path, sealed: list[str], body: str) -> ValueError:
    """Write a config, insist the load fails, and prove nothing ran first."""
    (root / "mergerail.toml").write_text(body, encoding="utf-8")
    with pytest.raises(ValueError) as raised:
        Config.load(root)
    assert sealed == []
    return raised.value


@pytest.mark.parametrize("body", EXECUTION_TABLES)
def test_any_execution_section_refuses_the_load(root: Path, sealed: list[str], body: str) -> None:
    message = str(refuse(root, sealed, body))
    assert "unsupported execution configuration" in message
    assert "[execution] in mergerail.toml" in message
    # It has to say why, and it has to say the host is not the answer.
    assert "Docker" in message
    assert "not fall back to running on the host" in message


@pytest.mark.parametrize("name", EXECUTION_ENV)
@pytest.mark.parametrize("value", ["docker", "local", "false", "0", "yes"])
def test_execution_environment_refuses_the_load(
    root: Path, sealed: list[str], monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    message = str(refuse(root, sealed, 'delivery = "pr"\n'))
    assert "unsupported execution configuration" in message
    assert name in message
    assert "not fall back to running on the host" in message


def test_execution_environment_refuses_with_no_config_file_at_all(
    root: Path, sealed: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERGERAIL_EXECUTION_BACKEND", "docker")
    message = str(refuse(root, sealed, ""))
    assert "MERGERAIL_EXECUTION_BACKEND" in message


def test_an_external_backend_is_never_built_for_a_refused_config(
    root: Path, sealed: list[str]
) -> None:
    command = json.dumps([sys.executable, "-m", "gemini_driver"])
    error = refuse(
        root,
        sealed,
        '[execution]\nbackend = "docker"\n[backends.gemini]\n'
        f'protocol = "mergerail-jsonl-v1"\ncommand = {command}\n',
    )
    assert "unsupported execution configuration" in str(error)


def test_a_parse_error_is_still_reported_as_a_parse_error(root: Path, sealed: list[str]) -> None:
    # Unparseable input is upstream of the refusal, and stays its own error.
    (root / "mergerail.toml").write_text("execution = [", encoding="utf-8")
    with pytest.raises(ValueError, match=r"cannot parse .*mergerail\.toml"):
        Config.load(root)
    assert sealed == []


def test_a_config_without_execution_still_loads(root: Path, detected: list[str]) -> None:
    (root / "mergerail.toml").write_text(
        'delivery = "pr"\nmax_rounds = 5\n[web]\nhost = "127.0.0.1"\n', encoding="utf-8"
    )
    config = Config.load(root)
    assert detected == ["detect_base_branch", "detect_checks", "convention_files"]
    assert config.base_branch == "trunk"
    assert [check.name for check in config.checks] == ["test"]
    assert config.conventions == ["README.md"]
    assert config.delivery == "pr"
    assert config.max_rounds == 5
    assert config.front("web") == {"host": "127.0.0.1"}
    assert "execution" not in config.fronts


def test_no_config_file_at_all_still_loads(root: Path, detected: list[str]) -> None:
    config = Config.load(root)
    assert detected == ["detect_base_branch", "detect_checks", "convention_files"]
    assert config.base_branch == "trunk"
    assert config.state_dir == root / ".mergerail"
    assert config.fronts == {}


def test_every_other_front_section_is_still_a_front(
    root: Path, detected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Blank and whitespace-only are not a request, and never were.
    monkeypatch.setenv("MERGERAIL_EXECUTION_BACKEND", "")
    monkeypatch.setenv("MERGERAIL_EXECUTION_REQUIRED", "   ")
    (root / "mergerail.toml").write_text(
        '[telegram]\ntoken = "t"\n[web]\nport = 8788\n[project]\nsummary = "s"\n',
        encoding="utf-8",
    )
    config = Config.load(root)
    assert set(config.fronts) == {"telegram", "web"}
    assert config.front("telegram")["token"] == "t"
    assert config.front("web")["port"] == 8788
    assert config.project.summary == "s"


def test_init_never_writes_an_execution_section(root: Path, detected: list[str]) -> None:
    # `mergerail init` round-trips through render_config, so the tool must not
    # be able to write itself into a state it then refuses to load.
    assert "execution" not in render_config(Config.load(root)).lower()


PIN = "example/runtime@sha256:" + "a" * 64


def test_valid_docker_config_roundtrips_without_probing_host_tools(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "mergerail.toml").write_text(f'[execution]\nimage = "{PIN}"\n')
    seen: list[bool] = []
    monkeypatch.setattr("mergerail.config.detect_base_branch", lambda path: "main")
    monkeypatch.setattr("mergerail.config.convention_files", lambda path: [])

    def detect(path: Path, *, container: bool = False) -> list[Check]:
        seen.append(container)
        return [Check("test", ["python3", "-m", "pytest"])]

    monkeypatch.setattr("mergerail.config.detect_checks", detect)
    monkeypatch.setattr("mergerail.config.default_registry", lambda: pytest.fail("host probe"))
    config = Config.load(root)
    assert seen == [True]
    assert config.execution == ExecutionPolicy(image=PIN)
    assert "execution" not in config.fronts
    (root / "mergerail.toml").write_text(render_config(config), encoding="utf-8")
    loaded = Config.load(root)
    assert loaded.execution == config.execution
    assert loaded.execution is not None and loaded.execution.digest == config.execution.digest


@pytest.mark.parametrize(
    "key,value",
    [
        ("cpus", float("nan")),
        ("cpus", True),
        ("memory_mib", 0),
        ("workspace_limit_mib", 2048),
        ("pids_limit", -1),
        ("required", False),
        ("image", {}),
        ("opencode_model", {}),
        ("idle_stop_seconds", 61),
        ("mounts", ["/Users:/host"]),
        ("network", "host"),
    ],
)
def test_docker_policy_rejects_unsafe_or_malformed_settings(key: str, value: object) -> None:
    with pytest.raises(ValueError):
        load_policy({"execution": {"image": PIN, key: value}})


def test_image_environment_alone_requests_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERGERAIL_EXECUTION_IMAGE", PIN)
    assert load_policy({}) == ExecutionPolicy(image=PIN)
