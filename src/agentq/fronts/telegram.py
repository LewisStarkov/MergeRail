"""Telegram, on the raw Bot API and the standard library.

No bot framework on purpose. This gets installed next to somebody else's
project, and a framework of ours is a version conflict of theirs — so the whole
front is long polling over ``urllib`` in one background thread. It writes to the
same JSON queue the runner reads, which is also why the two never have to talk.

**Setting it up is a token.** If no admin ids are configured, the first person
to send ``/start`` claims the bot and is written into the state file. That is
the difference between "export a token and message your bot" and "look up your
numeric user id first".

A message that is not a command is a task. Nothing else about the interface is
worth remembering, and a screenshot with no caption is a perfectly good brief.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import escape
from pathlib import Path
from typing import Any

from .. import log
from ..tasks import TERMINAL, Status, Task, TaskMessage, TaskStore, write_atomic
from .base import Front, StreamEvent

API = "https://api.telegram.org"

#: How long a poll hangs waiting for something to happen.
POLL_TIMEOUT = 25

#: Anything larger is refused with a reason rather than failing halfway.
MAX_FILE_BYTES = 20 * 1024 * 1024

#: Rows before the list is cut short. Telegram takes 4096 characters.
MAX_ROWS = 20
ROW_CHARS = 80
RECENT_MESSAGES = 3

HELP = (
    "<b>agentq</b>\n\n"
    "Send me anything — that is a task. A screenshot works too.\n\n"
    "/list — the queue\n"
    "/show &lt;id&gt; — one task in full\n"
    "/message &lt;id&gt; &lt;text&gt; — send a follow-up to the agent\n"
    "/comment &lt;id&gt; &lt;text&gt; — add a note without running the agent\n"
    "/retry_task &lt;id&gt; — run the agents again\n"
    "/retry_delivery &lt;id&gt; — retry only merge or PR\n"
    "/init — configure agents and current project context\n"
    "/cancel — cancel the setup dialog\n"
    "/done &lt;id&gt; — close it by hand\n"
    "/drop &lt;id&gt; — delete it"
)

EVENT_TEXT = {
    "started": "🚧 <b>#{id}</b> picked up\n{text}",
    "done": "✅ <b>#{id}</b> landed\n{note}",
    "failed": "❌ <b>#{id}</b> failed\n{note}",
    "blocked": "⚠️ <b>#{id}</b> needs you\n{note}",
}


def _one_line(text: str, limit: int) -> str:
    head = (text.strip().splitlines() or [""])[0]
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "…"


class TelegramFront(Front):
    """Long polling in a thread; the queue does the rest."""

    name = "telegram"

    def __init__(self, store: TaskStore, token: str, admins: set[int], state_dir: Path) -> None:
        super().__init__(store)
        self.token = token
        self.admins = set(admins)
        self.state_path = state_dir / "telegram.json"
        self.offset = 0
        self.stopping = False
        self.thread: threading.Thread | None = None
        self._setups: dict[int, dict[str, Any]] = {}
        self._live_lock = threading.Lock()
        self._live: dict[int, dict[str, Any]] = {}

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._load_state()
        me = self.api("getMe")
        username = str(me.get("username") or "?") if isinstance(me, dict) else "?"
        log.info("telegram.connected", bot=f"@{username}", admins=len(self.admins) or "unclaimed")
        if not self.admins:
            log.warn("telegram.unclaimed — send /start to the bot to claim it")
        self.thread = threading.Thread(target=self._poll, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping = True
        if self.thread is not None:
            self.thread.join(timeout=2)

    # --- the Bot API -----------------------------------------------------

    def api(self, method: str, *, http_timeout: int = 20, **payload: Any) -> Any:
        """One call, returning whatever ``result`` was — or ``None`` on failure.

        ``http_timeout`` is how long *we* wait; a ``timeout`` in the payload is
        Telegram's own long-poll parameter. They are different numbers on
        purpose: ours has to be the larger one.
        """
        request = urllib.request.Request(
            f"{API}/bot{self.token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=http_timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            log.warn("telegram.api_failed", method=method, error=log.clip(exc, 120))
            return None
        return body.get("result") if isinstance(body, dict) else None

    def send(self, chat_id: int | str, text: str) -> int | None:
        sent = self.api(
            "sendMessage",
            chat_id=chat_id,
            text=text[:4000],
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if isinstance(sent, dict) and str(sent.get("message_id", "")).isdigit():
            return int(sent["message_id"])
        return None

    def edit(self, chat_id: int | str, message_id: int, text: str) -> None:
        self.api(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text[:4000],
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    # --- polling ---------------------------------------------------------

    def _poll(self) -> None:
        while not self.stopping:
            batch = self.api(
                "getUpdates",
                http_timeout=POLL_TIMEOUT + 15,
                offset=self.offset,
                timeout=POLL_TIMEOUT,
            )
            if not isinstance(batch, list):
                # A failed call must not become a hot loop against the API.
                time.sleep(3)
                continue
            for update in batch:
                if not isinstance(update, dict):
                    continue
                self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                try:
                    self._handle(update)
                except Exception as exc:  # one bad message must not end the thread
                    log.warn("telegram.handler_failed", error=log.clip(exc, 200))
            if batch:
                self._save_state()

    def _handle(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("channel_post")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = int(chat.get("id", 0))
        user_id = int(sender.get("id", 0))
        text = str(message.get("caption") or message.get("text") or "").strip()

        if not self._authorised(user_id, chat_id, text):
            return
        if text and text.split(maxsplit=1)[0].split("@")[0].lower() == "/cancel":
            if self._setups.pop(chat_id, None) is not None:
                self.send(chat_id, "Setup cancelled.")
            else:
                self.send(chat_id, "Nothing to cancel.")
            return
        if chat_id in self._setups and not text.startswith("/init"):
            self._setup_answer(chat_id, text)
            return
        if text.startswith("/") and self._command(chat_id, text, sender):
            return
        if not text and not self._attachment(message):
            return
        self._accept(chat_id, sender, text, message)

    def _authorised(self, user_id: int, chat_id: int, text: str) -> bool:
        """Admins only — and the first ``/start`` decides who that is."""
        if user_id in self.admins:
            return True
        if not self.admins and text.startswith("/start"):
            self.admins.add(user_id)
            self._save_state()
            log.info("telegram.claimed", user=user_id)
            self.send(chat_id, "Claimed. This bot now takes tasks from you only.\n\n" + HELP)
            return False
        log.warn("telegram.ignored", user=user_id)
        return False

    # --- commands --------------------------------------------------------

    def _command(self, chat_id: int, text: str, sender: dict[str, Any] | None = None) -> bool:
        """``True`` if this was a command and has been answered."""
        parts = text.split()
        verb = parts[0].lstrip("/").split("@")[0].lower()
        argument = parts[1] if len(parts) > 1 else ""

        if verb in ("start", "help"):
            self.send(chat_id, HELP)
            return True
        if verb == "init":
            self._start_setup(chat_id)
            return True
        if verb in ("list", "tasks", "queue"):
            self.send(chat_id, self._render_list())
            return True
        if verb in ("message", "comment"):
            self._thread_message(chat_id, verb, text, sender or {})
            return True
        if (
            verb
            in (
                "show",
                "done",
                "close",
                "drop",
                "retry",
                "retry_task",
                "retry_delivery",
            )
            and argument.isdigit()
        ):
            self._act(chat_id, verb, int(argument))
            return True
        # ``/todo fix the header`` — the verb is noise, the rest is the task.
        if verb in ("todo", "task", "fix") and len(parts) > 1:
            return False
        self.send(chat_id, HELP)
        return True

    def _thread_message(
        self, chat_id: int, verb: str, command: str, sender: dict[str, Any]
    ) -> None:
        parts = command.split(maxsplit=2)
        usage = f"Usage: /{verb} &lt;id&gt; &lt;text&gt;"
        if len(parts) < 2 or not parts[1].isdigit():
            self.send(chat_id, usage)
            return
        task_id = int(parts[1])
        body = parts[2].strip() if len(parts) > 2 else ""
        if not body:
            self.send(chat_id, f"Write some text.\n{usage}")
            return
        task = self.store.get(task_id)
        if task is None:
            self.send(chat_id, f"No task #{task_id}.")
            return
        mode = "instruction" if verb == "message" else "comment"
        saved = self.store.append_message(
            task_id,
            body,
            role="user",
            mode=mode,
            author=self._telegram_author(sender),
        )
        if saved is None:
            self.send(chat_id, f"No task #{task_id}.")
            return
        if mode == "comment":
            self.send(chat_id, f"💬 Comment stored on <b>#{task_id}</b>.")
            return
        reopened = task.status in TERMINAL and self.store.retry_task(task_id) is not None
        suffix = " Task reopened." if reopened else ""
        self.send(chat_id, f"🕓 Message queued for <b>#{task_id}</b>.{suffix}")

    @staticmethod
    def _telegram_author(sender: dict[str, Any]) -> str:
        username = str(sender.get("username") or "").strip().lstrip("@")
        if username:
            return f"telegram:@{username}"
        name = str(sender.get("first_name") or "").strip()
        if name:
            return f"telegram:{name}"
        user_id = str(sender.get("id") or "").strip()
        return f"telegram:user-{user_id}" if user_id else "telegram"

    # --- project setup ---------------------------------------------------

    def _start_setup(self, chat_id: int) -> None:
        controller = self.setup_controller
        if controller is None:
            self.send(chat_id, "Setup is not available until the runner is attached.")
            return
        snapshot = controller.setup_snapshot()
        names = [
            name
            for name, info in snapshot.get("agents", {}).items()
            if isinstance(info, dict) and info.get("available")
        ]
        self._setups[chat_id] = {
            "step": 0,
            "answers": {},
            "agents": names,
        }
        choices = ", ".join(["auto", *names])
        self.send(
            chat_id,
            f"<b>Project setup · 1/4</b>\nAgent ({escape(choices)}):",
        )

    def _setup_answer(self, chat_id: int, text: str) -> None:
        state = self._setups[chat_id]
        step = int(state["step"])
        answers = state["answers"]
        agents = {"auto", *state["agents"]}
        value = text.strip()
        if step == 0:
            if value not in agents:
                self.send(chat_id, "Choose: " + escape(", ".join(sorted(agents))))
                return
            answers["agent"] = value
        elif step == 1:
            if value not in {"local", "staging", "production"}:
                self.send(chat_id, "Choose: local, staging, or production.")
                return
            answers["environment"] = value
        elif step == 2:
            answers["summary"] = "" if value == "-" else value
        else:
            if value not in {"forbid", "ask"}:
                self.send(chat_id, "Choose: forbid or ask.")
                return
            answers["external_actions"] = value
        step += 1
        state["step"] = step
        if step >= 4:
            self._setups.pop(chat_id, None)
            self._apply_setup(chat_id, answers)
            return
        questions = (
            "Environment — local, staging, or production:",
            "What are we working on now? Send <code>-</code> for empty:",
            "External actions — forbid or ask:",
        )
        self.send(chat_id, f"<b>Project setup · {step + 1}/4</b>\n{questions[step - 1]}")

    def _apply_setup(self, chat_id: int, answers: dict[str, object]) -> None:
        controller = self.setup_controller
        if controller is None:
            return
        status_message: int | None = None

        def progress(stage: str, message: str) -> None:
            nonlocal status_message
            body = f"⚙️ <b>Setup · {escape(stage)}</b>\n{escape(message)}"
            if status_message is None:
                status_message = self.send(chat_id, body)
            else:
                self.edit(chat_id, status_message, body)

        status, response = controller.apply_setup(answers, progress)
        if status >= 400:
            reason = escape(str(response.get("error") or "unknown error"))
            body = f"❌ <b>Setup failed</b>\n{reason}"
        else:
            body = (
                "✅ <b>Setup complete</b>\nThe next task uses the new agents and project context."
            )
        if status_message is None:
            self.send(chat_id, body)
        else:
            self.edit(chat_id, status_message, body)

    def _act(self, chat_id: int, verb: str, task_id: int) -> None:
        task = self.store.get(task_id)
        if task is None:
            self.send(chat_id, f"No task #{task_id}.")
            return
        if verb == "show":
            self.send(chat_id, self._render_card(task))
            return
        if verb == "drop":
            self.store.remove(task_id)
            self.send(chat_id, f"🗑 #{task_id} deleted.")
            return
        if verb in ("retry", "retry_task"):
            retried = self.store.retry_task(task_id)
            text = (
                f"🕓 #{task_id} back in the queue; previous branches were preserved."
                if retried is not None
                else f"Task #{task_id} cannot be retried."
            )
            self.send(chat_id, text)
            return
        if verb == "retry_delivery":
            retried = self.store.retry_delivery(task_id)
            text = (
                f"🟢 #{task_id} delivery queued for {retried.approved_sha}."
                if retried is not None
                else f"Task #{task_id} has no recoverable approved delivery."
            )
            self.send(chat_id, text)
            return
        self.store.update(task_id, status=Status.CLOSED)
        self.send(chat_id, f"🗄 #{task_id} closed.")

    # --- taking work in --------------------------------------------------

    def _accept(
        self, chat_id: int, sender: dict[str, Any], text: str, message: dict[str, Any]
    ) -> None:
        body = text
        for prefix in ("/todo", "/task", "/fix"):
            if body.lower().startswith(prefix):
                body = body[len(prefix) :].strip()
        task = self.store.add(
            body,
            source=self.name,
            origin={"chat_id": str(chat_id), "user_id": str(sender.get("id", ""))},
            author=str(sender.get("username") or sender.get("first_name") or ""),
        )
        saved = self._save_attachment(task, message)
        log.info("telegram.accepted", task=task.id, file=bool(saved))
        line = escape(_one_line(task.text, ROW_CHARS)) or "(no text)"
        lost = self._attachment(message) is not None and saved is None
        note = "\n(the attachment did not download)" if lost else ""
        self.send(chat_id, f"🕓 <b>#{task.id}</b> queued\n{line}{note}")

    def _attachment(self, message: dict[str, Any]) -> tuple[str, str] | None:
        """``(file_id, suffix)`` for the photo or document on this message."""
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            largest = photos[-1]
            if isinstance(largest, dict) and int(largest.get("file_size") or 0) <= MAX_FILE_BYTES:
                return str(largest.get("file_id")), ".jpg"
        document = message.get("document")
        if isinstance(document, dict) and int(document.get("file_size") or 0) <= MAX_FILE_BYTES:
            name = str(document.get("file_name") or "")
            suffix = Path(name).suffix or ".bin"
            return str(document.get("file_id")), suffix
        return None

    def _save_attachment(self, task: Task, message: dict[str, Any]) -> Path | None:
        """Download it, or lose the screenshot but keep the task."""
        found = self._attachment(message)
        if found is None:
            return None
        file_id, suffix = found
        described = self.api("getFile", file_id=file_id)
        remote = str(described.get("file_path") or "") if isinstance(described, dict) else ""
        if not remote:
            return None
        destination = self.store.media_path(task.id, suffix)
        try:
            with urllib.request.urlopen(
                f"{API}/file/bot{self.token}/{remote}", timeout=60
            ) as response:
                destination.write_bytes(response.read())
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log.warn("telegram.download_failed", task=task.id, error=log.clip(exc, 120))
            return None
        self.store.update(task.id, file=str(destination))
        task.file = str(destination)
        return destination

    # --- reporting -------------------------------------------------------

    def report(self, task: Task, event: str, text: str) -> None:
        template = EVENT_TEXT.get(event)
        if template is None:
            return
        body = template.format(
            id=task.id,
            text=escape(_one_line(task.text, ROW_CHARS)) or "—",
            note=escape(log.clip(text, 900)) or "—",
        )
        if task.url:
            body += f"\n\n{task.url}"
        if task.branch and event in ("failed", "blocked"):
            body += f"\n\n<code>{escape(task.branch)}</code>"
        if task.cost_usd and event in ("done", "failed", "blocked"):
            body += f"\n💸 ${task.cost_usd:.2f}"
        for chat_id in self._recipients(task):
            self.send(chat_id, body)
        if event in {"done", "failed", "blocked", "cancelled"}:
            with self._live_lock:
                self._live.pop(task.id, None)

    def stream(self, task: Task, event: StreamEvent) -> None:
        """Keep one live status message per recipient, updated in place."""
        with self._live_lock:
            if event.kind == "reset":
                self._live.pop(task.id, None)
            state = self._live.setdefault(
                task.id,
                {"stage": "", "roles": {}, "messages": {}},
            )
            if event.role == "runner" and event.kind == "status":
                state["stage"] = event.text
            elif event.role != "runner":
                role = state["roles"].setdefault(event.role, {"status": "", "text": ""})
                if event.kind == "status":
                    role["status"] = event.text
                elif event.kind == "text":
                    current = str(role["text"])
                    role["text"] = (
                        event.text if event.text.startswith(current) else current + event.text
                    )[-1200:]
                elif event.kind in {"result", "error"}:
                    role["status"] = "complete" if event.kind == "result" else "failed"
                    role["text"] = event.text[-1200:]
            if event.kind == "text":
                return
            body = self._render_live(task, state)
            messages = state["messages"]
            for chat_id in self._recipients(task):
                message_id = messages.get(chat_id)
                if message_id is None:
                    sent = self.send(chat_id, body)
                    if sent is not None:
                        messages[chat_id] = sent
                else:
                    self.edit(chat_id, message_id, body)

    @staticmethod
    def _render_live(task: Task, state: dict[str, Any]) -> str:
        lines = [f"⚙️ <b>#{task.id} live</b> · {escape(str(state.get('stage') or 'working'))}"]
        for name, role in state["roles"].items():
            lines.append(f"\n<b>{escape(name)}</b> · {escape(str(role['status'] or 'live'))}")
            if role["text"]:
                lines.append(f"<pre>{escape(log.clip(str(role['text']), 1200))}</pre>")
        return "\n".join(lines)

    def _recipients(self, task: Task) -> list[int]:
        """Whoever asked, or every admin when the task came from elsewhere."""
        origin = task.origin.get("chat_id")
        if task.source == self.name and origin and origin.lstrip("-").isdigit():
            return [int(origin)]
        return sorted(self.admins)

    # --- rendering -------------------------------------------------------

    def _render_list(self) -> str:
        tasks = self.store.load()
        if not tasks:
            return "Nothing queued. Send me something."
        order = sorted(tasks, key=lambda task: (task.is_open is False, -task.id))
        shown = order[:MAX_ROWS]
        lines = [f"<b>Queue</b> — {sum(1 for task in tasks if task.is_open)} open"]
        for task in shown:
            mark = "📎" if task.file else ""
            row = escape(_one_line(task.text, ROW_CHARS)) or "—"
            lines.append(f"{task.icon} <b>#{task.id}</b> {row} {mark}")
            if task.note:
                lines.append(f"    <i>{escape(_one_line(task.note, ROW_CHARS))}</i>")
        if len(order) > len(shown):
            lines.append(f"…and {len(order) - len(shown)} more")
        return "\n".join(lines)

    def _render_card(self, task: Task) -> str:
        lines = [
            f"{task.icon} <b>#{task.id}</b> — {escape(task.status)}",
            self._escaped_clip(task.text, 700) or "—",
        ]
        if task.file:
            lines.append(f"📎 <code>{self._escaped_clip(task.file, 220)}</code>")
        if task.branch:
            lines.append(f"🌿 <code>{self._escaped_clip(task.branch, 180)}</code>")
        if task.attempts:
            lines.append(f"attempts: {task.attempts}")
        if task.cost_usd:
            lines.append(f"💸 ${task.cost_usd:.2f}")
        if task.url:
            lines.append(self._escaped_clip(task.url, 250))
        if task.note:
            lines.append(f"<i>{self._escaped_clip(task.note, 450)}</i>")
        messages = self.store.messages(task.id, limit=2**31 - 1)
        pending = sum(message.status == "pending" for message in messages)
        lines.append(f"\n<b>Thread</b> — {len(messages)} messages · {pending} pending")
        for message in messages[-RECENT_MESSAGES:]:
            lines.extend(self._render_thread_message(message))
        return "\n".join(lines)

    @classmethod
    def _render_thread_message(cls, message: TaskMessage) -> list[str]:
        author = message.author or message.role
        detail = message.mode
        if message.status not in {"stored", "answered"}:
            detail += f" · {message.status}"
        icon = "🤖" if message.role == "assistant" else "💬"
        return [
            f"{icon} <b>{cls._escaped_clip(author, 90)}</b> · {escape(detail)}",
            cls._escaped_clip(message.text, 350) or "—",
        ]

    @staticmethod
    def _escaped_clip(value: object, limit: int) -> str:
        """Escape user text while keeping the rendered Telegram HTML valid."""
        text = str(value)
        escaped: list[str] = []
        used = 0
        for character in text:
            encoded = escape(character)
            if used + len(encoded) > limit - 1:
                escaped.append("…")
                break
            escaped.append(encoded)
            used += len(encoded)
        return "".join(escaped)

    # --- state -----------------------------------------------------------

    def _load_state(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, dict):
            return
        self.offset = int(raw.get("offset") or 0)
        stored = raw.get("admins")
        if isinstance(stored, list) and not self.admins:
            self.admins = {int(item) for item in stored if isinstance(item, int | str)}

    def _save_state(self) -> None:
        payload = {"offset": self.offset, "admins": sorted(self.admins)}
        write_atomic(self.state_path, json.dumps(payload))


__all__ = [
    "API",
    "HELP",
    "MAX_FILE_BYTES",
    "MAX_ROWS",
    "POLL_TIMEOUT",
    "RECENT_MESSAGES",
    "TelegramFront",
]
