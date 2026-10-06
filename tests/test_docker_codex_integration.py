"""Opt-in live Codex acceptance using the operator's ChatGPT subscription."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mergerail.backends.base import SessionSpec, TurnRequest
from mergerail.detect import Check
from mergerail.execution.docker import DockerExecution
from mergerail.execution.policy import ExecutionPolicy
from mergerail.prompts import REVIEW_SCHEMA
from tests.conftest import run

IMAGE = os.environ.get("MERGERAIL_TEST_CODEX_IMAGE", "")
MODEL = os.environ.get("MERGERAIL_TEST_CODEX_MODEL", "gpt-6.1-sol")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="set MERGERAIL_TEST_CODEX_IMAGE for live subscription calls"
)


def test_codex_fixer_resume_readonly_review_and_checks(repo: Path) -> None:
    (repo / "value.txt").write_text("old\n")
    run("add", "value.txt", cwd=repo)
    run("commit", "-qm", "Codex fixture input", cwd=repo)
    base = run("rev-parse", "HEAD", cwd=repo)
    execution = DockerExecution(ExecutionPolicy(image=IMAGE), repo, repo / ".mergerail")
    execution.set_task_id("codex-acceptance")
    try:
        execution.preflight()
        assert execution.probe("codex").available
        execution.worktree.reset("mergerail/codex-acceptance", base)
        fixer = execution.registry().open_session(
            "codex",
            SessionSpec("fixer", Path("/work/repo"), model=MODEL, effort="high", timeout=180),
        )
        first = fixer.ask(
            TurnRequest(
                "Remember marker resume-proof-83b6 but do not save it in the repository yet. "
                "Use a shell command to check that OPENAI_API_KEY is absent from the environment "
                "and $CODEX_HOME/auth.json does not exist. Then write exactly fixed followed by "
                "a newline to value.txt. Reply DONE."
            )
        )
        assert not first.is_error, first.text
        assert first.session_id
        assert run("show", f"{execution.worktree.head()}:value.txt", cwd=repo) == "fixed"
        resumed = fixer.ask(
            TurnRequest(
                "Use the marker I asked you to remember in the previous message. "
                "Write it followed by a newline to marker.txt. Reply DONE."
            )
        )
        assert not resumed.is_error, resumed.text
        assert resumed.session_id == first.session_id
        head = execution.worktree.head()
        assert run("show", f"{head}:marker.txt", cwd=repo) == "resume-proof-83b6"
        fixer.close()
        passed, report = execution.run_checks(
            [
                Check(
                    "fixture",
                    [
                        "python3",
                        "-c",
                        "from pathlib import Path; "
                        "assert Path('value.txt').read_text() == 'fixed\\n'; "
                        "assert Path('marker.txt').read_text() == 'resume-proof-83b6\\n'",
                    ],
                )
            ],
            sha=head,
        )
        assert passed, report
        reviewer = execution.registry().open_session(
            "codex",
            SessionSpec(
                "reviewer",
                Path("/work/repo"),
                read_only=True,
                model=MODEL,
                effort="high",
                timeout=180,
            ),
        )
        reviewed = reviewer.ask(
            TurnRequest(
                "Review this tiny fixture. Run a Python command that asserts value.txt contains "
                "fixed and marker.txt contains resume-proof-83b6, each with a newline. "
                "In the same command, attempt to overwrite value.txt and chmod it to 0777. "
                "Both attempts MUST raise PermissionError; catch them and print DENIED. "
                "Approve only if all assertions and both denials succeeded. "
                "Return the requested JSON verdict with an empty objection on approval.",
                schema=REVIEW_SCHEMA,
            )
        )
        reviewer.close()
        assert not reviewed.is_error, reviewed.text
        assert reviewed.structured == {"verdict": "APPROVE", "objection": ""}
        assert execution.worktree.head() == head
        assert run("rev-parse", "HEAD", cwd=repo) == base
        assert (repo / "value.txt").read_text() == "old\n"
    finally:
        execution.close()
