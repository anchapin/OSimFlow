# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Conventional Commits](https://www.conventionalcommits.org/).

## [Unreleased]

### Added
- `scripts/fetch_example_fixture.py`: downloads a real OpenStudio `.osm` model + `.epw` weather file into `example_package/` for real-OpenStudio E2E tests; the fetched binary files stay gitignored per the `.osm`/`.epw` policy, and the original placeholder model is preserved as `model.osm.placeholder` for stub-mode tests (fixes #938).

### Tests
- `tests/integration/test_observability_real_sinks.py`: real-sink validation for the CloudWatch, Prometheus, and OpenTelemetry backends (module + per-backend skip-gated; inert in normal CI) (fixes #947).
- `tests/integration/test_real_openstudio_campaign.py`: full-Campaign real-`openstudio.cli` E2E exercising all 7 DAG steps; skip-gated on `OSIMFLOW_RUN_REAL_OPENSTUDIO=1` + `openstudio.cli` on PATH; nightly `openstudio-cli-e2e.yml` now also fetches the real fixture and runs it (fixes #939).
- `tests/integration/test_google_batch_real.py` + `.github/workflows/google-batch-e2e.yml`: real-substrate E2E for `GoogleBatchExecutor` via Google Workload Identity Federation auth (skip-gated; nightly on `workflow_dispatch`) (fixes #959).
- `tests/integration/test_aws_batch_cache_resume.py`: real-AWS-Batch cache-warm/resume E2E asserting the second run is >=5x faster and fully cache-served (skip-gated behind `OSIMFLOW_AWS_BATCH_E2E=1` + result bucket; inert in normal CI) (fixes #960).

## [0.1.0] - 2026-06-24

> First official release. All APIs are subject to change (pre-1.0 development),
> but the orchestration foundation, CLI, executor abstraction, and release
> machinery are stable enough for early adopters to pin and evaluate.

### Features
- Campaign orchestration with 6-step DAG (generate LHS, apply parameters, run simulation, extract KPIs, aggregate results, generate plots)
- LocalExecutor, SlurmExecutor, AWSBatchExecutor, AzureBatchExecutor, GoogleBatchExecutor, DaskJobQueueExecutor, KubernetesExecutor, NomadExecutor, PBSExecutor, DockerSwarmExecutor implementations
- SQLite-based cache for resumable campaigns
- `osimflow run` CLI with full parameter set (executor selection, resource limits, algorithm selection)
- Algorithm plug-in framework with LHS, Sobol, Halton, Morris, FAST99, DifferentialEvolution, DualAnnealing, NSGA-II, PSO, GA, SPEA2, Rgenoud, GAISL, Factorial, Grid, Random, Repeat-All, Sequential Search, Calibration, UQ, Diag, Custom DOE samplers
- PAT-compatible OSA export (`.osa` ZIP archives) and OSA import
- Optional MLflow tracking integration
- Optional Rich TUI for live campaign monitoring
- REST API server with SSE event streaming and campaign stop endpoint
- Pluggable observability backends (CloudWatch, Prometheus, OpenTelemetry)
- BYOS (Bring Your Own Script) override framework for custom apply/kpi scripts
- Pluggable result storage backends (local, S3, GCS, Azure Blob)
- Distributed cache and job-queue coordination via Redis pub/sub
- Campaign registry for multi-campaign management (`osimflow list` / `show` / `compare` / `backup` / `restore`)
- `osimflow health` system health-check subcommand
- Data point lifecycle manager for reanalysis, merging, and priority ordering
- Chaos engineering harness for resilience testing
- Cost tracking for cloud/HPC resource estimation
- Centralized S3 artifact storage with presigned URLs
- Fire-and-forget campaign handoff to Coordinator service (`--detach`)

### Changed
- Nomad compatibility mode deprecation messaging now explicitly documents a one-minor-release migration window for `--no-nomad-remote-results-only`.
- Sigstore signing step switched to pure keyless OIDC (removed stale `SIGSTORE_IDENTITY_TOKEN` secret).
- Release workflow now generates a CycloneDX 1.5 SBOM between build and signing steps.

### Bug Fixes
- `osimflow/campaign.py`: scope the `OSIMFLOW_DOCKER_SWARM_DRY_RUN` env var to the dry-run campaign via save/restore so it no longer leaks across tests or long-lived processes like `osimflow serve` (fixes #976).
- (see git history for pre-0.1.0 fixes)

### Documentation
- Full project documentation in `docs/`
- Architecture Decision Records in `.agents/results/architecture/`
- OpenStudio image distribution guide
- `docs/compatibility-matrix.md` — OpenStudio compatibility matrix (fixes #260)
- `docs/release-process.md` — release management, versioning, and deprecation policy (fixes #260)
- `docs/user-guide.md` — Nomad remote-first behavior and compatibility-toggle migration guidance
- `docs/nomad-production.md` — production-focused Nomad remote-first/deprecation policy update

### Performance
- Cache invalidation on per-step code changes (SHA-256 of `bin/*.py` scripts)

### Tests
- Broadened Nomad remote-first baseline coverage for:
  - deprecation warning guidance in compatibility mode
  - remote-first behavior (local callable bypass in default mode)
  - dispatch metadata propagation for result transport/object-storage settings

### Breaking Changes
- None (0.1.x is pre-1.0 development)
