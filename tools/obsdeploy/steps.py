"""The step model — single source of truth for both automation and workbook.

Each step is executable exactly as rendered: the runner runs `cmds` in order
(fail-fast) against the step's target, uploads `files` first; the workbook
prints the same commands and file contents as copy-paste blocks.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Step:
    sid: str                      # unique id, e.g. "hub.up"
    title: str
    why: str
    target: str                   # "local" | "hub" | "agent" — resolved by the CLI
    cmds: list[str]
    files: list[tuple[str, str]] = field(default_factory=list)  # (remote_path, local_content)
    verify: str | None = None     # command; must exit 0
    expect: str | None = None     # substring required in verify output


@dataclass
class Phase:
    pid: str
    title: str
    steps: list[Step]


def _repo_root() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parents[2])


def hub_phases(cfg: dict[str, str]) -> list[Phase]:
    """Hub deployment phases. cfg = real vars (automation) or masked (workbook)."""
    v = cfg
    root = v.get("HUB_DIR") or _repo_root()   # where the repo lives on the hub
    stack = f"{root}/stack"
    bind, qport = v["BIND_IP"], v["QUERY_PORT"]
    lport, gport = v["LOKI_PORT"], v["GRAFANA_PORT"]
    bport, proj = v["BUCKET_WEB_PORT"], v["PROJECT_NAME"]
    varsf = v["_VARS_PATH"]

    render = Phase("render", "Render configurations", [
        Step("hub.render", "Render stack configs from the vars file",
             "Templates in stack/**.tmpl are filled with this deployment's values; "
             "secrets are generated once and appended to the vars file.",
             "local", [f"{root}/tools/render.sh {varsf}"],
             verify=f"test -f {stack}/.env && test -f {stack}/thanos/bucket.yaml"),
    ])

    up = Phase("up", "Bring up the hub stack", [
        Step("hub.up", "docker compose up (tools profile included)",
             "Starts prometheus, thanos (sidecar/query/store/compact), garage, "
             "loki, alloy, grafana, alertmanager, node-exporter, bucket-web.",
             "hub", [f"cd {stack} && docker compose --profile tools up -d"],
             verify=f"curl -fsS http://{bind}:{qport}/-/healthy",
             expect="ealthy"),
    ])

    init = Phase("init", "Bootstrap Garage (one-time, idempotent)", [
        Step("hub.garage-init", "Assign cluster layout, create bucket, import key",
             "Garage needs a layout assignment and an S3 key before Thanos can "
             "upload blocks. Safe to re-run.",
             "hub", [f"{root}/tools/garage-init.sh {varsf}"],
             verify=f"cd {stack} && docker compose exec -T garage /garage bucket list | grep -w {v['S3_BUCKET']}"),
    ])

    verify = Phase("verify", "Verify the hub end to end", [
        Step("hub.verify", "Sweep every hub endpoint",
             "Confirms query, loki (with this project's tenant), grafana, "
             "alertmanager and bucket-web answer, and that scrape targets exist. "
             "curl retries ride out readiness blips.",
             "hub", [
                 f"curl -fsS --retry 3 --retry-delay 2 --retry-all-errors http://{bind}:{qport}/-/healthy",
                 f"curl -fsS --retry 3 --retry-delay 2 --retry-all-errors http://{bind}:{lport}/ready",
                 f"curl -fsS --retry 3 --retry-delay 2 --retry-all-errors http://{bind}:{gport}/api/health",
                 f"curl -fsS --retry 3 --retry-delay 2 --retry-all-errors -H 'X-Scope-OrgID: {proj}' http://{bind}:{lport}/loki/api/v1/labels",
                 f"curl -fsS --retry 3 --retry-delay 2 --retry-all-errors http://{bind}:{bport}/-/healthy",
                 f"curl -s http://{bind}:{qport}/api/v1/label/job/values | grep -q node-exporter",
             ],
             verify=f"curl -fsS http://{bind}:{qport}/-/healthy"),
    ])

    backup = Phase("backup", "Install the backup timer (optional phase)", [
        Step("hub.backup", "Install obs-backup.service + .timer",
             f"Daily tar of stack/{v.get('DATA_DIR', './data')} plus the vars file "
             f"(the only credential store) into {v.get('BACKUP_DIR', '')}, "
             f"keeping {v.get('BACKUP_KEEP', '14')} generations.",
             "hub", [
                 f"install -Dm755 {root}/tools/backup.sh /usr/local/bin/obs-backup",
                 "install -Dm644 /tmp/obs-backup.service /etc/systemd/system/obs-backup.service",
                 f"install -Dm644 {stack}/systemd/obs-backup.timer /etc/systemd/system/obs-backup.timer",
                 "systemctl daemon-reload && systemctl enable --now obs-backup.timer",
             ],
             files=[("/tmp/obs-backup.service", PathSafe(
                 f"{stack}/systemd/obs-backup.service.tmpl").fill(
                 BACKUP_DIR=v.get("BACKUP_DIR", ""),
                 BACKUP_KEEP=v.get("BACKUP_KEEP", "14"),
                 VARS_PATH=f"{root}/deployments/{Path(varsf).name}"))],
             verify="systemctl is-enabled obs-backup.timer",
             expect="enabled"),
    ])

    phases = [render, up, init, verify]
    if v.get("BACKUP_DIR"):
        phases.append(backup)
    return phases


def agent_phases(cfg: dict[str, str], agent_name: str, device_addr: str,
                 hub_addr: str) -> list[Phase]:
    """Device enrollment phases. Runs over SSH to the agent; registration
    targets the hub. Arch is probed at run time ({{ARCH}} in workbook mode)."""
    v = cfg
    root = _repo_root()
    stack = f"{root}/stack"
    bind, qport = v["BIND_IP"], v["QUERY_PORT"]
    lport = v["LOKI_PORT"]

    ne_unit = PathSafe(f"{stack}/agent/node-exporter.service.tmpl")
    alloy_cfg = PathSafe(f"{stack}/agent/device.alloy.tmpl")
    alloy_unit = PathSafe(f"{stack}/agent/alloy.service.tmpl")

    probe = Phase("probe", "Probe the device", [
        Step("agent.probe", "Detect machine architecture and init system",
             "Install steps need ARCH (amd64/arm64) and systemd. "
             "Workbook mode prints the probe commands instead of pinning values.",
             "agent", [
                 "uname -m",
                 "test -d /run/systemd/system && echo systemd-ok",
                 "test \"$(id -u)\" = 0 || echo 'not root — commands below need root'",
             ]),
    ])

    nodeexp = Phase("node-exporter", "Install node_exporter", [
        Step("agent.node-exporter", "Install binary + systemd unit",
             "Official release binary, pinned by NODE_EXPORTER_VERSION; "
             "unprivileged user, hardened unit. The hub scrapes it (pull).",
             "agent", [
                 'V={{NODE_EXPORTER_VERSION}}; A={{ARCH}}; d=$(mktemp -d)\n'
                 'curl -fsSL "https://github.com/prometheus/node_exporter/releases/download/v${V}/node_exporter-${V}.linux-${A}.tar.gz" | tar xz -C "$d"\n'
                 'id node_exporter >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin node_exporter\n'
                 'install -m755 "$d/node_exporter-${V}.linux-${A}/node_exporter" /usr/local/bin/node_exporter\n'
                 'rm -rf "$d"',
                 "install -Dm644 /tmp/obs-node-exporter.service /etc/systemd/system/node_exporter.service",
                 "systemctl daemon-reload && systemctl enable --now node_exporter",
             ],
             files=[("/tmp/obs-node-exporter.service", ne_unit.real(v))],
             verify="curl -fsS localhost:9100/metrics | head -1",
             expect="# HELP"),
    ])

    alloy = Phase("alloy", "Install Alloy (logs)", [
        Step("agent.alloy", "Install binary + config + systemd unit",
             "Ships journald to the hub's Loki as tenant PROJECT_NAME. "
             "Needs unzip (installed best-effort via the distro package manager).",
             "agent", [
                 'V={{ALLOY_VERSION}}; A={{ARCH}}; d=$(mktemp -d)\n'
                 'command -v unzip >/dev/null 2>&1 || {\n'
                 '  if command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq unzip\n'
                 '  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q unzip\n'
                 '  elif command -v pacman >/dev/null 2>&1; then pacman -Sy --noconfirm unzip\n'
                 '  else echo "no known package manager: install unzip manually" >&2; exit 1; fi; }\n'
                 'curl -fsSL "https://github.com/grafana/alloy/releases/download/v${V}/alloy-linux-${A}.zip" -o "$d/alloy.zip"\n'
                 'unzip -o "$d/alloy.zip" -d "$d" >/dev/null\n'
                 'install -m755 "$(find "$d" -type f -name "alloy*" | head -1)" /usr/local/bin/alloy\n'
                 'mkdir -p /etc/alloy && rm -rf "$d"',
                 "install -Dm644 /tmp/obs-device.alloy /etc/alloy/config.alloy",
                 "install -Dm644 /tmp/obs-alloy.service /etc/systemd/system/alloy.service",
                 "systemctl daemon-reload && systemctl enable --now alloy",
             ],
             files=[
                 ("/tmp/obs-device.alloy", alloy_cfg.fill(
                     PROJECT_NAME=v["PROJECT_NAME"], AGENT_NAME=agent_name,
                     HUB_ADDRESS=hub_addr, LOKI_PORT=v["LOKI_PORT"])),
                 ("/tmp/obs-alloy.service", alloy_unit.real(v)),
             ],
             verify="curl -fsS localhost:12345/-/ready"),
    ])

    register = Phase("register", "Register the device on the hub", [
        Step("agent.register", "Write file_sd target + reload Prometheus",
             "Drops a targets file for this device into the hub's file_sd dir "
             "and triggers a config reload — no hub config file is edited.",
             "hub", [
                 f"curl -fsS -XPOST http://{bind}:{qport}/-/reload",
             ],
             files=[(
                 f"{stack}/prometheus/targets/{agent_name}.yml",
                 f"- targets:\n    - '{device_addr}'\n  labels:\n"
                 f"    instance: '{agent_name}'\n    project: '{v['PROJECT_NAME']}'\n",
             )],
             verify=f"curl -s http://{bind}:{qport}/api/v1/targets | grep -c '{device_addr}'",
             expect="1"),
    ])

    return [probe, nodeexp, alloy, register]


def PathSafe(path: str):
    """Tiny template helper: render a .tmpl with UPPER_CASE {{KEY}} substitution."""
    from pathlib import Path
    import re

    class _T:
        def __init__(self, p: str):
            self.p = Path(p)

        def real(self, v: dict[str, str]) -> str:
            return self.fill(**v)

        def fill(self, **kw) -> str:
            text = self.p.read_text()
            def sub(m):
                return kw.get(m.group(1), m.group(0))
            return re.sub(r"\{\{([A-Z0-9_]+)\}\}", sub, text)

    return _T(path)
