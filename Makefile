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
RUFF := $(VENV)/bin/ruff
BLACK := $(VENV)/bin/black
MYPY := $(VENV)/bin/mypy
PYTEST := $(VENV)/bin/pytest
PRECOMMIT := $(VENV)/bin/pre-commit

.PHONY: help install lint format typecheck test test-cov test-fast contract docs-sync agents-contract precommit act clean

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## pip install -e ".[dev,aws,slurm]"
	$(PY) -m pip install -e ".[dev,aws,slurm]"

lint: ## ruff check (linter)
	$(RUFF) check .

format: ## ruff format + black (write)
	$(RUFF) format .
	$(BLACK) .

typecheck: ## mypy --strict (osimflow/)
	$(MYPY) osimflow

test: ## pytest (full suite, no coverage gate)
	$(PYTEST)

test-cov: ## pytest --cov with 85% gate
	$(PYTEST) --cov=osimflow --cov-report=term-missing --cov-fail-under=85

test-fast: ## pytest unit + contract only (pre-commit mirror)
	$(PYTEST) tests/unit tests/contract -x -q

contract: agents-contract docs-sync ## run both contract checks

agents-contract: ## check AGENTS.md / code drift
	$(PY) tools/check_agents_contract.py

docs-sync: ## check docs/ references resolve
	$(PY) tools/check_docs_sync.py

precommit: ## pre-commit run --all-files
	$(PRECOMMIT) run --all-files

act: ## local CI mirror (lint + unit + contract)
	act -j lint
	act -j unit
	act -j agents-contract

clean: ## remove caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} +
