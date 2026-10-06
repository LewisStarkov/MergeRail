"""Real Docker checkpoints and runner gates with a fault-injected Codex JSONL CLI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from mergerail.backends.base import BackendInfo
from mergerail.backends.codex import CAPABILITIES
from mergerail.detect import Check
from mergerail.execution import docker
from mergerail.execution.docker import DockerExecution
from mergerail.runner import Runner
from mergerail.tasks import Status
from tests.conftest import run
from tests.test_docker_integration import configuration

IMAGE = os.environ.get("MERGERAIL_TEST_DOCKER_IMAGE", "")
pytestmark = pytest.mark.skipif(not IMAGE, reason="requires a pinned Docker runtime image")

CLI = r"""
import json, pathlib, subprocess, sys
thread = "fault-injected-thread"
home = pathlib.Path("/work/home/.codex/sessions")
home.mkdir(parents=True, exist_ok=True)
marker = home / "saved-checkpoint"
def event(value):
    print(json.dumps(value), flush=True)
event({"type": "thread.started", "thread_id": thread})
p = pathlib.Path("value.txt")
if "resume" not in sys.argv:
    p.write_text("fixed\n")
    subprocess.run(["git", "add", "value.txt"], check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c",
        "user.email=fixture@example.test", "commit", "-qm", "fix before disconnect"], check=True)
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], text=True).strip()
    marker.write_text(tree)
else:
    assert sys.argv[sys.argv.index("resume") + 1] == thread
    assert p.read_text() == "fixed\n"
    head = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], text=True).strip()
    assert head == marker.read_text()
    if not REPEAT_FAILURE:
        event({"type": "item.completed", "item": {"type": "agent_message",
            "text": "Verified existing commit; done"}})
        event({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
        raise SystemExit(0)
if SILENT and "resume" not in sys.argv:
    raise SystemExit(0)
event({"type": "error", "message": "Reconnecting... 1/5"})
event({"type": "turn.failed", "error": {"message":
    "stream disconnected before completion: stream closed before response.completed"}})
raise SystemExit(1)
"""


@pytest.mark.parametrize("outcome", ["approve", "checks-fail", "reject", "disconnected", "silent"])
def test_commit_stream_break_resume_checks_and_review(
    repo: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    config = configuration(
        repo,
        checks=[
            Check(
                "saved commit",
                [
                    "python3",
                    "-c",
                    "from pathlib import Path; assert Path('value.txt').read_text() == 'fixed\\n'"
                    + ("; raise SystemExit(7)" if outcome == "checks-fail" else ""),
                ],
            )
        ],
    )
    config.fixer.backend = "codex"
    config.fixer.timeout = 120
    config.max_rounds = 1
    if outcome == "reject":
        config.external_backends["fixture"][-1] = config.external_backends["fixture"][-1].replace(
            "VERDICT: APPROVE", "VERDICT: REJECT"
        )
    base = run("rev-parse", "HEAD", cwd=repo)
    original_probe = DockerExecution.probe

    def probe(self: DockerExecution, name: str) -> BackendInfo | dict[str, BackendInfo]:
        if name == "codex":
            return BackendInfo("codex", True, capabilities=CAPABILITIES)
        return original_probe(self, name)

    monkeypatch.setattr(DockerExecution, "probe", probe)
    monkeypatch.setattr(
        DockerExecution, "_start_gateway", lambda *args, **kwargs: ("", "172.30.0.2")
    )
    cli = CLI.replace("REPEAT_FAILURE", repr(outcome == "disconnected")).replace(
        "SILENT", repr(outcome == "silent")
    )
    hook = f"""
from mergerail.backends.codex import CodexBackend
from mergerail.backends.base import SessionSpec, TurnRequest
original_turn = _turn
def _turn(request):
    if request.get("backend") != "codex":
        return original_turn(request)
    session = CodexBackend(("python3", "-u", "-c", {cli!r})).open_session(
        SessionSpec("fixer", Path("/work/repo"), timeout=request["timeout"],
            resume_session_id=request.get("resume_session_id")))
    return _reply_dict(session.ask(TurnRequest(request["prompt"])))
"""
    bootstrap = docker._DOCKER_BOOTSTRAP.replace(
        "from mergerail.execution.worker import main;raise SystemExit(main())",
        f"import mergerail.execution.worker as w;exec({hook!r},w.__dict__);"
        "raise SystemExit(w.main())",
    )
    monkeypatch.setattr(docker, "_DOCKER_BOOTSTRAP", bootstrap)
    runner = Runner(config, "folder", supervise=False)
    task = runner.store.add("fix value after stream break", source="test")
    runner.run(until=task.id)
    result = runner.store.get(task.id)
    assert result is not None
    assert result.status == (Status.DONE if outcome in {"approve", "silent"} else Status.FAILED), (
        result.note
    )
    checkpoint = result.execution["agent_result_sha"]
    assert run("show", f"{checkpoint}:value.txt", cwd=repo) == "fixed"
    assert checkpoint != base
    records = runner.audit.read(task=task.id)
    turns: list[dict[str, Any]] = [row for row in records if row["event"] == "agent.turn"]
    fixer = next(row for row in turns if row["role"] == "fixer")
    assert [row["error_type"] for row in fixer["diagnostics"]] == [
        "incomplete" if outcome == "silent" else "transport",
        "transport" if outcome == "disconnected" else "none",
    ]
    assert fixer["diagnostics"][0]["exit_code"] == (0 if outcome == "silent" else 1)
    checks = [i for i, row in enumerate(records) if row["event"] == "checks.completed"]
    reviews = [
        i
        for i, row in enumerate(records)
        if row["event"] == "agent.turn" and row["role"] == "reviewer"
    ]
    assert bool(checks) is (outcome != "disconnected")
    assert bool(reviews) is (outcome in {"approve", "silent", "reject"})
    if reviews:
        assert checks[-1] < reviews[0]
    assert bool(result.approved_sha) is (outcome in {"approve", "silent"})
    if outcome not in {"approve", "silent"}:
        assert run("rev-parse", "HEAD", cwd=repo) == base
        assert (repo / "value.txt").read_text() == "old\n"
    assert not any(row["event"].startswith("devbot.") for row in records)
