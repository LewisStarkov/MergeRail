"""The interfaces work arrives through, and how one gets chosen by name."""

from __future__ import annotations

import os

from ..config import Config
from ..tasks import TaskStore
from .base import EVENTS, Front
from .folder import FolderFront
from .telegram import TelegramFront


def make_front(name: str, config: Config, store: TaskStore) -> Front:
    """Build a front by name, from the config file and the environment.

    Secrets come from the environment first: a token belongs in a shell profile
    or a launch agent, not in a file that gets committed by accident.
    """
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
    if name == "folder":
        return FolderFront(store, config.state_dir)
    raise SystemExit(f"agentq: unknown front '{name}' (known: telegram, folder)")


__all__ = ["EVENTS", "FolderFront", "Front", "TelegramFront", "make_front"]
