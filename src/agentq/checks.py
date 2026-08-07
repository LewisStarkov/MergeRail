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
import time
from collections.abc import Callable
from pathlib import Path

from . import log, procs
from .detect import Check

#: Long enough for a real test suite, short enough that a hung watcher does not
#: hold the queue overnight.
TIMEOUT_SECONDS = 1800


def run_one(
    check: Check,
    cwd: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """One check, one verdict, and whatever it printed."""
    try:
        controller = procs.ProcessController()
        process = procs.spawn(
            check.command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        controller.reset()
        controller.attach(process)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if cancelled is not None and cancelled():
                        controller.cancel()
                    if time.monotonic() >= deadline:
                        controller.cancel()
                        process.communicate()
                        return False, f"timed out after {TIMEOUT_SECONDS}s"
        finally:
            controller.detach(process)
        if controller.cancelled:
            return False, "cancelled"
        return process.returncode == 0, stdout or stderr or ""
    except FileNotFoundError:
        # Configured but not installed here. Loud, and a failure: a check
        # that silently does not run is worse than one that does not pass.
        return False, f"{check.command[0]}: not found on this machine"


def run(
    checks: list[Check],
    cwd: Path,
    *,
    allowed_failures: frozenset[str] = frozenset(),
    cancelled: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """Run every check in ``cwd``; report pass/fail and what to show the agents."""
    if not checks:
        return True, "(no checks configured for this project)"

    lines: list[str] = []
    passed = True
    for check in checks:
        ok, output = run_one(check, cwd, cancelled=cancelled)
        allowed = not ok and check.name in allowed_failures
        passed = passed and (ok or allowed)
        tail = output.splitlines()[-1] if output.splitlines() else ""
        state = "PASS" if ok else "KNOWN FAIL" if allowed else "FAIL"
        lines.append(f"{check.name}: {state} — {log.clip(tail, 200)}")
        if not ok and not allowed:
            lines.append(log.clip(output, 1500))
        if cancelled is not None and cancelled():
            break
    log.info("checks.done", passed=passed, count=len(checks))
    return passed, "\n".join(lines)


def baseline(checks: list[Check], cwd: Path) -> tuple[list[Check], list[Check]]:
    """Split the checks by whether they pass on a clean base: (healthy, failing).

    A check that already fails before any agent has touched anything would fail
    every task through no fault of the task — and the fixer would burn rounds
    trying to repair a suite it did not break.
    """
    healthy: list[Check] = []
    failing: list[Check] = []
    for check in checks:
        ok, output = run_one(check, cwd)
        (healthy if ok else failing).append(check)
        if not ok:
            tail = output.splitlines()[-1] if output.splitlines() else ""
            log.warn("checks.failing_on_base", check=check.name, tail=log.clip(tail, 200))
    return healthy, failing


__all__ = ["TIMEOUT_SECONDS", "baseline", "run", "run_one"]
