"""The web front, over real HTTP on an ephemeral port."""

from __future__ import annotations

import base64
import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentq.fronts.base import StreamEvent
from agentq.fronts.web import (
    MAX_LIVE_TASKS,
    MAX_LIVE_TEXT_CHARS,
    MAX_LOGIN_FAILURES,
    WebFront,
)
from agentq.tasks import Status, TaskStore


class SetupStub:
    def __init__(self) -> None:
        self.received: dict[str, object] = {}

    def setup_snapshot(self) -> dict[str, Any]:
        return {
            "available": True,
            "initialized": False,
            "busy": False,
            "agents": {"codex": {"available": True, "reason": "", "version": "1"}},
            "values": {
                "agent": "codex",
                "environment": "local",
                "summary": "",
                "external_actions": "forbid",
            },
        }

    def apply_setup(self, payload: Any, progress: Any) -> tuple[int, dict[str, Any]]:
        self.received = dict(payload)
        progress("validating", "Checking answers")
        progress("saving", "Writing agentq.toml")
        progress("complete", "Ready")
        return 200, {"ok": True}


@pytest.fixture
def front(tmp_path: Path) -> Iterator[WebFront]:
    served = WebFront(TaskStore(tmp_path / "tasks.json"), port=0)  # 0: any free port
    served.start()
    yield served
    served.stop()


def call(front: WebFront, path: str, payload: dict[str, Any] | None = None) -> Any:
    return call_response(front, path, payload)[1]


def call_response(
    front: WebFront, path: str, payload: dict[str, Any] | None = None
) -> tuple[int, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{front.port}{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_the_page_is_served(front: WebFront) -> None:
    with urllib.request.urlopen(f"http://127.0.0.1:{front.port}/", timeout=5) as response:
        page = response.read().decode("utf-8")
    assert "agentq" in page
    assert "textContent" in page  # task text must never become markup
    assert 'new EventSource("/api/events")' in page
    assert "aria-live" in page
    assert 'document.createElement("details")' in page
    assert "expandedStreams.has(taskId)" in page
    assert 'class="workbench"' in page
    assert 'id="sidebar-toggle"' in page
    assert 'id="sidebar-content"' in page
    assert 'id="sidebar-new-task"' in page
    assert 'id="sidebar-project-settings"' in page
    assert "sidebar-collapsed" in page
    assert "function setSidebarCollapsed" in page
    assert "prefers-reduced-motion: reduce" in page
    assert "el.dataset.status = t.status" in page
    assert 'el.setAttribute("aria-label", el.title)' in page
    assert "function sessionStatuses" not in page
    assert 'id="detail"' in page
    assert "function loadThread" in page
    assert "Send to agent" in page
    assert 'dataset.mode = "comment"' in page
    assert "list.append(agentActivity(task.id, task.live))" in page
    assert 'message.className = "thread-message system activity"' in page
    assert "body.append(live(t.id, t.live))" not in page
    assert 'class="pill' not in page
    assert "bootstrap-icons@1.13.1" in page
    assert "bi bi-terminal" not in page
    assert "statusIcons" in page
    assert "${t.icon}" not in page
    assert "📎" not in page
    assert "--accent: #0f62fe" in page
    assert "data-design" not in page
    assert "designPicker" not in page
    assert 'id="setup-form"' in page
    assert 'fetch("/api/setup"' in page
    assert 'id="setup-agent"' in page
    assert 'id="setup-environment"' in page
    assert 'id="setup-summary"' in page
    assert 'id="setup-external"' in page
    assert 'id="setup-fixer"' not in page
    assert 'id="setup-reviewer"' not in page
    assert 'id="setup-work-mode"' not in page
    assert 'id="setup-constraints"' not in page


def test_a_posted_task_lands_on_the_queue(front: WebFront) -> None:
    created = call(front, "/api/tasks", {"text": "fix the header"})
    assert created["id"] == 1

    listed = call(front, "/api/tasks")
    (task,) = listed["tasks"]
    assert task["text"] == "fix the header"
    assert task["source"] == "web"
    assert task["open"] is True
    assert task["message_count"] == 0
    assert task["pending_message_count"] == 0
    assert "messages" not in task


def test_task_messages_are_paginated_and_stats_stay_in_the_task_list(
    front: WebFront,
) -> None:
    task = front.store.add("original task", source="web")
    for number in range(3):
        status, created = call_response(
            front,
            f"/api/tasks/{task.id}/messages",
            {"text": f"comment {number}", "mode": "comment"},
        )
        assert status == 201
        assert created["message"]["status"] == "stored"

    first = call(front, f"/api/tasks/{task.id}/messages?limit=2")
    assert [message["text"] for message in first["messages"]] == [
        "comment 0",
        "comment 1",
    ]
    assert first["message_count"] == 3
    assert first["pending_message_count"] == 0

    second = call(front, f"/api/tasks/{task.id}/messages?after=2&limit=2")
    assert [message["text"] for message in second["messages"]] == ["comment 2"]

    (listed,) = call(front, "/api/tasks")["tasks"]
    assert listed["message_count"] == 3
    assert listed["last_message_at"]
    assert "messages" not in listed


def test_instruction_reopens_a_terminal_task_but_comment_does_not(front: WebFront) -> None:
    task = front.store.add("ship it", source="web")
    front.store.update(task.id, status=Status.DONE, note="first run")

    status, _ = call_response(
        front,
        f"/api/tasks/{task.id}/messages",
        {"text": "A note for later", "mode": "comment"},
    )
    assert status == 201
    current = front.store.get(task.id)
    assert current is not None and current.status == Status.DONE

    status, created = call_response(
        front,
        f"/api/tasks/{task.id}/messages",
        {
            "text": "Also update the docs",
            "mode": "instruction",
            "idempotency_key": "follow-up-1",
        },
    )
    assert status == 202
    assert created["message"]["status"] == "pending"
    current = front.store.get(task.id)
    assert current is not None and current.status == Status.NEW
    assert current.runs[-1].note == "first run"

    front.store.update(task.id, status=Status.DONE)
    front.store.update_message(task.id, created["message"]["id"], status="answered")
    call(
        front,
        f"/api/tasks/{task.id}/messages",
        {
            "text": "Also update the docs",
            "mode": "instruction",
            "idempotency_key": "follow-up-1",
        },
    )
    current = front.store.get(task.id)
    assert current is not None and current.status == Status.DONE
    assert len(front.store.messages(task.id)) == 2


def test_message_attachments_are_unique(front: WebFront) -> None:
    task = front.store.add("compare screenshots", source="web")
    paths = []
    for content in (b"first", b"second"):
        _, created = call_response(
            front,
            f"/api/tasks/{task.id}/messages",
            {
                "text": "screenshot",
                "mode": "comment",
                "file": {
                    "name": "same.png",
                    "data": base64.b64encode(content).decode("ascii"),
                },
            },
        )
        paths.append(Path(created["message"]["file"]))

    assert paths[0] != paths[1]
    assert [path.read_bytes() for path in paths] == [b"first", b"second"]


@pytest.mark.parametrize(
    "payload",
    [
        {"text": ""},
        {"text": 42},
        {"text": "hello", "mode": "run-now"},
        {"text": "hello", "idempotency_key": 42},
        {"text": "hello", "file": "not-an-object"},
        {"text": "hello", "file": {"name": "x.png", "data": "bad base64"}},
    ],
)
def test_invalid_task_messages_are_refused(front: WebFront, payload: dict[str, Any]) -> None:
    task = front.store.add("task", source="web")
    with pytest.raises(urllib.error.HTTPError) as caught:
        call(front, f"/api/tasks/{task.id}/messages", payload)
    assert caught.value.code == 400
    assert front.store.messages(task.id) == []


def test_messages_for_a_missing_task_are_404(front: WebFront) -> None:
    for payload in (None, {"text": "hello", "mode": "comment"}):
        with pytest.raises(urllib.error.HTTPError) as caught:
            call(front, "/api/tasks/999/messages", payload)
        assert caught.value.code == 404


def test_setup_is_applied_and_progress_is_in_the_sse_payload(front: WebFront) -> None:
    setup = SetupStub()
    front.bind_setup(setup)
    response = call(
        front,
        "/api/setup",
        {"agent": "codex", "environment": "production", "summary": "ship release"},
    )
    assert response == {"ok": True}
    assert setup.received["environment"] == "production"

    payload = call(front, "/api/tasks")
    assert payload["setup"]["progress"]["stage"] == "complete"
    assert [item["stage"] for item in payload["setup"]["progress"]["history"]] == [
        "validating",
        "saving",
        "complete",
    ]


def test_task_payload_is_paginated(tmp_path: Path) -> None:
    served = WebFront(TaskStore(tmp_path / "tasks.json"))
    for number in range(5):
        served.store.add(f"task {number}")
    payload = served.tasks_payload(offset=1, limit=2)
    assert payload["total"] == 5
    assert [task["text"] for task in payload["tasks"]] == ["task 3", "task 2"]


def test_live_agent_output_is_attached_to_its_task(front: WebFront) -> None:
    task = front.store.add("fix it", source="web")
    front.stream(task, StreamEvent("runner", "reset"))
    front.stream(task, StreamEvent("fixer", "status", "round 1"))
    front.stream(task, StreamEvent("fixer", "text", "working "))
    front.stream(task, StreamEvent("fixer", "text", "on it"))
    front.stream(task, StreamEvent("fixer", "tool", "Read · src/app.py"))

    (listed,) = call(front, "/api/tasks")["tasks"]
    assert listed["live"]["roles"]["fixer"] == {
        "status": "round 1",
        "text": "working on it",
        "tools": ["Read · src/app.py"],
        "history": [],
    }


def test_live_output_and_completed_traces_are_bounded(front: WebFront) -> None:
    first = None
    for number in range(MAX_LIVE_TASKS + 1):
        task = front.store.add(f"task {number}", source="web")
        first = first or task
        front.stream(task, StreamEvent("runner", "reset"))
        front.stream(task, StreamEvent("fixer", "text", "x" * (MAX_LIVE_TEXT_CHARS + 10)))
        front.report(task, "done", "")

    assert first is not None
    payload = {task["id"]: task for task in front.tasks_payload()["tasks"]}
    assert payload[first.id]["live"] is None
    newest = payload[MAX_LIVE_TASKS + 1]["live"]
    assert len(newest["roles"]["fixer"]["text"]) == MAX_LIVE_TEXT_CHARS


def test_sse_sends_the_current_queue_immediately(front: WebFront) -> None:
    front.store.add("stream me", source="web")
    with urllib.request.urlopen(f"http://127.0.0.1:{front.port}/api/events", timeout=5) as response:
        assert response.headers.get_content_type() == "text/event-stream"
        lines = [response.readline().decode("utf-8") for _ in range(3)]
    data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
    assert json.loads(data)["tasks"][0]["text"] == "stream me"


def test_an_attachment_is_saved_next_to_the_queue(front: WebFront, tmp_path: Path) -> None:
    data = base64.b64encode(b"png-bytes").decode("ascii")
    call(front, "/api/tasks", {"text": "see the shot", "file": {"name": "shot.png", "data": data}})
    (task,) = front.store.load()
    assert task.file is not None and task.file.endswith(".png")
    assert Path(task.file).read_bytes() == b"png-bytes"


def test_a_broken_attachment_loses_the_file_but_keeps_the_task(front: WebFront) -> None:
    answer = call(front, "/api/tasks", {"text": "still a task", "file": {"data": "not base64!!"}})
    assert "warning" in answer
    (task,) = front.store.load()
    assert task.file is None


def test_an_empty_submission_is_refused(front: WebFront) -> None:
    with pytest.raises(urllib.error.HTTPError) as caught:
        call(front, "/api/tasks", {"text": "   "})
    assert caught.value.code == 400


def test_retry_close_and_delete(front: WebFront) -> None:
    task = front.store.add("fix it")
    front.store.update(task.id, status=Status.FAILED)

    call(front, f"/api/tasks/{task.id}/retry", {})
    reloaded = front.store.get(task.id)
    assert reloaded is not None and reloaded.status == Status.NEW

    call(front, f"/api/tasks/{task.id}/close", {})
    reloaded = front.store.get(task.id)
    assert reloaded is not None and reloaded.status == Status.CLOSED

    call(front, f"/api/tasks/{task.id}/delete", {})
    assert front.store.get(task.id) is None


def test_acting_on_a_ghost_task_is_a_404(front: WebFront) -> None:
    with pytest.raises(urllib.error.HTTPError) as caught:
        call(front, "/api/tasks/99/retry", {})
    assert caught.value.code == 404


def test_active_task_must_be_cancelled_before_delete(front: WebFront) -> None:
    task = front.store.add("working")
    front.store.update(task.id, status=Status.RUNNING)
    status, _ = front.act(task.id, "delete")
    assert status == 409

    status, _ = front.act(task.id, "cancel")
    assert status == 200
    current = front.store.get(task.id)
    assert current is not None and current.status == Status.CANCELLING


def test_unknown_paths_are_404(front: WebFront) -> None:
    with pytest.raises(urllib.error.HTTPError) as caught:
        call(front, "/api/nothing")
    assert caught.value.code == 404


def test_auth_uses_the_styled_login_and_protects_api_and_sse(tmp_path: Path) -> None:
    served = WebFront(TaskStore(tmp_path / "tasks.json"), port=0)
    served.enable_auth("agentq", "correct-horse")
    served.store.add("protected task")
    served.start()
    root = f"http://127.0.0.1:{served.port}"
    try:
        with urllib.request.urlopen(root + "/", timeout=5) as response:
            page = response.read().decode("utf-8")
            assert response.url.endswith("/login")
        assert "Open Agent Workbench" in page
        assert "bootstrap-icons@1.13.1" in page
        assert 'class="login-shell"' in page

        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(root + "/api/tasks", timeout=5)
        assert denied.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as denied_thread:
            urllib.request.urlopen(root + "/api/tasks/1/messages", timeout=5)
        assert denied_thread.value.code == 401
        message_request = urllib.request.Request(
            root + "/api/tasks/1/messages",
            data=json.dumps({"text": "secret", "mode": "comment"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as denied_message:
            urllib.request.urlopen(message_request, timeout=5)
        assert denied_message.value.code == 401

        wrong = urllib.request.Request(
            root + "/login",
            data=urllib.parse.urlencode({"username": "agentq", "password": "wrong"}).encode(),
        )
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(wrong, timeout=5)
        assert rejected.value.code == 401
        assert "credentials do not match" in rejected.value.read().decode("utf-8")

        cookies = http.cookiejar.CookieJar()
        browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        login = urllib.request.Request(
            root + "/login",
            data=urllib.parse.urlencode(
                {"username": "agentq", "password": "correct-horse"}
            ).encode(),
        )
        with browser.open(login, timeout=5) as response:
            assert response.url == root + "/"
            assert 'class="workbench"' in response.read().decode("utf-8")
        (session,) = list(cookies)
        assert session.name == "agentq_session"
        assert session.has_nonstandard_attr("HttpOnly")

        with browser.open(root + "/api/events", timeout=5) as response:
            lines = [response.readline().decode("utf-8") for _ in range(3)]
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        assert json.loads(data)["tasks"][0]["text"] == "protected task"
    finally:
        served.stop()


def test_nonlocal_web_requires_authentication_or_explicit_unsafe(tmp_path: Path) -> None:
    refused = WebFront(TaskStore(tmp_path / "refused.json"), host="0.0.0.0", port=0)
    with pytest.raises(SystemExit, match="refusing to expose"):
        refused.start()

    authenticated = WebFront(TaskStore(tmp_path / "authenticated.json"), host="0.0.0.0", port=0)
    authenticated.enable_auth("agentq", "secret")
    authenticated.start()
    authenticated.stop()


def test_web_sessions_expire_logout_and_rate_limit(tmp_path: Path) -> None:
    served = WebFront(TaskStore(tmp_path / "tasks.json"), session_ttl=0.01)
    served.enable_auth("agentq", "secret")
    token = served.authenticate("agentq", "secret", client="ok")
    assert token is not None
    cookie = f"agentq_session={token}"
    assert served.authenticated(cookie)
    served.logout(cookie)
    assert not served.authenticated(cookie)

    expiring = served.authenticate("agentq", "secret", client="ok")
    assert expiring is not None
    time.sleep(0.02)
    assert not served.authenticated(f"agentq_session={expiring}")

    for _ in range(MAX_LOGIN_FAILURES):
        assert served.authenticate("agentq", "wrong", client="attacker") is None
    assert served.login_limited("attacker")
    assert served.authenticate("agentq", "secret", client="attacker") is None


def test_event_streams_have_a_connection_cap(tmp_path: Path) -> None:
    served = WebFront(TaskStore(tmp_path / "tasks.json"), max_sse_clients=1)
    assert served.acquire_stream()
    assert not served.acquire_stream()
    served.release_stream()
    assert served.acquire_stream()
    served.release_stream()
