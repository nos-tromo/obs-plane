# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Data confidentiality — hard rule

**NEVER expose actual production or testing data in any file committed or
pushed to git.** This covers not only file contents but also metadata that
references real data: filenames, file descriptions, social-media account
names or handles, user identifications, sample records, log excerpts, and
screenshots. It applies everywhere git sees — source code, tests, fixtures,
docs, examples, configs, commit messages, and CI files. Use fully synthetic,
invented placeholders instead.

**Likewise, NEVER expose local filepaths from development machines** —
absolute paths or home directories such as `/Users/<name>/...`,
`/home/<name>/...`, or `C:\Users\...` — anywhere git sees. The only
permitted paths are relative project paths starting from the project's
root (e.g. `docker/compose.yaml`).

## Planning

For any non-trivial change (>1 file or any root-cause fix), present a plan and wait for approval BEFORE editing code. Do not start with Edit/Write on a fresh investigation.

## What obs-plane is

The **observability tier** of the nos-tromo federation: a Docker Compose
project of pulled, digest-pinned images (Prometheus + Grafana + Loki +
Grafana Alloy + socket-proxy + node-exporter + cAdvisor +
blackbox-exporter + dcgm-exporter, the last gated behind the `gpu`
compose profile — `COMPOSE_PROFILES=gpu` in `.env` on NVIDIA hosts
only) that scrapes
metrics and collects logs from the rest of the host. It also holds the
federation's **audit lane** (`docs/audit.md`, deploy ADR 0003): a second
Loki (`loki-audit`), a second Grafana (`grafana-audit`) and
`audit-verifier`, which re-checks the hash chains of the audit records
Alloy routes there.

It joins **all three** shared external networks. On `inference-net` and
`data-net` it is a pure consumer — it scrapes and probes targets by
alias and claims no alias of its own. On `edge-net` it is not: `grafana`
attaches there with the alias `grafana`, which edge-plane's Caddy
proxies `/grafana/*` to (`../edge-plane/caddy/Caddyfile`), and
`grafana-audit` with the alias `grafana-audit` for `/audit/*`, so both
aliases are cross-repo contracts. obs-plane needed no member-repo changes to
stand up, but the coverage it has today does depend on member-side
endpoints (`prometheus-fastapi-instrumentator` in the four apps,
LiteLLM's prometheus callback in vllm-service) and on edge-plane's
`/grafana` route. For how this tier slots into the wider workspace
(inference vs state vs apps vs observability, bring-up order), see the
parent `../CLAUDE.md`.

The one piece of code is `audit/`: `verifier.py` (stdlib Python, run
from the pinned `python` image with the directory mounted read-only) and
the vendored `audit_event.py`, with a pytest suite in `audit/tests/` — no
venv, no `pyproject.toml`; CI runs it with `uv run --no-project`. The rest
of the repo is a `Makefile`, two compose files under `docker/`, service
configs under `prometheus/`, `loki/`, `alloy/`, `blackbox/`, `dcgm/`,
`grafana/`, `grafana-audit/`, and scripts under `scripts/` (airgap bundler,
anchor/export signing, the Alloy lane check).

## Load-bearing invariants

- **Image pins are `tag@digest`, never floating.** Every `image:` in
  `docker/compose.yaml` is an explicit release tag **and** digest
  (`repo:vX.Y.Z@sha256:...`). Never `latest`, `stable`, `main`, or an rc
  tag. Bumping a version means re-resolving tag and digest together.
- **No runtime fetching, no telemetry.** Grafana's analytics/update-check
  env flags are off (`GF_ANALYTICS_REPORTING_ENABLED`,
  `GF_ANALYTICS_CHECK_FOR_UPDATES`, `GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES`,
  `GF_NEWS_NEWS_FEED_ENABLED`, `GF_PLUGINS_PREINSTALL_DISABLED`), no
  Grafana plugins are installed, Loki runs
  with `analytics.reporting_enabled: false`, and Alloy runs with
  `--disable-reporting`. Dashboard JSON is committed, never fetched.
- **No host ports in the base compose.** `docker/compose.yaml` is
  production-shape and publishes nothing; `docker/compose.override.yaml`
  is the dev-only overlay that publishes Grafana
  (`${GRAFANA_HOST_PORT:-3001}`) and is layered only via `make up-dev`.
- **`make nuke` is the only volume destroyer — and never for the audit
  trail.** `prometheus-data`,
  `loki-data`, `grafana-data`, `alloy-data` (Alloy's log-tail read
  positions — durable so recreating the container doesn't duplicate or
  drop log lines) are declared `external`, so `docker compose down -v`
  here cannot remove them. Only the interactive `make nuke` (type `nuke`
  to confirm) deletes them by name. The audit volumes (`AUDIT_VOLUMES`:
  `loki-audit-data`, `grafana-audit-data`, `audit-verifier-state`) are
  external too and deleted by no target: audit lines leave only through
  `loki-audit`'s retention.
- **Audit records never reach the ops Loki.** One stage,
  `loki.process "lane"` in `alloy/config.alloy`, marks them
  (`lane="audit"`); the audit pipeline keeps only marked lines, the ops
  pipeline drops them before redacting. `scripts/check_alloy_lanes.sh`
  (CI) runs the real config on `alloy/lanes-test/` fixtures; regenerate
  `expected.txt` with `--update` only for an intended routing change, and
  read the diff. `loki-audit` and `grafana-audit` logs are dropped from both
  lanes (their query logs carry reviewers' search terms).
- **`loki-audit` is reachable only on the internal `audit` network**
  (alloy, grafana-audit, audit-verifier). Never put the ops `grafana` or
  `prometheus` on it, and never give the ops `grafana` a datasource for it.
- **`audit/audit_event.py` is vendored verbatim** from `nos-tromo/.github`
  (`configs/audit/`); never edit it here. The hub's `validate_audit_lib.py`
  drift-checks every `audit_event.py` in the repo, so no other file may
  take that name.

## Commands

```bash
make network                  # create external inference-net + data-net + edge-net (idempotent)
make volumes                  # create the external obs + audit volumes, chown alloy-data/audit-verifier-state (idempotent)
make pull                     # pull all images
make up                       # production shape — no host ports
make up-dev                   # layers docker/compose.override.yaml — publishes Grafana on the host
make down / make restart      # stop (volumes preserved) / down + up
make ps / make health         # service state / readiness + all scrape targets up
make logs S=prometheus        # tail one service (omit S= to tail all)
make bundle                   # airgap tarball from the latest release tag
make nuke                     # DESTROY the ops volumes, not the audit ones (interactive: type 'nuke')
make audit-verify FROM= TO=   # one-shot chain check of a range (exit 1 on problems)
make audit-export FROM= TO= [SERVICE=<compose project>] [REF=<text>]   # JSONL + .sha256 in audit-exports/
make audit-anchor DAY=        # anchor-<day>.json in audit-exports/; signs with $AUDIT_SIGNING_KEY if set
```

The `audit-*` targets read their arguments from the environment (make
exports command-line variables), so quotes and spaces pass through; they
`docker compose run --rm` the `audit-verifier` service, mounting
`audit-exports/` and running as the invoking user where they write files.

`make bundle` checks out the latest annotated release tag and bundles
that. To bundle the current working tree instead, set
`OBS_PLANE_VERSION_OVERRIDE=<version>` — this repo keeps a bespoke
Makefile (data-plane pattern), so there is no separate `bundle-dev`
target.

`make health` runs every check **from inside the prometheus container**
(busybox `wget`): Prometheus `/-/ready`, then `loki:3100/ready` and
`grafana:3000/api/health` over the project-internal network, then the
Prometheus query API for `up == 0` — it fails, naming the jobs, if any
scrape target is down. The `dcgm` job is excluded from that check unless
`.env` enables the `gpu` profile. `grafana-audit` and `loki-audit` are
checked from inside `grafana-audit` instead: the audit store is on the
`audit` network, which prometheus does not join.

Config-validation one-liners run in CI (`validate-configs` job) against
the exact pinned images, and are safe to run locally the same way:

```bash
docker run --rm -v "$PWD/prometheus:/etc/prometheus:ro" --entrypoint promtool \
  docker.io/prom/prometheus:v3.13.1@sha256:3c42b892cf723fa54d2f262c37a0e1f80aa8c8ddb1da7b9b0df9455a35a7f893 \
  check config /etc/prometheus/prometheus.yml

for config in loki loki-audit; do
  docker run --rm -v "$PWD/loki/$config.yaml:/etc/loki/loki.yaml:ro" \
    docker.io/grafana/loki:3.7.4@sha256:87f0a067673756a3cede1bcbf0c74875f7df9b09fddb53e399d0c576f756cfcc \
    -config.file=/etc/loki/loki.yaml -config.expand-env=true -verify-config
done

# -verify-config above doesn't load loki/rules/*.yml (no LogQL check); confirm
# the ruler actually loads them by starting loki and querying its rules API:
docker run -d --name loki-rules-check -p 3100:3100 \
  -v "$PWD/loki/loki.yaml:/etc/loki/loki.yaml:ro" \
  -v "$PWD/loki/rules:/loki/rules/fake:ro" \
  docker.io/grafana/loki:3.7.4@sha256:87f0a067673756a3cede1bcbf0c74875f7df9b09fddb53e399d0c576f756cfcc \
  -config.file=/etc/loki/loki.yaml -config.expand-env=true
curl -sf http://localhost:3100/loki/api/v1/rules | grep ErrorLogSpike  # retry until ready
docker rm -f loki-rules-check

docker run --rm -v "$PWD/alloy/config.alloy:/cfg/config.alloy:ro" \
  docker.io/grafana/alloy:v1.18.0@sha256:491b0578c04983fd54fe99b587b6fab4404dc46d0dc16677bd6b00cc1140b308 \
  fmt /cfg/config.alloy > /dev/null

./scripts/check_alloy_lanes.sh    # which lane each fixture line lands in (Docker + python3)

# the verifier's tests (CI job audit-tests); no Docker, no network
uv run --no-project --python 3.12 --with pytest python -m pytest audit/tests -q

docker run --rm -v "$PWD/blackbox/blackbox.yml:/etc/blackbox/blackbox.yml:ro" --entrypoint /bin/blackbox_exporter \
  docker.io/prom/blackbox-exporter:v0.28.0@sha256:e753ff9f3fc458d02cca5eddab5a77e1c175eee484a8925ac7d524f04366c2fc \
  --config.file=/etc/blackbox/blackbox.yml --config.check
```

`docker compose config` is validated the same way, seeded from
`.env.example` (the shared `infra-validation` reusable workflow's
compose-config sub-job only stubs `NEO4J_PASSWORD`/`OPENAI_API_KEY`, not
this repo's required `GRAFANA_ADMIN_PASSWORD` and
`AUDIT_GRAFANA_ADMIN_PASSWORD`, so obs-plane validates compose itself in
`validate-configs` rather than via that shared job).

Loki 3.7.4 behaviours the audit lane works around:

- `deletion_mode: disabled` makes every query log a `GetCacheGenNumbers`
  warning and a "deletion is not available" error — one reason Alloy does
  not collect `loki-audit`'s logs.
- Queries about time ranges older than ~3h skip the ingester, so a line
  pushed with an older timestamp is invisible until its chunk is flushed.
  `querier.query_ingesters_within` is still accepted but has no effect; in a
  test, `POST /flush` makes such lines visible at once.
- The delete endpoint answers 403 and the rules API 404 on `loki-audit`.

## Pointers

- Reference docs index: `docs/README.md`. In particular:
  - `docs/coverage.md` — every scrape/probe target, the deliberate gaps,
    and which targets are down by design. **Authoritative** where it
    disagrees with the dated design doc.
  - `docs/grafana-access.md` — gateway path, the `X-Auth-User` trust
    assumption, dev-overlay and SSH-tunnel fallbacks.
  - `docs/hardening.md` — hardening baseline and the accepted residual
    findings (cAdvisor `privileged: true`, dcgm profiling metrics).
  - `docs/audit.md` — the audit lane: producers, routing, retention,
    access, the verifier and its problem kinds, exports, anchors, limits.
- Audit-trail design: deploy ADR 0003
  (`../deploy/docs/decisions/0003-federation-audit-trail.md`); operator
  runbook `../deploy/docs/runbooks/audit.md`.
- Design: `docs/2026-07-22-obs-plane-design.md` — scope decisions, service
  table, dashboards, alert rules, follow-ups. Its "What is observable in
  v1" list is historical; see `docs/coverage.md` for current coverage.
- Federation bring-up and network seams: `../deploy/README.md`.
- Workspace-wide conventions and the twelve-project map: the infra root
  `../CLAUDE.md`.

## Git & PR Workflow

- Never commit directly to `main`; always branch (`feat/`, `fix/`) and open a PR.
- Never create a NEW PR when an existing PR for the work is open — push additional commits to that branch.
- Release order is strict: bump VERSION file -> commit -> tag. Never tag before the VERSION bump.
- Use single, non-compound shell commands for `gh` operations (no `&&` chains); if `gh pr merge` is blocked, fall back to the GitHub MCP merge tool.

## Verification

Before claiming a check is green, run the actual command and paste the output. `git ls-files` does not cover untracked files — use `pre-commit run --all-files`. After opening a PR, confirm CI actually triggered on the latest push before declaring done.

## Communication Style

Keep changes minimal and scoped. Do not add explanatory code comments for trivial or self-evident changes. Do not overwrite existing test files with Write — use Edit to append or modify tests.
