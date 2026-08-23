# obs-plane documentation

This directory holds the reference material for **obs-plane**, the
observability tier of the nos-tromo federation. It complements the
top-level [`README.md`](../README.md) — which covers what the plane is,
how to bring it up, and the day-to-day operator commands — with the
detail that would otherwise crowd it out.

## Table of contents

| Document | What it covers |
|---|---|
| [coverage.md](coverage.md) | Every scrape/probe target: what is observable, what is deliberately not, and which targets are down by design |
| [grafana-access.md](grafana-access.md) | Reaching Grafana through the edge gateway, the `X-Auth-User` trust assumption, and the dev-overlay / SSH-tunnel fallbacks |
| [hardening.md](hardening.md) | Container hardening baseline (deploy ADR 0001) and the accepted residual findings — cAdvisor's `privileged: true`, dcgm-exporter's dropped profiling metrics |

Design history lives alongside these files as dated `YYYY-MM-DD-*.md`
documents (the v1 design doc and its build plan); they record decisions as
they were made and are not kept current.

## Who this is for

- **Operators** bringing the plane up or answering "why is this target
  down?" — start with the top-level [`README.md`](../README.md), then
  [coverage.md](coverage.md).
- **Anyone who needs a dashboard** — [grafana-access.md](grafana-access.md)
  covers every access path, production and local.
- **Reviewers auditing the privilege posture** —
  [hardening.md](hardening.md), then deploy ADR 0001.

## Conventions used in these docs

- **Config references** name the repo-relative file that owns the setting
  (for example `prometheus/prometheus.yml`, `docker/compose.yaml`), so the
  authoritative version is always one open away.
- **Service names are network aliases** — `qdrant:6333`,
  `vllm-router:4000` and friends are how obs-plane reaches targets across
  compose projects, not host addresses.
- **Cross-repo links are workspace-relative** (`../../deploy/...`), which
  resolves inside the `infra/` workspace where all federation repos are
  checked out side by side.
- Documentation is plain Markdown (GitHub Flavored). No build step.
