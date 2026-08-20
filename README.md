<h1 align="center">
  <img src="docs/assets/mergerail-logo.svg" alt="MergeRail" width="520">
</h1>

MergeRail is a local, provider-neutral queue for coding agents. A fixer handles
the task, repository checks run, a separately configured reviewer accepts or
rejects the result, and MergeRail delivers the approved commit by local merge or
pull request.

Built-in backends support Claude Code, Codex and OpenCode. The fixer and
reviewer may use different tools, and other headless agents can connect through
the `mergerail-jsonl-v1` external-driver protocol.

```bash
cd your-project
uv tool install mergerail
mergerail init
mergerail run --web
```

MergeRail checks the repository for a newer stable `vX.Y.Z` tag before `run` and
`once`, at most once every 24 hours. It installs updates with `uv tool` and
restarts before taking ownership of the queue. A long-running local
`mergerail run --web` also probes while idle: pending delivery finishes first,
no new task is claimed, and installation starts only after the Web front,
configured process, agent sessions, and runner lease have shut down. The updated
release is pinned to the exact remote Git object observed by the probe, installed
once across concurrent runners, and then replaces the current process (or uses a
clean child handoff on Windows). If installation fails, the current service starts
again and waits for the normal check interval. If activation fails after a
successful install, MergeRail exits nonzero instead of resuming stale code; restart
it manually to use the installed release. Runtime replacement is not enabled for
`once`, programmatic CLI calls, custom fronts, non-local or unsafe Web instances,
shared Web servers, or `--no-update` runs.

Use `mergerail update --check` to check manually, `mergerail update` to install
immediately, or `--no-update` / `MERGERAIL_AUTO_UPDATE=0` to disable the automatic
check. A fork can set `MERGERAIL_UPDATE_REPOSITORY` to its Git URL.

## Let your coding agent start MergeRail

Paste this prompt into Codex, Claude Code, OpenCode, or another coding agent
that can use a terminal:

```text
Set up and start MergeRail for the Git repository in the current working directory.
MergeRail repository: https://github.com/LewisStarkov/MergeRail

Preserve every existing file, uncommitted change, branch, mergerail.toml setting,
and .mergerail/ state. Do not reset, clean, checkout, commit, or push anything.

1. Confirm that the current directory is a Git repository. Inspect its README
   and package manifests to infer a short project summary.
2. Confirm that `uv` is available. If it is, install MergeRail with
   `uv tool install mergerail`, or run `mergerail update` when MergeRail is already
   installed. If `uv` is unavailable, use its official installation method only
   when the environment permits it; otherwise report the exact blocker.
3. If mergerail.toml does not exist, run:
   `mergerail init --agent auto --environment local --work-mode development
   --project-summary "<inferred summary>" --external-actions forbid
   --non-interactive`
   Do not replace an existing mergerail.toml.
4. Run `mergerail doctor` and safely resolve any local setup issue that does not
   require credentials or a user decision.
5. Start `mergerail run --web` in a persistent terminal session. Do not start a
   duplicate runner when MergeRail already owns the repository. Wait until the Web
   UI is listening, report its local URL, and keep the process running.

Keep the Web UI local. Do not use --share, --share-unsafe, or --unsafe-expose
unless I explicitly request public access. If permissions, authentication, or a
material configuration choice blocks startup, ask one concise question and
include the command output that caused the block.
```

In a terminal, `init` asks for one AI agent, the target environment, the current
objective and whether external actions require approval. It saves those answers
in `mergerail.toml`; both fixer and reviewer receive them as standing context. For
automation, the same setup is available without prompts:

```bash
mergerail init --agent codex --environment local --work-mode development \
  --project-summary "build the customer API" --non-interactive
```

`--agent` is the readable alias for `--backend` and selects both roles;
`--fixer-agent` and `--reviewer-agent` select them separately. These flags also
work with `run` and `once`. `doctor` explains what would run. `--telegram`
starts the Telegram front; the default folder front reads `.mergerail/inbox/` and
writes `.mergerail/outbox/`. If a task still lacks information that would change
the implementation or risk external state, the fixer returns only the blocking
questions instead of guessing.

The same initialization is available while MergeRail is running. Open **Project
setup** in the Web sidebar, or send `/init` to the Telegram bot. Both interfaces
show validation, config write and live activation progress; no runner restart is
needed. In Telegram, live fixer/reviewer output is updated in one status message
instead of producing a message for every event. Send `/cancel` to leave the bot
wizard without changing the configuration.

Every task is also a durable discussion thread. In Web, select a task and use
**Comment** to store context without running an agent, or **Send to agent** to
queue a follow-up. Telegram exposes the same actions as `/comment ID TEXT` and
`/message ID TEXT`. Follow-ups reopen terminal tasks under the same task id,
retain prior run results, and resume that task's backend session when possible;
after a restart the on-disk thread remains the fallback source of context.

To share the web front temporarily, install and authenticate ngrok, then let
MergeRail own the tunnel:

```bash
mergerail run --share ngrok
```

The command prints the public HTTPS URL and one-session credentials for MergeRail's
login page, then stops ngrok when MergeRail exits. A custom ngrok Traffic Policy can
replace the built-in login with `--share-policy path/to/policy.yml`. Publishing
without authentication requires the explicit `--share-unsafe` flag.

## How a task runs

1. The fixer works on a fresh attempt branch in a reusable Git worktree,
   separate from the main checkout.
2. `mergerail` runs the configured checks. Checks already failing on the clean
   base can be excluded from the session by the baseline pass.
3. The reviewer receives the diff and check results. A rejection returns to the
   fixer, using the same backend session when that backend supports it.
4. Approval records the exact branch and commit before delivery starts.
5. For local delivery, `mergerail` builds the combined tree in a temporary
   integration worktree and reruns the active checks before moving the base.
6. `mergerail` performs the selected delivery operation and records each stage.

Only one runner may own a repository at a time. Its OS-level lease contains the
run id, process id, host and start time. After an unclean stop, the next runner
requeues tasks left in fixer/review states while preserving their previous
attempt branches for diagnosis.

Messages received while the fixer is working are applied before checks. A
message received during review invalidates that verdict and returns the task to
the fixer. Approved delivery is immutable: messages arriving during delivery
wait for the next attempt instead of changing the reviewed commit.

The reviewer uses the same agent worktree with a read-only policy requested
from its backend. The strength of that policy and of push/network restrictions
depends on the selected CLI; `mergerail` is not an absolute sandbox.

## Backends

`mergerail backends` shows the installed built-ins and their versions. In automatic
selection, each role takes the first available entry from `backend_order`.

```toml
[agents]
backend_order = ["codex", "claude", "opencode"]

[agents.fixer]
backend = "codex"       # auto | claude | codex | opencode | external name
model = ""
effort = "high"
permission = "safe"
timeout = 3600

[agents.reviewer]
backend = "claude"
model = ""
effort = "medium"
permission = "review"
timeout = 1800
```

For a trusted local fixer that must inspect Docker or host processes, set
`permission = "skip"`. With the Codex and Claude backends this disables their
sandbox and approval checks; reviewers remain read-only.

An external driver is registered explicitly; its executable must implement
`mergerail-jsonl-v1` over stdin/stdout JSONL:

```toml
[backends.gemini]
protocol = "mergerail-jsonl-v1"
command = ["mergerail-gemini-driver"]

[agents.fixer]
backend = "gemini"
```

Unknown usage and cost remain unknown. If `max_usd` is configured, preflight
refuses a backend that cannot report exact USD cost rather than pretending to
enforce the budget. External drivers are conservatively treated as unknown at
preflight in v0.1, because probing never executes their command.

## Delivery

Delivery is deterministic. The strategy is resolved once and never silently
falls back to another one:

- `auto` with an `origin` remote selects `pr`. If `gh` is unavailable, delivery
  blocks at preflight; it does not merge locally instead.
- `auto` without `origin` selects `local`.
- `local` merges into the checked-out base branch. It does **not** push the base
  branch or update any remote.
- `pr` pushes the attempt branch and creates or recovers its pull request.
- `merge` is accepted as a deprecated alias for `local`.

A dirty main checkout, wrong checked-out base, merge conflict, failed push or
failed PR creation leaves the approved branch and commit intact. A blocked
delivery records its stage and errors and can be resumed after a restart.

```bash
mergerail show 42              # branch, approved commit and delivery stage
mergerail retry-delivery 42    # retry merge/push/PR only; no fixer or review
mergerail retry-task 42        # run fixer and reviewer again on a new attempt branch
mergerail cancel 42            # cancel queued work or stop its current process tree
mergerail archive --keep 100   # move older terminal tasks to tasks.archive.jsonl
```

Existing failed or blocked attempt branches are not reset by `retry-task`;
task retry and delivery retry are intentionally different operations.

## Configuration

```toml
base_branch = "main"
delivery = "auto"           # auto | local | pr
max_rounds = 3
baseline_checks = true
baseline_mode = "exclude"      # exclude | compare | strict
max_usd = 0.0               # 0 disables the task-wide USD limit
strict_security = false      # require native reviewer isolation and push denial
process = ""                # optional app process to restart after local merge

[project]
environment = "local"       # local | staging | production
work_mode = "development"   # development | maintenance | incident
summary = "build the customer API"
external_actions = "forbid" # forbid | ask before changing external systems
constraints = ["do not use production data"]

[agents]
backend_order = ["claude", "codex", "opencode"]

[agents.fixer]
backend = "auto"
permission = "safe"

[agents.reviewer]
backend = "auto"
permission = "review"

[[checks]]
name = "test"
command = "uv run pytest"

[telegram]
admins = []

[web]
host = "127.0.0.1"
port = 8788
username = ""              # set together with MERGERAIL_WEB_PASSWORD
session_ttl = 28800
max_sse_clients = 16
```

Scalar settings can also be supplied as `MERGERAIL_<KEY>`. Role settings use
`MERGERAIL_FIXER_<KEY>` and `MERGERAIL_REVIEWER_<KEY>`. Keep credentials in the
environment rather than in `mergerail.toml`. Project context can be overridden by
`MERGERAIL_PROJECT_ENVIRONMENT`, `MERGERAIL_PROJECT_WORK_MODE`,
`MERGERAIL_PROJECT_SUMMARY`, and `MERGERAIL_PROJECT_EXTERNAL_ACTIONS`.

Useful commands:

```bash
mergerail doctor --run-checks
mergerail add "the header overlaps on mobile"
mergerail list
mergerail show 1
mergerail cancel 1
mergerail events --task 1
mergerail archive --keep 100
mergerail once "bump the copyright year" --delivery pr
mergerail backends
```

`once` is the scripting interface: it queues one task, works until it settles,
and exits successfully only when the task reaches `done`.

`baseline_mode = "exclude"` skips checks already failing on the clean base.
`compare` continues to run them and labels the existing failures without failing
the round; a newly failing check still blocks review. `strict` refuses to start
until every configured check passes on the base.

Every runner writes a versioned append-only audit journal to
`.mergerail/events.jsonl`. It rotates by size while retaining recent backups;
`mergerail events` reads across them and accepts `--task`, `--limit`, and `--json`
for filtering or machine-readable export. The hot task queue fails closed on
malformed JSON and preserves a timestamped corrupt copy instead of overwriting
it. `mergerail doctor` validates both the queue and runner lease location.

## Fronts and embedding

Folder, Telegram and localhost Web fronts ship with the package. Web uses
server-sent events to show fixer/reviewer output and tool activity as it
arrives, with polling only as a reconnect fallback. Its self-hosted operations
workspace uses a compact queue ledger, chronological task activity, search and
status views, setup and command drawers, keyboard navigation, saved density and
light/dark preferences, saved queue-panel state, and visible feedback for failed
actions. The compiled browser assets ship inside the Python package and make no
CDN or third-party runtime requests. Non-local binding is refused unless
username/password authentication is configured; the explicit `--unsafe-expose`
override is intended only for an already protected network. Login attempts,
sessions, page sizes and simultaneous event streams are bounded. A custom front
implements two required methods; `stream` is optional:

```python
class Front:
    def next_task(self) -> Task | None: ...
    def report(self, task: Task, event: str, text: str) -> None: ...
    def stream(self, task: Task, event: StreamEvent) -> None: ...
```

Start it with `mergerail run --front mypackage.fronts:SlackFront`, or embed the
runner:

```python
from mergerail import Runner

Runner(front="telegram").run()
```

## Requirements

- Python 3.11 or newer and Git.
- At least one installed and authenticated agent CLI: Claude Code, Codex or
  OpenCode; alternatively, an explicitly configured external driver.
- The authenticated `gh` CLI when delivery can resolve to `pr`.
- The authenticated `ngrok` CLI only when using `--share ngrok`.
- Linux, macOS or Windows.

The Python package has no runtime dependencies. Backend authentication and
model access are managed by the selected agent CLI.

## Limits

`mergerail` does not resolve merge conflicts, force-merge approved work, or invent
a reviewer verdict. Provider sandbox, permission and network guarantees vary;
check the selected CLI's guarantees and do not treat the shared review worktree
as a security boundary.

Operational recovery and release checks are documented in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).
