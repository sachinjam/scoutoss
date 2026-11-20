# ScoutOSS developer entry points. `make help` lists targets.
SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
COMPOSE ?= docker compose
TEST_COMPOSE := $(COMPOSE) -f docker-compose.yml -f docker-compose.test.yml
UV ?= uv
PY := $(UV) run --frozen
SERVICES := services/common services/indexer services/copilot

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS=":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- stack
.PHONY: prereqs
prereqs: ## Generate .env secrets, dev TLS certs, fetch pinned XNAT plugins
	@scripts/init-env.sh
	@scripts/gen-certs.sh
	@scripts/fetch-xnat-plugins.sh

.PHONY: up
up: prereqs ## Build and boot the whole stack (waits until healthy and bootstrapped)
	$(COMPOSE) up -d --build
	@scripts/wait-for-stack.sh

.PHONY: down
down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down --remove-orphans

.PHONY: clean
clean: ## Stop the stack and DELETE all volumes (data loss!)
	$(COMPOSE) down -v --remove-orphans

.PHONY: logs
logs: ## Tail JSON logs of all services (SERVICE=name to filter)
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: seed
seed: ## Push 10 synthetic (fake-PHI) studies through the de-id gateway and HL7 feed
	$(PY) python scripts/seed.py

.PHONY: health
health: ## Health-check every service
	@scripts/healthcheck.sh

# ---------------------------------------------------------------- quality
.PHONY: lint
lint: ## ruff + mypy --strict on services/*
	$(PY) ruff check services scripts tests
	$(PY) ruff format --check services scripts tests
	$(PY) mypy --strict services/common/scoutoss_common services/indexer/scoutoss_indexer services/copilot/scoutoss_copilot

.PHONY: fmt
fmt: ## Auto-format Python
	$(PY) ruff check --fix services scripts tests
	$(PY) ruff format services scripts tests

.PHONY: test-unit
test-unit: ## Unit tests with the >=80% coverage gate
	$(PY) pytest services --cov --cov-report=term --cov-report=xml:coverage-unit.xml -m "not integration"

.PHONY: test-integration
test-integration: ## Integration tests (Postgres + OpenSearch via testcontainers)
	$(PY) pytest tests/integration -m integration -v

.PHONY: test
test: ## Unit + integration tests with combined coverage (CI gate)
	$(PY) pytest services tests/integration --cov --cov-report=term --cov-report=xml:coverage.xml

.PHONY: test-in-docker
test-in-docker: ## Run the integration suite inside docker-compose.test.yml
	$(TEST_COMPOSE) --profile test run --rm --build test-runner

.PHONY: e2e
e2e: ## End-to-end smoke test against a running stack (`make up` first)
	@scripts/e2e.sh

.PHONY: ui-test
ui-test: ## Frontend type-check, lint and unit tests
	cd services/ui && npm ci && npm run typecheck && npm run lint && npm test

.PHONY: dashboards
dashboards: ## Regenerate dashboards/scoutoss.ndjson
	python3 scripts/gen_dashboards.py

.PHONY: helm-lint
helm-lint: ## Lint and render the Helm chart (uses a pinned helm container if helm is absent)
	@if command -v helm >/dev/null; then helm lint helm/scoutoss && helm template scoutoss helm/scoutoss >/dev/null; \
	else docker run --rm -v "$(CURDIR)/helm:/helm" alpine/helm:3.18.4 lint /helm/scoutoss; fi
