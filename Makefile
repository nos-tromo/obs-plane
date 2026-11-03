# obs-plane operator targets.
#
# Bespoke Makefile (data-plane pattern), NOT make/common.mk: obs-plane pulls
# rather than builds, has no Python, and adds health/nuke targets common.mk
# does not model. One compose profile exists: `gpu` (dcgm-exporter), enabled
# per-host via COMPOSE_PROFILES=gpu in .env. It adopts the shared airgap bundle
# library (scripts/bundle-lib.sh, CI drift-checked) via scripts/bundle_images.sh.

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

INFERENCE_NET ?= $(or $(strip $(shell test -f .env && grep -E '^INFERENCE_NET=' .env | cut -d= -f2)),inference-net)
DATA_NET      ?= $(or $(strip $(shell test -f .env && grep -E '^DATA_NET=' .env | cut -d= -f2)),data-net)
EDGE_NET      ?= $(or $(strip $(shell test -f .env && grep -E '^EDGE_NET=' .env | cut -d= -f2)),edge-net)

# External named volumes this project owns. Keep in sync with docker/compose.yaml.
# `make nuke` destroys VOLUMES only: the audit trail leaves its volumes only
# through loki-audit's retention (docs/audit.md).
VOLUMES       := prometheus-data loki-data grafana-data alloy-data
AUDIT_VOLUMES := loki-audit-data grafana-audit-data audit-verifier-state
AUDIT_EXPORTS := audit-exports

COMPOSE     := docker compose --env-file .env -f docker/compose.yaml
COMPOSE_DEV := docker compose --env-file .env -f docker/compose.yaml -f docker/compose.override.yaml

.PHONY: help network volumes pull bundle up up-dev stop down restart logs ps health nuke \
        audit-verify audit-export audit-anchor

help:
	@echo "obs-plane — observability for the federation (Prometheus + Grafana + Loki)."
	@echo
	@echo "Lifecycle:"
	@echo "  make network   create external inference-net + data-net + edge-net if missing"
	@echo "  make volumes   create the external obs volumes if missing"
	@echo "  make pull      pull all images from the registries"
	@echo "  make bundle    save images as a versioned airgap tarball"
	@echo "  make up        start (production shape — no host ports)"
	@echo "  make up-dev    like 'up', but publishes Grafana on the host"
	@echo "  make down      stop (volumes preserved)"
	@echo "  make restart   down + up"
	@echo "  make nuke      DESTROY metrics/logs/dashboard volumes (interactive; not the audit trail)"
	@echo
	@echo "Audit trail (docs/audit.md; signs the files when AUDIT_SIGNING_KEY is set):"
	@echo "  make audit-verify FROM=<ISO> TO=<ISO>   check the hash chains of a range"
	@echo "  make audit-export FROM=<ISO> TO=<ISO> [SERVICE=<project>] [REF=<text>]"
	@echo "                                          export a range to $(AUDIT_EXPORTS)/ as JSONL"
	@echo "  make audit-anchor DAY=<YYYY-MM-DD>      write that day's anchor to $(AUDIT_EXPORTS)/"
	@echo
	@echo "Observability of the observers:"
	@echo "  make ps        service status"
	@echo "  make health    readiness of prom/loki/grafana + all scrape targets up"
	@echo "  make logs S=prometheus   tail logs for one service"
	@echo
	@echo "GPU hosts: set COMPOSE_PROFILES=gpu in .env to run dcgm-exporter."

network:
	@for n in $(INFERENCE_NET) $(DATA_NET) $(EDGE_NET); do \
	  docker network inspect $$n >/dev/null 2>&1 \
	    || (echo ">> creating external network $$n" && docker network create $$n); \
	done

volumes:
	@for v in $(VOLUMES) $(AUDIT_VOLUMES); do \
	  docker volume inspect $$v >/dev/null 2>&1 \
	    || (echo ">> creating external volume $$v" && docker volume create $$v >/dev/null); \
	done
	@# alloy runs as its internal user (uid 473, deploy ADR 0001) and owns
	@# its positions volume. Idempotent; reuses the alloy image itself so
	@# airgap hosts need no extra image. Skipped (warning) until the image
	@# is loaded — run `make volumes` again after `make load`/`make pull`.
	@ALLOY_IMG=$$(grep -o 'docker\.io/grafana/alloy:[^"]*' docker/compose.yaml | head -1); \
	docker image inspect $$ALLOY_IMG >/dev/null 2>&1 \
	  && docker run --rm --entrypoint sh --user root -v alloy-data:/v $$ALLOY_IMG -c 'chown -R 473:473 /v' \
	  || echo ">> alloy image not loaded yet — re-run 'make volumes' after images are available (alloy-data must be owned by 473:473)"
	@# audit-verifier runs as the python image's `nobody` (65534) and keeps
	@# its checkpoints in audit-verifier-state; same idempotent pattern.
	@PY_IMG=$$(grep -o 'docker\.io/library/python:[^"]*' docker/compose.yaml | head -1); \
	docker image inspect $$PY_IMG >/dev/null 2>&1 \
	  && docker run --rm --user root -v audit-verifier-state:/v $$PY_IMG chown -R 65534:65534 /v \
	  || echo ">> python image not loaded yet — re-run 'make volumes' after images are available (audit-verifier-state must be owned by 65534:65534)"

# --profile gpu: always fetch dcgm-exporter too, so airgap prep on a
# non-GPU build host still bundles the image (profile-gated services are
# otherwise invisible to pull/config).
pull:
	$(COMPOSE) --profile gpu pull

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
	@echo "This will DESTROY the obs-plane ops volumes (metrics history, logs, dashboards):"
	@for v in $(VOLUMES); do echo "  - $$v"; done
	@echo "The audit trail's volumes are kept: $(AUDIT_VOLUMES)"
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
# network with grafana (through grafana-guard) and loki. The audit side is
# reachable only on its own networks, so its checks run from grafana-audit
# (also busybox wget): Grafana itself on loopback, the store through
# loki-audit-read, and the guard, which must refuse a request without the
# gateway's secret.
health:
	@$(COMPOSE) ps --format '{{.Name}}\t{{.State}}\t{{.Status}}'
	@echo
	@$(COMPOSE) exec -T prometheus wget -qO- http://localhost:9090/-/ready && echo " prometheus: ready"
	@$(COMPOSE) exec -T prometheus wget -qO- http://loki:3100/ready >/dev/null && echo "loki: ready"
	@$(COMPOSE) exec -T prometheus wget -qO- http://grafana:3000/api/health >/dev/null && echo "grafana: ready"
	@$(COMPOSE) exec -T grafana-audit wget -qO- http://127.0.0.1:3001/api/health >/dev/null && echo "grafana-audit: ready"
	@$(COMPOSE) exec -T grafana-audit wget -qO- http://loki-audit-read:3100/ready >/dev/null && echo "loki-audit: ready (through loki-audit-read)"
	@$(COMPOSE) exec -T grafana-audit sh -c 'wget -qO- http://127.0.0.1:3000/api/health 2>&1 | grep -q " 403 "' \
	  && echo "grafana-audit-guard: refuses requests without the edge secret"
	@# The dcgm job only has a live target when the `gpu` profile is on;
	@# exclude it from the down-check otherwise (expected down).
	@exclude='^$$'; grep -Eq '^COMPOSE_PROFILES=.*gpu' .env 2>/dev/null || exclude='^dcgm$$'; \
	  down=$$($(COMPOSE) exec -T prometheus wget -qO- 'http://localhost:9090/api/v1/query?query=up==0' \
	    | grep -o '"job":"[^"]*"' | cut -d'"' -f4 | sort -u | grep -Ev "$$exclude" || true); \
	  if [ -n "$$down" ]; then echo "FAIL: scrape job(s) down:" $$down; exit 1; \
	  else echo "targets: all up"; fi

logs:
ifndef S
	$(COMPOSE) logs --tail=200 -f
else
	$(COMPOSE) logs --tail=200 -f $(S)
endif

# ---- Audit trail (docs/audit.md) --------------------------------------------
# One-shot runs of the audit-verifier service. Arguments come from the
# environment (make exports command-line variables to recipes), so values
# with spaces or quotes pass through intact. Commands that write files mount
# $(AUDIT_EXPORTS)/ and run as the invoking user, so the files are theirs.
AUDIT_RUN     := $(COMPOSE) run --rm --no-deps -T
AUDIT_RUN_OUT := $(AUDIT_RUN) --user "$$(id -u):$$(id -g)" -v "$(CURDIR)/$(AUDIT_EXPORTS):/exports"

audit-verify:
	@[ -n "$$FROM" ] && [ -n "$$TO" ] \
	  || { echo "usage: make audit-verify FROM=<ISO 8601> TO=<ISO 8601>" >&2; exit 2; }
	@$(AUDIT_RUN) audit-verifier verify --from "$$FROM" --to "$$TO"

audit-export:
	@[ -n "$$FROM" ] && [ -n "$$TO" ] \
	  || { echo "usage: make audit-export FROM=<ISO 8601> TO=<ISO 8601> [SERVICE=<project>] [REF=<text>]" >&2; exit 2; }
	@mkdir -p $(AUDIT_EXPORTS)
	@set -- export --from "$$FROM" --to "$$TO"; \
	  [ -z "$$SERVICE" ] || set -- "$$@" --service "$$SERVICE"; \
	  [ -z "$$REF" ] || set -- "$$@" --ref "$$REF"; \
	  out=$$($(AUDIT_RUN_OUT) audit-verifier "$$@") && ./scripts/audit_sign.sh $(AUDIT_EXPORTS) $$out

audit-anchor:
	@[ -n "$$DAY" ] || { echo "usage: make audit-anchor DAY=<YYYY-MM-DD>" >&2; exit 2; }
	@mkdir -p $(AUDIT_EXPORTS)
	@out=$$($(AUDIT_RUN_OUT) audit-verifier anchor --day "$$DAY") && ./scripts/audit_sign.sh $(AUDIT_EXPORTS) $$out
