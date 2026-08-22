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

## What is observable

- **Host** — node-exporter: CPU, memory, disk, network, filesystem fill.
- **Every federation container** — cAdvisor, all compose projects.
- **Qdrant** — native `/metrics` on `qdrant:6333` over `data-net`.
- **vLLM backends** — `chat`/`embed`/`rerank`/`asr` on `inference-net`.
- **LiteLLM router** — per-model requests, latency including routing.
- **FastAPI app backends** — chorus, docint, Nextext, translator.
- **NVIDIA GPUs** — dcgm-exporter, GPU hosts only (`gpu` profile).
- **Black-box health** — router liveliness, `neo4j:7474`,
  `qdrant:6333/healthz`.
- **Logs of every container on the host** — Alloy discovery → Loki.
- **obs-plane itself** — Prometheus, Loki and Alloy are scraped too.

Not covered: Neo4j internal metrics (Enterprise-only; the federation runs
Community), and the `clip`/`diarize`/`vad` backends and `gliner`, none of
which expose a confirmed `/metrics`. See [coverage.md](docs/coverage.md)
for the endpoints and networks behind each line, and
[which targets are down by design](docs/coverage.md#when-a-target-is-down-by-design).

## Grafana access

On production Grafana is reached through the edge gateway at
`https://${EDGE_HOST}/grafana/`, which authenticates the request and
auto-logs-in the user. See
[grafana-access.md](docs/grafana-access.md) for the trusted-header
contract and the dev-overlay / SSH-tunnel fallbacks.

## Alerting

Alert rules are committed Prometheus/Loki rule files
(`prometheus/rules.yml`, `loki/rules/obs-plane.yml`), evaluated by
Prometheus and Loki themselves and listed read-only in Grafana's Alerting
view. There is **no notification channel by design** — airgapped
production has no outbound delivery path. Rule list:
[the design doc](docs/2026-07-22-obs-plane-design.md#alert-rules-v1-provisioned-no-notification-channel).

## Container hardening

Every service runs with `no-new-privileges` and `cap_drop: ALL`, with one
deliberate exception: **`cadvisor` runs `privileged: true`** — an accepted
residual finding, traded for federation-wide container metrics. Baseline
and both trade-offs:
[hardening.md](docs/hardening.md#residual-finding-cadvisor-runs-privileged);
governing decision:
[deploy ADR 0001](../deploy/docs/decisions/0001-container-engine-docker.md).

## Operating

```bash
make ps                       # service state
make health                   # readiness + all scrape targets up
make logs S=prometheus        # tail logs for one service (omit S= to tail all)
make down                     # stop, volumes preserved
make bundle                   # airgap tarball from the latest release tag
make nuke                     # interactive: DESTROY all volumes
```

`make help` lists every target. What `make health` checks and the
`OBS_PLANE_VERSION_OVERRIDE` bundle override are spelled out in
[`CLAUDE.md`](CLAUDE.md#commands) and the design doc's
[Makefile section](docs/2026-07-22-obs-plane-design.md#makefile-bespoke-data-plane-style).

## Documentation

[`docs/README.md`](docs/README.md) indexes the reference docs:
[coverage.md](docs/coverage.md) (every scrape/probe target and the
deliberate gaps), [grafana-access.md](docs/grafana-access.md) (access
paths, trusted-header contract), [hardening.md](docs/hardening.md)
(privilege posture). Design history — the v1 design doc and its build
plan — lives alongside them as dated files, recording decisions as they
were made rather than tracking the current state.

## Pointers

- **Repo conventions, invariants, config-validation one-liners**:
  [`CLAUDE.md`](CLAUDE.md).
- **Federation bring-up order and the network seams**:
  [`../deploy/README.md`](../deploy/README.md).
- **Workspace map** — every project, its tier, how they connect: the infra
  root [`../CLAUDE.md`](../CLAUDE.md).
- **Questions and bugs**: <https://github.com/nos-tromo/obs-plane/issues>.
