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
  the services' hash-chained audit events and the gateway's access log —
  are the exception: they go, unredacted, to the audit trail's own store
  and never to this Loki ([audit.md](audit.md)).
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
- **Ghostwriter** — `ghostwriter-backend:8000/metrics` in its own scrape job,
  `ghostwriter`, so that a host without it never shows `apps` down. It
  exposes the same `prometheus-fastapi-instrumentator` defaults as the apps
  above and resolves on `inference-net` and on `workbench-net`, both joined
  read-only. Its audit chain, emitter `ghostwriter` / `backend`, is checked
  by the verifier like the others ([audit.md](audit.md#source-binding)).
- **workbench** — `workbench-api:8000/metrics` (job `workbench`) over
  `workbench-net`, which prometheus joins read-only like the other seams,
  claiming no alias. No key is needed: the api's key gate exempts `/metrics`
  and `/healthz`, and the page holds counts only, labelled by tool or by
  the reason a job was refused — never a user, chat, file name or content.
  It exports the queue's length and wait, job duration by tool, refused
  jobs by reason, the data volume's disk use and capacity, and the file
  count. Two alerts in the `workbench` rule group:
  `WorkbenchDiskHigh` (volume above 80% for 15 minutes; the disk gauges are
  measured when the api starts and by each retention sweep, so they lag the
  filesystem) and `WorkbenchRefusing` (refusals at a non-zero rate for 15
  minutes). Its audit chain, emitter `workbench` / `api`, is checked by the
  verifier like the others ([audit.md](audit.md#source-binding)). The
  executor has no network and no metrics; cAdvisor sees it as a container.
  No dashboard here renders these metrics yet.

## Not available

- **What workbench's jobs processed** — inputs, outputs and file names are in
  no metric, by design; the scrape carries counts and durations. What is
  recorded of them is the audit chain, by reference.
- **`clip`, `diarize`, `vad`, `faces` backends** — first-party FastAPI
  servers, not vLLM; they expose no `/metrics` endpoint at all; cAdvisor
  coverage only.
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
- **`workbench`** — the target reads down until workbench is deployed, and
  prometheus cannot start at all while `workbench-net` does not exist
  (`make network` creates it, as `make up` does first).
- **`ghostwriter`** — the target reads down until Ghostwriter is deployed,
  and `make health` names the job meanwhile, as it does for `workbench`.
- **A member running an older image** — a scrape job configured here can
  only come up once the member's *deployed* build exposes the endpoint.
  A target reading `up == 0` right after a member gains a metrics
  endpoint upstream normally means that member has not been redeployed
  yet, not that obs-plane is misconfigured.
