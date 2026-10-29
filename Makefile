.DEFAULT_GOAL := help

COMPOSE := docker compose
SERVICE ?=
TAIL ?= 100

ifeq ($(OS),Windows_NT)
PYTHON := .venv/Scripts/python.exe
else
PYTHON := .venv/bin/python
endif

.PHONY: help up down stop start restart rebuild ps \
	logs logs-app logs-bigpms logs-db shell db test reset \
	lint lint-fix format format-check check test-unit test-e2e test-all install-dev

help: ## Show available commands
	@echo "make up              Build and start services"
	@echo "make down            Remove containers and network"
	@echo "make stop            Stop services"
	@echo "make start           Start stopped containers"
	@echo "make restart         Restart services"
	@echo "make rebuild         Rebuild without cache and recreate"
	@echo "make ps              Show container status"
	@echo "make logs            Follow service logs"
	@echo "make logs-app        Follow application logs"
	@echo "make logs-bigpms     Follow BigPMS logs"
	@echo "make logs-db         Follow PostgreSQL logs"
	@echo "make shell           Open app container shell"
	@echo "make db              Open PostgreSQL console"
	@echo "make install-dev     Install lint and test dependencies"
	@echo "make test            Run all unit tests (no Docker needed)"
	@echo "make test-unit       Run all unit tests (alias)"
	@echo "make test-e2e        Run disposable Docker E2E tests"
	@echo "make test-all        Run unit tests followed by E2E tests"
	@echo "make lint            Check lint errors in app/"
	@echo "make lint-fix        Fix correctable lint errors in app/"
	@echo "make format          Fix lint errors and format app/"
	@echo "make format-check    Check formatting in app/"
	@echo "make check           Run lint and formatting checks"
	@echo "make reset           DELETE database data and recreate"
	@echo "Options: SERVICE=app TAIL=200"

up: ## Build and start all services in the background
	$(COMPOSE) up --build -d

down: ## Remove containers and the Compose network
	$(COMPOSE) down

stop: ## Stop services without removing containers
	$(COMPOSE) stop $(SERVICE)

start: ## Start existing stopped containers
	$(COMPOSE) start $(SERVICE)

restart: ## Restart services; optionally pass SERVICE=app
	$(COMPOSE) restart $(SERVICE)

rebuild: ## Rebuild images without cache and recreate containers
	$(COMPOSE) build --no-cache $(SERVICE)
	$(COMPOSE) up -d --force-recreate $(SERVICE)

ps: ## Show service status and ports
	$(COMPOSE) ps -a

logs: ## Follow logs; optionally pass SERVICE=app TAIL=200
	$(COMPOSE) logs --tail=$(TAIL) -f $(SERVICE)

logs-app: ## Follow application logs
	$(COMPOSE) logs --tail=$(TAIL) -f app

logs-bigpms: ## Follow mock BigPMS logs
	$(COMPOSE) logs --tail=$(TAIL) -f mock-bigpms

logs-db: ## Follow PostgreSQL logs
	$(COMPOSE) logs --tail=$(TAIL) -f db

shell: ## Open a shell inside the app container
	$(COMPOSE) exec app sh

db: ## Open the PostgreSQL console
	$(COMPOSE) exec db psql -U leaseflow -d leaseflow

install-dev: ## Install local tooling and test dependencies
	$(PYTHON) -m pip install -r requirements-test.txt

test: test-unit ## Run all unit tests without Docker or PostgreSQL

test-unit: ## Test requirement 1 functions with mocked external dependencies
	$(PYTHON) -m pytest -q tests/unit

test-e2e: ## Build, test and clean up an isolated Docker stack
	$(PYTHON) scripts/run_e2e.py

test-all: ## Run unit tests followed by Docker E2E tests
	$(MAKE) test-unit
	$(MAKE) test-e2e

lint: ## Check Python lint errors in app/
	$(PYTHON) -m ruff check app/

lint-fix: ## Fix automatically correctable lint errors in app/
	$(PYTHON) -m ruff check --fix app/

format: ## Fix lint errors and format Python files in app/
	$(PYTHON) -m ruff check --fix app/
	$(PYTHON) -m ruff format app/

format-check: ## Check formatting without changing files
	$(PYTHON) -m ruff format --check app/

check: lint format-check ## Run lint and formatting checks

reset: ## DELETE database data and recreate the seeded environment
	$(COMPOSE) down -v
	$(COMPOSE) up --build -d
