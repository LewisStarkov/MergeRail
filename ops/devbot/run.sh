#!/usr/bin/env bash
set -Eeuo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runtime_dir=/Users/lama/.local/share/mergerail-devbot
colima start mergerail-devbot --activate=false --cpus 3 --memory 10 --disk 20 --root-disk 10 \
  --vm-type vz --mount "$runtime_dir:w" --ssh-agent=false --ssh-config=false --binfmt=false
docker --context colima-mergerail-devbot compose --env-file "$runtime_dir/runtime.env" \
  -f "$source_dir/ops/devbot/compose.yaml" up -d
export PYTHONPATH="$source_dir/src"
exec "$source_dir/python" -m mergerail.devbot_deploy \
  --outbox "$runtime_dir/outbox" --state "$runtime_dir/deployer"
