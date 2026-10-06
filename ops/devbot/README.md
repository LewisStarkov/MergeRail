# MergeRail → Rivals DevBot

DevBot is the existing `@rivalsgamedevbot` at `https://dev.rivals.baby`.
The integration calls `scripts/cpd-dev.sh`, not a new bot API. Ordinary reviewed
tasks deploy automatically to this development environment only.
[Recorded end-to-end verification](VERIFICATION.md) covers actual deployment,
health rejection, rollback, retry, access controls and restart recovery.

## Trust boundary

The controller is trusted and has the **dedicated Colima VM's** Docker socket.
It is not an unprivileged sandbox. Docker Desktop and `projects-main` sockets
are never mounted into it. The VM mounts only
`/Users/lama/.local/share/mergerail-devbot`; home directories and SSH agents are
not shared. The controller can authorize development deployment through its
trusted approval records; a compromised controller is therefore outside the
untrusted-worker threat model.

Workers receive committed Git objects, never bind mounts, tunnel credentials,
deployment credentials or Docker sockets. They use a read-only root, dropped
capabilities, non-root execution, cgroup v2, 2 CPUs, 4 GiB RAM, 256 PIDs,
2 GiB workspace tmpfs and 1536 MiB temporary tmpfs. The stage's trusted bootstrap
uses UID 0; project commands execute as UID 65534 (fixer/checks) or 65533
(reviewer). Online stages split the aggregate limits between the agent
(1.9 CPUs, 3840 MiB, 224 PIDs) and its restricted AI gateway
(0.1 CPU, 256 MiB, 32 PIDs); offline checks have no network.
Agent turns take at most
600 seconds, with at most three rounds; each check stage is limited to 1800
seconds. Failed Docker preflight stops the service; host fallback is forbidden.
Fixer and reviewer use Codex `gpt-6.1-sol` through the ChatGPT subscription.
Compose explicitly sets `MERGERAIL_FIXER_*` and `MERGERAIL_REVIEWER_*` operator
overrides, so the committed application's OpenCode defaults remain compatible
with older releases. This also avoids introducing an unrelated application
commit solely to change the execution provider.
The trusted host credential broker refreshes the normal Codex login and atomically
exports only its access token and account ID every 15 seconds into a private
directory. Exports expire after 120 seconds and are mounted only into the trusted
controller; the source-free gateway receives credentials through bounded stdin.
Workers never receive the login cache, refresh token or access token. API billing
is not enabled. If authentication is revoked, run `codex login` on the Mac and
retry the failed task. Keyless OpenCode remains supported as an explicit alternative.
If the credential broker exits, the supervisor restarts it without interrupting
an active deployment. Stale exports fail closed. Colima maps the private mount's
owner to UID 0 inside the controller; its reader still requires owner UID 0 and
mode 0600, while the host broker requires the Mac user's UID and directory mode 0700.

BuildKit is another trusted component within the dedicated VM. It needs
privileges there for Linux build namespaces and amd64 emulation; it has no
primary-host socket, home mount, SSH agent or deployment secrets. Its workload
is limited to 1 CPU, 3 GiB RAM, 256 PIDs, 1800 seconds per build and 2 GiB GC
cache. The VM bounds total Docker disk to 20 GiB. The host adapter alone uses
the already configured `projects-main` SSH connection to transport checksummed
images and run CPD. `--prebuilt-only` refuses builds on the application server.
[Docker's container-driver resource options](https://docs.docker.com/build/builders/drivers/docker-container/)
document the builder limits.

## Prepare and start

Prerequisites: macOS Apple Silicon, Colima/Docker CLI, Python 3.11+, controller
Git 2.48+, configured ngrok account, `projects-main` SSH and the checked Rivals
`main` checkout. Install Codex CLI 0.159.2 and run `codex login` on the Mac with
file-based login caching before starting the service. No account, paid resource
or production credential is created.

```bash
cd /Users/lama/Documents/dev/mergerail
uv sync --frozen
mkdir -p /Users/lama/.local/share/mergerail-devbot
chmod 700 /Users/lama/.local/share/mergerail-devbot
colima start mergerail-devbot --activate=false --cpus 3 --memory 10 --disk 20 --root-disk 10 \
  --vm-type vz --mount /Users/lama/.local/share/mergerail-devbot:w \
  --ssh-agent=false --ssh-config=false --binfmt=false
uv run python ops/devbot/prepare.py
uv run python ops/devbot/install-service.py
```

`prepare.py` copies dependency manifests from a recorded commit, builds fixed
operator Dockerfiles, generates a persistent password and seeds an empty
project volume. It preserves existing project/state volumes, passwords,
outbox and deployment checkout. Configure secrets from the examples if ngrok
was not already configured. Real secret files stay outside Git with mode 0600.
The selected catalogue is metadata from `~/.cache/opencode/models.json`, not
credentials. [OpenCode documents its model catalogue](https://opencode.ai/docs/models/).
Agents use the preinstalled `/opt/rivals-deps/.venv/bin/python`; the project
policy forbids creating another `.venv` or installing dependencies. Absolute
workspace symlinks are rejected during checkpoint validation.

The extra deployment checkout is
`/Users/lama/.local/share/mergerail-devbot/deployer/checkout`, on `main`, tracking
the same `origin/main`. Preparation compares its ancestry and CPD scripts with
the supported primary checkout. Its protocol uses `/opt/rivals-dev`, immutable
checksummed releases and `/opt/rivals-dev-config`; unrelated primary Graphify
changes are preserved. Application tasks cannot edit deployment scripts,
dependency manifests, migrations or operator policy. Automatic tasks also cannot
change file modes or symlinks, because the existing image tags hash file bytes.

Get the HTTPS endpoint:

```bash
docker --context colima-mergerail-devbot logs --tail 100 mergerail-devbot-controller-1
```

Log in as `mergerail`; the password is stored at
`/Users/lama/.local/share/mergerail-devbot/secrets/web-password`.
Ngrok runs in the same controller network namespace as `127.0.0.1:8788`.
There is no published origin port. UI, API and SSE require the same authenticated
session. Cookies are HttpOnly, SameSite=Strict and Secure over HTTPS; SSE
rechecks session validity and sends a fresh snapshot on reconnect. Inspection
of ngrok requests is disabled. The password is not printed in logs.
The generated ngrok hostname can change after a controller/tunnel restart;
retrieve the new HTTPS URL from the controller log.

## Deployment and recovery

The queue and event history persist in named volumes. Owner state persists
separately so restart cleanup recognizes only this runner's labelled resources.
A trusted service copy outside Documents avoids macOS background-file restrictions.
LaunchAgent `local.mergerail-devbot` starts the VM, controller and host adapter
at user login and restarts the adapter after failure. Availability depends on
this Mac being awake and the user session running.

Outbox jobs persist task ID, attempt, approved SHA, base SHA and bundle checksum.
Every deployment status transition is saved in the job’s `history/` directory,
including failed attempts and subsequent retries.
The adapter cross-checks the runner approval and saves its own copy before using
SSH. It deploys the approved Git objects, not a working directory or floating
HEAD. The release source and images derive from that exact SHA. Automatic jobs
are serialized; ancestor results are superseded, and a server-side comparison
under the environment lock prevents a changed verified SHA being overwritten.

The approved bootstrap runtime was compared with the previously verified
release. Product constraints remain Dice PvP/deposited USDT, deposit maximum
10 USDT, and no RIV conversion, NFT, Cases, Upgrade or new modes. Path gates
prevent infrastructure/schema changes; the agent reviewer also evaluates
product scope. Changing that scope requires an explicit operator decision.

After CPD verifies the candidate, the adapter checks public `/health` against
the full SHA, using CPD's `Rivals-Deploy/1.0` User-Agent. HTTP failures retain
their status code in the deployment record. On failure it records actual dev container state and calls the
existing schema/database-guarded rollback, comparing the observed current SHA
under the same deployment lock. The checked rollback helper is transferred
completely, checksum-verified, and run with stdin closed before Docker starts;
attached Docker commands cannot consume the remaining helper source.
A concurrent newer release is preserved. Image transport is limited to 1800 seconds and server import to 600 seconds;
failed staging archives and temporary VM image tags are cleaned. CPD/rollback
have a 1800-second observation deadline. If it expires, the remote process is
left active and the queue becomes `blocked` for inspection.
A refused rollback becomes `blocked`
and stops the queue for operator inspection. It never downgrades/restores a
database or blindly restarts code across a migration marker. `Retry deploy`
uses the saved bundle/SHA and does not rerun fixer or review.
Recovery checks retained app/gateway/backup images before invoking the rollback
wrapper. Missing images block recovery; prepare the saved SHA's images in the
isolated VM rather than letting a legacy helper build on the application server.

Docker logs are capped at 2 × 5 MiB; audit at 4 × 5 MiB; adapter logs at
3 × 5 MiB; build/deploy/rollback logs at 5 MiB each. Worker artifact cache is
8 GiB and outbox bundles 4 GiB. Disk exhaustion fails closed. Completed worker recovery archives are removed after the immutable request
is durable. Successful/superseded bundles retain the latest 20 within a 2 GiB
budget; request/status history remains. Failed jobs keep their bundles for retry.
Build source is limited to 512 MiB/50000 entries; image output is limited to 4 GiB
while writing. Disposable build contexts are removed on success or failure.
The uploaded VM image tag is removed after server checksum/image verification.
Archive unresolved jobs before their retained artifacts reach the bounds. Cleanup must select this profile's labels and explicit
paths. Never run Docker prune or operate on other projects' resources.

## Update, stop and rollback

Update only while no task/build/deployment is active. Stop the adapter and
controller with the commands below first; preparation refuses a running controller.
Images receive permanent digest tags before convenience tags move. Save `runtime.env`, rebuild
with `prepare.py`, and reinstall the service snapshot with `install-service.py`, then restart. An image/policy change invalidates
pending approvals; retain the original image/config to recover them or obtain
a new review. Preparation never resets an existing project volume.

```bash
launchctl kickstart -k gui/$(id -u)/local.mergerail-devbot
```

Stop MergeRail and its adapter, preserving all state and the deployed app:

```bash
launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/local.mergerail-devbot.plist"
cd /Users/lama/Documents/dev/mergerail
docker --context colima-mergerail-devbot compose \
  --env-file /Users/lama/.local/share/mergerail-devbot/runtime.env \
  -f ops/devbot/compose.yaml stop
colima stop mergerail-devbot
```

Rollback the application after stopping the adapter, with an existing verified
release whose migration inputs and bot configuration remain compatible:

```bash
cd /Users/lama/Documents/dev/rivals
bash scripts/rollback.sh development <full-previous-sha> \
  --expected-current-sha <full-current-verified-sha>
```

For a controller rollback, restore the saved private `runtime.env` pins and
restart Compose. Do not delete state/owner/project volumes. For a blocked
rollout, inspect `status.json`, `server_state`, private logs,
`/opt/rivals-dev-config/verified-commit`, actual containers and any migration
marker before reconciling the saved status. Do not retry unresolved external
state blindly.
