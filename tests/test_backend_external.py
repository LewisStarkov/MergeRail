from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from mergerail.backends.base import SessionSpec, TurnRequest
from mergerail.backends.external import ExternalBackend, ProtocolError

DRIVER = r"""
import json
import sys

for raw in sys.stdin:
    frame = json.loads(raw)
    kind = frame["type"]
    if kind == "hello":
        print(json.dumps({
            "type": "hello",
            "protocol": 1,
            "backend": "fake",
            "capabilities": {
                "conversations": True,
                "native_resume": True,
                "streaming": True,
                "structured_output": True,
                "usage_reporting": True,
                "context_reporting": True,
                "exact_cost_reporting": True,
                "native_budget_limit": True,
                "native_read_only": True,
                "native_push_denial": True,
                "attachments": False,
            },
        }), flush=True)
    elif kind == "open_session":
        print(json.dumps({
            "type": "session_opened",
            "request_id": frame["request_id"],
            "session_id": "external-1",
        }), flush=True)
    elif kind == "turn":
        print(json.dumps({
            "type": "text_delta",
            "request_id": frame["request_id"],
            "text": "working",
        }), flush=True)
        print(json.dumps({
            "type": "result",
            "request_id": frame["request_id"],
            "session_id": "external-1",
            "text": "done",
            "is_error": False,
            "structured": {"ok": True},
            "cost_usd": 0.25,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }), flush=True)
    elif kind == "close_session":
        break
"""


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[Mapping[str, Any]] = []

    def emit(self, event: Mapping[str, Any]) -> None:
        self.events.append(event)


def test_external_driver_handshake_turn_streaming_and_close(repo: Path) -> None:
    sink = RecordingSink()
    backend = ExternalBackend("fake", (sys.executable, "-u", "-c", DRIVER), timeout=2)
    session = backend.open_session(SessionSpec("fixer", repo, timeout=2), sink)

    assert session.capabilities.structured_output
    assert session.capabilities.native_push_denial
    reply = session.ask(TurnRequest("work", schema={"type": "object"}, max_cost_usd=1.0))
    assert reply.text == "done"
    assert reply.structured == {"ok": True}
    assert reply.cost_usd == 0.25
    assert reply.context_tokens == 10
    assert reply.session_id == "external-1"
    assert not reply.is_error
    assert [event["type"] for event in sink.events] == ["text_delta"]
    session.close()


MALFORMED_TURN_DRIVER = DRIVER.replace(
    'print(json.dumps({\n            "type": "text_delta",',
    'print("not-json", flush=True)\n        print(json.dumps({\n            "type": "text_delta",',
)


def test_external_driver_malformed_turn_is_a_failed_reply(repo: Path) -> None:
    backend = ExternalBackend(
        "malformed", (sys.executable, "-u", "-c", MALFORMED_TURN_DRIVER), timeout=2
    )
    session = backend.open_session(SessionSpec("fixer", repo, timeout=2))
    reply = session.ask(TurnRequest("work"))
    assert reply.is_error
    assert "malformed JSON" in reply.text


def test_external_turn_timeout_is_a_failed_reply(repo: Path) -> None:
    backend = ExternalBackend("timeout", (sys.executable, "-u", "-c", DRIVER), timeout=2)
    session = backend.open_session(SessionSpec("fixer", repo, timeout=0))
    reply = session.ask(TurnRequest("work"))
    assert reply.is_error
    assert "timed out" in reply.text


def test_external_driver_rejects_a_protocol_mismatch(repo: Path) -> None:
    mismatch = DRIVER.replace('"protocol": 1', '"protocol": 2', 1)
    backend = ExternalBackend("mismatch", (sys.executable, "-u", "-c", mismatch), timeout=2)
    with pytest.raises(ProtocolError, match="protocol mismatch"):
        backend.open_session(SessionSpec("fixer", repo, timeout=2))


def test_external_probe_does_not_execute_the_driver(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    command = (
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({json.dumps(str(marker))}).touch()",
    )
    info = ExternalBackend("explicit", command).probe()
    assert info.available
    assert not marker.exists()
