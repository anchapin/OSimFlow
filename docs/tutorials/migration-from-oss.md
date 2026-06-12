# Migration Guide: OpenStudio-Server to OSimFlow

Step-by-step guide for migrating your parametric building-energy simulation workflows from OpenStudio-Server (OSS) to OSimFlow.

**Audience:** OpenStudio-Server users familiar with PAT (OpenStudio-Server Analysis GUI) and Ruby-based workflows.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Concept Mapping](#2-concept-mapping)
3. [Prerequisites](#3-prerequisites)
4. [Step-by-Step Migration](#4-step-by-step-migration)
5. [Variables File Conversion](#5-variables-file-conversion)
6. [Measure Translation](#6-measure-translation)
7. [Troubleshooting](#7-troubleshooting)
8. [Reference](#8-reference)

---

## 1. Overview

### 1.1 Why Migrate to OSimFlow?

| Benefit | Description |
|---|---|
| **Python-Native** | Built for Python users; no Ruby required |
| **Flexible Execution** | Local, Slurm, AWS Batch, Nomad from one CLI |
| **Modern Sampling** | LHS, Sobol, Halton, Morris, FAST99, DE, DA, NSGA-II, PSO |
| **Reproducibility** | Containerized OpenStudio versions, code-hashed cache |
| **Community-Driven** | Open-source, extensible, transparent |

### 1.2 What You Can Migrate

OSimFlow can replace:

- PAT (OpenStudio-Server Analysis GUI) workflows
- Ruby-based analysis scripts
- MongoDB-based result storage
- Custom Bash/Python orchestration scripts

---

## 2. Concept Mapping

### 2.1 OpenStudio-Server → OSimFlow Terminology

| OpenStudio-Server (OSS) | OSimFlow | Notes |
|---|---|---|
| PAT Project | Campaign | Parametric study definition |
| Analysis JSON | `variables.yml` | Parameter definitions |
| Seed Model | `template_sim_package/model.osm` | Base building model |
| Measures | Measures | Ruby/Python measure scripts |
| Server | Executor | Local/Slurm/AWS Batch/Nomad |
| Worker | Submitit/Dask | Job scheduling backend |
| Results Database | `aggregated_results.csv` + Parquet | Tabular results |
| Datapoints | Samples | Individual simulation runs |
| AWS Auto-Scaling | AWS Batch | Cloud compute |
| On-Prem HPC | Slurm/Nomad | Cluster compute |

### 2.2 File Format Changes

| OSS Format | OSimFlow Format | Conversion |
|---|---|---|
| `analysis.json` | `variables.yml` | See §5 |
| Ruby scripts | Python scripts | See §6 |
| `.osw` (pre-modified) | `.osw` + parameters | Dynamic modification |
| MongoDB | SQLite + CSV/Parquet | Automatic |
| Log files | `run.json` + per-sample stdout/stderr | Automatic |

---

## 3. Prerequisites

### 3.1 Software Requirements

```bash
# Python 3.12+
python --version  # Must be 3.12 or higher

# OSimFlow
pip install -e ".[dev,aws,slurm]"

# Optional: Docker for OpenStudio container
docker pull nrel/openstudio:3.11.0
```

### 3.2 Knowledge Requirements

- Basic Python 3.12+ syntax
- YAML file editing
- Command-line terminal usage
- OpenStudio measures (optional, for advanced use)

### 3.3 Required Files from OSS Project

Before migrating, gather from your OSS setup:

1. **Seed Model** - Your `.osm` file
2. **Workflow** - Your `.osw` file
3. **Measures** - Custom Ruby/Python measures
4. **Analysis Definition** - The parameters you varied
5. **Weather File** - `.epw` file used

---

## 4. Step-by-Step Migration

### Step 1: Export Your PAT Project

In PAT (OpenStudio-Server Analysis GUI):

1. Export the analysis as a ZIP file
2. Extract to a local directory
3. Note the structure:

```
my_pat_project/
├── analysis.json      # Your analysis definition
├── seed/              # Seed model and measures
│   ├── model.osm
│   ├── workflow.osw
│   └── measures/
└── weather/           # Weather files
    └── in.epw
```

### Step 2: Convert analysis.json to variables.yml

The `analysis.json` from OSS defines variables like:

```json
{
  "variables": [
    {
      "name": "heating_setpoint",
      "display_name": "Heating Setpoint",
      "units": "C",
      "type": "Variable",
      "minimum": 18.0,
      "maximum": 22.0,
      "attributes": [
        { "name": "variable_type", "value": "double" },
        { "name": "distribution", "value": "uniform" }
      ]
    }
  ]
}
```

Convert to OSimFlow's `variables.yml`:

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
```

See §5 for full conversion reference.

### Step 3: Prepare Template Simulation Package

Create your `template_sim_package`:

```bash
mkdir -p my_template
cp path/to/model.osm my_template/
cp path/to/workflow.osw my_template/
cp -r path/to/measures my_template/
cp path/to/in.epw my_template/
```

Verify structure:

```
my_template/
├── model.osm
├── workflow.osw
├── measures/
│   ├── SetThermostat/
│   └── SetWindowProperties/
└── in.epw
```

### Step 4: Test with Local Executor (Stub Mode)

First, test without real simulations:

```bash
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./my_template \
  --n_samples 3 \
  --outdir ./test_results \
  --openstudio_version 3.11.0
```

This runs in stub mode (fast, no real OpenStudio) to verify your configuration.

### Step 5: Run Full Campaign

Once stub mode works:

```bash
# Local with real OpenStudio
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./my_template \
  --n_samples 50 \
  --outdir ./campaign_results \
  --openstudio_version 3.11.0 \
  --max-workers 4

# Or on HPC cluster
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition short \
  --input_variables variables.yml \
  --template_sim_package ./my_template \
  --n_samples 500 \
  --outdir ./campaign_results \
  --openstudio_version 3.11.0
```

### Step 6: Migrate Custom Ruby Measures to Python (Optional)

If you have custom Ruby measures, see §6 for translation guidance.

---

## 5. Variables File Conversion

### 5.1 Distribution Mapping

| OSS Distribution | OSimFlow Distribution | variables.yml Syntax |
|---|---|---|
| uniform | uniform | `distribution: uniform, min: X, max: Y` |
| normal | normal | `distribution: normal, mean: X, std: Y` |
| lognormal | lognormal | `distribution: lognormal, mean: X, std: Y` |
| triangular | triangular | `distribution: triangular, min: X, mode: Y, max: Z` |
| discrete | discrete | `distribution: discrete, values: [X, Y, Z]` |

### 5.2 Complete Variables.yml Example

```yaml
# variables.yml - OSimFlow format
variables:
  # Continuous uniform variable
  - name: heating_setpoint
    distribution: uniform
    min: 18.0
    max: 22.0
    description: "Indoor heating setpoint"

  # Continuous normal variable
  - name: internal_gains
    distribution: normal
    mean: 100.0
    std: 10.0
    description: "Internal gains from occupants and equipment"

  # Lognormal distribution
  - name: window_u_value
    distribution: lognormal
    mean: 1.8
    std: 0.2
    description: "Window U-value"

  # Triangular distribution
  - name: infiltration_rate
    distribution: triangular
    min: 0.1
    mode: 0.3
    max: 0.5
    description: "Air infiltration rate"

  # Discrete values
  - name: hvac_type
    distribution: discrete
    values:
      - VAV
      - CAV
      - HeatPump
    description: "HVAC system type"

  # Categorical
  - name: window_type
    distribution: categorical
    categories:
      - SinglePane
      - DoublePane
      - TriplePane
    description: "Window glazing type"

# Baseline comparison (optional)
baseline:
  sample_id: baseline_90.1
  parameters:
    heating_setpoint: 20.0
    cooling_setpoint: 24.0
    wall_r_value: 3.5

# Objective function (for optimization)
objective:
  name: eui
  direction: minimize
  weight: 1.0

# Constraints (for optimization)
constraints:
  - name: total_cost
    max: 500000
    min: 100000
```

---

## 6. Measure Translation

### 6.1 Ruby to Python Measure Translation

**Ruby (OSS):**

```ruby
# measures/SetThermostat/measure.rb
class SetThermostat < OpenStudio::Measure::ModelMeasure
  def run(model, runner)
    heating_setpoint = runner.getDouble('heating_setpoint')
    cooling_setpoint = runner.getDouble('cooling_setpoint')

    # Apply to model
    thermostats = model.getThermalZones.collect do |zone|
      zone.thermostatSetpointDualSetpoints
    end.compact

    thermostats.each do |thermostat|
      thermostat.setHeatingSetpointTemperatureSchedule(
        scheduleForHeating(heating_setpoint)
      )
      thermostat.setCoolingSetpointTemperatureSchedule(
        scheduleForCooling(cooling_setpoint)
      )
    end

    runner.registerInfo("Set heating to #{heating_setpoint}C")
    true
  end
end
```

**Python (OSimFlow BYOS):**

```python
# my_measures/set_thermostat.py
from pathlib import Path
import openstudio

def apply_thermostat(model_path: Path, parameters: dict, out_dir: Path) -> bool:
    """Apply thermostat settings to the model."""
    model = openstudio.model.Model.load(model_path.string()).get()

    heating_sp = parameters.get('heating_setpoint', 20.0)
    cooling_sp = parameters.get('cooling_setpoint', 25.0)

    # Find thermostats
    for zone in model.getThermalZones():
        thermostat = zone.thermostatSetpointDualSetpoints()
        if thermostat.is_initialized():
            thermostat = thermostat.get()
            # Apply schedules (simplified)
            ...

    # Save modified model
    model.save(out_dir / 'modified_model.osm', True)
    return True
```

### 6.2 Measure Directory Structure

OSS measures:
```
measures/
└── SetThermostat/
    ├── measure.rb
    └── measure.xml
```

OSimFlow BYOS:
```
my_measures/
├── set_thermostat.py      # Single function file
└── set_window_props.py
```

---

## 7. Troubleshooting

### 7.1 Common Issues and Solutions

| Issue | Cause | Solution |
|---|---|---|
| "openstudio.cli: command not found" | Docker not running | Start Docker or continue in stub mode |
| "No such file: workflow.osw" | Template package missing file | Ensure template_sim_package contains workflow.osw |
| "Cache stale" warnings | Files changed | Run with `--force` to bypass cache |
| "Slurm partition not found" | Wrong partition name | Check `sinfo` on cluster for valid partitions |
| "AWS Batch job failed" | Spot price exceeded | Increase `--aws-batch-max-spot-price-usd` |
| "Variables validation failed" | YAML syntax error | Validate YAML at yamllint.com |

### 7.2 Debug Mode

Enable verbose logging:

```bash
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 3 \
  --outdir ./debug_results \
  --log_level DEBUG
```

### 7.3 Inspect Failed Samples

Check per-sample logs:

```bash
# List failed samples
grep -l "failed" results/run.json | xargs cat

# View specific sample output
cat results/work/sim/sample_0023/stdout.log
cat results/work/sim/sample_0023/stderr.log
```

---

## 8. Reference

### 8.1 CLI Flag Mapping

| PAT/OSS Flag | OSimFlow Flag |
|---|---|
| `--analysis` | `--input_variables` |
| `--seed` | `--template_sim_package` |
| `--server` | `--executor` |
| `--workers` | `--max-workers` |
| `--max-hpc-wait-time` | `--slurm-timeout` |

### 8.2 Output Files Comparison

| OSS Output | OSimFlow Output |
|---|---|
| MongoDB database | `aggregated_results.csv` + `kpis/*.json` |
| Datapoint JSON files | Per-sample directories in `work/sim/` |
| `failed_datapoints.json` | `failed_simulations.csv` |
| Server log | `run.json` |

### 8.3 Additional Resources

- **[Your First Campaign](your-first-campaign.md)** - Hands-on tutorial
- **[Advanced Topics](advanced-topics.md)** - BYOS, custom algorithms
- **[Variables Schema](../variables-schema.md)** - Complete variable syntax
- **[Migration from openstudio-server](../migration-openstudio-server.md)** - Technical deep dive