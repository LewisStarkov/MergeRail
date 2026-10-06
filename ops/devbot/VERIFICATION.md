# Verified DevBot workflow — 2026-10-06

MergeRail: https://8cae-156-243-244-132.ngrok-free.app

Application: https://dev.rivals.baby/app/ (`@rivalsgamedevbot`).
The generated ngrok hostname can change after restarting its tunnel.

Two real tasks were submitted through authenticated HTTPS. OpenCode executed
them in Docker; full offline checks, agent review and final delivery checks
passed. Both changed only `tests/test_release_health.py` and automatically
entered the deployment outbox after local delivery of the approved commit.

| Task | Approved/deployed SHA | Git bundle SHA-256 |
|---|---|---|
| 1 | `e50c7ddb75f80e869b106bf56893fa28fc1a595a` | `4abd9efb8f13f1f8fdb71e570e3fe35e35248b2bad9232db676baf0b625a7022` |
| 2 | `436c35fd4b86f5b9d77f796a1db3e5280703384f` | `ef8e8340ec88b3cb611c4baa0a9161781a411d75791f3eb402fb8b33ba00b3e3` |

The actual `cpd-dev.sh` verified release checksums, public health/revision,
Mini App/admin assets, database identity/revision, container health, bot
identity and menu URL. The final public `/health`, release symlink and
`verified-commit` all identify task 2's SHA. The database remained on revision
0059; unchanged migration inputs caused backup/migration to be skipped.

## Failure, retry and recovery

After task 2's candidate passed CPD, a private test adapter directed its
additional health probe to a deliberately missing route. The real HTTP 404
triggered the ordinary guarded rollback to task 1's SHA. Public health,
canonical symlink and verified marker confirmed the restored working release.
The application code itself was not broken for this test.

Authenticated `retry-deploy` then deployed task 2's saved result successfully.
The bundle checksum, task attempt, approved SHA, sessions, runs and delivery
record were compared before/after and remained identical. Deployment history
contains `building → running → failed → building → running → succeeded` and
retains the HTTP 404 reason. The previous working release is task 1's SHA.

The controller was restarted with a completed task pending deployment; its
queue and outbox survived. The final adapter restart preserved both successful
jobs, artifacts and history without rerunning fixer. A real server-side
expected-SHA mismatch returned 42 before changing services. Unit tests also
cover stale result suppression and restart after candidate promotion with
healthy/unhealthy probes and missing/invalid rollback metadata.

## Access and isolation

- Anonymous Web UI requests redirect to login; authenticated UI returns 200.
- Anonymous API and SSE return 401. Authenticated cookies are Secure.
- SSE reconnects deliver snapshots; logout closes an existing stream.
- The final authenticated SSE snapshot contains task 2's matching SHA,
  `succeeded` status and DevBot URL.
- Live OpenCode ran as UID 65534 on an internal IPv4-only Docker network.
  The restricted AI gateway alone had outbound networking. Workers had no
  bind mounts, host home, Docker socket, SSH agent or deployment/tunnel secrets.
  Limits and the root bootstrap distinction are documented in [README](README.md).
- Docker's default context remained `desktop-linux`; task/build execution used
  the separate `colima-mergerail-devbot` Engine. Only its own resources were managed.

The retained images for the previous working release were checked on the real
server. A regression test verifies that a missing image prevents the rollback
wrapper from running, excluding legacy server-side build fallback.

MergeRail's full Python suite passed with 80.19% coverage; Ruff and mypy passed.
Web UI: 21 tests, TypeScript checking and production build passed. Independent
security/correctness reviews completed; the reported rollback transport and
restart recovery defects were fixed and regression-tested.

Private detailed evidence and logs are in
`/Users/lama/.local/share/mergerail-devbot`, including
`final-deployment-verification.json`, `automatic-rollback-verification.json`,
`access-verification.json`, `user-result-verification.json` and
`live-online-isolation.json`. Secrets are excluded from this report and Git.
# Codex switch — 2026-10-06

The dedicated controller now selects Codex `gpt-6.1-sol`, reasoning `high`, for
both fixer and reviewer through explicit Compose overrides. Authentication uses
the operator's ChatGPT subscription; OpenAI API billing is disabled. The host
broker retains the normal login/refresh cache and exports only access credentials.

Live Docker acceptance with explicit reasoning `high` passed in 89.40 seconds:
fixer edits, resumed conversation
in a second container, offline checks, and structured APPROVE from a reviewer whose
write and chmod attempts were denied. The regular suite passed (809 passed,
8 skipped, 80.26% coverage); additional real Bash broker lifecycle tests passed.
Ruff and mypy passed. A SIGTERM fault against the deployed credential broker
caused it to restart while the deployment adapter's PID remained unchanged.

Pinned images: controller
`sha256:a886249db0416ad94d0717d192dec42fbc8d6af7d82597014c296a7cf4542121`,
worker `sha256:8faf24f36aeb648ea38542e73e0beb6b82ff898668cedd5b71b594692ea60bd9`.
Private evidence is in `codex-switch-verification.json`, `codex-live-acceptance.log`
and `codex-broker-recovery.json` under the runtime directory.

The first application attempt exposed a CLI override bug: a reasoning `--config`
after `exec` replaced the gateway provider overrides before `exec`, so the client
tried direct DNS and the isolated network refused it. All config overrides now
precede `exec`; the high-effort live test covers that routing regression. Failed
attempts deployed nothing and preserved the verified application release.
