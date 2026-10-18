"""Render the step model as a markdown workbook (archinstaller-style):
the same steps the runner executes, as copy-paste blocks with rationale."""

from datetime import date

from .runner import substitute


def _fence(content: str, lang: str = "bash") -> str:
    return f"```{lang}\n{content.rstrip()}\n```"


def render(phases, cfg: dict[str, str], title: str, vars_path: str,
           with_secrets: bool) -> str:
    out = [f"# {title}", "",
           f"Rendered from `{vars_path}` on {date.today().isoformat()}. "
           "Every block below is exactly what `tools/obsdeploy` would execute "
           "for this configuration — run them top to bottom, in order.",
           ""]
    if not with_secrets:
        out += ["**Secrets appear as `{{KEY}}` placeholders** — substitute them "
                "from the vars file before pasting. Re-render with "
                "`--with-secrets` (file then contains real secrets; treat it "
                "as 0600 and rotate when done).", ""]
    else:
        out += ["**This workbook embeds real secrets** — mode 0600, rotate "
                "credentials once convenient.", ""]

    for phase in phases:
        out += [f"## Phase `{phase.pid}` — {phase.title}", ""]
        for step in phase.steps:
            out += [f"### {step.sid} — {step.title}", "", step.why, ""]
            for remote_path, content in step.files:
                out += [f"Write `{remote_path}`:",
                        _fence(f"cat > {remote_path} <<'EOF'\n{substitute(content, cfg)}\nEOF"), ""]
            if step.cmds:
                out += [_fence("\n".join(substitute(c, cfg) for c in step.cmds)), ""]
            if step.verify:
                v = substitute(step.verify, cfg)
                expect = f"  (expect: `{step.expect}`)" if step.expect else ""
                out += [f"Verify{expect}:", _fence(v), ""]

    return "\n".join(out) + "\n"
