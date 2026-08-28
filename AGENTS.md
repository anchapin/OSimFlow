# AGENTS.md

> **Audience:** AI coding assistants (Claude Code, Cursor, GitHub Copilot,
> Windsurf, Aider, Cline, etc.) operating in this repository. Read this
> file before proposing or writing code. Sister files
> (`CLAUDE.md`, `.cursorrules`, `.clinerules`) point here; do not duplicate
> rules into them.

---

## Table of Contents

- [0. Precedence and project-type boundaries](#0-precedence-and-project-type-boundaries)
  - [0.1 Precedence](#01-precedence) · [0.2 Project type](#02-project-type--read-this-first) · [0.3 The contract checker](#03-the-contract-checker-hard-gate)
- [1. Stack at a glance](#1-stack-at-a-glance)
- [2. Build & run commands](#2-build--run-commands)
- [3. CLI flags (compact, alphabetical, grouped)](#3-cli-flags-compact-alphabetical-grouped)
- [4. Testing](#4-testing)
- [5. Directory map (contract-checked)](#5-directory-map-contract-checked)
- [6. Code style](#6-code-style)
- [7. Domain glossary](#7-domain-glossary)
- [8. Gotchas (from PRD §6 — verify before changing)](#8-gotchas-from-prd-6--verify-before-changing)
- [9. Task routing hints](#9-task-routing-hints)
- [10. Security & data handling](#10-security--data-handling)
- [11. References](#11-references)

---

## 0. Precedence and project-type boundaries

### 0.1 Precedence

- **This `AGENTS.md` overrides** the generic agent role prompt for
  project-scoped decisions (test commands, file paths, architecture,
  conventions, doc location).
- **The generic role prompt overrides `AGENTS.md`** for cross-project
  decisions (process, tool defaults, security defaults).

### 0.2 Project type — read this first

**OSimFlow is a CLI + library hybrid that wraps the OpenStudio CLI**
(`openstudio.cli run -w workflow.osw`) to run large-scale parametric
building-energy simulation campaigns. It runs locally, on HPC
(Slurm / PBS / Nomad / Kubernetes / Dask-JobQueue), or on cloud
(AWS / Azure / Google Batch / Docker Swarm). **It is not a web
service.** There are no HTTP routes, no ORM models, no authentication
layer. Do not apply a generic Router → Service → Repository → Models
pattern.

Real layered structure:

```
Orchestrator → Executor → Work function
```

- **Orchestrator** — `osimflow/campaign.py` (`Campaign` class) drives
  the 7-step DAG.
- **Executor** — `osimflow/executors/`; 10 concrete executors
  (`LocalExecutor`, `SlurmExecutor`, `AWSBatchExecutor`,
  `NomadExecutor`, `PBSExecutor`, `AzureBatchExecutor`,
  `GoogleBatchExecutor`, `DaskJobQueueExecutor`,
  `KubernetesExecutor`, `DockerSwarmExecutor`) all conform to
  `submit()` → `Handle` from `osimflow/executors/base.py`.
- **Work function** — `osimflow/work.py` (per-step logic) +
  `osimflow/_work_scripts/` (CLI scripts invoked by the work layer).
  `bin/*.py` are thin shims over `_work_scripts/` — keep them thin.

### 0.3 The contract checker (hard gate)

`tools/check_agents_contract.py` is run by pre-commit and CI
(`make contract` / `.github/workflows/agents-contract.yml`). It
verifies that every public symbol in `osimflow/__init__.py`, every
`bin/*.py`, every executor file under `osimflow/executors/`, every
DAG step name, and every `--flag` in `osimflow/__main__.py` is
**mentioned somewhere in this file**. Substring match — so any
mention counts. When you add any of those, update this document in
the same change or `make contract` will fail.

---

## 1. Stack at a glance

| Layer | Tech |
|---|---|
| Orchestration | `osimflow/` package, `osimflow run` CLI |
| Executor | `BaseExecutor` + 10 implementations |
| Slurm | `submitit.AutoExecutor` (real) / `DebugExecutor` (dev) |
| AWS Batch | `boto3`, IAM role auth (no long-lived keys), spot + on-demand |
| Containers | Docker / Singularity; consumes **`nrel/openstudio:<version>`** from Docker Hub (ADR-0002 — the project does not build this image) |
| Sampling | `scipy.stats.qmc` (LHS + plugin algorithms) |
| Data | Python 3.12+, `pandas`, `pyarrow`, `matplotlib`, `seaborn` |
| Cache | `SQLiteCache` (single-node) or `DistributedCache` (Redis, `--redis-url`) |
| Document store | `SQLiteDocumentStore` (single-node) or `RedisDocumentStore` (Redis, `--redis-url` — issue #1014) |
| Observability | per-campaign `run.json` + pluggable backends (`--observability cloudwatch\|prometheus\|opentelemetry`) |
| CI | GitHub Actions: `lint`, `typecheck`, `test`, `contract`, `security` + per-substrate E2E (nightly / `workflow_dispatch`) |

PRD: `docs/OSimFlow.md` (cite by §). Foundation decision:
`.agents/results/decision-verdict.md` and
`.agents/results/architecture/0001-workflow-framework.md`.

---

## 2. Build & run commands

The `Makefile` is the canonical day-to-day interface. **Always run
through `.venv`** — the Makefile hard-codes `.venv/bin/{python,ruff,
mypy,pytest,pre-commit}` and a bare `pytest` resolves to a different
Python that lacks the `[dev,aws,slurm,api]` extras and fails with
`ModuleNotFoundError`.

```bash
make help       # list all targets
make install    # pip install -e ".[dev,aws,slurm,kubernetes,api,sensitivity,optimization,ga]"   (creates .venv)
make lint       # ruff check
make format     # ruff format (write)
make typecheck  # mypy --strict on osimflow/
make test       # pytest (full suite, no coverage gate)
make test-cov   # pytest --cov with 83% gate           (CI default)
make test-fast  # pytest tests/contract -x -q          (pre-commit mirror)
make contract   # regenerate BYOS runner + agents-contract + docs-sync + openapi-sync
make byos-generate  # regenerate osimflow/_byos_runner_generated.py only
make precommit  # pre-commit run --all-files          (pre-push safety net)
make act        # local CI mirror via nektos/act
```

Extras are independent: `[mlflow]`, `[sensitivity]` (SALib),
`[optimization]` (pymoo), `[ga]` (DEAP), `[api]` (FastAPI),
`[kubernetes]`, `[aws]`, `[viz]` (streamlit/plotly), `[tui]` (rich).

### Run a campaign

```bash
# Local smoke run (stub mode — no real OpenStudio needed)
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 --outdir ./results \
  --openstudio_version 3.11.0

# Slurm (real cluster — debug=False)
osimflow run --executor slurm --slurm-real --slurm-partition short \
  --input_variables variables.yml --n_samples 500 \
  --openstudio_version 3.11.0

# AWS Batch — IAM role on the Batch compute env, no long-lived keys.
osimflow run --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --input_variables variables.yml --template_sim_package ./example_package \
  --n_samples 1000 --outdir ./results --archive_intermediates
```

Re-running with the same `--outdir` is a cache hit on every step
(50 s cold → 0.1 s warm — see `decision-verdict.md` §1). Per-step
artifacts land at `${outdir}/{work,sim,kpis,plots,run.json,…}`.

### DAG steps (driven by `osimflow/campaign.py`)

`GENERATE_LHS_SAMPLES`, `PREFLIGHT_RUN_MODEL`, `APPLY_PARAMETERS`,
`RUN_OPENSTUDIO_SIM`, `EXTRACT_KPIS`, `AGGREGATE_RESULTS`,
`GENERATE_BASIC_PLOTS`. Cross-step file deps are declared in
`_STEP_DEPENDENCIES` near the top of `osimflow/campaign.py` — do not
bypass `_verify_step_inputs` when adding a step.

### CLI subcommands

`run` (campaign), `import-osa` / `export` (PAT/OSA I/O), `serve`
(REST, `[api]` extra), `list` / `show` / `compare` / `status` /
`download` (registry), `backup` / `restore` (registry),
`mark-for-reanalysis` / `merge` (data-point lifecycle),
`measure` / `list-measures` (BCL), `aggregate-runs` /
`query-results` / `export-results` (cross-campaign),
`health` (per-executor substrate check; `--executor <name>` promotes
that one to CRITICAL — issue #1024).

---

## 3. CLI flags (compact, alphabetical, grouped)

Contract requires every `--flag` from `osimflow/__main__.py` to
appear in this document. Listed in run-subcommand groups (or
"global" if used outside `run`):

- **Executor + parallelism:** `--algorithm`,
  `--aws-batch-instance-type`, `--aws-batch-job-definition`,
  `--aws-batch-max-retries`, `--aws-batch-max-spot-price-usd`,
  `--aws-batch-on-demand-price`, `--aws-batch-queue`, `--aws-batch-spot-price`,
  `--aws-batch-submit-rps`, `--aws-batch-fallback-to-on-demand`,
  `--azure-batch-account-name`, `--azure-batch-account-url`, `--azure-batch-location`,
  `--azure-batch-account-url`, `--azure-batch-location`,
  `--azure-batch-pool-id`, `--azure-fallback-to-on-demand`,
  `--azure-max-retries`, `--azure-use-spot`, `--coordinator-url`,
  `--container-digest`, `--dask-cluster-type`, `--dask-cpus-per-worker`,
  `--dask-max-workers`, `--dask-memory-per-worker`,
  `--dask-min-workers`, `--dask-project`, `--dask-queue`,
  `--dask-scheduler-address`, `--dask-walltime`, `--detach`,
  `--docker-swarm-image`, `--docker-swarm-max-poll-interval-s`,
  `--docker-swarm-network`, `--docker-swarm-poll-interval-s`,
  `--ecr-repository`, `--enable-cost-tracking`, `--executor`,
  `--export`, `--google-batch-project-id`, `--google-batch-region`,
  `--google-batch-service-account`, `--google-fallback-to-on-demand`,
  `--google-max-retries`, `--google-use-spot`,
  `--kubernetes-backoff-limit`, `--kubernetes-max-poll-interval-s`,
  `--kubernetes-namespace`, `--kubernetes-poll-interval-s`,
  `--kubernetes-queue-name`, `--kubernetes-ttl-seconds-after-finished`,
  `--max-generations`, `--max-sample-retries`, `--max-workers`,
  `--mlflow_tracking_uri`, `--nomad-address`, `--nomad-allocation-resolution-timeout-s`,
  `--nomad-ca-cert`, `--nomad-cert`, `--nomad-datacentre`,
  `--nomad-dispatch-policy`, `--nomad-fanout-submit-chunk-size`,
  `--nomad-fanout-submit-rate-per-sec`, `--nomad-key`,
  `--nomad-max-poll-interval-s`, `--nomad-poll-interval-s`,
  `--nomad-remote-results-only`, `--nomad-tls`, `--nomad-tls-verify`,
  `--nsga2-reference-directions`, `--nsga2-reference-points`,
  `--pbs-queue`, `--pbs-real`, `--pbs-server`, `--preset`,
  `--redis-url`, `--s3-artifact-bucket`, `--s3-artifact-endpoint`,
  `--s3-artifact-prefix`, `--s3-artifact-presigned-url-expiration`,
  `--s3-artifact-region`, `--shard-count`, `--shard-end`,
  `--shard-index`, `--shard-start`, `--slurm-account`,
  `--slurm-constraint`, `--slurm-cost-per-node-hour`,
  `--slurm-gres`, `--slurm-partition`, `--slurm-qos`, `--slurm-real`,
  `--task-queue`, `--uq-failure-threshold`, `--uq-method`,
  `--uq-n-samples`.

- **Inputs / outputs:** `--archive_intermediates`,
  `--bcl-api-key`, `--dry-run`, `--editor`, `--finalize-script`,
  `--init-script`, `--input_variables`, `--kpis`, `--log_level`,
  `--n_samples`, `--no-tui`, `--offline`, `--offline-bundle`,
  `--openstudio_version`, `--outdir`, `--result-storage-backend`,
  `--result-storage-bucket`, `--result-storage-endpoint`,
  `--sample`, `--skip-preflight`, `--template_sim_package`,
  `--track-costs`, `--validate-measures`.

- **BYOS:** `--byos-resource-limits`, `--byos-timeout-s`,
  `--byos-trust-level`,
  `--custom_apply_script`, `--custom_kpi_extractor`,
  `--require-trusted-scripts`.

- **Resilience (chaos fault injection, issue #1013):**
  `--chaos-delay-s`, `--chaos-duration-s`, `--chaos-enabled`,
  `--chaos-fail-after`, `--chaos-intensity` (0.0–1.0),
  `--chaos-jitter-s` (must not exceed `--chaos-delay-s`),
  `--chaos-probability`, `--chaos-scenarios`, `--chaos-schedule`, `--chaos-size-mb`. Off by default; the Campaign wires the
  registered scenarios into ``ChaosEngine`` at the configured
  schedule (``before_step`` / ``after_step`` / ``per_sample``)
  and records every invocation under ``run.json.chaos_invocations``.

- **Observability / cost / alerts:** `--alert-destinations`,
  `--alert-rules`, `--api-keys-file`, `--cloudwatch-log-group`,
  `--cloudwatch-namespace`, `--cost-on-demand-price`,
  `--cost-spot-price`, `--log-aggregation-url`, `--observability`,
  `--observability-flush-interval`,
  `--otel-endpoint`, `--prometheus-port`, `--rate-limit-key`,
  `--resource-quota`, `--webhook-url`.

- **Subcommand flags (non-`run`):** `serve` —
  `--api-key`, `--api-redis-url`, `--cors-origins`, `--dashboard`,
  `--enable-writes`, `--host`, `--port`, `--rate-limit`,
  `--rate-limit-key`, `--read-only`,
  `--read-write`, `--registry`, `--tls-cert`, `--tls-key`, `--ui`;
  `export` — `--algorithm`, `--format`, `--limit`, `--n_samples`,
  `--openstudio_version`, `--outdir`, `--target`;
  `list` — `--format`, `--limit`, `--project`, `--registry`, `--status`;
  `status` / `download` — `--include-intermediates`,
  `--output-dir`; `backup` / `restore` — `--merge`, `--output`,
  `--registry`; `health` — `--json`, `--offline`;
  `mark-for-reanalysis` — `--priority`;
  `merge` — `--source-ids`, `--target-id`, `--target-work-dir`;
  `measure` / `list-measures` — `--filter`, `--project`,
  `--template`, `--variables`;
  `aggregate-runs` / `compare` / `query-results` / `export-results` —
  `--campaign-ids`, `--include-failed`,
  `--no-include-failed`, `--labels`, `--outdirs`, `--page`,
  `--per-page`.

---

## 4. Testing

```bash
make test           # full suite
make test-fast      # contract only, no coverage gate (pre-commit mirror)
.venv/bin/pytest tests/integration/test_cache_invalidation.py -v
.venv/bin/pytest --cov=osimflow
```

CI (`make test-cov`) requires 83% coverage — gated by
`pyproject.toml [tool.pytest.ini_options]`. CI jobs in
`.github/workflows/ci.yml`: `lint` (ruff check + format --check),
`typecheck` (mypy --strict), `test` (pytest + 83%), `contract`,
`security` (pip-audit + gitleaks), `mlflow-real` (real MLflow
smoke), `slow` (-m slow), per-PR Nomad E2E. Per-substrate E2E
(`aws-batch-e2e.yml`, `slurm-e2e.yml`, `kubernetes-e2e.yml`,
`google-batch-e2e.yml`, `azure-batch-e2e.yml`,
`nomad-e2e.yml`, `openstudio-cli-e2e.yml`) are nightly or
`workflow_dispatch`-only; their integration tests are skip-gated in
normal CI. The full real-E2E coverage matrix (gate, trigger, CI
workflow, contract) lives at `docs/substrate-coverage.md`
(issue #1020).

For every new public surface, add a test in `tests/unit/` or
`tests/integration/`. Executor integration tests follow
`test_local_executor.py`, `test_slurm_executor_debug.py`,
`test_aws_batch_executor_stub.py` — 3-sample campaign asserting
the four artifacts (`aggregated_results.csv`,
`failed_simulations.csv`, KPI JSONs, plot files) plus `run.json`.
For real `bin/*.py` work, also add per-step unit tests and
pre-flight parameter-check tests (LHS variable must map to a real
measure argument / `.osm` attribute). Touching the per-sample work
hot path? Add a perf smoke at `tests/benchmarks/bench_campaign.py`
(3-sample cold + warm).

---

## 5. Directory map (contract-checked)

Files listed here are required to appear in `AGENTS.md` by the
contract checker (§0.3). When you add a public file, mention its
name in this section.

### `osimflow/` core

- `osimflow/__init__.py` — public API surface (`__all__`).
- `osimflow/__main__.py` — `argparse` CLI entry point (`osimflow run ...`).
- `osimflow/campaign.py` — `Campaign` orchestrator + `CampaignError` +
  `QuotaExceededError` + the 7-step DAG.
- `osimflow/config.py` — `CampaignConfig` + per-executor config
  dataclasses (`LocalConfig`, `SlurmConfig`, `AWSBatchConfig`,
  `AzureBatchConfig`, `GoogleBatchConfig`, `NomadConfig`,
  `ObservabilityConfig`, `DAGConfig`, `StorageConfig`) +
  `ResourceQuota` + `coerce_variable_type` + `load_config`.
- `osimflow/work.py` — per-step work functions + `BYOS` contract
  (`default_apply_parameters`, `run_openstudio_sim`, `extract_kpis`,
  `aggregate_results`, `generate_plots`,
  `SevereEnergyPlusError`).
- `osimflow/byos_contract.py` — single source of truth for the BYOS
  function-signature contract (`_BYOS_CONTRACT` + `ByosContractEntry`,
  issue #1061).  Both ``osimflow.byos`` and the inline subprocess
  runner read from this module; the inline runner is generated.
- `osimflow/_byos_runner_generated.py` — generated by
  `tools/_generate_byos_runner.py` from
  `osimflow.byos_contract._BYOS_CONTRACT`.  Holds
  `_SUBPROCESS_RUNNER` (the inline script passed to
  `subprocess.Popen(['python', '-c', ...])`) and a snapshot of the
  contract for the subprocess.  Regenerate via `make contract` or
  pre-commit.
- `osimflow/cache.py` — `SQLiteCache` + `CacheKey` + `CacheStats`.
- `osimflow/distributed_cache.py` — `DistributedCache` +
  `build_cache` + `campaign_state_namespace` (Redis-backed;
  pid-private local SQLite files in distributed mode).
- `osimflow/circuit_breaker.py` — `CircuitBreaker` +
  `CircuitOpenError` (closed/open/half-open; guards the Redis
  data plane in `DistributedCache` and `RedisDocumentStore`
  against persistent outages, issue #1111; `_consecutive_failures`
  is reset to 1 on ``half_open`` → ``open`` transition).
- `osimflow/distributed_jobqueue.py` — `DistributedJobQueue` +
  `build_job_queue` (Redis pub/sub wrapper).
- `osimflow/storage.py` — `ResultStorage` ABC + `LocalStorage`,
  `S3Storage`, `GCSStorage`, `AzureBlobStorage`,
  `S3ArtifactStorage`, `ResultStorageUploader`,
  `build_result_storage`.
- `osimflow/taskqueue.py` — `ProducerQueue` ABC (fan-out / push) +
  `ConsumerQueue` ABC (fan-in / pull) + `DaskTaskQueue`
  (implements both), `NoOpTaskQueue` (implements both),
  `TaskHandle`, `TaskQueueStatus`, `build_task_queue`.
- `osimflow/document_store.py` — `DocumentStore` ABC,
  `DocumentStoreError`, `DocumentNotFoundError`,
  `DuplicateDocumentError`, `SQLiteDocumentStore`,
  `RedisDocumentStore`, `build_document_store`
  (issue #1014; dispatch is by `redis_url` / `namespace` mirroring
  `build_cache`).
- `osimflow/jobqueue.py` — filesystem-based `JobQueue`
  (crash recovery).
- `osimflow/monitoring.py` — `RunTrace` (includes
  `chaos_schedule`, `circuit_breaker_states`, `alerts_fired`) +
  `StepTrace`; writes `run.json`.
- `osimflow/observability.py` — `ObservabilityBackend` ABC +
  `NullBackend`, `CloudWatchBackend`, `PrometheusBackend`, `OpenTelemetryBackend`
  + `new_trace_id` + `record_circuit_breaker_event`.
- `osimflow/_campaign_observability.py` — `ObservabilityManager`
  wrapping backend lifecycle from `Campaign`.
- `osimflow/_campaign_cost_tracker.py` — internal cost wiring
  used by `Campaign`.
- `osimflow/logging.py` — `JSONFormatter` + `RotatingFileHandler`
  + `get_logger`, `setup_logging`, `LogAggregator`.
- `osimflow/registry.py` — `CampaignRegistry` + `CampaignRecord`
  (powers `list`/`show`/`compare`/`backup`/`restore`).
- `osimflow/pareto.py` — `ParetoFront` + `ParetoSolution`.
- `osimflow/measures.py` — `MeasureRegistry` + `MeasureArgument` +
  `DiscoveredMeasure` + `MeasureRegistryError` +
  `UnmappedVariableError` + `AmbiguousVariableError`.
- `osimflow/measure_resolver.py`, `osimflow/measure_versioning.py`
  — measure internals.
- `osimflow/weather.py` — `discover_epw_files`, `download_epw`,
  `validate_epw`, `validate_epw_header`,
  `validate_all_epw_files`, `detect_climate_zone_from_stat`,
  `EPWValidationError`, `EPWDownloadError`.
- `osimflow/version_detection.py` — `VersionDetectionError`,
  `detect_openstudio_version`,
  `get_compatible_container_tag`,
  `verify_version_compatibility`.
- `osimflow/health.py` — `osimflow health` subcommand
  (`CheckResult`, `CheckStatus`, `CheckCategory`, `HealthReport`,
  `run_health_checks`). One `_check_<executor>()` per
  `ExecutorRegistry` executor (issue #1024); each returns
  `INFORMATIONAL` by default, promoted to `CRITICAL` when
  `--executor <name>` is passed.
- `osimflow/alerting.py` — `AlertManager`,
  `build_alert_manager`.
- `osimflow/notify.py` — `NotifyBackend` ABC +
  `EmailNotifyBackend`, `NullNotifyBackend`, `SNSNotifyBackend`,
  `WebhookNotifyBackend` + `build_notify_backend`.
- `osimflow/chaos.py` — `ChaosEngine` + `ChaosResult` +
  `FaultType` + `ChaosScenario` + `FaultInjector` ABC + `CPUSpikeInjector`,
  `MemoryPressureInjector`, `NetworkDelayInjector`,
  `KillSwitchInjector`, `run_chaos_scenario`.
- `osimflow/cost_tracking.py` — `CostEstimate`, `CostTracker`,
  `CampaignCostSummary`.
- `osimflow/data_point_manager.py` — `DataPoint`,
  `DataPointManager`, `DataPointStatus`.
- `osimflow/cross_run_aggregator.py` — `CrossRunAggregator`.
- `osimflow/handoff_record.py` — `HandoffRecord` +
  `NoHandoffRecordError` + `IDEMPOTENCY_KEY_HEADER` +
  `HANDOFF_RECORD_NAME` + `read_handoff_record`,
  `write_handoff_record`, `handoff_record_exists` (for
  `--detach` / Coordinator).
- `osimflow/remote_runner.py` — stdlib
  `python -m osimflow.remote_runner` worker for Nomad /
  Kubernetes Jobs (decodes `OSIMFLOW_TASK_PAYLOAD`, pushes
  artifacts to object storage).
- `osimflow/task_payload_hmac.py` — HMAC-SHA256 signing/verification
  for `OSIMFLOW_TASK_PAYLOAD` (issue #1177):
  `sign_task_payload`, `verify_task_payload`,
  `resolve_payload_secret`, `build_signature_env` +
  `TASK_PAYLOAD_ENV` / `TASK_PAYLOAD_SIG_ENV` /
  `TASK_PAYLOAD_SECRET_ENV` constants and the Nomad meta-key
  mirrors. `KubernetesExecutor` / `NomadExecutor` sign at
  submission; `remote_runner` verifies (via
  `hmac.compare_digest`, fail-closed) before decoding. Secret
  comes from the `OSIMFLOW_TASK_PAYLOAD_SECRET` env var (no CLI
  flag — avoids new public surface).
- `osimflow/apply_params.py`, `osimflow/aggregation.py`,
  `osimflow/audit.py`, `osimflow/byos.py`,
  `osimflow/event_log.py`, `osimflow/json_utils.py`,
  `osimflow/manifest.py`, `osimflow/results_db.py`,
  `osimflow/validation.py` (`ValidationError`),
  `osimflow/webhook.py` — internal supporting modules.
- `osimflow/client.py` — typed Python client for the `[api]`
  REST surface.
- `osimflow/mlflow_hook.py` — optional MLflow integration
  (lazy-imports `mlflow`).
- `osimflow/tui.py` — optional `rich`-based terminal UI.
- `osimflow/_eval_safe.py`, `osimflow/_subprocess_utils.py` —
  internal helpers.
- `osimflow/py.typed` — PEP 561 marker.

### `osimflow/_work_scripts/`

CLI scripts invoked by the work layer: `generate_lhs.py`,
`apply_params_to_model.py`, `extract_kpis.py`,
`aggregate_results.py`, `generate_plots.py`,
`excel_to_variables.py`. Mirrored 1:1 by `bin/*.py` shims.

### `osimflow/algorithms/`

- `__init__.py` — `BaseAlgorithm` ABC, `AlgorithmRegistry`,
  `LHSAlgorithm`, `discover_plugins()` (entry point
  `osimflow.algorithms`).
- `sobol.py`, `halton.py` — Sobol / Halton quasi-random
  (`scipy.stats.qmc`).
- `de.py`, `da.py` — `DifferentialEvolutionAlgorithm`,
  `DualAnnealingAlgorithm` (scipy).
- `ga.py` — `GeneticAlgorithm` (DEAP; `[ga]` extra).
- `nsga2.py`, `pso.py`, `spea2.py` — multi-objective
  (pymoo; `[optimization]`).
- `gaisl.py` — island-model parallel GA.
- `rgenoud.py` — hybrid GA + BFGS local search.
- `morris.py`, `fast99.py` — Morris / FAST99 sensitivity
  (SALib; `[sensitivity]`).
- `factorial.py` — `FullFactorialAlgorithm` +
  `GridSamplingAlgorithm`.
- `random_sampling.py` — pure Monte Carlo.
- `sequential_search.py` — deterministic sweep + adaptive
  sampling.
- `calibration.py` — BM25-based utility-bill calibration.
- `custom.py` — `CustomDOEAlgorithm` (CSV or Python callable).
- `qdiscrete.py` — inverse-CDF discrete sampling
  (`DoE.base::qdiscrete`).
- `repeat_all.py` — `RepeatAllAlgorithm`.
- `diag.py` — `DiagAlgorithm` (OAT, mirrors
  openstudio-server's `diag.rb`).
- `doe_analysis.py` — `DOEAnalysis` (main effects,
  interactions, ANOVA).
- `uq.py` — Monte Carlo uncertainty propagation + failure
  probability.

### `osimflow/executors/` (contract-checked)

`__init__.py` defines four executors inline — `LocalExecutor`,
`SlurmExecutor`, `AWSBatchExecutor`, `NomadExecutor` — and hosts
`ExecutorRegistry` (`register_health_check(name, fn)` attaches a
per-executor health check, issue #1024; `iter_health_checks()`
feeds `osimflow.health.run_health_checks`) plus `discover_plugins()`
via entry point `osimflow.executors`. `base.py` defines
`BaseExecutor` + `Handle`; `transport.py` is the
executor-agnostic result-reference contract. The remaining six
executors each have their own file:
`azure_batch_executor.py` (`AzureBatchExecutor`),
`dask_jobqueue_executor.py` (`DaskJobQueueExecutor`; Dask
JobQueue with Slurm/PBS/K8s schedulers — distinct from the
submitit-based `SlurmExecutor`), `docker_swarm_executor.py`
(`DockerSwarmExecutor`), `google_batch_executor.py`
(`GoogleBatchExecutor`), `kubernetes_executor.py`
(`KubernetesExecutor` — each Job runs
`python -m osimflow.remote_runner`;
`OSIMFLOW_TASK_PAYLOAD` carries the step call,
`OSIMFLOW_RESULT_*` carries the transport contract),
`pbs_executor.py` (`PBSExecutor`, submitit).

### `osimflow/api/` (optional, `[api]` extra)

`__init__.py` (`create_app`), `app.py` (FastAPI factory),
`events.py` (SSE + stop endpoints), `auth.py`, `campaigns.py`,
`coordinator.py`, `dashboard.py`, `files.py`, `measures.py`,
`pat_compat.py`, `results_query.py`, `results_viewer.py`,
`schemas.py`, `timeseries.py`, `variable_designer.py`,
`variables.py`, plus `static/` and `templates/` UI assets.

### `osimflow/viz/`

`dashboard.py` — Streamlit dashboard (`[viz]` extra).

### `osimflow/importers/`, `osimflow/exporters/`

`importers/osa.py` (`parse_osa`, `parse_analysis_json`,
`osa_to_variables_yml`); `exporters/osa.py` (`OSAExporter`,
`pack_osa` — PAT `.osa` archive).

### Top-level

- `bin/` — backward-compatible shims over `_work_scripts/`:
  `generate_lhs.py`, `apply_params_to_model.py`,
  `extract_kpis.py`, `aggregate_results.py`, `generate_plots.py`,
  `excel_to_variables.py`. ~25 lines each, re-exporting from
  `_work_scripts/`. Do not add logic here.
- `scripts/` — CLI utilities:
  `fetch_example_fixture.py` (downloads a real `.osm`+`.epw`,
  gitignored), `generate_openapi.py` (regenerates
  `docs/openapi.json`), `bundle_offline.py` (for
  `--offline-bundle`), `migrate_from_mongodb.py`,
   `apply_branch_protection.sh` (post-merge
  settings-as-code for `main`), `setup_nomad_vm.sh`,
   `sweep-stale-branches.sh` (dry-run stale-branch sweep;
   `--apply` deletes proven-merged; `--include-orphaned` opts into
   abandoned branches; `--worktree` scans stale local worktrees;
   see issue #1003).
- `tools/` — repo-internal check scripts:
  `check_agents_contract.py` (§0.3), `check_docs_sync.py`
  (docs path resolution), `_generate_byos_runner.py`
  (regenerates `osimflow/_byos_runner_generated.py` from
  `osimflow/byos_contract.py`; issue #1061).
- `user_scripts/` — user-supplied "Bring Your Own Script"
  overrides (see `user_scripts/README.md`). Loaded via
  `importlib.util` and signature-validated with
  `inspect.signature`.
- `example_package/` — tiny model + variables for local
  smoke tests and executor integration tests.
- `osimflow-deploy/` — cloud-deployment recipes sub-monorepo
  (independent `osimflow-deploy-v` tag prefix, CODEOWNERS
  for IaC). Links to actual IaC in `infra/`; does not
  duplicate it.
- `infra/aws/terraform/` — Terraform module for AWS Batch
  (VPC, S3, IAM, compute env, job queue, job definition
  using `nrel/openstudio`). IAM in `iam.tf`, job
  definition in `job-definition.tf`, ECR + lifecycle in
  `ecr.tf`. CI runs `terraform validate` on `infra/` path
  changes.
- `infra/aws/scripts/sync-openstudio-to-ecr.sh` — ECR mirror
  script (exponential-backoff retry, multi-region).
- `infra/nomad/examples/ha/` — native host-OS Nomad HA
  cluster recipe (3-server Raft, ACL bootstrap, mTLS).
  `infra/nomad/acl/policies/` for agent/worker policies.
  Tokens in `acl/tokens/` are git-ignored.
- `docs/` — `OSimFlow.md` (PRD), `DEVELOPMENT.md` (the
  day-to-day guide — read for depth), `CONTRIBUTING.md`,
  `GOVERNANCE.md`, `api.md`, `branch-protection.md`,
  `benchmarks.md`, `user-guide.md`,
  `substrate-coverage.md` (real-E2E matrix — issue #1020),
  plus per-feature guides (`aws-batch-terraform.md`,
  `nomad-production.md`, `kubernetes-deployment.md`,
  `container-image-strategy.md`, `observability.md`,
  `distributed-cache.md`, `openstudio-image-distribution.md`,
  `measure-runner-guide.md`, `packaging-measures.md`, etc.).
- `.agents/results/` — ADRs and the framework-decision
  verdict.
- `tests/contract/` — contract tests run by pre-commit and
  `make test-fast`.
- `tests/unit/`, `tests/integration/`, `tests/benchmarks/`
  — pytest trees. The real-substrate companion tests in
  `tests/integration/test_real_<substrate>_campaign.py`
  (`test_aws_batch_real.py`, `test_azure_batch_real.py`,
  `test_google_batch_real.py`, `test_kubernetes_executor_real.py`,
  `test_slurm_real_cluster.py`,
  `test_real_openstudio_campaign.py`,
  `test_real_nomad_ha_campaign.py`,
  `test_real_pbs_campaign.py`,
  `test_real_dask_campaign.py`,
  `test_real_docker_swarm_campaign.py`)
  follow the 3-sample mini-campaign pattern and are skip-gated by
  env var (see `docs/substrate-coverage.md` for the full matrix
  and `docs/DEVELOPMENT.md` §4 for the per-test skip-gate knobs).
  Issue #1020.

---

## 6. Code style

- **Python 3.12+**, PEP 8, full type hints on public functions
  (enforced by `mypy --strict` on `osimflow/`).
- `pathlib.Path` over `os.path`. `logging` over `print`.
- Exceptions: catch, log with `exc_info=True`, **re-raise**.
  Never swallow.
- No `from __future__ import annotations` (3.12+ target).
- CLI entry points use `argparse` with subcommands.
- `import openstudio` calls must be isolated behind
  `try/except` with a clear error message (the
  `scientific_python_image` build does not include the heavy
  C++ stack).
- **BYOS contract:** user supplies a Python file with a
  function of the right signature; the Campaign discovers
  and calls it via `inspect.signature`. Never define the same
  contract twice (once as a Python function, once as a CLI
  surface).
- **Cache key rule:** any code that affects per-step behavior
  must be hashed into the cache key. See
  `osimflow/campaign.py:_compute_code_hashes`. Editing
  `osimflow/_work_scripts/*.py` OR `bin/*.py` invalidates the
  affected step automatically via `code_hashes["bin"]` SHA-256
  (union, sorted + deduped — issue #1021). Editing
  `osimflow/work.py` (and modules it imports) is hashed
  separately as `code_hashes["work"]` for `AGGREGATE_RESULTS`.
  Do not bypass this hashing.
- **Executor resource directives:** `cpus`, `memory_mb`,
  `time_min` are advisory on `LocalExecutor`, propagated to
  Slurm via `submitit`'s `update_parameters`, and translated
  to Boto3 `containerOverrides` for `AWSBatchExecutor`. Add
  new resource kinds by extending the `submit()` signature,
  not by adding process-local config.
- **Enforcement:** ruff (lint + format), `mypy --strict`, and
  `make contract`. Run `make precommit` before pushing; CI
  mirrors the same checks. See `docs/DEVELOPMENT.md`.

Shell / CLI: `set -euo pipefail`, long options in docs
(`--openstudio_version` not `-o`). Per-sample stdout/stderr
land at `${outdir}/work/sim/<sample_id>/{stdout,stderr}.log`.

---

## 7. Domain glossary

| Term | Meaning |
|---|---|
| **LHS** | Latin Hypercube Sampling — `scipy.stats.qmc.LatinHypercube` |
| **`.osm`** | OpenStudio Model (the parametric building energy model) |
| **`.osw`** | OpenStudio Workflow — orchestrates which measures run |
| **`.idf`** | EnergyPlus Input Data File — **out of scope** for OSimFlow |
| **`.epw`** | EnergyPlus Weather file — **out of scope** for OSimFlow |
| **`eplusout.sql`** | SQLite output of an EnergyPlus simulation — primary source for KPI extraction |
| **`eplusout.err`** | EnergyPlus error log; `failed_simulations.csv` extracts the *first* "Severe Error" line via `grep -m 1 "  * Severe"` |
| **`eplusout.log`** | EnergyPlus full log (verbose; do not archive unless debugging) |
| **EUI** | Energy Use Intensity (kWh/m²/yr or kBtu/ft²/yr) — the canonical headline KPI |
| **Measure** | OpenStudio plug-in (Ruby or Python) modifying a model or workflow; arguments exposed in `.osw` |
| **`template_sim_package`** | User-supplied dir: base `.osm`/`.osw` + any required measure scripts |
| **`variables.yml`** | User-supplied input: parameters + LHS distributions |
| **BYOS** | "Bring Your Own Script" — user-supplied Python in `user_scripts/` overriding default `bin/` logic |
| **`run.json`** | Per-campaign monitoring trace (per-step timing, per-sample status, cache hit/miss) |
| **`nrel/openstudio:<version>`** | Dynamic container image tag (Docker Hub), selected via `--openstudio_version` |

---

## 8. Gotchas (from PRD §6 — verify before changing)

1. **`eplusout.err` is huge** — delete it from the work dir
   after a successful `handle.result()` in
   `step_run_openstudio_sim`. Don't archive under
   `--archive_intermediates`.
2. **Pre-flight parameter checks** — `step_apply_parameters`
   (via `bin/apply_params_to_model.py`) must verify every LHS
   variable maps to an existing measure argument or `.osm`
   attribute **before** simulation. Fail fast with a clear
   error.
3. **OpenStudio version pinning** — the version lives in the
   **container tag** (`CONTAINER_OS.format(version=...)`)
   passed to the executor, NOT in `variables.yml` or env vars.
   See `docs/openstudio-image-distribution.md` for the
   cache-key shape.
4. **`failed_simulations.csv`** — must contain the *first*
   "Severe Error" line from each `eplusout.err` only.
   `grep -m 1 "  * Severe"`. Implemented in
   `bin/aggregate_results.py`.
5. **`--archive_intermediates`** — publishes campaign inputs
   (`template_sim_package`, `variables.yml`) **and** per-sample
   `.osw`/`.osm` + `eplusout.sql`. Do not blindly archive
   `eplusout.err` / `eplusout.log`.
6. **AWS Batch security** — IAM roles for EC2 instances, not
   long-lived keys. `AWSBatchExecutor` must source credentials
   from the IAM role on the compute env.
7. **OpenStudio Measure deps** — custom Ruby/Python measure
   deps must be packaged *inside* the `template_sim_package`,
   not installed at runtime.
8. **Large time-series** — hourly outputs get huge fast.
   Default to daily/monthly aggregates in
   `aggregated_results.csv`; keep hourly only in per-sample
   `.sql` behind `--archive_intermediates`.
9. **Cache invalidation on per-step code edits** — see §6
   cache key rule. Do not bypass.
10. **`SlurmExecutor` `debug=True` by default** — without
    `--slurm-real`, jobs run locally via
    `submitit.DebugExecutor`. Always pass `--slurm-real` in
    production.
11. **Real vs stub OpenStudio CLI** — `run_openstudio_sim`
    invokes `openstudio.cli run -w workflow.osw` when the CLI
    is on PATH (`shutil.which`). When absent, falls back to
    the stub (sleep + placeholder output). Force stub with
    `OSIMFLOW_STUB_SIM=1`. Real E2E test
    (`tests/integration/test_real_openstudio_campaign.py`) is
    skip-gated on `OSIMFLOW_RUN_REAL_OPENSTUDIO=1`.
12. **Missing `workflow.osw` in real CLI mode** — when the
    CLI is available but no `workflow.osw` exists in the
    `modified_sim_package`, the work function raises
    `RuntimeError` before invoking the CLI. The
    `template_sim_package` must always contain a
    `workflow.osw`.
13. **Distributed cache uses pid-private local SQLite files**
    — since #993 `Campaign` builds its cache via
    `build_cache`: plain `SQLiteCache` for single-node local
    mode; Redis-backed `DistributedCache` (with pid-private
    local SQLite under the hood) when `--redis-url` is set.
    Concurrent processes never lock one database.
14. **Real-substrate E2E coverage matrix (issue #1020)** —
    every executor in `osimflow/executors/` has a
    `tests/integration/test_real_<substrate>_campaign.py`
    companion: Slurm #941, AWS Batch #942, Azure #958, Google
    #959, Kubernetes, Nomad (HA), PBS, Dask-JobQueue,
    Docker Swarm, and OpenStudio CLI #939. The full matrix —
    gate env var, trigger, CI workflow, contract — lives at
    `docs/substrate-coverage.md`. The OpenStudio CLI real-E2E
    (`.github/workflows/openstudio-cli-e2e.yml`) is
    PR-blocking for `release/**` branches (issue #1020
    acceptance criterion) in addition to its nightly /
    `workflow_dispatch` runs. Real-substrate tests follow
    the canonical skip-gate pattern (`pytestmark` skipif on
    the env var) so PR CI is not coupled to live
    infrastructure.
15. **Distributed document store mirrors the cache pattern
    (issue #1014)** — `build_document_store` returns a plain
    `SQLiteDocumentStore` for single-node mode and a Redis-backed
    `RedisDocumentStore` when `--redis-url` is set.  Authoritative
    state lives in Redis (one hash per collection, JSON-encoded
    documents, per-collection auto-increment counter, atomic
    `HSETNX` unique-index enforcement); per-process LRU absorbs
    repeated reads.  The same T8.1 SQLite lock reproducer that
    #993 fixed for the cache is now closed for the document store.
    `RedisDocumentStore` fails loud on Redis outages (raises
    `DocumentStoreError`) instead of silently falling back to
    local-only state — the document store is the source of truth,
    so a Redis outage must not silently diverge workers.

---

## 9. Task routing hints

| If the user asks to… | Edit |
|---|---|
| Add a new KPI | `osimflow/_work_scripts/extract_kpis.py` (or `bin/extract_kpis.py` shim) **and** `osimflow/monitoring.py:StepTrace` schema |
| Add a new sampling algorithm | new module in `osimflow/algorithms/`, subclass `BaseAlgorithm`, register via `AlgorithmRegistry.register` in `osimflow/algorithms/__init__.py`; or declare an entry point under `[project.entry-points."osimflow.algorithms"]` in a third-party `pyproject.toml` (auto-discovered) |
| Add a new execution platform | new file in `osimflow/executors/`, subclass `BaseExecutor` from `base.py`, register via `ExecutorRegistry.register` in `osimflow/executors/__init__.py`, add the choice to `osimflow/__main__.py:_build_executor`; or declare an entry point under `[project.entry-points."osimflow.executors"]` |
| Add a new step to the DAG | new method on `Campaign` in `osimflow/campaign.py`, call it from `Campaign.run`, emit `StepTrace` hooks, declare inputs/outputs in `_STEP_DEPENDENCIES`; update §2 of this file |
| Change a default OpenStudio version | `pyproject.toml` default **and** the `osimflow run --openstudio_version` default in `osimflow/__main__.py` |
| Add a user-facing CLI flag | `osimflow/__main__.py:_build_parser` (`add_argument`) **and** the matching `CampaignConfig` field in `osimflow/config.py` **and** the `load_config` parser |
| Change KPI output schema | `osimflow/_work_scripts/extract_kpis.py` (dict shape) **and** `osimflow/_work_scripts/aggregate_results.py` (column ordering); update §3 / §8 of this file if it affects the contract |
| Fix a bug in parameter application | `osimflow/work.py:default_apply_parameters` first; only touch `osimflow/campaign.py:step_apply_parameters` if you also need different `Campaign` semantics (retry, cache, monitoring) |
| Work with safe expression evaluation | `osimflow/_eval_safe.py` (`safe_eval`, `ExpressionError`); used by chaos engine for variable expansion |
| Add a new cache invalidation rule | `osimflow/campaign.py:_compute_code_hashes` **and** a test in `tests/integration/test_cache_invalidation.py` |
| Add an export format | new module in `osimflow/exporters/`, add the `--target` choice to `osimflow/__main__.py` export subcommand |
| Wire a real OpenStudio CLI invocation | `osimflow/work.py:run_openstudio_sim` — replace the stub body with `subprocess.run(["openstudio.cli", "run", ...])` and add per-sample stdout/stderr capture (the stub is already there for `OSIMFLOW_STUB_SIM=1`) |
| Change AWS Batch infrastructure (VPC, IAM, compute env) | `infra/aws/terraform/`; IAM roles in `iam.tf`, job definition in `job-definition.tf`; `terraform validate` is in CI on `infra/` path changes |
| Add a REST API endpoint | new route in `osimflow/api/app.py` **and** a test in `tests/unit/test_api_core.py`; re-run `python scripts/generate_openapi.py --output docs/openapi.json` afterwards; add a typed method + test in `osimflow/client.py` / `tests/unit/test_client.py` |
| Add or modify a health check | `osimflow/health.py` (`_check_*` function, register in `run_health_checks`) **and** a test in `tests/unit/test_health_check.py` |
| Sweep stale remote branches | `scripts/sweep-stale-branches.sh` (dry-run default; `--apply` deletes proven-merged; `--include-orphaned` also opts into abandoned branches; `--worktree` scans stale local worktrees) + `.github/workflows/branch-cleanup.yml` (nightly dry-run, posts to issue #1003; manual `apply=true`+`confirm=DELETE`); see `docs/branch-protection.md` §"Stale branch sweep" (issue #1003) |

### Tool selection

Tool family priority (when both standard tools and
context-mode / codebase-memory-mcp are exposed):
- Read a small file you intend to edit → `Read`.
- Read / transform a large file without showing full
  contents → `ctx_execute_file`.
- Find a function / class / route by name →
  `codebase-memory-mcp_search_graph`.
- Trace callers / callees (impact analysis) →
  `codebase-memory-mcp_trace_path`.
- Search for a string literal in a known path → `Grep`.
- Run a shell command with large / unpredictable output →
  `ctx_execute`.
- Read documentation from a URL → `ctx_fetch_and_index`.

---

## 10. Security & data handling

- **Never commit** `.osm`, `.osw`, `.idf`, `.epw`,
  `eplusout.*` files. `.gitignore` excludes them; double-check
  before staging. For very large inputs that must be tracked,
  use `git-lfs` — don't bypass the gitignore.
- **AWS:** IAM roles for EC2 compute environments only. No
  long-lived AWS access keys in the repo or in any config
  file. `AWSBatchExecutor` must source credentials from the
  IAM role on the compute env. Terraform
  (`infra/aws/terraform/iam.tf`) provisions least-privilege
  roles: a task role scoped to the campaign S3 bucket and
  CloudWatch Logs, a task-execution role for ECR image pulls,
  and a Batch service role.
- **Singularity on shared HPC:** never bind-mount secrets;
  pass via env vars or `submitit`'s
  `ex.update_parameters(setup=...)`, not as container mounts.
- **BYOS user scripts:** treat user-supplied scripts as
  untrusted. Loaded via `importlib.util` and signature-
  validated with `inspect.signature`. Default trust level is
  `subprocess` (isolated child process);
  `--byos-trust-level inprocess` is the legacy in-process load
  and should be rejected in production via
  `--require-trusted-scripts`. `LocalExecutor` runs in a
  thread pool with no resource limits — when wiring
  `SlurmExecutor` to production, set a per-job timeout
  (`time_min`) to bound blast radius.
- **Nomad ACL tokens** (`infra/nomad/acl/tokens/*.json`) are
  git-ignored. Never commit.
- `gitleaks` runs in pre-commit + CI as a final safety net.

---

## 11. References

- [PRD (docs/OSimFlow.md)](docs/OSimFlow.md) — §1.4 (Key
  Differentiators), §3.1 (In-Scope Features), §4.2 (Key
  Modules/Processes), §5.2 (Phase 3 Deliverables), §6
  (Potential Challenges).
- [Architecture decision
  (`.agents/results/architecture/0001-workflow-framework.md`)](.agents/results/architecture/0001-workflow-framework.md)
  — why a custom Python driver.
- [ADR-0002
  (`.agents/results/architecture/0002-adopt-nrel-upstream-image.md`)](.agents/results/architecture/0002-adopt-nrel-upstream-image.md)
  — adopt `nrel/openstudio` directly.
- [Decision verdict
  (`.agents/results/decision-verdict.md`)](.agents/results/decision-verdict.md)
  — spike outcome that ratified the foundation.
- [Monitoring decision
  (`.agents/results/monitoring-decision.md`)](.agents/results/monitoring-decision.md)
  — BYO monitoring (per-campaign `run.json`).
- [DEVELOPMENT.md](docs/DEVELOPMENT.md) — day-to-day guide
  (architecture, structure, dev env, tests, style, adding
  executors/steps/flags, BYOS, cache, CI, debugging). Read
  this when you want depth.
- [User Guide (docs/user-guide.md)](docs/user-guide.md) —
  install, config, run, interpret results, troubleshoot.
- [Observability guide
  (docs/observability.md)](docs/observability.md) —
  pluggable backends (CloudWatch, Prometheus,
  OpenTelemetry).
- [AWS Batch Terraform guide
  (docs/aws-batch-terraform.md)](docs/aws-batch-terraform.md)
  — zero-to-running AWS Batch deployment (issue #130).
- [Branch protection
  (docs/branch-protection.md)](docs/branch-protection.md) —
  settings-as-code for `main` branch rules (5 required status
  checks, linear history, no reviews/no force-pushes).
  Applied post-merge via
  `scripts/apply_branch_protection.sh`; see issue #975.
- [Nomad production
  (docs/nomad-production.md)](docs/nomad-production.md) —
  HA topology, ACL model, security checklist, TLS notes.
- [CONTRIBUTING.md](docs/CONTRIBUTING.md) — contributor
  onboarding, governance entry point, PR-review checklist.
- [GOVERNANCE.md](docs/GOVERNANCE.md) — community
  governance.
- [`submitit` documentation](https://github.com/facebookincubator/submitit)
  — Slurm executor backend.
- [OpenStudio CLI reference](https://openstudio.net/docs/cli/).