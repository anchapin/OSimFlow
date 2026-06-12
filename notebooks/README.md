# Jupyter Notebooks for OSimFlow Output Analysis

Starter notebooks for querying OSimFlow campaign results with DuckDB and pandas.

## Setup

```bash
pip install duckdb pandas jupyter matplotlib
```

## Launch

```bash
cd notebooks/
jupyter notebook duckdb_parquet_analysis.ipynb
```

## Pointing at campaign output

By default the notebook looks for data relative to the `notebooks/` directory.
Set `OUTDIR` in the first code cell to point at your campaign output folder:

```python
OUTDIR = "../results"  # adjust to your campaign output directory
```

The key files the notebook reads:

| File | Location | Contents |
|------|----------|----------|
| `aggregated_results.parquet` | `outdir/` | Per-sample KPIs (EUI, energy by end-use, status) |
| `failed_simulations.parquet` | `outdir/` | Failed samples with error summaries |
| `timeseries_aggregated.parquet` | `outdir/` | Monthly/daily aggregated time-series |

If your campaign produced CSV instead of Parquet, the notebook includes a
fallback that reads `.csv` files — just adjust the file extension in the path.

## Deeper reference

See [docs/time-series-management.md](../docs/time-series-management.md) for the
full guide on controlling aggregation granularity, storage estimation, and
direct SQL queries against per-sample `eplusout.sql` files.
