"""Execute the step model: local or over SSH, fail-fast, with verifications."""

import re
import subprocess
import sys
from pathlib import Path

ARCH_MAP = {"x86_64": "amd64", "aarch64": "arm64"}


def substitute(text: str, cfg: dict[str, str]) -> str:
    """Fill {{KEY}} placeholders; unknown keys stay (e.g. {{ARCH}} pre-probe)."""
    return re.sub(r"\{\{([A-Z0-9_]+)\}\}", lambda m: cfg.get(m.group(1), m.group(0)), text)


class Runner:
    def __init__(self, targets: dict[str, str | None], dry: bool = False):
        self.targets = targets          # {"local": None, "hub": ssh|None, "agent": ssh}
        self.dry = dry

    def _argv(self, target: str, cmd: str) -> list[str]:
        t = self.targets.get(target)
        if target == "local" or not t:
            return ["bash", "-ec", cmd]
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", t, cmd]

    def sh(self, target: str, cmd: str, quiet: bool = False) -> tuple[int, str]:
        if self.dry:
            print(f"    [{target}] $ {cmd}")
            return 0, ""
        r = subprocess.run(self._argv(target, cmd), capture_output=True, text=True)
        if not quiet:
            for line in r.stdout.strip().splitlines():
                print(f"    | {line}")
        if r.returncode != 0 and r.stderr.strip():
            for line in r.stderr.strip().splitlines()[-5:]:
                print(f"    ! {line}", file=sys.stderr)
        return r.returncode, r.stdout

    def upload(self, target: str, remote_path: str, content: str) -> int:
        if self.dry:
            print(f"    [{target}] write {len(content)}B -> {remote_path}")
            return 0
        t = self.targets.get(target)
        if target == "local" or not t:
            p = Path(remote_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return 0
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", t, f"cat > {remote_path}"],
            input=content, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"    ! upload to {remote_path} failed: {r.stderr}", file=sys.stderr)
        return r.returncode

    def probe_arch(self, target: str) -> str:
        rc, out = self.sh(target, "uname -m", quiet=True)
        return ARCH_MAP.get(out.strip(), "amd64") if rc == 0 else "amd64"

    def run_phase(self, phase, cfg: dict[str, str]) -> bool:
        print(f"\n=== phase {phase.pid}: {phase.title}")
        for step in phase.steps:
            print(f"--- {step.sid}: {step.title}")
            for remote_path, content in step.files:
                if self.upload(step.target, remote_path, content) != 0:
                    return False
            for cmd in step.cmds:
                rc, _ = self.sh(step.target, substitute(cmd, cfg))
                if rc != 0:
                    print(f"  FAILED: {step.sid} — aborting", file=sys.stderr)
                    return False
            if step.verify:
                if self.dry:
                    print(f"    [{step.target}] verify? {substitute(step.verify, cfg)}")
                    continue
                rc, out = self.sh(step.target, substitute(step.verify, cfg), quiet=True)
                ok = rc == 0 and (step.expect is None or step.expect in out)
                print(f"  verify: {'PASS' if ok else 'FAIL'}")
                if not ok:
                    return False
        return True

    def run(self, phases, cfg: dict[str, str], only: str | None = None) -> bool:
        selected = [p for p in phases if only is None or p.pid == only]
        if only and not selected:
            known = ", ".join(p.pid for p in phases)
            print(f"unknown phase '{only}' (known: {known})", file=sys.stderr)
            return False
        for phase in selected:
            if not self.run_phase(phase, cfg):
                return False
        print("\nall selected phases OK")
        return True
