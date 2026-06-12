# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Conventional Commits](https://www.conventionalcommits.org/).

## [Unreleased]

### Documentation
- `docs/compatibility-matrix.md` — OpenStudio compatibility matrix (fixes #260)
- `docs/release-process.md` — release management, versioning, and deprecation policy (fixes #260)

### Added
- Ongoing development tracking in `CHANGELOG.md`

## [0.1.0-dev] — development snapshot

> Initial development snapshot. All APIs are subject to change.

### Features
- Campaign orchestration with 6-step DAG (generate LHS, apply parameters, run simulation, extract KPIs, aggregate results, generate plots)
- LocalExecutor, SlurmExecutor, AWSBatchExecutor, NomadExecutor implementations
- SQLite-based cache for resumable campaigns
- `osimflow run` CLI with full parameter set (executor selection, resource limits, algorithm selection)
- Algorithm plug-in framework with LHS, Sobol, Halton, Morris, FAST99, DifferentialEvolution, DualAnnealing, NSGA-II, PSO samplers
- PAT-compatible OSA export (`.osa` ZIP archives) and OSA import
- Optional MLflow tracking integration
- Optional Rich TUI for live campaign monitoring
- REST API server with SSE event streaming and campaign stop endpoint
- Pluggable observability backends (CloudWatch, Prometheus, OpenTelemetry)
- BYOS (Bring Your Own Script) override framework for custom apply/kpi scripts

### Bug Fixes
- (see git history for pre-0.1.0 fixes)

### Documentation
- Full project documentation in `docs/`
- Architecture Decision Records in `.agents/results/architecture/`
- OpenStudio image distribution guide

### Performance
- Cache invalidation on per-step code changes (SHA-256 of `bin/*.py` scripts)

### Breaking Changes
- None (0.1.x is pre-1.0 development)