from __future__ import annotations

from pathlib import Path
from typing import Any

from mergerail.fronts.base import StreamEvent
from mergerail.fronts.folder import FolderFront
from mergerail.fronts.telegram import HELP, TelegramFront
from mergerail.tasks import Status, Task, TaskStore


class Recording(TelegramFront):
    """The front, with the network replaced by a list."""

    def __init__(self, store: TaskStore, admins: set[int], state_dir: Path) -> None:
        super().__init__(store, "token", admins, state_dir)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def api(self, method: str, *, http_timeout: int = 20, **payload: Any) -> Any:
        self.calls.append((method, payload))
        if method == "sendMessage":
            return {"message_id": len(self.calls)}
        return None

    @property
    def messages(self) -> list[str]:
        return [
            str(payload.get("text", ""))
            for method, payload in self.calls
            if method == "sendMessage"
        ]


def message(text: str, user: int = 5, chat: int = 5) -> dict[str, Any]:
    return {"update_id": 1, "message": {"chat": {"id": chat}, "from": {"id": user}, "text": text}}


def test_a_plain_message_is_a_task(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    front._handle(message("the header overlaps on mobile"))

    tasks = store.load()
    assert len(tasks) == 1
    assert tasks[0].text == "the header overlaps on mobile"
    assert tasks[0].origin == {"chat_id": "5", "user_id": "5"}
    assert "#1" in front.messages[0]


def test_the_command_prefix_is_optional_noise(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    front._handle(message("/todo fix the footer"))
    assert store.load()[0].text == "fix the footer"


def test_strangers_are_ignored(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    front._handle(message("do my bidding", user=999))
    assert store.load() == []
    assert front.messages == []


def test_the_first_start_claims_an_unowned_bot(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, set(), tmp_path)
    front._handle(message("/start", user=77))
    assert front.admins == {77}
    assert "Claimed" in front.messages[0]

    # ...and the next stranger is a stranger.
    front._handle(message("/start", user=78))
    assert front.admins == {77}


def test_list_and_drop(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    store.add("fix the header", source="telegram", origin={"chat_id": "5"})

    front._handle(message("/list"))
    assert "fix the header" in front.messages[-1]

    front._handle(message("/drop 1"))
    assert store.load() == []


def test_retry_puts_a_failed_task_back(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    task = store.add("fix it")
    store.update(task.id, status=Status.FAILED)

    front._handle(message("/retry 1"))
    reloaded = store.get(1)
    assert reloaded is not None and reloaded.status == Status.NEW


def test_message_adds_a_multiword_instruction_and_reopens_a_terminal_task(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    task = store.add("fix it")
    store.update(task.id, status=Status.DONE, note="first run")

    front._handle(message("/message 1 also update the release notes"))

    (saved,) = store.messages(task.id)
    assert saved.text == "also update the release notes"
    assert saved.mode == "instruction"
    assert saved.status == "pending"
    assert saved.author == "telegram:user-5"
    reloaded = store.get(task.id)
    assert reloaded is not None and reloaded.status == Status.NEW
    assert len(reloaded.runs) == 1
    assert "Message queued" in front.messages[-1]
    assert "Task reopened" in front.messages[-1]


def test_comment_is_stored_without_reopening_a_terminal_task(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    task = store.add("fix it")
    store.update(task.id, status=Status.FAILED)

    update = message("/comment 1 this is context only")
    update["message"]["from"]["username"] = "lama"
    front._handle(update)

    (saved,) = store.messages(task.id)
    assert (saved.text, saved.mode, saved.status) == (
        "this is context only",
        "comment",
        "stored",
    )
    assert saved.author == "telegram:@lama"
    reloaded = store.get(task.id)
    assert reloaded is not None and reloaded.status == Status.FAILED
    assert reloaded.runs == []
    assert "Comment stored" in front.messages[-1]


def test_thread_commands_validate_task_and_body(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    store.add("fix it")

    front._handle(message("/message nope text"))
    front._handle(message("/message 99 text"))
    front._handle(message("/comment 1"))

    assert "Usage:" in front.messages[-3]
    assert "No task #99" in front.messages[-2]
    assert "Write some text" in front.messages[-1]
    assert store.messages(1) == []


def test_show_includes_recent_thread_messages_and_pending_count(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    task = store.add("x" * 10_000)
    for number in range(4):
        store.append_message(
            task.id,
            f"thread message {number} " + "<>&" * 500,
            mode="comment" if number == 0 else "instruction",
            author=f"person-{number}",
        )

    front._handle(message("/show 1"))

    card = front.messages[-1]
    assert "4 messages · 3 pending" in card
    assert "thread message 0" not in card
    assert "thread message 1" in card
    assert "thread message 3" in card
    assert len(card) <= 4000


def test_help_describes_thread_commands() -> None:
    assert "/message &lt;id&gt; &lt;text&gt;" in HELP
    assert "/comment &lt;id&gt; &lt;text&gt;" in HELP


def test_reports_go_back_to_whoever_asked(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    task = Task(id=1, text="fix it", source="telegram", origin={"chat_id": "42"}, url="http://pr/1")

    front.report(task, "done", "moved the div")
    method, payload = front.calls[-1]
    assert method == "sendMessage"
    assert payload["chat_id"] == 42
    assert "moved the div" in payload["text"]
    assert "http://pr/1" in payload["text"]


def test_a_task_from_another_front_reports_to_the_admins(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5, 6}, tmp_path)
    front.report(Task(id=1, text="fix it", source="folder"), "failed", "the reviewer said no")
    assert [payload["chat_id"] for _, payload in front.calls] == [5, 6]


def test_html_in_a_task_cannot_break_the_message(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    front._handle(message("<b>bold</b> & broken"))
    assert "&lt;b&gt;" in front.messages[0]


def test_the_offset_moves_past_handled_updates(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = Recording(store, {5}, tmp_path)
    update = message("one")
    update["update_id"] = 40
    front.offset = max(front.offset, int(update["update_id"]) + 1)
    front._save_state()

    reloaded = Recording(store, set(), tmp_path)
    reloaded._load_state()
    assert reloaded.offset == 41
    assert reloaded.admins == {5}


class SetupStub:
    def __init__(self) -> None:
        self.received: dict[str, object] = {}

    def setup_snapshot(self) -> dict[str, Any]:
        return {
            "agents": {"codex": {"available": True}},
            "values": {},
        }

    def apply_setup(self, payload: Any, progress: Any) -> tuple[int, dict[str, Any]]:
        self.received = dict(payload)
        progress("validating", "Checking answers")
        progress("saving", "Writing mergerail.toml")
        progress("complete", "Ready")
        return 200, {"ok": True}


def test_init_guides_the_user_and_edits_realtime_progress(tmp_path: Path) -> None:
    front = Recording(TaskStore(tmp_path / "tasks.json"), {5}, tmp_path)
    setup = SetupStub()
    front.bind_setup(setup)

    for answer in (
        "/init",
        "codex",
        "production",
        "prepare release",
        "ask",
    ):
        front._handle(message(answer))

    assert setup.received == {
        "agent": "codex",
        "environment": "production",
        "summary": "prepare release",
        "external_actions": "ask",
    }
    assert any("1/4" in text for text in front.messages)
    edits = [payload["text"] for method, payload in front.calls if method == "editMessageText"]
    assert any("saving" in text for text in edits)
    assert "Setup complete" in edits[-1]


def test_agent_progress_updates_one_telegram_message(tmp_path: Path) -> None:
    front = Recording(TaskStore(tmp_path / "tasks.json"), {5}, tmp_path)
    task = Task(id=7, text="fix it", source="telegram", origin={"chat_id": "5"})

    front.stream(task, StreamEvent("runner", "reset"))
    front.stream(task, StreamEvent("fixer", "status", "round 1"))
    front.stream(task, StreamEvent("fixer", "text", "working"))
    front.stream(task, StreamEvent("fixer", "result", "done"))

    sent = [payload for method, payload in front.calls if method == "sendMessage"]
    edited = [payload for method, payload in front.calls if method == "editMessageText"]
    assert len(sent) == 1
    assert edited
    assert "done" in edited[-1]["text"]


def test_the_folder_front_takes_files_and_writes_answers(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.json")
    front = FolderFront(store, tmp_path)
    front.start()
    (front.inbox / "task.md").write_text("make the button blue", encoding="utf-8")

    claimed = front.next_task()
    assert claimed is not None
    assert claimed.text == "make the button blue"
    assert not (front.inbox / "task.md").exists()

    front.report(claimed, "done", "painted it")
    assert "painted it" in (front.outbox / "0001-done.md").read_text(encoding="utf-8")
