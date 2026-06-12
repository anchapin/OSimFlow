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
  - [5.5 Dry-Run and Single-Sample Modes](#55-dry-run-and-single-sample-modes)
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
| `--executor` | choice | `local` | Executor backend: `local`, `slurm`, `aws_batch`, or `nomad`. |
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

### 5.5 Dry-Run and Single-Sample Modes

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
│   └── scatter_*.png                 # Parameter vs. KPI scatter plots
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

### "ModuleNotFoundError: No module named 'submitit'"

**Cause:** You selected `--executor slurm` without installing the Slurm
extras.

**Fix:** `pip install "osimflow[slurm]"`

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

### Complete CLI Reference

```
osimflow run [OPTIONS]

Required:
  --input_variables PATH         Path to variables.yml
  --template_sim_package PATH    Path to template simulation package
  --n_samples INT                Number of LHS samples
  --outdir PATH                  Output directory

Executor:
  --executor {local,slurm,aws_batch,nomad}
                                 Executor backend (default: local)
  --max-workers INT              Local executor thread pool size (default: 4)

Slurm:
  --slurm-partition STRING       Partition name (default: short)
  --slurm-account STRING         Account for job accounting
  --slurm-real                   Submit to real Slurm (not debug mode)
  --slurm-qos STRING             QoS level (submitit >= 1.5)
  --slurm-constraint STRING      Feature constraint (submitit >= 1.5)
  --slurm-gres STRING            Generic resources, e.g. gpu:1 (submitit >= 1.5)

AWS Batch:
  --aws-batch-queue STRING       Job queue name
  --aws-batch-job-definition STRING
                                 Job definition ARN or name

Nomad:
  --nomad-address URL            Cluster HTTP address
  --nomad-datacentre STRING      Target datacentre (default: dc1)

Campaign:
  --openstudio_version STRING    OpenStudio version (default: 3.11.0)
  --archive_intermediates        Archive per-sample intermediates
  --weather_dir STRING           Weather subdirectory name (default: weather)
  --dry-run                      Validate setup with 1 sample
  --sample INT                   Run only sample N (0-indexed)

BYOS:
  --custom_apply_script PATH     Custom parameter-application script
  --custom_kpi_extractor PATH    Custom KPI extraction script

Integration:
  --mlflow_tracking_uri URL      MLflow tracking server URI

Logging:
  --log_level LEVEL              Logging level (default: WARNING)
```

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
