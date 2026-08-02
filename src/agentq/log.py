"""Logging, on the standard library and nothing else.

Events read ``name | key=value key=value`` because the two people who will ever
read this log are grepping it: one for a task id, one for the word ``failed``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

logger = logging.getLogger("agentq")


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
    )
    logger.handlers[:] = [handler]
    logger.setLevel(level.upper())
    logger.propagate = False


def compose(event: str, fields: dict[str, Any]) -> str:
    tail = " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))
    return f"{event} | {tail}" if tail else event


def info(event: str, **fields: Any) -> None:
    logger.info(compose(event, fields))


def warn(event: str, **fields: Any) -> None:
    logger.warning(compose(event, fields))


def error(event: str, **fields: Any) -> None:
    logger.error(compose(event, fields))


def exception(event: str, **fields: Any) -> None:
    logger.exception(compose(event, fields))


def raw(text: str) -> None:
    """A line produced by something else — printed as it came."""
    logger.info(text)


def clip(text: Any, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


__all__ = ["clip", "compose", "error", "exception", "info", "logger", "raw", "setup", "warn"]
