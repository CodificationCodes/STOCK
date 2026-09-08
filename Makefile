# Developer shortcuts. `make help` lists them.

VENV    ?= .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
PYTEST  := $(VENV)/bin/pytest
RUFF    := $(VENV)/bin/ruff

.DEFAULT_GOAL := help
.PHONY: help install dev server client test test-cov lint format typecheck \
        migrate migration reset-db docker docker-dev clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install the client only
	./install.sh

dev: ## Install everything, editable, with test tooling
	./install.sh --dev

server: ## Run a development server (fast clock, debug on)
	$(VENV)/bin/stockgame-server --debug --tick-seconds 1 --day-seconds 300

client: ## Run the terminal client against localhost
	$(VENV)/bin/stockgame --server 127.0.0.1:8765

test: ## Run the test suite
	$(PYTEST)

test-cov: ## Run tests with a coverage report
	$(PYTEST) --cov --cov-report=term-missing --cov-report=html

lint: ## Check formatting and lint rules
	$(RUFF) check src tests
	$(RUFF) format --check src tests

format: ## Auto-fix lint issues and format
	$(RUFF) check --fix src tests
	$(RUFF) format src tests

typecheck: ## Run mypy
	$(VENV)/bin/mypy

migrate: ## Apply pending database migrations
	$(VENV)/bin/alembic upgrade head

migration: ## Autogenerate a migration: make migration m="add widgets"
	$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

reset-db: ## DESTROY the local database and start a fresh world
	rm -rf data/stockgame.sqlite3 data/stockgame.sqlite3-wal data/stockgame.sqlite3-shm
	@echo "local world deleted; the next server start will seed a new one"

docker: ## Build and start the production stack
	docker compose up -d --build

docker-dev: ## Start the development stack with reload
	docker compose -f docker-compose.yml -f docker/docker-compose.dev.yml up --build

clean: ## Remove caches and build artefacts
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .ruff_cache \
	       .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
