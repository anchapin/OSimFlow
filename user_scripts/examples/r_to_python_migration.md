# R to Python BYOS Migration Cheatsheet

> Practical guide for converting openstudio-server R-based analysis
> scripts to OSimFlow Python BYOS scripts. Covers data loading,
> filtering, aggregation, and plotting with side-by-side comparisons.

## Table of Contents

- [1. Overview](#1-overview)
- [2. Data Loading](#2-data-loading)
- [3. Filtering and Subsetting](#3-filtering-and-subsetting)
- [4. Aggregation and Grouping](#4-aggregation-and-grouping)
- [5. Joining and Merging](#5-joining-and-merging)
- [6. Custom KPI Extraction](#6-custom-kpi-extraction)
- [7. Custom Aggregation](#7-custom-aggregation)
- [8. Plotting](#8-plotting)
- [9. Complete Example: Reporting Script](#9-complete-example-reporting-script)
- [10. Quick Reference Table](#10-quick-reference-table)

---

## 1. Overview

In openstudio-server, custom analysis was done through R scripts that
queried MongoDB for simulation results. In OSimFlow, the equivalent
is Python BYOS (Bring Your Own Script) that reads from file-based
output (CSV, Parquet, SQLite).

| Concept | openstudio-server (R) | OSimFlow (Python) |
|---|---|---|
| Data source | MongoDB via `mongolite` | CSV/Parquet via `pandas` / `duckdb` |
| KPI extraction | R script reading `eplusout.sql` | Python BYOS `extract_kpis()` function |
| Result aggregation | R post-processing | Built-in or custom `aggregate_results.py` |
| Plotting | `ggplot2` | `matplotlib` / `seaborn` |

---

## 2. Data Loading

### R (openstudio-server)

```r
library(mongolite)

# Connect to MongoDB
m <- mongo("data_points", url = "mongodb://localhost:27017/osh")

# Load all completed data points
data <- m$find('{"status.state": "completed"}')

# Or from a CSV export
data <- read.csv("export.csv", stringsAsFactors = FALSE)
```

### Python (OSimFlow)

```python
import pandas as pd

# From CSV (default output format)
df = pd.read_csv("results/aggregated_results.csv")

# From Parquet (large campaigns, faster)
df = pd.read_parquet("results/aggregated_results.parquet")

# With DuckDB (SQL on Parquet, no memory limit)
import duckdb
con = duckdb.connect()
df = con.execute("""
    SELECT * FROM read_parquet('results/aggregated_results.parquet')
    WHERE status = 'completed'
""").fetchdf()
```

---

## 3. Filtering and Subsetting

### R

```r
# Filter by EUI range
high_eui <- subset(data, results.eui > 200)

# Filter by parameter value
cold_case <- subset(data, measure_attributes.climate_zone == "6A")

# Multiple conditions
filtered <- subset(data,
  results.eui > 100 & results.eui < 300 &
  measure_attributes.insul_r > 10
)

# Select specific columns
selected <- data[, c("name", "results.eui", "measure_attributes.insul_r")]
```

### Python

```python
# Filter by EUI range
high_eui = df[df["eui_kwh_m2_yr"] > 200]

# Filter by parameter value
cold_case = df[df["climate_zone"] == "6A"]

# Multiple conditions
filtered = df[
    (df["eui_kwh_m2_yr"] > 100) &
    (df["eui_kwh_m2_yr"] < 300) &
    (df["insul_r"] > 10)
]

# Select specific columns
selected = df[["sample_id", "eui_kwh_m2_yr", "insul_r"]]
```

---

## 4. Aggregation and Grouping

### R

```r
# Average EUI
mean_eui <- mean(data$results.eui, na.rm = TRUE)

# Group by insulation R-value bin
data$insul_bin <- cut(data$measure_attributes.insul_r, breaks = 5)
by_insul <- aggregate(results.eui ~ insul_bin, data, mean)

# Summary statistics
summary_stats <- aggregate(
  results.eui ~ measure_attributes.hvac_type,
  data,
  function(x) c(mean = mean(x), sd = sd(x), n = length(x))
)
```

### Python

```python
# Average EUI
mean_eui = df["eui_kwh_m2_yr"].mean()

# Group by insulation R-value bin
df["insul_bin"] = pd.cut(df["insul_r"], bins=5)
by_insul = df.groupby("insul_bin")["eui_kwh_m2_yr"].mean()

# Summary statistics
summary_stats = df.groupby("hvac_type")["eui_kwh_m2_yr"].agg(
    ["mean", "std", "count"]
)
```

---

## 5. Joining and Merging

### R

```r
# Merge samples with parameters
samples <- m$find('{}', '{"name": 1, "results.eui": 1}')
params <- m$find('{}', '{"name": 1, "measure_attributes": 1}')

merged <- merge(samples, params, by = "name")
```

### Python

```python
# In OSimFlow, all data is already in one CSV
# But if you have separate files:
samples = pd.read_csv("results/aggregated_results.csv")
params = pd.read_json("results/work/kpis/sample_0001_kpis.json")

# Merge on sample_id
merged = pd.merge(samples, params, on="sample_id")
```

---

## 6. Custom KPI Extraction

This is the most common migration pattern: replacing an R script that
computed custom metrics with a Python BYOS `extract_kpis` function.

### R (openstudio-server)

```r
# R script: custom_kpi_report.R
# Runs after each simulation, reads eplusout.sql

library(RSQLite)

extract_kpi <- function(sample_dir, sample_id) {
  db <- dbConnect(SQLite(), file.path(sample_dir, "eplusout.sql"))

  # Total site EUI
  eui <- dbGetQuery(db, "
    SELECT Value FROM TabularDataWithStrings
    WHERE TableName = 'Site and Source Energy'
      AND RowName = 'Total Site Energy'
      AND ColumnName = 'Energy Per Total Floor Area'
  ")$Value[1]

  # Heating and cooling
  heating <- dbGetQuery(db, "
    SELECT SUM(CAST(Value AS REAL)) as total
    FROM TabularDataWithStrings
    WHERE TableName = 'End Uses'
      AND ColumnName = 'Heating'
      AND Value != ''
  ")$total[1]

  cooling <- dbGetQuery(db, "
    SELECT SUM(CAST(Value AS REAL)) as total
    FROM TabularDataWithStrings
    WHERE TableName = 'End Uses'
      AND ColumnName = 'Cooling'
      AND Value != ''
  ")$total[1]

  dbDisconnect(db)

  return(data.frame(
    sample_id = sample_id,
    eui_mj_m2 = as.numeric(eui),
    eui_kwh_m2 = as.numeric(eui) / 3.6,
    heating_gj = as.numeric(heating),
    cooling_gj = as.numeric(cooling)
  ))
}

# Process all samples
results <- lapply(sample_dirs, function(d) {
  extract_kpi(d, basename(d))
})
all_results <- do.call(rbind, results)
write.csv(all_results, "custom_kpis.csv", row.names = FALSE)
```

### Python BYOS (OSimFlow)

```python
# user_scripts/custom_kpi_migration.py
"""Equivalent Python BYOS KPI extractor — replaces the R script above."""

import json
import sqlite3
from pathlib import Path


def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    """Extract EUI and end-use breakdown from eplusout.sql.

    This function is called once per sample by the Campaign.

    Args:
        simulation_dir: Directory containing eplusout.sql.
        sample_id: Sample identifier string.
        out: Directory to write the KPI JSON file.

    Returns:
        Path to the written KPI JSON file.
    """
    sql_path = simulation_dir / "eplusout.sql"
    if not sql_path.exists():
        raise FileNotFoundError(f"eplusout.sql not found for {sample_id}")

    kpis: dict = {"sample_id": sample_id, "kpis": {}}

    with sqlite3.connect(sql_path) as con:
        # Total site EUI (same query as R version)
        row = con.execute("""
            SELECT Value
            FROM TabularDataWithStrings
            WHERE TableName = 'Site and Source Energy'
              AND RowName = 'Total Site Energy'
              AND ColumnName = 'Energy Per Total Floor Area'
        """).fetchone()

        if row:
            eui_mj = float(row[0])
            kpis["kpis"]["eui_mj_m2"] = eui_mj
            kpis["kpis"]["eui_kwh_m2_yr"] = eui_mj / 3.6

        # Heating total (same query as R version)
        row = con.execute("""
            SELECT SUM(CAST(Value AS REAL)) as total
            FROM TabularDataWithStrings
            WHERE TableName = 'End Uses'
              AND ColumnName = 'Heating'
              AND Value != ''
        """).fetchone()
        if row and row[0]:
            kpis["kpis"]["heating_gj"] = float(row[0])

        # Cooling total
        row = con.execute("""
            SELECT SUM(CAST(Value AS REAL)) as total
            FROM TabularDataWithStrings
            WHERE TableName = 'End Uses'
              AND ColumnName = 'Cooling'
              AND Value != ''
        """).fetchone()
        if row and row[0]:
            kpis["kpis"]["cooling_gj"] = float(row[0])

    # Write KPI JSON (OSimFlow expects this format)
    out_path = out / f"{sample_id}_kpis.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(kpis, indent=2), encoding="utf-8")
    return out_path
```

**Usage:**

```bash
osimflow run \
  --custom_kpi_extractor user_scripts/custom_kpi_migration.py \
  --input_variables variables.yml \
  --template_sim_package ./template_sim_package \
  --n_samples 100 \
  --outdir ./results
```

---

## 7. Custom Aggregation

### R (openstudio-server)

```r
# R script: aggregate_results.R
# Runs after all simulations complete

library(mongolite)
library(dplyr)

m <- mongo("data_points", url = "mongodb://localhost:27017/osh")
data <- m$find('{"status.state": "completed"}')

# Compute summary statistics
summary <- data %>%
  group_by(measure_attributes.hvac_type) %>%
  summarise(
    n = n(),
    mean_eui = mean(results.eui, na.rm = TRUE),
    sd_eui = sd(results.eui, na.rm = TRUE),
    min_eui = min(results.eui, na.rm = TRUE),
    max_eui = max(results.eui, na.rm = TRUE)
  )

write.csv(summary, "aggregated_summary.csv", row.names = FALSE)
```

### Python (OSimFlow)

OSimFlow has a built-in aggregation step, so you typically don't need a
custom script. However, for custom post-processing:

```python
# post_process.py — run AFTER the campaign completes
import pandas as pd

df = pd.read_csv("results/aggregated_results.csv")

# Compute summary statistics (same as R version)
summary = df.groupby("hvac_type")["eui_kwh_m2_yr"].agg(
    n="count",
    mean_eui="mean",
    sd_eui="std",
    min_eui="min",
    max_eui="max",
).reset_index()

summary.to_csv("aggregated_summary.csv", index=False)
```

---

## 8. Plotting

### R (openstudio-server)

```r
library(ggplot2)

# EUI histogram
ggplot(data, aes(x = results.eui)) +
  geom_histogram(bins = 30, fill = "steelblue", color = "white") +
  labs(x = "EUI (kWh/m²/yr)", y = "Count",
       title = "Distribution of EUI Across Samples")
ggsave("eui_histogram.png", width = 8, height = 6)

# EUI vs insulation R-value
ggplot(data, aes(x = measure_attributes.insul_r, y = results.eui)) +
  geom_point(alpha = 0.5, color = "steelblue") +
  geom_smooth(method = "lm", color = "red") +
  labs(x = "Insulation R-value", y = "EUI (kWh/m²/yr)",
       title = "EUI vs Insulation R-value")
ggsave("eui_vs_insulation.png", width = 8, height = 6)

# Box plot by HVAC type
ggplot(data, aes(x = measure_attributes.hvac_type, y = results.eui)) +
  geom_boxplot(fill = "steelblue", alpha = 0.7) +
  labs(x = "HVAC Type", y = "EUI (kWh/m²/yr)",
       title = "EUI by HVAC System Type") +
  theme(axis.text.x = element_text(angle = 45, hjust = 1))
ggsave("eui_by_hvac.png", width = 8, height = 6)

# Parallel coordinates (replaces PAT GUI)
library(GGally)
ggparcoord(
  data,
  columns = c(5:10),
  groupColumn = "measure_attributes.hvac_type",
  scale = "uniminmax"
) + labs(title = "Parallel Coordinates Plot")
ggsave("parallel_coords.png", width = 12, height = 6)
```

### Python (OSimFlow)

```python
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd

df = pd.read_csv("results/aggregated_results.csv")

# EUI histogram
fig, ax = plt.subplots(figsize=(8, 6))
ax.hist(df["eui_kwh_m2_yr"], bins=30, color="steelblue", edgecolor="white")
ax.set_xlabel("EUI (kWh/m²/yr)")
ax.set_ylabel("Count")
ax.set_title("Distribution of EUI Across Samples")
fig.savefig("eui_histogram.png", dpi=150, bbox_inches="tight")

# EUI vs insulation R-value
fig, ax = plt.subplots(figsize=(8, 6))
ax.scatter(df["insul_r"], df["eui_kwh_m2_yr"], alpha=0.5, color="steelblue")
# Add trend line
z = np.polyfit(df["insul_r"], df["eui_kwh_m2_yr"], 1)
p = np.poly1d(z)
ax.plot(df["insul_r"], p(df["insul_r"]), color="red", linewidth=2)
ax.set_xlabel("Insulation R-value")
ax.set_ylabel("EUI (kWh/m²/yr)")
ax.set_title("EUI vs Insulation R-value")
fig.savefig("eui_vs_insulation.png", dpi=150, bbox_inches="tight")

# Box plot by HVAC type
fig, ax = plt.subplots(figsize=(8, 6))
df.boxplot(column="eui_kwh_m2_yr", by="hvac_type", ax=ax,
           patch_artist=True, boxprops=dict(facecolor="steelblue", alpha=0.7))
plt.xticks(rotation=45, ha="right")
ax.set_xlabel("HVAC Type")
ax.set_ylabel("EUI (kWh/m²/yr)")
ax.set_title("EUI by HVAC System Type")
fig.suptitle("")  # Remove automatic subtitle
fig.savefig("eui_by_hvac.png", dpi=150, bbox_inches="tight")

# Parallel coordinates (replaces PAT GUI)
from pandas.plotting import parallel_coordinates
fig, ax = plt.subplots(figsize=(12, 6))
param_cols = ["insul_r", "wwr_south", "cooling_setpoint", "lighting_power"]
# Normalise to [0, 1] for visual comparison
normed = df[param_cols].apply(lambda x: (x - x.min()) / (x.max() - x.min()))
normed["hvac_type"] = df["hvac_type"]
parallel_coordinates(normed, "hvac_type", ax=ax, alpha=0.3)
ax.set_title("Parallel Coordinates Plot")
ax.set_ylabel("Normalised Value")
ax.legend(loc="upper right", fontsize="small")
fig.savefig("parallel_coords.png", dpi=150, bbox_inches="tight")
```

---

## 9. Complete Example: Reporting Script

A full migration of a typical openstudio-server R reporting script to
an OSimFlow Python post-processing workflow.

### R (openstudio-server)

```r
# full_report.R — Typical openstudio-server analysis script
library(mongolite)
library(ggplot2)
library(dplyr)
library(RSQLite)

# --- Configuration ---
analysis_id <- "507f1f77bcf86cd799439011"

# --- Connect to MongoDB ---
m <- mongo("data_points", url = "mongodb://localhost:27017/osh")

# --- Load data ---
data <- m$find(
  paste0('{"analysis_id": "', analysis_id, '", "status.state": "completed"}')
)
cat("Loaded", nrow(data), "completed samples\n")

# --- Compute custom metrics ---
data$eui_kwh <- as.numeric(data$results.eui) / 3.6
data$total_enduse <- rowSums(data[, grep("^results\\.", names(data))],
                              na.rm = TRUE)

# --- Summary table ---
summary <- data %>%
  summarise(
    n_samples = n(),
    mean_eui = mean(eui_kwh, na.rm = TRUE),
    median_eui = median(eui_kwh, na.rm = TRUE),
    min_eui = min(eui_kwh, na.rm = TRUE),
    max_eui = max(eui_kwh, na.rm = TRUE),
    pct_below_150 = mean(eui_kwh < 150, na.rm = TRUE) * 100
  )
print(summary)

# --- Sensitivity: correlation with parameters ---
param_cols <- grep("measure_attributes\\.", names(data), value = TRUE)
correlations <- sapply(param_cols, function(col) {
  cor(data[[col]], data$eui_kwh, use = "complete.obs")
})
cor_df <- data.frame(
  parameter = gsub("measure_attributes\\.", "", names(correlations)),
  correlation = abs(correlations)
)
cor_df <- cor_df[order(-cor_df$correlation), ]
cat("\nTop parameters by correlation with EUI:\n")
print(head(cor_df, 10))

# --- Plots ---
# EUI distribution
ggplot(data, aes(x = eui_kwh)) +
  geom_histogram(bins = 30, fill = "steelblue") +
  geom_vline(xintercept = 150, linetype = "dashed", color = "red") +
  labs(title = "EUI Distribution", x = "EUI (kWh/m²/yr)")
ggsave("report_eui_dist.png", width = 8, height = 5)

# Scatter matrix
pairs_data <- data[, c("eui_kwh", param_cols[1:min(4, length(param_cols))])]
pairs(pairs_data, main = "Parameter Scatter Matrix")

cat("\nReport complete.\n")
```

### Python (OSimFlow)

```python
# post_process_report.py — Equivalent OSimFlow post-processing
"""Replaces full_report.R — run after the campaign completes."""

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from pathlib import Path


def generate_report(results_dir: str = "results") -> None:
    """Generate a complete analysis report from campaign results.

    This replaces the typical openstudio-server R reporting workflow.
    Run after the campaign: `python post_process_report.py`
    """
    results_path = Path(results_dir)

    # --- Load data ---
    csv_path = results_path / "aggregated_results.csv"
    parquet_path = results_path / "aggregated_results.parquet"

    if parquet_path.exists():
        df = pd.read_parquet(parquet_path)
    elif csv_path.exists():
        df = pd.read_csv(csv_path)
    else:
        raise FileNotFoundError(
            f"No results found in {results_path}/. "
            "Run a campaign first with `osimflow run ...`"
        )

    # Filter to completed samples only
    if "status" in df.columns:
        df = df[df["status"] == "completed"]

    print(f"Loaded {len(df)} completed samples")

    # --- Identify parameter vs KPI columns ---
    # In OSimFlow output, KPI columns include eui_kwh_m2_yr and
    # per-end-use columns. Parameter columns are the variable names
    # from variables.yml.
    kpi_col = "eui_kwh_m2_yr"
    if kpi_col not in df.columns:
        print(f"Warning: {kpi_col} not found. Available columns: {list(df.columns)}")
        return

    # Exclude metadata columns to find parameter columns
    meta_cols = {"sample_id", "status", "start_time", "end_time", kpi_col}
    param_cols = [c for c in df.columns if c not in meta_cols and df[c].dtype in (float, int)]

    # --- Summary table ---
    summary = {
        "n_samples": len(df),
        "mean_eui": df[kpi_col].mean(),
        "median_eui": df[kpi_col].median(),
        "min_eui": df[kpi_col].min(),
        "max_eui": df[kpi_col].max(),
        "pct_below_150": (df[kpi_col] < 150).mean() * 100,
    }
    print("\n--- Summary ---")
    for k, v in summary.items():
        print(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v}")

    # --- Sensitivity: correlation with parameters ---
    correlations = {}
    for col in param_cols:
        if df[col].std() > 0:
            correlations[col] = abs(df[col].corr(df[kpi_col]))

    cor_series = pd.Series(correlations).sort_values(ascending=False)
    print("\nTop parameters by correlation with EUI:")
    print(cor_series.head(10).to_string())

    # --- Plots ---
    sns.set_theme()

    # EUI distribution
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(df[kpi_col], bins=30, color="steelblue", edgecolor="white")
    ax.axvline(150, color="red", linestyle="--", label="150 kWh/m²/yr target")
    ax.set_xlabel("EUI (kWh/m²/yr)")
    ax.set_ylabel("Count")
    ax.set_title("EUI Distribution")
    ax.legend()
    fig.savefig(results_path / "report_eui_dist.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    # Scatter matrix (top 4 parameters)
    top_params = list(cor_series.head(4).index)
    if top_params:
        scatter_cols = top_params + [kpi_col]
        scatter_df = df[scatter_cols].dropna()
        fig = sns.pairplot(scatter_df, diag_kind="kde", corner=True)
        fig.figure.suptitle("Parameter Scatter Matrix", y=1.02)
        fig.figure.savefig(
            results_path / "report_scatter_matrix.png",
            dpi=150, bbox_inches="tight",
        )
        plt.close("all")

    # Parameter sensitivity bar chart
    fig, ax = plt.subplots(figsize=(10, 5))
    cor_series.head(15).plot.barh(ax=ax, color="steelblue")
    ax.set_xlabel("Absolute Correlation with EUI")
    ax.set_title("Parameter Sensitivity (Correlation with EUI)")
    ax.invert_yaxis()
    fig.tight_layout()
    fig.savefig(results_path / "report_sensitivity.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    print(f"\nPlots saved to {results_path}/")
    print("Report complete.")


if __name__ == "__main__":
    import sys
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "results"
    generate_report(results_dir)
```

---

## 10. Quick Reference Table

| Task | R (openstudio-server) | Python (OSimFlow) |
|---|---|---|
| Load results | `m <- mongo(...); df <- m$find(...)` | `df = pd.read_csv("results.csv")` |
| Filter rows | `subset(df, col > val)` | `df[df["col"] > val]` |
| Select columns | `df[, c("a", "b")]` | `df[["a", "b"]]` |
| Group + aggregate | `aggregate(y ~ group, df, mean)` | `df.groupby("group")["y"].mean()` |
| Sort | `df[order(df$col), ]` | `df.sort_values("col")` |
| Merge | `merge(a, b, by="id")` | `pd.merge(a, b, on="id")` |
| Mutate | `df$new <- df$a / df$b` | `df["new"] = df["a"] / df["b"]` |
| Summary | `summary(df$col)` | `df["col"].describe()` |
| Histogram | `ggplot(...) + geom_histogram()` | `df["col"].hist()` or `sns.histplot()` |
| Scatter | `ggplot(...) + geom_point()` | `df.plot.scatter(x, y)` or `sns.scatterplot()` |
| Box plot | `ggplot(...) + geom_boxplot()` | `df.boxplot(column=..., by=...)` |
| Save plot | `ggsave("file.png")` | `fig.savefig("file.png")` |
| Read SQL | `dbGetQuery(db, "SELECT ...")` | `pd.read_sql("SELECT ...", con)` |
| Write CSV | `write.csv(df, "out.csv")` | `df.to_csv("out.csv", index=False)` |

---

## See Also

- [Migration Guide](../../docs/migration-openstudio-server.md) — Full migration walkthrough
- [user_scripts/README.md](../README.md) — BYOS interface reference
- [user_scripts/examples/](.) — Worked BYOS examples
- [eplusout.sql Guide](../../docs/eplusout-sql-guide.md) — Querying EnergyPlus SQL output
- [Data Analysis Notebook](../../examples/notebooks/legacy_migration_analysis.ipynb) — Jupyter cookbook
