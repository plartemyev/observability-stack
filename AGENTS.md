# Project conventions (observability-stack)

- Any Docker image we build ourselves must be based on `archlinux:latest`
  (user directive, 2026-10-18). Upstream images are pinned by exact tag in
  `deployments/<site>.env`; bump deliberately.
- Never use `/tmp/` — scratch files go to `tmp/` in the repo root (gitignored).
- Configs are always rendered, never hand-edited: `tools/render.sh
  deployments/<site>.env` (templates are `stack/**/*.tmpl`; the renderer
  substitutes only UPPER_CASE variables so Go templates survive).
- After re-render, `docker compose restart <service>` — compose does not
  recreate on bind-file content changes.
- Verification before claims: probe upstream behavior (config references,
  changelogs, source at the pinned tag) instead of trusting memory; grafana
  mimir docs/changelogs and thanos flags have both bitten this project.
- Registry note: thanos images use docker.io/thanosio/thanos (quay.io denies
  anonymous pulls from this host).
