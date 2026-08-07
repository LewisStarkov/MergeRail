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


@pytest.fixture
def front(tmp_path: Path) -> Iterator[WebFront]:
    served = WebFront(TaskStore(tmp_path / "tasks.json"), port=0)  # 0: any free port
    served.start()
    yield served
    served.stop()


def call(front: WebFront, path: str, payload: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        f"http://127.0.0.1:{front.port}{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_the_page_is_served(front: WebFront) -> None:
    with urllib.request.urlopen(f"http://127.0.0.1:{front.port}/", timeout=5) as response:
        page = response.read().decode("utf-8")
    assert "agentq" in page
    assert "textContent" in page  # task text must never become markup
    assert 'new EventSource("/api/events")' in page
    assert 'aria-live' in page
    assert 'document.createElement("details")' in page
    assert "expandedStreams.has(taskId)" in page
    assert 'class="workbench"' in page
    assert 'id="detail"' in page
    assert 'class="pill' not in page
    assert "bootstrap-icons@1.13.1" in page
    assert "bi bi-terminal" in page
    assert "statusIcons" in page
    assert "${t.icon}" not in page
    assert "📎" not in page
    assert "--accent: #0f62fe" in page
    assert "data-design" not in page
    assert "designPicker" not in page


def test_a_posted_task_lands_on_the_queue(front: WebFront) -> None:
    created = call(front, "/api/tasks", {"text": "fix the header"})
    assert created["id"] == 1

    listed = call(front, "/api/tasks")
    (task,) = listed["tasks"]
    assert task["text"] == "fix the header"
    assert task["source"] == "web"
    assert task["open"] is True


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

    authenticated = WebFront(
        TaskStore(tmp_path / "authenticated.json"), host="0.0.0.0", port=0
    )
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
