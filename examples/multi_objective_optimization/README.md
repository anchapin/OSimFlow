# Multi-Objective Optimization Example

Demonstrates optimization with multiple competing objectives using NSGA-II algorithm.

## Files

| File | Description |
|---|---|
| `variables.yml` | Parameter definitions with objectives and constraints |
| `run_optimization.sh` | Bash script to run the optimization |
| `template_sim_package/` | Minimal template with model.osm and workflow.osw |
| `README.md` | This file |

## Quick Start

```bash
./run_optimization.sh
```

## About Multi-Objective Optimization

This example optimizes for:
- **Objective 1 (minimize):** Energy Use Intensity (EUI) - operating energy cost
- **Objective 2 (minimize):** Initial construction cost
- **Constraint:** Thermal comfort (heating/cooling setpoints within range)

## Algorithm

Uses **NSGA-II** (Non-dominated Sorting Genetic Algorithm II) via `pymoo`.

```bash
osimflow run \
  --executor slurm \
  --algorithm nsga2 \
  --input_variables variables.yml \
  --template_sim_package ./template_sim_package \
  --n_samples 100 \
  --max-generations 50 \
  --outdir ./optimization_results \
  --openstudio_version 3.11.0
```

## Expected Pareto Front

After optimization, the Pareto front (`outdir/pareto/`) shows optimal trade-offs between competing objectives:

```
outdir/pareto/
├── gen_000.json   # Generation 0 (initial random population)
├── gen_010.json   # Generation 10
├── gen_020.json   # Generation 20
├── gen_050.json   # Final Pareto front
└── pareto_front.png  # Visualization
```

## Parameters

| Parameter | Range | Unit | Description |
|---|---|---|---|
| wall_r_value | 2.0 - 5.0 | m²K/W | Wall insulation |
| window_u_value | 1.0 - 3.0 | W/m²K | Window U-value |
| window_wwr | 0.2 - 0.6 | - | Window-to-wall ratio |
| heating_setpoint | 18.0 - 22.0 | °C | Indoor heating setpoint |
| cooling_setpoint | 24.0 - 28.0 | °C | Indoor cooling setpoint |

## Output Files

| File | Description |
|---|---|
| `run.json` | Campaign metadata and timing |
| `aggregated_results.csv` | All samples with objectives and constraints |
| `pareto/gen_*.json` | Pareto front at each generation |
| `plots/pareto_front.png` | Pareto front visualization |