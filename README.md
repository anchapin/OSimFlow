# OSimFlow

[![ci](https://github.com/anchapin/OSimFlow/actions/workflows/ci.yml/badge.svg)](https://github.com/anchapin/OSimFlow/actions/workflows/ci.yml)
[![bench](https://github.com/anchapin/OSimFlow/actions/workflows/bench.yml/badge.svg)](https://github.com/anchapin/OSimFlow/actions/workflows/bench.yml)
[![codecov](https://codecov.io/gh/anchapin/OSimFlow/graph/badge.svg?token=PLACEHOLDER)](https://codecov.io/gh/anchapin/OSimFlow)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

> **Status:** `v0.1.0` released (`2026-06-24`, see [CHANGELOG.md](CHANGELOG.md)). The orchestration foundation (`osimflow/` package) and per-step work-layer scripts (`osimflow/_work_scripts/`) are implemented; the `bin/*.py` entry points are stable shims over them. Active line: `v0.1.x` hardening + polish + ecosystem coverage.

OSimFlow is a community-driven open-source **Python** framework that wraps the OpenStudio CLI to run large-scale, reproducible, parametric building-energy simulation campaigns. It targets OpenStudio users (energy modelers, researchers, design optimization practitioners) who need to launch hundreds or thousands of `openstudio.cli run` invocations across cloud (AWS Batch) or on-premise HPC (Slurm) without writing bespoke orchestration glue for each campaign.

The framework foundation (a custom Python driver built on `submitit` for Slurm, Boto3 for AWS Batch, and a thin `concurrent.futures.ThreadPoolExecutor` for local development) was selected via an architecture decision spike — see [`.agents/results/decision-verdict.md`](.agents/results/decision-verdict.md) for the evidence.

## Quick links

- [Product Requirements Document](docs/OSimFlow.md)
- [AI assistant guide (AGENTS.md)](AGENTS.md)
- [Architecture decision record](.agents/results/architecture/0001-workflow-framework.md)
- [Decision verdict (spike results)](.agents/results/decision-verdict.md)
- [Monitoring decision (BYO `run.json`)](.agents/results/monitoring-decision.md)
- [User Guide](docs/user-guide.md) — installation, configuration, running campaigns, interpreting results
- [Migration Guide from OpenStudio-Server / PAT](docs/migration-openstudio-server.md) — step-by-step guide for transitioning OSS users
- [Contributing](docs/CONTRIBUTING.md)
- [Governance](docs/GOVERNANCE.md)

## Quick start

### 1. Install

```bash
make install    # creates .venv + pip install -e ".[dev,aws,slurm,kubernetes,api,sensitivity,optimization,ga]"
```

`make install` bootstraps `.venv` and installs the full development
extras set — always invoke tools through `.venv/bin/` so a bare
`pytest` never resolves to the wrong interpreter. Want a smaller
footprint? Inside a virtualenv, `pip install -e ".[api]"` is a minimal
local-executor-only subset, but `make install` is the supported full
dev environment.

### 2. Run a sample campaign

```bash
make smoke   # 3-sample stub-mode local campaign into ./results_smoke
```

`make smoke` runs the end-to-end DAG on the bundled `example_package/`
in stub mode (no real OpenStudio CLI required, no `.venv` activation
needed) and is the recommended way to validate a fresh install — see
[AGENTS.md §2](AGENTS.md) for the full set of `make` targets. If you
have the venv activated and want to run a longer campaign by hand,
`make` just wraps `.venv/bin/osimflow run ...`:

```bash
.venv/bin/osimflow run \
  --executor local \
  --input_variables example_package/variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

- **`variables.yml`** defines the parameters to vary and their probability distributions (uniform, normal, lognormal, etc.) for the Latin Hypercube Sampler.
- **`example_package/`** contains the seed building model (`model.osm`), the OpenStudio workflow (`workflow.osw`), and any required measure scripts.

### 3. Verify your installation

After the command completes, check that these outputs exist under `./results_smoke`:

| Artifact | Description |
|---|---|
| `aggregated_results.csv` | Per-sample KPI summary table |
| `run.json` | Campaign monitoring trace (step timing, sample status, cache hits) |
| `plots/` | Directory containing summary visualizations |

```bash
ls results_smoke/aggregated_results.csv results_smoke/run.json results_smoke/plots/
```

If all three are present, your installation is working correctly. See [AGENTS.md §2](AGENTS.md) for the full set of build/run commands and the [User Guide](docs/user-guide.md) for detailed configuration.

## License

[MIT](LICENSE)
