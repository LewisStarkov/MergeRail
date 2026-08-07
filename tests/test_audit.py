from __future__ import annotations

from pathlib import Path

from agentq.audit import AUDIT_SCHEMA_VERSION, AuditLog


def test_audit_log_appends_filters_and_ignores_broken_lines(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "events.jsonl")
    audit.emit("runner.started", root="/repo")
    audit.emit("task.finished", task=7, status="done")
    with audit.path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")

    assert [record["event"] for record in audit.read()] == ["runner.started", "task.finished"]
    assert audit.read(task=7)[0]["status"] == "done"
    assert audit.read(task=99) == []
    assert audit.read(limit=0) == []
    assert audit.read()[0]["schema"] == AUDIT_SCHEMA_VERSION


def test_audit_log_rotates_and_reads_across_backups(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "events.jsonl", max_bytes=140, backups=6)
    for number in range(6):
        audit.emit("tick", sequence=number, payload="x" * 30)

    assert (tmp_path / "events.jsonl.1").exists()
    records = audit.read(limit=10)
    assert [record["sequence"] for record in records] == list(range(6))


def test_audit_rotation_can_discard_without_backups(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "events.jsonl", max_bytes=1, backups=0)
    audit.emit("first")
    audit.emit("second")

    assert [record["event"] for record in audit.read()] == ["second"]
