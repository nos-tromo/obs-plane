# obs-plane

Observability plane for the nos-tromo federation: Prometheus + Grafana +
Loki + Grafana Alloy + node-exporter + cAdvisor + blackbox-exporter +
dcgm-exporter (GPU hosts), all pulled and digest-pinned, bundleable for
airgap. A pure consumer in the
`data-plane` mold — it owns its own volumes, joins the shared external
networks read-only as a scraper, and makes zero changes to any other
federation member.

## What lives here

Nine services, all lightweight next to the inference stack.

| Service | Role | Network membership |
|---|---|---|
| `prometheus` | Metrics store + scraper, retention `${PROMETHEUS_RETENTION:-30d}` | project-internal + `inference-net` + `data-net` |
| `grafana` | UI; datasources, dashboards, alert rules all file-provisioned | project-internal + `edge-net` (served at `/grafana/` behind the edge gateway) |
| `loki` | Log store, filesystem backend, retention `${LOKI_RETENTION:-720h}` | project-internal only |
| `socket-proxy` | Restricted read-only gateway to the Docker API for Alloy (only `GET /containers`, `/networks`, `/events`, `/_ping` enabled; every mutating/sensitive endpoint denied) | project-internal only; the sole Alloy-path holder of `/var/run/docker.sock` (ro) |
| `alloy` | Log collector: Docker-API discovery of every container's logs → Loki, log-tail positions persisted to `alloy-data` (prevents duplicate/lost lines on recreate); runs as its internal uid 473 | project-internal only; reaches the Docker API via `socket-proxy:2375` — no socket mount |
| `node-exporter` | Host metrics (CPU, memory, disk, network, filesystem fill) | internal (default) network |
| `cadvisor` | Per-container metrics for every compose project on the host | internal (default) network |
| `blackbox-exporter` | HTTP probes of federation endpoints | project-internal + `inference-net` + `data-net` |
| `dcgm-exporter` | NVIDIA GPU metrics (utilization, VRAM, temperature, power, clocks, ECC); runs only when the `gpu` compose profile is enabled (`COMPOSE_PROFILES=gpu` in `.env`, GPU hosts with the NVIDIA container toolkit) | internal (default) network |

`prometheus` and `blackbox-exporter` join the two shared external
networks (to reach scrape/probe targets by alias); `grafana` additionally
joins `edge-net` so the edge gateway can reach it — the rest stay on the
project-internal default network. obs-plane claims no alias other
services depend on — it is a read-only consumer of all three seams.

## Container hardening & residual findings (deploy ADR 0001)

Every service runs with `no-new-privileges` and `cap_drop: ALL`;
`socket-proxy`, `node-exporter`, `blackbox-exporter` and `dcgm-exporter`
additionally run read-only. Alloy no longer mounts the Docker socket — it reaches the API
through `socket-proxy`, which enables only the read-only endpoints Alloy
needs and rejects everything else (403), and it runs as its internal
uid 473 (`make volumes` chowns `alloy-data` accordingly; on hosts with an
existing volume re-run `make volumes` once).

**Residual finding, deliberately accepted:** `cadvisor` remains
`privileged: true` with `/var/run` (which includes the Docker socket) and
`/var/lib/docker` mounted read-only — its documented requirement for full
per-container stats. This is the one remaining socket exposure in the
stack; it is accepted in exchange for federation-wide container metrics
and revisited only if an assessment rejects it (see deploy ADR 0001's
reversal triggers).

`dcgm-exporter` is **not** a second exception: it keeps the full hardened
shape (no `SYS_ADMIN`), which means DCGM's Datacenter Profiling metrics
(`DCGM_FI_PROF_*` — tensor/SM occupancy, PCIe/NVLink throughput) are
deliberately unavailable. The collected NVML-backed set (utilization,
VRAM, temperature, power, clocks, ECC) is pinned in `dcgm/counters.csv`.

## What is observable / what is not

Reachable with zero member changes:

- **Host** — node-exporter (CPU, memory, disk, network, filesystem fill).
- **Every federation container** — cAdvisor (per-container CPU, memory,
  restarts, OOM kills) via the Docker socket, across all compose projects
  on the host, including the vLLM backends.
- **Qdrant** — native `/metrics` on `qdrant:6333` over `data-net`
  (verified against v1.17.0).
- **Black-box health** — blackbox-exporter HTTP probes of
  `vllm-router:4000/health/liveliness` (`inference-net`), `neo4j:7474`
  (`data-net`), `qdrant:6333/healthz` (`data-net`).
- **Logs of every container on the host** — Alloy Docker discovery →
  Loki, labeled by compose project + service.
- **NVIDIA GPUs** (GPU hosts, `gpu` compose profile) — dcgm-exporter
  device telemetry: utilization, framebuffer memory, temperature, power,
  SM clock, memory-bandwidth utilization, ECC error counters. Rendered on
  the `gpu.json` dashboard; complements the vLLM job's KV-cache metrics,
  which are model-server-side, not device-side. With the profile off the
  `dcgm` scrape target reads down — expected, and excluded by
  `make health`.
- **obs-plane's own internals** — Prometheus (`prometheus:9090`), Loki
  (`loki:3100/metrics`), and Alloy (`alloy:12345/metrics`, HTTP server
  rebound to `0.0.0.0` so prometheus can reach it) are scraped like any
  other target and rendered on the `obs-plane.json` dashboard: TSDB size,
  ingestion rate, scrape duration by job, Loki ingest bytes/chunk-flush
  rate, Alloy shipped-bytes rate.
- **vLLM backends** (`chat`, `embed`, `rerank`, `asr`) — native
  `vllm:...` Prometheus metrics (token throughput, latency, KV-cache
  usage) scraped by service name on `inference-net`, pending
  [nos-tromo/vllm-service#67](https://github.com/nos-tromo/vllm-service/pull/67)
  (unmerged), which attaches those backends to `inference-net`. Until it
  merges, the `vllm` scrape job's targets are down — expected, not a
  regression. See the `vllm.json` dashboard below.
- **LiteLLM router** (`vllm-router:4000/metrics/`) — the routing layer the
  `vllm` job cannot see: per-model request counts, end-to-end latency
  *including* routing, and failures that never reached a backend. Requires
  vllm-service to enable LiteLLM's Prometheus callback
  ([nos-tromo/vllm-service#107](https://github.com/nos-tromo/vllm-service/pull/107)),
  without which the router does not mount `/metrics` at all; until that is
  deployed the `litellm` target is down — expected. Scraped
  unauthenticated on `inference-net` like the `vllm` job: the router's
  master key doubles as every backend's `--api-key`, so it is deliberately
  not duplicated into this repo. Rendered on the `litellm.json` dashboard.

Explicitly **not** available in v1:

- **Neo4j internal metrics** — Prometheus/CSV metrics are Enterprise-only;
  the federation runs Community. Coverage is cAdvisor + HTTP probe only.
- **`clip`, `diarize`, `vad` vLLM backends** — expose no `/metrics`
  endpoint at all; cAdvisor coverage only.
- **`gliner`** — Ray Serve, not `vllm serve`; whether it exposes a
  Prometheus endpoint is unconfirmed, so it stays unscraped pending
  investigation.

**Observable once the apps deploy updated images**:

- **FastAPI app metrics** — `chorus-backend:8000`, `docint-backend:8000`,
  `nextext-backend:8000`, `translator-backend:8000` are configured as a
  Prometheus `apps` scrape job (`prometheus/prometheus.yml`), each exposing
  `prometheus-fastapi-instrumentator` defaults (`http_requests_total`,
  `http_request_duration_seconds` buckets; labeled `method`/`handler`
  (route template)/`status`). The instrumentation PRs (chorus#95,
  docint#341, Nextext#105, translator#71) are merged; the targets read
  down (`up == 0`) until each app deploys an image containing the change.
  `chorus-backend` and `docint-backend` resolve on `data-net` (also on
  `inference-net`); `nextext-backend` and `translator-backend` are
  `inference-net` only.

## Quick start

```bash
cp .env.example .env
$EDITOR .env                  # set GRAFANA_ADMIN_PASSWORD at minimum

make network                  # create the external inference-net + data-net + edge-net (idempotent)
make volumes                  # create the external obs volumes (prometheus-data, loki-data, grafana-data, alloy-data; idempotent)
make up-dev                   # start, publishing Grafana on the host
```

Grafana is then at `http://localhost:3001` (`${GRAFANA_HOST_PORT:-3001}`),
default user `admin` / the password set above. `make network` and
`make volumes` are also prerequisites of `make up`/`make up-dev`, so a
fresh host can just run `make up-dev` directly.

**GPU hosts**: additionally set `COMPOSE_PROFILES=gpu` in `.env` to run
`dcgm-exporter` (requires the NVIDIA container toolkit). On hosts without
it, leave the variable empty — the service never starts, and the `dcgm`
scrape target reads down (expected; `make health` excludes it).

## Grafana access on production

Grafana's primary browser path is now the edge gateway:
`https://${EDGE_HOST}/grafana/`, routed by `edge-plane`'s Caddy +
Authelia forward-auth. `GF_SERVER_ROOT_URL` / `GF_SERVER_SERVE_FROM_SUB_PATH`
tell Grafana it is served under `/grafana/`; `GF_AUTH_PROXY_*` configure
Grafana's auth.proxy so a request carrying the trusted `X-Auth-User`
header auto-logs-in and auto-provisions that user (default org role
`${GRAFANA_VIEWER_ROLE:-Viewer}`). This revises the v1 decision (below)
that treated the tunnel as the only path — that path remains, as the
admin/fallback route.

**Trusted-zone note**: `X-Auth-User` is only trustworthy because
edge-plane's Caddy unconditionally strips any client-supplied copy of it
and injects its own after Authelia authenticates the request — Grafana
itself does no verification of the header's origin. Any container joined
to `edge-net` could reach `grafana:3000` directly and send an arbitrary
`X-Auth-User` value, auto-provisioning or impersonating a user. This is
the same trust posture already accepted by the app frontends
(chorus/docint/Nextext) that consume the identical header contract; it
is not a new exposure introduced by this change. Re-evaluate this acceptance if edge-net membership ever grows beyond the gateway, the app frontends, and Grafana.

The production shape (`make up`, `docker/compose.yaml` alone) still
publishes **no host ports** — same convention as every other federation
member. Without the gateway (or for admin access, since the login form
stays enabled), reach Grafana either with `make up-dev` (layers
`docker/compose.override.yaml`, which publishes
`${GRAFANA_HOST_PORT:-3001}`) or via an SSH tunnel to the container port
instead of publishing it at all. `compose.override.yaml` also relaxes the
node-exporter root-filesystem mount from `rslave` to a plain bind for
local (e.g. macOS/Docker Desktop) development — production keeps
`rslave` so post-boot mounts stay visible to `node_filesystem_*` metrics.

## Operating

```bash
make ps                       # service state
make health                   # readiness + all scrape targets up
make logs S=prometheus        # tail logs for one service (omit S= to tail all)
make down                     # stop, volumes preserved
make bundle                   # airgap tarball from the latest release tag
make nuke                     # interactive: DESTROY all volumes
```

`make health` runs its checks **from inside the prometheus container**
(busybox `wget`, no host tooling required): it asserts Prometheus
`/-/ready`, then reaches `loki:3100/ready` and `grafana:3000/api/health`
over the project-internal network, then queries Prometheus's own
query API and fails if any scrape job reports down — except the `dcgm`
job, which is excluded unless `.env` enables the `gpu` profile.

`make bundle` bundles the latest annotated release tag; set
`OBS_PLANE_VERSION_OVERRIDE=<version>` to bundle the current working
tree instead — this repo keeps a bespoke Makefile (data-plane pattern),
so there is no separate `bundle-dev` target.

## Alerting

Alert rules are committed, plain Prometheus/Loki rule files —
`prometheus/rules.yml` and `loki/rules/obs-plane.yml` — mounted read-only
and evaluated by Prometheus and Loki themselves. Grafana's unified
Alerting view lists these datasource-evaluated rules read-only; there is
no Grafana-side alert provisioning to maintain.

There is **no notification channel by design**: airgapped production has
no outbound delivery path, so rules surface only in Grafana → Alerting.
A Telegram contact point for non-airgapped staging hosts (reusing the
existing bot from `pr-notify`) is a follow-up, not v1 scope.

## Layout

```
obs-plane/
  docker/
    compose.yaml            production shape — no host ports
    compose.override.yaml   dev overlay — publishes Grafana + relaxes node-exporter mount
  prometheus/
    prometheus.yml          scrape configs (static targets by alias)
    rules.yml               Prometheus alerting rules
  loki/
    loki.yaml               single-binary config, filesystem storage, retention
    rules/                  Loki ruler rules (log-spike alert)
  alloy/
    config.alloy            Docker discovery → Loki, compose-project/service labels
  blackbox/
    blackbox.yml            http_2xx probe module(s)
  dcgm/
    counters.csv            dcgm-exporter collector list (GPU metric set, pinned)
  grafana/
    provisioning/           datasources + dashboard provider config + alerting view
    dashboards/             dashboard JSON, committed
  scripts/
    bundle_images.sh        airgap bundler, sources vendored bundle-lib.sh
    bundle-lib.sh           vendored verbatim from nos-tromo/.github
  .env.example              copy to .env; GRAFANA_ADMIN_PASSWORD required
  Makefile                  bespoke operator commands (data-plane style)
  VERSION                   one-line semver, read by release-tag workflow
  docs/                     design doc + build plan
  CLAUDE.md                 conventions + pointers
```
