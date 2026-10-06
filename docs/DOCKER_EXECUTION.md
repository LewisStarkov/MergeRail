# Docker execution

Docker execution is opt-in. Without `[execution]`, MergeRail keeps its existing local execution mode. When Docker is requested, failed validation stops the task; MergeRail does not run its agent, checks, or application on the host.

## Setup

Install MergeRail outside the repository that the agent will edit. Use host Git 2.48 or newer with the `files` reference-storage backend and start a local Docker Engine 28 or newer running Linux containers. The initial supported configuration is native Docker on Linux or Docker Desktop on macOS, with the container architecture matching the host. Remote engines, emulation, unsupported storage/runtime capabilities, and images declaring volumes are rejected.

Build the trusted runtime from MergeRail's own Dockerfile, then use its immutable local image ID:

```sh
docker build -t mergerail-runtime:local -f docker/Dockerfile .
docker image inspect --format '{{.Id}}' mergerail-runtime:local
docker pull python@sha256:e2a5fce94bd761967528a12f16d707c2613e1522f3f2d77fa45766f45962547f
```

The Dockerfile includes Python, Git, Node, npm, OpenCode, and Codex CLI 0.159.2. Install any additional project tools in an operator-maintained image before running a task. MergeRail never builds a project's Dockerfile or installs dependencies on the host. Images must already exist locally and must be pinned by `sha256:` ID or registry digest.

Add this to `mergerail.toml`, substituting the inspected image ID:

```toml
[execution]
backend = "docker"
required = true
profile = "eco"
image = "sha256:REPLACE_WITH_64_HEX_DIGITS"

[agents.fixer]
backend = "opencode"

[agents.reviewer]
backend = "opencode"
```

Keep any existing agent options in those tables. The default online model is `opencode/space-bunny-free`. A selected model must be present in the local OpenCode model catalogue and declare zero input/output/cache cost. OpenCode uses the fixed keyless OpenCode Zen origin. Codex uses a separate authenticated gateway, described below. Claude online execution is unavailable. Operator-configured external driver commands can run offline inside the image.

Alternatively, pass `--docker-image sha256:…` to `init`, `doctor`, or `run`, or set `MERGERAIL_EXECUTION_IMAGE`. `MERGERAIL_EXECUTION_BACKEND=docker` and `MERGERAIL_EXECUTION_REQUIRED=true` are also supported. `init` persists the validated execution settings. Run `mergerail doctor --run-checks` to validate the engine, image, backend, and baseline before queueing work.

## Codex authentication

Install Codex CLI 0.159.2 on the controller host for login and token refresh.
Run `codex login`, then select Codex for both agents:

```toml
[execution]
backend = "docker"
required = true
profile = "eco"
image = "sha256:REPLACE_WITH_64_HEX_DIGITS"
codex_auth = "chatgpt"

[agents.fixer]
backend = "codex"
model = "gpt-6.1-sol"

[agents.reviewer]
backend = "codex"
model = "gpt-6.1-sol"
```

The default `codex_auth = "chatgpt"` uses subscription authentication from the
host's `~/.codex/auth.json`, or `$CODEX_HOME/auth.json` when configured. File-based
login caching is required; an OS keyring alone is unavailable to this runtime.
The controller reads the cache again for each turn. It sends only the access
token and account ID to a source-free gateway container through bounded stdin.
Refresh tokens, the host home, and the login cache are never copied into the
agent container. Before expiry, the controller uses the host Codex CLI account RPC to refresh
authentication in its normal login cache. This RPC opens no coding session and
runs outside the project. If the login is revoked or rotation fails, run
`codex login` on the host and retry the task.

To opt into usage-based OpenAI API billing, set `codex_auth = "api"` and provide
`MERGERAIL_CODEX_ALLOW_API_BILLING=1` and `OPENAI_API_KEY` in the controller
environment, or enable that billing flag and use a host auth cache created
with `codex login --with-api-key`. A key in the environment never switches a
ChatGPT task to API billing automatically. Keep credentials out of
`mergerail.toml` and Git.

Codex model traffic reaches only the gateway's fixed Responses endpoint at
`chatgpt.com/backend-api/codex` or `api.openai.com/v1`, according to the selected
authentication method. The gateway supplies credentials independently of client
headers. Agent sessions persist between fixer turns; login and provider config
files are excluded when restoring the task home. Codex uses the container's
isolation and permissions for both roles, including the reviewer's immutable
source snapshot.

## What a task can access

MergeRail transfers committed Git objects through bounded bundles. Dirty, ignored, and untracked host files are not input. Submodules, Git LFS content, unsafe paths, and outward source symlinks are rejected. There are no host directory mounts, Docker socket mounts, host home directories, or host agent credentials inside the task.

The committed `mergerail.toml` is readable source, but a task cannot add, delete, rename, or change it. This preserves the operator's execution, backend, and check settings for subsequent runs. Update that file directly when changing MergeRail configuration.

Each stage uses a read-only container root, dropped capabilities, no privilege escalation, and explicit CPU, memory, PID, log, and tmpfs limits. The default workspace plus task home is 512 MiB, temporary storage is 128 MiB, RAM is 2 GiB, and CPU is one core. Scratch space is ephemeral. Archives and bundles in MergeRail's state directory have individual bounds and a cache budget per repository state directory. Operator-managed image storage is separate.

Online stages divide that same RAM, CPU, PID, and temporary-storage budget between the agent and its gateway. With defaults, the gateway receives 256 MiB RAM, 0.1 CPU, 32 PIDs, and 16 MiB temporary storage; the agent receives the remainder. Limits too small for both processes block online execution. Interrupted containers belonging to this user and repository are stopped before a new stage starts. Containers from another repository, another user, or an unknown owner block new stages and are preserved for their owner to inspect or recover.

The fixer can edit its task workspace. Checks receive a fresh immutable Git snapshot without the fixer's home or session. The reviewer receives a separate source snapshot whose files it cannot write or chmod. Online agent containers use an internal bridge with Docker’s `isolated` gateway mode and reach the fixed origin through a narrow HTTP gateway; checks, recovery, merge, and external drivers run offline. The supervisor also runs inside Docker, stops before a heavy stage, and has a maximum idle lifetime of 60 seconds. The network mode requires Engine 28 or newer; see [Docker gateway modes](https://docs.docker.com/engine/network/port-publishing/#gateway-modes).

At the end of a turn, a container helper stops and terminates remaining task processes before exporting its workspace. A separate offline container validates the snapshot, sanitizes Git metadata, commits remaining changes, and exports a canonical Git bundle. The host imports only this validated result. It never executes project hooks, merge drivers, or filters.

## Delivery and recovery

MergeRail saves the reviewed commit and execution policy with the task. Local integration and its checks run in another sandbox. Delivery applies the exact validated candidate with Git reference and index locks; a dirty checkout, moved branch, or conflicting untracked file blocks delivery and preserves the result branch. A delivery retry uses the saved approved commit, without running the fixer again. A changed execution policy blocks that retry until the original policy is restored or the task is retried from the beginning.

Task history and the web UI expose the Docker backend, phase, base/result SHAs, validation state, and recovery information. A durable checkpoint can be restored after interruption. Changes made after the last successfully exported checkpoint may be lost. Interrupted host checkout uses a journal and only releases locks with recorded ownership; unexpected local edits or unknown locks require inspection instead of automatic overwriting. A crash between Git preparing a reference transaction and MergeRail recording its lock identities also requires inspection; recovery preserves those unknown locks.

The feature does not provide host-mounted previews, arbitrary network access, secrets injection, automatic image construction, or a validated Windows support claim. The earlier `DOCKER_EXECUTION_STAGE_A.md` and probe JSON files record historical experiments, including storage approaches that this runtime does not use.

## Acceptance tests

The regular Python suite covers policy and protocol validation. Real engine tests are opt-in:

```sh
MERGERAIL_TEST_DOCKER_IMAGE=sha256:YOUR_IMAGE_ID pytest tests/test_docker_integration.py
```

They exercise a fixer, destructive check side effects, an enforced read-only reviewer, exact local delivery, failure preservation, and delivery retry. Online provider validation additionally requires the model catalogue and a reachable fixed upstream.

Live Codex acceptance uses the controller's ChatGPT login and is separately opt-in:

```sh
MERGERAIL_TEST_CODEX_IMAGE=sha256:YOUR_IMAGE_ID pytest tests/test_docker_codex_integration.py
```

Set `MERGERAIL_TEST_CODEX_MODEL` if the account uses another model. This test
makes real subscription calls and verifies fixer edits, resume across separate
containers, offline checks, and structured review with denied write/chmod
attempts. It leaves the host branch unchanged.
