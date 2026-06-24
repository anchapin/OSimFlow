# tests/ — End-to-end integration tests (placeholder)

> **Status:** Comprehensive E2E tests are a Phase 3 deliverable (PRD §5.2).
> This directory will house the cross-profile test suite that verifies
> `aggregated_results.csv`, `failed_simulations.csv`, per-sample KPI JSONs,
> and plot outputs across `local`, `docker`, `aws_batch`, and `slurm` profiles.

## Planned layout

```
tests/
├── README.md                       (this file)
├── fixtures/
│   └── tiny_template/              # minimal .osm + .osw + measures
├── integration/
│   ├── test_local.nf
│   ├── test_docker.nf
│   ├── test_aws_batch.nf
│   └── test_slurm.nf
├── unit/
│   ├── test_generate_lhs.py
│   ├── test_apply_params_to_model.py
│   ├── test_extract_kpis.py
│   ├── test_aggregate_results.py
│   └── test_generate_plots.py
└── benchmark/
    └── perf_smoke.nf               # PRD §5.2 "Performance Benchmarking" workflow
```

## Conventions

- All E2E tests use `n_samples = 3` against `fixtures/tiny_template/`.
- Assertions are made in Python (pytest) on the resulting `--outdir` contents.
- A passing test must produce **all four** of:
  - `aggregated_results.csv` with the right number of rows.
  - `failed_simulations.csv` (may be empty for a clean fixture).
  - One KPI JSON per sample.
  - At least one PNG plot.
- Per-profile tests are skipped automatically if the executor isn't available
  in the CI runner (e.g., no Slurm on GitHub-hosted runners).
