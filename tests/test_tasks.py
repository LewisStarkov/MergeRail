from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentq.gitctl import attempt_branch
from agentq.tasks import (
    SCHEMA_VERSION,
    DeliveryRecord,
    QueueCorruptError,
    Status,
    Task,
    TaskStore,
)


def store_at(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.json")


def test_add_assigns_increasing_ids(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    first = store.add("one")
    second = store.add("two")
    assert (first.id, second.id) == (1, 2)
    assert [task.id for task in store.load()] == [2, 1]


def test_take_next_claims_the_oldest_once(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json", claimant="run-1")
    store.add("one")
    store.add("two")

    claimed = store.take_next()
    assert claimed is not None and claimed.id == 1
    assert claimed.status == Status.RUNNING
    assert claimed.attempts == 1
    assert claimed.claimed_by == "run-1"
    assert claimed.claimed_at

    # The second call must not hand the same task out again.
    again = store.take_next()
    assert again is not None and again.id == 2
    assert store.take_next() is None


def test_update_ignores_unknown_fields(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    task = store.add("one")
    updated = store.update(task.id, note="done", nonsense="x")
    assert updated is not None
    assert updated.note == "done"
    assert not hasattr(updated, "nonsense")


def test_a_corrupt_file_is_preserved_and_never_overwritten(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(QueueCorruptError, match="preserved at"):
        store.load()
    with pytest.raises(QueueCorruptError):
        store.add("one")
    assert store.path.read_text(encoding="utf-8") == "{ not json"
    assert list(tmp_path.glob("tasks.corrupt-*.json"))


def test_orphan_recovery_preserves_the_branch_and_requeues(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    task = store.add("one")
    store.update(task.id, status=Status.REVIEW, branch="agentq/1/a1", claimed_by="old")

    recovered = store.recover_orphans()
    assert [item.id for item in recovered] == [task.id]
    current = store.get(task.id)
    assert current is not None
    assert current.status == Status.NEW
    assert current.branch is None
    assert current.previous_branches == ["agentq/1/a1"]
    assert current.claimed_by == ""


def test_cancellation_is_only_available_before_delivery(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    queued = store.add("queued")
    cancelled = store.request_cancel(queued.id)
    assert cancelled is not None and cancelled.status == Status.CANCELLED

    active = store.add("active")
    store.take_next()
    requested = store.request_cancel(active.id)
    assert requested is not None and requested.status == Status.CANCELLING
    finished = store.complete_cancel(active.id, "stopped")
    assert finished is not None and finished.status == Status.CANCELLED

    approved = store.add("approved")
    store.update(approved.id, status=Status.APPROVED)
    assert store.request_cancel(approved.id) is None


def test_from_dict_tolerates_an_older_file() -> None:
    task = Task.from_dict({"id": 3, "text": "x", "retired_field": 1})
    assert task.id == 3
    assert task.status == Status.NEW


def test_delivery_record_round_trips(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    task = store.add("one")
    approved = store.approve(
        task.id,
        branch="agentq/1/a1",
        commit="abc123",
        requested_mode="auto",
        base_branch="main",
        summary="implemented safely",
        review="APPROVE",
    )
    assert approved is not None and approved.status == Status.APPROVED

    loaded = store.get(task.id)
    assert loaded is not None
    assert loaded.approved_sha == "abc123"
    assert loaded.delivery == DeliveryRecord(
        requested_mode="auto",
        status="pending",
        base_branch="main",
        branch="agentq/1/a1",
        commit="abc123",
        summary="implemented safely",
        review="APPROVE",
    )


def test_delivery_retry_does_not_repeat_agent_work(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    task = store.add("one")
    claimed = store.take_next()
    assert claimed is not None
    store.approve(
        task.id,
        branch="agentq/1/a1",
        commit="abc123",
        requested_mode="pr",
        base_branch="main",
    )
    started = store.start_delivery(task.id, "pr")
    assert started is not None and started.delivery.attempts == 1
    store.update_delivery_stage(task.id, "push")
    blocked = store.block_delivery(
        task.id, stage="push", code="push_failed", message="origin refused"
    )
    assert blocked is not None and blocked.status == Status.BLOCKED
    assert blocked.delivery.errors[0].stage == "push"

    retried = store.retry_delivery(task.id)
    assert retried is not None and retried.status == Status.APPROVED
    assert retried.attempts == 1
    restarted = store.start_delivery(task.id, "pr")
    assert restarted is not None and restarted.delivery.attempts == 2
    assert len(restarted.delivery.errors) == 1


def test_retry_task_preserves_the_old_branch_and_uses_a_new_attempt(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.add("one")
    first = store.take_next()
    assert first is not None and first.attempts == 1
    first_branch = attempt_branch("agentq", first.id, first.attempts)
    store.update(
        first.id,
        branch=first_branch,
        approved_sha="old-sha",
        delivery=DeliveryRecord(commit="old-sha", branch=first_branch, status="blocked"),
        status=Status.FAILED,
        url="https://example.test/old",
        cost_usd=1.25,
    )

    retried = store.retry_task(first.id)
    assert retried is not None
    assert retried.branch is None
    assert retried.previous_branches == [first_branch]
    assert retried.approved_sha == ""
    assert retried.delivery == DeliveryRecord()
    assert retried.url == ""
    assert retried.cost_usd == 0.0
    second = store.take_next()
    assert second is not None and second.attempts == 2
    assert attempt_branch("agentq", second.id, second.attempts) == "agentq/1/a2"


def test_v1_blocked_task_is_preserved_as_legacy_and_backed_up(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.path.write_text(
        '{"version": 1, "tasks": [{"id": 4, "text": "x", "status": "blocked", '
        '"branch": "agentq/4"}]}',
        encoding="utf-8",
    )
    legacy = store.get(4)
    assert legacy is not None and legacy.legacy_blocked is True
    assert legacy.branch == "agentq/4"
    assert store.retry_delivery(4) is None

    store.update(4, note="still needs a human")
    assert (tmp_path / "tasks.v1.backup.json").exists()
    assert f'"version": {SCHEMA_VERSION}' in store.path.read_text(encoding="utf-8")


def test_open_and_terminal_statuses(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    task = store.add("one")
    assert store.open_tasks()
    store.update(task.id, status=Status.DONE)
    assert store.open_tasks() == []


def test_remove(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    task = store.add("one")
    assert store.remove(task.id) is True
    assert store.remove(task.id) is False


def test_archive_moves_old_terminal_tasks_out_of_the_hot_queue(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    for number in range(5):
        task = store.add(f"done {number}")
        store.update(task.id, status=Status.DONE)
    waiting = store.add("waiting")

    assert store.archive(keep=2) == 3
    assert [task.id for task in store.load()] == [waiting.id, 5, 4]
    assert [task.id for task in store.archived()] == [3, 2, 1]
    assert store.archive(keep=2) == 0


def test_title_is_the_first_line() -> None:
    assert Task(id=1, text="fix the header\nand the footer").title == "fix the header"


def test_concurrent_adds_lose_nothing(tmp_path: Path) -> None:
    # The lock, exercised: every add is a read-modify-write, and a lost update
    # would show up here as a duplicate or a missing id.
    store = store_at(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: store.add(f"task {index}"), range(24)))
    assert sorted(task.id for task in store.load()) == list(range(1, 25))
