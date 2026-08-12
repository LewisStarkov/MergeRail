# Operations

## Initial operating context

Run `agentq init` in an interactive terminal before the first task. The short
wizard records one agent for both roles, target environment, current objective
and external-action policy. Both agents receive the resulting `[project]`
section in their system prompt, so a
production incident is not treated like an ordinary local refactor.

For scripts and reproducible setup, pass the same answers as flags and suppress
questions explicitly:

```bash
agentq init --agent codex --reviewer-agent claude \
  --environment production --work-mode incident \
  --project-summary "restore payment processing" \
  --external-actions ask --constraint "preserve audit logs" \
  --non-interactive
```

Context is an instruction and does not expand agent permissions. In particular,
it never grants deployment, push, messaging or production-write authority.

The Web front exposes the same four fields under **Project setup** and streams
each setup stage over its existing SSE connection. The Telegram front provides a
four-step `/init` dialog; `/cancel` abandons it. A successful setup is written
atomically and activated for the next task without restarting the runner. Setup
is rejected while a task is active so its agents and operating context cannot
change midway through a run.

## Recovery and cancellation

AgentQ takes `.agentq/runner.lock` before baseline checks or front startup. A
second runner exits with the recorded owner metadata instead of competing for
tasks. If a runner dies, its operating-system lock is released automatically;
the next runner requeues tasks left in `running` or `review`, clears their old
claims, and retains the abandoned attempt branch in task history.

`agentq cancel ID` immediately cancels queued work. Active fixer, reviewer and
check processes receive process-tree termination followed by a forced kill if
they outlive the grace period. Partial work is committed to the attempt branch
before the task is marked `cancelled`. Delivery that has already been approved
must be allowed to finish or repaired with `retry-delivery`.

## Durable state

Queue updates are serialized across processes and replaced atomically. Invalid
JSON stops mutations and is copied to a timestamped `tasks.corrupt-*.json` file
for inspection. Fix the original queue or restore a known-good copy, then run
`agentq doctor` before restarting the runner.

Task discussions are append-only journals under `.agentq/threads/`. Message
status changes are appended as events and folded on read, so an interrupted
runner can return `processing` messages to `pending` without rewriting the
thread. Resumable backend session ids and previous execution snapshots live in
the task record; session ids are task-scoped and are discarded if a backend
rotates its context or rejects resume. The journal is then used to reconstruct
the request in a fresh session.

The audit journal rotates at 5 MiB and keeps three backups by default. Task
history can be compacted without losing terminal records:

```bash
agentq archive --keep 100
agentq events --limit 200
agentq doctor --run-checks
```

Archived tasks are appended to `.agentq/tasks.archive.jsonl`; the newest 100
terminal tasks remain in the hot queue in this example.

## Local delivery

Approved work is merged with the current base in `.agentq/integration`, where
the active check set runs against the combined tree. The checked-out base is
advanced only if those checks pass and the base remained clean and unchanged
during validation. A conflict, check failure or concurrently moved base leaves
the base untouched and preserves the approved commit for `retry-delivery`.

## Web exposure

The Web front binds to loopback by default. For a non-local bind, configure both
credentials and keep the password in the environment:

```toml
[web]
host = "0.0.0.0"
username = "agentq"
```

```bash
export AGENTQ_WEB_PASSWORD='use-a-secret-manager-in-production'
agentq run --web
```

`--unsafe-expose` bypasses this safeguard and should only be used behind an
independent authenticated proxy. For temporary sharing, `--share ngrok`
creates one-session credentials unless a custom Traffic Policy is supplied.

## Release gate

CI runs Ruff, strict mypy, the full test suite with branch coverage of at least
80%, on Linux, macOS and Windows. Tag releases repeat those checks, require the
tag `vX.Y.Z` to match the package version, build both distributions and run
`twine check` before trusted publishing to PyPI.
