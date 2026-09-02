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

# CI pytest flags (issue #1476) — single source of truth for the merge-gate
# invocation. The CI `test` job (.github/workflows/ci.yml) runs
# `make test-cov`, which composes PYTEST_CI_FLAGS + PYTEST_COV_FLAGS; the
# local `test` target reuses the same PYTEST_CI_FLAGS with the coverage
# gate off. Editing these variables changes CI and local targets together —
# do not duplicate the flags inline in ci.yml.
#
# `chaos` is deselected in the merge gate on purpose (issue #1468): chaos
# tests may use probabilistic fault injection and are exercised by the
# dedicated, NON-gating `chaos` CI job (`pytest -m chaos`), so a flake
# there cannot block PRs. tests/contract/test_ci_marker_policy.py pins
# this policy to the marker docs in pyproject.toml.
PYTEST_CI_FLAGS := -n 2 --dist loadgroup --timeout=120 --ignore=tests/contract -m "not nomad_e2e and not slow and not chaos"
PYTEST_COV_FLAGS := --cov=osimflow --cov-report=xml --cov-report=term-missing --cov-fail-under=82

.PHONY: help install lint format typecheck test test-cov test-fast smoke contract byos-generate docs-sync agents-contract openapi-sync precommit act clean

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: | $(VENV) ## pip install -e ".[dev,aws,slurm,kubernetes,api,sensitivity,optimization,ga]"
	$(PY) -m pip install -e ".[dev,aws,slurm,kubernetes,api,sensitivity,optimization,ga]"

# Bootstrap the virtualenv on fresh clones (issue #1447): created only
# when absent (order-only prerequisite — never rebuilt once it exists).
$(VENV):
	python3 -m venv $(VENV)
	$(VENV)/bin/python -m pip install --upgrade pip

lint: ## ruff check (linter)
	$(RUFF) check .

format: ## ruff format (write)
	$(RUFF) format .

typecheck: ## mypy --strict (osimflow/)
	$(MYPY) osimflow

test: ## pytest with CI flags, no coverage gate (same selection/timeouts as CI; use test-cov for the gate)
	$(PYTEST) $(PYTEST_CI_FLAGS)

test-cov: ## pytest with CI flags + 82% gate (exact CI test-job invocation — ci.yml runs this; issues #1417, #1476)
	$(PYTEST) $(PYTEST_CI_FLAGS) $(PYTEST_COV_FLAGS) -q

test-fast: ## pytest contract only (pre-commit mirror)
	$(PYTEST) -o addopts="" tests/contract -x -q

smoke: ## 3-sample stub-mode local campaign into ./results_smoke (validates the install; no real OpenStudio needed; issue #1479)
	OSIMFLOW_STUB_SIM=1 $(PY) -m osimflow run \
	  --executor local \
	  --input_variables example_package/variables.yml \
	  --template_sim_package ./example_package \
	  --n_samples 3 --outdir ./results_smoke \
	  --openstudio_version 3.11.0 \
	  --log_level WARNING
	@echo "Smoke run complete. Artifacts: ./results_smoke"

byos-generate: ## regenerate the inline BYOS subprocess runner from osimflow.byos_contract
	$(PY) tools/_generate_byos_runner.py

contract: byos-generate agents-contract docs-sync openapi-sync ## run all contract checks (BYOS generator + AGENTS.md + docs/ + openapi)

agents-contract: ## check AGENTS.md / code drift
	$(PY) tools/check_agents_contract.py

docs-sync: ## check docs/ references resolve
	$(PY) tools/check_docs_sync.py

openapi-sync: ## check docs/openapi.json matches the live FastAPI app (issue #1049)
	@$(PY) -c "import fastapi" 2>/dev/null || $(PY) -m pip install -e ".[dev,api]" --quiet
	$(PY) tools/check_openapi_sync.py --summary

precommit: ## pre-commit run --all-files
	$(PRECOMMIT) run --all-files

act: ## local CI mirror (lint + typecheck + test + contract + security)
	act -j lint -j typecheck -j test -j contract -j security

clean: ## remove caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} +
