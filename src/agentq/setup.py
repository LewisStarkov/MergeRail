"""Validation and application of interactive project setup answers."""

from __future__ import annotations

import copy
from collections.abc import Mapping

from .config import Config

ENVIRONMENTS = ("local", "staging", "production")
WORK_MODES = ("development", "maintenance", "incident")
EXTERNAL_ACTIONS = ("forbid", "ask")


def configured_copy(config: Config, payload: Mapping[str, object]) -> Config:
    """Return a validated copy without exposing half-applied live settings."""
    candidate = copy.deepcopy(config)
    shared = _text(payload, "agent", candidate.fixer.backend)
    candidate.fixer.backend = _text(payload, "fixer_agent", shared)
    candidate.reviewer.backend = _text(payload, "reviewer_agent", shared)
    candidate.project.environment = _choice(
        payload, "environment", candidate.project.environment, ENVIRONMENTS
    )
    candidate.project.work_mode = _choice(
        payload, "work_mode", candidate.project.work_mode, WORK_MODES
    )
    candidate.project.summary = _optional_text(
        payload, "summary", candidate.project.summary
    )
    candidate.project.external_actions = _choice(
        payload,
        "external_actions",
        candidate.project.external_actions,
        EXTERNAL_ACTIONS,
    )
    constraints = payload.get("constraints", candidate.project.constraints)
    if isinstance(constraints, str):
        candidate.project.constraints = [
            item for part in constraints.split(";") if (item := part.strip())
        ]
    elif isinstance(constraints, list):
        candidate.project.constraints = [
            text for item in constraints if (text := str(item).strip())
        ]
    else:
        raise ValueError("constraints must be a list or semicolon-separated text")
    return candidate


def _text(payload: Mapping[str, object], key: str, default: str) -> str:
    value = payload.get(key)
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        raise ValueError(f"{key} must be text")
    return str(value).strip() or default


def _optional_text(payload: Mapping[str, object], key: str, default: str) -> str:
    if key not in payload:
        return default
    value = payload[key]
    if isinstance(value, (dict, list)):
        raise ValueError(f"{key} must be text")
    return str(value).strip()


def _choice(
    payload: Mapping[str, object], key: str, default: str, choices: tuple[str, ...]
) -> str:
    value = _text(payload, key, default)
    if value not in choices:
        raise ValueError(f"{key} must be one of: {', '.join(choices)}")
    return value


__all__ = ["ENVIRONMENTS", "EXTERNAL_ACTIONS", "WORK_MODES", "configured_copy"]
