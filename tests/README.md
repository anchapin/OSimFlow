# tests/ — OSimFlow test suite

OSimFlow uses **pytest** throughout. All tests live under `tests/` and are
organised into four categories.

## Directory layout

```
tests/
├── README.md                  (this file)
├── __init__.py
├── test_preflight.py          # pre-flight validation smoke test
├── unit/                     # unit tests — isolated components
│   ├── fixtures/             # minimal .osm + .osw + variables for unit tests
│   ├── conftest.py
│   └── test_*.py             # one file per module/class being tested
├── integration/              # integration & E2E tests — full campaign runs
│   ├── conftest.py
│   └── test_*.py             # executor, coordinator, API, algorithm tests
├── benchmarks/               # performance smoke tests
│   ├── bench_campaign.py
│   └── test_bench_regression.py
└── contract/                 # contract tests — AGENTS.md, CLI, API surface
    ├── test_api_contract.py
    ├── test_developer_practices.py
    └── test_quickstart.py
```

The `example_package/` directory at the project root (sibling to `tests/`)
holds the canonical tiny template (`.osm`, `.osw`, `variables.yml`) used by
integration and benchmark tests.

## Conventions

- All campaign-level tests use **3 samples** against `example_package/`.
- A passing campaign-integration test must produce all four of:
  - `aggregated_results.csv` with the correct number of rows.
  - `failed_simulations.csv` (may be empty for a clean fixture).
  - One KPI JSON per sample.
  - At least one PNG plot.
- Per-executor integration tests are **skipped automatically** when the
  executor is not available in the CI runner (e.g. no Slurm on GitHub-hosted
  runners). See `docs/substrate-coverage.md` for the full skip-gate matrix.
- Run the full suite with `make test`; fast contract-only with `make test-fast`.

## Adding new tests

| What to test | Where to add it |
|---|---|
| Single function / class | `tests/unit/test_<name>.py` |
| Executor or multi-step workflow | `tests/integration/test_<name>.py` |
| CLI flag or public API contract | `tests/contract/test_<name>.py` |
| Performance regression | `tests/benchmarks/test_<name>.py` |
