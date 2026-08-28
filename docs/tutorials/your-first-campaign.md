# Your First Real Campaign

A step-by-step walkthrough for running your first parametric building-energy
simulation campaign with OSimFlow. By the end, you will have run a 5-sample
study, examined the output artifacts, and know how to scale up.

**Time:** ~25 minutes.
**Audience:** You know OpenStudio but are new to OSimFlow.

---

## Prerequisites

- **Python 3.12+** (`python --version` to check).
- A terminal (Bash, Zsh, or PowerShell).
- *(Optional)* Docker or Podman, if you want to run simulations inside the
  official OpenStudio container. The tutorial works without Docker — OSimFlow
  falls back to a stub mode automatically.

---

## Part 1: Install OSimFlow (2 minutes)

```bash
pip install -e ".[dev,aws,slurm]"
```

This installs:

| What | Where |
|---|---|
| `osimflow` CLI command | On your `PATH` |
| The `osimflow` Python package | Available for `import` |

Verify the installation:

```bash
osimflow --help
```

You should see the `run` subcommand listed.

### Optional: Install Docker and pull the OpenStudio image

If you want to run real simulations (not stubs), install
[Docker Desktop](https://docs.docker.com/get-docker/) and pull the
OpenStudio image for the version you need:

```bash
docker pull nrel/openstudio:3.11.0
docker run --rm nrel/openstudio:3.11.0 openstudio.cli --version
```

> **No Docker?** OSimFlow detects whether `openstudio.cli` is available. When
> it is not found, every simulation step runs in stub mode (fast placeholder
> output). This is fine for learning the workflow. Set
> `OSIMFLOW_STUB_SIM=1` to force stub mode even when the CLI is installed.

---

## Part 2: Understand the two inputs (5 minutes)

Every OSimFlow campaign needs exactly two inputs:

1. **`variables.yml`** — which parameters to vary and how.
2. **`template_sim_package/`** — your base model and workflow.

### 2.1 The template simulation package

A template simulation package is a directory containing at minimum:

```
example_package/
├── model.osm        # Your OpenStudio model
└── workflow.osw     # The workflow that defines which measures run
```

OSimFlow ships an `example_package/` at the repo root for testing. Open
`example_package/workflow.osw` in a text editor — you will see it defines
two measure steps with arguments like `heating_setpoint`, `wwr`
(window-to-wall ratio), and `wall_r_value`:

```json
{
  "name": "example-parametric-workflow",
  "steps": [
    {
      "measure_dir_name": "SetThermostatSchedule",
      "arguments": {
        "heating_setpoint": 20.0,
        "cooling_setpoint": 25.0
      }
    },
    {
      "measure_dir_name": "SetEnvelopePerformance",
      "arguments": {
        "heating_setpoint": 18.0,
        "wwr": 0.4,
        "wall_r_value": 3.5
      }
    }
  ]
}
```

These argument names are exactly what you reference in `variables.yml`.

### 2.2 The variables file

`variables.yml` tells OSimFlow which parameters to sweep and their
distributions. Here is a minimal version that varies three parameters:

```yaml
# my_variables.yml
variables:
  - name: heating_setpoint
    distribution: uniform
    min: 18.0
    max: 24.0

  - name: wwr
    distribution: uniform
    min: 0.2
    max: 0.6

  - name: wall_r_value
    distribution: uniform
    min: 2.0
    max: 6.0
```

Each variable has:

| Field | Purpose |
|---|---|
| `name` | Must match a measure argument or `.osm` attribute in your template. |
| `distribution` | Sampling distribution. OSimFlow supports `uniform`, `normal`, `lognormal`, `triangular`, `beta`, `gamma`, and `exponential`. |
| `min` / `max` | Range bounds (for `uniform`, `triangular`). |
| `mean` / `sigma` | Center and spread (for `normal`, `lognormal`). |

Create this file now:

```bash
cat > my_variables.yml << 'EOF'
variables:
  - name: heating_setpoint
    distribution: uniform
    min: 18.0
    max: 24.0
  - name: wwr
    distribution: uniform
    min: 0.2
    max: 0.6
  - name: wall_r_value
    distribution: uniform
    min: 2.0
    max: 6.0
EOF
```

> **What parameters can I vary?** Any argument in a measure step of your
> `workflow.osw`, or any attribute on the `.osm` model. OSimFlow runs a
> pre-flight check before simulations start — if a variable name in
> `variables.yml` does not map to anything in the template, the campaign
> fails immediately with a clear error. This prevents wasting compute time
> on invalid runs.

---

## Part 3: Run the campaign (5 minutes)

```bash
osimflow run \
  --executor local \
  --input_variables my_variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 \
  --outdir ./my_first_campaign \
  --openstudio_version 3.11.0
```

What this does:

1. **GENERATE_LHS_SAMPLES** — creates 5 unique parameter sets using Latin
   Hypercube Sampling.
2. **APPLY_PARAMETERS** — for each sample, writes the parameter values into
   a copy of the template package. Pre-flight validation catches invalid
   parameter names before anything runs.
3. **RUN_OPENSTUDIO_SIM** — invokes `openstudio.cli run -w workflow.osw` for
   each sample. Falls back to stub mode if the CLI is not installed.
4. **EXTRACT_KPIS** — reads `eplusout.sql` from each simulation and writes
   a JSON of key performance indicators (EUI, end uses, etc.).
5. **AGGREGATE_RESULTS** — merges all KPI JSONs into `aggregated_results.csv`.
6. **GENERATE_BASIC_PLOTS** — renders summary plots (EUI histogram, scatter
   plots).

You will see a progress bar as each fan-out step runs.

---

## Part 4: Inspect the output (5 minutes)

After the campaign finishes, the output directory looks like this:

```
my_first_campaign/
├── run.json                          # Campaign monitoring trace
├── aggregated_results.csv            # KPIs for every sample
├── aggregated_results.parquet        # Same data in Parquet format
├── failed_simulations.csv            # One-line error summaries (if any)
├── plots/
│   ├── eui_histogram.png             # Distribution of EUI across samples
│   └── scatter_*.png                 # Parameter vs. KPI scatter plots
└── work/
    ├── lhs/                          # LHS parameter sets
    ├── apply/                        # Per-sample modified packages
    ├── sim/                          # Per-sample simulation output
    │   ├── sample_000/
    │   │   ├── stdout.log
    │   │   └── stderr.log
    │   ├── sample_001/
    │   └── ...
    └── kpis/                         # Per-sample KPI JSON files
```

### 4.1 Check the campaign trace: `run.json`

```bash
cat my_first_campaign/run.json
```

The top-level fields are:

| Field | What it tells you |
|---|---|
| `elapsed_s` | Total wall-clock time. |
| `summary.n_succeeded` | Samples that completed all steps. |
| `summary.n_failed` | Samples with at least one failed step. |
| `steps[]` | Per-step timing and cache status (`HIT` / `MISS` / `MISS×N`). |
| `per_sample[]` | Per-sample status, exit codes, and log file paths. |

A healthy campaign shows `"n_failed": 0` and all `exit_code` values at `0`.

### 4.2 Read the results: `aggregated_results.csv`

```bash
cat my_first_campaign/aggregated_results.csv
```

Each row is one sample. Columns include the LHS parameter values and the
extracted KPIs (EUI, heating energy, cooling energy, etc.). Open this file
in Excel, pandas, or your preferred tool to compare runs.

### 4.3 Check for failures: `failed_simulations.csv`

```bash
cat my_first_campaign/failed_simulations.csv
```

If this file is empty or contains only a header row, all 5 samples
succeeded. When samples fail, each row contains the sample ID and the
first "Severe Error" line from `eplusout.err` — enough to diagnose the
problem without reading the full log.

### 4.4 Look at the plots

```bash
ls my_first_campaign/plots/
```

Open `eui_histogram.png` to see the spread of Energy Use Intensity across
your 5 samples. The scatter plots show how each input parameter (e.g.,
`wall_r_value`) correlates with EUI.

### 4.5 Debug a specific sample

If a sample failed, check its logs:

```bash
cat my_first_campaign/work/sim/sample_002/stderr.log
```

The `run.json` `per_sample[]` array gives you the exact log path for every
sample.

---

## Part 5: Iterate and scale up (8 minutes)

### 5.1 Re-run with the same settings (instant — cached)

Run the exact same command again:

```bash
osimflow run \
  --executor local \
  --input_variables my_variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 \
  --outdir ./my_first_campaign \
  --openstudio_version 3.11.0
```

The second run completes in under a second because every step is a cache
hit. You will see `cache: "HIT"` and `cache: "HIT×N"` in `run.json` for
all cached steps. OSimFlow uses a content-hashed SQLite cache — the same
inputs, code, and container version always produce the same cached result.

### 5.2 Increase the sample count

```bash
osimflow run \
  --executor local \
  --input_variables my_variables.yml \
  --template_sim_package ./example_package \
  --n_samples 50 \
  --outdir ./my_first_campaign_50 \
  --openstudio_version 3.11.0
```

A 50-sample run fills in the EUI distribution more completely and reveals
nonlinear relationships in the scatter plots. The `--max-workers` flag
(default: 4) controls how many samples run in parallel locally.

### 5.3 Edit your parameters and re-run

Change the window-to-wall ratio range in `my_variables.yml`:

```yaml
  - name: wwr
    distribution: uniform
    min: 0.1
    max: 0.8
```

Then run again (point to a new `--outdir` so you do not overwrite results):

```bash
osimflow run \
  --executor local \
  --input_variables my_variables.yml \
  --template_sim_package ./example_package \
  --n_samples 20 \
  --outdir ./my_second_campaign \
  --openstudio_version 3.11.0
```

### 5.4 Use a different OpenStudio version

```bash
osimflow run \
  --executor local \
  --input_variables my_variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 \
  --outdir ./os_380_campaign \
  --openstudio_version 3.8.0
```

The `--openstudio_version` flag selects the `nrel/openstudio:<version>`
container tag. Changing the version invalidates the simulation cache for
`RUN_OPENSTUDIO_SIM` — the other steps reuse their cached results.

### 5.5 Run on a Slurm cluster

When you have access to an HPC cluster with Slurm:

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition short \
  --input_variables my_variables.yml \
  --template_sim_package ./example_package \
  --n_samples 200 \
  --outdir ./hpc_campaign \
  --openstudio_version 3.11.0
```

Without `--slurm-real`, jobs run locally via `submitit.DebugExecutor` —
useful for testing the submission path without a real cluster.

### 5.6 Run on AWS Batch

```bash
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --input_variables my_variables.yml \
  --template_sim_package ./example_package \
  --n_samples 1000 \
  --outdir ./cloud_campaign \
  --openstudio_version 3.11.0
```

AWS Batch requires: `pip install osimflow[aws]`, a registered job
definition, and IAM role credentials on the compute environment. See
`docs/OSimFlow.md` for the full setup requirements.

---

## Where to go next

| Goal | Resource |
|---|---|
| Add your own KPIs | Write a custom KPI extractor in `user_scripts/` and pass `--custom_kpi_extractor` |
| Custom parameterization | Write a custom apply script and pass `--custom_apply_script` |
| Multi-climate studies | Add a variable with `target: epw_file` and a `mapping` of climate names to `.epw` paths |
| Track experiments in MLflow | Pass `--mlflow_tracking_uri http://your-server:5000` (requires `pip install osimflow[mlflow]`) |
| Understand all CLI flags | Run `osimflow run --help` |
| Interpret `run.json` in detail | See `docs/monitoring-schema.md` |
| Understand the architecture | See `docs/OSimFlow.md` (the PRD) |
| Contribute to OSimFlow | See `docs/CONTRIBUTING.md` and `docs/DEVELOPMENT.md` |

---

## Troubleshooting

### "PRE-FLIGHT check failed: unmapped parameter"

A variable name in `variables.yml` does not match any measure argument or
`.osm` attribute in your template. Check spelling and make sure the name
appears in `workflow.osw`.

### All simulations succeed but KPIs are zeros

You are running in stub mode (no real OpenStudio CLI). Install the CLI or
use the Docker container. Set `OSIMFLOW_STUB_SIM=0` to confirm.

### "ModuleNotFoundError: No module named 'submitit'"

Install the Slurm extra: `pip install -e ".[slurm]"`. For AWS Batch:
`pip install -e ".[aws]"`.

### Second run is not instant

Cache keys include a SHA-256 hash of every `bin/*.py` file plus
`osimflow/work.py`. If you edited any of those files, the cache
invalidates for the affected steps. This is by design — it ensures
results always reflect the current code.
