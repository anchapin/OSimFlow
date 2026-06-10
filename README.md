# OSimFlow

[![ci](https://github.com/anchapin/OSimFlow/actions/workflows/ci.yml/badge.svg)](https://github.com/anchapin/OSimFlow/actions/workflows/ci.yml)
[![bench](https://github.com/anchapin/OSimFlow/actions/workflows/bench.yml/badge.svg)](https://github.com/anchapin/OSimFlow/actions/workflows/bench.yml)
[![codecov](https://codecov.io/gh/anchapin/OSimFlow/graph/badge.svg?token=PLACEHOLDER)](https://codecov.io/gh/anchapin/OSimFlow)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

> **Status:** Pre-MVP / skeleton. The orchestration foundation (`osimflow/` Python package) is now landed. The `bin/*.py` work-layer scripts remain as stubs and will be implemented in the next phase.

OSimFlow is a community-driven open-source **Python** framework that wraps the OpenStudio CLI to run large-scale, reproducible, parametric building-energy simulation campaigns. It targets OpenStudio users (energy modelers, researchers, design optimization practitioners) who need to launch hundreds or thousands of `openstudio.cli run` invocations across cloud (AWS Batch) or on-premise HPC (Slurm) without writing bespoke orchestration glue for each campaign.

The framework foundation (a custom Python driver built on `submitit` for Slurm, Boto3 for AWS Batch, and a thin `concurrent.futures.ThreadPoolExecutor` for local development) was selected via an architecture decision spike — see [`.agents/results/decision-verdict.md`](.agents/results/decision-verdict.md) for the evidence.

## Quick links

- [Product Requirements Document](docs/OSimFlow.md)
- [AI assistant guide (AGENTS.md)](AGENTS.md)
- [Architecture decision record](.agents/results/architecture/0001-workflow-framework.md)
- [Decision verdict (spike results)](.agents/results/decision-verdict.md)
- [Monitoring decision (BYO `run.json`)](.agents/results/monitoring-decision.md)
- [Contributing](docs/CONTRIBUTING.md) — *to be written*
- [Governance](docs/GOVERNANCE.md) — *to be written*

## Quick start

```bash
pip install -e ".[dev,aws,slurm]"

osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 \
  --outdir ./results \
  --openstudio_version 3.4.0
```

See [AGENTS.md §4](AGENTS.md) for the full set of build/run commands.

## License

[MIT](LICENSE)
