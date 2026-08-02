from __future__ import annotations

from pathlib import Path

from agentq.tasks import Status, Task, TaskStore


def store_at(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.json")


def test_add_assigns_increasing_ids(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    first = store.add("one")
    second = store.add("two")
    assert (first.id, second.id) == (1, 2)
    assert [task.id for task in store.load()] == [2, 1]


def test_take_next_claims_the_oldest_once(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.add("one")
    store.add("two")

    claimed = store.take_next()
    assert claimed is not None and claimed.id == 1
    assert claimed.status == Status.RUNNING
    assert claimed.attempts == 1

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


def test_a_corrupt_file_reads_as_empty(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.path.write_text("{ not json", encoding="utf-8")
    assert store.load() == []
    # ...and writing over it still works, so one bad write is not permanent.
    assert store.add("one").id == 1


def test_from_dict_tolerates_an_older_file() -> None:
    task = Task.from_dict({"id": 3, "text": "x", "retired_field": 1})
    assert task.id == 3
    assert task.status == Status.NEW


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


def test_title_is_the_first_line() -> None:
    assert Task(id=1, text="fix the header\nand the footer").title == "fix the header"
