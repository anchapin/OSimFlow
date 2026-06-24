# OSimFlow User Guide

> The canonical entry point for OSimFlow users. This guide covers
> installation, configuration, running campaigns, interpreting results,
> advanced topics, and troubleshooting.

## Table of Contents

- [1. Introduction](#1-introduction)
- [2. Installation](#2-installation)
- [3. Quick Start](#3-quick-start)
- [4. Configuration Reference](#4-configuration-reference)
  - [4.1 CLI Flags](#41-cli-flags)
  - [4.2 variables.yml Schema](#42-variablesyml-schema)
  - [4.3 template_sim_package Structure](#43-template_sim_package-structure)
- [5. Running Campaigns](#5-running-campaigns)
  - [5.1 Local Execution](#51-local-execution)
  - [5.2 Slurm Execution (HPC)](#52-slurm-execution-hpc)
  - [5.3 AWS Batch Execution (Cloud)](#53-aws-batch-execution-cloud)
  - [5.4 Nomad Execution](#54-nomad-execution)
  - [5.5 Azure Batch Execution](#55-azure-batch-execution)
  - [5.6 Google Cloud Batch Execution](#56-google-cloud-batch-execution)
  - [5.7 Kubernetes Execution](#57-kubernetes-execution)
  - [5.8 PBS/Torque Execution](#58-pbstorque-execution)
  - [5.9 Dask-JobQueue Execution](#59-dask-jobqueue-execution)
  - [5.10 Docker Swarm Execution](#510-docker-swarm-execution)
  - [5.11 Dry-Run and Single-Sample Modes](#511-dry-run-and-single-sample-modes)
- [6. Understanding Results](#6-understanding-results)
  - [6.1 Output Directory Structure](#61-output-directory-structure)
  - [6.2 run.json Interpretation](#62-runjson-interpretation)
  - [6.3 aggregated_results.csv](#63-aggregated_resultscsv)
  - [6.4 failed_simulations.csv](#64-failed_simulationscsv)
  - [6.5 Quality Checks](#65-quality-checks)
- [7. Advanced Topics](#7-advanced-topics)
  - [7.1 BYOS Custom Scripts](#71-byos-custom-scripts)
  - [7.2 Time-Series Management](#72-time-series-management)
  - [7.3 Conditional Sampling and Baseline Comparison](#73-conditional-sampling-and-baseline-comparison)
  - [7.4 MLflow Integration](#74-mlflow-integration)
  - [7.5 Cache and Resume Behavior](#75-cache-and-resume-behavior)
  - [7.6 OpenStudio Version Selection](#76-openstudio-version-selection)
  - [7.7 Importing from OpenStudio Analysis Spreadsheet (.osa)](#77-importing-from-openstudio-analysis-spreadsheet-osa)
- [8. Troubleshooting](#8-troubleshooting)
- [9. Reference](#9-reference)

---

## 1. Introduction

**OSimFlow** is an open-source Python framework for running large-scale,
reproducible, parametric building-energy simulation campaigns. It wraps the
**OpenStudio CLI** and orchestrates hundreds to thousands of simulation
variants across local machines, HPC clusters (Slurm), or cloud (AWS Batch).

### Who is OSimFlow for?

- **Energy modelers** who need to explore parameter spaces (window U-values,
  insulation levels, HVAC setpoints) across many design variants.
- **Researchers** running sensitivity analyses or optimization studies.
- **Design practitioners** quantifying the impact of envelope or system
  choices on building energy performance.

### What does OSimFlow do?

1. **Generates** parameter samples using Latin Hypercube Sampling (LHS).
2. **Applies** those parameters to a template building model.
3. **Runs** each variant through OpenStudio/EnergyPlus.
4. **Extracts** key performance indicators (KPIs) from simulation output.
5. **Aggregates** results into CSV/Parquet for analysis.
6. **Plots** summary visualizations (EUI histograms, parameter scatter plots).

### What OSimFlow does NOT do

- It is not a building model editor. You provide a finished `.osm`/`.osw`.
- It does not provide a GUI. It is a CLI + Python library.
- It does not host web dashboards. Monitoring is via `run.json` + optional
  MLflow.

---

## 2. Installation

### Requirements

- **Python 3.12+** (the project uses modern syntax features)
- **pip** (or any PEP 660-compatible installer)
- **Docker** (optional — only needed for real OpenStudio simulations; see
  [docker-onboarding.md](docker-onboarding.md) for setup)

### Install from PyPI

```bash
pip install osimflow
```

### Install from source (development)

```bash
git clone https://github.com/anchapin/OSimFlow.git
cd OSimFlow
pip install -e .
```

### Optional extras

```bash
# AWS Batch executor support (brings in boto3)
pip install "osimflow[aws]"

# Slurm executor support (brings in submitit)
pip install "osimflow[slurm]"

# MLflow experiment tracking (brings in mlflow)
pip install "osimflow[mlflow]"

# All extras at once
pip install "osimflow[aws,slurm,mlflow]"

# Development dependencies (linting, testing, type checking)
pip install "osimflow[dev]"
```

### Docker setup (optional)

OSimFlow can run simulations inside the official `nrel/openstudio` container.
This eliminates the need to install OpenStudio on your host machine. See
[docker-onboarding.md](docker-onboarding.md) for:

- Installing Docker Desktop or Docker Engine
- Pulling the correct `nrel/openstudio:<version>` image
- Podman as an alternative ([podman-guide.md](podman-guide.md))
- Container configuration for Slurm (Singularity/Apptainer)

### Verify your installation

```bash
osimflow run --help
```

You should see a list of CLI flags organized by category.

---

## 3. Quick Start

> For a full step-by-step walkthrough with screenshots of every output file,
> see [tutorials/your-first-campaign.md](tutorials/your-first-campaign.md).

### Minimal example

You need two inputs: a **variables file** and a **template simulation
package**.

**1. Create `variables.yml`:**

```yaml
variables:
  - name: window_u_value
    distribution: uniform
    min: 1.0
    max: 5.0
  - name: cooling_setpoint
    distribution: uniform
    min: 22.0
    max: 28.0
```

**2. Prepare your template package** (a directory with at minimum a
`model.osm` and `workflow.osw`):

```
my_package/
├── model.osm
└── workflow.osw
```

**3. Run the campaign:**

```bash
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./my_package \
  --n_samples 10 \
  --outdir ./my_campaign \
  --openstudio_version 3.9.0
```

**4. Inspect the results:**

```bash
# Campaign summary
cat my_campaign/run.json

# Aggregated KPIs
head my_campaign/aggregated_results.csv

# Check for failures
cat my_campaign/failed_simulations.csv
```

That's it. The next sections explain every option in detail.

---

## 4. Configuration Reference

### 4.1 CLI Flags

All flags are passed to the `osimflow run` subcommand.

#### Campaign parameters

| Flag | Type | Default | Description |
|---|---|---|---|
| `--input_variables` | path | **required** | Path to `variables.yml`. |
| `--template_sim_package` | path | **required** | Path to the template simulation package directory. |
| `--n_samples` | int | **required** | Number of LHS samples to generate. |
| `--outdir` | path | **required** | Output directory for campaign results. |
| `--openstudio_version` | string | `3.11.0` | OpenStudio version. Determines the container image tag. |
| `--archive_intermediates` | flag | off | Archive per-sample `.osw`/`.osm`/`eplusout.sql` files. |

#### Executor selection

| Flag | Type | Default | Description |
|---|---|---|---|
| `--executor` | choice | `local` | Executor backend. Accepted values: `local`, `slurm`, `aws_batch`, `azure_batch`, `google_batch`, `kubernetes`, `pbs`, `dask_jobqueue`, `nomad`, `docker_swarm`. See [§5 Running Campaigns](#5-running-campaigns) for one-paragraph quick-starts per executor. |
| `--max-workers` | int | `4` | Thread pool size for `local` executor. |

#### Slurm-specific

| Flag | Type | Default | Description |
|---|---|---|---|
| `--slurm-partition` | string | `short` | Slurm partition (queue). |
| `--slurm-account` | string | none | Slurm account for job accounting. |
| `--slurm-real` | flag | off | Submit to real Slurm (default: debug/local mode via submitit). |
| `--slurm-qos` | string | none | Slurm QoS level. Requires submitit >= 1.5. |
| `--slurm-constraint` | string | none | Slurm constraint feature (e.g., `gpu`). Requires submitit >= 1.5. |
| `--slurm-gres` | string | none | Slurm generic resources (e.g., `gpu:1`). Requires submitit >= 1.5. |

#### AWS Batch-specific

| Flag | Type | Default | Description |
|---|---|---|---|
| `--aws-batch-queue` | string | `osimflow-batch-queue` | AWS Batch job queue name. |
| `--aws-batch-job-definition` | string | none | AWS Batch job definition ARN or name. |

#### Nomad-specific

| Flag | Type | Default | Description |
|---|---|---|---|
| `--nomad-address` | string | `NOMAD_ADDR` env or `http://127.0.0.1:4646` | Nomad cluster HTTP address. |
| `--nomad-datacentre` | string | `dc1` | Nomad datacentre to target. |
| `--nomad-dispatch-policy` | choice | `keep_manual` | Dispatch model: `keep_manual`, `force_dispatch`, `auto_prefer_dispatch`. |
| `--nomad-allocation-resolution-timeout-s` | float | `30.0` | Timeout to resolve EvalID to AllocationID. |
| `--nomad-poll-interval-s` | float | `5.0` | Initial allocation polling interval. |
| `--nomad-max-poll-interval-s` | float | `60.0` | Max allocation polling interval cap. |
| `--nomad-fanout-submit-rate-per-sec` | float | none | Optional fan-out submit rate limiter. |
| `--nomad-fanout-submit-chunk-size` | int | `0` | Optional bounded chunk size for Nomad fan-out submission. |
| `--shard-count` / `--shard-index` | int | none | Partition sharding controls for multi-coordinator runs. |
| `--shard-start` / `--shard-end` | int | none | Explicit sample index range sharding controls. |
| `--nomad-remote-results-only` / `--no-nomad-remote-results-only` | bool | `true` | **Deprecated compatibility toggle.** Default `true` keeps remote-first behavior. `--no-nomad-remote-results-only` temporarily enables legacy local-callable compatibility and is planned for removal after one minor release. |

Nomad runtime environment variables:

- `NOMAD_TOKEN` for ACL-authenticated clusters.
- `OSIMFLOW_PYTHON_CONTAINER_IMAGE` to override the Python post-processing
  image used by APPLY/KPI/AGGREGATE/PLOTS jobs when worker nodes cannot
  pull the default GHCR image.

#### BYOS (Bring Your Own Script)

| Flag | Type | Default | Description |
|---|---|---|---|
| `--custom_apply_script` | path | none | Path to a custom parameter-application script. |
| `--custom_kpi_extractor` | path | none | Path to a custom KPI extraction script. |

#### Advanced

| Flag | Type | Default | Description |
|---|---|---|---|
| `--dry-run` | flag | off | Run 1 sample locally to validate setup. Skips aggregation/plots. |
| `--sample` | int | none | Run only sample N (0-indexed) through steps 2-4. Reuses existing `samples.json`. |
| `--weather_dir` | string | `weather` | Subdirectory name for `.epw` files inside `template_sim_package`. |
| `--mlflow_tracking_uri` | string | none | MLflow tracking server URI. Requires `pip install osimflow[mlflow]`. |
| `--log_level` | string | `WARNING` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

### 4.2 variables.yml Schema

The `variables.yml` file declares which parameters vary and how they are
distributed. OSimFlow supports **nine distributions**: `uniform`,
`lognormal`, `normal`, `triangular`, `beta`, `gamma`, `exponential`,
`pareto`, and `weibull`.

**Minimal example:**

```yaml
variables:
  - name: window_u_value
    distribution: uniform
    min: 1.0
    max: 5.0
    target: measure_argument
    measure: SetEnvelopePerformance
    argument: window_u_value

  - name: cooling_setpoint
    distribution: normal
    mean: 24.0
    sigma: 2.0
    target: measure_argument
    measure: SetThermostatSchedule
    argument: cooling_setpoint
```

**With baseline comparison:**

```yaml
variables:
  - name: window_u_value
    distribution: uniform
    min: 1.0
    max: 5.0

baseline:
  sample_id: ashrae901_baseline
  parameters:
    window_u_value: 3.5
```

For the complete schema including all distribution parameters, conditional
variables, and `target` types, see
[variables-schema.md](variables-schema.md).

### 4.3 template_sim_package Structure

The `template_sim_package` is a directory containing your base building model
and workflow. At minimum it needs:

```
template_sim_package/
├── model.osm              # Seed building model
└── workflow.osw            # OpenStudio workflow definition
```

With measures and weather:

```
template_sim_package/
├── model.osm
├── workflow.osw
├── weather/
│   └── USA_CO_Denver.epw
└── measures/
    ├── SetThermostatSchedule/
    │   ├── measure.rb
    │   └── measure.xml
    └── SetEnvelopePerformance/
        ├── measure.rb
        └── measure.xml
```

For detailed guidance on packaging measures, `measure_paths` configuration,
Ruby vs Python measures, and the `requirements.txt` convention, see
[packaging-measures.md](packaging-measures.md).

---

## 5. Running Campaigns

### 5.1 Local Execution

Use the `local` executor for development, testing, and small campaigns
(< 50 samples with fast models).

```bash
osimflow run \
  --executor local \
  --max-workers 4 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./results \
  --openstudio_version 3.9.0
```

**Notes:**

- `--max-workers` controls the thread pool parallelism. Set to `1` for
  serial execution (easier debugging).
- Without Docker/the OpenStudio CLI on PATH, the campaign runs in **stub
  mode** (simulated work, placeholder outputs). This is useful for testing
  the pipeline without a real simulation engine.
- Force stub mode even with the CLI installed: `OSIMFLOW_STUB_SIM=1`.

### 5.2 Slurm Execution (HPC)

Use the `slurm` executor for campaigns on HPC clusters.

**Basic (debug mode — jobs run locally via submitit.DebugExecutor):**

```bash
osimflow run \
  --executor slurm \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 500 \
  --outdir ./results \
  --openstudio_version 3.9.0
```

**Production (real Slurm cluster):**

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition short \
  --slurm-account my_project \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 500 \
  --outdir ./results \
  --openstudio_version 3.9.0
```

**With advanced directives (requires submitit >= 1.5):**

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition gpu \
  --slurm-qos high \
  --slurm-constraint gpu \
  --slurm-gres gpu:1 \
  --input_variables variables.yml \
  --n_samples 200 \
  --outdir ./results \
  --openstudio_version 3.10.0
```

**Log directory setup:**

submitit writes job logs to a directory on a shared filesystem. Set the
environment variable before running:

```bash
export OSIMFLOW_SLURM_LOGS=/scratch/$USER/osimflow-slurm-logs
mkdir -p "$OSIMFLOW_SLURM_LOGS"
```

For the full Slurm deployment guide including Singularity/Apptainer
configuration, see [deployment/slurm.md](deployment/slurm.md).

### 5.3 AWS Batch Execution (Cloud)

Use the `aws_batch` executor for large-scale cloud campaigns.

```bash
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 1000 \
  --outdir ./results \
  --openstudio_version 3.9.0 \
  --archive_intermediates
```

**Prerequisites:**

1. Install with AWS support: `pip install "osimflow[aws]"`.
2. A registered AWS Batch job definition whose container image matches
   `nrel/openstudio:<openstudio_version>`.
3. AWS credentials via the **IAM role** attached to the Batch compute
   environment. Long-lived access keys are intentionally not supported.
4. `AWS_REGION` set in your environment.

The executor polls `batch.describe_jobs` with exponential backoff (5s start,
60s cap) until each task completes. Failed tasks raise a `RuntimeError`
with the Batch `statusReason`.

For the full setup guide including IAM roles, S3 buckets, and job
definitions, see [deployment/aws-batch.md](deployment/aws-batch.md).

### 5.4 Nomad Execution

Use the `nomad` executor for HashiCorp Nomad clusters:

```bash
export OSIMFLOW_PYTHON_CONTAINER_IMAGE=registry.example.com/osimflow/scientific_python_image:latest

osimflow run \
  --executor nomad \
  --nomad-address http://nomad.local:4646 \
  --nomad-datacentre dc1 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results \
  --openstudio_version 3.9.0
```

If Nomad clients cannot pull `ghcr.io/anchapin/scientific_python_image`,
preload a local tag on the worker nodes and point OSimFlow at it:

```bash
export OSIMFLOW_PYTHON_CONTAINER_IMAGE=scientific_python_image:local
```

See `docs/nomad-production.md` for the OpenStack preload workflow using
`scripts/setup_nomad_vm.sh`.

Nomad now runs in **remote-first** mode by default. In this mode, OSimFlow
does not execute per-sample local callables for result values; it relies on
remote result hints and configured transport materialization (shared filesystem
or object storage). If you still need the legacy local-callable behavior during
migration, use `--no-nomad-remote-results-only` temporarily. That compatibility
path is deprecated and kept for one minor release.

For scale hardening guidance (dispatch behavior, staged ramp 500→2k→5k→10k,
shard-first recommendation for 10k, and polling/submission/storage backpressure
operations), see `docs/nomad-production.md`.

### 5.5 Azure Batch Execution

Use the `azure_batch` executor to run campaigns on **Azure Batch** pools,
which is the recommended path when your organisation standardises on Azure
for compute, storage, and identity (Microsoft Entra ID / managed
identities). Provide an existing Batch account and pool via
`--azure-batch-account-name`, `--azure-batch-account-url`,
`--azure-batch-pool-id`, and `--azure-batch-location`; Azure Spot /
preemptible VMs are supported through `--azure-use-spot`,
`--azure-fallback-to-on-demand`, and `--azure-max-retries` (issue #352).
Authentication uses the per-job Azure SDK credential chain (env vars,
managed identity, or `DefaultAzureCredential`) — no long-lived keys are
read by the executor. See AGENTS.md §4 (`--azure-*`) for the complete
flag set; this executor currently has no dedicated deployment guide.

### 5.6 Google Cloud Batch Execution

Use the `google_batch` executor to run campaigns on **Google Cloud Batch**,
Google's managed batch-compute service. Configure the GCP project, region,
and the service account Batch will impersonate via
`--google-batch-project-id`, `--google-batch-region`, and
`--google-batch-service-account`. Preemptible VM handling mirrors the
Azure/AWS Spot story: `--google-use-spot`,
`--google-fallback-to-on-demand`, and `--google-max-retries` (issue
#352). Workload Identity / per-job service-account authentication is used
in place of long-lived keys. See AGENTS.md §4 (`--google-*`) for the
complete flag set; this executor currently has no dedicated deployment
guide.

### 5.7 Kubernetes Execution

Use the `kubernetes` executor to run campaigns as **Kubernetes Jobs** on a
self-hosted or managed cluster (EKS/GKE/AKS/Kind). Each per-sample task is
submitted as a Job in the namespace selected by `--kubernetes-namespace`;
resource directives (CPUs, memory) map to Kubernetes `requests`/`limits`.
Polling cadence is tunable with `--kubernetes-poll-interval-s` and
`--kubernetes-max-poll-interval-s` (issue #377). For RBAC manifests, node
sizing, and persistent-volume patterns, see
[kubernetes-deployment.md](kubernetes-deployment.md).

### 5.8 PBS/Torque Execution

Use the `pbs` executor for HPC sites that run **PBS Pro** or **Torque**
rather than Slurm. As with Slurm, pass `--pbs-real` to submit to the real
scheduler (otherwise `submitit` runs locally in debug mode) and supply
`--pbs-server` and `--pbs-queue` (issue #351). Resource directives
(`cpus`, `memory_mb`, `time_min`) are forwarded to `submitit`'s PBS
backend exactly like the Slurm executor. See AGENTS.md §4 (`--pbs-*`)
for the complete flag set; this executor currently has no dedicated
deployment guide.

### 5.9 Dask-JobQueue Execution

Use the `dask_jobqueue` executor for **elastic HPC** clusters where the
Dask scheduler should auto-scale workers across a backing batch system
(Slurm, PBS, or Kubernetes). This is the right choice when sample
throughput is bursty and you want the scheduler to grow and shrink the
worker pool. Key flags: `--dask-cluster-type`, `--dask-min-workers`,
`--dask-max-workers`, `--dask-cpus-per-worker`,
`--dask-memory-per-worker`, `--dask-walltime`, `--dask-queue`, and
`--dask-project` (issue #338). Pair it with `--task-queue dask` and
`--dask-scheduler-address` if you already operate a long-lived Dask
scheduler. See AGENTS.md §4 (`--dask-*`) for the complete flag set; this
executor currently has no dedicated deployment guide.

### 5.10 Docker Swarm Execution

Use the `docker_swarm` executor for campaigns on a **Docker Swarm**
cluster — a lightweight alternative when you already manage Swarm
services and do not want a full Nomad or Kubernetes deployment. Each
per-sample task is launched as a Swarm Service; polling uses exponential
backoff tuned by `--docker-swarm-poll-interval-s` and
`--docker-swarm-max-poll-interval-s` (issue #582). Configure the worker
image with `--docker-swarm-image` and the overlay network with
`--docker-swarm-network`. See AGENTS.md §4 (`--docker-swarm-*`) for the
complete flag set; this executor currently has no dedicated deployment
guide.

### 5.11 Dry-Run and Single-Sample Modes

**Dry-run** validates your setup by running exactly one sample locally:

```bash
osimflow run \
  --dry-run \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./test_run \
  --openstudio_version 3.9.0
```

This forces `n_samples=1`, uses the `local` executor, and runs steps 1-4
only (no aggregation or plots). Use it to catch configuration errors before
committing to a full campaign.

**Single-sample mode** runs a specific sample by index (0-based):

```bash
osimflow run \
  --sample 3 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./results \
  --openstudio_version 3.9.0
```

This skips the LHS generation step and reuses the existing `samples.json`.
Useful for debugging a specific failing sample.

---

## 6. Understanding Results

### 6.1 Output Directory Structure

After a campaign completes, the output directory contains:

```
my_campaign/
├── run.json                          # Campaign monitoring trace
├── aggregated_results.csv            # KPIs for every sample
├── aggregated_results.parquet        # Same data in Parquet format
├── failed_simulations.csv            # One-line error summaries (if any)
├── plots/
│   ├── eui_histogram.png             # Distribution of EUI across samples
│   ├── eui_distribution.png          # Histogram + empirical CDF (GAP-012)
│   ├── top_var_vs_eui.png           # Most variable parameter vs. EUI
│   ├── radar_plot.png               # Multi-KPI spider/radar chart (GAP-012)
│   ├── density_heatmap.png          # 2-D KDE heatmap (GAP-012)
│   ├── failure_summary.png           # Failure reason counts
│   ├── pareto_front.png             # Pareto front (if multi-objective)
│   ├── pareto_convergence.png       # Hypervolume convergence
│   ├── doe_*.png                   # DOE analysis plots
│   └── interactive_report.html       # Interactive Plotly report
└── work/
    ├── lhs/                          # LHS parameter sets (samples.json)
    ├── apply/                        # Per-sample modified packages
    ├── sim/                          # Per-sample simulation output
    │   ├── sample_000/
    │   │   ├── stdout.log
    │   │   └── stderr.log
    │   ├── sample_001/
    │   └── ...
    ├── kpis/                         # Per-sample KPI JSON files
    └── cache.sqlite                  # Resume cache (do not edit manually)
```

### 6.2 run.json Interpretation

The `run.json` file is the primary monitoring artifact. It captures
wall-clock timing, per-step cache status, and per-sample outcomes.

**Top-level fields:**

| Field | Description |
|---|---|
| `elapsed_s` | Total wall-clock time for the campaign. |
| `config` | Snapshot of campaign configuration (executor, version, n_samples, etc.). |
| `steps[]` | Per-step timing: name, duration, cache status (`HIT` / `MISS`). |
| `per_sample[]` | Per-sample status: sample_id, exit_code, log file paths. |
| `summary.n_succeeded` | Number of samples that completed all steps. |
| `summary.n_failed` | Number of samples with at least one failed step. |
| `baseline_comparison` | Baseline metrics (if `baseline:` was set in variables.yml). |

**Healthy campaign:**

```json
{
  "elapsed_s": 45.2,
  "summary": { "n_succeeded": 10, "n_failed": 0 },
  "steps": [
    { "name": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.3 },
    { "name": "APPLY_PARAMETERS", "cache": "MISS", "elapsed_s": 2.1 },
    ...
  ]
}
```

**Cached (resumed) campaign:**

All steps show `"cache": "HIT"` and `elapsed_s` is near zero.

For the complete schema with annotated examples, see
[runjson-guide.md](runjson-guide.md) and
[monitoring-schema.md](monitoring-schema.md).

### 6.3 aggregated_results.csv

A table with one row per sample. Common columns:

| Column | Description |
|---|---|
| `sample_id` | Sample identifier (e.g., `sample_000`). |
| `total_energy_kwh` | Total annual energy consumption. |
| `eui_kwh_per_m2` | Energy Use Intensity (kWh/m²/yr). |
| `heating_energy_kwh` | Annual heating energy. |
| `cooling_energy_kwh` | Annual cooling energy. |
| `...` | Additional KPIs depend on your model and extract script. |

A matching `aggregated_results.parquet` file is also written for efficient
downstream analysis with pandas, polars, or DuckDB.

When `--archive_intermediates` is set, per-sample `eplusout.sql` files are
preserved for detailed time-series queries.

### 6.4 failed_simulations.csv

If any simulations fail, this file contains one row per failure with:

| Column | Description |
|---|---|
| `sample_id` | The failing sample. |
| `error_summary` | First "Severe Error" line from `eplusout.err`. |
| `exit_code` | Process exit code. |

**Common error patterns:**

- **`** Severe ** ...`** — EnergyPlus convergence failure. Check model inputs.
- **`Exit code 137`** — Out of memory. Increase `memory_mb` or reduce model
  complexity.
- **`workflow.osw not found`** — The template package is missing a required
  file.

For detailed debugging, check the per-sample logs:

```bash
cat my_campaign/work/sim/sample_003/stderr.log
```

### 6.5 Quality Checks

After every campaign, verify:

1. **`summary.n_failed == 0`** in `run.json`. If non-zero, inspect
   `failed_simulations.csv`.
2. **EUI range is plausible.** For office buildings, expect roughly
   50-300 kWh/m²/yr depending on climate and envelope quality.
3. **No all-zero KPIs.** If all KPIs are zero, the extract step may be
   querying the wrong table in `eplusout.sql`.
4. **Sample count matches.** `n_succeeded` should equal `n_samples`.

### 6.6 Visualization Types

OSimFlow generates the following plot files in the `plots/` directory:

| File | Type | Description |
|---|---|---|
| `eui_histogram.png` | Histogram + KDE | Distribution of EUI across all samples |
| `eui_distribution.png` | **GAP-012** Histogram + Empirical CDF | Dual-axis view showing frequency and cumulative probability of EUI |
| `top_var_vs_eui.png` | Scatter | Most variable design parameter vs. EUI |
| `radar_plot.png` | **GAP-012** Radar / Spider | Normalised multi-KPI profile (min 3 KPIs required) |
| `density_heatmap.png` | **GAP-012** 2-D KDE Heatmap | Density surface for the two most variable design parameters |
| `failure_summary.png` | Horizontal bar | Counts of each failure reason |
| `pareto_front.png` | Scatter (multi-gen) | Pareto front coloured by generation |
| `pareto_convergence.png` | Line | Hypervolume convergence across generations |
| `doe_main_effects.png` | Line + error bars | DOE main effects per factor |
| `doe_interaction_matrix.png` | Heatmap | DOE 2-way interaction F-statistics |
| `doe_factor_sensitivity.png` | Horizontal bar | DOE factor contribution to variance |
| `interactive_report.html` | Plotly HTML | Standalone interactive report (open in any browser) |

**Radar / Spider Plot** (`radar_plot.png`): Each sample is a line on the spider chart with one spoke per KPI. All KPIs are min-max normalised to `[0, 1]` so the shape — not absolute values — reveals similarity across samples. The red "mean profile" shows the average normalised performance across the campaign.

**EUI Distribution** (`eui_distribution.png`): A dual-axis chart overlaying a frequency histogram (blue, left y-axis) with the empirical cumulative distribution function (red, right y-axis). A dashed red vertical line marks the baseline EUI when a baseline sample is provided. Use this to read both "how many samples fall below X kWh/m²/yr" (CDF, right y-axis at X) and "how many samples are near X" (histogram, left y-axis at X).

**Density Heatmap** (`density_heatmap.png`): A 2-D kernel density estimate (KDE) showing where the majority of samples concentrate in the space of the two most variable design parameters. Individual sample points are overlaid as white dots. Use this to identify parameter correlations and regions of the design space that are under-sampled.

All plots are also embedded in `interactive_report.html` for browser-based exploration.

---

## 7. Advanced Topics

### 7.1 BYOS Custom Scripts

OSimFlow supports **Bring Your Own Script (BYOS)** overrides for parameter
application and KPI extraction. Place your scripts in `user_scripts/` and
point to them with CLI flags.

**Custom KPI extractor:**

```bash
osimflow run \
  --custom_kpi_extractor user_scripts/my_kpis.py \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./results
```

The script must define a function matching the BYOS contract:

```python
def extract_kpis(sim_dir: Path, sample_id: str) -> dict:
    """Extract KPIs from simulation output.

    Args:
        sim_dir: Path to the per-sample simulation output directory.
        sample_id: Sample identifier string.

    Returns:
        Dictionary of KPI name -> numeric value.
    """
    ...
```

**Custom apply script:**

```python
def apply_parameters(
    template_pkg: Path,
    sample_params: dict,
    out_dir: Path,
    sample_id: str,
) -> Path:
    """Apply parameters to the template model.

    Args:
        template_pkg: Path to the template simulation package.
        sample_params: Dictionary of parameter name -> value.
        out_dir: Directory to write the modified package.
        sample_id: Sample identifier string.

    Returns:
        Path to the modified simulation package directory.
    """
    ...
```

The Campaign validates the function signature with `inspect.signature` at
load time. Ready-to-use templates are in `user_scripts/templates/`, and
worked examples are in `user_scripts/examples/`. See
[user_scripts/README.md](../user_scripts/README.md) for the full walkthrough.

### 7.2 Time-Series Management

By default, OSimFlow aggregates time-series data to **monthly** resolution
in `aggregated_results.csv` to keep file sizes manageable. Raw hourly data
stays in per-sample `eplusout.sql` files.

Control the resolution with `--ts_resolution`:

```bash
osimflow run \
  --ts_resolution daily \
  ...
```

Options: `hourly`, `daily`, `monthly` (default), `annual`.

For large campaigns (> 100 samples), monthly or annual aggregation is
recommended. See [time-series-management.md](time-series-management.md)
for:

- Worked examples of data size growth
- SQL query patterns for `eplusout.sql`
- Best practices for Parquet/DuckDB downstream analysis

### 7.3 Conditional Sampling and Baseline Comparison

**Conditional sampling** lets you define variables whose distribution
depends on the value of another variable. See the "Conditional Variables"
section of [variables-schema.md](variables-schema.md) for syntax.

**Baseline comparison** adds a fixed-parameter baseline sample to the
campaign for ASHRAE 90.1 or similar code-compliance analysis:

```yaml
baseline:
  sample_id: ashrae901
  parameters:
    window_u_value: 3.5
    cooling_setpoint: 24.0
```

When present, `aggregated_results.csv` includes percentage-improvement
columns relative to the baseline.

### 7.4 MLflow Integration

OSimFlow can log parameters, metrics, and artifacts to an MLflow tracking
server:

```bash
pip install "osimflow[mlflow]"

osimflow run \
  --mlflow_tracking_uri http://localhost:5000 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 50 \
  --outdir ./results
```

When set, the Campaign:

1. Creates an MLflow experiment named `osimflow`.
2. Logs all campaign parameters (n_samples, openstudio_version, etc.).
3. Logs aggregate metrics (mean EUI, failure rate).
4. Logs `aggregated_results.csv` and plots as artifacts.

Without `--mlflow_tracking_uri`, no MLflow code is imported (zero overhead).

### 7.5 Cache and Resume Behavior

OSimFlow uses a **SQLite cache** (`work/cache.sqlite`) to enable fast
resume. Re-running with the same `--outdir` is a cache hit on every
completed step.

**What invalidates the cache:**

- Editing any `bin/*.py` script (tracked via SHA-256 hash).
- Changing `--openstudio_version` (different container image).
- Changing `--input_variables` or `--n_samples` (different sample space).
- Changing `--template_sim_package` contents (different model).
- Changing BYOS scripts (`--custom_apply_script`,
  `--custom_kpi_extractor`).

**Force a full re-run:**

```bash
rm -rf my_campaign/work/cache.sqlite
```

Or delete the entire output directory:

```bash
rm -rf my_campaign/
```

**Performance:** A cached resume of a 5-sample campaign takes ~0.1s vs ~50s
for the initial run (verified in benchmarks).

### 7.6 OpenStudio Version Selection

The `--openstudio_version` flag determines which `nrel/openstudio` container
image is used. The version maps directly to the Docker Hub tag:

| `--openstudio_version` | Container image |
|---|---|
| `3.9.0` | `docker.io/nrel/openstudio:3.9.0` |
| `3.10.0` | `docker.io/nrel/openstudio:3.10.0` |
| `3.11.0` | `docker.io/nrel/openstudio:3.11.0` |

**When to pin:** Always pin in production campaigns for reproducibility.
Changing the version invalidates the cache for the simulation step.

**When to float:** During development, you may omit the flag (defaults to
`3.11.0`) or update it to test compatibility with a new OpenStudio release.

For supported versions and availability checking, see
[openstudio-image-distribution.md](openstudio-image-distribution.md).

### 7.7 Importing from OpenStudio Analysis Spreadsheet (.osa)

OSimFlow can import parametric variable definitions from an OpenStudio
Analysis Spreadsheet (`.osa`) file:

```bash
python -m osimflow import-osa path/to/analysis.osa --output variables.yml
```

This converts the `.osa` parameter definitions into a `variables.yml` file
compatible with OSimFlow's LHS sampler.

---

## 8. Troubleshooting

### "PRE-FLIGHT check failed: unmapped parameter"

**Cause:** A variable in `variables.yml` has a `target: measure_argument`
but the specified measure or argument name doesn't exist in `workflow.osw`.

**Fix:** Verify the `measure` and `argument` fields match the exact names
in your `.osw` file's `steps[]` entries.

### All simulations succeed but KPIs are zeros

**Cause:** The default KPI extractor queries standard EnergyPlus output
tables. If your model doesn't produce those tables (wrong output variables,
disabled reporting), the values will be zero.

**Fix:** Write a custom KPI extractor (`--custom_kpi_extractor`) that
queries the correct tables in `eplusout.sql`.

### "Simulations finish in seconds / KPIs are empty (stub mode)"

**Cause:** The real `openstudio.cli` is not on `PATH` (or
`OSIMFLOW_STUB_SIM=1` is set), so `run_openstudio_sim` falls back to the
stub — a short sleep that writes a placeholder `eplusout.sql`. The campaign
reports a green status with no error, but the KPIs are empty because no real
simulation ran.

**Fix:** Install the OpenStudio CLI (or run inside the `nrel/openstudio`
container), unset `OSIMFLOW_STUB_SIM`, and verify the real engine is invoked
with `OSIMFLOW_RUN_REAL_OPENSTUDIO=1`. See AGENTS.md §8 gotcha #11.

### "ModuleNotFoundError: No module named 'submitit'"

**Cause:** You selected `--executor slurm` without installing the Slurm
extras.

**Fix:** `pip install "osimflow[slurm]"`

### "Slurm job ran locally / finished instantly"

**Cause:** `SlurmExecutor` defaults to `debug=True` (submitit's
`DebugExecutor`). Running `--executor slurm` without `--slurm-real` silently
executes jobs on the local machine instead of submitting to the cluster —
they finish almost instantly with correct-looking output and no error.

**Fix:** Pass `--slurm-real` in production so jobs are submitted to the real
Slurm cluster. See AGENTS.md §8 gotcha #10.

### Second run is not instant (cache not working)

**Cause:** Something changed between runs. The cache key includes the
content hash of `bin/*.py` scripts, the OpenStudio version, and the
`variables.yml` content.

**Fix:** Check `run.json` for which steps show `MISS` vs `HIT`. If
`bin/*.py` files changed, the affected step invalidates.

### "container not found" / Docker errors

**Cause:** The `nrel/openstudio:<version>` image is not pulled.

**Fix:**

```bash
docker pull nrel/openstudio:3.9.0
```

See [docker-onboarding.md](docker-onboarding.md) for full setup.

### "AWS Batch task FAILED"

**Cause:** The Batch job definition's container image doesn't match the
requested OpenStudio version, or the IAM role lacks permissions.

**Fix:** Ensure the job definition uses `nrel/openstudio:<version>` as the
container image and the compute environment's IAM role has the necessary
permissions. See [deployment/aws-batch.md](deployment/aws-batch.md).

### OOM Kills (Exit Code 137)

**Cause:** The simulation exceeded available memory.

**Fix:** Increase memory allocation. For Slurm, use `--slurm-gres` or
request a node with more RAM. For AWS Batch, update the job definition's
memory setting. See [resource-allocation.md](resource-allocation.md) for
sizing guidance.

### "No samples generated from variables.yml"

**Cause:** The `variables.yml` file is empty, has no `variables:` key, or
contains a syntax error.

**Fix:** Validate your YAML. Ensure the `variables:` key contains a
non-empty list. See [variables-schema.md](variables-schema.md) for
the correct schema.

### Simulation timeout

**Cause:** The model is too complex for the allocated time.

**Fix:** Increase the time limit. For Slurm, the `SlurmExecutor` defaults
to 2 hours. Override via constructor parameters. For AWS Batch, update the
job definition's timeout. See [resource-allocation.md](resource-allocation.md).

---

## 9. Reference

### CLI Reference

> **`osimflow run --help` is authoritative.** AGENTS.md §4 ("Build & run
> commands" → "CLI flags") documents the **complete 100+ flag surface**;
> this section summarises the most-used flags by group. For any flag not
> shown here — including the full `--azure-*`, `--google-*`,
> `--kubernetes-*`, `--docker-swarm-*`, `--pbs-*`, `--dask-*`,
> `--nomad-*`, `--shard-*`, `--s3-artifact-*`, and advanced Slurm /
> AWS-Batch flags — run `osimflow run --help` or consult AGENTS.md §4.

#### Executor selection

| Flag | Description |
|---|---|
| `--executor {local,slurm,aws_batch,azure_batch,google_batch,kubernetes,pbs,dask_jobqueue,nomad,docker_swarm}` | Executor backend (default: `local`). See [§5](#5-running-campaigns) for per-executor quick-starts. |
| `--max-workers INT` | `local` executor thread-pool size (default: 4). |
| `--task-queue {none,dask}` | Distributed task-queue backend for multi-node fan-out (default: `none`). |

#### Campaign basics

| Flag | Default | Description |
|---|---|---|
| `--input_variables PATH` | **required** | Path to `variables.yml`. |
| `--template_sim_package PATH` | **required** | Template simulation package directory. |
| `--n_samples INT` | **required** | Number of samples to generate. |
| `--outdir PATH` | **required** | Output directory. |
| `--openstudio_version STRING` | `3.11.0` | OpenStudio version (drives the `nrel/openstudio:<version>` tag). |
| `--archive_intermediates` | off | Preserve per-sample `.osw`/`.osm`/`eplusout.sql`. |
| `--preset NAME` | none | Named preset of recommended flag values (issue #384); individual flags override the preset. |
| `--init-script` / `--finalize-script PATH` | none | Pre/post-campaign shell hooks (issue #108). |
| `--webhook-url URL` | none | Campaign-completion webhook callback (issue #283). |
| `--max-sample-retries INT` | `3` | Max retries for transient per-sample failures (issue #252). |

#### Algorithm & sampling

| Flag | Description |
|---|---|
| `--algorithm NAME` | Sampling strategy selector dispatched through `AlgorithmRegistry`. Built-ins: `lhs` (default), `sobol`, `halton`, `random_sampling`, `repeat_all`, `full_factorial`, `grid`, `morris`, `fast99`, `diag`, `calibration`, `de`, `da`, `ga`, `nsga2`, `spea2`, `pso`, `rgenoud`, `gaisl`, `sequential_search`, `uq`, `custom`. Add new strategies via the plug-in framework (issue #121). |
| `--max-generations INT` | Max DAG generations (default: 1 for single-shot LHS; raise for iterative algorithms — issue #122). |

#### Common Slurm flags

| Flag | Description |
|---|---|
| `--slurm-real` | Submit to a real Slurm cluster (default: debug/local via `submitit.DebugExecutor`). |
| `--slurm-partition STRING` | Partition (queue). |
| `--slurm-account STRING` | Account for job accounting. |
| `--slurm-qos` / `--slurm-constraint` / `--slurm-gres` | Advanced directives (requires `submitit >= 1.5`). |
| `--slurm-cost-per-node-hour USD` | Node-hour cost for `--enable-cost-tracking` (issue #447). |

See [deployment/slurm.md](deployment/slurm.md) for the full Slurm guide.

#### Common AWS Batch flags

| Flag | Description |
|---|---|
| `--aws-batch-queue STRING` | Batch job queue name. |
| `--aws-batch-job-definition STRING` | Job definition ARN or name. |
| `--aws-batch-max-spot-price-usd USD` | Spot price ceiling in USD/vCPU-hour (issue #131). |
| `--aws-batch-fallback-to-on-demand` | Fall back to on-demand when Spot exceeds ceiling or retries exhaust. |
| `--aws-batch-max-retries INT` | Max Spot-interruption retries (default: 3). |

See [deployment/aws-batch.md](deployment/aws-batch.md) and
[aws-batch-terraform.md](aws-batch-terraform.md) for full setup. Other
executors' flag groups (`--azure-*`, `--google-*`, `--kubernetes-*`,
`--docker-swarm-*`, `--pbs-*`, `--dask-*`, `--nomad-*`) are documented in
AGENTS.md §4; per-executor quick-starts are in [§5](#5-running-campaigns).

#### Observability & integration

| Flag | Description |
|---|---|
| `--observability {none,cloudwatch,prometheus,opentelemetry}` | Observability backend selector (default: `none`). Issues #127, #145. |
| `--cloudwatch-log-group` / `--cloudwatch-namespace` | CloudWatch config (when `--observability cloudwatch`). |
| `--prometheus-port INT` | Prometheus metrics HTTP port (when `--observability prometheus`). |
| `--otel-endpoint URL` | OpenTelemetry OTLP endpoint (when `--observability opentelemetry`). |
| `--mlflow_tracking_uri URL` | Optional MLflow tracking URI (requires `pip install osimflow[mlflow]`). |
| `--log-aggregation-url URL` | CloudWatch Logs aggregation URL for distributed log collection (issue #340). |

See [observability.md](observability.md) for backend configuration.

#### Result storage & cost tracking

| Flag | Description |
|---|---|
| `--result-storage-backend {local,s3,gs,azure}` | Result storage backend (default: `local`). Issue #339. |
| `--result-storage-bucket NAME` | Bucket / container name for the chosen backend. |
| `--result-storage-endpoint URL` | Custom S3-compatible endpoint URL. |
| `--enable-cost-tracking` / `--track-costs` | Enable cloud/HPC resource cost estimation (issue #447). |
| `--cost-on-demand-price USD` / `--cost-spot-price USD` | Price per vCPU-hour for cost estimation. |
| `--s3-artifact-bucket NAME` | Centralised S3 artifact bucket with optional presigned URLs (issue #601). |

See [cost-estimation.md](cost-estimation.md) for the cost model.

#### BYOS (Bring Your Own Script)

| Flag | Description |
|---|---|
| `--custom_apply_script PATH` | Custom parameter-application script. |
| `--custom_kpi_extractor PATH` | Custom KPI extraction script. |
| `--byos-trust-level {subprocess,inprocess}` | BYOS execution mode (default: `subprocess`). Issue #269. |
| `--byos-resource-limits ...` | CPU/memory limits for the BYOS subprocess wrapper (issue #343). |

See [§7.1 BYOS Custom Scripts](#71-byos-custom-scripts) for the contract.

#### Detach / Coordinator

| Flag | Description |
|---|---|
| `--detach` | Hand the campaign to a Coordinator service and exit immediately (issue #602). |
| `--coordinator-url URL` | Base URL of the Coordinator service (required with `--detach`). |
| `--shard-count` / `--shard-index` / `--shard-start` / `--shard-end` | Coordinator shard configuration for distributed execution. |

#### Debugging modes

| Flag | Description |
|---|---|
| `--dry-run` | Force `local` executor, 1 sample, steps 1-4 only. |
| `--sample INT` | Re-run a single sample (0-indexed) from existing `samples.json`. |
| `--skip-preflight` | Skip the `PREFLIGHT_RUN_MODEL` seed-model validation (issue #107). |
| `--offline` | Skip Docker Hub pulls, PyPI checks, and online weather downloads (issue #261). |
| `--log_level {DEBUG,INFO,WARNING,ERROR}` | Logging level (default: `WARNING`). |

#### `osimflow serve` subcommand

The optional REST API server (requires `pip install osimflow[api]`):

```bash
osimflow serve --outdir ./results --host 0.0.0.0 --port 8000 --read-write
```

`--read-write` enables campaign start/stop and SSE event streaming
(issue #143); omit it for read-only monitoring. Other `serve` flags:
`--api-key`, `--cors-origins`, `--rate-limit`, `--tls-cert` / `--tls-key`,
`--ui`, `--editor`, `--dashboard`, `--api-redis-url`. See [api.md](api.md)
for the endpoint reference and `osimflow serve --help` for the full list.

#### Other subcommands

| Subcommand | Purpose |
|---|---|
| `osimflow health` | System health checks (Python, SQLite, OpenStudio/Docker, disk, network). Issue #411. |
| `osimflow import-osa` / `osimflow export` | OSA analysis.json ↔ campaign config conversion. |
| `osimflow list` / `show` / `compare` / `status` / `download` | Multi-campaign registry and status operations. Issue #266. |
| `osimflow backup` / `restore` | Registry backup / restore / import. Issue #440. |
| `osimflow mark-for-reanalysis` / `merge` | Data-point lifecycle operations. Issues #418, #419, #420. |
| `osimflow measure` / `list-measures` | Measure discovery and BCL browsing. Issues #532, #580. |
| `osimflow query-results` / `export-results` / `aggregate-runs` | Result querying, export, and cross-campaign aggregation. Issues #585, #588. |

### Documentation Index

| Document | Description |
|---|---|
| [Tutorial: Your First Campaign](tutorials/your-first-campaign.md) | Step-by-step walkthrough |
| [variables.yml Schema](variables-schema.md) | Complete variable and distribution reference |
| [Packaging Measures](packaging-measures.md) | Building your template_sim_package |
| [Docker Onboarding](docker-onboarding.md) | Container setup for real simulations |
| [Podman Guide](podman-guide.md) | Podman as a Docker alternative |
| [run.json Guide](runjson-guide.md) | Detailed monitoring trace interpretation |
| [Monitoring Schema](monitoring-schema.md) | Complete run.json schema reference |
| [Resource Allocation](resource-allocation.md) | CPU, memory, and time sizing |
| [Time-Series Management](time-series-management.md) | Controlling output data volume |
| [Cost Estimation](cost-estimation.md) | AWS Batch and Slurm cost modeling |
| [OpenStudio Images](openstudio-image-distribution.md) | Container image versions and availability |
| [Deployment: Slurm](deployment/slurm.md) | Full Slurm/HPC setup guide |
| [Deployment: AWS Batch](deployment/aws-batch.md) | Full cloud deployment guide |
| [eplusout.sql Guide](eplusout-sql-guide.md) | Querying EnergyPlus SQL output |
| [Benchmarks](benchmarks.md) | Performance benchmarking reference |
| [Development Guide](DEVELOPMENT.md) | Contributing to OSimFlow internals |
| [Contributing Guide](CONTRIBUTING.md) | How to contribute |
| [PRD (OSimFlow.md)](OSimFlow.md) | Product Requirements Document |
| [Migration from openstudio-server](migration-openstudio-server.md) | Migrating from PAT and openstudio-server |
| [user_scripts/README.md](../user_scripts/README.md) | BYOS script templates and examples |

### The Six-Step DAG

Every campaign runs through these steps:

| Step | Fan-out | Description |
|---|---|---|
| `GENERATE_LHS_SAMPLES` | single-shot | Create parameter sample sets. |
| `APPLY_PARAMETERS` | per-sample | Apply parameters to the template model. |
| `RUN_OPENSTUDIO_SIM` | per-sample | Run the OpenStudio/EnergyPlus simulation. |
| `EXTRACT_KPIS` | per-sample | Parse simulation output into KPIs. |
| `AGGREGATE_RESULTS` | single-shot | Merge per-sample KPIs into CSV/Parquet. |
| `GENERATE_BASIC_PLOTS` | single-shot | Create summary visualizations. |

### Glossary

| Term | Meaning |
|---|---|
| **LHS** | Latin Hypercube Sampling — stratified random sampling. |
| **EUI** | Energy Use Intensity (kWh/m²/yr or kBtu/ft²/yr). |
| **`.osm`** | OpenStudio Model file. |
| **`.osw`** | OpenStudio Workflow file. |
| **`.epw`** | EnergyPlus Weather file. |
| **Measure** | An OpenStudio plug-in that modifies a model or workflow. |
| **BYOS** | Bring Your Own Script — user-provided Python overrides. |
| **`run.json`** | Per-campaign monitoring trace. |
| **`template_sim_package`** | User-supplied directory with model, workflow, and measures. |
