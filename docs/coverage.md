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
  usage) scraped by service name on `inference-net`. vllm-service
  attaches those backends to `inference-net` in addition to its own
  internal `vllm-net`, which is what makes them reachable from here.
  Rendered on the `vllm.json` dashboard.
- **LiteLLM router** (`vllm-router:4000/metrics/`) — the routing layer the
  `vllm` job cannot see: per-model request counts, end-to-end latency
  *including* routing, and failures that never reached a backend. It
  depends on vllm-service enabling LiteLLM's Prometheus callback
  (`litellm_settings.callbacks: ["prometheus"]` in
  `docker/litellm.config.yaml`), without which the router does not mount
  `/metrics` at all. Scraped unauthenticated on `inference-net` like the
  `vllm` job: the router's master key doubles as every backend's
  `--api-key`, so it is deliberately not duplicated into this repo.
  Rendered on the `litellm.json` dashboard.
- **FastAPI app metrics** — `chorus-backend:8000`, `docint-backend:8000`,
  `nextext-backend:8000`, `translator-backend:8000` are configured as a
  Prometheus `apps` scrape job (`prometheus/prometheus.yml`), each exposing
  `prometheus-fastapi-instrumentator` defaults (`http_requests_total`,
  `http_request_duration_seconds` buckets; labeled `method`/`handler`
  (route template)/`status`). All four apps register `GET /metrics` on
  their FastAPI app. `chorus-backend` and `docint-backend` resolve on
  `data-net` (also on `inference-net`); `nextext-backend` and
  `translator-backend` are `inference-net` only.

## Not available

- **Neo4j internal metrics** — Prometheus/CSV metrics are Enterprise-only;
  the federation runs Community. Coverage is cAdvisor + HTTP probe only.
- **`clip`, `diarize`, `vad` vLLM backends** — expose no `/metrics`
  endpoint at all; cAdvisor coverage only.
- **`gliner`** — Ray Serve, not `vllm serve`; whether it exposes a
  Prometheus endpoint is unconfirmed, so it stays unscraped pending
  investigation.
- **DCGM Datacenter Profiling metrics** (`DCGM_FI_PROF_*` — tensor/SM
  occupancy, PCIe/NVLink throughput) — traded away by keeping
  `dcgm-exporter` fully hardened. See
  [hardening.md](hardening.md#dcgm-exporter-is-not-a-second-exception).

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
