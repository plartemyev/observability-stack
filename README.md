# observability-stack

Reproducible per-project observability hub: **Prometheus + Thanos** (metrics,
automatic downsampled "trends" tiers), **Loki** (logs), **Grafana**
(dashboards), **Garage** (S3-compatible bucket), deployed as a self-contained
docker compose project with a thin Ansible wrapper (Phase 2).

One stack per project/region; devices are scraped over a tailnet (pull,
Zabbix-style); logs are pushed by Grafana Alloy.

## Status

Phases 1–3 complete — standalone `stack/` verified on this machine
(2026-10-18), deployment tooling (`tools/obsdeploy`) with workbook mode,
Telegram alerting, YACE (off by default), dashboards and backups. The only
path not exercised against a real remote host is SSH device enrollment
(dry-run verified only).

## Architecture

```
 devices (any region, over tailnet)         project hub (compose)
 ┌──────────────────────┐                 ┌─────────────────────────────────────┐
 │ node_exporter         │  pull (scrape) │ prometheus ── sidecar ──▶ garage    │
 │   (metrics, hub pulls │◀───────────────│   ▲ local 48h    │ (S3 API)         │
 │    like Zabbix does)  │                │ thanos query ◀── store-gateway       │
 │ alloy (logs push)     │── push ───────▶│   ▲                    ▲             │
 │  journald/docker/app  │                │ grafana ◀─ loki       compactor      │
 └──────────────────────┘                │   alertmanager (+ profiles:          │
                                         │   bucket-web, yace*, thanos-ruler*)  │
                                         └─────────────────────────────────────┘
```

- **Metrics**: Prometheus scrapes devices (60s default). The sidecar uploads
  2h blocks to the Garage bucket; the compactor merges them and automatically
  builds **5m and 1h downsampled tiers** (the Zabbix "trends" analog), each
  with its own retention. Grafana charts transparently switch resolution as
  you zoom out. All three tiers are independently retained:
  `RETENTION_RAW=30d`, `RETENTION_5M=180d`, `RETENTION_1H=3y`.
- **Logs**: Alloy ships container/journald logs to Loki; every writer sends
  `X-Scope-OrgID: <PROJECT_NAME>` (tenant). The header is in every agent
  config already, so consolidating two projects onto one hub later is a
  config change, not a redesign.
- **Bucket**: Garage single-node (`replication_factor = 1`, one copy on this
  disk — every bucket block is derived data, rebuildable from Prometheus).
  To move history to cloud S3 (R2/B2): edit `stack/thanos/bucket.yaml`
  (endpoint + keys) and restart — nothing else references the bucket.
- **State**: everything lives in `stack/data/` — the single backup target.

## Quickstart (hub)

Copy the example vars file and edit it (`deployments/local.env.example` →
`deployments/<site>.env`; the copy holds generated secrets and is gitignored), then:

```bash
python3 tools/obsdeploy hub                    # render → up → garage-init → verify
python3 tools/obsdeploy verify                 # the verification sweep only
python3 tools/obsdeploy hub up                 # a single phase, manual when you want it
python3 tools/obsdeploy hub --dry-run          # print the plan, touch nothing
```

Everything is driven by the deployment vars file; secrets are generated once
and appended to it (printed by the render phase, archived by backups).

- Grafana: `http://<BIND_IP>:3000` — dashboards "Node basics" + "Hub overview"
  are provisioned automatically
- Thanos query API/UI: `http://<BIND_IP>:9090`
- Bucket web UI: `http://<BIND_IP>:9094`
- Loki push endpoint for agents: `http://<BIND_IP>:3100` (tenant = project)

### Enroll a device

The hub pulls metrics (node_exporter, Zabbix-style) and the device pushes
logs (Alloy). VPN provisioning is out of scope — bring your own tailnet;
`--device-addr`/`--hub-addr` are just addresses on it.

```bash
python3 tools/obsdeploy agent \
  --agent root@device.host --agent-name mypi \
  --device-addr mypi.yournet:9100 \
  --hub-addr hub.yournet
```

Phases: `probe → node-exporter → alloy → register` (run one with
`agent <phase>`). Registration drops a file_sd targets file on the hub and
reloads Prometheus — no hub config file is ever hand-edited.

### Workbook (manual execution, archinstaller-style)

`tools/obsdeploy workbook` renders the same step model the runner executes as
a markdown file — rationale + copy-paste blocks per step, verifications
included. A workbook always matches what automation would run for the same
configuration.

```bash
python3 tools/obsdeploy workbook --out workbook.md        # secrets as {{KEY}} placeholders
python3 tools/obsdeploy workbook --with-secrets --out wb.md  # real secrets → 0600 file
```

## Alerts (Telegram)

Set in the vars file and re-render:

```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789        # numeric chat id
```

With the token unset, Alertmanager runs a placeholder receiver (alerts visible
in Prometheus/AM UI, nothing delivered). Routing: grouped by
alertname+instance, repeat 4h, resolved messages sent.

## CloudWatch (YACE) — prepared, off by default

1. Set `YACE_ENABLED=true` and `YACE_ACCOUNTS_FILE=/abs/path/accounts.yaml`
   (schema documented in `stack/yace/config.yaml.tmpl`), plus optional
   `YACE_AWS_*` credential vars.
2. Re-render, then: `cd stack && docker compose --profile cloudwatch up -d yace`.

The profile keeps the container out of the default bring-up; a scrape target
file is rendered only when enabled.

## Backups

Set `BACKUP_DIR` (optionally `BACKUP_KEEP`, default 14) in the vars file —
the `backup` phase then joins the default hub run and installs a daily
systemd timer (04:30, randomized): tar of `stack/data/` + a copy of the vars
file (the only credential store), 0600. Manual: `tools/backup.sh`.

## Deployment contract

One vars file per deployment (`deployments/<site>.env`) is the full contract:
project name, versions, ports, retention tiers, alert receiver. Secrets are
generated once by `tools/render.sh` and appended to that file — back the vars
file up; it is the single backup target for credentials. For a remote hub set
`HUB_DIR` (where the repo is cloned on the hub); for local runs it is inferred.

Knobs (defaults in `tools/render.sh`): `SCRAPE_INTERVAL=60s`,
`TSDB_RETENTION=48h` (prometheus local), `RETENTION_RAW/5M/1H`,
`LOKI_RETENTION=30d`, `BIND_IP` (bind every published port to the tailnet IP
on real hubs; `127.0.0.1` for local tests), `GARAGE_CAPACITY` (layout
capacity, suffix required — plain integers are bytes), `BACKUP_DIR`/`BACKUP_KEEP`,
`TELEGRAM_*`, `YACE_*`.

## What is verified (Phase 1, on this machine)

- Full metrics path: Prometheus → sidecar → Garage bucket → store-gateway →
  thanos-query; all 11 scrape targets `up` (incl. Garage's own metrics).
- Logs: Alloy → Loki with tenant isolation (`X-Scope-OrgID`; wrong tenant
  sees nothing). Grafana datasources provisioned from files (Thanos default,
  Loki with tenant header).
- Downsampling: compactor automatically produced **5m-resolution blocks**
  from 2d-range compacted raw blocks (verified via `tools bucket inspect`).
- Garage: single-node layout, bucket, imported key, read/write grants.
- Deployment tooling: `obsdeploy hub` full chain against the live stack
  (verify sweep PASS), `agent`/`backup` dry-runs, workbook rendering with
  secret masking.
- Alerts: Telegram receiver path rendered and validated with `amtool
  check-config` (placeholder receiver used until `TELEGRAM_*` vars are set).
- Grafana: "Node basics" + "Hub overview" dashboards provisioned from files.
- Backups: `tools/backup.sh` run for real against the local stack (0600
  tarball + vars copy, retention pruning).

### Expected but not yet verified here

- **1h tier**: requires a compacted raw block with range ≥ 10d AND a 5m block
  with range ≥ 10d. In this synthetic test the Thanos planner did not merge
  raw blocks beyond the 2d level (v0.42 planner excludes the newest block;
  see Thanos `tsdbBasedPlanner.Plan`). With real continuous ingest this is
  the standard ladder (2h → 8h → 2d → 14d → 60d); confirm during Phase 2/3
  soak that 14d-range blocks form. **If raw compaction truly stops at 2d in
  v0.42, the 1h tier never forms — open question, tracked for the soak test.**
- **SSH device enrollment** (`obsdeploy agent` over a real remote host) was
  dry-run verified only — no spare device was available. The workbook renders
  the exact commands for a manual first run.

## Test data procedure (used for Phase 1 verification)

`tmp/gen_testdata.py` generates 2 synthetic series inside one complete 14d
Thanos window; promtool cuts them into blocks; rclone uploads them; the
compactor runs one-shot passes. Clean up by purging the bucket objects
(rclone per-directory `purge`; `rclone delete` does NOT descend into
"directories", `purge` on the bucket root fails with 403 by design — the key
is read/write only):

```bash
# inside an rclone container configured for the garage endpoint:
rclone lsd g:thanos --max-depth 1 | awk '{print $NF}' | while read u; do rclone purge "g:thanos/$u"; done
```

Test 5m blocks (~100 KB) age out via `RETENTION_5M` eventually; purge them
manually if you want a pristine bucket.

## Gotchas learned during bring-up (kept so you don't re-learn them)

- **Thanos consistency delay (30m default)**: components ignore blocks whose
  ULID timestamp (or `thanos.uploadTime`) is younger than 30m. Synthetic
  promtool-cut blocks are "fresh" until then — for tests pass
  `--consistency-delay=1s` to one-shot compactor runs, or just wait.
- **envsubst eats Go templates**: the renderer substitutes only UPPER_CASE
  variables; `$labels`/`$value` in alert annotation templates must survive.
- Prometheus 3.x: `external_labels` lives **under `global:`**.
- Thanos 0.42 flag renames: retention is `--retention.resolution-raw/5m/1h`
  (dash, not dot); query uses `--endpoint=` (`--store` is gone); compact
  needs `--storage.tsdb.min-block-duration=2h --storage.tsdb.max-block-duration=2h`
  on the Prometheus side for the sidecar to serve StoreAPI.
- Downsampling thresholds are about the **block's own range** (≥40h → 5m,
  ≥10d → 1h), not its age.
- Garage v2: layout capacity needs a suffix (`-c 10G`); `key import` requires
  `--yes`; the docker image has no shell (drive CLI via `compose exec`).
- quay.io rejects anonymous pulls from this host → Thanos images come from
  the project's official Docker Hub mirror `thanosio/thanos`.
- `minio/mc` images were pulled from Docker Hub (MinIO 2025 community
  retreat) → rclone is the bucket swiss army knife here; note `rclone delete`
  doesn't descend into S3 "directories" and `purge` on the bucket root needs
  delete-bucket permission.
- grafana/alloy image: `/var/lib/alloy` is `root:alloy 2770` inside the image
  — mount the state volume **over `/var/lib/alloy` itself**, not a subdir.
- Alloy tenancy: set `__tenant_id__` in a pipeline `loki.relabel` (sources
  drop `__`-labels from discovery targets; `loki.write` has no tenant_id arg).
- docker compose does not recreate containers when only bind-mounted file
  *content* changes — `docker compose restart <svc>` after re-render.

## Remaining roadmap

- Soak test on real continuous ingest: confirm 14d-range raw blocks form
  (→ 1h tier); if v0.42 truly caps raw compaction at 2d, either accept
  5m-only "trends" or evaluate pinning Thanos to a version with the classic
  range ladder.
- Optional meta-Grafana (laptop/tiny VPS) adding each hub's thanos-query and
  Loki as datasources over the tailnet — global view without weakening
  per-project isolation.
- thanos-ruler profile (recording/alerting rules over bucket data beyond the
  48h local window) — component list already anticipated it.
