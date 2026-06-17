# Migrating from OpenStudio-server and PAT to OSimFlow  <!-- docs-skip -->

> A comprehensive guide for users transitioning from the NREL
> **openstudio-server** web application and the **Parametric Analysis
> Tool (PAT)** to OSimFlow's CLI + library approach.

## Table of Contents

- [1. Why Migrate?](#1-why-migrate)
- [2. Conceptual Mapping](#2-conceptual-mapping)
- [3. Step-by-Step: Converting a PAT Project](#3-step-by-step-converting-a-pat-project)
- [4. Infrastructure Shift](#4-infrastructure-shift)
- [5. Data Format Mapping](#5-data-format-mapping)
- [6. From R Scripts to Python BYOS](#6-from-r-scripts-to-python-byos)
- [7. Monitoring and Dashboards](#7-monitoring-and-dashboards)
- [8. MLflow Dashboard Setup](#8-mlflow-dashboard-setup)
- [9. Common Migration Patterns](#9-common-migration-patterns)
- [10. Troubleshooting Migration Issues](#10-troubleshooting-migration-issues)

---

## 1. Why Migrate?

OSimFlow replaces openstudio-server's web-service architecture with a
lightweight **CLI + Python library** approach. Key motivations:

| Aspect | openstudio-server / PAT | OSimFlow |
|---|---|---|
| Architecture | Ruby on Rails web server + MongoDB | Python CLI + library |
| Scaling | Vertical (single server) | Horizontal (Slurm / AWS Batch / Nomad) |
| Data format | MongoDB (BSON) | Parquet + CSV + SQLite |
| Monitoring | Web dashboard (real-time) | `run.json` + optional MLflow |
| Custom analysis | R scripts via `analysis.json` | Python BYOS scripts |
| Reproducibility | Manual (database state) | Built-in cache + SHA-256 content hashing |
| Installation | Docker Compose + MongoDB | `pip install osimflow` |

### When to stay on openstudio-server

If you rely on the **PAT GUI** for interactive model exploration and
real-time parameter tweaking, the web-based workflow may still be
appropriate for early-stage design studies. OSimFlow is optimised for
**large-scale, batch-parametric campaigns** (hundreds to thousands of
simulations) where automation and reproducibility matter more than
interactive GUI feedback.

---

## 2. Conceptual Mapping

The table below maps every major openstudio-server concept to its
OSimFlow equivalent.

### Core Architecture

| openstudio-server | OSimFlow | Notes |
|---|---|---|
| `openstudio-server` web application | `osimflow` CLI (`osimflow run`) | Single binary, no server to manage |
| `openstudio-meta-cli` infrastructure | `infra/aws/terraform/` Terraform modules | Infrastructure-as-code for AWS Batch |
| MongoDB database | Parquet / CSV files in `outdir/` | File-based, no database to maintain |
| Rails API server (`POST /analyses`) | `osimflow run --input_variables ...` | CLI invocation replaces HTTP calls |
| PAT GUI (Electron app) | CLI + Python scripts + MLflow UI | No GUI; text-driven workflow |
| Background job queue (Delayed::Job) | Executor abstraction (`LocalExecutor`, `SlurmExecutor`, `AWSBatchExecutor`, `NomadExecutor`) | Pluggable backends |

### Data and Results

| openstudio-server | OSimFlow | Notes |
|---|---|---|
| MongoDB `analyses` collection | `outdir/run.json` | Per-campaign monitoring trace |
| MongoDB `data_points` collection | `outdir/aggregated_results.csv` / `.parquet` | One row per sample |
| MongoDB `measure_attributes` | KPI JSONs (`outdir/work/kpis/`) | Per-sample extraction |
| `analysis.json` (Ruby gem format) | `variables.yml` | Parameter and distribution definitions |
| `.osa` ZIP archive | `template_sim_package/` directory | Model + measures + weather |
| R report scripts (`*.R`) | Python BYOS scripts (`user_scripts/`) | See §6 below |

### Sampling and Algorithms

| openstudio-server | OSimFlow | Notes |
|---|---|---|
| `lhs` algorithm | `--algorithm lhs` (default) | Latin Hypercube Sampling |
| `nsga_nrel` algorithm | `--algorithm nsga2` | Multi-objective optimisation (requires `pip install osimflow[optimization]` in addition to base install) |
| `pso` algorithm | `--algorithm pso` | Particle Swarm Optimisation (requires `pip install osimflow[optimization]` in addition to base install) |
| `doe` / `ga` algorithm | `--algorithm de` | Differential Evolution |
| `sobol` algorithm | `--algorithm sobol` | Quasi-random sequences |
| PAT "Seed Model" | `--template_sim_package ./pkg` | Directory containing `.osm`/`.osw` |
| PAT "Alternatives" | `variables.yml` distributions | Declared as distribution parameters |

### Monitoring

| openstudio-server | OSimFlow | Notes |
|---|---|---|
| Web dashboard (port 8080) | `run.json` + optional MLflow UI | Real-time file or web UI |
| Real-time status via WebSocket | `run.json` polling / MLflow metrics | Text-based or MLflow dashboard |
| Download CSV from web UI | `outdir/aggregated_results.csv` | Direct file access |
| Download R data from web UI | Parquet / CSV output | Interoperable with pandas, DuckDB, R |

---

## 3. Step-by-Step: Converting a PAT Project

This section walks through converting an existing PAT `.osa` project
into OSimFlow's `template_sim_package/` + `variables.yml` format.

### Prerequisites

Since OSimFlow is not yet published on PyPI, install from source:

```bash
git clone https://github.com/anchapin/OSimFlow.git
cd OSimFlow
pip install -e ".[dev,aws,slurm]"
```

# Or install from PyPI (when available)
# pip install osimflow

### Step 1: Export from PAT

In PAT, use **File → Export → Export Analysis Spreadsheet (.osa)** or
**Export Analysis JSON** to save your project as a `.osa` ZIP file or
`analysis.json`.

### Step 2: Import into OSimFlow

Use the `import-osa` subcommand to convert variable definitions:

```bash
# From a .osa ZIP file
osimflow import-osa my_study.osa --output variables.yml

# From an already-extracted analysis.json
osimflow import-osa analysis.json --output variables.yml
```

This produces a `variables.yml` file mapping every OSA variable to
OSimFlow's distribution schema:

```yaml
algorithm: lhs
variables:
  - name: insul_r
    distribution: uniform
    min: 5.0
    max: 30.0
    display_name: Insulation R-value
    measure_argument: SetInsulationRValue.r_value
  - name: wwr_south
    distribution: uniform
    min: 0.2
    max: 0.8
    display_name: Window-to-Wall Ratio (South)
    measure_argument: SetWindowToWallRatio.wwr
```

### Step 3: Prepare the Template Simulation Package

PAT projects contain a seed model, measures, and weather files. Organise
them into a `template_sim_package/` directory:

```
template_sim_package/
├── model.osw            # OpenStudio Workflow file
├── model.osm            # Seed model (optional; .osw may reference it)
├── measures/
│   ├── SetInsulationRValue/
│   │   ├── measure.rb
│   │   └── measure.xml
│   └── SetWindowToWallRatio/
│       ├── measure.rb
│       └── measure.xml
└── weather/
    └── USA_CO_Denver.epw
```

**Key differences from PAT:**

- OSimFlow expects the `.osw` at the top level of the package directory.
- Weather files go in a `weather/` subdirectory (configurable via
  `--weather_dir`).
- Measures must be in subdirectories matching the measure class name
  (same convention as PAT).

### Step 4: Run the Campaign

```bash
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./template_sim_package \
  --n_samples 100 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

### Step 5: Verify Results

```bash
# Check campaign status
cat results/run.json | python -m json.tool | head -30

# View aggregated results
head results/aggregated_results.csv

# Check for failures
cat results/failed_simulations.csv
```

### Distribution Mapping Reference

When you run `osimflow import-osa`, distributions are mapped as follows:

| OSA Distribution Type | OSimFlow Distribution | Parameters |
|---|---|---|
| `uniform` | `uniform` | `min`, `max` |
| `normal` | `normal` | `mean`, `sigma` |
| `lognormal` | `lognormal` | `mean`, `sigma` |
| `triangular` | `triangular` | `min`, `max`, `mode` (optional) |
| `discrete` | `discrete` | `values` |
| `categorical` / `enum` | `categorical` | `values`, `mapping` (optional) |
| `pivot` | `categorical` (with `pivot: true`) | `values` |
| (none / locked) | `static` | `value` |

### Variable Type Mapping

| OSA Variable Type | OSimFlow Field | Notes |
|---|---|---|
| `variable` | `variable_type=variable` | Default; applies to model directly |
| `argument` | `variable_type=argument` | Applies to a measure argument |
| `pivot` | `pivot=true` | Categorical with pivot flag |

---

## 4. Infrastructure Shift

### From openstudio-meta-cli to Terraform

openstudio-server used the `openstudio-meta-cli` Ruby gem to provision
AWS infrastructure. OSimFlow replaces this with Terraform modules in
`infra/aws/terraform/`.

| Aspect | openstudio-meta-cli | OSimFlow Terraform |
|---|---|---|
| Language | Ruby (Chef / Vagrant) | HCL (Terraform) |
| IaC approach | Procedural scripts | Declarative modules |
| Compute target | Single EC2 instance | AWS Batch (serverless) |
| Container image | Custom Docker build | `nrel/openstudio` from Docker Hub |
| Reproducibility | Manual AMI snapshots | Terraform state + versioned modules |

### Quick Start: AWS Batch Deployment

```bash
cd infra/aws/terraform/

# Initialise Terraform
terraform init

# Review the plan
terraform plan -out=tfplan

# Apply
terraform apply tfplan
```

See [aws-batch-terraform.md](aws-batch-terraform.md) for the full
zero-to-running guide and [container-image-strategy.md](container-image-strategy.md)
for the ECR mirroring strategy.

### Running on Slurm (HPC)

openstudio-server had limited HPC support via custom scripts. OSimFlow
has first-class Slurm support via `submitit`:

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm_partition short \
  --input_variables variables.yml \
  --template_sim_package ./template_sim_package \
  --n_samples 500 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

See [deployment/slurm.md](deployment/slurm.md) for the full Slurm setup
guide.

---

## 5. Data Format Mapping

### MongoDB → Parquet / CSV

The biggest data-format change is from MongoDB's document store to
file-based Parquet and CSV output. This section maps common MongoDB
queries to their pandas / DuckDB equivalents.

#### MongoDB `data_points` → `aggregated_results.csv`

| MongoDB field | CSV / Parquet column | Type |
|---|---|---|
| `_id` | `sample_id` | string |
| `analysis_id` | (directory name) | — |
| `status.state` | `status` | string (`completed` / `failed`) |
| `name` | `sample_id` | string |
| `results.eui` | `eui_kwh_m2_yr` | float |
| `results.**` | Variable KPI columns | float |
| `measure_attributes.**` | Per-measure parameter columns | mixed |
| `created_at` | `start_time` | ISO 8601 timestamp |
| `end_time` | `end_time` | ISO 8601 timestamp |

#### Common Queries: MongoDB vs pandas

**All completed data points with EUI:**

```javascript
// MongoDB
db.data_points.find(
  { "status.state": "completed", "analysis_id": ObjectId("...") },
  { "results.eui": 1, "name": 1 }
)
```

```python
# pandas
import pandas as pd

df = pd.read_csv("results/aggregated_results.csv")
completed = df[df["status"] == "completed"][["sample_id", "eui_kwh_m2_yr"]]
```

**Average EUI by parameter bin:**

```javascript
// MongoDB aggregation pipeline
db.data_points.aggregate([
  { $match: { "status.state": "completed" } },
  { $group: {
      _id: "$measure_attributes.insul_r_bin",
      avg_eui: { $avg: "$results.eui" }
  }}
])
```

```python
# pandas
import pandas as pd

df = pd.read_csv("results/aggregated_results.csv")
df["insul_r_bin"] = pd.cut(df["insul_r"], bins=5)
summary = df.groupby("insul_r_bin")["eui_kwh_m2_yr"].mean()
```

**Failed simulations with error messages:**

```javascript
// MongoDB
db.data_points.find(
  { "status.state": "failed" },
  { "name": 1, "status.error_message": 1 }
)
```

```python
# OSimFlow: direct file access
import pandas as pd

failed = pd.read_csv("results/failed_simulations.csv")
# Columns: sample_id, error_summary, exit_code
```

#### DuckDB for Large Campaigns

For campaigns with >10,000 samples, DuckDB provides fast SQL queries
directly on Parquet files:

```python
import duckdb

con = duckdb.connect()

# Query Parquet directly (no loading into memory)
result = con.execute("""
    SELECT
        insul_r,
        wwr_south,
        eui_kwh_m2_yr,
        heating_kwh,
        cooling_kwh
    FROM read_parquet('results/aggregated_results.parquet')
    WHERE eui_kwh_m2_yr > 0
    ORDER BY eui_kwh_m2_yr
    LIMIT 20
""").fetchdf()

print(result)
```

### R Data Frames → pandas DataFrames

| R Operation | pandas Equivalent |
|---|---|
| `read.csv("data.csv")` | `pd.read_csv("data.csv")` |
| `df$col` | `df["col"]` or `df.col` |
| `subset(df, eui > 100)` | `df[df["eui_kwh_m2_yr"] > 100]` |
| `aggregate(eui ~ group, df, mean)` | `df.groupby("group")["eui_kwh_m2_yr"].mean()` |
| `merge(df1, df2, by="id")` | `pd.merge(df1, df2, on="id")` |
| `ggplot(df, aes(x, y)) + geom_point()` | `df.plot.scatter(x="x", y="y")` or `sns.scatterplot(data=df, x="x", y="y")` |

See the [R to Python BYOS Cheatsheet](../user_scripts/examples/r_to_python_migration.md)
for worked examples covering data loading, filtering, aggregation, and
plotting.

---

## 6. From R Scripts to Python BYOS

openstudio-server allowed users to write R scripts for custom analysis.
In OSimFlow, the equivalent is the **BYOS (Bring Your Own Script)**
system, where you write Python functions that the campaign calls at the
appropriate step.

### Quick Reference: R → Python BYOS

| openstudio-server R Script | OSimFlow Python Script | CLI Flag |
|---|---|---|
| Custom R reporting script | `custom_kpi_extractor.py` | `--custom_kpi_extractor` |
| R post-processing script | `aggregate_results.py` override | (built-in, overridable via BYOS) |
| R data visualisation | Python matplotlib / seaborn plots | Built-in `GENERATE_BASIC_PLOTS` step |

### R Script Pattern (openstudio-server)

```r
# R script that runs after simulations complete
# openstudio-server passes the MongoDB connection

library(mongolite)
library(ggplot2)

# Connect to MongoDB
m <- mongo("data_points", url = "mongodb://localhost:27017/osh")

# Extract completed simulations
data <- m$find(
  '{"status.state": "completed"}',
  '{"name": 1, "results.eui": 1, "measure_attributes.insul_r": 1}'
)

# Plot EUI vs insulation
ggplot(data, aes(x = measure_attributes.insul_r, y = results.eui)) +
  geom_point() +
  labs(x = "Insulation R-value", y = "EUI (kWh/m²/yr)",
       title = "EUI vs Insulation")
ggsave("eui_vs_insulation.png")
```

### Python BYOS Equivalent (OSimFlow)

```python
# user_scripts/custom_kpi_eui_migration.py
"""Extract EUI and per-end-use breakdown — replaces R reporting script."""

from pathlib import Path
import json
import sqlite3


def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    """Extract KPIs from eplusout.sql.

    This replaces the R script that queried MongoDB for per-sample results.
    Instead of MongoDB queries, you read from the EnergyPlus SQL output.

    Args:
        simulation_dir: Directory containing eplusout.sql for this sample.
        sample_id: Sample identifier string.
        out: Directory to write the KPI JSON.

    Returns:
        Path to the KPI JSON file.
    """
    sql_path = simulation_dir / "eplusout.sql"
    if not sql_path.exists():
        raise FileNotFoundError(f"eplusout.sql not found for {sample_id}")

    kpis: dict = {"sample_id": sample_id, "kpis": {}}

    with sqlite3.connect(sql_path) as con:
        # EUI from TabularData (replaces MongoDB results.eui)
        row = con.execute("""
            SELECT Value
            FROM TabularDataWithStrings
            WHERE TableName = 'Site and Source Energy'
              AND RowName = 'Total Site Energy'
              AND ColumnName = 'Energy Per Total Floor Area'
        """).fetchone()
        if row:
            kpis["kpis"]["eui_mj_m2"] = float(row[0])
            kpis["kpis"]["eui_kwh_m2_yr"] = float(row[0]) / 3.6

        # End-use breakdown (replaces separate MongoDB queries)
        for row in con.execute("""
            SELECT RowName, ColumnName, Value
            FROM TabularDataWithStrings
            WHERE TableName = 'End Uses'
              AND Value != '' AND Value != '0.0'
        """).fetchall():
            end_use, fuel, val = row[0], row[1], float(row[2])
            kpis["kpis"][f"{end_use.lower()}_{fuel.lower()}_gj"] = val

    out_path = out / f"{sample_id}_kpis.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(kpis, indent=2), encoding="utf-8")
    return out_path
```

For full worked examples covering data loading, filtering, aggregation,
and plotting, see the
[R to Python BYOS Cheatsheet](../user_scripts/examples/r_to_python_migration.md).

---

## 7. Monitoring and Dashboards

### What Replaces the Web Dashboard?

openstudio-server provided a real-time web dashboard showing simulation
progress, status, and results. OSimFlow provides two alternatives:

| Need | openstudio-server | OSimFlow |
|---|---|---|
| Campaign status | Web dashboard (port 8080) | `run.json` (text file) |
| Per-sample progress | WebSocket updates | `run.json` + per-sample logs |
| Result plots | In-browser charts | PNG/PDF files in `outdir/` |
| Real-time monitoring | Live web page | MLflow UI (optional) |
| Historical comparison | MongoDB queries | Compare `outdir/` directories |

### Using `run.json` for Campaign Monitoring

The `run.json` file is updated after each DAG step. Poll it from a
terminal:

```bash
# Watch campaign progress
watch -n 5 'cat results/run.json | python -m json.tool'

# Or use jq
watch -n 5 'jq ".summary" results/run.json'
```

See [runjson-guide.md](runjson-guide.md) for the full schema reference.

### Using MLflow for a Dashboard Experience

For a visual monitoring experience closer to openstudio-server's
dashboard, set up MLflow:

> Note: These commands assume OSimFlow is already installed. If installing from source, use `pip install -e ".[dev,aws,slurm,mlflow]"` instead.

```bash
pip install "osimflow[mlflow]"
```

# Terminal 1: Start MLflow UI
mlflow ui --port 5000

# Terminal 2: Run campaign with MLflow tracking
osimflow run \
  --mlflow_tracking_uri http://localhost:5000 \
  --input_variables variables.yml \
  --template_sim_package ./template_sim_package \
  --n_samples 50 \
  --outdir ./results
```

Open `http://localhost:5000` in your browser to see:
- Campaign parameters
- Aggregate metrics (mean EUI, failure rate)
- Artifact downloads (CSV, plots)

---

## 8. MLflow Dashboard Setup

This section provides a complete setup guide for using MLflow as a
replacement for the openstudio-server web dashboard.

### 8.1 Installation

> Note: This command assumes OSimFlow is already installed. If installing from source, use `pip install -e ".[dev,aws,slurm,mlflow]"` instead.

```bash
pip install "osimflow[mlflow]"
```

This installs the `mlflow` package. No other dependencies are required.

### 8.2 Starting the MLflow UI

```bash
# Local UI (default: http://localhost:5000)
mlflow ui

# Custom host/port
mlflow ui --host 0.0.0.0 --port 8080

# Persistent storage (survives restarts)
mlflow ui --backend-store-uri sqlite:///mlflow.db
```

### 8.3 Running a Campaign with MLflow

```bash
osimflow run \
  --executor local \
  --mlflow_tracking_uri http://localhost:5000 \
  --input_variables variables.yml \
  --template_sim_package ./template_sim_package \
  --n_samples 50 \
  --outdir ./results
```

When `--mlflow_tracking_uri` is set, the Campaign automatically:

1. **Creates an MLflow experiment** named `osimflow`.
2. **Logs parameters**: `n_samples`, `openstudio_version`, `executor`,
   algorithm name, and all variable definitions.
3. **Logs metrics**: mean EUI, failure rate, total wall-clock time.
4. **Logs artifacts**: `aggregated_results.csv`, `failed_simulations.csv`,
   and plot PNG files.

### 8.4 What You See in the MLflow UI

| MLflow Tab | What It Shows | openstudio-server Equivalent |
|---|---|---|
| **Parameters** | n_samples, openstudio_version, algorithm | Analysis settings page |
| **Metrics** | Mean EUI, failure rate, elapsed seconds | Dashboard summary cards |
| **Artifacts** | CSV files, PNG plots | Download links in web UI |
| **Runs list** | One run per `osimflow run` invocation | Analysis history |

### 8.5 Comparing Campaigns

MLflow excels at comparing multiple campaigns — a common workflow when
iterating on building designs:

```bash
# Campaign 1: Baseline
osimflow run \
  --mlflow_tracking_uri http://localhost:5000 \
  --input_variables variables_baseline.yml \
  --template_sim_package ./pkg_v1 \
  --n_samples 100 \
  --outdir ./results_baseline

# Campaign 2: Improved envelope
osimflow run \
  --mlflow_tracking_uri http://localhost:5000 \
  --input_variables variables_improved.yml \
  --template_sim_package ./pkg_v2 \
  --n_samples 100 \
  --outdir ./results_improved
```

In the MLflow UI, both runs appear under the `osimflow` experiment.
Click **Compare** to see side-by-side parameter and metric differences.

### 8.6 Remote MLflow Server (Production)

For teams sharing results, deploy MLflow to a shared server:

```bash
# On a shared server
mlflow server \
  --backend-store-uri postgresql://user:pass@db:5432/mlflow \
  --default-artifact-root s3://my-bucket/mlflow-artifacts \
  --host 0.0.0.0
```

Then point the campaign at the remote server:

```bash
osimflow run \
  --mlflow_tracking_uri http://mlflow.company.com:5000 \
  ...
```

### 8.7 MLflow vs openstudio-server Dashboard Comparison

| Feature | openstudio-server Dashboard | MLflow UI |
|---|---|---|
| Real-time per-sample status | Yes (WebSocket) | No (updates on step completion) |
| Parameter comparison | Limited | Rich (chart, table, parallel coords) |
| Metric history | Limited | Full metric charting |
| Artifact browsing | CSV download | Any file type |
| Multi-campaign comparison | Manual | Built-in "Compare" |
| Access control | Built-in | Via reverse proxy |
| Setup complexity | Docker Compose + MongoDB | `pip install "osimflow[mlflow]"` |

---

## 9. Common Migration Patterns

### Pattern 1: Single Study Migration

You have one PAT project with 20 parameters and want to run 100
simulations locally.

```bash
# 1. Export from PAT as .osa
# 2. Import
osimflow import-osa my_study.osa --output variables.yml

# 3. Copy seed model + measures into template_sim_package/
cp -r /path/to/pat/seed/* template_sim_package/

# 4. Run
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./template_sim_package \
  --n_samples 100 \
  --outdir ./results
```

### Pattern 2: Large-Scale Cloud Migration

You have a PAT project that previously ran on an EC2-hosted
openstudio-server and want to scale to 10,000 samples on AWS Batch.

```bash
# 1. Set up AWS Batch infrastructure (one-time)
cd infra/aws/terraform/
terraform init && terraform apply

# 2. Import OSA project
osimflow import-osa large_study.osa --output variables.yml

# 3. Mirror OpenStudio image to ECR (avoids Docker Hub rate limits)
infra/aws/scripts/sync-openstudio-to-ecr.sh 3.11.0 us-east-1

# 4. Run on AWS Batch
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --ecr-repository <account>.dkr.ecr.us-east-1.amazonaws.com/openstudio \
  --input_variables variables.yml \
  --template_sim_package ./template_sim_package \
  --n_samples 10000 \
  --outdir ./results_large
```

### Pattern 3: Multi-Objective Optimisation

You were using `nsga_nrel` in openstudio-server and want to switch to
NSGA-II in OSimFlow.

```bash
# Install optimisation extra (in addition to base OSimFlow install)
pip install "osimflow[optimization]"

# Run with NSGA-II
osimflow run \
  --algorithm nsga2 \
  --max-generations 20 \
  --executor slurm \
  --slurm-real \
  --slurm_partition long \
  --input_variables variables.yml \
  --template_sim_package ./template_sim_package \
  --n_samples 50 \
  --outdir ./results_nsga2
```

Pareto front results are written to `outdir/pareto/` per generation.

### Pattern 4: Custom Analysis (R → Python)

You had an R script that computed custom KPIs. Convert it to a Python
BYOS script:

```bash
# Write custom KPI extractor (see §6 for the full template)
cp user_scripts/examples/custom_kpi_eui.py user_scripts/my_kpis.py

# Edit to match your specific KPIs
$EDITOR user_scripts/my_kpis.py

# Run with custom extractor
osimflow run \
  --custom_kpi_extractor user_scripts/my_kpis.py \
  --input_variables variables.yml \
  --template_sim_package ./template_sim_package \
  --n_samples 100 \
  --outdir ./results
```

---

## 10. Troubleshooting Migration Issues

### "Variable X not found in model"

**Cause:** The OSA `measure_argument` reference doesn't match the actual
measure argument name in your `.osw`.

**Fix:** Open `workflow.osw` in a text editor and verify the measure
argument names match those in `variables.yml`. The `measure_argument`
field in `variables.yml` must be in `MeasureName.argument_name` format.

### "Missing `workflow.osw` in template_sim_package"

**Cause:** PAT's seed model structure doesn't have a `.osw` at the top
level.

**Fix:** Create or copy the `.osw` file:

```bash
# PAT typically generates the .osw; look in the project's measures/ dir
find /path/to/pat/project -name "*.osw" -exec cp {} template_sim_package/ \;
```

### Distribution not supported

**Cause:** OSA distributions like `beta` or `exponential` don't have
direct OSimFlow equivalents.

**Fix:** They are imported as `uniform` spanning the same min/max range.
Edit `variables.yml` manually if you need a more precise distribution.

### "MongoDB connection refused" (old habits)

**Cause:** You're trying to use MongoDB commands or the
openstudio-server API.

**Fix:** OSimFlow doesn't use MongoDB. All data is in the `outdir/`
directory. Use pandas or DuckDB to query results:

```python
import pandas as pd
df = pd.read_csv("results/aggregated_results.csv")
```

### Algorithm not available

**Cause:** Some algorithms require optional dependencies.

**Fix:**

```bash
# NSGA-II and PSO (in addition to base OSimFlow install)
pip install "osimflow[optimization]"

# Morris and FAST99 sensitivity analysis (in addition to base OSimFlow install)
pip install "osimflow[sensitivity]"
```

### Large OSA projects with many variables

**Cause:** Some PAT projects have hundreds of variables, many of which
are static (locked).

**Fix:** The `import-osa` command converts static variables with
`distribution: static`. Review the output `variables.yml` and remove
static entries that don't need sampling to keep the campaign focused.

---

## See Also

- [User Guide](user-guide.md) — Complete OSimFlow usage reference
- [variables.yml Schema](variables-schema.md) — Full variable and distribution reference
- [OSA Import Reference (user-guide §7.7)](user-guide.md#77-importing-from-openstudio-analysis-spreadsheet-osa)
- [R to Python BYOS Cheatsheet](../user_scripts/examples/r_to_python_migration.md) — Worked R→Python examples
- [Data Analysis Notebook](../examples/notebooks/legacy_migration_analysis.ipynb) — Jupyter cookbook for migrated data
- [MLflow Integration (user-guide §7.4)](user-guide.md#74-mlflow-integration) — MLflow setup details
- [Docker Onboarding](docker-onboarding.md) — Container setup for real simulations
- [AWS Batch Terraform Guide](aws-batch-terraform.md) — Cloud infrastructure setup
- [eplusout.sql Guide](eplusout-sql-guide.md) — Querying EnergyPlus SQL output
- [user_scripts/README.md](../user_scripts/README.md) — BYOS script templates
