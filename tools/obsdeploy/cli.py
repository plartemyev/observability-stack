"""CLI: tools/obsdeploy [hub|agent|verify|workbook|phases].

Automation and workbook share one step model (steps.py) — a workbook always
matches what the runner would execute for the same configuration.
"""

import argparse
import sys
from pathlib import Path

from . import steps, workbook
from .runner import Runner
from .vars import load_vars, masked, require

ROOT = Path(__file__).resolve().parents[2]

EPILOG = """
targets:
  --hub  omit      run hub steps locally (this machine is the hub)
  --hub  user@host run hub steps over SSH (repo must exist at HUB_DIR on the hub)
  --agent root@dev run agent steps over SSH as root

device enrollment flags (--agent command):
  --agent-name  short name, used as instance label and targets filename
  --device-addr how the HUB reaches the device (host:9100)
  --hub-addr    how the DEVICE reaches the hub (tailnet host/IP)
"""

HUB_REQUIRED = ("PROJECT_NAME", "BIND_IP", "QUERY_PORT", "LOKI_PORT",
                "GRAFANA_PORT", "BUCKET_WEB_PORT", "S3_BUCKET")


_OPTS = [
    (("--vars",), dict(default=str(ROOT / "deployments/local.env"),
                       help="deployment vars file")),
    (("--hub",), dict(default=None, help="ssh target for hub steps")),
    (("--agent",), dict(default=None, help="ssh target for device steps")),
    (("--agent-name",), dict(default=None)),
    (("--device-addr",), dict(default=None)),
    (("--hub-addr",), dict(default=None)),
    (("--dry-run",), dict(action="store_true", help="print plan, touch nothing")),
    (("--with-secrets",), dict(action="store_true",
                               help="workbook: inline real secrets (0600)")),
    (("--out",), dict(default=None, help="workbook: output file")),
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="obsdeploy", epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    for args, kw in _OPTS:
        p.add_argument(*args, **kw)

    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("hub", "deploy/verify the hub"),
                        ("agent", "enroll a device"),
                        ("workbook", "render the markdown workbook")):
        sp = sub.add_parser(name, help=help_,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        # SUPPRESS so flags given before the subcommand are not clobbered
        for args, kw in _OPTS:
            kw2 = {k: v for k, v in kw.items() if k != "default"}
            sp.add_argument(*args, default=argparse.SUPPRESS, **kw2)
        sp.add_argument("phase", nargs="?", default=None,
                        help="run a single phase (see `phases`)")
    sub.add_parser("phases", help="list available phases")
    sub.add_parser("verify", help="hub verification sweep only")
    return p


def _hub_cfg(cfg: dict[str, str]) -> dict[str, str]:
    require(cfg, *HUB_REQUIRED)
    return cfg


def _agent_args(a) -> tuple[str, str, str]:
    missing = [n for n, v in (("agent", a.agent), ("--agent-name", a.agent_name),
                              ("--device-addr", a.device_addr),
                              ("--hub-addr", a.hub_addr)) if not v]
    if missing:
        raise SystemExit(f"agent command needs: {', '.join(missing)}")
    return a.agent_name, a.device_addr, a.hub_addr


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    cfg = load_vars(a.vars)
    cfg["_VARS_PATH"] = a.vars

    if a.cmd == "workbook":
        run_cfg = cfg if a.with_secrets else masked(cfg)
        hub = steps.hub_phases(run_cfg)
        agent = steps.agent_phases(run_cfg, "{{AGENT_NAME}}",
                                   "{{DEVICE_ADDR}}", "{{HUB_ADDRESS}}")
        phases = hub + agent if a.phase in (None, "all") else (
            hub if a.phase == "hub" else agent)
        md = workbook.render(phases, run_cfg,
                             f"observability-stack workbook — {cfg['PROJECT_NAME']}",
                             a.vars, a.with_secrets)
        if a.out:
            Path(a.out).write_text(md)
            print(f"workbook: {a.out}")
        else:
            sys.stdout.write(md)
        return 0

    runner = Runner({"local": None, "hub": a.hub, "agent": a.agent}, dry=a.dry_run)

    if a.cmd == "phases":
        for ph in steps.hub_phases(cfg) + steps.agent_phases(
                cfg, "NAME", "ADDR:9100", "HUBADDR"):
            print(f"{ph.pid:14s} {ph.title}")
        return 0

    if a.cmd == "verify":
        return 0 if runner.run(steps.hub_phases(cfg), cfg, only="verify") else 1

    if a.cmd == "hub":
        phases = steps.hub_phases(_hub_cfg(cfg))
        return 0 if runner.run(phases, cfg, only=a.phase) else 1

    if a.cmd == "agent":
        name, addr, hubaddr = _agent_args(a)
        acfg = dict(cfg)
        if not a.dry_run:
            acfg["ARCH"] = runner.probe_arch("agent")
        phases = steps.agent_phases(acfg, name, addr, hubaddr)
        return 0 if runner.run(phases, acfg, only=a.phase) else 1

    return 2
