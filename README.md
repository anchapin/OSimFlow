# OSimFlow

> **Status:** Pre-MVP / skeleton. This repository currently contains the PRD copy, project documentation, and Nextflow/Python stubs for the six processes described in §4.2 of [`docs/OSimFlow.md`](docs/OSimFlow.md). No working pipeline yet — first implementation commit will follow.

OSimFlow is a community-driven open-source Nextflow framework that wraps the OpenStudio CLI to run large-scale, reproducible, parametric building energy simulation campaigns. It targets OpenStudio users (energy modelers, researchers, design optimization practitioners) who need to launch hundreds or thousands of `openstudio.cli run` invocations across cloud (AWS Batch) or on-premise HPC (Slurm) without writing bespoke orchestration glue for each campaign.

See [`docs/OSimFlow.md`](docs/OSimFlow.md) for the full Product Requirements Document.

## Quick links

- [Product Requirements Document](docs/OSimFlow.md)
- [AI assistant guide (AGENTS.md)](AGENTS.md)
- [Contributing](docs/CONTRIBUTING.md)
- [Governance](docs/GOVERNANCE.md)

## License

[MIT](LICENSE)
