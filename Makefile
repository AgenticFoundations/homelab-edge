.DEFAULT_GOAL := help
UV ?= uv
APP = $(UV) run --locked python -m homelab_edge

.PHONY: help setup doctor network up down restart status logs check test lint typecheck

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install locked dependencies and create a private .env; starts nothing
	$(UV) sync --locked
	$(APP) setup

doctor: ## Check configuration and OrbStack without starting the tunnel
	$(APP) doctor

network: ## Create the shared network if absent; never starts a container
	$(APP) network

up: ## Start the shared tunnel and wait for a Cloudflare connection
	$(APP) up

down: ## Stop the shared tunnel; preserve networking and application containers
	$(APP) down

restart: ## Restart the shared tunnel with current .env configuration
	$(APP) restart

status: ## Show shared tunnel container status
	$(APP) status

logs: ## Follow tunnel logs
	$(APP) logs

check: ## Check whether the tunnel has an active Cloudflare connection
	$(APP) check

test: ## Run offline unit tests
	$(UV) run --locked pytest -q

lint: ## Check Python style and formatting
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .

typecheck: ## Run strict mypy on application source
	$(UV) run --locked mypy
