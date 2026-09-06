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
  - [7.8 Chaos Fault Injection (Resilience Testing)](#78-chaos-fault-injection-resilience-testing)
- [8. Health Checks](#8-health-checks)
- [9. Troubleshooting](#9-troubleshooting)
- [10. Reference](#10-reference)

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
| `--project` | string | `""` | Campaign name used for registry grouping (e.g. `--project 'Building Energy Analysis Q1 2026'`). |
| `--kpis` | string list | all KPIs | Restrict KPI extraction to the named KPIs (e.g. `--kpis eui peak_demand`). All KPIs extracted when omitted. |

#### Executor selection

| Flag | Type | Default | Description |
|---|---|---|---|
| `--executor` | choice | `local` | Executor backend. Accepted values: `local`, `slurm`, `aws_batch`, `azure_batch`, `google_batch`, `kubernetes`, `pbs`, `dask_jobqueue`, `nomad`, `docker_swarm`. See [§5 Running Campaigns](#5-running-campaigns) for one-paragraph quick-starts per executor. |
| `--max-workers` | int | `cpu_count() or 4` | Thread pool size for `local` executor. Defaults to the host CPU count (PR #1375). |
| `--submit-rps` | float | per-executor | Submit rate limit (requests/second) applied via the shared token-bucket rate limiter (issue #1563). Overrides the chosen executor's substrate-appropriate default (AWS/Azure/Google Batch = 10, Nomad/Kubernetes = 5, Slurm/PBS = 100, Docker Swarm = 20, Dask-JobQueue = 50, Local = off). Set to a low value for throttling conformance checks; leave unset to use the executor default. The legacy `--aws-batch-submit-rps` and `--nomad-fanout-submit-rate-per-sec` flags are still accepted but superseded by `--submit-rps`. |

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
| `--aws-batch-instance-type` | string | none | EC2 instance type scoping the Spot price-ceiling check; when omitted the check uses the minimum price across all instance types with a warning (issue #792). See [cost-estimation.md](cost-estimation.md). |
| `--aws-batch-submit-rps` | float | `800` | Token-bucket submit rate limit (submissions/second), below AWS Batch's 1000 TPS account limit; lower it on smaller accounts to avoid `ThrottlingException` (issue #1010). |
| `--aws-batch-spot-price` | float | `$0.0036`/vCPU·hr | Spot rate (USD per vCPU-hour) for cost tracking with `--track-costs` (issue #447). |
| `--aws-batch-on-demand-price` | float | `$0.0132`/vCPU·hr | On-demand rate (USD per vCPU-hour) for cost tracking (issue #447). See [cost-estimation.md](cost-estimation.md). |

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
| `--nomad-tls` | flag | off | Enable TLS for the Nomad HTTP API; pair with the mTLS flags below (SEC-009). |
| `--nomad-cert` / `--nomad-key` | path | none | Client certificate / private key (PEM) for mTLS; required when `--nomad-tls` is enabled. |
| `--nomad-ca-cert` | path | system default | CA bundle (PEM) used to verify the Nomad server certificate. |
| `--nomad-tls-verify` | bool | `true` | Verify the Nomad TLS certificate; disable only for development with self-signed certificates (`--nomad-tls-verify=false`). |
| `--nomad-allow-insecure-token` | flag | off | Allow `NOMAD_TOKEN` over non-TLS to a non-local address — fails closed without this flag (SEC-009, issue #1450); dev/test only. |
| `--nomad-dispatch-job-id` | string | derived from outdir hash | Override the Nomad dispatch job ID in dispatch mode, e.g. to reuse a pre-registered job spec (issue #1316). |

Nomad runtime environment variables:

- `NOMAD_TOKEN` for ACL-authenticated clusters.
- `OSIMFLOW_PYTHON_CONTAINER_IMAGE` to override the Python post-processing
  image used by APPLY/KPI/AGGREGATE/PLOTS jobs when worker nodes cannot
  pull the default GHCR image.

TLS/mTLS configuration for production clusters (including the full
`--nomad-tls` flag walkthrough) is covered in
[nomad-production.md](nomad-production.md).

#### Kubernetes-specific

| Flag | Type | Default | Description |
|---|---|---|---|
| `--kubernetes-queue-name` | string | none | Kueue `ClusterQueue` name applied as the `kueue.x-k8s.io/queue-name` label on Jobs; enables Kueue suspend/resume, fair-sharing, and preemption. Inert on clusters without Kueue installed (issue #997). |
| `--kubernetes-ttl-seconds-after-finished` | int | unset | Native Job `ttlSecondsAfterFinished` — the API server garbage-collects completed/failed Jobs after this many seconds, releasing etcd and pod resources across large sweeps (issue #997). |

See [kubernetes-deployment.md](kubernetes-deployment.md#cli-flags) for the
full Kubernetes flag table (including `--kubernetes-backoff-limit` and its
interaction with `--max-sample-retries`).

#### Supply-chain security

| Flag | Type | Default | Description |
|---|---|---|---|
| `--require-cosign-identity` | string | none | Verify the OpenStudio image signature at campaign init via keyless `cosign verify` (sigstore); the campaign refuses to run when verification fails or the `cosign` binary is unavailable (issue #1385). |
| `--cosign-oidc-issuer` | string | `https://token.actions.githubusercontent.com` | Expected OIDC issuer for `--require-cosign-identity` keyless verification. |
| `--container-digest` | string | none | Pin container images by SHA256 digest (`sha256:abc...` or `repo@sha256:abc...`); overrides the mutable tag for all executors (issue #1081). |
| `--ecr-repository` | string | none | ECR repository URI for the OpenStudio image — pull from your ECR mirror instead of Docker Hub. |

See [container-image-strategy.md](container-image-strategy.md) for the
signature-verification workflow and
[air-gapped-deployment.md](air-gapped-deployment.md) for ECR mirroring.

#### BYOS (Bring Your Own Script)

| Flag | Type | Default | Description |
|---|---|---|---|
| `--custom_apply_script` | path | none | Path to a custom parameter-application script. |
| `--custom_kpi_extractor` | path | none | Path to a custom KPI extraction script. |
| `--byos-timeout-s` | float | none (unbounded) | Wall-clock timeout in seconds for the BYOS subprocess and the real OpenStudio CLI simulation subprocess; a timeout kill is non-transient and is not retried (issues #1109, #1534). Default unbounded — bound long runs via the executor's walltime, or set this explicitly. |

#### Advanced

| Flag | Type | Default | Description |
|---|---|---|---|
| `--dry-run` | flag | off | Run 1 sample locally to validate setup. Skips aggregation/plots. |
| `--sample` | int | none | Run only sample N (0-indexed) through steps 2-4. Reuses existing `samples.json`. |
| `--weather_dir` | string | `weather` | Subdirectory name for `.epw` files inside `template_sim_package`. |
| `--mlflow_tracking_uri` | string | none | MLflow tracking server URI. Requires `pip install osimflow[mlflow]`. |
| `--log_level` | string | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `--bcl-api-key` | string | none | NREL BCL API key. Required when `--validate-measures` is set. Can also be set via `BCL_API_KEY` env var. |
| `--validate-measures` | flag | off | Validate measure arguments against the BCL taxonomy when discovering BCL measures. Logs warnings for argument name/type deviations. |
| `--uq-failure-threshold` | string list | none | Failure threshold for probability-of-failure analysis (`--algorithm uq`). Format: `kpi_name=threshold_value` (e.g. `eui=150`). Repeatable for multiple KPIs. |
| `--redis-url` | string | none | Redis URL for distributed campaign state and cache coordination; each process then uses a pid-private local SQLite file instead of contending on one database (issue #993). See [distributed-cache.md](distributed-cache.md). |
| `--resource-quota` | JSON | none | Campaign quota limits, e.g. `'{"max_samples": 100, "max_cost_usd": 5000.0, "max_wall_time_min": 240, "max_concurrent_samples": 10}'` — fail-fast at start when a quota is already exceeded; further sample submissions skipped once exhausted (issue #446). |
| `--alert-rules` | path | none | YAML file of alert rules (`event_type`, `severity`, `message_template`, `condition`) added alongside the built-in rules (issue #438). |
| `--alert-destinations` | path | none | YAML file of alert destinations (`webhook`, `email`, or `log`); without it alerts are only logged (issue #438). See [runjson-guide.md](runjson-guide.md). |
| `--observability-flush-interval` | float | `30.0` | Periodic metrics flush interval in seconds for observability backends; `0` disables periodic flush (flush only at campaign end) (issue #1186). See [observability.md](observability.md). |
| `--offline-bundle` | path | none | Offline bundle directory created by `scripts/bundle_offline.py` (contains `pip/`, `docker/`, and `weather/` subdirectories); required when `--offline` is set. See [air-gapped-deployment.md](air-gapped-deployment.md). |
| `--no-tui` | flag | off | Disable the `rich` TUI (auto-active when rich ≥ 13 is installed and stdout is a TTY); pass when piping output or in CI (issue #1221). |

#### Sampling & algorithms

| Flag | Type | Default | Description |
|---|---|---|---|
| `--uq-method` | choice | `latin_hypercube` | Uncertainty-Quantification sampling method — `latin_hypercube` or `monte_carlo` — used when `--algorithm uq` is set (issue #530). |
| `--uq-n-samples` | int | `--n_samples` | Number of Monte Carlo samples for UQ analysis, when a different sample count than `--n_samples` is wanted; used when `--algorithm uq` is set (issue #530). |
| `--nsga2-reference-points` | string | none | Reference (aspiration) points for R-NSGA-II as comma-separated fractions on the Pareto front, e.g. `0.25,0.5,0.75` for 2 objectives; only used when `--algorithm nsga2` (issue #529). |
| `--nsga2-reference-directions` | choice | none | R-NSGA-II reference-direction strategy: `das-dennis` (structured points), `energy` (Riesz s-Energy well-spaced), `wedge`, or `incremental`; only used when `--algorithm nsga2` (issue #529). |

### 4.2 variables.yml Schema

The `variables.yml` file declares which parameters vary and how they are
distributed. OSimFlow supports **ten distributions**: `uniform`,
`lognormal`, `normal`, `triangular`, `beta`, `gamma`, `exponential`,
`discrete`, `categorical`, and `conditional`.

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

- `--max-workers` controls the thread pool parallelism. Defaults to the
  host CPU count (with a fallback of 4); set to `1` for serial execution
  (easier debugging).
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

**Fail-dense by default (issue #944):** when Docker is unavailable or
the daemon is not in Swarm mode, `DockerSwarmExecutor.submit()` raises
a `RuntimeError` instead of silently falling back to `LocalExecutor`.
This prevents BYOS scripts from running in the orchestrator process — the
security-sensitive default that AGENTS.md §10 requires.

To enable development/CI fallback to `LocalExecutor`, set the
`OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1` environment variable:

```bash
export OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1
osimflow run \
  --executor docker_swarm \
  --input_variables variables.yml \
  ...
```

`--dry-run` mode automatically sets `OSIMFLOW_DOCKER_SWARM_DRY_RUN=1`
internally, so the fallback is available without an explicit env var.

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
    ├── samples.<label>.json          # Generated samples (label defaults to "" → samples.json)
    ├── apply/                        # Per-sample modified packages
    ├── sim/                          # Per-sample simulation output
    │   ├── sample_000/
    │   │   ├── stdout.log
    │   │   └── stderr.log
    │   ├── sample_001/
    │   └── ...
    ├── kpis/                         # Per-sample KPI JSON files (kpi_<sid>.json)
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
def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    """Extract KPIs from simulation output.

    Args:
        simulation_dir: Path to the per-sample simulation output directory.
        sample_id: Sample identifier string.
        out: Path to write the KPI JSON file.

    Returns:
        Path to the written KPI JSON file.
    """
    ...
```

**Custom apply script:**

```python
def apply_parameters(
    template: Path,
    parameters: dict,
    sample_id: str,
    out: Path,
) -> Path:
    """Apply parameters to the template model.

    Args:
        template: Path to the template simulation package.
        parameters: Dictionary of parameter name -> value.
        sample_id: Sample identifier string.
        out: Directory to write the modified package.

    Returns:
        Path to the modified simulation package directory.
    """
    ...
```

The Campaign validates the function signature with `inspect.signature` at
load time. Ready-to-use templates are in `user_scripts/templates/`, and
worked examples are in `user_scripts/examples/`. See
[user_scripts/README.md](../user_scripts/README.md) for the full walkthrough.

#### Security Considerations

BYOS scripts are treated as **untrusted** in production environments. OSimFlow
provides two execution modes controlled by `--byos-trust-level`:

| Trust level | Isolation | Production safe? | Notes |
|---|---|---|---|
| `subprocess` (default) | Isolated child process | **Yes** — recommended | Cannot access orchestrator memory, credentials, or open file handles. |
| `inprocess` | Same process as orchestrator | No | Loads user script directly via `importlib`. Can access all orchestrator state. Legacy mode. |

**Production recommendation:** Always use the default `subprocess` mode. Enable
`inprocess` only for local development when you trust the script author.

**`--require-trusted-scripts`** enforces `subprocess` mode and rejects `inprocess`
loading outright — use this flag on shared clusters or any environment where
script authors may be untrusted:

```bash
osimflow run \
  --require-trusted-scripts \
  --custom_apply_script user_scripts/untrusted_contributor.py ...
```

When `inprocess` is requested without `--require-trusted-scripts`, OSimFlow emits
a warning but proceeds:

```
WARNING: BYOS script running inprocess without --require-trusted-scripts.
Set --require-trusted-scripts to enforce subprocess isolation in production.
```

Cloud executors (AWS Batch, Slurm, Kubernetes) already run each job inside a
container or job isolation boundary. The `subprocess` BYOS mode provides an
additional defence-in-depth layer on top of that isolation.

### 7.2 Time-Series Management

By default, OSimFlow aggregates time-series data to **monthly** resolution
in `timeseries_aggregated.csv`/`.parquet` to keep file sizes manageable —
there is no `osimflow run` flag that changes this. Raw hourly data stays
in per-sample `eplusout.sql` files.

To re-aggregate a completed campaign at another resolution, re-run the
`bin/aggregate_results.py` work script directly with its
`--ts_resolution` flag:

```bash
python bin/aggregate_results.py \
  --kpis results/work/kpis/kpi_*.json \
  --simulation_dirs results/work/sim/* \
  --out_csv results/aggregated_results.csv \
  --out_parquet results/aggregated_results.parquet \
  --out_failed results/failed_simulations.csv \
  --ts_resolution daily
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

### 7.7 Recovery: Redis outage mid-campaign (issue #1562 / ADR-0004)

When `--redis-url` is set, four OSimFlow planes coordinate through one
Redis instance: `DistributedCache` (cross-worker cache hint),
`RedisDocumentStore` (the source of truth for documents), the
`DistributedJobQueue` (control plane), and the API rate limiter.
By design, the document store refuses silent divergence on outage
(issue #1014) — a mid-campaign Redis outage aborts the running
campaign rather than resuming against stale state.

The recovery story is **cache-replay resume**: re-run the campaign
against the same `--outdir`. Completed steps replay from cache;
only the steps the breaker caught in flight re-execute.

**Step 1 — Confirm Redis is back** (or stand up a new instance at the
same `--redis-url`). You can probe the deployment mode before
re-running:

```bash
osimflow health --offline --redis-url redis://redis.internal:6379/0
```

The `Redis Deployment Mode` check (issue #1562) reports the topology
the rest of the system assumes and surfaces the recovery story in
its `detail`. PASS for single-instance Redis; WARN for Sentinel /
Cluster URLs (the current client path cannot route through); FAIL
when the URL is unreachable.

**Step 2 — Re-run the campaign with the same `--outdir`**:

```bash
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 1000 \
  --outdir ./results \
  --redis-url redis://redis.internal:6379/0 \
  --openstudio_version 3.11.0
```

Completed steps hit the cache (`work/cache.sqlite` for the
single-node SQLite store, or the pid-private local SQLite backed by
the Redis shared store in distributed mode — see
`docs/distributed-cache.md`). The campaign picks up at the first
un-cached step and continues.

**Step 3 — Verify the recovery**. The `run.json.cache_hit_rate` field
reflects how much work was replayed; on a clean recovery it climbs
back to ~100 % for the completed steps. The `circuit_breaker_states`
field (issue #1191, #1307) records the per-plane breaker states at
shutdown — an `"open"` entry confirms Redis was unreachable.

**Why not Sentinel / Cluster?** Per
[ADR-0004](../.agents/results/architecture/0004-redis-ha-scope.md),
single-instance Redis is the scoped decision for the current
release. Sentinel / Cluster client wiring is tracked as future work
(see ADR-0004 §"Gap Closure Criteria"); deploying Sentinel today
gives you no failover benefit because the OSimFlow client path does
not route through Sentinel.

### 7.6 OpenStudio Version Selection

The `--openstudio_version` flag determines which `nrel/openstudio` container
image is used. The version maps directly to the Docker Hub tag:

| `--openstudio_version` | Container image | Notes |
|---|---|---|
| `3.7.0` | `docker.io/nrel/openstudio:3.7.0` | Floor version (Ubuntu 20.04 base) |
| `3.7.0-2204` | `docker.io/nrel/openstudio:3.7.0-2204` | Ubuntu 22.04 base |
| `3.8.0` | `docker.io/nrel/openstudio:3.8.0` | |
| `3.9.0` | `docker.io/nrel/openstudio:3.9.0` | |
| `3.10.0` | `docker.io/nrel/openstudio:3.10.0` | |
| `3.11.0` | `docker.io/nrel/openstudio:3.11.0` | latest stable (default) |

**When to pin:** Always pin in production campaigns for reproducibility.
Changing the version invalidates the cache for the simulation step.

**When to float:** During development, you may omit the flag (defaults to
`3.11.0`) or update it to test compatibility with a new OpenStudio release.

For the full supported-version policy and how to add a new tag, see
[openstudio-image-distribution.md](openstudio-image-distribution.md) and
[compatibility-matrix.md](compatibility-matrix.md).

### 7.7 Importing from OpenStudio Analysis Spreadsheet (.osa)

OSimFlow can import parametric variable definitions from an OpenStudio
Analysis Spreadsheet (`.osa`) file:

```bash
python -m osimflow import-osa path/to/analysis.osa --output variables.yml
```

This converts the `.osa` parameter definitions into a `variables.yml` file
compatible with OSimFlow's LHS sampler.

### 7.8 Chaos Fault Injection (Resilience Testing)

OSimFlow can inject controlled faults — process kills, network delay,
CPU spikes, memory pressure — into a running campaign to validate that
retries, caching, and executor failover behave under stress (issue #1013).
Chaos is **off by default**: it only fires when you explicitly pass
`--chaos-enabled`. Every injection is recorded under
`chaos_invocations` in [`run.json`](runjson-guide.md), so you can compare
chaos vs. baseline campaigns after the fact. A minimal smoke test:

```bash
osimflow run --executor slurm \
  --chaos-enabled --chaos-scenarios network_delay \
  --chaos-schedule before_step --chaos-probability 0.05 \
  --input_variables variables.yml --template_sim_package ./pkg \
  --n_samples 50 --outdir ./results-chaos-smoke
```

Never enable chaos against a production campaign you cannot afford to
interrupt. The full ten-flag reference, scenario catalog, and schedule
semantics live in [chaos-engine.md](chaos-engine.md).

---

## 8. Health Checks

Run `osimflow health` before starting a campaign to catch configuration problems early. The command probes Python version, core packages, SQLite, write permissions, external tools (OpenStudio CLI, Docker), disk space, and per-executor substrates.

```bash
# Full check (default output)
osimflow health

# JSON output (machine-readable)
osimflow health --json

# Skip network checks (air-gapped environments)
osimflow health --offline

# Promote a specific executor's check to CRITICAL (fail-fast if misconfigured)
osimflow health --executor slurm
```

### Exit codes

| Exit code | Meaning |
|---|---|
| `0` | All critical checks passed |
| `1` | One or more critical checks failed |

### Check categories

**Critical** — failures here mean OSimFlow cannot run at all:

- Python version >= 3.12
- Core packages (numpy, scipy, pandas, pyarrow, matplotlib, seaborn, tqdm, openpyxl, yaml)
- SQLite functional
- Write permissions in the campaign output directory

**Informational** — failures limit functionality but basic local runs still work:

- Optional packages (AWS SDK, Slurm, MLflow, etc.)
- External tools (OpenStudio CLI, Docker, Podman)
- Disk space
- Network connectivity
- Per-executor substrate checks (see below)

### Per-executor substrate checks

Each registered executor has a health check registered in `ExecutorRegistry`. These return **INFORMATIONAL** by default — a warning is printed but the command exits `0`. However, when you pass `--executor <name>`, that executor's check is promoted to **CRITICAL** because a failure there means the campaign cannot dispatch any sample:

```bash
# Check Slurm (INFORMATIONAL — warns but exits 0 even if Slurm is missing)
osimflow health

# Check Slurm (CRITICAL — exits 1 if Slurm is unreachable)
osimflow health --executor slurm

# Check AWS Batch (CRITICAL — exits 1 if boto3 or AWS region is missing)
osimflow health --executor aws_batch
```

Available executor checks: `local`, `slurm`, `pbs`, `aws_batch`, `azure_batch`, `google_batch`, `nomad`, `kubernetes`, `docker_swarm`, `dask_jobqueue`.

### JSON output format

`--json` emits a structured JSON document:

```json
{
  "summary": {
    "total": 17,
    "passed": 14,
    "failed": 1,
    "warnings": 2,
    "skipped": 0,
    "critical_failures": 1,
    "healthy": false
  },
  "checks": [
    {
      "name": "Python Version",
      "status": "pass",
      "category": "critical",
      "message": "Python 3.12 >= required 3.12",
      "detail": "Running Python 3.12.4 (CPython)"
    },
    ...
  ]
}
```

Use `--json` to integrate with monitoring dashboards or CI pipelines.

---

## 9. Troubleshooting

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

## 10. Reference

### CLI Reference

> **`osimflow run --help` is authoritative.** [§4.1 CLI Flags](#41-cli-flags)
> documents the complete run-flag surface by group — including the
> Kubernetes, supply-chain-security, and sampling & algorithm tables;
> this section summarises the most-used flags, documents
> subcommand-specific flags, and enumerates every subcommand. For any
> flag not shown inline, consult [§4.1](#41-cli-flags) or the linked
> specialist guide.

#### Executor selection

| Flag | Description |
|---|---|
| `--executor {local,slurm,aws_batch,azure_batch,google_batch,kubernetes,pbs,dask_jobqueue,nomad,docker_swarm}` | Executor backend (default: `local`). See [§5](#5-running-campaigns) for per-executor quick-starts. |
| `--max-workers INT` | `local` executor thread-pool size (default: host CPU count, fallback 4 — PR #1375). |
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
| `--max-sample-retries INT` | `3` | Max retries for transient per-sample failures. Honored by the `APPLY_PARAMETERS`, `RUN_OPENSTUDIO_SIM`, and `EXTRACT_KPIS` fan-out submits (issue #1394); see [Which DAG steps honor `--max-sample-retries`](#which-dag-steps-honor---max-sample-retries) below. |

#### `--max-sample-retries`: which DAG steps honor it

`--max-sample-retries` (default `3`, `0` disables retry) controls **orchestrator-side** retry on the per-sample fan-out submits that execute the heavy work of a campaign. As of the post-#1394 fan-out fix, `osimflow/campaign.py` forwards `max_retries=self.cfg.max_sample_retries` at **eight submission sites** spanning **three DAG steps**:

| DAG step | What gets retried | Why it can fail transiently |
|---|---|---|
| `APPLY_PARAMETERS` | Per-sample measure-argument mutation + `.osm` write | File locks, partial `.osm` writes, transient `_apply_osm_mutations` errors |
| `RUN_OPENSTUDIO_SIM` | Per-sample `openstudio.cli run -w workflow.osw` invocation | Container start failures, transient infrastructure errors, EnergyPlus convergence warnings that retried runs can clear |
| `EXTRACT_KPIS` | Per-sample `eplusout.sql` → KPI JSON | Read-after-write races on slow object storage, transient parser errors |

The remaining four DAG steps do **not** consume `--max-sample-retries`:

- `GENERATE_LHS_SAMPLES`, `PREFLIGHT_RUN_MODEL`, `AGGREGATE_RESULTS`, `GENERATE_BASIC_PLOTS` — all run **once per generation** (no per-sample fan-out). They have no transient-retry failure mode that the orchestrator-level retry knob is designed to address; tune resilience for those steps via the underlying executor (e.g. `--kubernetes-backoff-limit`, see below).

**Interaction with Kubernetes `backoffLimit`** — `--max-sample-retries` and `--kubernetes-backoff-limit` are **alternatives, not complements**. The kubelet-side `backoffLimit` restarts a failed pod inside the same Job without a resubmit round-trip through the orchestrator; `--max-sample-retries` resubmits the entire Job with the same parameter sample. Running both will **double-count failures** (a K8s pod restart counts as one failure to the orchestrator, which may then resubmit again). Pick one. See [`docs/kubernetes-deployment.md`](kubernetes-deployment.md#cli-flags) for the full `--kubernetes-backoff-limit` table and the "Kueue Interplay" section for the trade-off analysis.

**Per-sample cost impact** — the retry knob is a primary lever on campaign cost and result completeness. With `--max-sample-retries 3` and a transient-failure rate of `p`, the expected number of orchestrator-side submissions per sample is roughly `1 / (1 - p)` for `p < 1`, so a 5% transient-failure rate on a 1000-sample campaign adds ~50 expected resubmits. Set to `0` for hard-fail-fast (debug) workflows.

#### Algorithm & sampling

| Flag | Description |
|---|---|
| `--algorithm NAME` | Sampling strategy selector dispatched through `AlgorithmRegistry`. Built-ins: `lhs` (default), `sobol`, `halton`, `random_sampling`, `repeat_all`, `full_factorial`, `grid`, `morris`, `fast99`, `diag`, `calibration`, `de`, `da`, `ga`, `nsga2`, `spea2`, `pso`, `rgenoud`, `gaisl`, `sequential_search`, `uq`, `custom`. Add new strategies via the plug-in framework (issue #121). |
| `--max-generations INT` | Max DAG generations (default: 1 for single-shot LHS; raise for iterative algorithms — issue #122). |

UQ (`--uq-method`, `--uq-n-samples`, `--uq-failure-threshold`) and
R-NSGA-II (`--nsga2-reference-points`, `--nsga2-reference-directions`)
flags are documented in
[§4.1 Sampling & algorithms](#41-cli-flags).

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
[aws-batch-terraform.md](aws-batch-terraform.md) for full setup. The
remaining AWS Batch flags (`--aws-batch-instance-type`,
`--aws-batch-submit-rps`, and the `--track-costs` rates
`--aws-batch-spot-price` / `--aws-batch-on-demand-price`) and the other
executors' flag groups (`--azure-*`, `--google-*`, `--kubernetes-*`,
`--docker-swarm-*`, `--pbs-*`, `--dask-*`, `--nomad-*`) are documented in
[§4.1 CLI Flags](#41-cli-flags); per-executor quick-starts are in
[§5](#5-running-campaigns).

#### Observability & integration

| Flag | Description |
|---|---|
| `--observability {none,cloudwatch,prometheus,opentelemetry}` | Observability backend selector (default: `none`). Issues #127, #145. |
| `--cloudwatch-log-group` / `--cloudwatch-namespace` | CloudWatch config (when `--observability cloudwatch`). |
| `--prometheus-port INT` | Prometheus metrics HTTP port (when `--observability prometheus`). |
| `--otel-endpoint URL` | OpenTelemetry OTLP endpoint (when `--observability opentelemetry`). |
| `--mlflow_tracking_uri URL` | Optional MLflow tracking URI (requires `pip install osimflow[mlflow]`). |
| `--log-aggregation-url URL` | CloudWatch Logs aggregation URL for distributed log collection (issue #340). |

Alerting (`--alert-rules`, `--alert-destinations`) and the flush interval
(`--observability-flush-interval`) are documented in
[§4.1 Advanced](#41-cli-flags).

See [observability.md](observability.md) for backend configuration.

#### Result storage & cost tracking

| Flag | Description |
|---|---|
| `--result-storage-backend {local,s3,gs,azure}` | Result storage backend (default: `local`). Issue #339. |
| `--result-storage-bucket NAME` | Bucket / container name for the chosen backend. |
| `--result-storage-endpoint URL` | Custom S3-compatible endpoint URL. Must use `https://` for non-loopback hosts unless `--allow-insecure-storage-endpoint` is set (issue #1386). |
| `--enable-cost-tracking` / `--track-costs` | Enable cloud/HPC resource cost estimation (issue #447). |
| `--cost-on-demand-price USD` / `--cost-spot-price USD` | Price per vCPU-hour for cost estimation. |
| `--s3-artifact-bucket NAME` | Centralised S3 artifact bucket with optional presigned URLs (issue #601). |
| `--s3-artifact-endpoint URL` | Custom S3-compatible endpoint for the artifact bucket; same `https://` rule as `--result-storage-endpoint`. |
| `--s3-artifact-prefix PREFIX` | Prefix within the artifact bucket for this campaign (e.g. `campaign-123`); required when `--s3-artifact-bucket` is set (issue #601). |
| `--s3-artifact-region REGION` | AWS region for the artifact bucket; omitted → region from the IAM role or default credential chain (issue #601). |
| `--s3-artifact-presigned-url-expiration INT` | Presigned-URL expiration in seconds (default 3600; min 60, max 43200) — remote executor nodes must download artifacts within this window (issue #601). |

**HTTPS-only storage endpoints:** `--result-storage-endpoint` and
`--s3-artifact-endpoint` must use `https://` for any non-loopback host
(issue #1386); other schemes are rejected outright, and empty values pass
through untouched. Loopback hosts (`localhost`, `127.0.0.1`, `::1`,
`0.0.0.0`) are exempt — they never traverse a real network, so a local
MinIO / dev endpoint works unchanged. To reach a non-loopback plain-HTTP
endpoint, pass `--allow-insecure-storage-endpoint`: the campaign proceeds
with a loud warning, but plaintext HTTP leaks AWS SigV4 signing material
and campaign artifacts in cleartext — dev/test only, never production.

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
| `--log_level {DEBUG,INFO,WARNING,ERROR}` | Logging level (default: `INFO`). |

#### Chaos fault injection (resilience testing)

Opt-in fault injection for validating campaign resilience (issue #1013).
**Off by default** — no scenario fires unless `--chaos-enabled` is passed.
Every invocation is recorded in `run.json` → `chaos_invocations`.

| Flag | Default | Description |
|---|---|---|
| `--chaos-enabled` | off | Enable chaos injection (required for any scenario to fire). |
| `--chaos-scenarios LIST` | none | Scenarios to activate: `kill_switch`, `network_delay`, `cpu_spike`, `memory_pressure`. |
| `--chaos-schedule {none,before_step,after_step,per_sample}` | `none` | When injection fires relative to DAG steps / samples. |
| `--chaos-probability FLOAT` | `1.0` | Probability (0.0–1.0) that a given call triggers the injector. |
| `--chaos-delay-s FLOAT` / `--chaos-jitter-s FLOAT` | `0.1` / `0.05` | Base delay ± random jitter in seconds (`network_delay`). |
| `--chaos-duration-s FLOAT` | `0.5` | Fault duration in seconds (`cpu_spike` / `memory_pressure`). |
| `--chaos-intensity FLOAT` | `0.5` | Fault intensity fraction 0.0–1.0 (`cpu_spike` / `memory_pressure`). |
| `--chaos-size-mb INT` | `64` | Memory allocation size in MB (`memory_pressure`). |
| `--chaos-fail-after INT` | `2` | Calls before the kill switch activates (`kill_switch`). |

See [chaos-engine.md](chaos-engine.md) for the scenario catalog, schedule
semantics, and production examples, or
[§7.8 Chaos Fault Injection](#78-chaos-fault-injection-resilience-testing)
for the introductory walkthrough.

#### `osimflow serve` subcommand

The optional REST API server (requires `pip install osimflow[api]`):

```bash
osimflow serve --outdir ./results --host 0.0.0.0 --port 8000 --read-write
```

`--read-write` enables campaign start/stop and SSE event streaming
(issue #143); omit it for read-only monitoring. See [api.md](api.md)
for the endpoint reference and `osimflow serve --help` for the full list.

| Flag | Description |
|---|---|
| `--host` | Bind address (default: `127.0.0.1`). |
| `--port` | Port number (default: `8000`). |
| `--read-write` | Enable campaign start/stop and SSE event streaming (issue #143). |
| `--enable-writes` | Enable write endpoints (POST/PUT/DELETE); default: read-only. |
| `--registry PATH` | Campaign registry database path (default: `~/.osimflow/registry.db`). |
| `--ui` | Serve the Streamlit dashboard UI. |
| `--editor` | Enable the interactive variable designer. |
| `--dashboard` | Enable the campaign comparison dashboard. |
| `--api-redis-url` | Redis URL for distributed rate limiting and document store. |
| `--api-key` | Single-key API authentication (SEC-001; required on non-local interfaces). |
| `--api-keys-file` | Path to JSON file with multiple API keys and per-user roles (issue #395). File must be mode `0600`; group/world readable files are refused at load time (issue #1480). |
| `--allow-insecure-api-keys-file` | Override the `--api-keys-file` permission check (issue #1480; dev/test only — mirrors `--allow-insecure-storage-endpoint`). |
| `--cors-origins` | Comma-separated allowed CORS origins (e.g. `http://localhost:3000`). |
| `--rate-limit` | Rate limit string, e.g. `60/minute` (default: `60/minute`). |
| `--rate-limit-key` | Rate limit key type: `ip` (default), `user`, or `campaign` (issue #445). |
| `--tls-cert` | Path to PEM-encoded TLS certificate (SEC-004; requires `--tls-key`). |
| `--tls-key` | Path to PEM-encoded TLS private key (SEC-004; requires `--tls-cert`). |

#### Subcommand reference

OSimFlow registers 23 subcommands. One-line descriptions below; follow the
cross-links for depth.

| Subcommand | Purpose |
|---|---|
| `osimflow run` | Run a parametric campaign — the main command. See [§3 Quick Start](#3-quick-start) and [§5 Running Campaigns](#5-running-campaigns). |
| `osimflow warm-cache` | Pre-populate the simulation cache with `--n_warm` pilot samples (default 10) before a campaign; accepts the full `run` flag surface. See [§7.5 Cache and Resume Behavior](#75-cache-and-resume-behavior). |
| `osimflow import-osa` | Import a PAT/OpenStudio Analysis `.osa` or `analysis.json` into campaign config. See [§7.7](#77-importing-from-openstudio-analysis-spreadsheet-osa) and [pat-migration.md](pat-migration.md). |
| `osimflow export` | Export campaign state to an external format (`--target pat`) with `--variables` / `--n_samples` / `--algorithm`. |
| `osimflow serve` | Start the REST API server (flags above; requires `pip install osimflow[api]`). See [api.md](api.md). |
| `osimflow dashboard` | Launch a local ephemeral dashboard for campaign results (`--port`, default 8000). |
| `osimflow list` | List registered campaigns (`--status`, `--limit`, `--format`, `--project`, `--registry`). Issue #266. |
| `osimflow show` | Show detailed info for one campaign. Issue #266. |
| `osimflow compare` | Compare campaigns side by side — from the registry or arbitrary `--outdirs` with optional `--labels`; `--export` writes a combined CSV. |
| `osimflow aggregate-runs` | Aggregate KPI results from two or more campaign runs into a combined dataset (issue #588). |
| `osimflow status` | Show detailed status of a campaign (reads `run.json`). Issue #266. |
| `osimflow download` | Download results from a completed campaign (`--output-dir`, `--include-intermediates`). Issue #266. |
| `osimflow cancel` | Request graceful cancellation of a running campaign — the final `run.json` records `"cancelled"`. See [runjson-guide.md](runjson-guide.md) §2.6 for the lifecycle status fields. |
| `osimflow pause` | Request graceful pause of a running campaign (issue #444) — `run.json` records `paused_at`. |
| `osimflow resume` | Resume a paused campaign (issue #444). |
| `osimflow mark-for-reanalysis` | Mark a completed/failed sample for re-running (`--priority`, issue #420). |
| `osimflow merge` | Merge multiple data points into a single target (`--source-ids`, `--target-id`, `--target-work-dir`; issue #418). |
| `osimflow backup` | Create a backup of the campaign registry (`--output`, `--registry`; issue #440). |
| `osimflow restore` | Restore/import the campaign registry from a backup (`--merge` merges into the existing registry instead of replacing; issue #440). |
| `osimflow health` | Verify system health before starting a campaign (see [§8 Health Checks](#8-health-checks)). Issue #411. |
| `osimflow measure` | Discover and inspect measures in a template package: `osimflow measure list --template <pkg>` (issues #532, #580). See [packaging-measures.md](packaging-measures.md). |
| `osimflow query-results` | Query aggregated results across campaigns (`--campaign-ids` or `--outdirs`, `--filter`, `--page` / `--per-page`; issue #585). |
| `osimflow export-results` | Export aggregated results to CSV or JSON (`--include-failed` / `--no-include-failed`; issue #585). |

#### Subcommand-specific flags

Flags that only exist on non-`run` subcommands (the `run`-flag surface is
in [§4.1](#41-cli-flags); shared flags like `--outdir`, `--log_level`, or
`--openstudio_version` are not repeated here):

| Flag | Subcommand(s) | Description |
|---|---|---|
| `--registry PATH` | `list`, `show`, `compare`, `backup`, `restore`, `serve` | Campaign registry database path (default `~/.osimflow/registry.db`). |
| `--status STATUS` | `list` | Filter listed campaigns by status (`running`, `success`, `failure`). |
| `--limit INT` | `list` | Maximum number of campaigns to show (default 50). |
| `--format FMT` | `list`, `measure`, `query-results`, `export-results` | Output format (default `table`; JSON where supported). |
| `--outdirs PATH ...` | `compare`, `query-results` | Two or more campaign output directories to operate on, bypassing the registry. |
| `--labels LIST` | `compare`, `aggregate-runs` | Optional display labels for each path/campaign (must match the count). |
| `--export PATH` | `compare` | Export combined results to a CSV path. |
| `--campaign-ids IDS` | `query-results`, `export-results` | Comma-separated campaign IDs to query (default: all campaigns in the current directory). |
| `--filter EXPR` | `query-results`, `export-results` | Filter expression in `column op value` form, e.g. `eui > 100; status == ok`; supports `>`, `<`, `>=`, `<=`, `==`, `!=`. |
| `--page INT` / `--per-page INT` | `query-results` | Pagination (default 1 / 100; per-page max 1000). |
| `--include-failed` / `--no-include-failed` | `export-results` | Include (default) or exclude failed simulations in the export. |
| `--output PATH` | `import-osa`, `aggregate-runs`, `backup`, `export-results` | Output file path for the produced artifact. |
| `--output-dir PATH` | `download` | Destination directory (default `<outdir>-downloads/<campaign_id>`). |
| `--include-intermediates` | `download` | Also download per-sample `.osw`/`.osm` and `eplusout.sql` files. |
| `--priority INT` | `mark-for-reanalysis` | Priority of the new reanalysis sample (default 0). |
| `--source-ids IDS` | `merge` | Source sample IDs to merge (at least one required). |
| `--target-id ID` | `merge` | Target sample ID for the merged result. |
| `--target-work-dir PATH` | `merge` | Path to the target sample's work directory. |
| `--merge` | `restore` | Merge backup records into the existing registry instead of replacing all records (issue #440). |
| `--target FMT` | `export` | Export format (currently only `pat` for PAT-compatible `analysis.json`). |
| `--variables PATH` | `export` | Path to `variables.yml` to export. |
| `--n_warm INT` | `warm-cache` | Number of pilot samples to run for cache warming (default 10). |
| `--enable-writes` | `serve` | Enable write endpoints (POST/PUT/DELETE); default: read-only. |

### Documentation Index

Every user-facing page under `docs/` is reachable from this index
(internal artefacts like `gap-analysis-*` and the user guide itself are
exempt — see `tools/check_docs_sync.py::INDEX_EXEMPT_DOCS`).

#### Tutorials & Quick Start

| Document | Description |
|---|---|
| [Tutorial: Your First Campaign](tutorials/your-first-campaign.md) | Step-by-step walkthrough |
| [Tutorial: Getting Started](tutorials/getting-started.md) | Install + run your first campaign (~30 min) |
| [Tutorial: Advanced Topics](tutorials/advanced-topics.md) | Custom workflows for experienced users |
| [Tutorial: Migration from OpenStudio-Server](tutorials/migration-from-oss.md) | OSS/PAT → OSimFlow walkthrough |

#### Configuration & Reference

| Document | Description |
|---|---|
| [variables.yml Schema](variables-schema.md) | Complete variable and distribution reference |
| [run.json Guide](runjson-guide.md) | Detailed monitoring trace interpretation |
| [Monitoring Schema](monitoring-schema.md) | Complete run.json schema reference |
| [Resource Allocation](resource-allocation.md) | CPU, memory, and time sizing |
| [Time-Series Management](time-series-management.md) | Controlling output data volume |
| [Cost Estimation](cost-estimation.md) | AWS Batch and Slurm cost modeling |
| [Chaos Engine](chaos-engine.md) | Resilience testing via fault injection |
| [eplusout.sql Guide](eplusout-sql-guide.md) | Querying EnergyPlus SQL output |
| [Benchmarks](benchmarks.md) | Performance benchmarking reference |
| [Compatibility Matrix](compatibility-matrix.md) | OpenStudio version compatibility |
| [Observability](observability.md) | CloudWatch / Prometheus / OpenTelemetry backends |
| [API Reference](api.md) | REST endpoints, TLS, and API keys (`[api]` extra) |
| [Packaging Measures](packaging-measures.md) | Building your `template_sim_package` |
| [Measure Runner Guide](measure-runner-guide.md) | Running OpenStudio measures programmatically + BYOS KPI extractors |
| [CLI Lifecycle Management](cli-lifecycle-management.md) | OpenStudio CLI process supervision patterns |
| [R DataFrame Export](r-dataframe-export.md) | OSS R/Rserve → Parquet bridge workflow |

#### Deployment & Operations

| Document | Description |
|---|---|
| [Installation](installation.md) | `pip` install + standalone binary |
| [Docker Onboarding](docker-onboarding.md) | Container setup for real simulations |
| [Podman Guide](podman-guide.md) | Podman as a Docker alternative |
| [Container Image Strategy](container-image-strategy.md) | Digest pinning + ECR mirroring + cosign (issue #1320) |
| [Container Image Customization](container-customization.md) | Pre-installing measures, gems, patched OpenStudio builds |
| [OpenStudio Image Distribution](openstudio-image-distribution.md) | Container image versions and availability |
| [Distributed Cache](distributed-cache.md) | `--redis-url` TLS baseline + circuit breakers |
| [Air-Gapped Deployment](air-gapped-deployment.md) | Quick-start air-gap install |
| [Offline Deployment Guide](offline-deployment-guide.md) | Full air-gap / offline operational runbook |
| [Blue/Green Deployment](blue-green-deployment.md) | Zero-downtime `osimflow serve` updates (issue #402) |
| [MongoDB Storage](mongodb-storage.md) | SQLite → distributed storage backend options |
| [Deployment: Slurm](deployment/slurm.md) | Full Slurm/HPC setup guide |
| [Deployment: AWS Batch](deployment/aws-batch.md) | Full cloud deployment guide |
| [AWS Batch Terraform](aws-batch-terraform.md) | Zero-to-running AWS Batch IaC |
| [Deployment: Multi-Executor](deployment/multi-executor.md) | Azure / Google / PBS / Dask-JobQueue / Docker Swarm |
| [Nomad Production](nomad-production.md) | HA Nomad cluster topology and ACLs |
| [Kubernetes Deployment](kubernetes-deployment.md) | K8s Job-based deployment (with HMAC task payloads) |

#### Security

| Document | Description |
|---|---|
| [Secret Management](secret-management.md) | HMAC secret provisioning, Vault / Secrets Manager, TLS baseline, `--allow-insecure-storage-endpoint` rationale (issues #1449, #1459, #1386) |

#### Migration

| Document | Description |
|---|---|
| [Migration from openstudio-server](migration-openstudio-server.md) | Migrating from PAT and openstudio-server |
| [PAT Migration](pat-migration.md) | PAT-format data import/export |
| [Algorithm Migration](algorithm-migration.md) | OSS algorithms → OSimFlow equivalents |
| [Analysis Gem Migration](analysis-gem-migration.md) | openstudio-analysis-gem (Ruby) → OSimFlow Python library |
| [MongoDB Migration](mongodb-migration.md) | legacy OSS MongoDB collections → SQLite document store (GAP-018) |

#### Substrate Coverage

| Document | Description |
|---|---|
| [Substrate Coverage Matrix](substrate-coverage.md) | Real-E2E coverage by execution substrate and external sink (issue #1020) |

#### Project & Contributing

| Document | Description |
|---|---|
| [PRD (OSimFlow.md)](OSimFlow.md) | Product Requirements Document |
| [Development Guide](DEVELOPMENT.md) | Contributing to OSimFlow internals |
| [Contributing Guide](CONTRIBUTING.md) | How to contribute |
| [Governance](GOVERNANCE.md) | Project governance and decision-making |
| [Release Process](release-process.md) | Maintainer release runbook (semantic versioning) |
| [Branch Protection](branch-protection.md) | Settings-as-code for `main` branch rules (issue #975) |

#### BYOS

| Document | Description |
|---|---|
| [user_scripts/README.md](../user_scripts/README.md) | BYOS script templates and examples |

### The Seven-Step DAG

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
