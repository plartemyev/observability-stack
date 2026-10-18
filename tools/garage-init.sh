#!/usr/bin/env bash
# One-time (idempotent) Garage bootstrap: assign the node to the cluster
# layout, create the bucket, import the S3 key, grant the key.
#
# Usage: tools/garage-init.sh [vars-file]
# Must run on the hub host with docker compose available; the garage service
# must be renderable (run tools/render.sh first).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VARS=${1:-$ROOT/deployments/local.env}
[ -f "$ROOT/stack/.env" ] || { echo "stack/.env missing — run tools/render.sh first" >&2; exit 1; }

set -a; . "$VARS"; set +a
: "${S3_BUCKET:=thanos}"
: "${GARAGE_ZONE:=dc1}"
: "${GARAGE_CAPACITY:=10G}"   # plain integers are bytes in garage v2 — always use a suffix

cd "$ROOT/stack"

# garage container has no shell; drive the CLI via compose exec.
g() { docker compose exec -T garage /garage "$@"; }

# wait until the daemon answers (bounded: 30 x 2s)
for _ in $(seq 1 30); do
  if g status >/dev/null 2>&1; then break; fi
  sleep 2
done
g status >/dev/null   # fail hard if still down

NODE_ID=$(g node id)
echo "garage node: $NODE_ID"

if g layout show | grep -q "$NODE_ID"; then
  echo "layout: already applied"
else
  g layout assign -z "$GARAGE_ZONE" -c "$GARAGE_CAPACITY" "$NODE_ID"
  VERSION=$(g layout show | sed -n 's/.*Current cluster layout version: \([0-9]*\).*/\1/p')
  g layout apply --version "$((VERSION + 1))"
  echo "layout: assigned node (zone $GARAGE_ZONE, capacity $GARAGE_CAPACITY) and applied"
fi

if g bucket list | grep -qw "$S3_BUCKET"; then
  echo "bucket: $S3_BUCKET exists"
else
  g bucket create "$S3_BUCKET"
  echo "bucket: created $S3_BUCKET"
fi

if g key list | grep -qw obs-thanos; then
  echo "key: obs-thanos exists"
else
  g key import --yes -n obs-thanos "$S3_ACCESS_KEY" "$S3_SECRET_KEY"
  echo "key: imported obs-thanos"
fi

g bucket allow "$S3_BUCKET" --read --write --key obs-thanos
echo "granted obs-thanos → $S3_BUCKET (read/write)"
echo
g status
