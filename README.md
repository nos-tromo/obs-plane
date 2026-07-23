# obs-plane

Observability plane for the nos-tromo federation: Prometheus + Grafana +
Loki + Grafana Alloy + node-exporter + cAdvisor + blackbox-exporter, all
pulled and digest-pinned, bundleable for airgap. A pure consumer in the
`data-plane` mold — it owns its own volumes, joins the shared external
networks read-only as a scraper, and makes zero changes to any other
federation member.

## What lives here

Seven services, all lightweight next to the inference stack.

| Service | Role | Network membership |
|---|---|---|
| `prometheus` | Metrics store + scraper, retention `${PROMETHEUS_RETENTION:-30d}` | project-internal + `inference-net` + `data-net` |
| `grafana` | UI; datasources, dashboards, alert rules all file-provisioned | project-internal + `edge-net` (served at `/grafana/` behind the edge gateway) |
| `loki` | Log store, filesystem backend, retention `${LOKI_RETENTION:-720h}` | project-internal only |
| `alloy` | Log collector: Docker-API discovery of every container's logs → Loki, log-tail positions persisted to `alloy-data` (prevents duplicate/lost lines on recreate) | project-internal only; reads `/var/run/docker.sock` (ro) |
| `node-exporter` | Host metrics (CPU, memory, disk, network, filesystem fill) | internal (default) network |
| `cadvisor` | Per-container metrics for every compose project on the host | internal (default) network |
| `blackbox-exporter` | HTTP probes of federation endpoints | project-internal + `inference-net` + `data-net` |

`prometheus` and `blackbox-exporter` join the two shared external
networks (to reach scrape/probe targets by alias); `grafana` additionally
joins `edge-net` so the edge gateway can reach it — the rest stay on the
project-internal default network. obs-plane claims no alias other
services depend on — it is a read-only consumer of all three seams.

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

Explicitly **not** available in v1:

- **Neo4j internal metrics** — Prometheus/CSV metrics are Enterprise-only;
  the federation runs Community. Coverage is cAdvisor + HTTP probe only.
- **LiteLLM router `/metrics`** — enterprise-gated in current LiteLLM
  releases. Coverage is cAdvisor + liveliness probe only.
- **vLLM backend `/metrics`** — the backends (`chat`, `embed`, `rerank`,
  `clip`, `asr`, `diarize`, `vad`, `gliner`) sit only on the internal
  `vllm-net`; only the router joins `inference-net`. Native vLLM metrics
  (token throughput, latency, KV-cache usage) require a one-line
  vllm-service change (attach backends to `inference-net`) — the first
  follow-up PR after v1.
**Observable, pending companion PRs**:

- **FastAPI app metrics** — `chorus-backend:8000`, `docint-backend:8000`,
  `nextext-backend:8000`, `translator-backend:8000` are configured as a
  Prometheus `apps` scrape job (`prometheus/prometheus.yml`), each exposing
  `prometheus-fastapi-instrumentator` defaults (`http_requests_total`,
  `http_request_duration_seconds` buckets; labeled `method`/`handler`
  (route template)/`status`). The targets read down (`up == 0`) until the
  corresponding companion PR merges and ships `/metrics`: chorus#95,
  docint#341, Nextext#105, translator#71. `chorus-backend` and
  `docint-backend` resolve on `data-net` (also on `inference-net`);
  `nextext-backend` and `translator-backend` are `inference-net` only.

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
targets API and fails if any scrape target reports down.

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
