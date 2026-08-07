"""The interfaces work arrives through, and how one gets chosen by name."""

from __future__ import annotations

import importlib
import os

from ..config import Config
from ..tasks import TaskStore
from .base import EVENTS, Front, StreamEvent
from .folder import FolderFront
from .telegram import TelegramFront
from .web import WebFront


def make_front(name: str, config: Config, store: TaskStore) -> Front:
    """Build a front by name, from the config file and the environment.

    Secrets come from the environment first: a token belongs in a shell profile
    or a launch agent, not in a file that gets committed by accident.

    A name with a colon in it — ``mypackage.fronts:SlackFront`` — is somebody
    else's front: imported, constructed as ``Class(store, config)``, plugged in.
    """
    if ":" in name:
        return _load_front(name, config, store)
    if name == "telegram":
        settings = config.front("telegram")
        token = (
            os.environ.get("AGENTQ_TELEGRAM_TOKEN", "").strip()
            or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            or str(settings.get("token") or "")
        )
        if not token:
            raise SystemExit(
                "agentq: no Telegram token. Set AGENTQ_TELEGRAM_TOKEN, or put "
                "token = \"...\" under [telegram] in agentq.toml."
            )
        listed = settings.get("admins")
        admins = {int(item) for item in listed} if isinstance(listed, list) else set()
        for raw in os.environ.get("AGENTQ_TELEGRAM_ADMINS", "").replace(",", " ").split():
            if raw.strip().isdigit():
                admins.add(int(raw))
        return TelegramFront(store, token, admins, config.state_dir)
    if name == "web":
        settings = config.front("web")
        host = os.environ.get("AGENTQ_WEB_HOST", "").strip() or str(
            settings.get("host") or "127.0.0.1"
        )
        port = int(os.environ.get("AGENTQ_WEB_PORT", "").strip() or settings.get("port") or 8788)
        unsafe_raw = os.environ.get("AGENTQ_WEB_UNSAFE_EXPOSE", "").strip()
        unsafe = _bool(unsafe_raw) if unsafe_raw else bool(settings.get("unsafe_expose", False))
        session_ttl = float(settings.get("session_ttl", 8 * 60 * 60))
        max_sse_clients = int(settings.get("max_sse_clients", 16))
        front = WebFront(
            store,
            host=host,
            port=port,
            unsafe_expose=unsafe,
            session_ttl=session_ttl,
            max_sse_clients=max_sse_clients,
        )
        username = os.environ.get("AGENTQ_WEB_USERNAME", "").strip() or str(
            settings.get("username") or ""
        )
        password = os.environ.get("AGENTQ_WEB_PASSWORD", "").strip() or str(
            settings.get("password") or ""
        )
        if bool(username) != bool(password):
            raise SystemExit("agentq: web authentication requires both username and password")
        if username and password:
            front.enable_auth(username, password)
        return front
    if name == "folder":
        return FolderFront(store, config.state_dir)
    raise SystemExit(
        f"agentq: unknown front '{name}' (known: telegram, web, folder, or pkg.mod:Class)"
    )


def _load_front(name: str, config: Config, store: TaskStore) -> Front:
    module_name, _, class_name = name.partition(":")
    try:
        cls = getattr(importlib.import_module(module_name), class_name)
    except (ImportError, AttributeError) as exc:
        raise SystemExit(f"agentq: cannot load front '{name}': {exc}") from exc
    front = cls(store, config)
    if not isinstance(front, Front):
        raise SystemExit(f"agentq: {name} is not a Front subclass")
    return front


def _bool(value: str) -> bool:
    return value.lower() not in {"0", "false", "no", "off"}


__all__ = [
    "EVENTS",
    "FolderFront",
    "Front",
    "StreamEvent",
    "TelegramFront",
    "WebFront",
    "make_front",
]
