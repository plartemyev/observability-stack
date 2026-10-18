"""Deployment vars: load a KEY=VALUE file, mask secrets for workbooks."""

import re
from pathlib import Path

_SECRET = re.compile(r"SECRET|PASSWORD|TOKEN|_KEY|^KEY")


def load_vars(path: str | Path) -> dict[str, str]:
    """Parse a deployment vars file (KEY=VALUE lines, # comments)."""
    out: dict[str, str] = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def masked(vars_: dict[str, str]) -> dict[str, str]:
    """Replace secret values with {{KEY}} placeholders (workbook default)."""
    return {
        k: ("{{" + k + "}}" if _SECRET.search(k) else v) for k, v in vars_.items()
    }


def require(cfg: dict[str, str], *keys: str) -> None:
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        raise SystemExit(f"vars missing required keys: {', '.join(missing)}")
