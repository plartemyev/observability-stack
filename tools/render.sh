#!/usr/bin/env bash
# Render the stack's templates + generated secrets for one deployment.
#
# Usage: tools/render.sh [vars-file]        (default: deployments/local.env)
#
# The vars file is the full contract of a deployment (versions, retention
# tiers, ports, names). Secrets are generated once and appended to the vars
# file, so re-renders are idempotent and the file is the single backup target
# for credentials. Newly generated values are printed at the end.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
STACK=$ROOT/stack
VARS=${1:-$ROOT/deployments/local.env}

[ -f "$VARS" ] || { echo "vars file not found: $VARS" >&2; exit 1; }
command -v envsubst >/dev/null || { echo "envsubst not found (gettext)" >&2; exit 1; }

set -a; . "$VARS"; set +a
: "${PROJECT_NAME:?set PROJECT_NAME in $VARS}"

# ── defaults ────────────────────────────────────────────────────────────────
: "${DATA_DIR:=./data}"
: "${RUN_UID:=$(id -u)}"
: "${RUN_GID:=$(id -g)}"
: "${BIND_IP:=127.0.0.1}"
: "${QUERY_PORT:=9090}"
: "${LOKI_PORT:=3100}"
: "${GRAFANA_PORT:=3000}"
: "${BUCKET_WEB_PORT:=9094}"
: "${SCRAPE_INTERVAL:=60s}"
: "${TSDB_RETENTION:=48h}"
: "${RETENTION_RAW:=30d}"
: "${RETENTION_5M:=180d}"
: "${RETENTION_1H:=3y}"
: "${LOKI_RETENTION:=30d}"
: "${S3_BUCKET:=thanos}"
: "${GARAGE_ZONE:=dc1}"
: "${GRAFANA_ADMIN_USER:=admin}"
: "${HUB_HOSTNAME:=$(hostname -s)}"
: "${YACE_ENABLED:=false}"
: "${YACE_ACCOUNTS_FILE:=}"
: "${YACE_VERSION:=v0.68.0}"
: "${GARAGE_VERSION:=v2.4.1}"
: "${THANOS_VERSION:=v0.42.4}"
: "${PROMETHEUS_VERSION:=v3.15.0}"
: "${ALERTMANAGER_VERSION:=v0.34.1}"
: "${LOKI_VERSION:=3.7.8}"
: "${ALLOY_VERSION:=v1.20.1}"
: "${GRAFANA_VERSION:=13.2.4}"
: "${NODE_EXPORTER_VERSION:=v1.12.1}"
: "${DOCKER_GID:=$(stat -c %g /var/run/docker.sock 2>/dev/null || echo 999)}"
: "${COMPOSE_PROJECT_NAME:=obs-$PROJECT_NAME}"
export COMPOSE_PROJECT_NAME COMPOSE_PROJECT_NAME DATA_DIR RUN_UID RUN_GID BIND_IP \
  QUERY_PORT LOKI_PORT GRAFANA_PORT BUCKET_WEB_PORT SCRAPE_INTERVAL TSDB_RETENTION \
  RETENTION_RAW RETENTION_5M RETENTION_1H LOKI_RETENTION S3_BUCKET GARAGE_ZONE \
  GRAFANA_ADMIN_USER HUB_HOSTNAME GARAGE_VERSION THANOS_VERSION PROMETHEUS_VERSION \
  YACE_ENABLED YACE_ACCOUNTS_FILE YACE_VERSION \
  ALERTMANAGER_VERSION LOKI_VERSION ALLOY_VERSION GRAFANA_VERSION NODE_EXPORTER_VERSION \
  DOCKER_GID

# ── secrets: generate once, persist into the vars file ─────────────────────
GENERATED=()
gen_if_missing() {
  local name=$1 val
  val=$(grep -E "^${name}=" "$VARS" | head -1 | cut -d= -f2- || true)
  if [ -z "$val" ]; then
    val=$2
    printf '%s=%s\n' "$name" "$val" >> "$VARS"
    GENERATED+=("$name")
  fi
  export "$name=$val"
}
gen_if_missing RPC_SECRET        "$(openssl rand -hex 32)"
gen_if_missing GARAGE_ADMIN_TOKEN "$(openssl rand -base64 32 | tr -d '\n')"
gen_if_missing S3_ACCESS_KEY     "GK$(openssl rand -hex 10)"
gen_if_missing S3_SECRET_KEY     "$(openssl rand -hex 32)"
gen_if_missing GRAFANA_ADMIN_PASSWORD "$(openssl rand -hex 12)"

# ── dirs (state + mount points; owned by RUN_UID — no root needed) ──────────
mkdir -p "$STACK/$DATA_DIR"/{garage/meta,garage/data,prometheus,loki,grafana,alertmanager,thanos-store,thanos-compact,alloy}
mkdir -p "$STACK/prometheus/targets" "$STACK/yace" "$STACK/grafana/dashboards"

# ── render ──────────────────────────────────────────────────────────────────
# Substitute ONLY UPPER_CASE variables: component configs legitimately contain
# lowercase shell-var-looking text (Go templates: $labels, $value, $instance)
# that must survive rendering untouched.
SUBST_LIST=$(env | sed -n 's/^\([A-Z0-9_]*\)=.*/$\1/p' | sort -u | tr '\n' ' ')
# agent/ and systemd/ templates are rendered per-device/hub-phase by
# tools/obsdeploy — render.sh handles the hub stack only.
find "$STACK" \( -path "$STACK/agent" -o -path "$STACK/systemd" \) -prune -o \
  -name '*.tmpl' -print0 | while IFS= read -r -d '' f; do
  envsubst "$SUBST_LIST" < "$f" > "${f%.tmpl}"
done
# the two alertmanager variants render to their own names as side effects —
# remove them; the conditional below writes the real alertmanager.yml
rm -f "$STACK/alertmanager/alertmanager-telegram.yml" "$STACK/alertmanager/alertmanager-placeholder.yml"

# Alertmanager receiver: Telegram when configured, else a placeholder that
# fires nowhere (alerts visible in Prometheus/AM UI only).
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  envsubst "$SUBST_LIST" < "$STACK/alertmanager/alertmanager-telegram.yml.tmpl" > "$STACK/alertmanager/alertmanager.yml"
else
  envsubst "$SUBST_LIST" < "$STACK/alertmanager/alertmanager-placeholder.yml.tmpl" > "$STACK/alertmanager/alertmanager.yml"
fi

# CloudWatch: always render aws.env (compose validates env_file even for
# inactive profiles); config comes from the deployment's accounts file when
# enabled, placeholder otherwise. Scrape target file only when enabled.
if [ "${YACE_ENABLED}" = "true" ]; then
  [ -n "$YACE_ACCOUNTS_FILE" ] && [ -f "$YACE_ACCOUNTS_FILE" ] \
    || { echo "YACE_ENABLED=true but YACE_ACCOUNTS_FILE not set/no file" >&2; exit 1; }
  cp "$YACE_ACCOUNTS_FILE" "$STACK/yace/config.yaml"
  printf -- "- targets: ['yace:5000']\n  labels:\n    job: yace\n" > "$STACK/prometheus/targets/yace.yml"
else
  printf '# AWS credentials for YACE (empty — profile disabled)\n' > "$STACK/yace/aws.env"
  printf -- "- targets: []\n" > "$STACK/prometheus/targets/yace.yml"
fi

# Files carrying secrets: restrict to the render user.
chmod 600 "$STACK/alertmanager/alertmanager.yml" "$STACK/garage/garage.toml" \
          "$STACK/thanos/bucket.yaml" "$STACK/yace/aws.env" 2>/dev/null || true

echo "rendered: $STACK (vars: $VARS)"
echo
echo "── summary ──"
echo "  project           : $PROJECT_NAME"
echo "  grafana           : http://$BIND_IP:$GRAFANA_PORT  (user: $GRAFANA_ADMIN_USER)"
echo "  thanos query      : http://$BIND_IP:$QUERY_PORT"
echo "  thanos bucket web : http://$BIND_IP:$BUCKET_WEB_PORT  (profile: tools)"
echo "  loki (push)       : http://$BIND_IP:$LOKI_PORT  (tenant: $PROJECT_NAME)"
echo "  data dir          : $STACK/$DATA_DIR   ← back this up"
if [ "${#GENERATED[@]}" -gt 0 ]; then
  echo "  generated (also appended to $VARS):"
  for name in "${GENERATED[@]}"; do
    echo "    $name=$(grep -E "^$name=" "$VARS" | cut -d= -f2-)"
  done
fi
