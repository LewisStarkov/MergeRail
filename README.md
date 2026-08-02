# agentq

Write the task down. An agent does it, an adversarial reviewer checks it, the
checks run, and it lands — merged, or as a pull request.

```bash
cd your-project
uvx agentq --telegram
```

That is the whole installation. It works out of what is already in the
repository: the base branch from the refs, the checks from your manifest, the
rules from your `CLAUDE.md`.

## What happens to a task

1. The shared worktree is reset onto a fresh branch cut from the base — the
   agents never touch the checkout your app runs from.
2. A **fixer** agent makes the change and commits it.
3. **agentq** runs your checks. Not the model: they are deterministic, so a
   model watching them scroll past is paying tokens for output a subprocess
   produces for free — and a failure goes straight back to the fixer without
   spending a review round.
4. A **reviewer** agent, in the same worktree but unable to edit anything, reads
   the diff *and* the check results and answers `APPROVE` or `REJECT`.
5. A rejection goes back to the fixer **in the same session**, up to
   `max_rounds` times.
6. An approval lands.

## Where it lands

`delivery = "auto"` (the default) tries to merge into the base branch and opens
a pull request when it can't. That means the repository decides: a protected
`main` refuses the merge, the change arrives as a PR, and nobody had to
configure a policy. Force it either way with `merge` or `pr`.

The PR carries the task as written, what changed, and the reviewer's own words.

## Interfaces

A front is two methods — take work in, report back:

```python
class Front:
    def next_task(self) -> Task | None: ...
    def report(self, task: Task, event: str, text: str) -> None: ...
```

Two ship with it. **Telegram** (`--telegram`): send the bot anything and it is a
task; a screenshot with no caption is a perfectly good brief. Set
`AGENTQ_TELEGRAM_TOKEN` and the first person to send `/start` claims the bot.
**Folder** (the default): drop a `.md` file into `.agentq/inbox/`, read the
answer in `.agentq/outbox/`. That one needs no credentials and is what you point
a web form, a cron job or CI at.

Anything else — a site, Slack, GitHub issues — is those two methods and a
`Runner(config, YourFront(...))`.

## Embedding

When you want the runner to own your app process as well, so that "merged" and
"restarted" are one step and the app's own error log becomes context for the
agent:

```python
from agentq import Runner

Runner(front="telegram").run()
```

with `process = "uv run python -m app"` in `agentq.toml`.

## Configuration

None is required. `agentq init` writes what was detected so you have something
to edit; `agentq doctor` tells you what would run before anything runs.

```toml
base_branch = "main"
delivery    = "auto"      # auto | merge | pr
max_rounds  = 3
model       = "sonnet"
permission  = "acceptEdits"   # or "skip" — see below
process     = ""              # optional app to run and restart

[[checks]]
name    = "test"
command = "npm run test -- --run"

[telegram]
admins = []                   # empty: the first /start claims the bot
```

Every key also reads from the environment as `AGENTQ_<KEY>`, which wins over the
file — tokens belong in a shell profile, not in a commit.

## What it will not do

It never force-merges and never resolves a conflict on your behalf. A merge it
cannot make cleanly becomes a pull request; a pull request it cannot open leaves
the branch intact and says so. A reviewer that does not end its reply with a
verdict is read as a rejection, because the alternative is landing work on the
strength of a truncated message.

`permission = "skip"` passes `--dangerously-skip-permissions` to the agents.
It is defensible because they work in a throwaway worktree and nothing they do
reaches your base branch without passing the checks and the reviewer — but it is
opt-in, and it should stay that way.

## Requirements

Python 3.11+, git, and the [Claude Code](https://claude.com/claude-code) CLI,
signed in. `gh` only if you want pull requests. No Python dependencies at all:
this gets installed next to your project, and a dependency of ours would be a
version conflict of yours.
