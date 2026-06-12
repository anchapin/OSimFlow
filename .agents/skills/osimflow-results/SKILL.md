# OSimFlow Results Analysis

Guides AI agents through analyzing, extending, and troubleshooting OSimFlow simulation results — KPI extraction, result aggregation, custom KPIs, SQL querying, and plot generation.

## Triggers

- "KPI", "key performance indicator", "extract KPIs"
- "EUI", "energy use intensity"
- "results", "simulation results", "aggregated results"
- "aggregated_results", "aggregated_results.csv"
- "failed_simulations", "failed_simulations.csv"
- "eplusout.sql", "eplusout.err", "EnergyPlus output"
- "extract kpis", "extract_kpis"
- "aggregate results", "aggregate_results"
- "generate plots", "generate_plots"
- "custom KPI", "BYOS kpi"
- "plot", "visualization", "matplotlib", "seaborn"

## Quick Reference

### Output File Structure

After a successful campaign, `${outdir}/` contains:

```
results/
├── run.json                              # Per-campaign monitoring trace
├── samples.json                          # LHS sample values
├── aggregated_results.csv                # All samples, all KPIs (primary output)
├── failed_simulations.csv                # Failed samples + first severe error
├── plots/                                # Generated plots (PNG/PDF)
│   ├── scatter_matrix.png
│   ├── parallel_coordinates.png
│   └── correlation_heatmap.png
└── work/
    └── sim/
        └── <sample_id>/
            ├── stdout.log                # Simulation stdout
            ├── stderr.log                # Simulation stderr
            ├── kpis.json                 # Per-sample extracted KPIs
            ├── modified_sim_package/     # The per-sample .osw/.osm
            └── eplusout.sql              # EnergyPlus SQL output (if archived)
```

### Key Files

| File | Purpose |
|---|---|
| `bin/extract_kpis.py` | Default KPI extraction from `eplusout.sql` |
| `bin/aggregate_results.py` | Aggregates per-sample KPIs into CSVs |
| `bin/generate_plots.py` | Matplotlib/seaborn plot generation |
| `osimflow/work.py` | `extract_kpis`, `aggregate_results`, `generate_plots` work functions |
| `osimflow/monitoring.py` | `SampleTrace` — per-sample result schema |

### Primary KPIs (Default Extraction)

| KPI | Unit | Source |
|---|---|---|
| **EUI** (Energy Use Intensity) | kWh/m²/yr | `eplusout.sql` — annual total energy / floor area |
| **Total Site Energy** | kWh | `eplusout.sql` — tabular report |
| **Total Source Energy** | kWh | `eplusout.sql` — tabular report |
| **Heating Energy** | kWh | `eplusout.sql` — end-use breakdown |
| **Cooling Energy** | kWh | `eplusout.sql` — end-use breakdown |

### Failed Simulation Output

`failed_simulations.csv` columns:
- `sample_id` — the failing sample identifier
- `error_summary` — the **first** "Severe Error" line from `eplusout.err`
- `step` — which DAG step failed

## Detailed Guide

### Understanding aggregated_results.csv

The primary output file. Each row is one sample; columns include:
- All parameter values from `samples.json`
- All extracted KPIs from `kpis.json`
- A `status` column (`success` or `failed`)
- A `sample_id` column

```python
import pandas as pd

df = pd.read_csv("results/aggregated_results.csv")

# Filter successful simulations
success = df[df["status"] == "success"]

# Find lowest EUI
best = success.loc[success["eui_kwh_m2_yr"].idxmin()]
print(f"Best EUI: {best['eui_kwh_m2_yr']:.2f} kWh/m²/yr")
print(f"  Insulation R-value: {best['InsulationRValue']:.1f}")
print(f"  Window SHGC: {best['WindowSHGC']:.2f}")
```

### Querying eplusout.sql Directly

EnergyPlus writes simulation output to a SQLite database. Key tables:

```sql
-- Annual energy summary
SELECT
    VariableName,
    VariableValue
FROM TabularDataWithStrings
WHERE TableName = 'End Uses'
  AND ReportName = 'AnnualBuildingUtilityPerformanceSummary';

-- Energy per area (EUI)
SELECT
    VariableValue
FROM TabularDataWithStrings
WHERE TableName = 'Site and Source Energy'
  AND RowName = 'Total Site Energy Per Total Building Area'
  AND ColumnName = 'Energy Per Total Building Area';

-- Monthly energy breakdown
SELECT
    RowName,
    VariableValue
FROM TabularDataWithStrings
WHERE TableName = 'Energy Per Area'
  AND ReportName = 'EnergyMeters';

-- Zone temperatures
SELECT
    ZoneName,
    VariableValue
FROM TabularDataWithStrings
WHERE TableName = 'Comfort and Setpoint Not Met Summary'
  AND ReportName = 'SystemSummary';
```

Python helper:

```python
import sqlite3
import pandas as pd

def query_eplusout_sql(sql_path: str, query: str) -> pd.DataFrame:
    """Run a SQL query against an EnergyPlus SQL output file."""
    conn = sqlite3.connect(sql_path)
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df
```

### Adding Custom KPIs via BYOS

Create a custom KPI extractor in `user_scripts/`:

```python
# user_scripts/my_kpis.py
import json
import sqlite3
from pathlib import Path


def extract_kpis(
    sample_dir: Path,
    sample_id: str,
) -> dict:
    """
    Extract custom KPIs from simulation output.

    Args:
        sample_dir: Path to the sample's work directory.
        sample_id: Unique sample identifier.

    Returns:
        Dict of KPI name → value. Values must be JSON-serializable.
    """
    sql_path = sample_dir / "eplusout.sql"
    kpis = {}

    if sql_path.exists():
        conn = sqlite3.connect(str(sql_path))

        # Custom KPI: Peak cooling load
        row = conn.execute(
            "SELECT VariableValue FROM TabularDataWithStrings "
            "WHERE TableName = 'Equipment Summary' "
            "AND RowName = 'Cooling' AND ColumnName = 'Maximum Load'"
        ).fetchone()
        if row:
            kpis["peak_cooling_load_w"] = float(row[0])

        # Custom KPI: Unmet hours
        row = conn.execute(
            "SELECT VariableValue FROM TabularDataWithStrings "
            "WHERE TableName = 'Comfort and Setpoint Not Met Summary' "
            "AND RowName LIKE '%Not Met%'"
        ).fetchone()
        if row:
            kpis["unmet_hours"] = float(row[0])

        conn.close()

    return kpis
```

Then run with:

```bash
osimflow run \
  --custom_kpi_extractor user_scripts/my_kpis.py \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./results
```

### Plot Generation

The default `bin/generate_plots.py` produces:

1. **Scatter matrix** — pairwise parameter vs KPI scatter plots
2. **Parallel coordinates** — all parameters and KPIs on parallel axes
3. **Correlation heatmap** — parameter-KPI correlation matrix

Plots are saved to `${outdir}/plots/` as PNG files.

To customize plots, create a BYOS plot generator and wire it through `osimflow/work.py:generate_plots`.

### Understanding kpis.json (Per-Sample)

Each sample produces a `kpis.json`:

```json
{
  "sample_id": "sample_001",
  "status": "success",
  "eui_kwh_m2_yr": 127.4,
  "total_site_energy_kwh": 45678.0,
  "heating_energy_kwh": 12345.0,
  "cooling_energy_kwh": 23456.0,
  "peak_cooling_load_w": 8500.0
}
```

Custom KPIs are merged into this same structure.

### Diagnosing Failed Simulations

1. Check `failed_simulations.csv` for the error summary:

   ```bash
   cat results/failed_simulations.csv
   ```

2. Inspect the sample's stderr log:

   ```bash
   cat results/work/sim/<sample_id>/stderr.log
   ```

3. If `--archive_intermediates` was set, check `eplusout.err`:

   ```bash
   grep "Severe" results/work/sim/<sample_id>/eplusout.err
   ```

4. The first "Severe Error" line is the most actionable. Subsequent severe errors are often downstream effects.

### Multi-Objective Results

For iterative algorithms (NSGA-II, PSO) with multi-objective optimization:

- Results are stored per-generation in `${outdir}/pareto/gen_N.json`
- The `ParetoFront` class in `osimflow/pareto.py` tracks non-dominated solutions
- Final Pareto front is in `${outdir}/pareto/final.json`

```python
import json
from pathlib import Path

pareto = json.loads(Path("results/pareto/final.json").read_text())
for solution in pareto["solutions"]:
    print(f"EUI: {solution['objectives']['eui']:.2f}, "
          f"Cost: {solution['objectives']['cost']:.2f}, "
          f"Params: {solution['parameters']}")
```

## Common Patterns

### Quick Results Summary

```bash
# Count successes vs failures
csvtool col 1 results/aggregated_results.csv | sort | uniq -c

# Find the best (lowest EUI) sample
head -1 results/aggregated_results.csv  # see column names
sort -t, -k<eui_col> -n results/aggregated_results.csv | head -5
```

### Python Analysis Script

```python
import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_csv("results/aggregated_results.csv")
success = df[df["status"] == "success"]

# EUI distribution
success["eui_kwh_m2_yr"].hist(bins=20)
plt.xlabel("EUI (kWh/m²/yr)")
plt.ylabel("Count")
plt.title("EUI Distribution Across Samples")
plt.savefig("eui_distribution.png")

# Parameter sensitivity (correlation with EUI)
param_cols = ["InsulationRValue", "WindowSHGC"]  # from variables.yml
correlations = success[param_cols + ["eui_kwh_m2_yr"]].corr()["eui_kwh_m2_yr"]
print(correlations.sort_values())
```

### Batch SQL Query Across All Samples

```python
import sqlite3
from pathlib import Path
import pandas as pd

def batch_query(query: str, results_dir: str = "results") -> pd.DataFrame:
    """Run the same SQL query across all sample eplusout.sql files."""
    results = []
    sim_dir = Path(results_dir) / "work" / "sim"

    for sample_path in sorted(sim_dir.iterdir()):
        sql_path = sample_path / "eplusout.sql"
        if not sql_path.exists():
            continue

        conn = sqlite3.connect(str(sql_path))
        df = pd.read_sql_query(query, conn)
        conn.close()

        df["sample_id"] = sample_path.name
        results.append(df)

    return pd.concat(results, ignore_index=True)
```

### Comparing Two Campaigns

```python
import pandas as pd

campaign_a = pd.read_csv("results_v1/aggregated_results.csv")
campaign_b = pd.read_csv("results_v2/aggregated_results.csv")

campaign_a["campaign"] = "v1"
campaign_b["campaign"] = "v2"

combined = pd.concat([campaign_a, campaign_b])

# Compare EUI distributions
for name, group in combined.groupby("campaign"):
    print(f"{name}: EUI mean={group['eui_kwh_m2_yr'].mean():.2f}, "
          f"std={group['eui_kwh_m2_yr'].std():.2f}")
```

## Gotchas

1. **`failed_simulations.csv` contains only the first severe error** — EnergyPlus often cascades errors. The first "Severe Error" is the root cause; subsequent ones are usually consequences. Use `grep -m 1 "  * Severe"` to find it.

2. **Large time-series data** — Hourly outputs for thousands of samples get huge. Default to daily/monthly aggregates in `aggregated_results.csv`. Keep hourly data only in per-sample `.sql` files behind `--archive_intermediates`.

3. **EUI units depend on the model** — The default KPI extractor reports in kWh/m²/yr, but the underlying EnergyPlus output may use different units (J, kBtu, etc.). Check the `TabularDataWithStrings` column headers for unit information.

4. **Per-sample `eplusout.sql` is not archived by default** — Use `--archive_intermediates` to keep these files. Without it, they are deleted after KPI extraction to save disk space.

5. **`aggregated_results.csv` column ordering** — Columns follow the order: `sample_id`, parameter columns (from `variables.yml`), KPI columns, `status`. When adding custom KPIs, they appear after the default KPIs.

6. **Custom KPI return values must be JSON-serializable** — The BYOS extractor must return a `dict[str, float | int | str | bool]`. Complex types (lists, nested dicts) will cause a serialization error.

7. **Plot generation requires all samples to complete** — `GENERATE_BASIC_PLOTS` runs only after `AGGREGATE_RESULTS` completes. If some samples fail, plots use only the successful subset.

8. **`run.json` is the monitoring source of truth** — For per-step timing and per-sample status, always check `run.json` first. It's more reliable than counting files in the output directory.
