# Container hardening and residual findings

How obs-plane's nine containers are constrained, and the two privilege
trade-offs that were made deliberately rather than overlooked. The
governing decision is deploy ADR 0001,
[0001-container-engine-docker.md](../../deploy/docs/decisions/0001-container-engine-docker.md),
which also defines the reversal triggers referenced below.

## Baseline

Eight of the nine services take the `x-hardened` anchor —
`no-new-privileges` and `cap_drop: ALL`. `cadvisor` is the exception and
takes neither (see below). Four of the eight — `socket-proxy`,
`node-exporter`, `blackbox-exporter` and `dcgm-exporter` — additionally
run read-only. Alloy no longer mounts the Docker socket — it reaches the API
through `socket-proxy`, which enables only the read-only endpoints Alloy
needs and rejects everything else (403), and it runs as its internal
uid 473 (`make volumes` chowns `alloy-data` accordingly; on hosts with an
existing volume re-run `make volumes` once).

The shared constraints are applied through the `x-hardened` anchor in
`docker/compose.yaml`; the socket-proxy allowlist (only `GET /containers`,
`/networks`, `/events`, `/_ping`) is set on the `socket-proxy` service in
the same file.

## Residual finding: cAdvisor runs privileged

**Deliberately accepted:** `cadvisor` is the one service that does not
take the `x-hardened` anchor — it keeps the default capability set and
runs `privileged: true`, with `/var/run` (which includes the Docker
socket) and `/var/lib/docker` mounted read-only — its documented
requirement for full per-container stats. This is the one remaining socket exposure in the
stack; it is accepted in exchange for federation-wide container metrics
and revisited only if an assessment rejects it (see deploy ADR 0001's
reversal triggers).

## dcgm-exporter is not a second exception

`dcgm-exporter` is **not** a second exception: it keeps the full hardened
shape (no `SYS_ADMIN`), which means DCGM's Datacenter Profiling metrics
(`DCGM_FI_PROF_*` — tensor/SM occupancy, PCIe/NVLink throughput) are
deliberately unavailable. The collected NVML-backed set (utilization,
VRAM, temperature, power, clocks, ECC) is pinned in `dcgm/counters.csv`.

The same gap is listed from the coverage side in
[coverage.md](coverage.md#not-available).
