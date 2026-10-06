#!/usr/bin/env bash
set -Eeuo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runtime_dir=/Users/lama/.local/share/mergerail-devbot
export PYTHONPATH="$source_dir/src"
"$source_dir/python" -m mergerail.codex_credentials \
  --directory "$runtime_dir/secrets/codex-gateway"
colima start mergerail-devbot --activate=false --cpus 3 --memory 10 --disk 20 --root-disk 10 \
  --vm-type vz --mount "$runtime_dir:w" --ssh-agent=false --ssh-config=false --binfmt=false
docker --context colima-mergerail-devbot compose --env-file "$runtime_dir/runtime.env" \
  -f "$source_dir/ops/devbot/compose.yaml" up -d
start_credentials() {
  "$source_dir/python" -m mergerail.codex_credentials \
    --directory "$runtime_dir/secrets/codex-gateway" --loop &
  credential_pid=$!
}
start_credentials
"$source_dir/python" -m mergerail.devbot_deploy \
  --outbox "$runtime_dir/outbox" --state "$runtime_dir/deployer" &
deployment_pid=$!
cleanup() {
  kill "$credential_pid" "$deployment_pid" 2>/dev/null || true
  wait "$credential_pid" "$deployment_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT
while kill -0 "$deployment_pid" 2>/dev/null; do
  if ! kill -0 "$credential_pid" 2>/dev/null; then
    wait "$credential_pid" 2>/dev/null || true
    start_credentials
  fi
  sleep 5
done
wait "$deployment_pid"
