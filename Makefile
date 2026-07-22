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
VOLUMES := prometheus-data loki-data grafana-data alloy-data

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
