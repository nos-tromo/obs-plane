# Observability coverage

The target-by-target list of what obs-plane sees, what it deliberately
does not see, and which targets are down by design. The top-level
[README](../README.md#what-is-observable) carries a summary of this list;
this file is the authoritative version.

Scrape jobs live in `prometheus/prometheus.yml`, probe modules in
`blackbox/blackbox.yml`, and the dashboards that render each source under
`grafana/dashboards/`.

## Observable

- **Host** — node-exporter (CPU, memory, disk, network, filesystem fill).
- **Every federation container** — cAdvisor (per-container CPU, memory,
  restarts, OOM kills) via the Docker socket, across all compose projects
  on the host, including the vLLM backends.
- **Qdrant** — native `/metrics` on `qdrant:6333` over `data-net`
  (verified against v1.18.3).
- **Black-box health** — blackbox-exporter HTTP probes of
  `vllm-router:4000/health/liveliness` (`inference-net`),
  `qdrant:6333/healthz` (`data-net`).
- **Logs of every container on the host** — Alloy Docker discovery →
  Loki, labeled by compose project + service. Query-string values are
  redacted before storage (`?entity_text=<redacted>`; parameter names are
  kept), so search terms and access tokens in request lines, URLs and
  referrers never reach Loki — including from sources whose own log format
  cannot be changed (nginx's error log, upstream images). Audit records —
  the services' hash-chained audit events, the gateway's access log and
  Open WebUI's audit log — are the exception: they go, unredacted, to the
  audit trail's own store and never to this Loki ([audit.md](audit.md)).
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
- **The audit trail's integrity** — `audit-verifier:9105`: events checked,
  chain problems by kind, open chains, unchained lines per project, last
  successful run. Counts only, never records; the `audit` rule group alerts
  on them ([audit.md](audit.md#metrics-and-alerts)).
- **vLLM backends** (`chat`, `embed`, `rerank`, `asr`) — native
  `vllm:...` Prometheus metrics (token throughput, latency, KV-cache
  usage) scraped by service name on `inference-net`. vllm-service
  attaches those backends to `inference-net` in addition to its own
  internal `vllm-net`, which is what makes them reachable from here.
  Rendered on the `vllm.json` dashboard.
- **LiteLLM router** (`vllm-router:4000/metrics/`) — the routing layer the
  `vllm` job cannot see: per-model request counts, end-to-end latency
  *including* routing, and failures that never reached a backend. It
  depends on vllm-service enabling LiteLLM's Prometheus callback
  (`litellm_settings.callbacks: ["prometheus"]` in *that repo's*
  `docker/litellm.config.yaml` — set there today), without which the
  router does not mount `/metrics` at all. Scraped unauthenticated on
  `inference-net` like the `vllm` job: the router's master key doubles
  as every backend's `--api-key`, so it is deliberately not duplicated
  into this repo.
  Rendered on the `litellm.json` dashboard.
- **FastAPI app metrics** — `docint-backend:8000`, `nextext-backend:8000`,
  `translator-backend:8000` are configured as a
  Prometheus `apps` scrape job (`prometheus/prometheus.yml`), each exposing
  `prometheus-fastapi-instrumentator` defaults (`http_requests_total`,
  `http_request_duration_seconds` buckets; labeled `method`/`handler`
  (route template)/`status`). All three apps register `GET /metrics` on
  their FastAPI app; docint gates it behind `METRICS_ENABLED`
  (default `true`), so an `apps` target reading down on a member that is
  definitely deployed is worth checking against that variable. Nextext
  additionally exports job-outcome counters (`nextext_jobs_total`,
  `nextext_jobs_skipped_total`, `nextext_jobs_failed_total`) on the same
  endpoint; no dashboard here renders them yet.
  `docint-backend` resolves on
  `data-net` (also on `inference-net`); `nextext-backend` and
  `translator-backend` are `inference-net` only.

## Not available

- **`clip`, `diarize`, `vad` vLLM backends** — expose no `/metrics`
  endpoint at all; cAdvisor coverage only.
- **`gliner`** — Ray Serve, not `vllm serve`; whether it exposes a
  Prometheus endpoint is unconfirmed, so it stays unscraped pending
  investigation.
- **DCGM Datacenter Profiling metrics** (`DCGM_FI_PROF_*` — tensor/SM
  occupancy, PCIe/NVLink throughput) — traded away by keeping
  `dcgm-exporter` fully hardened. See
  [hardening.md](hardening.md#dcgm-exporter-is-not-a-second-exception).
- **The audit store's own internals** — `loki-audit` is not scraped (it
  sits on the internal `audit` network, which prometheus does not join),
  and the logs of `loki-audit`, `loki-audit-read`, `grafana-audit` and
  `grafana-audit-guard` are collected by neither lane, since they can name
  what reviewers searched for. They stay on the host
  (`make logs S=loki-audit`); `make health` checks readiness (the store
  through `loki-audit-read`, and that the guard refuses requests without the
  edge secret), and the verifier's alerts fire when the store stops
  answering.

## When a target is down by design

`make health` fails if any scrape job reports down, so it is worth
knowing which ones are expected to be:

- **`dcgm`** — `dcgm-exporter` runs only under the `gpu` compose profile.
  With the profile off the target reads down, and `make health` excludes
  the job unless `.env` enables the profile.
- **A member running an older image** — a scrape job configured here can
  only come up once the member's *deployed* build exposes the endpoint.
  A target reading `up == 0` right after a member gains a metrics
  endpoint upstream normally means that member has not been redeployed
  yet, not that obs-plane is misconfigured.
