# examples/notebooks/ — Analysis Notebooks

Jupyter notebooks for post-campaign analysis and migration workflows.

## Notebooks

| Notebook | Description |
|---|---|
| `legacy_migration_analysis.ipynb` | Cookbook for migrating from openstudio-server MongoDB queries to pandas/DuckDB analysis. Includes parallel coordinate plots, scatter plots, and boilerplate snippets. |
| `../../notebooks/duckdb_parquet_analysis.ipynb` | General DuckDB + Parquet analysis patterns for OSimFlow results. |

## Quick start

```bash
pip install jupyter pandas duckdb matplotlib seaborn
jupyter notebook examples/notebooks/
```

## See also

- [Migration Guide](../../docs/migration-openstudio-server.md)
- [R to Python BYOS Cheatsheet](../../user_scripts/examples/r_to_python_migration.md)
