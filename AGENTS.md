# AGENTS.md

> **Audience:** AI coding assistants (Claude Code, Cursor, GitHub Copilot, Windsurf, Aider, Claude/Cursor/Cline/etc.) operating in this repository. Read this file before proposing or writing code. Human contributors are also welcome to read it — it's the canonical project-orientation document for both audiences.

---

## 0. Precedence and project-type boundaries

> This section resolves the conflicts between the generic AI agent
> role prompt and this project's `AGENTS.md` (see issue #29). Read
> this section before applying any rule from the role prompt.

### 0.1 Precedence rule

**Project-specific `AGENTS.md` overrides the generic role prompt for
project-scoped decisions**: test commands, file paths, project
architecture, project-specific conventions, and the location of
project documentation.

**The generic role prompt overrides `AGENTS.md` for cross-project
decisions**: process (where to write result files), tool family
defaults, and security defaults (e.g. "no `.env*` files committed").

When the two sources disagree, this precedence rule wins; when they
agree, follow the more specific guidance.

### 0.2 Project type

OSimFlow is a **CLI + library** hybrid. The project wraps the
OpenStudio CLI to run parametric building-energy simulation
campaigns. It runs locally on a developer machine or on HPC/cloud
via per-job IAM roles — it is **not** a web service.

### 0.3 Architecture pattern (this project)

The actual layered structure is:

```
Orchestrator → Executor → Work function
```

- **Orchestrator** — `osimflow/campaign.py` (the `Campaign` class)
  drives the 6-step DAG.
- **Executor** — `osimflow/executors/__init__.py` provides
  `BaseExecutor` with `LocalExecutor`, `SlurmExecutor`, `AWSBatchExecutor`,
  `AzureBatchExecutor`, `GoogleBatchExecutor`, `DaskJobQueueExecutor`, `KubernetesExecutor`, `NomadExecutor`, and `PBSExecutor` implementations.
- **Work function** — `osimflow/work.py` (per-step logic) and
  `bin/*.py` (CLI scripts invoked by the work layer) implement the
  actual step work.

**Do not** apply a generic web-service pattern (Router → Service →
Repository → Models) to this project. There are no HTTP routes, no
DDD Repositories, and no Models in the ORM sense.

### 0.4 What this project does NOT have

Be aware of these absences so you don't waste cycles searching for
code that doesn't exist:

- **No authentication layer.** There are no user accounts, no
  passwords, and no JWT/bcrypt code. The role prompt's auth rule
  (when present) does not apply here.
- **No SQL injection surface in user-facing code.** OSimFlow uses
  SQLite in exactly one place (`osimflow/cache.py:SQLiteCache`) with
  parameterized queries throughout. Do not add a "scan for SQL
  injection" task to your workflow unless you are touching that
  one file.
- **No `.env*` / IAM access keys / bind-mounted secrets** —
  credentials are sourced from the IAM role on the compute
  environment (AWS Batch) or from per-job env vars (Slurm /
  Singularity). See §10.

---

## 1. Project summary

**OSimFlow** is a community-driven open-source **Python** framework that wraps the **OpenStudio CLI** to run large-scale, reproducible, parametric building-energy simulation campaigns. It targets **OpenStudio users** — energy modelers, researchers, and design-optimization practitioners — who need to launch hundreds to thousands of `openstudio.cli run` invocations across cloud (**AWS Batch**) or on-premise HPC (**Slurm**) without writing bespoke orchestration glue for each campaign.

The full vision, scope, and technical architecture are defined in [`docs/OSimFlow.md`](docs/OSimFlow.md) (PRD). This file is the AI-assistant counterpart: it tells you the conventions, the gotchas, and the routing logic so you don't have to re-derive them from the PRD every time.

**Foundation decision:** the project uses a custom Python driver — see `.agents/results/decision-verdict.md` and `.agents/results/architecture/0001-workflow-framework.md` for the rationale. The custom driver is built on `submitit` (Slurm), `dask-jobqueue` (alternative HPC), and a thin Boto3-based AWS Batch adapter. Per-sample work is heavy (5 min – 4 h) and embarrassingly parallel.

**Current status:** **Pre-MVP / skeleton.** The repository contains the PRD, project docs, and Python *stubs* for the six processes from PRD §4.2. The orchestration foundation (the `osimflow/` package) is now landed; the next step is filling in the `bin/*.py` logic that the work layer calls. See the *Next steps* section at the bottom of `decision-verdict.md`.

**MVP target:** PRD §5.2 — multi-environment orchestration, OpenStudio version selection, robustness/refinement. Estimated 3–4 weeks of focused work.

---

## 2. Stack at a glance

| Layer | Technology | Notes |
|---|---|---|
| Workflow orchestration | **Custom Python driver** (`osimflow/`) | ~300 LoC `Campaign` class; subcommand CLI `osimflow run`. |
| Executor abstraction | `BaseExecutor` with `LocalExecutor`, `SlurmExecutor`, `AWSBatchExecutor`, `AzureBatchExecutor`, `GoogleBatchExecutor`, `DaskJobQueueExecutor`, `NomadExecutor`, `PBSExecutor` | All conform to the same `submit()` → `Handle` interface. |
| Slurm backend | **`submitit.AutoExecutor`** | Drop-in `submitit.DebugExecutor` for local dev; real Slurm via `debug=False`. |
| AWS Batch backend | **`boto3`** (future) | Stub today; `AWSBatchExecutor.submit()` is a placeholder. |
| Containerization | **Docker** (local/cloud) and **Singularity** (HPC) | Two images: `nrel/openstudio:<version>` (consumed from Docker Hub — see [`docs/openstudio-image-distribution.md`](docs/openstudio-image-distribution.md)) and `scientific_python_image` (project-owned). |
| Simulation engine | **OpenStudio CLI** + **OpenStudio Python bindings** | Invoked as `openstudio.cli run -w workflow.osw` inside the dynamic container. |
| Statistical sampling | **`scipy.stats`** | Latin Hypercube Sampling (LHS) of design variables. |
| Data processing | **Python 3.12+**, **`pandas`**, **`pyarrow`** (Parquet) | KPI extraction, aggregation, error parsing. |
| Plotting | **`matplotlib`** + **`seaborn`** | 1–3 static summary plots (PNG/PDF). |
| Container registry | **Docker Hub** (OpenStudio) + **`ghcr.io`** (scientific Python) | `docker.io/nrel/openstudio:<version>`, `ghcr.io/anchapin/scientific_python_image:latest`. |
| Monitoring | **BYO: per-campaign `run.json` + tqdm** + pluggable observability backends | See `.agents/results/monitoring-decision.md` and [`docs/observability.md`](docs/observability.md). Optional MLflow add-on via `--mlflow_tracking_uri` (see `osimflow/mlflow_hook.py`). Observability backends (CloudWatch, Prometheus, OpenTelemetry) via `--observability` flag (issue #145, #127). |
| CI/CD | **GitHub Actions** | Terraform validate in CI for `infra/` changes (issue #148). |

---

## 3. Directory map

| Path | Purpose |
|---|---|
| `osimflow/__init__.py` | Public API: `Campaign`, `SQLiteCache`, `DistributedCache`, `build_cache`, `DistributedJobQueue`, `build_job_queue`, `CampaignConfig`, `coerce_variable_type`, `CampaignRegistry`, `CampaignRecord`, `SevereEnergyPlusError`, `CacheStats`, `QuotaExceededError`, `ResourceQuota`, `DiscoveredMeasure`, `MeasureRegistryError`, `UnmappedVariableError`, `AmbiguousVariableError`, executors, the algorithm plug-in framework (`BaseAlgorithm`, `LHSAlgorithm`, `AlgorithmRegistry`, `DOEAnalysis`), the result storage backend (`ResultStorage`, `LocalStorage`, `S3Storage`, `GCSStorage`, `AzureBlobStorage`, `ResultStorageUploader`, `build_result_storage`), the document store backend (`DocumentStore`, `DocumentStoreError`, `DocumentNotFoundError`, `DuplicateDocumentError`, `SQLiteDocumentStore`, `build_document_store`), plus the weather helpers (`discover_epw_files`, `download_epw`, `validate_epw`, `validate_epw_header`, `validate_all_epw_files`), the alerting helpers (`AlertManager`, `build_alert_manager`), the cost tracking helpers (`CostEstimate`, `CostTracker`, `CampaignCostSummary`), the data point lifecycle helpers (`DataPoint`, `DataPointManager`, `DataPointStatus` for issues #418/#419/#420), the version detection helpers (`VersionDetectionError`, `detect_openstudio_version`, `get_compatible_container_tag`, `verify_version_compatibility`), and logging setup (`get_logger`, `setup_logging`). |
| `osimflow/campaign.py` | The orchestrator class. ~300 LoC. Owns the 6-step DAG. |
| `osimflow/cache.py` | `SQLiteCache` + `CacheKey` + `CacheStats` — explicit, testable resume semantics and hit rate statistics (issue #426). |
| `osimflow/data_point_manager.py` | `DataPoint` + `DataPointManager` + `DataPointStatus` — JSON-persisted data point lifecycle manager for reanalysis, merging, and priority ordering (issues #418, #419, #420). |
| `osimflow/document_store.py` | `DocumentStore` ABC + `SQLiteDocumentStore` — MongoDB-equivalent document store using SQLite JSON1; provides `insert_one`, `find_one`, `find_many`, `update_one`, `delete_one`, `create_index`, and `aggregate` for campaign data persistence (issue #389). |
| `osimflow/distributed_cache.py` | `DistributedCache` + `build_cache` — Redis pub/sub wrapper for cross-node cache invalidation in multi-node campaigns (issue #330). |
| `osimflow/distributed_jobqueue.py` | `DistributedJobQueue` + `build_job_queue` — Redis pub/sub wrapper for cross-node job queue coordination in multi-node campaigns (issue #393). |
| `osimflow/config.py` | `CampaignConfig` dataclass + `load_config()` + `coerce_variable_type` + `ResourceQuota` (issue #409 type auto-coercion). |
| `osimflow/storage.py` | `ResultStorage` ABC, `LocalStorage` (no-op), `S3Storage` (boto3), `GCSStorage` (google-cloud-storage), `AzureBlobStorage` (azure-storage-blob async), `ResultStorageUploader` (sync wrapper), and `build_result_storage` factory (issue #339). |
| `osimflow/monitoring.py` | `RunTrace` + `StepTrace` + `SampleTrace`; writes `run.json`. |
| `osimflow/logging.py` | Structured JSON logging with `JSONFormatter` + `RotatingFileHandler` (issue #258). Exports `get_logger`, `setup_logging`, and `LogAggregator`. |
| `osimflow/observability.py` | `ObservabilityBackend` ABC + `NullBackend` + `CloudWatchBackend` + `PrometheusBackend` + `OpenTelemetryBackend` + `new_trace_id` (per-sample trace-ID helper); plug-in metrics backends (issue #145, #127). |
| `osimflow/pareto.py` | `ParetoFront` + `ParetoSolution` — non-dominated solution tracking for multi-objective algorithms (issue #141). Persists per-generation JSON to `outdir/pareto/gen_N.json`. |
| `osimflow/registry.py` | `CampaignRegistry` + `CampaignRecord` — SQLite-backed campaign registry for multi-campaign management (issue #266). Supports `osimflow list`, `osimflow show`, `osimflow compare`, `osimflow backup`, and `osimflow restore` subcommands. Registry backup/export/import methods: `export_registry()`, `import_registry()`, `backup()` (issue #440). |
 | `osimflow/weather.py` | `.epw` file discovery, download, and header validation (issue #63): `discover_epw_files`, `download_epw`, `validate_epw`, `validate_epw_header`, `validate_all_epw_files`, `detect_climate_zone_from_stat`, plus `EPWValidationError` / `EPWDownloadError` (issue #424). |
| `osimflow/health.py` | CLI health check module (issue #411): `CheckResult`, `CheckStatus`, `CheckCategory`, `HealthReport`, `run_health_checks`, `format_results`, `to_json`, `get_exit_code`. Powers the `osimflow health` subcommand — verifies Python version, core packages, SQLite, write permissions, disk space, external tools (OpenStudio/Docker/Podman), optional packages, and network connectivity. |
| `osimflow/api/__init__.py` | REST API public surface: `create_app`. Optional `[api]` extra (issue #138). |
| `osimflow/api/app.py` | FastAPI application factory with `/health`, `/ready`, `/api/v1/campaign`, `/api/v1/steps` endpoints (issue #138, G23a). |
| `osimflow/api/events.py` | SSE live events and campaign stop endpoints (issue #143): `GET /api/v1/events` (Server-Sent Events stream watching `run.json`), `POST /api/v1/campaign/stop` (writes `.stop` flag to halt a running campaign). |
| `osimflow/mlflow_hook.py` | Optional MLflow integration (issue #7). Lazy-imports `mlflow`; the Campaign calls these helpers when `--mlflow_tracking_uri` is set. |
| `osimflow/alerting.py` | `AlertManager` + `build_alert_manager` — Alert routing for campaign events (Slack, PagerDuty, email). |
| `osimflow/cost_tracking.py` | `CostEstimate` + `CostTracker` + `CampaignCostSummary` — Cloud/HPC resource cost estimation and tracking (issue #447). |
| `osimflow/version_detection.py` | `VersionDetectionError`, `detect_openstudio_version`, `get_compatible_container_tag`, `verify_version_compatibility` — OpenStudio version detection and container tag resolution. |
| `osimflow/algorithms/__init__.py` | Algorithm plug-in framework (issue #121): `BaseAlgorithm` ABC, `AlgorithmRegistry` singleton, built-in `LHSAlgorithm`, plus shared helpers (`_sample_with_engine`, `_apply_distribution`). Subclass and register to add new sampling strategies. `AlgorithmRegistry.discover_plugins()` auto-discovers third-party algorithms via `entry_points` group `osimflow.algorithms` (issue #432). |
| `osimflow/algorithms/sobol.py` | `SobolAlgorithm` — Sobol quasi-random sequence sampler using `scipy.stats.qmc.Sobol` (issue #139). |
| `osimflow/algorithms/halton.py` | `HaltonAlgorithm` — Halton quasi-random sequence sampler using `scipy.stats.qmc.Halton` (issue #139). |
| `osimflow/algorithms/de.py` | `DifferentialEvolutionAlgorithm` — Differential evolution optimizer using `scipy.optimize.differential_evolution` (issue #125). Iterative. |
| `osimflow/algorithms/da.py` | `DualAnnealingAlgorithm` — Dual annealing optimizer using `scipy.optimize.dual_annealing` (issue #125). Iterative. |
| `osimflow/algorithms/ga.py` | `GeneticAlgorithm` — canonical Genetic Algorithm using DEAP with tournament selection, SBX crossover, and polynomial mutation (issue #345). Iterative. Optional `[ga]` extra. |
| `osimflow/algorithms/nsga2.py` | `NSGA2Algorithm` — NSGA-II multi-objective optimizer using `pymoo` (issue #140). Iterative. Optional `[optimization]` extra. |
| `osimflow/algorithms/pso.py` | `PSOAlgorithm` — Particle Swarm Optimization using a custom velocity-update loop (issue #140). Iterative. Optional `[optimization]` extra. |
| `osimflow/algorithms/morris.py` | `MorrisAlgorithm` — Morris method sensitivity analysis sampler using SALib (issue #136). Optional `[sensitivity]` extra. |
| `osimflow/algorithms/fast99.py` | `FAST99Algorithm` — Fourier Amplitude Sensitivity Test (FAST99) sampler using SALib (issue #136). Optional `[sensitivity]` extra. |
| `osimflow/algorithms/doe_analysis.py` | `DOEAnalysis` — Design of Experiments analysis: main effects, interaction effects, factor sensitivity/Pareto ranking, and ANOVA-based variance decomposition (issue #405). |
| `osimflow/executors/__init__.py` | `BaseExecutor` + `LocalExecutor` + `SlurmExecutor` + `AWSBatchExecutor` + `AzureBatchExecutor` + `GoogleBatchExecutor` + `DaskJobQueueExecutor` + `KubernetesExecutor` + `NomadExecutor` + `PBSExecutor`. Also includes `ExecutorRegistry` singleton with `discover_plugins()` for third-party executor auto-discovery via `entry_points` group `osimflow.executors` (issue #432). |
| `osimflow/executors/dask_jobqueue_executor.py` | `DaskJobQueueExecutor` — elastic HPC executor using `dask-jobqueue` with auto-scaling across Slurm/PBS/Kubernetes backends (issue #338). |
| `osimflow/executors/base.py` | `BaseExecutor` — abstract base for all executors; defines the `submit()` → `Handle` interface and shared resource-directive handling. |
| `osimflow/executors/azure_batch_executor.py` | `AzureBatchExecutor` — Azure Batch executor using the Azure SDK. |
| `osimflow/executors/google_batch_executor.py` | `GoogleBatchExecutor` — Google Cloud Batch executor using the Google Cloud SDK. |
| `osimflow/executors/kubernetes_executor.py` | `KubernetesExecutor` — Kubernetes executor using the Kubernetes Python client; maps resource directives to K8s requests/limits (issue #254). |
| `osimflow/executors/pbs_executor.py` | `PBSExecutor` — PBS/Torque executor using submitit (issue #351). |
| `osimflow/executors/kubernetes_executor.py` | `KubernetesExecutor` — Kubernetes-native executor using the official Kubernetes Python client (issue #377). |
| `osimflow/measures.py` | `MeasureRegistry`, `Measure`, `MeasureArgument`, `MeasureRegistryError`, `UnmappedVariableError`, `AmbiguousVariableError` — measure discovery, argument introspection, and variable validation for parametric campaigns (issue #532). |
| `osimflow/jobqueue.py` | `JobQueue` — filesystem-based job queue for crash recovery (issue #263). Manages job lifecycle (pending → in_progress → completed/failed) with atomic JSON file moves. |
| `osimflow/taskqueue.py` | Distributed task queue abstraction (issue #335): `TaskQueue` ABC, `DaskTaskQueue` (Dask-based), `NoOpTaskQueue` (passthrough), `TaskHandle`, `TaskQueueStatus`, and `build_task_queue` factory. |
| `osimflow/importers/__init__.py` | OSA import support: `parse_osa`, `parse_analysis_json`, `osa_to_variables_yml`. |
| `osimflow/importers/osa.py` | OSA analysis.json parser and variables.yml converter (issue #104). Reverse of `exporters/osa.py`. |
| `osimflow/exporters/__init__.py` | Export campaign state to various formats. |
| `osimflow/exporters/osa.py` | `OSAExporter` — export campaign config to PAT-compatible analysis.json (issue #142) and ``.osa`` ZIP archives (issue #134). ``pack_osa()`` bundles analysis.json + seed model + measures + weather into a portable ``.osa`` file. Reverse of `importers/osa.py`. |
| `osimflow/work.py` | Per-step work functions: `default_apply_parameters`, `run_openstudio_sim`, `extract_kpis`, `aggregate_results`, `generate_plots`. The BYOS contract lives here. |
| `osimflow/client.py` | Typed async Python client for the REST API (issue #433): `OSimFlowClient` (httpx-based), Pydantic response models (`HealthResponse`, `CampaignResponse`, `StepsResponse`, `SamplesResponse`, etc.), and typed exception hierarchy (`AuthenticationError`, `NotFoundError`, `RateLimitError`, `ServerError`). Optional `[api]` extra (httpx). |
| `osimflow/__main__.py` | CLI entry point (`osimflow run ...`). |
| `scripts/generate_openapi.py` | Export the OpenAPI spec from the FastAPI app to `docs/openapi.json` (issue #433). Run: `python scripts/generate_openapi.py --output docs/openapi.json`. |
| `docs/openapi.json` | Auto-generated OpenAPI 3.1 spec for the OSimFlow REST API (issue #433). Regenerate after adding/modifying API endpoints. Consumable by code generators (openapi-generator, etc.). |
| `bin/generate_lhs.py` | LHS sampler (scipy.stats). |
| `bin/apply_params_to_model.py` | Default parameter-application logic. |
| `bin/extract_kpis.py` | Default KPI extractor. |
| `bin/aggregate_results.py` | Result aggregation + error-summary extraction. |
| `bin/generate_plots.py` | Matplotlib/seaborn plot generator. |
| `bin/excel_to_variables.py` | PAT/Analysis Gem Excel spreadsheet to ``variables.yml`` converter. Reads a PAT-style ``.xlsx`` and produces a OSimFlow ``variables.yml`` with support for uniform, normal, lognormal, triangular, discrete, categorical, and static distributions. |
| `osimflow/tui.py` | Optional `rich`-based terminal UI for live campaign tracking (issue #197). Auto-detected when `rich` is installed and stdout is a TTY. Optional `[tui]` extra. |
| `tests/integration/test_cache_invalidation.py` | Cache invalidation test suite (8 cases). |
| `tests/benchmarks/bench_campaign.py` | Performance benchmark script (issue #10). Runs cold + warm 3-sample campaign, writes `benchmarks.json`. |
| `tests/benchmarks/test_bench_regression.py` | Pytest assertions for the bench artifact shape + threshold gate. |
| `user_scripts/` | User-provided "Bring Your Own Script" (BYOS) overrides. See `user_scripts/README.md`. |
| `docs/OSimFlow.md` | The PRD — the source of truth for scope and architecture. |
| `docs/benchmarks.md` | How to interpret the `benchmarks.json` artifact (issue #10). |
| `docs/CONTRIBUTING.md` | Contributor onboarding (stub for Phase 3). |
| `docs/GOVERNANCE.md` | Community governance model (stub for Phase 3). |
| `.agents/results/` | Architecture decision records (ADRs) and the framework-decision verdict. |
| `osimflow-deploy/` | Cloud deployment recipes sub-monorepo (issue #164). Contains platform-specific README guides (AWS, Nomad, Docker), an independent CHANGELOG (`osimflow-deploy-v` tag prefix), and a CODEOWNERS file for IaC review. Links back to the actual IaC in `infra/`. Does **not** duplicate or move `infra/` files. |
| `infra/aws/terraform/` | Terraform module for AWS Batch infrastructure (issue #148): VPC, S3 bucket, IAM roles, Batch compute environment, job queue, and job definition using `nrel/openstudio` container image. CI runs `terraform validate` on `infra/` changes. |
| `infra/aws/terraform/iam.tf` | Least-privilege IAM roles (issue #130): ECS instance profile, task role (S3 + CWL), task-execution role (ECR pull), Batch service role. |
| `infra/aws/terraform/job-definition.tf` | Batch job definition (issue #130): parameterised vCPU, memory, timeout, execution role. |
| `infra/aws/terraform/examples/basic/` | Minimal on-demand example config (documentation only). |
| `infra/aws/terraform/examples/spot/` | Spot instance example config with cost tags (documentation only). |
| `infra/aws/scripts/sync-openstudio-to-ecr.sh` | ECR mirror script (issue #129): pulls `nrel/openstudio` from Docker Hub with exponential-backoff retry, pushes to ECR in one or more regions. Avoids Docker Hub rate limits for production Batch jobs. |
| `infra/aws/terraform/ecr.tf` | ECR repository + lifecycle policy for mirrored OpenStudio images (issue #129). Keeps last 5 tagged `3.*` images. |
| `docs/container-image-strategy.md` | Container image strategy: why we mirror to ECR, how to use the sync script, lifecycle policy, multi-region replication, and cost considerations (issue #129). |
| `docs/aws-batch-terraform.md` | Zero-to-running deployment guide for AWS Batch with Terraform (issue #130). |
| `docs/api.md` | REST API reference: endpoints, SSE event stream, read-only vs read-write modes, and authentication notes (issue #143). |
| `docs/observability.md` | Pluggable observability backends (CloudWatch, Prometheus, OpenTelemetry): configuration, usage, and extension guide (issue #145, #127). |
| `infra/nomad/examples/ha/` | Docker Compose HA cluster for Nomad (3 server + 2 client) with ACL bootstrap (issue #123). |
| `infra/nomad/examples/ha/docker-compose.yml` | 3-server + 2-client Docker Compose with named volumes and bridge networking. |
| `infra/nomad/examples/ha/server*.hcl` | Per-server HCL configs with `bootstrap_expect=3` and `retry_join`. |
| `infra/nomad/examples/ha/client.hcl` | Client HCL config with Docker task driver and server join. |
| `infra/nomad/examples/ha/bootstrap.sh` | ACL bootstrap script: generates management + worker tokens, registers policies. |
| `infra/nomad/acl/policies/agent.hcl` | Read-only agent/node ACL policy for operators. |
| `infra/nomad/acl/policies/worker.hcl` | Least-privilege job submission ACL policy for the NomadExecutor. |
| `infra/nomad/acl/tokens/` | Generated ACL tokens (git-ignored). Never commit. |
| `docs/nomad-production.md` | Nomad production deployment guide: HA topology, ACL model, security checklist, TLS notes (issue #123). |
| `.gitignore` | Standard Python ignores + `.osm/.osw/.idf/.epw/eplusout.*` (never commit) + Nomad token secrets. |
| `LICENSE` | MIT. |
| `README.md` | One-paragraph project pitch + status. |

---

## 4. Build & run commands

> All commands assume CWD = repo root. The orchestration foundation runs end-to-end against stub `bin/*.py` scripts (no real OpenStudio CLI needed for the MVP smoke test); see `tests/integration/test_cache_invalidation.py` for the cache-correctness gate.

### DAG step names (referenced from `osimflow/campaign.py`)

The 7-step DAG that the `Campaign` class drives:

- `GENERATE_LHS_SAMPLES` — single-shot, no fan-out.
- `PREFLIGHT_RUN_MODEL` — single-shot, validates seed model before cloud spend (issue #107).
- `APPLY_PARAMETERS` — fan-out over N samples.
- `RUN_OPENSTUDIO_SIM` — fan-out over N samples (heavy).
- `EXTRACT_KPIS` — fan-out over N samples.
- `AGGREGATE_RESULTS` — one shot after all KPIs.
- `GENERATE_BASIC_PLOTS` — one shot after aggregation.

### CLI flags (referenced from `osimflow/__main__.py`)

- `--executor` (local / slurm / aws_batch / azure_batch / google_batch / dask_jobqueue / nomad)
- `--preset` (named preset of recommended flags; reduces 50+ CLI surface for common use cases. Individual flags override preset values. Issue #384)
- `--max-workers` (local executor parallelism)
- `--slurm-partition`, `--slurm-account`, `--slurm-real`
- `--slurm-qos`, `--slurm-constraint`, `--slurm-gres` (advanced; submitit >= 1.5 only)
- `--slurm-cost-per-node-hour` (Slurm cost in USD per node-hour for cost tracking; issue #447)
- `--aws-batch-queue`, `--aws-batch-job-definition`
- `--aws-batch-max-spot-price-usd` (Spot price ceiling in USD/vCPU-hour. Issue #131)
- `--aws-batch-fallback-to-on-demand` (fall back to on-demand when Spot exceeds ceiling or retries exhausted. Issue #131)
- `--aws-batch-max-retries` (max Spot interruption retries; default 3. Issue #131)
- `--aws-batch-spot-price` (AWS Batch Spot price in USD per vCPU-hour for cost tracking; issue #447)
- `--aws-batch-on-demand-price` (AWS Batch on-demand price in USD per vCPU-hour for cost tracking; issue #447)
- `--azure-batch-account-name`, `--azure-batch-account-url`, `--azure-batch-pool-id`, `--azure-batch-location` (Azure Batch executor configuration)
- `--azure-use-spot`, `--azure-fallback-to-on-demand`, `--azure-max-retries` (Azure spot/preemptible instance handling; issue #352)
- `--google-batch-project-id`, `--google-batch-region`, `--google-batch-service-account` (Google Cloud Batch executor configuration)
- `--google-use-spot`, `--google-fallback-to-on-demand`, `--google-max-retries` (Google preemptible VM handling; issue #352)
- `--pbs-queue`, `--pbs-real`, `--pbs-server` (PBS/Torque executor configuration; issue #351)
- `--dask-cluster-type`, `--dask-min-workers`, `--dask-max-workers`, `--dask-cpus-per-worker`, `--dask-memory-per-worker`, `--dask-walltime`, `--dask-queue`, `--dask-project` (Dask-JobQueue elastic HPC executor configuration; issue #338)
- `--task-queue` (distributed task queue backend: `none` (default) or `dask`; issue #335)
- `--dask-scheduler-address` (Dask scheduler address for task queue; issue #335)
- `--ecr-repository` (ECR repository URI for OpenStudio images; overrides Docker Hub. Issue #144)
- `--offline` (skip Docker Hub pulls, PyPI version checks, and online weather downloads; issue #261)
- `--offline-bundle` (path to offline bundle directory created by `scripts/bundle_offline.py`; issue #261)
- `--nomad-address`, `--nomad-datacentre`
- `--nomad-ca-cert`, `--nomad-cert`, `--nomad-key`, `--nomad-tls`, `--nomad-tls-verify` (Nomad TLS configuration; issue #344)
- `--kubernetes-namespace`, `--kubernetes-poll-interval-s`, `--kubernetes-max-poll-interval-s` (Kubernetes executor configuration)
- `--input_variables`, `--template_sim_package`, `--n_samples`, `--outdir`
- `--algorithm` (sampling strategy selector; dispatches through `AlgorithmRegistry`. Default: `lhs`. Issue #121)
- `--openstudio_version`, `--archive_intermediates`
- `--init-script`, `--finalize-script` (pre/post campaign shell hooks. Issue #108)
- `--custom_apply_script`, `--custom_kpi_extractor` (BYOS)
- `--byos-trust-level` (BYOS script execution mode: `subprocess` (default, isolated child process) or `inprocess` (legacy, loads into orchestrator). Issue #269)
- `--byos-resource-limits` (CPU/memory limits for BYOS subprocess wrapper; issue #343)
- `--api-keys-file` (path to JSON file for multi-user API key authentication; issue #395)
- `--rate-limit-key` (rate limit key type: `ip` (default), `user`, or `campaign`; issue #445)
- `--mlflow_tracking_uri` (optional; logs params/metrics/artifacts to MLflow. Requires `pip install osimflow[mlflow]`)
- `--observability` (observability backend selector: `none` / `cloudwatch` / `prometheus` / `opentelemetry`. Default: `none`. Issue #145, #127)
- `--cloudwatch-log-group` (CloudWatch log group name; used when `--observability cloudwatch`)
- `--cloudwatch-namespace` (CloudWatch metric namespace; used when `--observability cloudwatch`)
- `--prometheus-port` (Prometheus metrics HTTP port; used when `--observability prometheus`)
- `--otel-endpoint` (OpenTelemetry OTLP endpoint URL; used when `--observability opentelemetry`)
- `--log-aggregation-url` (CloudWatch Logs aggregation URL for distributed log collection; issue #340)
- `--no-tui` (disable rich terminal UI even when `rich` is installed; issue #197)
- `--dry-run` (dry-run mode: force LocalExecutor, 1 sample, steps 1-4 only)
- `--sample` (single-sample mode: re-run sample N from existing samples.json)
- `--skip-preflight` (skip the PREFLIGHT_RUN_MODEL step that validates the seed model; issue #107)
- `--max-generations` (maximum number of DAG generations; default 1 for single-shot LHS. Issue #122)
- `--max-sample-retries` (maximum retry attempts for transient per-sample failures; default 3. Issue #252)
- `--webhook-url` (campaign completion webhook callback URL; issue #283)
- `--result-storage-backend` (result storage backend: `local` (default), `s3`, `gs`, `azure`; issue #339)
- `--result-storage-bucket` (bucket/container name for result storage; issue #339)
- `--result-storage-endpoint` (custom S3-compatible endpoint URL for result storage; issue #339)
- `--log_level`
- `--alert-destinations` (alert receiver endpoints for campaign events)
- `--alert-rules` (alert routing rules for campaign events)
- `--enable-cost-tracking` (enable cloud/HPC resource cost estimation; issue #447)
- `--cost-on-demand-price` (on-demand price per vCPU-hour for cost estimation; issue #447)
- `--cost-spot-price` (Spot price per vCPU-hour for cost estimation; issue #447)
- `--track-costs` (enable campaign cost tracking; issue #447)
- `--aws-batch-spot-price` (AWS Batch Spot price in USD per vCPU-hour for cost tracking; issue #447)
- `--aws-batch-on-demand-price` (AWS Batch on-demand price in USD per vCPU-hour for cost tracking; issue #447)
- `--slurm-cost-per-node-hour` (Slurm cost in USD per node-hour for cost tracking; issue #447)
- `--resource-quota` (resource quota limits for campaign execution)

**Backup subcommand flags** (issue #440):
- `backup` — `--output` (custom backup file path), `--registry` (registry DB path), `--log_level`
- `restore` — `<backup_file>` (positional), `--registry` (registry DB path), `--merge` (merge instead of replace), `--log_level`

**Subcommands:** `run` (campaign execution), `import-osa` (OSA import), `export` (PAT export), `serve` (REST API server; issue #138), `list` (campaign registry listing), `show` (single campaign details), `compare` (side-by-side comparison), `status` (campaign run.json status), `download` (download campaign results), `backup` (registry backup; issue #440), `restore` (registry restore/import; issue #440), `health` (system health checks; issue #411), `mark-for-reanalysis` (mark a completed/failed sample for re-running; issue #420), `merge` (merge multiple data points into a single target; issue #418). The `serve` subcommand accepts `--outdir`, `--host`, `--port`, `--read-only`, `--read-write`, `--enable-writes`, `--api-key`, `--cors-origins`, `--rate-limit`, `--tls-cert`, `--tls-key`, `--ui`, and `--dashboard` flags. Requires `pip install osimflow[api]`. The `list` subcommand accepts `--format` (table/json), `--status`, `--limit`, and `--registry`. The `status` subcommand accepts `<outdir>`. The `download` subcommand accepts `<outdir>`, `--output-dir`, and `--include-intermediates`. The `backup` subcommand accepts `--output` (custom backup path) and `--registry`; it creates a timestamped SQLite backup using the online backup API. The `restore` subcommand accepts `<backup_file>`, `--registry`, and `--merge` (merge vs. replace mode). The `health` subcommand accepts `--outdir` (directory to check write permissions/disk space; default: cwd), `--json` (machine-readable JSON output), and `--offline` (skip network connectivity check). The `mark-for-reanalysis` subcommand accepts `<outdir>`, `<sample_id>` (must be COMPLETED or FAILED), and `--priority` (default 0). The `merge` subcommand accepts `<outdir>`, `--source-ids` (one or more source sample IDs), `--target-id` (target sample ID), and `--target-work-dir` (path to target work directory). Exit code 0 if all critical checks pass, 1 otherwise.

### Developer workflow targets (Makefile)

The `Makefile` is the canonical day-to-day interface. Every CI job has a
`make` equivalent:

```bash
make install    # pip install -e ".[dev,aws,slurm]"
make lint       # ruff check
make format     # ruff format + black
make typecheck  # mypy --strict (osimflow/)
make test       # pytest (full suite)
make test-fast  # pytest unit + contract only (pre-commit mirror)
make contract   # tools/check_agents_contract.py + tools/check_docs_sync.py
make precommit  # pre-commit run --all-files (the pre-push safety net)
make act        # local CI mirror via nektos/act
```

See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for the day-to-day workflow.

### Install

```bash
# Editable install with dev + aws + slurm extras
pip install -e ".[dev,aws,slurm]"

# Minimal install (no slurm/boto3 — local executor only)
pip install -e .

# Optional MLflow add-on (issue #7). Brings in the `mlflow` package;
# only needed if you pass `--mlflow_tracking_uri` on the CLI.
pip install -e ".[mlflow]"

# Optional sensitivity analysis add-on (issue #136). Brings in SALib;
# required for Morris and FAST99 sampling algorithms.
pip install -e ".[sensitivity]"

# Optional multi-objective optimization add-on (issue #140). Brings in
# pymoo; required for NSGA-II and PSO sampling algorithms.
pip install -e ".[optimization]"

# Optional REST API add-on (issue #138). Brings in FastAPI + uvicorn;
# required for the `osimflow serve` subcommand.
pip install -e ".[api]"
```

### Run a campaign

```bash
# Local smoke run: 5 samples, local executor
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 \
  --outdir ./results \
  --openstudio_version 3.11.0

# HPC run via Slurm — pined OpenStudio version, real Slurm (not debug)
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm_partition short \
  --openstudio_version 3.11.0 \
  --input_variables variables.yml \
  --n_samples 500

# HPC run with advanced Slurm directives (GPU jobs, QoS, etc.)
# Requires submitit >= 1.5.
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm_partition gpu \
  --slurm_qos high \
  --slurm_constraint gpu \
  --slurm_gres gpu:1 \
  --openstudio_version 3.11.0 \
  --input_variables variables.yml \
  --n_samples 200

# Cloud run on AWS Batch (issue #5).
#
# Prerequisites:
#   * `pip install osimflow[aws]` (brings in boto3).
#   * A registered Batch job definition whose container image matches
#     `openstudio_cli_image:<openstudio_version>` (the dynamic tag the
#     campaign passes via the `container` kwarg; see PRD §1.4). The
#     executor forwards the tag in the `OSIMFLOW_CONTAINER` env var on
#     every task so the work script can read it.
#   * AWS credentials via the IAM role attached to the Batch compute
#     environment (PRD §6 *Cloud Security Practices*). Long-lived
#     `aws_access_key_id` / `aws_secret_access_key` are intentionally
#     not accepted by the executor.
#   * `AWS_REGION` set in the environment (or `~/.aws/config` /
#     `AWS_DEFAULT_REGION`). The executor does NOT pin a region.
#
# Polling: the executor polls `batch.describe_jobs` with exponential
# backoff (start 5s, cap 60s) until the task is SUCCEEDED. FAILED
# tasks re-raise a `RuntimeError` whose message carries the Batch
# `statusReason`, so the Campaign's `except Exception` branch logs it.
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --openstudio_version 3.11.0 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 1000 \
  --outdir ./results \
  --archive_intermediates

# User-provided custom KPI extractor
osimflow run \
  --executor local \
  --custom_kpi_extractor user_scripts/my_kpis.py \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./results

# MLflow tracking (optional add-on). Requires `pip install osimflow[mlflow]`.
osimflow run \
  --executor local \
  --mlflow_tracking_uri http://localhost:5000 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 \
  --outdir ./results

# REST API server (optional add-on, issue #138). Requires `pip install osimflow[api]`.
osimflow serve \
  --outdir ./results \
  --host 0.0.0.0 \
  --port 8000

# Read-write mode — enables campaign start/stop and SSE event streaming (issue #143)
osimflow serve \
  --outdir ./results \
  --host 0.0.0.0 \
  --port 8000 \
  --read-write
```

The campaign writes `${outdir}/run.json` with per-step timing and per-sample status; this is the primary monitoring artifact (see `.agents/results/monitoring-decision.md`).

### Resume a partial run

Re-running with the same `--outdir` is a cache hit on every step. The first run takes 50s; the second run takes 0.1s (verified — see `decision-verdict.md` §1).

---

## 5. Testing

Tests live under `tests/`. **Always run them through the project
`.venv` — the Makefile hard-codes the venv paths so a bare
`pytest` will resolve to a Python interpreter that does not have
`submitit`, `boto3`, or `types-PyYAML` installed and the suite will
fail with `ModuleNotFoundError`.** The supported entry points are:

```bash
# Canonical: uses .venv/bin/pytest under the hood
make test           # full suite
make test-fast      # contract + unit, no coverage gate
make test-cov       # full suite + 85% coverage gate

# Direct (when you need a single file or a custom flag) — must use
# the venv's pytest, not whatever `pytest` is first on $PATH:
.venv/bin/pytest tests/integration/test_cache_invalidation.py -v
.venv/bin/pytest --cov=osimflow
```

If a test command in this document says "pytest" without a leading
`.venv/bin/`, that is shorthand for the canonical `make test` form.
Do **not** invoke the system `pytest`; if it resolves at all on
your shell, it will silently run with the wrong interpreter.

When implementing the real `bin/*.py` logic, add:
- **End-to-end smoke test** with 1-3 samples and a tiny template package, verifying the four output artifacts (`aggregated_results.csv`, `failed_simulations.csv`, KPI JSONs, plot files).
- **Per-step unit tests** for each `bin/*.py` script.
- **Pre-flight parameter check tests** — the LHS variable name must map to a real measure argument / `.osm` attribute.
- **Performance Benchmarking** smoke test (PRD §5.2) that records wall-clock + memory for a 3-sample run.

### Executor integration tests (issue #11)

The four files in `tests/integration/` named after the executor profiles are
end-to-end smoke tests of the Campaign running against each substrate
(PRD §5.2 *Comprehensive end-to-end integration tests*). They run a
3-sample campaign through the `example_package/` and assert all four
output artifacts plus the per-campaign `run.json` are produced:

- `tests/integration/test_local_executor.py` — `LocalExecutor` happy path.
- `tests/integration/test_slurm_executor_debug.py` — `SlurmExecutor(debug=True)` (uses `submitit.DebugExecutor`; no real cluster needed in CI).
- `tests/integration/test_aws_batch_executor_stub.py` — `AWSBatchExecutor` with a mocked `boto3` client.
- `tests/integration/test_aws_batch_real.py` — Real AWS Batch E2E test (issue #146). Skipped unless `OSIMFLOW_AWS_BATCH_E2E=1`. Runs via the nightly `aws-batch-e2e` workflow against real Batch infrastructure with OIDC auth.
- `tests/integration/test_cache_resume.py` — runs the same campaign twice against the same `outdir`; the warm run must be at least 5x faster than the cold run (the issue quotes ~280x for 5 samples on the spike).
- `tests/integration/test_osa_round_trip.py` — OSA round-trip integration test (issue #134). Verifies that `OSAExporter.pack_osa()` produces a valid `.osa` ZIP and that export → pack → unpack → import preserves algorithm type, variable names, distributions, measure arguments, and template package files.
- `tests/integration/test_api_events.py` — SSE events and campaign stop endpoint tests (issue #143). Validates SSE stream, `.stop` flag file behaviour, and read-only vs read-write mode enforcement.

The full executor suite runs in well under 60s on a single core. The CI
workflow runs them on every PR via the same `pytest` invocation as the
unit tests.

### CI workflow

Every push to a PR branch and every merge to `main` runs the workflow in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) (issues #8, #15, #76).
That workflow is the green/red signal contributors see on a PR; it is a thin
mirror of `make test-cov` + `make contract` + `make typecheck`, running
`ruff check`, `ruff format --check`, `mypy osimflow`, and `pytest
--cov=osimflow --cov-fail-under=85` on a single Python 3.12 runner. The
`ci` workflow is split into parallel `lint`, `typecheck`, `test`, `contract`,
and `security` jobs (issue #76) so wall-clock time is dominated by the
slowest single job, not the sum of all checks. A green check on every
required job is the gate to merge. Lint-only fast feedback is the
`lint` job inside the same `ci` workflow (the older separate
`.github/workflows/lint.yml` was folded in by issue #76); the
AGENTS.md / docs drift gate lives in the `contract` job of the same
workflow, with a mirror in
[`.github/workflows/agents-contract.yml`](.github/workflows/agents-contract.yml).
The performance-benchmark job (issue #10) lives in
[`.github/workflows/bench.yml`](.github/workflows/bench.yml) — it runs
the cold/warm 3-sample benchmark and uploads `benchmarks.json` as a
per-Python-version artifact (interpreted in
[`docs/benchmarks.md`](docs/benchmarks.md)).
See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for the full set of jobs
and the `make act` local mirror.

The nightly AWS Batch E2E workflow (issue #146) lives in
[`.github/workflows/aws-batch-e2e.yml`](.github/workflows/aws-batch-e2e.yml) —
it runs a 3-sample campaign against real AWS Batch infrastructure daily at
06:00 UTC and on manual `workflow_dispatch`. It uses OIDC authentication
(`aws-actions/configure-aws-credentials`) and requires `AWS_ROLE_ARN`
(secret), plus `AWS_REGION`, `AWS_BATCH_QUEUE`, and
`AWS_BATCH_JOB_DEFINITION` (repository variables).

---

## 6. Code style

### Python (osimflow/, bin/, user_scripts/, tests/)
- **PEP 8** + **type hints** everywhere. Public functions must have full annotations.
- Use `pathlib.Path` over `os.path`. Use `logging` (not `print`).
- Exceptions: catch, log with `exc_info=True`, **re-raise**. Never swallow.
- The package targets Python 3.12+. Do not add `from __future__ import annotations` (the syntax is supported natively).
- CLI entry points use `argparse` with subcommands (`osimflow run ...`).
- For OpenStudio Python bindings, isolate all `import openstudio` calls behind a `try/except` and provide a clear error message if the bindings aren't installed (relevant in `scientific_python_image` builds that don't include the heavy C++ stack).
- **BYOS contract**: a user-supplied function (in `user_scripts/`) is discovered by name. The Campaign validates the function signature with `inspect.signature`. Never define the same contract twice (once as a Python function, once as a CLI surface).
- **Cache key rule**: any code that affects per-step behavior must be hashed into the cache key. See `osimflow/campaign.py:_compute_code_hashes` for the pattern.
- **Executor resource directives**: `cpus`, `memory_mb`, `time_min` are advisory on `LocalExecutor`, propagated to Slurm via `submitit`'s `update_parameters` for `SlurmExecutor`, and translated to Boto3 `containerOverrides` for `AWSBatchExecutor`. Add new resource kinds by extending the `submit()` signature, not by adding process-local config.
- **Enforcement**: the rules above are enforced by `ruff` (lint + format), `mypy --strict` (types), and the AGENTS.md / docs contract checks. Run `make precommit` before pushing; CI mirrors the same checks. See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

### Shell / CLI
- All user-facing scripts use `set -euo pipefail`.
- Long options over short ones in documentation (e.g., `--openstudio_version` not `-o`).
- Per-sample stdout/stderr land at `${outdir}/work/sim/<sample_id>/{stdout,stderr}.log`.

---

## 7. Domain glossary

| Term | Meaning |
|---|---|
| **LHS** | Latin Hypercube Sampling — a stratified random sampling method. We use `scipy.stats.qmc.LatinHypercube`. |
| **`.osm`** | OpenStudio Model file (the parametric building energy model). |
| **`.osw`** | OpenStudio Workflow file — orchestrates which measures run on a model. |
| **`.idf`** | EnergyPlus Input Data File. **Out of scope** for OSimFlow (PRD §3.2). |
| **`.epw`** | EnergyPlus Weather file. **Out of scope** for OSimFlow. |
| **`eplusout.sql`** | SQLite output of an EnergyPlus simulation — primary source for KPI extraction. |
| **`eplusout.err`** | EnergyPlus error log. Used by `failed_simulations.csv` to extract one-line summaries (`grep -m 1 "  * Severe"`). |
| **`eplusout.log`** | EnergyPlus full log. Verbose; usually not archived unless debugging. |
| **EUI** | Energy Use Intensity (kWh/m²/yr or kBtu/ft²/yr) — the canonical headline KPI. |
| **Measure** | An OpenStudio plug-in (Ruby or Python) that modifies a model or workflow. Arguments are exposed in `.osw`. |
| **`template_sim_package`** | A user-supplied directory containing a base `.osm`/`.osw` and any required measure scripts. The campaign's starting point. |
| **`variables.yml`** | User-supplied input file declaring which parameters vary and their LHS distributions. |
| **BYOS** | "Bring Your Own Script" — user-provided Python scripts in `user_scripts/` that override default `bin/` logic. The override interface is a Python function signature. |
| **`run.json`** | The per-campaign monitoring trace (per-step timing, per-sample status, cache hit/miss). The primary observability artifact. |
| **`nrel/openstudio:<version>`** | The dynamic container image tag (consumed from Docker Hub), selected via `--openstudio_version`. The project does not own this image. |

---

## 8. Common gotchas (from PRD §6)

These are *known traps* the PRD explicitly calls out. When you write code, check yourself against this list:

1. **Large `eplusout.err` files** — delete from the work directory on successful simulation (PRD §1.4 *Intelligent Intermediate File Optimization*). The Campaign does this in `step_run_openstudio_sim` after a successful handle.result().
2. **Pre-flight parameter checks** — `step_apply_parameters` (via `bin/apply_params_to_model.py`) must verify that every LHS variable actually maps to an existing measure argument or `.osm` attribute *before* the simulation runs (PRD §1.4 *Pre-flight Parameter Applicability Validation*). Fail fast with a clear error.
3. **OpenStudio version pinning** — version lives in the **container tag** (`CONTAINER_OS.format(version=...)`) passed to the executor, not in `variables.yml` or env vars. The `nrel/openstudio:<version>` is dynamically selected in `step_run_openstudio_sim` from `--openstudio_version`. See [`docs/openstudio-image-distribution.md`](docs/openstudio-image-distribution.md) for the cache-key shape.
4. **Failed simulation summaries** — `failed_simulations.csv` must contain the *first* "Severe Error" line from each `eplusout.err`, not the whole file. Use `grep -m 1 "  * Severe"`. Implemented in `bin/aggregate_results.py`.
5. **`--archive_intermediates`** — when set, publish: all campaign inputs (`template_sim_package`, `variables.yml`) **and** per-sample `.osw/.osm` + `eplusout.sql`. Don't blindly archive `eplusout.err`/`eplusout.log` — too large. This is a future addition to the `Campaign` orchestrator (copy a step's `publishDir` pattern).
6. **AWS Batch security** — IAM roles for EC2 instances, not long-lived access keys (PRD §6 *Cloud Security Practices*). `AWSBatchExecutor` must source credentials from the IAM role on the compute environment, never from `boto3` long-lived keys.
7. **OpenStudio Measure dependencies** — custom Ruby/Python measure deps must be packaged *inside* the `template_sim_package`, not installed at runtime.
8. **Large time-series data** — hourly outputs for thousands of samples get huge fast. Default to daily/monthly aggregates in `aggregated_results.csv`; keep hourly data only in per-sample `.sql` files behind `--archive_intermediates`.
9. **Cache invalidation on `bin/*.py` edits** — the cache key includes a SHA-256 of every `bin/*.py` file (`code_hashes["bin"]`), so editing a script invalidates the cache for the affected step. **Do not** introduce a step that bypasses this hashing.
10. **SlurmExecutor `debug=True` by default** — without `--slurm-real`, jobs run locally. This is the documented `submitit` pattern. Always pass `--slurm_real` in production.
11. **Real vs stub OpenStudio CLI** — `run_openstudio_sim` invokes `openstudio.cli run -w workflow.osw` when the CLI is on PATH (detected via `shutil.which`). When the CLI is not available, it falls back to the stub (sleep + placeholder output). Set `OSIMFLOW_STUB_SIM=1` to force stub mode even when the CLI is installed (the escape hatch for testing). Existing integration tests use the stub; set `OSIMFLOW_RUN_REAL_OPENSTUDIO=1` to run the real E2E test.
12. **Missing workflow.osw in real CLI mode** — when `openstudio.cli` is available but no `workflow.osw` exists in the `modified_sim_package`, the work function raises `RuntimeError` before invoking the CLI. The `template_sim_package` must always contain a `workflow.osw`.

---

## 9. Task routing hints for AI agents

Use these patterns to decide where to make a change.

| If the user asks to… | Edit |
|---|---|
| Add a new KPI | `bin/extract_kpis.py` (and the schema doc in `osimflow/monitoring.py:SampleTrace`). |
| Add a new sampling distribution | `osimflow/algorithms/` package (subclass `BaseAlgorithm` in a new module, register via `AlgorithmRegistry.register` in `__init__.py`) **and** update the `variables.yml` example in `docs/`. |
| Add a new execution platform | New class in `osimflow/executors/__init__.py` (subclass `BaseExecutor`) **and** register via `ExecutorRegistry.register` in `__init__.py` **and** add the executor choice to `osimflow/__main__.py:_build_executor`. |
| Add a third-party algorithm plug-in | Declare an entry point in the external package's `pyproject.toml` under `[project.entry-points."osimflow.algorithms"]`. Discovery is automatic via `AlgorithmRegistry.discover_plugins()` — no code change in `osimflow/` needed (issue #432). |
| Add a third-party executor plug-in | Declare an entry point in the external package's `pyproject.toml` under `[project.entry-points."osimflow.executors"]`. Discovery is automatic via `ExecutorRegistry.discover_plugins()` — no code change in `osimflow/` needed (issue #432). |
| Add a new step to the DAG | A new method on `Campaign` in `osimflow/campaign.py` **and** call it from `Campaign.run` **and** emit `StepTrace` hooks. Update the directory map in this file. |
| Change a default OpenStudio version | `pyproject.toml` default **and** the `osimflow run --openstudio_version` default in `osimflow/__main__.py`. |
| Add a user-facing CLI flag | `osimflow/__main__.py:_build_parser` (add the `add_argument` call) **and** the `CampaignConfig` dataclass in `osimflow/config.py` **and** the `load_config` parser. |
| Change KPI output schema | `bin/extract_kpis.py` (output dict shape) **and** `bin/aggregate_results.py` (column ordering) **and** update the `variables.yml` example in `docs/`. |
| Fix a bug in parameter application | `osimflow/work.py:default_apply_parameters` first; only touch `osimflow/campaign.py:step_apply_parameters` if you also need different Campaign semantics (retry, cache, monitoring). |
| Add a new cache invalidation rule | `osimflow/campaign.py:step_*` (the cache key construction) **and** a test in `tests/integration/test_cache_invalidation.py`. |
| Add an export format | New module in `osimflow/exporters/` (e.g. `osa.py` for PAT) **and** add the `--target` choice to `osimflow/__main__.py` export subcommand. |
| Wire a real OpenStudio CLI invocation | `osimflow/work.py:run_openstudio_sim` — replace the stub body with `subprocess.run(["openstudio.cli", "run", ...])` and add per-sample stdout/stderr capture. |
| Change AWS Batch infrastructure (VPC, IAM, compute env) | `infra/aws/terraform/` — modify the Terraform module. IAM roles are in `iam.tf`, job definition in `job-definition.tf`. Run `terraform validate` to check. CI validates on `infra/` path changes. |
| Add a REST API endpoint | New route in `osimflow/api/app.py` **and** a test in `tests/unit/test_api_core.py`. Requires `pip install osimflow[api]`. After adding endpoints, re-run `python scripts/generate_openapi.py` to regenerate `docs/openapi.json`, and add a typed method + test in `osimflow/client.py` / `tests/unit/test_client.py` (issue #433). |
| Add or modify a health check | `osimflow/health.py` (add/modify a `_check_*` function, register it in `run_health_checks`) **and** a test in `tests/unit/test_health_check.py`. |

### 9.1 Tool selection decision tree

When the same task can be done several ways — and the opencode session
exposes both the standard tool family (Read/Write/Edit/Bash/Grep/Glob)
and the context-mode (`ctx_*`) and codebase-memory-mcp
(`codebase-memory-mcp_*`) tool families — reach for the **smallest
tool that gets the job done**. The decision tree below is the
project-specific tie-breaker (see §0.1 — this list wins over the
generic role prompt's tool guidance when they disagree).

| Task | Tool |
|---|---|
| Read a small file you intend to edit | `Read` |
| Read / transform a large file without showing full contents | `ctx_execute_file` |
| Find a function / class / route definition by name | `codebase-memory-mcp_search_graph` |
| Trace callers / callees of a function (impact analysis) | `codebase-memory-mcp_trace_path` |
| Search for a string literal in a known path | `Grep` |
| Run a shell command whose output is short and fixed | `Bash` |
| Run a shell command with large / unpredictable output | `ctx_execute` |
| Read documentation from a URL (lib docs, RFCs) | `ctx_fetch_and_index` |

---

## 10. Security & data handling

- **Never commit** `.osm`, `.osw`, `.idf`, `.epw`, `eplusout.*` files. The `.gitignore` already excludes them; double-check before staging.
- For very large inputs that *must* be tracked, use **`git-lfs`** — don't bypass the gitignore.
- **AWS**: IAM roles for EC2 compute environment only. No long-lived AWS access keys in the repo or in any config file. The `AWSBatchExecutor` must source credentials from the IAM role on the compute environment. The Terraform module (`infra/aws/terraform/iam.tf`) provisions least-privilege roles: a task role scoped to the campaign S3 bucket and CloudWatch Logs, a task-execution role for ECR image pulls, and a Batch service role.
- **Singularity on shared HPC**: never bind-mount secrets; pass via env vars or submitit's `ex.update_parameters(setup=...)`, not as container mounts.
- **BYOS user scripts**: when a user supplies a script, treat it as untrusted. The Campaign loads it via `importlib.util` and validates the function signature with `inspect.signature`. The default `LocalExecutor` runs in a thread pool with no resource limits — when wiring `SlurmExecutor` to production, set a per-job timeout (`time_min`) to bound blast radius.

---

## 11. References

- [PRD (docs/OSimFlow.md)](docs/OSimFlow.md) — sections to cite by number:
  - §1.4 — Key Differentiators
  - §3.1 — In-Scope Features
  - §4.2 — Key Modules/Processes
  - §5.2 — Phase 3 Deliverables
  - §6 — Potential Challenges & Considerations
- [Architecture decision (`.agents/results/architecture/0001-workflow-framework.md`)](.agents/results/architecture/0001-workflow-framework.md) — why the project uses a custom Python driver.
- [OpenStudio image distribution (`docs/openstudio-image-distribution.md`)](docs/openstudio-image-distribution.md) — where the OpenStudio CLI container comes from, and why we don't build it ourselves.
- [ADR-0002 (`.agents/results/architecture/0002-adopt-nrel-upstream-image.md`)](.agents/results/architecture/0002-adopt-nrel-upstream-image.md) — the decision record for adopting `nrel/openstudio` directly.
- [Decision verdict (`.agents/results/decision-verdict.md`)](.agents/results/decision-verdict.md) — the spike's outcome that ratified the foundation.
- [Monitoring decision (`.agents/results/monitoring-decision.md`)](.agents/results/monitoring-decision.md) — why OSimFlow ships BYO monitoring (per-campaign `run.json`).
- [Observability guide (docs/observability.md)](docs/observability.md) — pluggable observability backends (CloudWatch, Prometheus, OpenTelemetry).
- [AWS Batch Terraform guide (docs/aws-batch-terraform.md)](docs/aws-batch-terraform.md) — zero-to-running deployment guide for AWS Batch infrastructure (issue #130).
- [User Guide (docs/user-guide.md)](docs/user-guide.md) — the canonical entry point for users (installation, configuration, running campaigns, interpreting results, troubleshooting).
- [CONTRIBUTING.md](docs/CONTRIBUTING.md) — contributor onboarding.
- [GOVERNANCE.md](docs/GOVERNANCE.md) — community governance model.
- [`submitit` documentation](https://github.com/facebookincubator/submitit) — the Slurm executor backend.
- [OpenStudio CLI reference](https://openstudio.net/docs/cli/)
