# obs-plane

Observability plane for the nos-tromo federation: Prometheus + Grafana +
Loki + Grafana Alloy + socket-proxy + node-exporter + cAdvisor +
blackbox-exporter + dcgm-exporter (GPU hosts), plus the federation's
data-protection audit trail (`loki-audit`, `grafana-audit`,
`audit-verifier`), all pulled and digest-pinned, bundleable for airgap.
Built in the `data-plane` mold — it owns its own external volumes, and
joins all three shared external networks: `inference-net` and `data-net`
read-only as a scraper, `edge-net` so the edge gateway can reach Grafana
and the audit-trail viewer.

## What lives here

Fifteen services, all lightweight next to the inference stack.

| Service | Role | Network membership |
|---|---|---|
| `prometheus` | Metrics store + scraper, retention `${PROMETHEUS_RETENTION:-30d}` | project-internal + `inference-net` + `data-net` |
| `grafana` | UI; datasources and dashboards file-provisioned. Alert rules are not — they are Prometheus/Loki rule files, listed here read-only. Listens on loopback only, behind `grafana-guard` | project-internal + `edge-net`, alias `grafana` (served at `/grafana/` behind the edge gateway) |
| `grafana-guard` | nginx in `grafana`'s network namespace, answering on `:3000`: passes the gateway's identity headers on only with `GRAFANA_EDGE_SECRET` (unset: as before) | shares `grafana`'s |
| `loki` | Log store, filesystem backend, retention `${LOKI_RETENTION:-720h}` | project-internal only |
| `socket-proxy` | Restricted read-only gateway to the Docker API for Alloy (only `GET /containers`, `/networks`, `/events`, `/_ping` enabled; every mutating/sensitive endpoint denied) | project-internal only; the sole Alloy-path holder of `/var/run/docker.sock` (ro) |
| `alloy` | Log collector: Docker-API discovery of every container's logs → Loki, with query-string values redacted on the way (`?q=<redacted>`); audit records go unredacted to `loki-audit` instead. Log-tail positions persisted to `alloy-data` (prevents duplicate/lost lines on recreate); runs as its internal uid 473 | project-internal + `audit`; reaches the Docker API via `socket-proxy:2375` — no socket mount |
| `node-exporter` | Host metrics (CPU, memory, disk, network, filesystem fill) | internal (default) network |
| `cadvisor` | Per-container metrics for every compose project on the host | internal (default) network |
| `blackbox-exporter` | HTTP probes of federation endpoints | project-internal + `inference-net` + `data-net` |
| `dcgm-exporter` | NVIDIA GPU metrics (utilization, VRAM, temperature, power, clocks, ECC); runs only when the `gpu` compose profile is enabled (`COMPOSE_PROFILES=gpu` in `.env`, GPU hosts with the NVIDIA container toolkit) | internal (default) network |
| `loki-audit` | The audit trail's store: audit records only, retention `${AUDIT_RETENTION:-8760h}`, no deletion API; written only by `alloy` | `audit` only |
| `loki-audit-read` | nginx in front of `loki-audit` for its readers: `GET` on Loki's read API, 403 for everything else | `audit` + `audit-read` |
| `grafana-audit` | The audit-trail viewer: one datasource (the store, through `loki-audit-read`), one dashboard; readers sign in as Viewers; no local admin. Listens on loopback only, behind `grafana-audit-guard` | `audit-read` + `edge-net`, alias `grafana-audit` (served at `/audit/` behind the edge gateway) |
| `grafana-audit-guard` | nginx in `grafana-audit`'s network namespace, answering on `:3000`: 403 for every request without `AUDIT_EDGE_SECRET` | shares `grafana-audit`'s |
| `audit-verifier` | Re-checks the trail's hash chains every `${AUDIT_VERIFY_INTERVAL:-300}` s, through `loki-audit-read`, and exports the result as metrics; runs the `make audit-*` one-shots | project-internal + `audit-read` |

`prometheus` and `blackbox-exporter` join `inference-net` and `data-net`
(to reach scrape/probe targets by alias); `grafana` and `grafana-audit`
join `edge-net` — the rest stay on the project-internal networks. On the
first two seams obs-plane is a read-only consumer that claims no alias. On
`edge-net` it publishes two: `grafana` and `grafana-audit`, which
edge-plane's Caddy proxies `/grafana/*` and `/audit/*` to
([`caddy/Caddyfile`](../edge-plane/caddy/Caddyfile)) together with
each one's edge secret; the guards in front of the two Grafanas check it
([grafana-access.md](docs/grafana-access.md#the-edge-guards)). The audit
store has two internal project networks of its own: `audit` (the store,
its only writer `alloy`, and `loki-audit-read`) and `audit-read`
(`loki-audit-read` and the two readers, `grafana-audit` and
`audit-verifier`). The ops `grafana` is on neither.

## Quick start

```bash
cp .env.example .env
$EDITOR .env                  # set GRAFANA_ADMIN_PASSWORD and AUDIT_EDGE_SECRET at minimum

make network                  # create the external inference-net + data-net + edge-net (idempotent)
make volumes                  # create the external obs + audit volumes (idempotent)
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
- **Logs of every container on the host** — Alloy discovery → Loki;
  audit records go to the audit trail instead (below).
- **obs-plane itself** — Prometheus, Loki and Alloy are scraped too.

Not covered: Neo4j internal metrics (Enterprise-only; the federation runs
Community), and the `clip`/`diarize`/`vad` backends and `gliner`, none of
which expose a confirmed `/metrics`. See [coverage.md](docs/coverage.md)
for the endpoints and networks behind each line, and
[which targets are down by design](docs/coverage.md#when-a-target-is-down-by-design).

## Grafana access

On production Grafana is reached through the edge gateway at
`https://${EDGE_HOST}/grafana/`, which authenticates the request and
auto-logs-in the user. Unlike every other routed app, `/grafana` is
**restricted to Authelia's `admins` group** — that gate lives in
edge-plane's `authelia/configuration.yml`, not here. Grafana itself
listens on loopback only; `grafana-guard` in front of it passes the
gateway's identity header on only with `GRAFANA_EDGE_SECRET`. See
[grafana-access.md](docs/grafana-access.md) for the trusted-header
contract, the edge guards and the dev-overlay / SSH-tunnel fallbacks.

## Audit trail

Audit records — the hash-chained events of the federation's services, the
gateway's access log and Open WebUI's audit log — bypass the operational
Loki: Alloy routes them, unredacted, to `loki-audit`, which keeps them for
`AUDIT_RETENTION` (default one year) and offers no way to delete lines.
`grafana-audit` shows them at `https://${EDGE_HOST}/audit/` to the
gateway's `audit` group only — its guard refuses anything that does not
carry the gateway's `AUDIT_EDGE_SECRET` — and reads the store through
`loki-audit-read`, which allows reads and nothing else. Only `alloy`
writes to the store. Ops admins see the verifier's metrics and alerts,
never the records.

```bash
make audit-verify FROM=2026-01-01 TO=2026-02-01   # check the hash chains of a range
make audit-export FROM=… TO=… [SERVICE=…] [REF=…] # export as JSONL for a review
make audit-anchor DAY=2026-01-01                  # the day's anchor, to sign and keep offline
```

Routing, integrity checks, signing and limits:
[audit.md](docs/audit.md).

## Alerting

Alert rules are committed Prometheus/Loki rule files
(`prometheus/rules.yml`, `loki/rules/obs-plane.yml`), evaluated by
Prometheus and Loki themselves and listed read-only in Grafana's Alerting
view — Grafana provisions datasources and dashboards, never alert rules.
There is **no notification channel by design** — airgapped production has
no outbound delivery path.

Twelve rules in five groups: `federation` (`ProbeDown`,
`ScrapeTargetDown`, `ContainerRestartLooping`), `host`
(`HostDiskAlmostFull`, `HostMemoryPressure`), `gpu`
(`GPUHighTemperature`, `GPUECCErrors`, `GPUMemoryAlmostFull` — GPU hosts
only), `audit` (`AuditChainProblem`, `AuditVerifierStale`,
`AuditVerifierDown`), and Loki's `logs` (`ErrorLogSpike`).

## Container hardening

Fourteen of the fifteen services run with `no-new-privileges` and
`cap_drop: ALL`. The fifteenth, **`cadvisor`, takes neither and runs
`privileged: true`** — an accepted residual finding, traded for
federation-wide container metrics. Baseline and both trade-offs:
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
make nuke                     # interactive: DESTROY the ops volumes (not the audit trail's)
```

`make help` lists the operator targets (`stop` is an unlisted bare
compose passthrough; `down` is the documented stop). What `make health`
checks and the `OBS_PLANE_VERSION_OVERRIDE` bundle override are spelled out in
[`CLAUDE.md`](CLAUDE.md#commands) and the design doc's
[Makefile section](docs/2026-07-22-obs-plane-design.md#makefile-bespoke-data-plane-style).

## Documentation

[`docs/README.md`](docs/README.md) indexes the reference docs:
[coverage.md](docs/coverage.md) (every scrape/probe target and the
deliberate gaps), [grafana-access.md](docs/grafana-access.md) (access
paths, trusted-header contract), [hardening.md](docs/hardening.md)
(privilege posture), [audit.md](docs/audit.md) (the audit trail). Design history — the v1 design doc and its build
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
