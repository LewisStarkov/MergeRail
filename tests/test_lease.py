from __future__ import annotations

import json
from pathlib import Path

import pytest

from mergerail.lease import RunnerBusy, RunnerLease


def test_runner_lease_is_exclusive_and_reusable(tmp_path: Path) -> None:
    path = tmp_path / "runner.lock"
    first = RunnerLease(path)
    second = RunnerLease(path)

    run_id = first.acquire()
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == run_id
    with pytest.raises(RunnerBusy, match="pid"):
        second.acquire()

    first.release()
    assert second.acquire() != run_id
    second.release()
