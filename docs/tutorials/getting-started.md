# Getting Started with OSimFlow
<!-- docs-skip -->

A practical guide to getting OSimFlow installed and running your first parametric building-energy simulation campaign.

**Time:** ~30 minutes  
**Audience:** New to OSimFlow, familiar with OpenStudio

---

## Table of Contents

1. [Installation](#1-installation)
2. [Understanding Campaign Inputs](#2-understanding-campaign-inputs)
3. [Your First Campaign](#3-your-first-campaign)
4. [Understanding the Output](#4-understanding-the-output)
5. [Common Use Cases](#5-common-use-cases)
6. [Next Steps](#6-next-steps)

---

## 1. Installation

### 1.1 System Requirements

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.12+ | Required |
| pip | Latest | For package installation |
| Docker | Optional | For real OpenStudio simulations |
| git | 2.x | For version control |

### 1.2 Standard Installation

```bash
# Install from source (recommended for development)
git clone https://github.com/anchapin/OSimFlow.git
cd OSimFlow
pip install -e ".[dev,aws,slurm]"

# Or install from PyPI (when available)
pip install osimflow
```

### 1.3 Verify Installation

```bash
osimflow --help
```

You should see:

```
usage: osimflow [-h] [--version] {run,serve,import-osa,export,list,show,compare,status,download} ...
```

### 1.4 Optional: Docker Setup for Real Simulations

To run actual OpenStudio simulations instead of stub mode:

```bash
# Install Docker Desktop
# Pull the OpenStudio container image
docker pull nrel/openstudio:3.11.0

# Verify the image works
docker run --rm nrel/openstudio:3.11.0 openstudio.cli --version
```

> **No Docker?** OSimFlow automatically falls back to stub mode when OpenStudio CLI is unavailable. All outputs are generated but use placeholder values. This is perfect for learning the workflow or testing your campaign structure.

---

## 2. Understanding Campaign Inputs

Every OSimFlow campaign requires two inputs:

### 2.1 The Template Simulation Package

A directory containing your base OpenStudio model and workflow:

```
my_project/
├── model.osm              # Your OpenStudio model file
├── workflow.osw           # OpenStudio workflow definition
└── measures/              # Optional: custom measures
    ├── SetThermostat/
    └── SetWindowProperties/
```

**Minimum contents:**
- `model.osm` - OpenStudio model file
- `workflow.osw` - Workflow file defining measure steps

### 2.2 The Variables File (variables.yml)

Defines which parameters vary and their probability distributions:

```yaml
variables:
  - name: heating_setpoint
    distribution: uniform
    min: 18.0
    max: 22.0

  - name: wall_r_value
    distribution: uniform
    min: 2.0
    max: 5.0

  - name: window_wwr
    distribution: uniform
    min: 0.2
    max: 0.6
```

**Supported distributions:**
- `uniform` - Equal probability between min and max
- `normal` - Gaussian distribution with mean and std
- `lognormal` - Log-normal distribution
- `triangular` - Triangular distribution with min, mode, max
- `discrete` - Specific values only
- `categorical` - Named categories

---

## 3. Your First Campaign

### 3.1 Quick Smoke Test (No OpenStudio Needed)

```bash
# Create a test directory
mkdir -p ~/osimflow_tutorial
cd ~/osimflow_tutorial

# Run a minimal 3-sample campaign
osimflow run \
  --executor local \
  --input_variables path/to/variables.yml \
  --template_sim_package path/to/template_sim_package \
  --n_samples 3 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

This runs in stub mode if OpenStudio CLI is not available.

### 3.2 A Complete Walkthrough

#### Step 1: Create Your Variables File

Create `variables.yml`:

```yaml
variables:
  - name: heating_setpoint
    distribution: uniform
    min: 18.0
    max: 22.0
    description: "Indoor heating setpoint in Celsius"

  - name: cooling_setpoint
    distribution: uniform
    min: 24.0
    max: 28.0
    description: "Indoor cooling setpoint in Celsius"

  - name: wall_r_value
    distribution: uniform
    min: 2.0
    max: 5.0
    description: "Wall thermal resistance in m²K/W"
```

#### Step 2: Prepare Your Template Package

Copy your OpenStudio model and workflow to a directory:

```bash
mkdir -p my_template
cp /path/to/model.osm my_template/
cp /path/to/workflow.osw my_template/
```

#### Step 3: Run the Campaign

```bash
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./my_template \
  --n_samples 50 \
  --outdir ./campaign_results \
  --openstudio_version 3.11.0 \
  --max-workers 4
```

#### Step 4: Monitor Progress

OSimFlow writes real-time status to `campaign_results/run.json`:

```bash
# Watch progress
watch -n 5 'cat campaign_results/run.json | python -m json.tool'
```

---

## 4. Understanding the Output

After a successful campaign, your `outdir` contains:

```
campaign_results/
├── run.json                    # Campaign metadata and timing
├── samples.json                # LHS parameter sets
├── aggregated_results.csv      # All KPI results
├── failed_simulations.csv      # Failed runs with error summaries
├── kpis/                       # Per-sample KPI JSON files
│   ├── sample_0001.json
│   ├── sample_0002.json
│   └── ...
└── plots/                      # Generated visualizations
    ├── eui_histogram.png
    └── scatter_wall_r_value_vs_eui.png
```

### 4.1 The run.json File

Contains campaign-level metadata:

```json
{
  "campaign_id": "...",
  "n_samples": 50,
  "status": "completed",
  "steps": {
    "generate_lhs_samples": { "duration_seconds": 0.12 },
    "apply_parameters": { "duration_seconds": 1.45, "cache_hit": false },
    "run_openstudio_sim": { "duration_seconds": 847.2, "cache_hit": 23 },
    "extract_kpis": { "duration_seconds": 12.3 },
    "aggregate_results": { "duration_seconds": 0.8 }
  },
  "samples": [...]
}
```

### 4.2 The aggregated_results.csv

Contains one row per sample with all KPIs:

```csv
sample_id,eui_kwh_m2,total_heating_kwh,total_cooling_kwh,wall_r_value,heating_setpoint
sample_0001,145.2,12000,8500,3.5,20.0
sample_0002,138.7,11200,9100,4.2,19.5
...
```

### 4.3 The failed_simulations.csv

Lists any failed simulations with error summaries:

```csv
sample_id,error_summary,exit_code
sample_0023, Severe Error: No solution found for HVAC system,1
sample_0041, Severe Error: Zone floor area mismatch,1
```

---

## 5. Common Use Cases

### 5.1 Sensitivity Analysis

Explore how building parameters affect energy consumption:

```bash
osimflow run \
  --executor local \
  --input_variables sensitivity_variables.yml \
  --template_sim_package ./template \
  --n_samples 100 \
  --outdir ./sensitivity_results \
  --openstudio_version 3.11.0
```

### 5.2 Optimization Study

Find optimal building parameters:

```bash
osimflow run \
  --executor slurm \
  --input_variables optimization_variables.yml \
  --template_sim_package ./template \
  --n_samples 200 \
  --outdir ./optimization_results \
  --openstudio_version 3.11.0 \
  --algorithm de \
  --max-generations 50
```

### 5.3 Monte Carlo Uncertainty Analysis

Quantify uncertainty in predictions:

```bash
osimflow run \
  --executor aws_batch \
  --input_variables uncertainty_variables.yml \
  --template_sim_package ./template \
  --n_samples 1000 \
  --outdir ./uncertainty_results \
  --openstudio_version 3.11.0
```

---

## 6. Next Steps

- **[Your First Real Campaign](your-first-campaign.md)** - Deeper walkthrough with real OpenStudio
- **[Advanced Topics](advanced-topics.md)** - Custom algorithms, BYOS, multi-environment campaigns
- **[Migration from OpenStudio-Server](migration-from-oss.md)** - Guide for OSS users
- **[User Guide](../user-guide.md)** - Complete command reference

---

## Troubleshooting

### "openstudio.cli: command not found"

OSimFlow is running in stub mode. Install Docker and pull the OpenStudio image, or continue without it for testing.

### "No such file or directory: model.osm"

Your template_sim_package is missing required files. Ensure it contains `model.osm` and `workflow.osw`.

### "Cache is stale" warnings

This is normal after editing variables.yml or changing algorithm parameters. Use `--force` to bypass cache if needed.