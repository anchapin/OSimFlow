# =============================================================================
# OSimFlow Makefile — developer best-practices targets
# =============================================================================
# Each target mirrors a CI job. Run `make help` to list.
#
# Conventions:
#   - .PHONY marks targets that are not file outputs.
#   - All tools run via the project .venv.
#   - Pre-commit hooks wrap the same checks; `make precommit` is the
#     pre-push safety net.
# =============================================================================

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy
PYTEST := $(VENV)/bin/pytest
PRECOMMIT := $(VENV)/bin/pre-commit

.PHONY: help install lint format typecheck test test-cov test-fast contract byos-generate docs-sync agents-contract openapi-sync precommit act clean

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## pip install -e ".[dev,aws,slurm,kubernetes,api,sensitivity,optimization,ga]"
	$(PY) -m pip install -e ".[dev,aws,slurm,kubernetes,api,sensitivity,optimization,ga]"

lint: ## ruff check (linter)
	$(RUFF) check .

format: ## ruff format (write)
	$(RUFF) format .

typecheck: ## mypy --strict (osimflow/)
	$(MYPY) osimflow

test: ## pytest (full suite, no coverage gate — use test-cov for the gate)
	$(PYTEST) -o addopts=""

test-cov: ## pytest --cov with 83% gate (mirrors CI test job)
	$(PYTEST) --cov=osimflow --cov-report=xml --cov-report=term-missing --cov-fail-under=83 --ignore=tests/contract

test-fast: ## pytest contract only (pre-commit mirror)
	$(PYTEST) -o addopts="" tests/contract -x -q

byos-generate: ## regenerate the inline BYOS subprocess runner from osimflow.byos_contract
	$(PY) tools/_generate_byos_runner.py

contract: byos-generate agents-contract docs-sync openapi-sync ## run all contract checks (BYOS generator + AGENTS.md + docs/ + openapi)

agents-contract: ## check AGENTS.md / code drift
	$(PY) tools/check_agents_contract.py

docs-sync: ## check docs/ references resolve
	$(PY) tools/check_docs_sync.py

openapi-sync: ## check docs/openapi.json matches the live FastAPI app (issue #1049)
	$(PY) -m pip install -e ".[dev,api]" --quiet
	$(PY) tools/check_openapi_sync.py --summary

precommit: ## pre-commit run --all-files
	$(PRECOMMIT) run --all-files

act: ## local CI mirror (lint + typecheck + test + contract + security)
	act -j lint
	act -j typecheck
	act -j test
	act -j contract
	act -j security

clean: ## remove caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} +
