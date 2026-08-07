from __future__ import annotations

from agentq.streaming import normalize_agent_event


def test_claude_text_and_tool_events_are_normalized() -> None:
    updates = normalize_agent_event(
        "claude",
        "fixer",
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Inspecting"},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "src/app.py"},
                    },
                ]
            },
        },
    )
    assert [(event.kind, event.text) for event in updates] == [
        ("text", "Inspecting"),
        ("tool", "Read · src/app.py"),
    ]


def test_codex_and_opencode_events_are_normalized() -> None:
    codex = normalize_agent_event(
        "codex",
        "reviewer",
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Looks good"}},
    )
    opencode = normalize_agent_event(
        "opencode", "fixer", {"type": "text", "part": {"text": "Changing the file"}}
    )
    assert [(event.kind, event.text) for event in codex] == [("text", "Looks good")]
    assert [(event.kind, event.text) for event in opencode] == [
        ("text", "Changing the file")
    ]


def test_external_delta_and_errors_are_normalized() -> None:
    delta = normalize_agent_event(
        "external", "fixer", {"type": "text_delta", "text": "partial"}
    )
    failed = normalize_agent_event(
        "external", "fixer", {"type": "error", "error": {"message": "offline"}}
    )
    assert [(event.kind, event.text) for event in delta] == [("text", "partial")]
    assert [(event.kind, event.text) for event in failed] == [("error", "offline")]
