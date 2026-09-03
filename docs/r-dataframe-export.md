# R DataFrame Export Guide

> **Sources:** db-engineer, pm-planner (issue #284)
>
> OSS uses R/Rserve for statistical computing and provides R dataframe export.
> OSimFlow is Python-native with no built-in R integration. This guide
> documents the recommended Parquet bridge workflow that already works today.

---

## Overview

OSimFlow's [aggregate_results.py](../bin/aggregate_results.py)
writes two output formats by default:

| File | Format | R read function |
|------|--------|-----------------|
| `aggregated_results.csv` | CSV | `read.csv()`, `readr::read_csv()` |
| `aggregated_results.parquet` | Parquet | `arrow::read_parquet()` (recommended) |
| `failed_simulations.csv` | CSV | `read.csv()` |
| `timeseries_aggregated.parquet` | Parquet | `arrow::read_parquet()` |

The **Parquet format is recommended** for R because:

1. `pyarrow` (already an OSimFlow dependency) writes IEEE-754 compliant
   Parquet files with full floating-point fidelity — no rounding that
   `read.csv()` can introduce on large numeric columns.
2. The `arrow` R package reads Parquet with zero-copy where possible,
   making it faster than parsing CSV for large campaigns.
3. Column types (integer, double, string, logical) are preserved
   accurately across the Python → Parquet → R round-trip.
4. Both `arrow` and `arrow` R package handle `NaN` / `NA` values
   consistently.

---

## Quick Start

### 1. Install R packages (one-time)

```r
install.packages("arrow")
install.packages("dplyr")   # optional but recommended
install.packages("ggplot2") # optional for plotting
```

### 2. Read OSimFlow results in R

```r
library(arrow)

# Read aggregated campaign results
results <- read_parquet("results/aggregated_results.parquet")

# Read failed simulations
failures <- read_parquet("results/failed_simulations.parquet")

# Read the monthly time-series aggregation (always written)
ts <- read_parquet("results/timeseries_aggregated.parquet")
```

### 3. Basic EDA with dplyr

```r
library(dplyr)

# Summary statistics
summary(results)

# Group-wise aggregation
results |>
  group_by(pack) |>
  summarise(
    mean_eui = mean(eui, na.rm = TRUE),
    sd_eui = sd(eui, na.rm = TRUE),
    .groups = "drop"
  )
```

### 4. Plotting with ggplot2

```r
library(ggplot2)

# EUI distribution
ggplot(results, aes(x = eui)) +
  geom_histogram(bins = 30, fill = "steelblue") +
  labs(
    title = "Energy Use Intensity Distribution",
    x = "EUI (kWh/m²/yr)"
  )

# Pareto front (if multi-objective optimization was run)
pareto <- read_parquet("results/pareto/gen_1.parquet")
ggplot(pareto, aes(x = objective1, y = objective2)) +
  geom_point(aes(color = rank)) +
  scale_color_viridis_c() +
  labs(title = "Pareto Front", x = "Objective 1", y = "Objective 2")
```

---

## Programmatic Export with `RDataFrameExporter`

The [`osimflow.exporters.r_dataframe`](../osimflow/exporters/r_dataframe.py)
module provides a Python API for exporting results to R-readable formats:

```python
from pathlib import Path
from osimflow.exporters.r_dataframe import RDataFrameExporter

exporter = RDataFrameExporter(outdir=Path("r_export"), format="parquet")

# Export all result files from a campaign directory
outputs = exporter.export_all(work_dir=Path("campaign_results"))
# Returns: {"parquet": Path("r_export/campaign_results.parquet"), ...}
```

### CLI usage

```bash
# After a campaign completes, export results for R:
python -c "
from pathlib import Path
from osimflow.exporters.r_dataframe import RDataFrameExporter
exporter = RDataFrameExporter(outdir=Path('r_export'))
exporter.export_all(work_dir=Path('results'))
"
```

---

## CSV Fallback (no arrow package)

If the `arrow` R package is not available, use the CSV fallback:

```r
results <- read.csv("results/aggregated_results.csv", stringsAsFactors = FALSE)
failures <- read.csv("results/failed_simulations.csv", stringsAsFactors = FALSE)
ts <- read.csv("results/timeseries_aggregated.csv", stringsAsFactors = FALSE)
```

Note: When using CSV, large numeric columns (e.g. EUI values with many
decimal places) may lose precision due to floating-point string encoding.
Prefer Parquet for numerically sensitive analyses.

---

## Statistical Computing with R

Once data is loaded into R, the full R ecosystem is available:

```r
# Linear mixed-effects models
library(lme4)
lmer(eui ~ wall_r_value + roof_r_value + (1|pack), data = results)

# Sensitivity analysis (SALib-compatible interface)
library(sensitivity)
X <- results[, c("wall_r_value", "roof_r_value", "window_shgc")]
Y <- results$eui
sobol <- sobol2007(X, Y, order = 2, nboot = 100)

# Time-series analysis
library(forecast)
ts_data <- ts(ts$avg_value, frequency = 12)
fit <- auto.arima(ts_data)
forecast(fit, h = 12)
```

---

## Parquet → R Round-Trip Fidelity

OSimFlow uses `pandas.DataFrame.to_parquet()` (pyarrow backend) which
produces Parquet files compliant with the
[Apache Parquet format specification](https://parquet.apache.org/docs/).
The following column types are preserved accurately:

| Python dtype | Parquet type | R class |
|-------------|--------------|---------|
| `int64` | `INT64` | `integer64` / `double` |
| `float64` | `DOUBLE` | `numeric` |
| `str` | `BYTE_ARRAY` | `character` |
| `bool` | `BOOLEAN` | `logical` |
| `datetime64[ns]` | `INT96` | `POSIXct` |
| NaN / None | `NULL` | `NA` |

---

## FAQ

**Q: Does OSimFlow require R to run?**
No. R integration is entirely optional. OSimFlow is Python-native and
uses scipy/pandas for all built-in post-processing.

**Q: Why not use Rserve?**
Rserve is a TCP/IP server protocol for calling R from other languages.
It adds deployment complexity (Rserve daemon, network latency, version
coupling). The Parquet bridge is simpler, more robust, and works
offline — the same data file can be shared with any number of R sessions
on any machine.

**Q: Can I export directly from the campaign DAG?**
Not yet. The `RDataFrameExporter` works on post-campaign result files.
Direct integration with the `Campaign` class is a future enhancement
(issue #284 follow-up).

**Q: What about reticulate (calling Python from R)?**
`reticulate` is an alternative but requires a matching Python environment
on the R machine. The Parquet bridge works with only the `arrow` R package
and no Python dependency.
