# PAT Project Import Example

Demonstrates importing a Project import from OpenStudio PAT (OpenStudio-Server Analysis GUI) into OSimFlow format.

## Files

| File | Description |
|---|---|
| `analysis.json` | Example PAT analysis.json export |
| `convert_to_variables.py` | Python script to convert PAT analysis to OSimFlow variables.yml |
| `README.md` | This file |

## Quick Start

### Step 1: Export from PAT

In OpenStudio PAT:
1. Create or open your analysis
2. Export as `analysis.json` (or download from OSS server)

### Step 2: Convert to OSimFlow Format

```bash
python convert_to_variables.py path/to/analysis.json
```

This generates `variables.yml` in OSimFlow format.

### Step 3: Run Campaign

```bash
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./my_template \
  --n_samples 100 \
  --outdir ./campaign_results \
  --openstudio_version 3.11.0
```

## The Conversion Script

The `convert_to_variables.py` script handles:

| PAT analysis.json | OSimFlow variables.yml |
|---|---|
| `variables[].name` | `variables[].name` |
| `variables[].minimum` | `variables[].min` |
| `variables[].maximum` | `variables[].max` |
| `variables[].mean` | `variables[].mean` |
| `variables[].std` | `variables[].std` |
| `variables[].attributes` | Map to distributions |
| `analysis_type` | Map to `--algorithm` |

## Example PAT analysis.json

```json
{
  "analysis_type": "Single Zone EnergyPlus",
  "problem": {
    "objective_functions": [
      {
        "name": "eui",
        "objective": "minimize"
      }
    ],
    "design_variables": [
      {
        "name": "heating_setpoint",
        "display_name": "Heating Setpoint",
        "units": "C",
        "minimum": 18.0,
        "maximum": 22.0,
        "attributes": [
          {"name": "variable_type", "value": "double"},
          {"name": "distribution", "value": "uniform"}
        ]
      }
    ]
  }
}
```

## Supported Analysis Types

| PAT Analysis Type | OSimFlow Algorithm |
|---|---|
| `Single Zone EnergyPlus` | `lhs` |
| `DOE` | `lhs` |
| `Sequential Search` | `lhs` |
| `Morris` | `morris` |
| `FAST99` | `fast99` |
| `SOBOL` | `sobol` |
| `Dakota` | (use custom BYOS) |

## Notes

- **Measures**: PAT measures must be converted to OSimFlow BYOS scripts
- **Seed Model**: Extract from PAT export's `seed/` directory
- **Weather File**: Extract from PAT export's `weather/` directory