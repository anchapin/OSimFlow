# AGENTS.md

> **Audience:** AI coding assistants (Claude Code, Cursor, GitHub Copilot,
> Windsurf, Aider, Cline, etc.) operating in this repository. Read this
> file before proposing or writing code. Human contributors are welcome
> too — it is the canonical project-orientation document.
>
> Sister files auto-discovered by other tools: `CLAUDE.md` (Claude Code),
> `.cursorrules` (Cursor), `.clinerules` (Cline). They are short pointers
> back to this file; do not duplicate rules into them.

---

## 0. Precedence and project-type boundaries

### 0.1 Precedence

- **Project-specific `AGENTS.md` overrides** the generic agent role prompt
  for project-scoped decisions: test commands, file paths, project
  architecture, project conventions, location of project documentation.
- **The generic role prompt overrides `AGENTS.md`** for cross-project
  decisions: process (where to write result files), tool family defaults,
  and security defaults (e.g. "no `.env*` files committed").

### 0.2 Project type and architecture

**OSimFlow** is a **CLI + library** hybrid that wraps the **OpenStudio
CLI** to run large-scale parametric building-energy simulation campaigns.
Runs locally on a developer machine, on HPC (Slurm / PBS / Nomad /
Kubernetes / Dask-JobQueue), or on cloud (AWS Batch, Azure Batch,
Google Batch, Docker Swarm). **It is not a web service.** There are no
HTTP routes, no ORM models, no authentication layer. Do not apply a
generic Router → Service → Repository → Models pattern.

The actual layered structure is:

```
Orchestrator → Executor → Work function
```

- **Orchestrator** — `osimflow/campaign.py` (`Campaign` class) drives
  the DAG.
- **Executor** — `osimflow/executors/` provides `BaseExecutor` and ten
  concrete executors: `LocalExecutor`, `SlurmExecutor`, `AWSBatchExecutor`,
  `NomadExecutor` (plus `PBSExecutor`, `DaskJobQueueExecutor`,
  `KubernetesExecutor`, `DockerSwarmExecutor`, `AzureBatchExecutor`,
  `GoogleBatchExecutor` in their own files). All conform to
  `submit()` → `Handle` from `osimflow/executors/base.py`.
- **Work function** — `osimflow/work.py` (per-step logic) and
  `osimflow/_work_scripts/` (CLI scripts invoked by the work layer).
  The `bin/*.py` files are stable shims over `_work_scripts/` — keep
  them thin and never reimplement the logic in `bin/`.

### 0.3 What this project does NOT have (do not search for these)

- **No authentication layer** — no user accounts, no passwords, no
  JWT/bcrypt code. The role prompt's auth rule does not apply here.
- **No SQL-injection surface in user-facing code** — OSimFlow uses
  SQLite in exactly one place (`osimflow/cache.py:SQLiteCache`) with
  parameterized queries throughout. Skip a "scan for SQL injection"
  task unless you are touching that one file.
- **No `.env*` / IAM access keys / bind-mounted secrets** —
  credentials come from the IAM role on the compute environment (AWS
  Batch) or per-job env vars (Slurm / Singularity). See §10.

### 0.4 The contract checker

`tools/check_agents_contract.py` is a pre-push and CI gate
(`make contract` / `.github/workflows/ci.yml` `contract` job). It
verifies that every public symbol in `osimflow/__init__.py`, every
`bin/*.py`, every executor file under `osimflow/executors/`, every DAG
step name, and every `--flag` in `osimflow/__main__.py` is mentioned
in this file. When you add any of those, update this document in the
same change or `make contract` will fail.

---

## 1. Project summary

Community-driven open-source Python framework for reproducible
parametric OpenStudio / EnergyPlus simulation campaigns. Targets energy
modelers, researchers, and design-optimization practitioners who need
to launch hundreds to thousands of `openstudio.cli run` invocations
across cloud or HPC without writing bespoke orchestration glue.

- **Foundation decision:** custom Python driver built on `submitit`
  (Slurm), `dask-jobqueue` (alternative HPC), and a thin Boto3-based
  AWS Batch adapter. See `.agents/results/decision-verdict.md` and
  `.agents/results/architecture/0001-workflow-framework.md`.
- **Per-sample work** is heavy (5 min – 4 h) and embarrassingly
  parallel; the Campaign fans out to the configured executor and
  caches every step.
- **Status:** v0.1.0 released 2026-06-24. The orchestration foundation
  and per-step work layer are in; active work is hardening, polish,
  and broader ecosystem coverage.
- **PRD / vision:** [`docs/OSimFlow.md`](docs/OSimFlow.md). Cite by
  section number (e.g. "PRD §4.2").

---

## 2. Stack at a glance

| Layer | Technology |
|---|---|
| Orchestration | Custom Python driver (`osimflow/`) — `osimflow run` CLI |
| Executor abstraction | `BaseExecutor` (10 implementations, see §0.2) |
| Slurm backend | `submitit.AutoExecutor` (real) / `DebugExecutor` (dev) |
| AWS Batch | `boto3`, IAM role auth, spot + on-demand |
| Containers | Docker (local/cloud) and Singularity (HPC); consumes `nrel/openstudio:<version>` from Docker Hub (the project does not build this image — see ADR-0002) |
| Simulation | `openstudio.cli run -w workflow.osw` inside the dynamic container |
| Sampling | `scipy.stats` — LHS plus many plug-in algorithms |
| Data | Python 3.12+, `pandas`, `pyarrow` (Parquet), `matplotlib`, `seaborn` |
| Cache | `SQLiteCache` (single-node) or `DistributedCache` (Redis-backed, `--redis-url`) |
| Monitoring | Per-campaign `run.json` + pluggable observability (`--observability cloudwatch\|prometheus\|opentelemetry`) |
| CI | GitHub Actions, parallel jobs (lint / typecheck / test / contract / security + per-substrate E2E) |

---

## 3. Build & run commands

The `Makefile` is the canonical day-to-day interface. Every CI job has
a `make` equivalent. **Always run through the project `.venv` — the
Makefile hard-codes `.venv/bin/{python,ruff,black,mypy,pytest,
pre-commit}` and a bare `pytest` will resolve to a different Python
that lacks the `[dev,aws,slurm]` extras and fail with
`ModuleNotFoundError`.** Local pre-commit is the day-to-day mirror;
CI is the merge gate.

```bash
make install    # pip install -e ".[dev,aws,slurm]"   (creates .venv)
make lint       # ruff check
make format     # ruff format (write)
make typecheck  # mypy --strict on osimflow/
make test       # pytest (full suite, no coverage gate)
make test-cov   # pytest --cov with 83% gate           (CI default)
make test-fast  # pytest tests/contract -x -q          (pre-commit mirror)
make contract   # tools/check_agents_contract.py + tools/check_docs_sync.py
make precommit  # pre-commit run --all-files          (pre-push safety net)
make act        # local CI mirror via nektos/act
```

Install extras are independent: `[mlflow]`, `[sensitivity]` (SALib),
`[optimization]` (pymoo), `[ga]` (DEAP), `[api]` (FastAPI/uvicorn),
`[kubernetes]`, `[aws]`, `[viz]` (streamlit/plotly), `[tui]` (rich).

### Run a campaign

```bash
# Local smoke run (stub mode, no real OpenStudio needed)
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 \
  --outdir ./results \
  --openstudio_version 3.11.0

# Slurm (real cluster — debug=False)
osimflow run --executor slurm --slurm-real --slurm_partition short \
  --input_variables variables.yml --n_samples 500 \
  --openstudio_version 3.11.0

# AWS Batch — IAM role on the Batch compute env, no long-lived keys.
# Set AWS_REGION; the executor does not pin a region.
osimflow run --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --input_variables variables.yml --template_sim_package ./example_package \
  --n_samples 1000 --outdir ./results --archive_intermediates
```

Re-running with the same `--outdir` is a cache hit on every step
(50 s cold → 0.1 s warm — see `decision-verdict.md` §1). The per-step
artifacts land at `${outdir}/{work,sim,kpis,plots,run.json,...}`.

### DAG step names (referenced from `osimflow/campaign.py`)

The Campaign drives a 7-step DAG; the `step="..."` constants visible to
the contract checker are the hard-coded ones (the seventh,
`GENERATE_BASIC_PLOTS`, plus the dynamic `GENERATE_{algo}_SAMPLES`, are
emitted via the cache code and `StepTrace` rather than the constant
pattern, so they don't appear in the regex scan but are real steps):

1. `GENERATE_LHS_SAMPLES` (dynamic — `GENERATE_{algorithm}_SAMPLES`)
2. `PREFLIGHT_RUN_MODEL` — validates seed model before cloud spend
3. `APPLY_PARAMETERS` — fan-out over N samples
4. `RUN_OPENSTUDIO_SIM` — fan-out (heavy)
5. `EXTRACT_KPIS` — fan-out
6. `AGGREGATE_RESULTS` — one shot
7. `GENERATE_BASIC_PLOTS` — one shot

Cross-step file dependencies are declared in
`_STEP_DEPENDENCIES` near the top of `osimflow/campaign.py`; do not
bypass `_verify_step_inputs` in a new step.

### CLI subcommands

`osimflow` subcommands (see `osimflow/__main__.py`): `run` (campaign
execution), `import-osa` / `export` (PAT/OSA I/O), `serve` (REST API;
requires `[api]` extra), `list` / `show` / `compare` / `status` /
`download` (campaign registry), `backup` / `restore` (registry
backup/restore), `health` (system health checks; per-executor substrate
checks dispatch via `ExecutorRegistry` — pass `--executor <name>` to
promote that one to CRITICAL, issue #1024), `mark-for-reanalysis`
/ `merge` (data-point lifecycle), `measure` / `list-measures` (BCL
browse), `aggregate-runs` / `query-results` / `export-results`
(cross-campaign analysis).

### CLI flag reference (compact, alphabetical)

The `run` subcommand flags (grouped):

- **Execturo + parallelism:** `--executor`, `--max-workers`,
  `--preset`, `--task-queue`, `--dask-cluster-type`,
  `--dask-min-workers`, `--dask-max-workers`, `--dask-cpus-per-worker`,
  `--dask-memory-per-worker`, `--dask-walltime`, `--dask-queue`,
  `--dask-project`, `--dask-scheduler-address`.
- **Slurm:** `--slurm-partition`, `--slurm-account`, `--slurm-real`,
  `--slurm-qos`, `--slurm-constraint`, `--slurm-gres`,
  `--slurm-cost-per-node-hour`.
- **AWS Batch:** `--aws-batch-queue`, `--aws-batch-job-definition`,
  `--aws-batch-max-spot-price-usd`, `--aws-batch-fallback-to-on-demand`,
  `--aws-batch-max-retries`, `--aws-batch-spot-price`,
  `--aws-batch-on-demand-price`, `--aws-batch-instance-type`,
  `--ecr-repository`.
- **Azure Batch:** `--azure-batch-account-name`,
  `--azure-batch-account-url`, `--azure-batch-pool-id`,
  `--azure-batch-location`, `--azure-use-spot`,
  `--azure-fallback-to-on-demand`, `--azure-max-retries`.
- **Google Batch:** `--google-batch-project-id`,
  `--google-batch-region`, `--google-batch-service-account`,
  `--google-use-spot`, `--google-fallback-to-on-demand`,
  `--google-max-retries`.
- **PBS:** `--pbs-queue`, `--pbs-real`, `--pbs-server`.
- **Nomad:** `--nomad-address`, `--nomad-datacentre`,
  `--nomad-remote-results-only`, `--nomad-ca-cert`, `--nomad-cert`,
  `--nomad-key`, `--nomad-tls`, `--nomad-tls-verify`,
  `--nomad-poll-interval-s`, `--nomad-max-poll-interval-s`,
  `--nomad-allocation-resolution-timeout-s`, `--nomad-dispatch-policy`,
  `--nomad-fanout-submit-chunk-size`, `--nomad-fanout-submit-rate-per-sec`.
- **Docker Swarm:** `--docker-swarm-image`, `--docker-swarm-network`,
  `--docker-swarm-poll-interval-s`, `--docker-swarm-max-poll-interval-s`.
- **Kubernetes:** `--kubernetes-namespace`,
  `--kubernetes-poll-interval-s`, `--kubernetes-max-poll-interval-s`,
  `--kubernetes-backoff-limit`, `--kubernetes-ttl-seconds-after-finished`,
  `--kubernetes-queue-name`.
- **Coordinator / distributed:** `--detach`, `--coordinator-url`,
  `--redis-url`, `--shard-count`, `--shard-index`, `--shard-start`,
  `--shard-end`.
- **Inputs / outputs:** `--input_variables`, `--template_sim_package`,
  `--n_samples`, `--outdir`, `--openstudio_version`,
  `--archive_intermediates`, `--max-generations`, `--max-sample-retries`,
  `--skip-preflight`, `--sample`, `--dry-run`, `--no-tui`,
  `--init-script`, `--finalize-script`.
- **Algorithms:** `--algorithm`, `--nsga2-reference-points`,
  `--nsga2-reference-directions`, `--uq-method`, `--uq-n-samples`,
  `--uq-failure-threshold`.
- **BYOS:** `--custom_apply_script`, `--custom_kpi_extractor`,
  `--byos-trust-level`, `--require-trusted-scripts`,
  `--byos-resource-limits`.
- **Observability / MLflow / cost tracking / alerts:**
  `--observability`, `--cloudwatch-log-group`, `--cloudwatch-namespace`,
  `--prometheus-port`, `--otel-endpoint`, `--log-aggregation-url`,
  `--mlflow_tracking_uri`, `--enable-cost-tracking`,
  `--cost-on-demand-price`, `--cost-spot-price`, `--track-costs`,
  `--resource-quota`, `--webhook-url`, `--alert-destinations`,
  `--alert-rules`, `--api-keys-file`, `--rate-limit-key`.
- **Result / artifact storage:** `--result-storage-backend`,
  `--result-storage-bucket`, `--result-storage-endpoint`,
  `--s3-artifact-bucket`, `--s3-artifact-prefix`,
  `--s3-artifact-region`, `--s3-artifact-endpoint`,
  `--s3-artifact-presigned-url-expiration`.
- **Offline / BCL / editor:** `--offline`, `--offline-bundle`,
  `--bcl-api-key`, `--validate-measures`, `--editor`, `--log_level`.

Other subcommand flags: `serve` — `--outdir`, `--host`, `--port`,
`--read-only`, `--read-write`, `--enable-writes`, `--api-key`,
`--cors-origins`, `--rate-limit`, `--api-redis-url`, `--tls-cert`,
`--tls-key`, `--ui`, `--editor`, `--dashboard`. `list` — `--format`,
`--status`, `--limit`, `--registry`. `status` — `<outdir>`. `download`
— `<outdir>`, `--output-dir`, `--include-intermediates`. `backup` —
`--output`, `--registry`. `restore` — `<backup_file>`, `--registry`,
`--merge`. `health` — `--outdir`, `--json`, `--offline`.
`mark-for-reanalysis` — `<outdir>`, `<sample_id>`, `--priority`. `merge`
— `<outdir>`, `--source-ids`, `--target-id`, `--target-work-dir`.
`measure` / `list-measures` — `--template`, `--variables`, `--filter`,
`--project`. `aggregate-runs` / `compare` / `query-results` /
`export-results` — `--outdirs`, `--labels`, `--campaign-ids`,
`--include-failed`, `--no-include-failed`, `--page`, `--per-page`.

---

## 4. Testing

```bash
# Canonical: uses .venv/bin/pytest under the hood
make test           # full suite
make test-fast      # contract + unit, no coverage gate (pre-commit mirror)

# Direct (single file or custom flags) — must use the venv's pytest
.venv/bin/pytest tests/integration/test_cache_invalidation.py -v
.venv/bin/pytest --cov=osimflow
```

If this file says "pytest" without `.venv/bin/`, that is shorthand
for `make test`. Do **not** invoke the system `pytest`; if it resolves
at all on your shell, it will silently run with the wrong interpreter.

CI runs in parallel jobs (`.github/workflows/ci.yml`): `lint`
(`ruff check` + `ruff format --check`), `typecheck` (`mypy --strict
osimflow/`), `test` (pytest with 83% coverage gate), `contract` (the
two `tools/check_*.py` scripts), `security` (`pip-audit`), plus
`mlflow-real` (real MLflow smoke test), `slow` (`-m slow`), and per-PR
Nomad single-node E2E. Per-substrate E2E (`aws-batch-e2e.yml`,
`google-batch-e2e.yml`, `azure-batch-e2e.yml`, `slurm-e2e.yml`,
`kubernetes-e2e.yml`, `openstudio-cli-e2e.yml`) are nightly or
`workflow_dispatch`-only; the unit/integration test files are
skip-gated so they are inert in normal CI.

For every new public surface, add the appropriate test in
`tests/unit/` or `tests/integration/`. Executor integration tests
follow the pattern of `test_local_executor.py`,
`test_slurm_executor_debug.py`, and `test_aws_batch_executor_stub.py`
— 3-sample campaign against the substrate, asserting the four
artifacts (`aggregated_results.csv`, `failed_simulations.csv`, KPI
JSONs, plot files) plus `run.json`.

When implementing real `bin/*.py` logic, also add per-step unit tests
in `tests/unit/`, pre-flight parameter-check tests (LHS variable must
map to a real measure argument / `.osm` attribute), and a
performance-benchmark smoke test (`tests/benchmarks/bench_campaign.py`,
3-sample cold + warm) if it touches the per-sample work hot path.

---

## 5. Directory map

Files in this list are verified by the contract checker (§0.4) — when
you add a public file here, mention its name in this section or
`make contract` fails.

### `osimflow/` core
- `osimflow/__init__.py` — public API surface (`__all__`).
- `osimflow/__main__.py` — `argparse` CLI entry point (`osimflow run ...`).
- `osimflow/campaign.py` — `Campaign` orchestrator + `CampaignError` + `QuotaExceededError` + the 7-step DAG.
- `osimflow/config.py` — `CampaignConfig` + per-executor config dataclasses (`LocalConfig`, `SlurmConfig`, `AWSBatchConfig`, `AzureBatchConfig`, `GoogleBatchConfig`, `NomadConfig`, `ObservabilityConfig`, `DAGConfig`, `StorageConfig`) + `ResourceQuota` + `coerce_variable_type` + `load_config`.
- `osimflow/work.py` — per-step work functions + `BYOS` contract (`default_apply_parameters`, `run_openstudio_sim`, `extract_kpis`, `aggregate_results`, `generate_plots`, plus `SevereEnergyPlusError`).
- `osimflow/cache.py` — `SQLiteCache` + `CacheKey` + `CacheStats`.
- `osimflow/distributed_cache.py` — `DistributedCache` + `build_cache` + `campaign_state_namespace` (Redis-backed for multi-node campaigns; pid-private local SQLite files in distributed mode).
- `osimflow/distributed_jobqueue.py` — `DistributedJobQueue` + `build_job_queue` (Redis pub/sub wrapper).
- `osimflow/storage.py` — `ResultStorage` ABC, `LocalStorage`, `S3Storage`, `GCSStorage`, `AzureBlobStorage`, `S3ArtifactStorage`, `ResultStorageUploader`, `build_result_storage`.
- `osimflow/taskqueue.py` — `TaskQueue` ABC, `DaskTaskQueue`, `NoOpTaskQueue`, `TaskHandle`, `TaskQueueStatus`, `build_task_queue`.
- `osimflow/document_store.py` — `DocumentStore` ABC, `DocumentStoreError`, `DocumentNotFoundError`, `DuplicateDocumentError`, `SQLiteDocumentStore`, `build_document_store`.
- `osimflow/jobqueue.py` — filesystem-based `JobQueue` (crash recovery).
- `osimflow/monitoring.py` — `RunTrace` + `StepTrace`; writes `run.json`.
- `osimflow/observability.py` — `ObservabilityBackend` ABC, `NullBackend`, `CloudWatchBackend`, `PrometheusBackend`, `OpenTelemetryBackend`, `new_trace_id`.
- `osimflow/_campaign_observability.py` — `ObservabilityManager` wrapping all `ObservabilityBackend` lifecycle from `Campaign`.
- `osimflow/_campaign_cost_tracker.py` — internal cost wiring used by `Campaign`.
- `osimflow/logging.py` — `JSONFormatter` + `RotatingFileHandler`; `get_logger`, `setup_logging`, `LogAggregator`.
- `osimflow/registry.py` — `CampaignRegistry` + `CampaignRecord` (SQLite-backed; powers `list`/`show`/`compare`/`backup`/`restore`).
- `osimflow/pareto.py` — `ParetoFront` + `ParetoSolution` (multi-objective tracking).
- `osimflow/measures.py` — `MeasureRegistry` + `MeasureArgument` + `DiscoveredMeasure` + `MeasureRegistryError` + `UnmappedVariableError` + `AmbiguousVariableError`.
- `osimflow/measure_resolver.py`, `osimflow/measure_versioning.py` — measure internals.
- `osimflow/weather.py` — `discover_epw_files`, `download_epw`, `validate_epw`, `validate_epw_header`, `validate_all_epw_files`, `detect_climate_zone_from_stat`, `EPWValidationError`, `EPWDownloadError`.
- `osimflow/version_detection.py` — `VersionDetectionError`, `detect_openstudio_version`, `get_compatible_container_tag`, `verify_version_compatibility`.
- `osimflow/health.py` — `osimflow health` subcommand (`CheckResult`, `CheckStatus`, `CheckCategory`, `HealthReport`, `run_health_checks`). Includes one `_check_<executor>()` function per executor registered in `ExecutorRegistry` (issue #1024): `local`, `slurm`, `pbs`, `aws_batch`, `azure_batch`, `google_batch`, `nomad`, `kubernetes`, `docker_swarm`, `dask_jobqueue`. Each returns `INFORMATIONAL` by default; `run_health_checks` promotes the matching check to `CRITICAL` when `--executor <name>` is passed on the CLI.
- `osimflow/alerting.py` — `AlertManager`, `build_alert_manager`.
- `osimflow/notify.py` — `NotifyBackend` ABC, `EmailNotifyBackend`, `NullNotifyBackend`, `SNSNotifyBackend`, `WebhookNotifyBackend`, `build_notify_backend`.
- `osimflow/chaos.py` — `ChaosEngine`, `ChaosResult`, `ChaosScenario`, `FaultInjector` ABC, `CPUSpikeInjector`, `MemoryPressureInjector`, `NetworkDelayInjector`, `KillSwitchInjector`, `run_chaos_scenario`.
- `osimflow/cost_tracking.py` — `CostEstimate`, `CostTracker`, `CampaignCostSummary`.
- `osimflow/data_point_manager.py` — `DataPoint`, `DataPointManager`, `DataPointStatus` (reanalysis, merging, priority).
- `osimflow/cross_run_aggregator.py` — `CrossRunAggregator`.
- `osimflow/handoff_record.py` — `HandoffRecord`, `NoHandoffRecordError`, `IDEMPOTENCY_KEY_HEADER`, `HANDOFF_RECORD_NAME`, `read_handoff_record`, `write_handoff_record`, `handoff_record_exists` (for `--detach` / Coordinator).
- `osimflow/remote_runner.py` — stdlib `python -m osimflow.remote_runner` worker for Nomad/Kubernetes Jobs (decodes `OSIMFLOW_TASK_PAYLOAD`, pushes result artifacts to object storage).
- `osimflow/apply_params.py`, `osimflow/aggregation.py`, `osimflow/audit.py`, `osimflow/byos.py`, `osimflow/event_log.py`, `osimflow/json_utils.py`, `osimflow/manifest.py`, `osimflow/results_db.py`, `osimflow/validation.py`, `osimflow/webhook.py` — internal supporting modules.
- `osimflow/mlflow_hook.py` — optional MLflow integration (lazy-imports `mlflow`).
- `osimflow/tui.py` — optional `rich`-based terminal UI.
- `osimflow/_eval_safe.py`, `osimflow/_subprocess_utils.py` — internal helpers.
- `osimflow/py.typed` — PEP 561 marker (the package is typed).

### `osimflow/_work_scripts/` (CLI scripts invoked by the work layer)
- `generate_lhs.py`, `apply_params_to_model.py`, `extract_kpis.py`,
  `aggregate_results.py`, `generate_plots.py`, `excel_to_variables.py`.

### `osimflow/algorithms/` (sampling + analysis)
- `__init__.py` — `BaseAlgorithm` ABC, `AlgorithmRegistry`, `LHSAlgorithm`,
  shared helpers, `discover_plugins()` (entry-point `osimflow.algorithms`).
- `sobol.py`, `halton.py` — Sobol / Halton quasi-random (scipy.stats.qmc).
- `de.py`, `da.py` — `DifferentialEvolutionAlgorithm`, `DualAnnealingAlgorithm` (scipy).
- `ga.py` — `GeneticAlgorithm` (DEAP; `[ga]` extra).
- `nsga2.py`, `pso.py` — multi-objective optimizers (pymoo; `[optimization]`).
- `spea2.py` — SPEA2 (pymoo).
- `gaisl.py` — island-model parallel GA.
- `rgenoud.py` — hybrid GA + BFGS local search.
- `morris.py`, `fast99.py` — Morris / FAST99 sensitivity (SALib; `[sensitivity]`).
- `factorial.py` — `FullFactorialAlgorithm` + `GridSamplingAlgorithm`.
- `random_sampling.py` — pure Monte Carlo.
- `sequential_search.py` — deterministic sweep + adaptive sampling.
- `calibration.py` — BM25-based utility-bill calibration.
- `custom.py` — `CustomDOEAlgorithm` (CSV or Python callable).
- `qdiscrete.py` — inverse-CDF discrete sampling (`DoE.base::qdiscrete`).
- `repeat_all.py` — `RepeatAllAlgorithm` (repeats sample set N times).
- `diag.py` — `DiagAlgorithm` (OAT, mirrors openstudio-server's `diag.rb`).
- `doe_analysis.py` — `DOEAnalysis` (main effects, interactions, ANOVA).
- `uq.py` — Monte Carlo uncertainty propagation + failure probability.

### `osimflow/executors/` (`BaseExecutor` + concrete executors)
- `__init__.py` — `LocalExecutor`, `SlurmExecutor`, `AWSBatchExecutor`,
  `NomadExecutor`, `ExecutorRegistry` + `discover_plugins()` (entry-point
  `osimflow.executors`). `ExecutorRegistry.register_health_check(name, fn)`
  attaches an optional per-executor health check (issue #1024);
  `iter_health_checks()` yields `(name, fn)` pairs for the dispatcher in
  `osimflow.health.run_health_checks`.
- `base.py` — `BaseExecutor` + `Handle` interface.
- `transport.py` — executor-agnostic result reference contract.
- `azure_batch_executor.py` — `AzureBatchExecutor`.
- `google_batch_executor.py` — `GoogleBatchExecutor`.
- `dask_jobqueue_executor.py` — `DaskJobQueueExecutor` (Slurm/PBS/K8s).
- `docker_swarm_executor.py` — `DockerSwarmExecutor`.
- `kubernetes_executor.py` — `KubernetesExecutor` (each Job runs
  `python -m osimflow.remote_runner`; `OSIMFLOW_TASK_PAYLOAD` carries
  the step call, `OSIMFLOW_RESULT_*` carries the transport contract).
- `pbs_executor.py` — `PBSExecutor` (submitit).

### `osimflow/api/` (optional, `[api]` extra)
- `__init__.py` — `create_app`.
- `app.py` — FastAPI app factory.
- `events.py` — SSE live events + campaign stop endpoints.
- `auth.py`, `campaigns.py`, `coordinator.py`, `dashboard.py`,
  `files.py`, `measures.py`, `pat_compat.py`, `results_query.py`,
  `results_viewer.py`, `schemas.py`, `timeseries.py`,
  `variable_designer.py`, `variables.py` — REST endpoints +
  Pydantic models.
- `static/`, `templates/` — UI assets.

### `osimflow/viz/` (optional, `[viz]` extra)
- `dashboard.py` — Streamlit dashboard.

### `osimflow/importers/`, `osimflow/exporters/`
- `importers/osa.py` — `parse_osa`, `parse_analysis_json`, `osa_to_variables_yml`.
- `exporters/osa.py` — `OSAExporter`, `pack_osa` (PAT `.osa` archive).

### Top-level
- `bin/` — backward-compatible shim scripts over `_work_scripts/`:
  `generate_lhs.py`, `apply_params_to_model.py`, `extract_kpis.py`,
  `aggregate_results.py`, `generate_plots.py`, `excel_to_variables.py`.
  Each is ~25 lines that re-export from the corresponding
  `_work_scripts/` module. Do not add logic here.
- `scripts/` — CLI utilities: `fetch_example_fixture.py` (downloads a
  real `.osm` + `.epw` for real-OpenStudio E2E; gitignored),
  `generate_openapi.py` (regenerates `docs/openapi.json`),
  `bundle_offline.py` (for `--offline-bundle`), `migrate_from_mongodb.py`,
  `apply_branch_protection.sh` (post-merge settings-as-code for
  `main`), `setup_nomad_vm.sh`.
- `tools/` — repo-internal check scripts:
  `check_agents_contract.py` (the §0.4 contract gate),
  `check_docs_sync.py` (docs/ path resolution gate).
- `user_scripts/` — user-supplied "Bring Your Own Script" overrides;
  see `user_scripts/README.md`. The Campaign loads them via
  `importlib.util` and validates the function signature with
  `inspect.signature`.
- `example_package/` — tiny model + variables for local smoke tests
  and the executor integration tests.
- `osimflow-deploy/` — cloud deployment recipes sub-monorepo
  (independent `osimflow-deploy-v` tag prefix, CODEOWNERS for IaC).
  Links to the actual IaC in `infra/`; does not duplicate it.
- `infra/aws/terraform/` — Terraform module for AWS Batch
  (VPC, S3, IAM, compute env, job queue, job definition using
  `nrel/openstudio`; CI runs `terraform validate` on `infra/` changes).
  IAM roles in `iam.tf`, job definition in `job-definition.tf`, ECR
  repository + lifecycle in `ecr.tf`.
- `infra/aws/scripts/sync-openstudio-to-ecr.sh` — ECR mirror script
  (exponential-backoff retry, multi-region).
- `infra/nomad/examples/ha/` — native host-OS Nomad HA cluster recipe
  (3-server Raft, ACL bootstrap, mTLS). `infra/nomad/acl/policies/`
  for agent/worker policies. Tokens in `acl/tokens/` are gitignored.
- `docs/` — `OSimFlow.md` (PRD), `DEVELOPMENT.md` (the day-to-day
  guide — read this when you want depth), `CONTRIBUTING.md`,
  `GOVERNANCE.md`, `api.md`, `branch-protection.md`, `benchmarks.md`,
  `user-guide.md`, plus per-feature guides (`aws-batch-terraform.md`,
  `nomad-production.md`, `kubernetes-deployment.md`,
  `container-image-strategy.md`, `observability.md`,
  `distributed-cache.md`, etc.).
- `.agents/results/` — ADRs and the framework-decision verdict.
- `tests/contract/` — contract tests run by pre-commit and the
  `make test-fast` job.
- `tests/unit/`, `tests/integration/`, `tests/benchmarks/` — pytest trees.

---

## 6. Code style

- **Python 3.12+**, PEP 8, full type hints on public functions
  (enforced by `mypy --strict` on `osimflow/`).
- `pathlib.Path` over `os.path`. `logging` over `print`.
- Exceptions: catch, log with `exc_info=True`, **re-raise**. Never
  swallow.
- No `from __future__ import annotations` (Python 3.12+ is the target).
- CLI entry points use `argparse` with subcommands.
- `import openstudio` calls must be isolated behind `try/except` with
  a clear error message (the `scientific_python_image` build does
  not include the heavy C++ stack).
- **BYOS contract:** the user supplies a Python file with a function
  of the right signature; the Campaign discovers and calls it via
  `inspect.signature`. Never define the same contract twice (once as
  a Python function, once as a CLI surface).
- **Cache key rule:** any code that affects per-step behavior must
  be hashed into the cache key. See
  `osimflow/campaign.py:_compute_code_hashes` for the pattern.
  Editing `osimflow/_work_scripts/*.py` OR `bin/*.py` invalidates the
  affected step automatically via the `code_hashes["bin"]` SHA-256
  (the implementation unions both directories, sorted+deduped, so dev
  checkouts and wheel installs agree — issue #1021). Do not bypass
  this hashing.
- **Executor resource directives:** `cpus`, `memory_mb`, `time_min`
  are advisory on `LocalExecutor`, propagated to Slurm via
  `submitit`'s `update_parameters`, and translated to Boto3
  `containerOverrides` for `AWSBatchExecutor`. Add new resource
  kinds by extending the `submit()` signature, not by adding
  process-local config.
- **Enforcement:** ruff (lint + format), mypy --strict, and the
  `make contract` checks. Run `make precommit` before pushing; CI
  mirrors the same checks. See `docs/DEVELOPMENT.md`.

Shell / CLI: `set -euo pipefail`, long options in docs
(`--openstudio_version` not `-o`). Per-sample stdout/stderr land at
`${outdir}/work/sim/<sample_id>/{stdout,stderr}.log`.

---

## 7. Domain glossary

| Term | Meaning |
|---|---|
| **LHS** | Latin Hypercube Sampling — `scipy.stats.qmc.LatinHypercube` |
| **`.osm`** | OpenStudio Model file (the parametric building energy model) |
| **`.osw`** | OpenStudio Workflow file — orchestrates which measures run on a model |
| **`.idf`** | EnergyPlus Input Data File — **out of scope** for OSimFlow |
| **`.epw`** | EnergyPlus Weather file — **out of scope** for OSimFlow |
| **`eplusout.sql`** | SQLite output of an EnergyPlus simulation — primary source for KPI extraction |
| **`eplusout.err`** | EnergyPlus error log; `failed_simulations.csv` extracts the *first* "Severe Error" line via `grep -m 1 "  * Severe"` |
| **`eplusout.log`** | EnergyPlus full log (verbose; do not archive unless debugging) |
| **EUI** | Energy Use Intensity (kWh/m²/yr or kBtu/ft²/yr) — the canonical headline KPI |
| **Measure** | OpenStudio plug-in (Ruby or Python) that modifies a model or workflow; arguments are exposed in `.osw` |
| **`template_sim_package`** | User-supplied directory with a base `.osm`/`.osw` + any required measure scripts |
| **`variables.yml`** | User-supplied input declaring which parameters vary and their LHS distributions |
| **BYOS** | "Bring Your Own Script" — user-supplied Python in `user_scripts/` overriding default `bin/` logic |
| **`run.json`** | Per-campaign monitoring trace (per-step timing, per-sample status, cache hit/miss) |
| **`nrel/openstudio:<version>`** | Dynamic container image tag (Docker Hub), selected via `--openstudio_version` |

---

## 8. Common gotchas (from PRD §6)

1. **Large `eplusout.err` files** — delete from the work directory on
   successful simulation (`step_run_openstudio_sim` does this after a
   successful `handle.result()`). Don't archive them blindly under
   `--archive_intermediates` — they get huge.
2. **Pre-flight parameter checks** — `step_apply_parameters` (via
   `bin/apply_params_to_model.py`) must verify that every LHS variable
   actually maps to an existing measure argument or `.osm` attribute
   *before* the simulation runs. Fail fast with a clear error.
3. **OpenStudio version pinning** — the version lives in the
   **container tag** (`CONTAINER_OS.format(version=...)`) passed to
   the executor, not in `variables.yml` or env vars. The
   `nrel/openstudio:<version>` image is dynamically selected in
   `step_run_openstudio_sim` from `--openstudio_version`. See
   `docs/openstudio-image-distribution.md` for the cache-key shape.
4. **Failed simulation summaries** — `failed_simulations.csv` must
   contain the *first* "Severe Error" line from each `eplusout.err`,
   not the whole file. Use `grep -m 1 "  * Severe"`. Implemented in
   `bin/aggregate_results.py`.
5. **`--archive_intermediates`** — when set, publish all campaign
   inputs (`template_sim_package`, `variables.yml`) **and** per-sample
   `.osw`/`.osm` + `eplusout.sql`. Don't blindly archive `eplusout.err`
   / `eplusout.log` — too large. (A future addition to the
   `Campaign` orchestrator; copy a step's `publishDir` pattern.)
6. **AWS Batch security** — IAM roles for EC2 instances, not
   long-lived access keys. `AWSBatchExecutor` must source credentials
   from the IAM role on the compute environment, never from `boto3`
   long-lived keys.
7. **OpenStudio Measure dependencies** — custom Ruby/Python measure
   deps must be packaged *inside* the `template_sim_package`, not
   installed at runtime.
8. **Large time-series data** — hourly outputs for thousands of
   samples get huge fast. Default to daily/monthly aggregates in
   `aggregated_results.csv`; keep hourly data only in per-sample
   `.sql` files behind `--archive_intermediates`.
9. **Cache invalidation on per-step code edits** — the cache key
   includes a SHA-256 of every `osimflow/_work_scripts/*.py` and
   `bin/*.py` file (union, sorted, deduped; issue #1021) via
   `code_hashes["bin"]`, so editing either directory invalidates the
   cache for the affected step. Editing `osimflow/work.py` (and the
   other per-step modules it imports) is hashed separately as
   `code_hashes["work"]` for `AGGREGATE_RESULTS`. Do not introduce a
   step that bypasses this hashing.
10. **SlurmExecutor `debug=True` by default** — without
    `--slurm-real`, jobs run locally via `submitit.DebugExecutor`.
    Always pass `--slurm-real` in production.
11. **Real vs stub OpenStudio CLI** — `run_openstudio_sim` invokes
    `openstudio.cli run -w workflow.osw` when the CLI is on PATH
    (detected via `shutil.which`). When the CLI is not available, it
    falls back to the stub (sleep + placeholder output). Set
    `OSIMFLOW_STUB_SIM=1` to force stub mode even when the CLI is
    installed. The real E2E test
    (`tests/integration/test_real_openstudio_campaign.py`) is
    skip-gated on `OSIMFLOW_RUN_REAL_OPENSTUDIO=1`.
12. **Missing `workflow.osw` in real CLI mode** — when `openstudio.cli`
    is available but no `workflow.osw` exists in the
    `modified_sim_package`, the work function raises `RuntimeError`
    before invoking the CLI. The `template_sim_package` must always
    contain a `workflow.osw`.
13. **Distributed cache uses pid-private local SQLite files** —
    since #993 (T8.2) `Campaign` builds its cache via `build_cache`,
    keeping plain `SQLiteCache` for single-node local mode and a
    Redis-backed `DistributedCache` (with pid-private local SQLite
    under the hood) when `--redis-url` is set. Concurrent processes
    never lock one database.

---

## 9. Task routing hints for AI agents

| If the user asks to… | Edit |
|---|---|
| Add a new KPI | `osimflow/_work_scripts/extract_kpis.py` (or `bin/extract_kpis.py` shim) **and** `osimflow/monitoring.py:StepTrace` schema |
| Add a new sampling algorithm | new module in `osimflow/algorithms/`, subclass `BaseAlgorithm`, register via `AlgorithmRegistry.register` in `osimflow/algorithms/__init__.py`; or declare an entry point under `[project.entry-points."osimflow.algorithms"]` in a third-party `pyproject.toml` (auto-discovered) |
| Add a new execution platform | new file in `osimflow/executors/`, subclass `BaseExecutor` from `base.py`, register via `ExecutorRegistry.register` in `osimflow/executors/__init__.py`, add the choice to `osimflow/__main__.py:_build_executor`; or declare an entry point under `[project.entry-points."osimflow.executors"]` |
| Add a new step to the DAG | new method on `Campaign` in `osimflow/campaign.py`, call it from `Campaign.run`, emit `StepTrace` hooks, declare inputs/outputs in `_STEP_DEPENDENCIES`; update §3 of this file |
| Change a default OpenStudio version | `pyproject.toml` default **and** the `osimflow run --openstudio_version` default in `osimflow/__main__.py` |
| Add a user-facing CLI flag | `osimflow/__main__.py:_build_parser` (`add_argument`) **and** the matching `CampaignConfig` field in `osimflow/config.py` **and** the `load_config` parser |
| Change KPI output schema | `osimflow/_work_scripts/extract_kpis.py` (dict shape) **and** `osimflow/_work_scripts/aggregate_results.py` (column ordering); update §3 / §8 of this file if it affects the contract |
| Fix a bug in parameter application | `osimflow/work.py:default_apply_parameters` first; only touch `osimflow/campaign.py:step_apply_parameters` if you also need different `Campaign` semantics (retry, cache, monitoring) |
| Add a new cache invalidation rule | `osimflow/campaign.py:_compute_code_hashes` **and** a test in `tests/integration/test_cache_invalidation.py` |
| Add an export format | new module in `osimflow/exporters/`, add the `--target` choice to `osimflow/__main__.py` export subcommand |
| Wire a real OpenStudio CLI invocation | `osimflow/work.py:run_openstudio_sim` — replace the stub body with `subprocess.run(["openstudio.cli", "run", ...])` and add per-sample stdout/stderr capture (the stub is already there for `OSIMFLOW_STUB_SIM=1`) |
| Change AWS Batch infrastructure (VPC, IAM, compute env) | `infra/aws/terraform/`; IAM roles in `iam.tf`, job definition in `job-definition.tf`; `terraform validate` is in CI on `infra/` path changes |
| Add a REST API endpoint | new route in `osimflow/api/app.py` **and** a test in `tests/unit/test_api_core.py`; re-run `python scripts/generate_openapi.py --output docs/openapi.json` afterwards; add a typed method + test in `osimflow/client.py` / `tests/unit/test_client.py` |
| Add or modify a health check | `osimflow/health.py` (`_check_*` function, register in `run_health_checks`) **and** a test in `tests/unit/test_health_check.py` |

When the same task can be done several ways and the opencode session
exposes both the standard tool family (Read/Write/Edit/Bash/Grep/Glob)
and the context-mode / codebase-memory-mcp tool families, reach for
the smallest tool that gets the job done:
- Read a small file you intend to edit → `Read`.
- Read / transform a large file without showing full contents →
  `ctx_execute_file`.
- Find a function / class / route definition by name →
  `codebase-memory-mcp_search_graph`.
- Trace callers / callees (impact analysis) →
  `codebase-memory-mcp_trace_path`.
- Search for a string literal in a known path → `Grep`.
- Run a shell command with large / unpredictable output →
  `ctx_execute`.
- Read documentation from a URL → `ctx_fetch_and_index`.

---

## 10. Security & data handling

- **Never commit** `.osm`, `.osw`, `.idf`, `.epw`, `eplusout.*` files.
  `.gitignore` excludes them; double-check before staging. For very
  large inputs that *must* be tracked, use `git-lfs` — don't bypass
  the gitignore.
- **AWS:** IAM roles for EC2 compute environments only. No
  long-lived AWS access keys in the repo or in any config file.
  `AWSBatchExecutor` must source credentials from the IAM role on
  the compute environment. The Terraform module
  (`infra/aws/terraform/iam.tf`) provisions least-privilege roles:
  a task role scoped to the campaign S3 bucket and CloudWatch Logs,
  a task-execution role for ECR image pulls, and a Batch service role.
- **Singularity on shared HPC:** never bind-mount secrets; pass via
  env vars or `submitit`'s `ex.update_parameters(setup=...)`, not as
  container mounts.
- **BYOS user scripts:** treat user-supplied scripts as untrusted.
  The Campaign loads them via `importlib.util` and validates the
  function signature with `inspect.signature`. Default trust level
  is `subprocess` (isolated child process); `--byos-trust-level
  inprocess` is the legacy in-process load and should be rejected in
  production via `--require-trusted-scripts`. The default
  `LocalExecutor` runs in a thread pool with no resource limits —
  when wiring `SlurmExecutor` to production, set a per-job timeout
  (`time_min`) to bound blast radius.
- **Nomad ACL tokens** (`infra/nomad/acl/tokens/*.json`) are
  git-ignored. Never commit.

---

## 11. References

- [PRD (docs/OSimFlow.md)](docs/OSimFlow.md) — sections to cite by
  number: §1.4 (Key Differentiators), §3.1 (In-Scope Features), §4.2
  (Key Modules/Processes), §5.2 (Phase 3 Deliverables), §6
  (Potential Challenges).
- [Architecture decision
  (`.agents/results/architecture/0001-workflow-framework.md`)](.agents/results/architecture/0001-workflow-framework.md)
  — why the project uses a custom Python driver.
- [ADR-0002
  (`.agents/results/architecture/0002-adopt-nrel-upstream-image.md`)](.agents/results/architecture/0002-adopt-nrel-upstream-image.md)
  — the decision to adopt `nrel/openstudio` directly.
- [Decision verdict (`.agents/results/decision-verdict.md`)](.agents/results/decision-verdict.md)
  — the spike's outcome that ratified the foundation.
- [Monitoring decision (`.agents/results/monitoring-decision.md`)](.agents/results/monitoring-decision.md)
  — why OSimFlow ships BYO monitoring (per-campaign `run.json`).
- [DEVELOPMENT.md](docs/DEVELOPMENT.md) — the day-to-day developer
  guide (architecture, project structure, dev env, tests, code
  style, adding executors/steps/flags, BYOS, cache, CI, debugging).
  Read this when you want depth.
- [User Guide (docs/user-guide.md)](docs/user-guide.md) — canonical
  entry point for users (install, config, run, interpret results,
  troubleshoot).
- [Observability guide (docs/observability.md)](docs/observability.md)
  — pluggable backends (CloudWatch, Prometheus, OpenTelemetry).
- [AWS Batch Terraform guide (docs/aws-batch-terraform.md)](docs/aws-batch-terraform.md)
  — zero-to-running AWS Batch deployment (issue #130).
- [Branch protection (docs/branch-protection.md)](docs/branch-protection.md)
  — settings-as-code for the `main` branch rules (5 required status
  checks, linear history, no reviews/no force-pushes). Applied
  post-merge via `scripts/apply_branch_protection.sh`; see issue #975.
- [Nomad production (docs/nomad-production.md)](docs/nomad-production.md)
  — Nomad HA topology, ACL model, security checklist, TLS notes.
- [CONTRIBUTING.md](docs/CONTRIBUTING.md) — contributor onboarding,
  governance entry point, PR-review checklist.
- [GOVERNANCE.md](docs/GOVERNANCE.md) — community governance.
- [`submitit` documentation](https://github.com/facebookincubator/submitit)
  — Slurm executor backend.
- [OpenStudio CLI reference](https://openstudio.net/docs/cli/).
