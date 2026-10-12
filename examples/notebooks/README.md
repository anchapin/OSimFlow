# examples/notebooks/ — Analysis Notebooks

Jupyter notebooks for post-campaign analysis and migration workflows.

## Notebooks

| Notebook | Description |
|---|---|
| `legacy_migration_analysis.ipynb` | Cookbook for migrating from openstudio-server MongoDB queries to pandas/DuckDB analysis. Includes parallel coordinate plots, scatter plots, and boilerplate snippets. |
| `duckdb_parquet_analysis.ipynb` | General DuckDB + Parquet analysis patterns for OSimFlow results. |

## Quick start

```bash
pip install jupyter pandas duckdb matplotlib seaborn
jupyter notebook examples/notebooks/
```

## See also

- [Migration Guide](../../docs/migration/openstudio-server.md)
- [R to Python BYOS Cheatsheet](../../user_scripts/examples/r_to_python_migration.md)

## Pointing `duckdb_parquet_analysis.ipynb` at campaign output

Set `OUTDIR` in the first code cell to your campaign output folder
(default `../../results`). The notebook reads `aggregated_results.parquet`,
`failed_simulations.parquet` and `timeseries_aggregated.parquet` from `outdir/`,
falling back to `.csv` when Parquet is absent. See
[docs/time-series-management.md](../../docs/time-series-management.md).
