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

The Dockerfile includes Python, Git, Node, npm, and OpenCode. Install any additional project tools in an operator-maintained image before running a task. MergeRail never builds a project's Dockerfile or installs dependencies on the host. Images must already exist locally and must be pinned by `sha256:` ID or registry digest.

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

Keep any existing agent options in those tables. The default online model is `opencode/space-bunny-free`. A selected model must be present in the local OpenCode model catalogue and declare zero input/output/cache cost. Docker's online path currently supports the fixed keyless OpenCode Zen origin only. Claude and Codex online execution are unavailable. Operator-configured external driver commands can run offline inside the image.

Alternatively, pass `--docker-image sha256:…` to `init`, `doctor`, or `run`, or set `MERGERAIL_EXECUTION_IMAGE`. `MERGERAIL_EXECUTION_BACKEND=docker` and `MERGERAIL_EXECUTION_REQUIRED=true` are also supported. `init` persists the validated execution settings. Run `mergerail doctor --run-checks` to validate the engine, image, backend, and baseline before queueing work.

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
