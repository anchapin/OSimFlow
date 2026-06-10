# AGENTS.md

> **Audience:** AI coding assistants (Claude Code, Cursor, GitHub Copilot, Windsurf, Aider, Claude/Cursor/Cline/etc.) operating in this repository. Read this file before proposing or writing code. Human contributors are also welcome to read it — it's the canonical project-orientation document for both audiences.

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
| Executor abstraction | `BaseExecutor` with `LocalExecutor`, `SlurmExecutor`, `AWSBatchExecutor` | All conform to the same `submit()` → `Handle` interface. |
| Slurm backend | **`submitit.AutoExecutor`** | Drop-in `submitit.DebugExecutor` for local dev; real Slurm via `debug=False`. |
| AWS Batch backend | **`boto3`** (future) | Stub today; `AWSBatchExecutor.submit()` is a placeholder. |
| Containerization | **Docker** (local/cloud) and **Singularity** (HPC) | Two images: `nrel/openstudio:<version>` (consumed from Docker Hub — see [`docs/openstudio-image-distribution.md`](docs/openstudio-image-distribution.md)) and `scientific_python_image` (project-owned). |
| Simulation engine | **OpenStudio CLI** + **OpenStudio Python bindings** | Invoked as `openstudio.cli run -w workflow.osw` inside the dynamic container. |
| Statistical sampling | **`scipy.stats`** | Latin Hypercube Sampling (LHS) of design variables. |
| Data processing | **Python 3.11+**, **`pandas`**, **`pyarrow`** (Parquet) | KPI extraction, aggregation, error parsing. |
| Plotting | **`matplotlib`** + **`seaborn`** | 1–3 static summary plots (PNG/PDF). |
| Container registry | **Docker Hub** (OpenStudio) + **`ghcr.io`** (scientific Python) | `docker.io/nrel/openstudio:<version>`, `ghcr.io/anchapin/scientific_python_image:latest`. |
| Monitoring | **BYO: per-campaign `run.json` + tqdm** | See `.agents/results/monitoring-decision.md`. No external service. Optional MLflow add-on via `--mlflow_tracking_uri` (see `osimflow/mlflow_hook.py`). |
| CI/CD | **GitHub Actions** | (workflow to be added post-MVP) |

---

## 3. Directory map

| Path | Purpose |
|---|---|
| `osimflow/__init__.py` | Public API: `Campaign`, `SQLiteCache`, `CampaignConfig`, executors. |
| `osimflow/campaign.py` | The orchestrator class. ~300 LoC. Owns the 6-step DAG. |
| `osimflow/cache.py` | `SQLiteCache` + `CacheKey` — explicit, testable resume semantics. |
| `osimflow/config.py` | `CampaignConfig` dataclass + `load_config()`. |
| `osimflow/monitoring.py` | `RunTrace` + `StepTrace` + `SampleTrace`; writes `run.json`. |
| `osimflow/mlflow_hook.py` | Optional MLflow integration (issue #7). Lazy-imports `mlflow`; the Campaign calls these helpers when `--mlflow_tracking_uri` is set. |
| `osimflow/executors/__init__.py` | `BaseExecutor` + `LocalExecutor` + `SlurmExecutor` + `AWSBatchExecutor`. |
| `osimflow/work.py` | Per-step work functions: `default_apply_parameters`, `run_openstudio_sim`, `extract_kpis`, `aggregate_results`, `generate_plots`. The BYOS contract lives here. |
| `osimflow/__main__.py` | CLI entry point (`osimflow run ...`). |
| `bin/generate_lhs.py` | LHS sampler (scipy.stats). |
| `bin/apply_params_to_model.py` | Default parameter-application logic. |
| `bin/extract_kpis.py` | Default KPI extractor. |
| `bin/aggregate_results.py` | Result aggregation + error-summary extraction. |
| `bin/generate_plots.py` | Matplotlib/seaborn plot generator. |
| `tests/integration/test_cache_invalidation.py` | Cache invalidation test suite (8 cases). |
| `user_scripts/` | User-provided "Bring Your Own Script" (BYOS) overrides. See `user_scripts/README.md`. |
| `docs/OSimFlow.md` | The PRD — the source of truth for scope and architecture. |
| `docs/CONTRIBUTING.md` | Contributor onboarding (stub for Phase 3). |
| `docs/GOVERNANCE.md` | Community governance model (stub for Phase 3). |
| `.agents/results/` | Architecture decision records (ADRs) and the framework-decision verdict. |
| `.gitignore` | Standard Python ignores + `.osm/.osw/.idf/.epw/eplusout.*` (never commit). |
| `LICENSE` | MIT. |
| `README.md` | One-paragraph project pitch + status. |

---

## 4. Build & run commands

> All commands assume CWD = repo root. The orchestration foundation runs end-to-end against stub `bin/*.py` scripts (no real OpenStudio CLI needed for the MVP smoke test); see `tests/integration/test_cache_invalidation.py` for the cache-correctness gate.

### DAG step names (referenced from `osimflow/campaign.py`)

The 6-step DAG that the `Campaign` class drives:

- `GENERATE_LHS_SAMPLES` — single-shot, no fan-out.
- `APPLY_PARAMETERS` — fan-out over N samples.
- `RUN_OPENSTUDIO_SIM` — fan-out over N samples (heavy).
- `EXTRACT_KPIS` — fan-out over N samples.
- `AGGREGATE_RESULTS` — one shot after all KPIs.
- `GENERATE_BASIC_PLOTS` — one shot after aggregation.

### CLI flags (referenced from `osimflow/__main__.py`)

- `--executor` (local / slurm / aws_batch)
- `--max-workers` (local executor parallelism)
- `--slurm-partition`, `--slurm-account`, `--slurm-real`
- `--slurm-qos`, `--slurm-constraint`, `--slurm-gres` (advanced; submitit >= 1.5 only)
- `--aws-batch-queue`, `--aws-batch-job-definition`
- `--input_variables`, `--template_sim_package`, `--n_samples`, `--outdir`
- `--openstudio_version`, `--archive_intermediates`
- `--custom_apply_script`, `--custom_kpi_extractor` (BYOS)
- `--mlflow_tracking_uri` (optional; logs params/metrics/artifacts to MLflow. Requires `pip install osimflow[mlflow]`)
- `--log_level`

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
  --openstudio_version 3.4.0

# HPC run via Slurm — pined OpenStudio version, real Slurm (not debug)
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm_partition short \
  --openstudio_version 3.4.0 \
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
  --openstudio_version 3.5.0 \
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
  --openstudio_version 3.5.0 \
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

---

## 6. Code style

### Python (osimflow/, bin/, user_scripts/, tests/)
- **PEP 8** + **type hints** everywhere. Public functions must have full annotations.
- Use `pathlib.Path` over `os.path`. Use `logging` (not `print`).
- Exceptions: catch, log with `exc_info=True`, **re-raise**. Never swallow.
- The package targets Python 3.11+. Do not add `from __future__ import annotations` (the syntax is supported natively).
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

---

## 9. Task routing hints for AI agents

Use these patterns to decide where to make a change.

| If the user asks to… | Edit |
|---|---|
| Add a new KPI | `bin/extract_kpis.py` (and the schema doc in `osimflow/monitoring.py:SampleTrace`). |
| Add a new sampling distribution | `osimflow/campaign.py:step_generate_lhs` (extend the distribution dispatch) **and** update the `variables.yml` example in `docs/`. |
| Add a new execution platform | New class in `osimflow/executors/__init__.py` (subclass `BaseExecutor`) **and** add the executor choice to `osimflow/__main__.py:_build_executor`. |
| Add a new step to the DAG | A new method on `Campaign` in `osimflow/campaign.py` **and** call it from `Campaign.run` **and** emit `StepTrace` hooks. Update the directory map in this file. |
| Change a default OpenStudio version | `pyproject.toml` default **and** the `osimflow run --openstudio_version` default in `osimflow/__main__.py`. |
| Add a user-facing CLI flag | `osimflow/__main__.py:_build_parser` (add the `add_argument` call) **and** the `CampaignConfig` dataclass in `osimflow/config.py` **and** the `load_config` parser. |
| Change KPI output schema | `bin/extract_kpis.py` (output dict shape) **and** `bin/aggregate_results.py` (column ordering) **and** update the `variables.yml` example in `docs/`. |
| Fix a bug in parameter application | `osimflow/work.py:default_apply_parameters` first; only touch `osimflow/campaign.py:step_apply_parameters` if you also need different Campaign semantics (retry, cache, monitoring). |
| Add a new cache invalidation rule | `osimflow/campaign.py:step_*` (the cache key construction) **and** a test in `tests/integration/test_cache_invalidation.py`. |
| Wire a real OpenStudio CLI invocation | `osimflow/work.py:run_openstudio_sim` — replace the stub body with `subprocess.run(["openstudio.cli", "run", ...])` and add per-sample stdout/stderr capture. |

---

## 10. Security & data handling

- **Never commit** `.osm`, `.osw`, `.idf`, `.epw`, `eplusout.*` files. The `.gitignore` already excludes them; double-check before staging.
- For very large inputs that *must* be tracked, use **`git-lfs`** — don't bypass the gitignore.
- **AWS**: IAM roles for EC2 compute environment only. No long-lived AWS access keys in the repo or in any config file. The `AWSBatchExecutor` must source credentials from the IAM role on the compute environment.
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
- [CONTRIBUTING.md](docs/CONTRIBUTING.md) — *to be written*
- [GOVERNANCE.md](docs/GOVERNANCE.md) — *to be written*
- [`submitit` documentation](https://github.com/facebookincubator/submitit) — the Slurm executor backend.
- [OpenStudio CLI reference](https://openstudio.net/docs/cli/)
