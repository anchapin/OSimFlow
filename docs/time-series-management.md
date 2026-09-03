# Time-Series Data Management

> PRD §6 gotcha #8: "Hourly outputs for thousands of samples get huge fast."

This document explains how OSimFlow manages large EnergyPlus time-series
outputs, how to control aggregation granularity, and how to estimate
storage requirements for parametric campaigns.

---

## Jupyter Notebooks

A starter notebook for exploring campaign results with DuckDB is available at
[`notebooks/duckdb_parquet_analysis.ipynb`](../notebooks/duckdb_parquet_analysis.ipynb).
It includes ready-made queries for monthly energy by end-use, peak demand profiling,
EUI distribution histograms, failed-simulation summaries, and cross-sample comparison.
See [`notebooks/README.md`](../notebooks/README.md) for setup instructions.

---

## Why time-series data gets large

EnergyPlus produces hourly (or sub-hourly) time-series data in the
`eplusout.sql` SQLite output. Each variable at each timestep is stored as
a row in the `ReportData` table. The raw data size is:

```
size_bytes = N_samples × hours_per_year × N_variables × 8 bytes
```

### Worked examples

| Samples | Variables | Resolution | Raw size (hourly) | Monthly CSV |
|---------|-----------|------------|-------------------|-------------|
| 10      | 50        | Hourly     | 35 MB             | 0.5 MB      |
| 100     | 50        | Hourly     | 350 MB            | 5 MB        |
| 1,000   | 50        | Hourly     | 3.5 GB            | 50 MB       |
| 5,000   | 50        | Hourly     | 17.5 GB           | 250 MB      |
| 10,000  | 100       | Hourly     | 70 GB             | 1 GB        |

At 5,000 samples with 50 output variables, the hourly dataset is ~17.5 GB
— far too large for a single CSV. Aggregating to monthly reduces this to
~250 MB, a 70× reduction.

---

## Default aggregation strategy

OSimFlow uses **monthly** aggregation by default in the campaign output
CSV (`timeseries_aggregated.csv`). This gives per-variable, per-month
statistics (sum, average, min, max, count) across all samples.

| Resolution | Rows per sample (50 vars) | Use case |
|------------|--------------------------|----------|
| Hourly     | 438,000                  | Detailed analysis; use with `--archive_intermediates` |
| Daily      | 18,250                   | Daily profiles, peak-day analysis |
| **Monthly**| **600**                  | **Default.** Seasonal analysis, campaign comparison |
| Annual     | 50                       | Summary statistics only |

Raw hourly data is always preserved in the per-sample `eplusout.sql`
files. These files are only copied to the output directory when
`--archive_intermediates` is enabled.

---

## Controlling aggregation resolution

There is **no `--ts_resolution` flag on `osimflow run`**. The campaign DAG's
`AGGREGATE_RESULTS` step always aggregates at **monthly** resolution — that
is the work-layer default in `osimflow/work.py:aggregate_results`
(`ts_resolution="monthly"`), and no campaign-level override is exposed.

`--ts_resolution` **is** a real flag of the underlying
[`bin/aggregate_results.py`](../bin/aggregate_results.py) work script. To
get a different resolution, re-run the aggregator directly on a completed
campaign's per-sample outputs:

```bash
# Daily aggregation for peak-day analysis (re-aggregate existing outputs):
python bin/aggregate_results.py \
  --kpis results/work/kpis/kpi_*.json \
  --simulation_dirs results/work/sim/* \
  --out_csv results/aggregated_results.csv \
  --out_parquet results/aggregated_results.parquet \
  --out_failed results/failed_simulations.csv \
  --ts_resolution daily

# Annual — summary only
python bin/aggregate_results.py \
  --kpis results/work/kpis/kpi_*.json \
  --simulation_dirs results/work/sim/* \
  --out_csv results/aggregated_results.csv \
  --out_parquet results/aggregated_results.parquet \
  --out_failed results/failed_simulations.csv \
  --ts_resolution annual

# Hourly — only sensible when --archive_intermediates kept the .sql files
python bin/aggregate_results.py \
  --kpis results/work/kpis/kpi_*.json \
  --simulation_dirs results/work/sim/* \
  --out_csv results/aggregated_results.csv \
  --out_parquet results/aggregated_results.parquet \
  --out_failed results/failed_simulations.csv \
  --ts_resolution hourly
```

Choices: `hourly`, `daily`, `monthly` (default), `annual`. The aggregator
also accepts `--ts_outdir` to place `timeseries_aggregated.csv`/`.parquet`
somewhere other than the `--out_csv` directory, and `--baseline_sample_id`
/ `--samples_json` to reproduce the campaign's baseline-comparison and
parameter-merge columns.

> **Warning:** Using `hourly` resolution for large campaigns (>100 samples)
> produces very large CSV files. Prefer `monthly` or `daily` and query the
> per-sample `.sql` files directly for detailed analysis.

---

## Using `--archive_intermediates` for full data

When `--archive_intermediates` is enabled, the campaign copies the
per-sample `eplusout.sql` files to the output directory. You can then
query these files directly for any resolution:

```python
import sqlite3
import pandas as pd

sql_path = "results/work/sim/0001/eplusout.sql"
conn = sqlite3.connect(sql_path)

# Extract hourly zone temperatures for January
df = pd.read_sql_query("""
    SELECT
        ddd.Name,
        t.Month,
        t.Day,
        t.Hour,
        rd.Value
    FROM ReportData rd
    JOIN ReportDataDictionary ddd
        ON rd.DictionaryIndex = ddd.ReportDataDictionaryIndex
    JOIN Time t ON rd.TimeIndex = t.TimeIndex
    WHERE ddd.Name LIKE '%Zone Mean Air Temperature%'
      AND t.Month = 1
""", conn)
conn.close()
```

---

## Best practices for large campaigns

### 1. Control EnergyPlus output frequency at the source

Reduce the number of output variables in your `.osw` or `.idf`:

```ruby
# In an OpenStudio measure — only output what you need
model.getOutputVariables.each do |ov|
  ov.remove if !wanted_variables.include?(ov.variableName)
end
```

In the `.idf`:
```
Output:Variable, *, !- Key Value
  Zone Mean Air Temperature,  !- Variable Name
  Timestep;                   !- Reporting Frequency → use Monthly or Daily
```

### 2. Use monthly aggregation for campaigns > 100 samples

Monthly is the campaign default — no flag needed. To re-aggregate
existing outputs at another resolution, re-run `bin/aggregate_results.py`
with `--ts_resolution` (see "Controlling aggregation resolution" above).

### 3. Use Parquet for downstream analysis

The aggregator always writes **both** `aggregated_results.csv` and
`aggregated_results.parquet`. The Parquet files are ~50-70% smaller than
CSV and much faster to read with pandas/DuckDB:

```python
import pandas as pd

df = pd.read_parquet("results/timeseries_aggregated.parquet")
```

### 4. Query per-sample .sql for detailed analysis

When you need hourly data for specific samples, query the per-sample
`eplusout.sql` directly rather than concatenating all samples into a
giant CSV.

### 5. Partition large datasets with DuckDB/polars

For campaigns > 5,000 samples, consider partitioning the time-series
data by sample_id or month using Parquet partitioning:

```python
import pyarrow as pa
import pyarrow.parquet as pq

table = pa.Table.from_pandas(ts_df)
pq.write_to_dataset(
    table,
    root_path="results/ts_partitioned",
    partition_cols=["sample_id"],
)
```

---

## SQL query patterns

### List available time-series variables

```sql
SELECT DISTINCT Name, ReportingFrequency, IndexGroup
FROM ReportDataDictionary
ORDER BY IndexGroup, Name;
```

### Monthly energy consumption by end use

```sql
SELECT
    ddd.Name,
    t.Month,
    SUM(rd.Value) AS monthly_sum,
    AVG(rd.Value) AS monthly_avg
FROM ReportData rd
JOIN ReportDataDictionary ddd
    ON rd.DictionaryIndex = ddd.ReportDataDictionaryIndex
JOIN Time t ON rd.TimeIndex = t.TimeIndex
WHERE ddd.Name LIKE '%Electricity%Facility%'
GROUP BY ddd.Name, t.Month
ORDER BY t.Month;
```

### Peak demand by month

```sql
SELECT
    t.Month,
    MAX(rd.Value) AS peak_demand_w
FROM ReportData rd
JOIN ReportDataDictionary ddd
    ON rd.DictionaryIndex = ddd.ReportDataDictionaryIndex
JOIN Time t ON rd.TimeIndex = t.TimeIndex
WHERE ddd.Name LIKE '%Electric Demand%Facility%'
GROUP BY t.Month;
```

### Daily temperature profiles for a specific zone

```sql
SELECT
    t.Month,
    t.Day,
    AVG(rd.Value) AS avg_temp_c
FROM ReportData rd
JOIN ReportDataDictionary ddd
    ON rd.DictionaryIndex = ddd.ReportDataDictionaryIndex
JOIN Time t ON rd.TimeIndex = t.TimeIndex
WHERE ddd.Name = 'Zone Mean Air Temperature'
  AND ddd.IndexGroup LIKE '%THERMAL ZONE: OFFICE%'
GROUP BY t.Month, t.Day
ORDER BY t.Month, t.Day;
```

---

## Related references

- PRD §6 gotcha #8: Large time-series data
- PRD §3.1: Data aggregation and statistical analysis
- `bin/aggregate_results.py`: `TimeSeriesAggregator` class
- `AGENTS.md` §8 gotcha #8
