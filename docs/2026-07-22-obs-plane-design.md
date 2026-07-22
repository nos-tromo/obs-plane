# obs-plane — federation observability plane (v1 design)

Status: approved design, pre-implementation
Date: 2026-07-22
Scope: new repository `nos-tromo/obs-plane`

## Purpose

The federation has ordered bring-up, health gates, external volumes, and
backup runbooks — but once running, no answer to "is production healthy,
and what happened at 3am?" beyond `docker ps` and per-container
`docker logs`. On an airgapped single host there is no cloud APM to fall
back on; if the federation doesn't ship its own observability, none
exists.

obs-plane is a new federation member in the `data-plane` mold: a pure
Docker Compose project of pulled, digest-pinned images, bundleable for
airgap, owning its own external volumes, joining the shared external
networks read-only as a scraper.

**v1 is purely additive: zero changes to any other federation repo.**
Deploy-repo tier wiring, vllm-service network changes, and app
instrumentation are explicit follow-ups.

## v1 scope decisions (settled)

| Decision | Choice |
|---|---|
| Coverage | Metrics **and** logs (Prometheus + Loki) in one repo from the start |
| Grafana access | Override-only: production shape publishes no host ports; `compose.override.yaml` publishes Grafana (`make up-dev` or SSH tunnel on prod hosts) |
| Alerting | Provisioned Grafana alert rules, **no** notification channel (airgapped prod has no outbound path), no Alertmanager |
| Blast radius | obs-plane repo only; no member-repo changes |
| Stack | Prometheus + Grafana + Loki + Grafana Alloy + node-exporter + cAdvisor + blackbox-exporter |

Stack rationale: industry-standard components with the largest prebuilt
dashboard ecosystem; all free OSS images, pullable and digest-pinnable.
Promtail is deprecated (EOL 2026-03) — Alloy is its successor and the
only correct choice for a new repo in mid-2026. Alloy reads container
logs through the Docker API, which works with the `local` logging driver
the whole federation already uses, so **no fleet-wide logging-driver
change is needed**.

## What is observable in v1 — and what is not

Reachable with zero member changes:

- **Host**: node-exporter (CPU, memory, disk, network, filesystem fill).
- **Every federation container**: cAdvisor (per-container CPU, memory,
  restarts, OOM kills) via the Docker socket — covers all compose
  projects on the host, including the vLLM backends.
- **Qdrant**: native `/metrics` on `qdrant:6333` over `data-net`.
- **Black-box health**: blackbox-exporter HTTP probes of
  `vllm-router:4000/health/liveliness` (inference-net),
  `neo4j:7474` (data-net), `qdrant:6333/healthz` (data-net).
- **Logs of every container on the host**: Alloy Docker discovery →
  Loki, labeled by compose project + service.

Explicitly **not** available in v1 (documented in the repo README):

- **Neo4j internal metrics** — Prometheus/CSV metrics are Enterprise-only;
  the federation runs Community. Coverage is cAdvisor + HTTP probe only.
- **LiteLLM router `/metrics`** — enterprise-gated in current LiteLLM
  releases. Coverage is cAdvisor + liveliness probe.
- **vLLM backend `/metrics`** — the backends (`chat`, `embed`, `rerank`,
  `clip`, `asr`, `diarize`, `vad`, `gliner`) sit only on the internal
  `vllm-net`; only the router joins `inference-net`. Native vLLM metrics
  (token throughput, latency, KV-cache usage) require a one-line
  vllm-service change (attach backends to `inference-net`) — the first
  follow-up PR after v1.
- **FastAPI app metrics** — the four Python apps expose no `/metrics`;
  instrumentation (`prometheus-fastapi-instrumentator`) is a later
  follow-up.

## Services

Seven services, all lightweight next to the inference stack (Prometheus
and Loki each run in a few hundred MB at single-host scale).

| Service | Image family | Role | Special access |
|---|---|---|---|
| `prometheus` | prom/prometheus | Metrics store + scraper. Retention `${PROMETHEUS_RETENTION:-30d}` | joins `inference-net` + `data-net` |
| `grafana` | grafana/grafana-oss | UI; datasources, dashboards, alert rules all file-provisioned | — |
| `loki` | grafana/loki | Log store, filesystem backend, retention `${LOKI_RETENTION:-720h}` | — |
| `alloy` | grafana/alloy | Log collector: Docker-API discovery of all containers → Loki | `/var/run/docker.sock` (ro) |
| `node-exporter` | prom/node-exporter | Host metrics | host `/`, `/proc`, `/sys` (ro) |
| `cadvisor` | gcr.io/cadvisor/cadvisor | Per-container metrics, whole host | `/var/run/docker.sock` + host fs (ro) |
| `blackbox-exporter` | prom/blackbox-exporter | HTTP probes of federation endpoints | joins `inference-net` + `data-net` |

All images referenced as `repo:tag@sha256:...` (digest-pinned), the
federation convention. At implementation time each image is pinned to
the **latest stable release** of its project (no `latest`/`main`/rc
tags), with the digest resolved from the registry for the exact tag —
same procedure as data-plane's Neo4j/Qdrant pins. Version bumps are
ordinary PRs that re-resolve tag + digest together. All services use the shared `x-logging` `local`
driver block (obs-plane's own containers' logs are also picked up by
Alloy — it observes itself).

### Airgap hardening (no runtime fetching, no phone-home)

- Grafana: `GF_ANALYTICS_REPORTING_ENABLED=false`,
  `GF_ANALYTICS_CHECK_FOR_UPDATES=false`,
  `GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false`,
  `GF_NEWS_NEWS_FEED_ENABLED=false`; no plugin installs (none needed —
  Prometheus and Loki datasources are built in).
- Loki: `analytics.reporting_enabled: false`.
- Alloy: run with `--disable-reporting`.
- Prometheus / exporters: pull-only scrapers, nothing outbound by design.

## Networks

- obs-plane's **default project-internal network** carries
  grafana ↔ prometheus ↔ loki ↔ alloy traffic.
- `prometheus` and `blackbox-exporter` additionally join the external
  `inference-net` and `data-net` (`${INFERENCE_NET:-inference-net}`,
  `${DATA_NET:-data-net}`) to reach scrape/probe targets by alias. Both
  are read-only consumers; obs-plane claims no aliases other services
  depend on.
- **No new external network.** `make network` creates the two external
  networks if absent (idempotent, same contract as the app repos).

## Volumes

`prometheus-data`, `loki-data`, `grafana-data` — declared `external`,
created by `make volumes`, destroyed only by an interactive `make nuke`.
Same blast-radius design as data-plane: `docker compose down -v` here can
never delete history or dashboards. (Observability data is less precious
than graph/vector data, but the pattern costs nothing and keeps the
fleet-wide rule simple: *external volumes everywhere, one nuke per repo*.)

## Configuration layout

```
obs-plane/
  docker/
    compose.yaml            production shape — no host ports
    compose.override.yaml   dev overlay — publishes Grafana on ${GRAFANA_HOST_PORT:-3001}
  prometheus/
    prometheus.yml          scrape configs (static targets by alias)
  loki/
    loki.yaml               single-binary config, filesystem storage, retention
  alloy/
    config.alloy            Docker discovery → Loki, compose-project/service labels
  blackbox/
    blackbox.yml            http_2xx probe module(s)
  grafana/
    provisioning/
      datasources/          Prometheus + Loki (default: Prometheus)
      dashboards/           provider config + dashboard JSON
      alerting/             provisioned alert rules
  .env.example              copy to .env; GRAFANA_ADMIN_PASSWORD required
  Makefile                  bespoke (data-plane style)
  VERSION                   one-line semver, read by release-tag workflow
  scripts/bundle.sh         sources vendored bundle-lib.sh
  .github/workflows/        ci.yml + release-tag.yml caller
  README.md                 quick start, target matrix, not-observable list
  CLAUDE.md                 conventions + pointers
  docs/                     this design doc moves here
```

Configs are bind-mounted read-only from the repo — no config baked into
images, no image builds at all.

`.env` contract (fail-fast like data-plane's `NEO4J_PASSWORD`):
`GRAFANA_ADMIN_PASSWORD` is required (`:?` expansion); everything else
has defaults (`GRAFANA_HOST_PORT`, `PROMETHEUS_RETENTION`,
`LOKI_RETENTION`, `INFERENCE_NET`, `DATA_NET`).

## Dashboards (v1, file-provisioned)

1. **Federation overview** — the landing page: per-container up/CPU/
   memory/restart-count table across all compose projects (cAdvisor),
   blackbox probe status row (router / neo4j / qdrant), host disk-fill
   gauge. Answers "is everything healthy?" in one screen.
2. **Host** — node-exporter: CPU, memory, disk I/O + fill, network.
3. **Qdrant** — native metrics: collections, points, RPC rates, latency.
4. **Logs** — Loki panel dashboard filtered by compose project/service;
   ad-hoc search via Grafana Explore.

Dashboard JSON is committed; where an upstream community dashboard is
used as a base, it is vendored (committed) — never fetched at runtime.

## Alert rules (v1, provisioned; no notification channel)

Rules are Prometheus alerting-rule files (plus one Loki ruler rule for
the log-spike alert), committed in the repo and mounted read-only —
Grafana's unified alerting UI lists datasource-evaluated rules, so they
appear there without Grafana-side alert provisioning.

Rules surface in Grafana's alert view only — airgapped production has no
outbound delivery path. A Telegram contact point for non-airgapped
staging is a follow-up.

- Blackbox probe down (router / neo4j / qdrant) for 5m.
- Prometheus scrape target down for 5m.
- Container restart-looping (cAdvisor restart count increasing).
- Host filesystem > 85% full.
- Host memory pressure (available < 10% for 10m).
- Error-log spike: Loki query, rate of `level=error`-like lines per
  service above threshold.

## Makefile (bespoke, data-plane style)

Targets: `help` (default) · `network` · `volumes` · `up` · `up-dev` ·
`down` · `ps` · `logs [S=svc]` · `health` · `bundle` · `nuke`.

- `up` = production shape (no ports); `up-dev` adds the override
  (Grafana on host). Both detached, `--no-build` (nothing to build).
- `health` = curl Prometheus `/-/ready`, Loki `/ready`, Grafana
  `/api/health`, then query the Prometheus targets API and fail if any
  target is down — the same role data-plane's `make health` plays, and
  the local smoke-verification for the repo.
- `bundle` = airgap tarball from the latest annotated tag via the
  vendored `bundle-lib.sh` (pulled-images list only — no locally-built
  images). `OBS_PLANE_VERSION_OVERRIDE=<v>` bundles the working tree
  (bespoke Makefile — no `bundle-dev`, same as data-plane).
- `nuke` = interactive confirm, then delete the three named volumes.

## Release + CI

- **Release**: `VERSION` file + `.github/workflows/release-tag.yml`
  caller of the shared `release-tag` reusable workflow (`@v3`) —
  merge-to-main mints `vX.Y.Z`, standard federation flow.
- **CI** (`ci.yml`): yamllint + shellcheck; config validation by running
  the pinned images themselves — `promtool check config`,
  `loki -verify-config`, `alloy fmt` / `alloy validate`, and
  `docker compose config` for both shapes. No Python toolchain, no
  common.mk (bespoke member, like data-plane).

## Testing / verification

This is a config repo — no unit-test framework. Verification layers:

1. CI config validation (above) on every PR.
2. `make up-dev && make health` locally: all services healthy, all
   Prometheus targets up, Grafana reachable.
3. Manual v1 acceptance: Federation-overview dashboard shows every
   running federation container; Logs dashboard returns lines from at
   least one app container; stopping a probed service fires its alert
   rule in Grafana within the `for:` window.

## Follow-ups (explicitly not v1)

1. **deploy wiring** — obs tier in `deploy/Makefile` (bring-up after
   apps; join `ps`/`logs`/`bundle` fan-out; `wait-healthy.sh` gate).
2. **vllm-service PR** — attach vLLM backends to `inference-net` (or
   expose a metrics path) to unlock native vLLM dashboards.
3. **App instrumentation** — `prometheus-fastapi-instrumentator` in the
   four Python apps + per-app scrape jobs and dashboards.
4. **Staging notifications** — Telegram contact point (existing bot) for
   non-airgapped staging hosts.
5. **Neo4j metrics** — revisit only if the federation ever moves off
   Community edition.
