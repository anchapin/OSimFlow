# Simple LHS Campaign Example

A minimal example demonstrating a Latin Hypercube Sampling campaign with OSimFlow.

## Files

| File | Description |
|---|---|
| `variables.yml` | Parameter definitions for LHS sampling |
| `run_campaign.sh` | Bash script to run the campaign |
| `template_sim_package/` | Minimal template with model.osm and workflow.osw |
| `README.md` | This file |

## Quick Start

```bash
# Run the campaign
./run_campaign.sh

# Or run manually
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./template_sim_package \
  --n_samples 20 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

## Expected Output

After running, `results/` contains:

```
results/
├── run.json                  # Campaign metadata
├── samples.json              # LHS parameter sets
├── aggregated_results.csv    # All KPI results
├── failed_simulations.csv    # Failed runs (if any)
├── kpis/                     # Per-sample KPI JSONs
└── plots/                    # Summary plots
```

## Parameters

This example varies:

- `heating_setpoint` - Indoor heating setpoint (18-22°C)
- `cooling_setpoint` - Indoor cooling setpoint (24-28°C)
- `wall_r_value` - Wall thermal resistance (2.0-5.0 m²K/W)