"""The deterministic half of the review, run by the runner rather than a model.

Linters and test suites produce the same answer every time and cost nothing to
run. A model watching that output scroll past is a model paying tokens to read
what a subprocess produces for free — and a failure caught here goes straight
back to the author without spending a review round at all.

Only failures are quoted at length. "1363 passed" is not something either agent
needs to read in full.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import log
from .detect import Check

#: Long enough for a real test suite, short enough that a hung watcher does not
#: hold the queue overnight.
TIMEOUT_SECONDS = 1800


def run(checks: list[Check], cwd: Path) -> tuple[bool, str]:
    """Run every check in ``cwd``; report pass/fail and what to show the agents."""
    if not checks:
        return True, "(no checks configured for this project)"

    lines: list[str] = []
    passed = True
    for check in checks:
        try:
            result = subprocess.run(
                check.command, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS
            )
            ok = result.returncode == 0
            output = result.stdout or result.stderr or ""
        except subprocess.TimeoutExpired:
            ok, output = False, f"timed out after {TIMEOUT_SECONDS}s"
        except FileNotFoundError:
            # Configured but not installed here. Loud, and a failure: a check
            # that silently does not run is worse than one that does not pass.
            ok, output = False, f"{check.command[0]}: not found on this machine"
        passed = passed and ok
        tail = output.splitlines()[-1] if output.splitlines() else ""
        lines.append(f"{check.name}: {'PASS' if ok else 'FAIL'} — {log.clip(tail, 200)}")
        if not ok:
            lines.append(log.clip(output, 1500))
    log.info("checks.done", passed=passed, count=len(checks))
    return passed, "\n".join(lines)


__all__ = ["TIMEOUT_SECONDS", "run"]
