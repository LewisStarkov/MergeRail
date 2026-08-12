# Agent backends

`agentq` separates orchestration from the command-line agent that performs a turn. The
fixer and reviewer may use different backends, while the runner sees the same
`SessionSpec`, `TurnRequest`, streaming events, and `AgentReply` from each one.

## Selecting backends

The built-in registry contains `claude`, `codex`, and `opencode`. A role set to `auto`
uses the first available entry in `agents.backend_order`; once a project has chosen a
backend, setting it explicitly keeps later runs predictable.

The CLI accepts either backend-oriented or user-facing names. `--agent codex`
is an alias for `--backend codex`; `--fixer-agent` and `--reviewer-agent` map to
their corresponding backend flags. `agentq init` asks for these selections and
persists them, while `run` and `once` flags override the file for one process.

```toml
[agents]
backend_order = ["claude", "codex", "opencode"]

[agents.fixer]
backend = "codex"
model = ""
effort = "high"
permission = "safe"

[agents.reviewer]
backend = "claude"
model = "sonnet"
permission = "review"
```

The neutral permissions are `safe` for a fixer and `review` for a reviewer. Each backend
maps them to its own CLI controls. The reviewer session is also opened with
`read_only=true`; a backend capability describes whether that restriction is enforced by
the tool itself.

### Built-in adapters

| Backend | Invocation | Resume | Structured output | Usage and cost | Isolation notes |
| --- | --- | --- | --- | --- | --- |
| Claude Code | `claude -p --output-format stream-json` | `--resume` | `--json-schema` when supported by the installed CLI | Reports tokens, context, and exact USD cost; accepts a native per-turn budget | Reviewer write tools and `git push` are denied through Claude tool permissions |
| Codex | `codex exec --json` | `codex exec ... resume` | `--output-schema` | Reports token usage and context, but not exact USD cost | Reviewer uses the native read-only sandbox; no native `git push` denial is claimed |
| OpenCode | `opencode run --format json` | `--session` | Text verdict protocol; schema enforcement is not claimed | Reports usage and exact cost when present | Uses `OPENCODE_PERMISSION` for reviewer read-only and push denial without changing `opencode.json` |

The adapters do not invent missing telemetry. In particular, Codex replies have
`cost_usd = None`, not zero. A configured `max_usd` budget fails preflight when either
selected backend cannot report exact cost. `native_budget_limit` is a separate
capability: exact accounting lets the runner stop between turns, while a native limit
can stop a single turn before it exceeds the remaining budget.

## Capability model

`BackendCapabilities` records guarantees provided by a backend. It is intentionally more
precise than a single "supported" flag.

| Capability | Meaning |
| --- | --- |
| `conversations` | A logical conversation can span turns |
| `native_resume` | The underlying tool resumes a provider session by id |
| `streaming` | Events are available before the final reply |
| `structured_output` | The tool enforces a supplied JSON schema |
| `usage_reporting` | Token usage is reported |
| `context_reporting` | Enough usage data is reported to measure current context |
| `exact_cost_reporting` | The reported USD cost is exact enough for budget accounting |
| `native_budget_limit` | The tool enforces the requested per-turn cost ceiling |
| `native_read_only` | Reviewer read-only behavior is enforced outside the prompt |
| `native_push_denial` | The backend prevents agent-initiated pushes outside the prompt |
| `attachments` | Non-text turn attachments are accepted |

Capabilities are negotiated per opened session. Consumers must degrade a feature or
stop when its guarantee is absent; they must not replace an unknown value with a
successful-looking default. In v0.1, external commands are not launched during
preflight, so their probe is deliberately conservative and `max_usd` cannot be combined
with an external backend.

With `strict_security = true` (or `agentq run --strict-security`), preflight requires
native push denial for both roles and native read-only enforcement for the reviewer.
This deliberately rejects a backend whose probe cannot establish those guarantees.
Agent sessions belong to one task. The runner closes in-memory handles at task
boundaries; when `native_resume` is available, it persists the provider session id and
restores it only for that same task. Otherwise, the durable task journal reconstructs
the discussion context. Session ids are never shared between tasks.

## Trusted external driver protocol

An external driver connects another headless agent to `agentq-jsonl-v1`. Configure its
command explicitly; external executables are never discovered or launched by probing.

```toml
[backends.gemini]
protocol = "agentq-jsonl-v1"
command = ["agentq-gemini-driver"]

[agents.fixer]
backend = "gemini"
```

The driver is one long-lived process per role. It reads one JSON object per line from
stdin and writes one JSON object per line to stdout. Human-readable logs belong on
stderr; any non-JSON stdout line is a protocol error. Frames use UTF-8 and are limited to
4 MiB by default.

### Handshake

Immediately after starting the process, `agentq` sends:

```json
{"type":"hello","protocol":1,"agentq_version":"0.1.0"}
```

The driver must answer before the handshake timeout:

```json
{
  "type": "hello",
  "protocol": 1,
  "backend": "gemini",
  "capabilities": {
    "conversations": true,
    "native_resume": true,
    "streaming": true,
    "structured_output": false,
    "usage_reporting": true,
    "context_reporting": true,
    "exact_cost_reporting": false,
    "native_budget_limit": false,
    "native_read_only": true,
    "native_push_denial": true,
    "attachments": false
  }
}
```

The protocol number must be exactly `1`. Capability values, when present, must be JSON
booleans; omitted capabilities are false.

### Open a session

After the handshake, `agentq` sends an `open_session` request:

```json
{
  "type": "open_session",
  "request_id": "1",
  "role": "fixer",
  "cwd": "/absolute/path/to/worktree",
  "policy": "workspace-write",
  "system_prompt": "standing role instructions",
  "model": null,
  "effort": "",
  "settings": {}
}
```

For a reviewer, `policy` is `read-only`. The driver returns a correlated session id:

```json
{"type":"session_opened","request_id":"1","session_id":"driver-session-42"}
```

Terminal responses without the matching `request_id` are rejected. The session id is
included in subsequent turns; the driver decides how it maps that id to its underlying
tool.

### Run a turn

```json
{
  "type": "turn",
  "request_id": "2",
  "session_id": "driver-session-42",
  "prompt": "Implement the requested change.",
  "schema": null,
  "max_cost_usd": 1.25
}
```

`schema` may be null. `max_cost_usd` is omitted when no budget is configured. A driver
must advertise `structured_output=true` only if it enforces the schema, and
`native_budget_limit=true` only if it enforces this turn's ceiling.

Before the terminal response, the driver may emit correlated streaming events. Their
payload is backend-defined and is forwarded to the event sink:

```json
{"type":"text_delta","request_id":"2","text":"Inspecting the repository"}
```

A successful terminal response is:

```json
{
  "type": "result",
  "request_id": "2",
  "session_id": "driver-session-42",
  "text": "Implemented and tested the change.",
  "is_error": false,
  "structured": null,
  "cost_usd": 0.18,
  "context_tokens": 12000,
  "usage": {
    "input_tokens": 10000,
    "output_tokens": 900,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 2000
  }
}
```

All telemetry fields are optional. Omit `cost_usd` when cost is unknown; sending `0`
means the exact cost was zero. If `context_tokens` is absent, `agentq` derives it from
the input and cache token fields when possible. `structured` must be a JSON object when
present.

For a failed turn, either return `result` with `is_error=true` or an error frame:

```json
{
  "type": "error",
  "request_id": "2",
  "error": {"code": "provider_unavailable", "message": "Provider is unavailable"}
}
```

An error during session opening raises a protocol error. An error during a turn becomes
an unsuccessful `AgentReply`. When the session closes, `agentq` sends a best-effort
`close_session` frame containing `request_id` and `session_id`, then stops the process.

### Timeouts and protocol failures

- The external backend constructor's timeout covers `hello` and `open_session`; its
  default is 10 seconds.
- The selected role's session timeout covers each `turn`.
- A timeout, malformed JSON frame, oversized frame, closed stdout, or exited driver
  fails the current operation and stops the whole driver process tree.
- Shutdown first requests process-tree termination, then force-kills a process that does
  not exit within the grace period.
- Cancelling an active task uses the same process-tree shutdown path. Partial repository
  changes are committed on the attempt branch before the task becomes `cancelled`.
- Frames for other request ids may be forwarded as events, but terminal `result` and
  `error` frames must always carry the active request id.

## Security boundary

External drivers are trusted code, not sandboxed plugins. They inherit the `agentq`
process environment, receive an absolute worktree path, prompts, model settings, and the
declared policy, and may start their own child processes or access the network. Only
configure executables you would be willing to run directly in that repository.

Capability negotiation is a declaration, not remote attestation. A malicious or broken
driver can claim read-only or push denial while ignoring both. Drivers should enforce
reviewer isolation and push denial with their tool's native sandbox or permission system,
strip credentials they do not require, avoid loading unrelated user plugins, and keep all
delivery credentials outside agent subprocesses. `agentq` validates framing, correlation,
timeouts, and process cleanup; it cannot prove that an external driver obeyed its declared
security policy.
