"""A web page on localhost, for everyone a chat does not reach.

``mergerail --web`` serves one page at ``http://127.0.0.1:8788``: a box to write
the task in, the queue underneath, a file picker for screenshots. Durable task
state stays in the shared JSON queue; transient agent output reaches the page
through a local server-sent event stream.

No framework on purpose, on either side. The server is ``http.server`` from
the standard library in one daemon thread; the packaged page uses Bootstrap
Icons from its published CDN. Attachments arrive as base64 inside the
JSON body rather than as multipart, because parsing multipart without ``cgi`` —
removed in 3.13 — is a project of its own, and a 20 MB cap makes the difference
irrelevant.

It binds to localhost by default. A wider bind requires configured credentials
unless the operator explicitly opts into an unsafe unauthenticated exposure.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import secrets
import threading
import time
from contextlib import suppress
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .. import log
from ..tasks import Status, Task, TaskStore
from .base import Front, SetupController, StreamEvent

#: Anything larger is refused with a reason rather than failing halfway.
MAX_FILE_BYTES = 20 * 1024 * 1024

#: The JSON body cap: a file at the limit, base64-inflated, plus slack.
MAX_BODY_BYTES = MAX_FILE_BYTES * 4 // 3 + 64 * 1024

MAX_LOGIN_BYTES = 4 * 1024
MAX_LIVE_TASKS = 50
MAX_LIVE_TEXT_CHARS = 24_000
MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60
SESSION_TTL_SECONDS = 8 * 60 * 60
MAX_AUTH_CLIENTS = 1_000
MAX_AUTH_SESSIONS = 2_048
MAX_SSE_CLIENTS = 16
MAX_TASK_PAGE = 200
MAX_SETUP_BYTES = 64 * 1024
MAX_MESSAGE_PAGE = 200
MAX_MESSAGE_TEXT_CHARS = 100_000
MAX_IDEMPOTENCY_KEY_CHARS = 200

SESSION_COOKIE = "mergerail_session"

ACTION = re.compile(r"/api/tasks/(\d+)/(retry|retry-task|retry-delivery|cancel|close|delete)")
MESSAGES = re.compile(r"/api/tasks/(\d+)/messages")

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class WebFront(Front):
    """One page, one poll loop; the queue does the rest."""

    name = "web"

    def __init__(
        self,
        store: TaskStore,
        host: str = "127.0.0.1",
        port: int = 8788,
        *,
        unsafe_expose: bool = False,
        session_ttl: float = SESSION_TTL_SECONDS,
        max_sse_clients: int = MAX_SSE_CLIENTS,
    ) -> None:
        super().__init__(store)
        self.host = host
        self.port = port
        self.unsafe_expose = unsafe_expose
        self.session_ttl = max(session_ttl, 0.0)
        self.max_sse_clients = max(max_sse_clients, 1)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._condition = threading.Condition()
        self._version = 0
        self._stopping = False
        self._live: dict[int, dict[str, Any]] = {}
        self._setup_progress: dict[str, Any] = {
            "status": "idle",
            "stage": "",
            "message": "",
            "history": [],
        }
        self._setup_snapshot: dict[str, Any] = {
            "available": False,
            "initialized": False,
            "agents": {},
            "values": {},
        }
        self._auth: tuple[str, str] | None = None
        self._sessions: dict[str, float] = {}
        self._login_failures: dict[str, list[float]] = {}
        self._sse_clients = 0

    # --- lifecycle -------------------------------------------------------

    def bind_setup(self, controller: SetupController) -> None:
        super().bind_setup(controller)
        self._setup_snapshot = controller.setup_snapshot()

    def start(self) -> None:
        if self.host not in LOCAL_HOSTS and not self.auth_enabled and not self.unsafe_expose:
            raise SystemExit(
                "mergerail: refusing to expose the web front without authentication; "
                "configure web credentials or pass --unsafe-expose"
            )
        front = self

        class Handler(_Handler):
            pass

        Handler.front = front
        with self._condition:
            self._stopping = False
        try:
            self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            raise SystemExit(
                f"mergerail: cannot serve the web front on {self.host}:{self.port} — {exc}"
            ) from exc
        self.port = self.server.server_address[1]
        self.server.daemon_threads = True
        if self.host not in LOCAL_HOSTS and self.unsafe_expose:
            log.warn("web.exposed", host=self.host, note="authentication explicitly disabled")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        log.info("web.listening", url=f"http://{self.host}:{self.port}")

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def report(self, task: Task, event: str, text: str) -> None:
        del text
        with self._condition:
            trace = self._live.get(task.id)
            if trace is not None and event in {"done", "failed", "blocked", "cancelled"}:
                trace["stage"] = event
                trace["sequence"] = int(trace["sequence"]) + 1
            self._trim_live()
            self._version += 1
            self._condition.notify_all()

    def stream(self, task: Task, event: StreamEvent) -> None:
        with self._condition:
            if event.kind == "reset":
                self._live.pop(task.id, None)
                self._live[task.id] = {"sequence": 0, "stage": "", "roles": {}}
            trace = self._live.setdefault(task.id, {"sequence": 0, "stage": "", "roles": {}})
            trace["sequence"] = int(trace["sequence"]) + 1
            if event.role == "runner":
                if event.kind == "status":
                    trace["stage"] = event.text
            else:
                roles = trace["roles"]
                role = roles.setdefault(
                    event.role,
                    {"status": "", "text": "", "tools": [], "history": []},
                )
                self._apply_stream_event(role, event)
            self._trim_live()
            self._version += 1
            self._condition.notify_all()

    @staticmethod
    def _apply_stream_event(role: dict[str, Any], event: StreamEvent) -> None:
        if event.kind == "status":
            if event.text.startswith("round ") and role["text"]:
                role["history"].append(str(role["text"])[-MAX_LIVE_TEXT_CHARS:])
                role["history"] = role["history"][-5:]
                role["text"] = ""
                role["tools"] = []
            role["status"] = event.text
        elif event.kind == "text" and event.text:
            current = str(role["text"])
            if event.text.startswith(current):
                role["text"] = event.text[-MAX_LIVE_TEXT_CHARS:]
            elif not current.endswith(event.text):
                role["text"] = (current + event.text)[-MAX_LIVE_TEXT_CHARS:]
        elif event.kind == "tool" and event.text:
            role["tools"].append(event.text[-1000:])
            role["tools"] = role["tools"][-40:]
        elif event.kind in {"result", "error"}:
            if event.text:
                role["text"] = event.text[-MAX_LIVE_TEXT_CHARS:]
            role["status"] = "complete" if event.kind == "result" else "failed"

    def _trim_live(self) -> None:
        while len(self._live) > MAX_LIVE_TASKS:
            finished = next(
                (
                    task_id
                    for task_id, trace in self._live.items()
                    if trace.get("stage") in {"done", "failed", "blocked", "cancelled"}
                ),
                None,
            )
            self._live.pop(finished if finished is not None else next(iter(self._live)))

    def _changed(self) -> None:
        with self._condition:
            self._version += 1
            self._condition.notify_all()

    def wait_for_change(self, version: int, timeout: float) -> int | None:
        with self._condition:
            self._condition.wait_for(
                lambda: self._version != version or self._stopping,
                timeout=timeout,
            )
            return None if self._stopping else self._version

    # --- what the handler calls ------------------------------------------

    @property
    def auth_enabled(self) -> bool:
        return self._auth is not None

    def enable_auth(self, username: str, password: str) -> None:
        with self._condition:
            self._auth = (username, password)
            self._sessions.clear()
            self._login_failures.clear()

    def authenticate(self, username: str, password: str, *, client: str = "") -> str | None:
        now = time.monotonic()
        with self._condition:
            self._purge_auth_state(now)
            expected = self._auth
            failures = self._login_failures.get(client, [])
        if expected is None:
            return None
        if len(failures) >= MAX_LOGIN_FAILURES:
            return None
        if not secrets.compare_digest(username, expected[0]) or not secrets.compare_digest(
            password, expected[1]
        ):
            with self._condition:
                if (
                    client not in self._login_failures
                    and len(self._login_failures) >= MAX_AUTH_CLIENTS
                ):
                    self._login_failures.pop(next(iter(self._login_failures)))
                self._login_failures.setdefault(client, []).append(now)
            return None
        token = secrets.token_urlsafe(32)
        with self._condition:
            self._login_failures.pop(client, None)
            if len(self._sessions) >= MAX_AUTH_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))
            self._sessions[token] = now + self.session_ttl
        return token

    def login_limited(self, client: str) -> bool:
        now = time.monotonic()
        with self._condition:
            self._purge_auth_state(now)
            return len(self._login_failures.get(client, [])) >= MAX_LOGIN_FAILURES

    def authenticated(self, cookie: str) -> bool:
        with self._condition:
            if self._auth is None:
                return True
        jar = SimpleCookie()
        try:
            jar.load(cookie)
        except CookieError:
            return False
        morsel = jar.get(SESSION_COOKIE)
        if morsel is None:
            return False
        with self._condition:
            self._purge_auth_state(time.monotonic())
            expires = self._sessions.get(morsel.value, 0.0)
            return expires > time.monotonic()

    def logout(self, cookie: str) -> None:
        jar = SimpleCookie()
        with suppress(CookieError):
            jar.load(cookie)
        morsel = jar.get(SESSION_COOKIE)
        if morsel is not None:
            with self._condition:
                self._sessions.pop(morsel.value, None)

    def _purge_auth_state(self, now: float) -> None:
        self._sessions = {
            token: expires for token, expires in self._sessions.items() if expires > now
        }
        self._login_failures = {
            client: recent
            for client, stamps in self._login_failures.items()
            if (recent := [stamp for stamp in stamps if now - stamp < LOGIN_WINDOW_SECONDS])
        }

    def acquire_stream(self) -> bool:
        with self._condition:
            if self._sse_clients >= self.max_sse_clients:
                return False
            self._sse_clients += 1
            return True

    def release_stream(self) -> None:
        with self._condition:
            self._sse_clients = max(self._sse_clients - 1, 0)

    def tasks_payload(self, *, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        with self._condition:
            live = copy.deepcopy(self._live)
            progress = copy.deepcopy(self._setup_progress)
        tasks = self.store.load()
        offset = max(offset, 0)
        limit = min(max(limit, 1), MAX_TASK_PAGE)
        rows = [
            {
                **task.to_dict(),
                "icon": task.icon,
                "open": task.is_open,
                "live": live.get(task.id),
                **self.store.message_stats(task.id),
            }
            for task in tasks[offset : offset + limit]
        ]
        return {
            "tasks": rows,
            "total": len(tasks),
            "offset": offset,
            "limit": limit,
            "setup": self.setup_payload(progress),
        }

    def setup_payload(self, progress: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._condition:
            snapshot = copy.deepcopy(self._setup_snapshot)
            state = copy.deepcopy(progress or self._setup_progress)
        snapshot["progress"] = state
        snapshot.setdefault("available", self.setup_controller is not None)
        return snapshot

    def configure(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        controller = self.setup_controller
        if controller is None:
            return 503, {"error": "setup is not attached to this front"}
        with self._condition:
            self._setup_progress = {
                "status": "running",
                "stage": "starting",
                "message": "Starting setup",
                "history": [],
            }
            self._version += 1
            self._condition.notify_all()

        def progress(stage: str, message: str) -> None:
            with self._condition:
                history = list(self._setup_progress["history"])
                history.append({"stage": stage, "message": message})
                self._setup_progress = {
                    "status": "failed"
                    if stage == "failed"
                    else ("complete" if stage == "complete" else "running"),
                    "stage": stage,
                    "message": message,
                    "history": history[-20:],
                }
                self._version += 1
                self._condition.notify_all()

        status, response = controller.apply_setup(payload, progress)
        if status < 400:
            snapshot = controller.setup_snapshot()
            with self._condition:
                self._setup_snapshot = snapshot
        with self._condition:
            still_running = self._setup_progress["status"] == "running"
        if status >= 400 and still_running:
            progress("failed", str(response.get("error") or "setup failed"))
        return status, response

    def accept(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        text = str(payload.get("text") or "").strip()
        file = payload.get("file") if isinstance(payload.get("file"), dict) else None
        if not text and not file:
            return 400, {"error": "write a task, attach a file, or both"}
        task = self.store.add(text, source=self.name)
        lost = ""
        if file is not None:
            saved = self._save_attachment(task, file)
            if saved is None:
                lost = "the attachment could not be saved; the task is queued without it"
        response: dict[str, Any] = {"id": task.id}
        if lost:
            response["warning"] = lost
        self._changed()
        return 201, response

    def messages_payload(
        self, task_id: int, *, after: int = 0, limit: int = 100
    ) -> tuple[int, dict[str, Any]]:
        if self.store.get(task_id) is None:
            return 404, {"error": f"no task #{task_id}"}
        messages = self.store.messages(task_id, after=after, limit=limit)
        return 200, {
            "messages": [message.to_dict() for message in messages],
            **self.store.message_stats(task_id),
            "after": after,
            "limit": limit,
        }

    def accept_message(self, task_id: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        task = self.store.get(task_id)
        if task is None:
            return 404, {"error": f"no task #{task_id}"}

        raw_text = payload.get("text", "")
        if not isinstance(raw_text, str):
            return 400, {"error": "text must be a string"}
        text = raw_text.strip()
        if not text:
            return 400, {"error": "write a message"}
        if len(text) > MAX_MESSAGE_TEXT_CHARS:
            return 400, {"error": "message is too long"}

        mode = payload.get("mode", "instruction")
        if not isinstance(mode, str) or mode not in {"comment", "instruction"}:
            return 400, {"error": "mode must be comment or instruction"}

        idempotency_key = payload.get("idempotency_key", "")
        if not isinstance(idempotency_key, str):
            return 400, {"error": "idempotency_key must be a string"}
        if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_CHARS:
            return 400, {"error": "idempotency_key is too long"}

        file = payload.get("file")
        attachment: tuple[str, bytes] | None = None
        if file is not None:
            if not isinstance(file, dict):
                return 400, {"error": "file must be an object"}
            name = file.get("name", "")
            raw = file.get("data", "")
            if not isinstance(name, str) or not isinstance(raw, str):
                return 400, {"error": "file name and data must be strings"}
            try:
                blob = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                return 400, {"error": "file data must be valid base64"}
            if not blob:
                return 400, {"error": "file is empty"}
            if len(blob) > MAX_FILE_BYTES:
                return 413, {"error": "file is too large"}
            attachment = (Path(name).suffix.lower() or ".bin", blob)

        message = self.store.append_message(
            task_id,
            text,
            role="user",
            mode=mode,
            author="web",
            idempotency_key=idempotency_key,
        )
        if message is None:  # task was removed between validation and append
            return 404, {"error": f"no task #{task_id}"}
        warning = ""
        if attachment is not None and not message.file:
            suffix, blob = attachment
            destination = self.store.message_media_path(task_id, message.id, suffix)
            try:
                destination.write_bytes(blob)
            except OSError as exc:  # pragma: no cover - disk trouble
                log.warn(
                    "web.message_attachment_failed",
                    task=task_id,
                    message=message.id,
                    error=log.clip(exc, 120),
                )
                warning = "the attachment could not be saved"
            else:
                updated = self.store.update_message(task_id, message.id, file=str(destination))
                if updated is not None:
                    message = updated

        if mode == "instruction" and message.status == "pending" and not task.is_open:
            self.store.retry_task(task_id)

        response: dict[str, Any] = {"message": message.to_dict()}
        if warning:
            response["warning"] = warning
        self._changed()
        return (202 if mode == "instruction" else 201), response

    def _save_attachment(self, task: Task, file: dict[str, Any]) -> Path | None:
        """Decode it, or lose the screenshot but keep the task."""
        raw = str(file.get("data") or "")
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return None
        if not blob or len(blob) > MAX_FILE_BYTES:
            return None
        suffix = Path(str(file.get("name") or "")).suffix.lower()
        destination = self.store.media_path(task.id, suffix or ".bin")
        try:
            destination.write_bytes(blob)
        except OSError as exc:  # pragma: no cover - disk trouble
            log.warn("web.attachment_failed", task=task.id, error=log.clip(exc, 120))
            return None
        self.store.update(task.id, file=str(destination))
        return destination

    def act(self, task_id: int, verb: str) -> tuple[int, dict[str, Any]]:
        task = self.store.get(task_id)
        if task is None:
            return 404, {"error": f"no task #{task_id}"}
        if verb == "cancel":
            if self.store.request_cancel(task_id) is None:
                return 409, {"error": f"task #{task_id} cannot be cancelled"}
        elif verb == "delete":
            if task.status in {
                Status.RUNNING,
                Status.REVIEW,
                Status.APPROVED,
                Status.DELIVERING,
                Status.CANCELLING,
            }:
                return 409, {"error": f"task #{task_id} is active; cancel it first"}
            self.store.remove(task_id)
            with self._condition:
                self._live.pop(task_id, None)
        elif verb in ("retry", "retry-task"):
            if self.store.retry_task(task_id) is None:
                return 409, {"error": f"task #{task_id} cannot be retried"}
            with self._condition:
                self._live.pop(task_id, None)
        elif verb == "retry-delivery":
            if self.store.retry_delivery(task_id) is None:
                return 409, {"error": f"task #{task_id} has no recoverable delivery"}
        elif verb == "close" and task.status == Status.NEW:
            self.store.update(task_id, status=Status.CLOSED)
        else:
            return 400, {"error": f"unknown or invalid action {verb!r}"}
        self._changed()
        return 200, {"ok": True}


class _Handler(BaseHTTPRequestHandler):
    front: WebFront
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        with suppress(BrokenPipeError, ConnectionResetError):
            super().handle()

    def log_message(self, format: str, *args: Any) -> None:  # stdlib's chosen name
        """Quiet. The runner's log is for the runner; polling is not news."""

    def do_GET(self) -> None:
        target = urlsplit(self.path)
        if target.path == "/login":
            if self.front.authenticated(self.headers.get("Cookie", "")):
                self._redirect("/")
            else:
                self._html(
                    LOGIN_PAGE.replace("{{error}}", ""), headers={"Cache-Control": "no-store"}
                )
            return
        if not self._authorized():
            return
        if target.path in ("/", "/index.html"):
            self._html(PAGE)
        elif target.path == "/api/tasks":
            query = parse_qs(target.query)
            try:
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                self._json(400, {"error": "offset and limit must be integers"})
                return
            self._json(200, self.front.tasks_payload(offset=offset, limit=limit))
        elif match := MESSAGES.fullmatch(target.path):
            query = parse_qs(target.query)
            try:
                after = int(query.get("after", ["0"])[0])
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                self._json(400, {"error": "after and limit must be integers"})
                return
            if after < 0 or limit < 1:
                self._json(400, {"error": "after must be non-negative and limit positive"})
                return
            status, payload = self.front.messages_payload(
                int(match.group(1)), after=after, limit=min(limit, MAX_MESSAGE_PAGE)
            )
            self._json(status, payload)
        elif target.path == "/api/setup":
            self._json(200, self.front.setup_payload())
        elif target.path == "/api/events":
            self._events()
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        target = urlsplit(self.path)
        if target.path == "/login":
            self._login()
            return
        if not self._authorized():
            return
        if target.path == "/logout":
            self.front.logout(self.headers.get("Cookie", ""))
            self._redirect(
                "/login",
                headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly"},
            )
            return
        action = ACTION.fullmatch(target.path)
        if action:
            status, payload = self.front.act(int(action.group(1)), action.group(2))
            self._json(status, payload)
            return
        message_path = MESSAGES.fullmatch(target.path)
        if target.path not in ("/api/tasks", "/api/setup") and message_path is None:
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        maximum = MAX_SETUP_BYTES if target.path == "/api/setup" else MAX_BODY_BYTES
        if length <= 0 or length > maximum:
            self._json(413 if length else 400, {"error": "bad request body"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "the body must be JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "the body must be a JSON object"})
            return
        if message_path is not None:
            status, payload = self.front.accept_message(int(message_path.group(1)), body)
        elif target.path == "/api/setup":
            status, payload = self.front.configure(body)
        else:
            status, payload = self.front.accept(body)
        self._json(status, payload)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, page: str, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.front.authenticated(self.headers.get("Cookie", "")):
            return True
        if self.path.startswith("/api/"):
            self._json(401, {"error": "authentication required"})
        else:
            self._redirect("/login", status=302)
        return False

    def _login(self) -> None:
        if not self.front.auth_enabled:
            self._redirect("/")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_LOGIN_BYTES:
            self._login_failed()
            return
        try:
            values = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            self._login_failed()
            return
        client = self.client_address[0]
        token = self.front.authenticate(
            values.get("username", [""])[0],
            values.get("password", [""])[0],
            client=client,
        )
        if token is None:
            self._login_failed(status=429 if self.front.login_limited(client) else 401)
            return
        cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
        if self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip() == "https":
            cookie += "; Secure"
        self._redirect("/", headers={"Set-Cookie": cookie})

    def _login_failed(self, *, status: int = 401) -> None:
        error = (
            '<div class="login-error" role="alert">'
            '<i class="bi bi-exclamation-circle" aria-hidden="true"></i>'
            "Those credentials do not match.</div>"
        )
        self._html(
            LOGIN_PAGE.replace("{{error}}", error),
            status=status,
            headers=(
                {"Cache-Control": "no-store", "Retry-After": "60"}
                if status == 429
                else {"Cache-Control": "no-store"}
            ),
        )

    def _redirect(
        self,
        location: str,
        *,
        status: int = 303,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _events(self) -> None:
        if not self.front.acquire_stream():
            self._json(503, {"error": "too many event streams"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        version = -1
        try:
            while True:
                changed = self.front.wait_for_change(version, timeout=15)
                if changed is None:
                    return
                if changed == version:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    payload = json.dumps(
                        self.front.tasks_payload(), ensure_ascii=False, separators=(",", ":")
                    )
                    self.wfile.write(f"id: {changed}\ndata: {payload}\n\n".encode())
                    version = changed
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.front.release_stream()


def _asset(name: str) -> str:
    return files(__package__).joinpath("web_assets", name).read_text(encoding="utf-8")


CONTROL_ROOM_CSS = _asset("control_room.css")
PAGE = _asset("index.html").replace("{{styles}}", CONTROL_ROOM_CSS)
LOGIN_PAGE = _asset("login.html").replace("{{styles}}", CONTROL_ROOM_CSS)

__all__ = ["CONTROL_ROOM_CSS", "LOGIN_PAGE", "MAX_FILE_BYTES", "PAGE", "WebFront"]
