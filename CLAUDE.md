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

## What obs-plane is

The **observability tier** of the nos-tromo federation: a Docker Compose
project of pulled, digest-pinned images (Prometheus + Grafana + Loki +
Grafana Alloy + node-exporter + cAdvisor + blackbox-exporter) that scrapes
metrics and collects logs from the rest of the host. It is a **pure
consumer** — it joins the two shared external networks (`inference-net`,
`data-net`) read-only to scrape/probe targets by alias, owns nothing any
other member depends on, and requires **zero changes to any other
federation repo**. For how this tier slots into the wider workspace
(inference vs state vs apps vs observability, bring-up order), see the
parent `../CLAUDE.md`.

No application code. No Python venv, no test suite, no linter beyond
config validation. The whole repo is a `Makefile`, two compose files under
`docker/`, service configs under `prometheus/`, `loki/`, `alloy/`,
`blackbox/`, `grafana/`, and an airgap bundler under `scripts/`.

## Load-bearing invariants

- **Image pins are `tag@digest`, never floating.** Every `image:` in
  `docker/compose.yaml` is an explicit release tag **and** digest
  (`repo:vX.Y.Z@sha256:...`). Never `latest`, `stable`, `main`, or an rc
  tag. Bumping a version means re-resolving tag and digest together.
- **No runtime fetching, no telemetry.** Grafana's analytics/update-check
  env flags are off (`GF_ANALYTICS_REPORTING_ENABLED`,
  `GF_ANALYTICS_CHECK_FOR_UPDATES`, `GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES`,
  `GF_NEWS_NEWS_FEED_ENABLED`), no Grafana plugins are installed, Loki runs
  with `analytics.reporting_enabled: false`, and Alloy runs with
  `--disable-reporting`. Dashboard JSON is committed, never fetched.
- **No host ports in the base compose.** `docker/compose.yaml` is
  production-shape and publishes nothing; `docker/compose.override.yaml`
  is the dev-only overlay that publishes Grafana
  (`${GRAFANA_HOST_PORT:-3001}`) and is layered only via `make up-dev`.
- **`make nuke` is the only volume destroyer.** `prometheus-data`,
  `loki-data`, `grafana-data` are declared `external`, so `docker compose
  down -v` here cannot remove them. Only the interactive `make nuke`
  (type `nuke` to confirm) deletes them by name.

## Commands

```bash
make network                  # create external inference-net + data-net (idempotent)
make volumes                  # create external prometheus-data/loki-data/grafana-data (idempotent)
make pull                     # pull all images
make up                       # production shape — no host ports
make up-dev                   # layers docker/compose.override.yaml — publishes Grafana on the host
make down / make restart      # stop (volumes preserved) / down + up
make ps / make health         # service state / readiness + all scrape targets up
make logs S=prometheus        # tail one service (omit S= to tail all)
make bundle                   # airgap tarball from the latest release tag
make nuke                     # DESTROY all volumes (interactive: type 'nuke' to confirm)
```

`make bundle` checks out the latest annotated release tag and bundles
that. To bundle the current working tree instead, set
`OBS_PLANE_VERSION_OVERRIDE=<version>` — this repo keeps a bespoke
Makefile (data-plane pattern), so there is no separate `bundle-dev`
target.

`make health` runs every check **from inside the prometheus container**
(busybox `wget`): Prometheus `/-/ready`, then `loki:3100/ready` and
`grafana:3000/api/health` over the project-internal network, then the
Prometheus targets API — it fails if any scrape target is down.

Config-validation one-liners run in CI (`validate-configs` job) against
the exact pinned images, and are safe to run locally the same way:

```bash
docker run --rm -v "$PWD/prometheus:/etc/prometheus:ro" --entrypoint promtool \
  docker.io/prom/prometheus:v3.13.1@sha256:3c42b892cf723fa54d2f262c37a0e1f80aa8c8ddb1da7b9b0df9455a35a7f893 \
  check config /etc/prometheus/prometheus.yml

docker run --rm -v "$PWD/loki/loki.yaml:/etc/loki/loki.yaml:ro" \
  docker.io/grafana/loki:3.7.4@sha256:87f0a067673756a3cede1bcbf0c74875f7df9b09fddb53e399d0c576f756cfcc \
  -config.file=/etc/loki/loki.yaml -config.expand-env=true -verify-config

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

docker run --rm -v "$PWD/blackbox/blackbox.yml:/etc/blackbox/blackbox.yml:ro" --entrypoint /bin/blackbox_exporter \
  docker.io/prom/blackbox-exporter:v0.28.0@sha256:e753ff9f3fc458d02cca5eddab5a77e1c175eee484a8925ac7d524f04366c2fc \
  --config.file=/etc/blackbox/blackbox.yml --config.check
```

`docker compose config` is validated the same way, seeded from
`.env.example` (the shared `infra-validation` reusable workflow's
compose-config sub-job only stubs `NEO4J_PASSWORD`/`OPENAI_API_KEY`, not
this repo's required `GRAFANA_ADMIN_PASSWORD`, so obs-plane validates
compose itself in `validate-configs` rather than via that shared job).

## Pointers

- Design: `docs/2026-07-22-obs-plane-design.md` — scope decisions, service
  table, dashboards, alert rules, follow-ups.
- Federation bring-up and network seams: `../deploy/README.md`.
- Workspace-wide conventions and the ten-project map: the infra root
  `../CLAUDE.md`.
