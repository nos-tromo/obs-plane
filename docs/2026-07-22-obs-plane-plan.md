# obs-plane v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the `nos-tromo/obs-plane` repo — an airgap-first Docker Compose observability stack (Prometheus, Grafana, Loki, Alloy, node-exporter, cAdvisor, blackbox-exporter) in the data-plane mold.

**Architecture:** Pure pulled-image compose project. Prometheus + blackbox-exporter join the external `inference-net`/`data-net` as read-only scrapers; everything else talks over the project-internal default network. Alert rules are Prometheus/Loki ruler files (surfaced read-only in Grafana's alerting UI). Three external volumes; interactive `make nuke` is the only destroyer. Spec: `2026-07-22-obs-plane-design.md` (sibling of this plan; both move to `obs-plane/docs/` in Task 1).

**Tech Stack:** Docker Compose, Make, bash; no Python, no image builds.

## Global Constraints

- **Confidentiality (hard rule):** nothing committed may contain real data or absolute local paths (e.g., `/home/<name>` or similar). Only repo-relative paths in every committed file.
- **Image pins (security requirement, user-mandated):** every `image:` is `registry/repo:vX.Y.Z@sha256:...` — explicit release tag **and** digest. Never `latest`, `stable`, `main`, or rc tags. Exact pins are given in Task 2 (resolved 2026-07-22); re-resolve only if a task fails because a tag/digest has been yanked.
- **Airgap:** no runtime fetching, no telemetry. Grafana analytics/update-check env flags off, Loki `analytics.reporting_enabled: false`, Alloy `--disable-reporting`. No Grafana plugins.
- **No host ports in `docker/compose.yaml`.** Only `compose.override.yaml` publishes (Grafana on `${GRAFANA_HOST_PORT:-3001}`).
- **Bespoke Makefile** (data-plane pattern), not `common.mk`. Vendored `scripts/bundle-lib.sh` verbatim from `nos-tromo/.github` `configs/bundle/bundle-lib.sh` (CI drift-checks it).
- Work happens in a fresh clone at the infra workspace root: `<infra>/obs-plane` (sibling of `data-plane`). All commands below run from that directory unless stated.
- Commit style: short imperative subjects like the fleet (`feat: ...`, `ci: ...`, `docs: ...`).

---

### Task 1: Repo scaffold

**Files:**
- Create: `VERSION`, `.gitignore`, `README.md` (stub), `docs/2026-07-22-obs-plane-design.md`, `docs/2026-07-22-obs-plane-plan.md`

**Interfaces:**
- Produces: GitHub repo `nos-tromo/obs-plane`, local clone with `main` + working branch `feature/obs-plane-v1`. All later tasks commit to that branch.

- [ ] **Step 1: Create the GitHub repo matching the fleet's visibility**

```bash
VIS=$(gh repo view nos-tromo/data-plane --json visibility -q .visibility | tr '[:upper:]' '[:lower:]')
gh repo create nos-tromo/obs-plane --description "Observability plane for the nos-tromo federation (Prometheus + Grafana + Loki)" --$VIS
cd "$(git -C ../data-plane rev-parse --show-toplevel)/.." && git clone https://github.com/nos-tromo/obs-plane.git && cd obs-plane
```

- [ ] **Step 2: Scaffold files**

`VERSION`:
```
0.1.0
```

`.gitignore`:
```
.env
*.tar.gz
.obs-plane-version
```

`README.md` (stub — replaced in Task 9):
```markdown
# obs-plane

Observability plane for the nos-tromo federation. v1 under construction — see docs/2026-07-22-obs-plane-design.md.
```

Copy the two design docs from the infra workspace root `docs/` into `docs/` here (`2026-07-22-obs-plane-design.md`, `2026-07-22-obs-plane-plan.md`). **Check them for absolute paths before committing** (verify no real local paths are present).

- [ ] **Step 3: Verify and commit to main, then branch**

Verify no real local paths are present:
```bash
grep -rnE '/(Users|home)/[A-Za-z0-9_.-]+' . --exclude-dir=.git && echo "FAIL: local paths found" || echo "clean"
```

Then commit:
```bash
git add -A && git commit -m "chore: scaffold obs-plane (VERSION, docs, gitignore)"
git push -u origin main
git checkout -b feature/obs-plane-v1
```

---

### Task 2: Compose files + .env.example

**Files:**
- Create: `docker/compose.yaml`, `docker/compose.override.yaml`, `.env.example`

**Interfaces:**
- Produces: service names `prometheus`, `grafana`, `loki`, `alloy`, `node-exporter`, `cadvisor`, `blackbox-exporter`; volumes `prometheus-data`, `loki-data`, `grafana-data`; bind-mount paths consumed by Tasks 3–7 (`prometheus/`, `loki/`, `alloy/`, `blackbox/`, `grafana/`). Compose project name `obs-plane`.

**Image pins (resolved 2026-07-22 — latest stable tag + registry manifest-list digest):**

| Service | Pin |
|---|---|
| prometheus | `docker.io/prom/prometheus:v3.13.1@sha256:3c42b892cf723fa54d2f262c37a0e1f80aa8c8ddb1da7b9b0df9455a35a7f893` |
| grafana | `docker.io/grafana/grafana-oss:13.0.2@sha256:5dad0df181cb644a14e13617b913b261a54f7d4fd4510721dba420929f35bea2` |
| loki | `docker.io/grafana/loki:3.7.4@sha256:87f0a067673756a3cede1bcbf0c74875f7df9b09fddb53e399d0c576f756cfcc` |
| alloy | `docker.io/grafana/alloy:v1.18.0@sha256:491b0578c04983fd54fe99b587b6fab4404dc46d0dc16677bd6b00cc1140b308` |
| node-exporter | `docker.io/prom/node-exporter:v1.12.1@sha256:1b4e4438faca4dd7e001dd445d161a4a2091b0fededa84093b3a8dfeae1f1be0` |
| cadvisor | `gcr.io/cadvisor/cadvisor:v0.55.1@sha256:3de2bd5203120b866d74a9b283b2ffb8ec382fbf9dc321814700c6ea6f44ec57` |
| blackbox-exporter | `docker.io/prom/blackbox-exporter:v0.28.0@sha256:e753ff9f3fc458d02cca5eddab5a77e1c175eee484a8925ac7d524f04366c2fc` |

- [ ] **Step 1: Write `docker/compose.yaml`**

```yaml
### obs-plane compose — observability for the nos-tromo federation.
###
### What this project owns:
###   - Prometheus (metrics), Loki (logs), Grafana (UI) + their named volumes
###   - Collectors: Alloy (logs), node-exporter, cAdvisor, blackbox-exporter
###
### What it does NOT own:
###   - inference-net / data-net (external; joined read-only as a scraper)
###   - Any service other members depend on — obs-plane is a pure consumer.
###
### Airgap: all images pulled + digest-pinned; every phone-home/update-check
### is disabled below. Nothing fetches anything at runtime.

name: obs-plane

x-logging: &default-logging
  driver: "local"
  options:
    max-size: "50m"
    max-file: "5"
    compress: "true"

networks:
  default:
  inference-net:
    external: true
    name: ${INFERENCE_NET:-inference-net}
  data-net:
    external: true
    name: ${DATA_NET:-data-net}

services:
  ##########################################################
  # Prometheus — metrics store + scraper
  ##########################################################
  prometheus:
    image: docker.io/prom/prometheus:v3.13.1@sha256:3c42b892cf723fa54d2f262c37a0e1f80aa8c8ddb1da7b9b0df9455a35a7f893
    logging: *default-logging
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=${PROMETHEUS_RETENTION:-30d}
    volumes:
      - ../prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ../prometheus/rules.yml:/etc/prometheus/rules.yml:ro
      - prometheus-data:/prometheus
    networks:
      default:
      inference-net:
      data-net:
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:9090/-/ready"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s
    restart: unless-stopped

  ##########################################################
  # Grafana — UI; everything provisioned from files
  ##########################################################
  grafana:
    image: docker.io/grafana/grafana-oss:13.0.2@sha256:5dad0df181cb644a14e13617b913b261a54f7d4fd4510721dba420929f35bea2
    logging: *default-logging
    environment:
      GF_SECURITY_ADMIN_USER: ${GRAFANA_ADMIN_USER:-admin}
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?GRAFANA_ADMIN_PASSWORD must be set in .env}
      # Airgap: no phone-home, no update checks, no news feed.
      GF_ANALYTICS_REPORTING_ENABLED: "false"
      GF_ANALYTICS_CHECK_FOR_UPDATES: "false"
      GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES: "false"
      GF_NEWS_NEWS_FEED_ENABLED: "false"
    volumes:
      - ../grafana/provisioning:/etc/grafana/provisioning:ro
      - ../grafana/dashboards:/var/lib/grafana/dashboards:ro
      - grafana-data:/var/lib/grafana
    depends_on:
      prometheus:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:3000/api/health"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s
    restart: unless-stopped

  ##########################################################
  # Loki — log store (single-binary, filesystem backend)
  ##########################################################
  loki:
    image: docker.io/grafana/loki:3.7.4@sha256:87f0a067673756a3cede1bcbf0c74875f7df9b09fddb53e399d0c576f756cfcc
    logging: *default-logging
    command:
      - -config.file=/etc/loki/loki.yaml
      - -config.expand-env=true
    environment:
      LOKI_RETENTION: ${LOKI_RETENTION:-720h}
    volumes:
      - ../loki/loki.yaml:/etc/loki/loki.yaml:ro
      - ../loki/rules:/loki/rules/fake:ro
      - loki-data:/loki
    # The loki image may ship no wget/curl (qdrant precedent) — external
    # readiness is asserted by `make health` via the prometheus container.
    restart: unless-stopped

  ##########################################################
  # Alloy — ships every container's logs to Loki via the Docker API
  # (works with the fleet-wide `local` log driver; no driver changes)
  ##########################################################
  alloy:
    image: docker.io/grafana/alloy:v1.18.0@sha256:491b0578c04983fd54fe99b587b6fab4404dc46d0dc16677bd6b00cc1140b308
    logging: *default-logging
    command:
      - run
      - /etc/alloy/config.alloy
      - --disable-reporting
      - --storage.path=/var/lib/alloy/data
    volumes:
      - ../alloy/config.alloy:/etc/alloy/config.alloy:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
    depends_on:
      - loki
    restart: unless-stopped

  ##########################################################
  # node-exporter — host metrics
  ##########################################################
  node-exporter:
    image: docker.io/prom/node-exporter:v1.12.1@sha256:1b4e4438faca4dd7e001dd445d161a4a2091b0fededa84093b3a8dfeae1f1be0
    logging: *default-logging
    command:
      - --path.rootfs=/host
    pid: host
    volumes:
      - /:/host:ro,rslave
    restart: unless-stopped

  ##########################################################
  # cAdvisor — per-container metrics for the whole host
  ##########################################################
  cadvisor:
    image: gcr.io/cadvisor/cadvisor:v0.55.1@sha256:3de2bd5203120b866d74a9b283b2ffb8ec382fbf9dc321814700c6ea6f44ec57
    logging: *default-logging
    privileged: true
    devices:
      - /dev/kmsg
    volumes:
      - /:/rootfs:ro
      - /var/run:/var/run:ro
      - /sys:/sys:ro
      - /var/lib/docker:/var/lib/docker:ro
      - /dev/disk:/dev/disk:ro
    restart: unless-stopped

  ##########################################################
  # blackbox-exporter — HTTP health probes of federation endpoints
  ##########################################################
  blackbox-exporter:
    image: docker.io/prom/blackbox-exporter:v0.28.0@sha256:e753ff9f3fc458d02cca5eddab5a77e1c175eee484a8925ac7d524f04366c2fc
    logging: *default-logging
    command:
      - --config.file=/etc/blackbox/blackbox.yml
    volumes:
      - ../blackbox/blackbox.yml:/etc/blackbox/blackbox.yml:ro
    networks:
      default:
      inference-net:
      data-net:
    restart: unless-stopped

##########################################################
# Volumes — external; created by `make volumes`, destroyed
# only by the interactive `make nuke` (data-plane pattern).
##########################################################
volumes:
  prometheus-data:
    external: true
  loki-data:
    external: true
  grafana-data:
    external: true
```

- [ ] **Step 2: Write `docker/compose.override.yaml`**

```yaml
# Dev overlay — publishes the Grafana UI on the host. Production shape
# (compose.yaml alone) publishes nothing; on prod hosts use `make up-dev`
# for obs-plane only, or an SSH tunnel.
services:
  grafana:
    ports:
      - "${GRAFANA_HOST_PORT:-3001}:3000"
```

- [ ] **Step 3: Write `.env.example`** (synthetic values only)

```bash
# Copy to .env and set GRAFANA_ADMIN_PASSWORD at minimum.
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=changeme-set-a-real-password
GRAFANA_HOST_PORT=3001
PROMETHEUS_RETENTION=30d
LOKI_RETENTION=720h
INFERENCE_NET=inference-net
DATA_NET=data-net
```

- [ ] **Step 4: Verify both shapes parse**

```bash
cp .env.example .env
docker compose --env-file .env -f docker/compose.yaml config -q
docker compose --env-file .env -f docker/compose.yaml -f docker/compose.override.yaml config -q
```
Expected: both exit 0, no output. (`../prometheus/...` mounts referencing not-yet-written files is fine — `config` doesn't stat bind sources.)

- [ ] **Step 5: Commit**

```bash
git add docker/ .env.example
git commit -m "feat: compose files — 7 pinned services, external nets/volumes"
```

---

### Task 3: Prometheus config + alert rules

**Files:**
- Create: `prometheus/prometheus.yml`, `prometheus/rules.yml`

**Interfaces:**
- Consumes: service DNS names from Task 2 (`node-exporter`, `cadvisor`, `blackbox-exporter`, `loki`); federation aliases `vllm-router` (inference-net), `neo4j`, `qdrant` (data-net).
- Produces: job names `prometheus`, `node`, `cadvisor`, `qdrant`, `blackbox` and alert names used by dashboards (Task 7) and README (Task 9).

- [ ] **Step 1: Write `prometheus/prometheus.yml`**

```yaml
# Scrape targets reachable with ZERO member-repo changes.
# Not scrapeable in v1 (see README): Neo4j Community (no metrics endpoint),
# LiteLLM /metrics (enterprise-gated), vLLM backends (internal vllm-net only).
global:
  scrape_interval: 30s
  evaluation_interval: 30s

rule_files:
  - /etc/prometheus/rules.yml

scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]

  - job_name: node
    static_configs:
      - targets: ["node-exporter:9100"]

  - job_name: cadvisor
    static_configs:
      - targets: ["cadvisor:8080"]

  - job_name: qdrant
    static_configs:
      - targets: ["qdrant:6333"]

  - job_name: blackbox
    metrics_path: /probe
    params:
      module: [http_2xx]
    static_configs:
      - targets:
          - http://vllm-router:4000/health/liveliness
          - http://neo4j:7474
          - http://qdrant:6333/healthz
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115
```

- [ ] **Step 2: Write `prometheus/rules.yml`**

```yaml
# Alert rules — no notification channel by design (airgapped prod has no
# outbound path). They fire in Prometheus and appear read-only in
# Grafana -> Alerting -> Alert rules.
groups:
  - name: federation
    rules:
      - alert: ProbeDown
        expr: probe_success == 0
        for: 5m
        labels: {severity: critical}
        annotations:
          summary: "Endpoint {{ $labels.instance }} failing HTTP probe for 5m"
      - alert: ScrapeTargetDown
        expr: up == 0
        for: 5m
        labels: {severity: critical}
        annotations:
          summary: "Scrape target {{ $labels.job }}/{{ $labels.instance }} down for 5m"
      - alert: ContainerRestartLooping
        expr: changes(container_start_time_seconds{name!=""}[1h]) > 3
        for: 0m
        labels: {severity: warning}
        annotations:
          summary: "Container {{ $labels.name }} restarted >3x in 1h"
  - name: host
    rules:
      - alert: HostDiskAlmostFull
        expr: >
          node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"}
          / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"} < 0.15
        for: 10m
        labels: {severity: warning}
        annotations:
          summary: "Filesystem {{ $labels.mountpoint }} >85% full"
      - alert: HostMemoryPressure
        expr: node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes < 0.10
        for: 10m
        labels: {severity: warning}
        annotations:
          summary: "Host memory available <10% for 10m"
```

- [ ] **Step 3: Validate with the pinned image's promtool**

```bash
docker run --rm -v "$PWD/prometheus:/cfg:ro" --entrypoint promtool \
  docker.io/prom/prometheus:v3.13.1@sha256:3c42b892cf723fa54d2f262c37a0e1f80aa8c8ddb1da7b9b0df9455a35a7f893 \
  check config /cfg/prometheus.yml
```
Expected: `SUCCESS` for config and rules (check config recurses into rule_files; the rules path inside the container resolves because both files sit in /cfg — if it complains about `/etc/prometheus/rules.yml`, also run `check rules /cfg/rules.yml` and adjust the mount to `-v "$PWD/prometheus:/etc/prometheus:ro"` with `check config /etc/prometheus/prometheus.yml`).

- [ ] **Step 4: Commit**

```bash
git add prometheus/
git commit -m "feat: prometheus scrape config + federation/host alert rules"
```

---

### Task 4: Blackbox + Loki + Alloy configs

**Files:**
- Create: `blackbox/blackbox.yml`, `loki/loki.yaml`, `loki/rules/obs-plane.yml`, `alloy/config.alloy`

**Interfaces:**
- Consumes: Loki DNS name `loki` (Task 2); mount points `/loki/rules/fake` (Loki ruler) and `/etc/alloy/config.alloy` (Task 2).
- Produces: Loki log labels `compose_project`, `compose_service`, `container` — consumed by dashboards (Task 7) and the Loki alert rule.

- [ ] **Step 1: Write `blackbox/blackbox.yml`**

```yaml
modules:
  http_2xx:
    prober: http
    timeout: 5s
    http:
      preferred_ip_protocol: ip4
      follow_redirects: true
```

- [ ] **Step 2: Write `loki/loki.yaml`** (single-binary, filesystem, retention on, no phone-home)

```yaml
auth_enabled: false

server:
  http_listen_port: 3100

common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: "2026-01-01"
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

compactor:
  working_directory: /loki/compactor
  retention_enabled: true
  delete_request_store: filesystem

limits_config:
  retention_period: ${LOKI_RETENTION:-720h}

ruler:
  storage:
    type: local
    local:
      directory: /loki/rules
  rule_path: /loki/ruler-tmp
  enable_api: true

analytics:
  reporting_enabled: false
```

Note: rules mount in compose is `../loki/rules:/loki/rules/fake:ro` — the local ruler store expects per-tenant subdirectories and `auth_enabled: false` uses tenant id `fake`. That is Loki's documented convention, not a placeholder.

- [ ] **Step 3: Write `loki/rules/obs-plane.yml`** (the log-spike alert)

```yaml
groups:
  - name: logs
    rules:
      - alert: ErrorLogSpike
        expr: >
          sum by (compose_project, compose_service)
          (rate({compose_project=~".+"} |~ `(?i)(error|exception|traceback)` [5m])) > 1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Error-log spike in {{ $labels.compose_project }}/{{ $labels.compose_service }}"
```

- [ ] **Step 4: Write `alloy/config.alloy`** (Docker-API log collection — works with the `local` log driver)

```alloy
// Discover every container on the host and ship its stdout/stderr to Loki,
// labeled by compose project + service. Read-only Docker socket.

discovery.docker "containers" {
  host = "unix:///var/run/docker.sock"
}

discovery.relabel "containers" {
  targets = discovery.docker.containers.targets

  rule {
    source_labels = ["__meta_docker_container_label_com_docker_compose_project"]
    target_label  = "compose_project"
  }
  rule {
    source_labels = ["__meta_docker_container_label_com_docker_compose_service"]
    target_label  = "compose_service"
  }
  rule {
    source_labels = ["__meta_docker_container_name"]
    regex         = "/(.*)"
    target_label  = "container"
  }
}

loki.source.docker "containers" {
  host       = "unix:///var/run/docker.sock"
  targets    = discovery.relabel.containers.output
  forward_to = [loki.write.default.receiver]
}

loki.write "default" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
```

- [ ] **Step 5: Validate Loki + Alloy configs with the pinned images**

```bash
docker run --rm -v "$PWD/loki/loki.yaml:/etc/loki/loki.yaml:ro" \
  docker.io/grafana/loki:3.7.4@sha256:87f0a067673756a3cede1bcbf0c74875f7df9b09fddb53e399d0c576f756cfcc \
  -config.file=/etc/loki/loki.yaml -config.expand-env=true -verify-config
docker run --rm -v "$PWD/alloy/config.alloy:/cfg/config.alloy:ro" \
  docker.io/grafana/alloy:v1.18.0@sha256:491b0578c04983fd54fe99b587b6fab4404dc46d0dc16677bd6b00cc1140b308 \
  fmt /cfg/config.alloy > /dev/null
```
Expected: Loki prints a "config is valid" line and exits 0; `alloy fmt` exits 0 (parse-clean).

- [ ] **Step 6: Commit**

```bash
git add blackbox/ loki/ alloy/
git commit -m "feat: blackbox probes, loki store+ruler, alloy docker log shipping"
```

---

### Task 5: Grafana provisioning

**Files:**
- Create: `grafana/provisioning/datasources/datasources.yaml`, `grafana/provisioning/dashboards/dashboards.yaml`

**Interfaces:**
- Consumes: DNS names `prometheus`, `loki` (Task 2).
- Produces: datasource UIDs `prometheus` and `loki` — dashboard JSON in Task 7 must reference exactly these UIDs. Dashboard JSON directory `/var/lib/grafana/dashboards` (bound from `grafana/dashboards/`).

- [ ] **Step 1: Write `grafana/provisioning/datasources/datasources.yaml`**

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
  - name: Loki
    uid: loki
    type: loki
    access: proxy
    url: http://loki:3100
    editable: false
```

- [ ] **Step 2: Write `grafana/provisioning/dashboards/dashboards.yaml`**

```yaml
apiVersion: 1
providers:
  - name: obs-plane
    folder: Federation
    type: file
    disableDeletion: true
    updateIntervalSeconds: 60
    options:
      path: /var/lib/grafana/dashboards
```

- [ ] **Step 3: Commit**

```bash
git add grafana/
git commit -m "feat: grafana file provisioning — datasources + dashboard provider"
```

---

### Task 6: Makefile + bundle script

**Files:**
- Create: `Makefile`, `scripts/bundle_images.sh`, `scripts/bundle-lib.sh` (vendored)

**Interfaces:**
- Consumes: compose files + external volume names from Task 2.
- Produces: targets `network volumes pull up up-dev down restart stop ps logs health bundle nuke help`; `make health` is the smoke gate used in Task 8.

- [ ] **Step 1: Vendor `bundle-lib.sh` verbatim**

```bash
mkdir -p scripts
cp ../data-plane/scripts/bundle-lib.sh scripts/bundle-lib.sh
```
(Canonical source `nos-tromo/.github` → `configs/bundle/bundle-lib.sh`; the data-plane copy is CI-drift-checked against it, so copying it is copying canonical. Do not edit it.)

- [ ] **Step 2: Write `scripts/bundle_images.sh`** (data-plane's, minus profiles)

```bash
#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091,SC2154  # sources vendored scripts/bundle-lib.sh (sets BUNDLE_*)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
. scripts/bundle-lib.sh

[[ -n "${BUNDLE_DEV:-}" ]] || bundle_checkout_release obs-plane
bundle_version obs-plane; VER="$BUNDLE_VERSION"

COMPOSE=(docker compose --env-file .env -f docker/compose.yaml)
"${COMPOSE[@]}" pull
bundle_collect_pulled < <("${COMPOSE[@]}" config --images)

if (( ${#BUNDLE_PULLED[@]} == 0 )); then
  echo "No images resolved." >&2
  exit 1
fi
echo "Saving images: ${BUNDLE_PULLED[*]}"
docker save "${BUNDLE_PULLED[@]}" | gzip > "obs-plane-pulled-${VER}.tar.gz"
echo "Wrote: obs-plane-pulled-${VER}.tar.gz"
```

```bash
chmod +x scripts/bundle_images.sh
```

- [ ] **Step 3: Write `Makefile`**

```makefile
# obs-plane operator targets.
#
# Bespoke Makefile (data-plane pattern), NOT make/common.mk: obs-plane pulls
# rather than builds, has no Python, no profiles, and adds health/nuke
# targets common.mk does not model. It adopts the shared airgap bundle
# library (scripts/bundle-lib.sh, CI drift-checked) via scripts/bundle_images.sh.

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

INFERENCE_NET ?= $(or $(strip $(shell test -f .env && grep -E '^INFERENCE_NET=' .env | cut -d= -f2)),inference-net)
DATA_NET      ?= $(or $(strip $(shell test -f .env && grep -E '^DATA_NET=' .env | cut -d= -f2)),data-net)

# External named volumes this project owns. Keep in sync with docker/compose.yaml.
VOLUMES := prometheus-data loki-data grafana-data

COMPOSE     := docker compose --env-file .env -f docker/compose.yaml
COMPOSE_DEV := docker compose --env-file .env -f docker/compose.yaml -f docker/compose.override.yaml

.PHONY: help network volumes pull bundle up up-dev stop down restart logs ps health nuke

help:
	@echo "obs-plane — observability for the federation (Prometheus + Grafana + Loki)."
	@echo
	@echo "Lifecycle:"
	@echo "  make network   create external inference-net + data-net if missing"
	@echo "  make volumes   create the external obs volumes if missing"
	@echo "  make pull      pull all images from the registries"
	@echo "  make bundle    save images as a versioned airgap tarball"
	@echo "  make up        start (production shape — no host ports)"
	@echo "  make up-dev    like 'up', but publishes Grafana on the host"
	@echo "  make down      stop (volumes preserved)"
	@echo "  make restart   down + up"
	@echo "  make nuke      DESTROY metrics/logs/dashboard volumes (interactive)"
	@echo
	@echo "Observability of the observers:"
	@echo "  make ps        service status"
	@echo "  make health    readiness of prom/loki/grafana + all scrape targets up"
	@echo "  make logs S=prometheus   tail logs for one service"

network:
	@for n in $(INFERENCE_NET) $(DATA_NET); do \
	  docker network inspect $$n >/dev/null 2>&1 \
	    || (echo ">> creating external network $$n" && docker network create $$n); \
	done

volumes:
	@for v in $(VOLUMES); do \
	  docker volume inspect $$v >/dev/null 2>&1 \
	    || (echo ">> creating external volume $$v" && docker volume create $$v >/dev/null); \
	done

pull:
	$(COMPOSE) pull

bundle:
	./scripts/bundle_images.sh

up: network volumes
	$(COMPOSE) up --no-build -d

up-dev: network volumes
	$(COMPOSE_DEV) up --no-build -d

stop:
	$(COMPOSE) stop

down:
	$(COMPOSE) down

restart: down up

nuke:
	@echo "This will DESTROY all obs-plane volumes (metrics history, logs, dashboards):"
	@for v in $(VOLUMES); do echo "  - $$v"; done
	@read -p "Type 'nuke' to confirm: " confirm && [ "$$confirm" = "nuke" ] \
	  || (echo "aborted"; exit 1)
	$(COMPOSE) down
	@for v in $(VOLUMES); do \
	  docker volume rm $$v >/dev/null 2>&1 && echo "  removed $$v" || true; \
	done

ps:
	$(COMPOSE) ps

# Works against the portless production shape: every check runs from inside
# the prometheus container (busybox wget), which shares the project-internal
# network with grafana and loki.
health:
	@$(COMPOSE) ps --format '{{.Name}}\t{{.State}}\t{{.Status}}'
	@echo
	@$(COMPOSE) exec -T prometheus wget -qO- http://localhost:9090/-/ready && echo " prometheus: ready"
	@$(COMPOSE) exec -T prometheus wget -qO- http://loki:3100/ready >/dev/null && echo "loki: ready"
	@$(COMPOSE) exec -T prometheus wget -qO- http://grafana:3000/api/health >/dev/null && echo "grafana: ready"
	@down=$$($(COMPOSE) exec -T prometheus wget -qO- http://localhost:9090/api/v1/targets \
	  | grep -o '"health":"down"' | wc -l | tr -d ' '); \
	  if [ "$$down" != "0" ]; then echo "FAIL: $$down scrape target(s) down"; exit 1; \
	  else echo "targets: all up"; fi

logs:
ifndef S
	$(COMPOSE) logs --tail=200 -f
else
	$(COMPOSE) logs --tail=200 -f $(S)
endif
```

- [ ] **Step 4: Verify parse + lint**

```bash
make help
make -n up >/dev/null && make -n nuke >/dev/null
shellcheck scripts/bundle_images.sh
```
Expected: help text prints; dry-runs exit 0; shellcheck clean.

- [ ] **Step 5: Commit**

```bash
git add Makefile scripts/
git commit -m "feat: bespoke Makefile + airgap bundle script (bundle-lib vendored)"
```

---

### Task 7: Dashboards

**Files:**
- Create: `grafana/dashboards/federation-overview.json`, `grafana/dashboards/host.json`, `grafana/dashboards/qdrant.json`, `grafana/dashboards/logs.json`

**Interfaces:**
- Consumes: datasource UIDs `prometheus` / `loki` (Task 5), metric jobs (Task 3), log labels `compose_project`/`compose_service` (Task 4).
- Produces: four dashboard JSON files with fixed `uid`s: `fed-overview`, `fed-host`, `fed-qdrant`, `fed-logs`.

Author the JSON directly (current schema as emitted by the pinned Grafana; set `"editable": false`, fixed `uid`, `"tags": ["federation"]`). Dashboard JSON is thousands of lines of boilerplate — the engineering content is the exact panels and queries below; implement each row verbatim. Keep panel layout simple (full-width rows, default palettes/thresholds except where stated).

**federation-overview.json** (`uid: fed-overview`) — the landing page:

| Panel | Type | Datasource | Query |
|---|---|---|---|
| Probe status | stat (one per probe) | prometheus | `probe_success` — mapping 1=UP (green) / 0=DOWN (red), legend `{{instance}}` |
| Scrape targets | stat | prometheus | `count(up == 0)` — threshold: 0 green, >0 red |
| Containers running | table | prometheus | `container_last_seen{name!=""}` (instant, format table) — columns name, image |
| Container CPU | timeseries | prometheus | `sum by (name) (rate(container_cpu_usage_seconds_total{name!=""}[5m]))` |
| Container memory | timeseries | prometheus | `sum by (name) (container_memory_working_set_bytes{name!=""})`, unit bytes |
| Container restarts (1h) | table | prometheus | `changes(container_start_time_seconds{name!=""}[1h]) > 0` (instant) |
| Host disk fill | gauge | prometheus | `1 - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs\|overlay"} / node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs\|overlay"}`, unit percentunit, thresholds 0.75/0.85 |

**host.json** (`uid: fed-host`):

| Panel | Type | Query |
|---|---|---|
| CPU usage | timeseries | `1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))`, percentunit |
| Load average | timeseries | `node_load1`, `node_load5`, `node_load15` |
| Memory | timeseries | `node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes` and `node_memory_MemTotal_bytes`, bytes |
| Disk I/O | timeseries | `rate(node_disk_read_bytes_total[5m])`, `rate(node_disk_written_bytes_total[5m])`, Bps |
| Filesystem fill | bargauge | `1 - node_filesystem_avail_bytes{fstype!~"tmpfs\|overlay\|squashfs"} / node_filesystem_size_bytes{fstype!~"tmpfs\|overlay\|squashfs"}` by mountpoint, percentunit |
| Network | timeseries | `rate(node_network_receive_bytes_total{device!="lo"}[5m])`, `rate(node_network_transmit_bytes_total{device!="lo"}[5m])`, Bps |

**qdrant.json** (`uid: fed-qdrant`):

| Panel | Type | Query |
|---|---|---|
| Collections | stat | `qdrant_collections_total` |
| REST responses | timeseries | `sum by (endpoint) (rate(rest_responses_total[5m]))` |
| REST errors | timeseries | `sum(rate(rest_responses_fail_total[5m]))` |
| gRPC responses | timeseries | `sum by (endpoint) (rate(grpc_responses_total[5m]))` |
| Response time | timeseries | `rest_responses_avg_duration_seconds`, unit s |

(Qdrant metric names: verify the exact set against `wget -qO- http://qdrant:6333/metrics` during Task 8 and adjust — the names above are Qdrant's documented v1.x telemetry names.)

**logs.json** (`uid: fed-logs`):

| Panel | Type | Datasource | Query |
|---|---|---|---|
| Log volume by project | timeseries | loki | `sum by (compose_project) (rate({compose_project=~".+"}[5m]))` |
| Error rate by service | timeseries | loki | same LogQL as the ErrorLogSpike rule in `loki/rules/obs-plane.yml` (Task 4 Step 3), but `sum by (compose_service)` |
| Live tail | logs panel | loki | `{compose_project=~"$project"}` with dashboard variable `project` = `label_values(compose_project)` (include All) |

- [ ] **Step 1: Author the four JSON files per the tables above**
- [ ] **Step 2: Verify each file is valid JSON**

```bash
for f in grafana/dashboards/*.json; do python3 -m json.tool "$f" > /dev/null && echo "OK $f"; done
```
Expected: `OK` x4. (Rendering is verified live in Task 8.)

- [ ] **Step 3: Commit**

```bash
git add grafana/dashboards/
git commit -m "feat: federation-overview, host, qdrant, logs dashboards"
```

---

### Task 8: Live smoke test (`up-dev` + `health` + acceptance)

**Files:** none created — verification only. Fix-forward anything it catches (amend the relevant earlier commit or add a `fix:` commit).

**Interfaces:**
- Consumes: everything from Tasks 2–7.

- [ ] **Step 1: Bring up dev shape**

```bash
make up-dev
sleep 45
make ps
```
Expected: 7 services; prometheus + grafana `healthy`, rest `running`. (On a macOS dev machine cAdvisor may crash-loop — it needs a Linux cgroup fs. If so, note it, verify cAdvisor on a Linux host or accept `make health` reporting its target down for local dev, and continue; everything else must pass.)

- [ ] **Step 2: `make health`**

Expected: three `ready` lines and `targets: all up` — except targets that are legitimately absent on this dev host (no federation services running → blackbox/qdrant targets down). To smoke fully, start `data-plane` (`make -C ../data-plane up`) first so `qdrant`/`neo4j` resolve; `vllm-router` may stay down on a non-GPU dev machine — that *is* the ProbeDown alert path working.

- [ ] **Step 3: Acceptance checks in Grafana** (http://localhost:3001, admin / value from .env)

- Four dashboards under folder **Federation**; federation-overview shows containers incl. obs-plane's own and data-plane's.
- Logs dashboard returns lines (obs-plane's own containers at minimum) — proves Alloy → Loki works against the `local` log driver.
- Alerting → Alert rules lists the Prometheus groups (`federation`, `host`) and Loki group (`logs`).
- With `vllm-router` absent: ProbeDown shows Pending → Firing after 5m.
- Fix the Qdrant dashboard metric names against `docker compose exec -T prometheus wget -qO- http://qdrant:6333/metrics | head -40` if they differ.

- [ ] **Step 4: Verify blast radius + teardown**

```bash
docker compose --env-file .env -f docker/compose.yaml down -v   # must NOT remove external volumes
docker volume inspect prometheus-data loki-data grafana-data > /dev/null && echo "volumes survived"
make up && make down
```
Expected: `volumes survived`; up/down clean.

- [ ] **Step 5: Commit any fixes**

```bash
git add -A && git commit -m "fix: smoke-test corrections" || echo "nothing to fix"
```

---

### Task 9: CI + release-tag workflows

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/release-tag.yml`

- [ ] **Step 1: Write `.github/workflows/release-tag.yml`** (identical to data-plane's caller)

```yaml
name: release-tag
# Mints the annotated vX.Y.Z tag on merge to main by reading the VERSION file
# via the shared reusable workflow. Idempotent: no-op unless the version changed.
on:
  push:
    branches: [main]
permissions:
  contents: write
concurrency:
  group: release-tag-${{ github.ref }}
jobs:
  tag:
    uses: nos-tromo/.github/.github/workflows/release-tag.yml@v3
    with:
      version-file: VERSION
      version-source: plain
```

- [ ] **Step 2: Write `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  validate:
    # Shared infra checks incl. bundle-lib drift-check (same as data-plane).
    uses: nos-tromo/.github/.github/workflows/infra-validation.yml@v3
    with:
      compose-files: '-f docker/compose.yaml -f docker/compose.override.yaml'
      shell-scripts-glob: 'scripts/*.sh'

  validate-configs:
    # Validate every service config with the exact pinned images that will run it.
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: promtool check config + rules
        run: |
          docker run --rm -v "$PWD/prometheus:/etc/prometheus:ro" --entrypoint promtool \
            docker.io/prom/prometheus:v3.13.1@sha256:3c42b892cf723fa54d2f262c37a0e1f80aa8c8ddb1da7b9b0df9455a35a7f893 \
            check config /etc/prometheus/prometheus.yml
      - name: loki verify-config
        run: |
          docker run --rm -v "$PWD/loki/loki.yaml:/etc/loki/loki.yaml:ro" \
            docker.io/grafana/loki:3.7.4@sha256:87f0a067673756a3cede1bcbf0c74875f7df9b09fddb53e399d0c576f756cfcc \
            -config.file=/etc/loki/loki.yaml -config.expand-env=true -verify-config
      - name: alloy fmt (parse check)
        run: |
          docker run --rm -v "$PWD/alloy/config.alloy:/cfg/config.alloy:ro" \
            docker.io/grafana/alloy:v1.18.0@sha256:491b0578c04983fd54fe99b587b6fab4404dc46d0dc16677bd6b00cc1140b308 \
            fmt /cfg/config.alloy > /dev/null
      - name: blackbox config check
        run: |
          docker run --rm -v "$PWD/blackbox/blackbox.yml:/etc/blackbox/blackbox.yml:ro" --entrypoint /bin/blackbox_exporter \
            docker.io/prom/blackbox-exporter:v0.28.0@sha256:e753ff9f3fc458d02cca5eddab5a77e1c175eee484a8925ac7d524f04366c2fc \
            --config.file=/etc/blackbox/blackbox.yml --config.check
      - name: dashboards are valid JSON
        run: |
          for f in grafana/dashboards/*.json; do python3 -m json.tool "$f" > /dev/null; done
```

If `infra-validation.yml@v3` requires inputs this repo lacks (check its `workflow_call` inputs in `nos-tromo/.github` first — e.g. a mandatory `dockerfiles-glob`), pass a glob that matches nothing, exactly as data-plane passes `docker/Dockerfile.*` with no Dockerfiles present. Note the reusable workflow's compose check needs `.env` — if it doesn't create one from `.env.example` automatically (check how data-plane's run handles `NEO4J_PASSWORD`), add a step before it: `cp .env.example .env`.

- [ ] **Step 3: Verify workflow YAML parses**

```bash
python3 -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('.github/workflows/*.yml')]" && echo OK
```
Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add .github/
git commit -m "ci: infra-validation + pinned-image config checks; release-tag caller"
```

---

### Task 10: README + CLAUDE.md + PR

**Files:**
- Create: `CLAUDE.md`
- Modify: `README.md` (replace stub)

- [ ] **Step 1: Write `README.md`** — data-plane README structure, with these sections (write them fully, in the fleet's voice):

1. **What lives here** — service table (the 7 services, role, network membership).
2. **What is observable / what is not** — copy the target matrix from `docs/2026-07-22-obs-plane-design.md` §"What is observable in v1", including the Neo4j-Community / LiteLLM-enterprise / vLLM-backend limitations and their follow-ups.
3. **Quick start** — `cp .env.example .env`, set `GRAFANA_ADMIN_PASSWORD`, `make network volumes up-dev`, Grafana at `localhost:3001`.
4. **Grafana access on production** — production shape publishes no ports; `make up-dev` or SSH tunnel.
5. **Operating** — `make ps / health / logs S=... / down / bundle / nuke`, noting `OBS_PLANE_VERSION_OVERRIDE` for working-tree bundles (no `bundle-dev`, data-plane pattern).
6. **Alerting** — rules fire in Prometheus/Loki, visible in Grafana → Alerting; no notification channel by design (airgap); staging Telegram contact point is a follow-up.
7. **Layout** — directory tree.

- [ ] **Step 2: Write `CLAUDE.md`** — short, data-plane-style:

- One paragraph: what obs-plane is, pure consumer, zero member changes.
- Hard rules: image pins are `tag@digest`, never floating; no runtime fetching/telemetry; no host ports in base compose; `make nuke` is the only volume destroyer.
- Commands: the Makefile targets; config-validation one-liners from CI.
- Pointers: `docs/2026-07-22-obs-plane-design.md`, `../deploy/README.md`, infra root `CLAUDE.md`.
- Include the federation data-confidentiality hard rule block (copy the wording pattern used in the other nine repos' CLAUDE.md).

- [ ] **Step 3: Final sweep + push + PR**

Verify no real local paths in the entire tree:
```bash
grep -rnE '/(Users|home)/[A-Za-z0-9_.-]+' . --exclude-dir=.git && echo "FAIL: local paths found" || echo "clean"
```

Then commit and push:
```bash
git add README.md CLAUDE.md && git commit -m "docs: README + CLAUDE.md"
git push -u origin feature/obs-plane-v1
gh pr create --title "obs-plane v1: federation observability plane" --body "Implements docs/2026-07-22-obs-plane-design.md — Prometheus + Grafana + Loki + Alloy + node-exporter + cAdvisor + blackbox-exporter, all images pinned tag@digest, data-plane-style Makefile/bundle/nuke, CI config validation with the pinned images. Zero changes to other federation members."
```

- [ ] **Step 4: Watch CI, fix if red**

```bash
gh pr checks --watch
```
Expected: `validate` + `validate-configs` green. On merge, release-tag mints `v0.1.0`.

---

## Deferred (not in this plan — from spec §Follow-ups)

deploy-repo tier wiring; vllm-service backend `inference-net` attach; app FastAPI instrumentation; staging Telegram contact point.
