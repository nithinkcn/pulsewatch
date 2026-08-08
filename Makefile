.PHONY: help install test test-unit lint fmt migrate revision up down logs clean

PY := .venv/bin/python

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install dependencies
	uv venv --python 3.13
	uv pip install -e ".[dev]"

test: ## Full suite (starts PostgreSQL in a container)
	$(PY) -m pytest -q

test-unit: ## Only the tests that need no containers
	$(PY) -m pytest tests/test_evaluator.py tests/test_probes.py -q

lint: ## ruff + mypy --strict
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy app

fmt: ## Autoformat and autofix
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

migrate: ## Apply migrations
	$(PY) -m alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add x"
	$(PY) -m alembic revision --autogenerate -m "$(m)"

up: ## Bring the whole stack up
	docker compose up --build

down: ## Tear it down, including volumes
	docker compose down -v

logs: ## Follow worker and beat logs
	docker compose logs -f worker beat

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
