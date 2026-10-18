#!/usr/bin/env bash
# Hub state backup: tar the data dir + copy the deployment vars (the only
# credential store) into $BACKUP_DIR, keeping $BACKUP_KEEP generations.
# Designed to run from the obs-backup.timer installed by tools/obsdeploy hub backup.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
: "${BACKUP_DIR:?export BACKUP_DIR=<path>}"
: "${BACKUP_KEEP:=14}"
VARSF="${1:-$ROOT/deployments/local.env}"

[ -f "$VARSF" ] || { echo "vars file not found: $VARSF" >&2; exit 1; }
[ -d "$ROOT/stack/data" ] || { echo "no stack/data dir — nothing to back up" >&2; exit 1; }

ts=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"

tar czf "$BACKUP_DIR/obs-hub-$ts.tar.gz" -C "$ROOT/stack" data
cp -p "$VARSF" "$BACKUP_DIR/obs-vars-$ts.env"
chmod 600 "$BACKUP_DIR"/obs-*-$ts.*

ls -1t "$BACKUP_DIR"/obs-hub-*.tar.gz | tail -n +$((BACKUP_KEEP + 1)) | xargs -r rm -f
ls -1t "$BACKUP_DIR"/obs-vars-*.env  | tail -n +$((BACKUP_KEEP + 1)) | xargs -r rm -f

echo "backup: $BACKUP_DIR/obs-hub-$ts.tar.gz (kept: $BACKUP_KEEP)"
