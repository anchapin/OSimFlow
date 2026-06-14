"""R DataFrame export — bridge from OSimFlow aggregated results to R.

This module provides utilities for exporting OSimFlow campaign results
(aggregated KPIs, time-series, and failed-simulations) to formats that
R can consume natively:

- **CSV** — read via ``read.csv()`` / ``readr::read_csv()``
- **Parquet** — read via ``arrow::read_parquet()`` (recommended for large
  campaigns; preserves column types better than CSV)

Parquet is the preferred format because:

1. pyarrow (already a dependency) writes IEEE-754 compliant Parquet files
   that R's arrow package reads with full fidelity (no floating-point
   rounding that ``read.csv()`` can introduce).
2. Nested/structured columns (if any) are preserved.
3. Filter push-down and column selection in R are possible without
   loading the full dataset into memory.

Example R usage
---------------

::

    # Install once
    install.packages("arrow")
    install.packages("dplyr")

    library(arrow)
    library(dplyr)

    # Read aggregated results
    results <- read_parquet("campaign_results.parquet")

    # Read failed simulations
    failures <- read_csv("failed_simulations.csv")

    # Read time-series (if archived)
    ts <- read_parquet("timeseries_aggregated.parquet")

    # Basic EDA
    library(dplyr)
    results |> group_by(pack) |> summarise(mean_eui = mean(eui))

    # Pareto front (if multi-objective optimization was run)
    pareto <- read_parquet("pareto/gen_1.parquet")
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger("osimflow.exporters.r_dataframe")

# ---------------------------------------------------------------------------
# Format registry
# ---------------------------------------------------------------------------

SUPPORTED_FORMATS: frozenset[str] = frozenset({"csv", "parquet"})


def _validate_paths(
    aggregated_csv: Path | None,
    aggregated_parquet: Path | None,
    failed_csv: Path | None,
    timeseries_parquet: Path | None,
) -> None:
    """Validate that at least one input file exists."""
    inputs = [
        aggregated_csv,
        aggregated_parquet,
        failed_csv,
        timeseries_parquet,
    ]
    if not any(inp is not None and inp.exists() for inp in inputs):
        raise FileNotFoundError(
            "No result files found. Provide at least one of: "
            "--aggregated_csv, --aggregated_parquet, --failed_csv, "
            "--timeseries_parquet"
        )


# ---------------------------------------------------------------------------
# R DataFrame exporter
# ---------------------------------------------------------------------------


class RDataFrameExporter:
    """Export OSimFlow aggregated results to R-readable formats.

    Parameters
    ----------
    outdir
        Directory where output files will be written.
    format
        Output format: ``"parquet"`` (default, recommended) or ``"csv"``.
        Both formats are always written when possible to maximise
        compatibility with different R environments.
    """

    def __init__(
        self,
        outdir: Path,
        format: str = "parquet",
    ) -> None:
        if format not in SUPPORTED_FORMATS:
            raise ValueError(
                f"Invalid format {format!r}. Must be one of {sorted(SUPPORTED_FORMATS)}"
            )
        self.outdir = Path(outdir)
        self.format = format

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def export_results(
        self,
        aggregated_csv: Path | None = None,
        aggregated_parquet: Path | None = None,
    ) -> dict[str, Path]:
        """Export aggregated results (KPI CSV/Parquet) to R-readable format.

        Parameters
        ----------
        aggregated_csv
            Path to ``aggregated_results.csv`` (from ``aggregate_results.py``).
        aggregated_parquet
            Path to ``aggregated_results.parquet`` (from ``aggregate_results.py``).

        Returns
        -------
        dict[str, Path]
            Mapping of output path(s) written.
        """
        self.outdir.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, Path] = {}

        # --- Determine source DataFrame ---
        df = self._load_aggregated(aggregated_csv, aggregated_parquet)
        if df is None:
            log.warning("No aggregated results found — skipping results export")
            return outputs

        # --- Write primary format ---
        if self.format == "parquet":
            if "sample_id" in df.columns:
                df = df.copy()
                df["sample_id"] = df["sample_id"].astype(str).str.zfill(4)
            out_path = self.outdir / "campaign_results.parquet"
            df.to_parquet(out_path, index=False)
            log.info(
                "Wrote R-readable Parquet: %s (%d rows, %d cols)",
                out_path,
                len(df),
                len(df.columns),
            )
            outputs["parquet"] = out_path

            # Always also write CSV for environments without arrow
            csv_path = self.outdir / "campaign_results.csv"
            df.to_csv(csv_path, index=False)
            log.info("Wrote CSV fallback: %s", csv_path)
            outputs["csv"] = csv_path
        else:
            out_path = self.outdir / "campaign_results.csv"
            df.to_csv(out_path, index=False)
            log.info(
                "Wrote R-readable CSV: %s (%d rows, %d cols)", out_path, len(df), len(df.columns)
            )
            outputs["csv"] = out_path

        return outputs

    def export_failures(
        self,
        failed_csv: Path | None = None,
    ) -> dict[str, Path]:
        """Export failed-simulations CSV to R-readable format.

        Parameters
        ----------
        failed_csv
            Path to ``failed_simulations.csv`` (from ``aggregate_results.py``).

        Returns
        -------
        dict[str, Path]
            Mapping of output path(s) written.
        """
        self.outdir.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, Path] = {}

        if failed_csv is None or not failed_csv.exists():
            log.warning("No failed_simulations.csv found — skipping failures export")
            return outputs

        try:
            df = pd.read_csv(failed_csv)
        except Exception as exc:
            log.warning("Could not read failed_simulations.csv: %s", exc)
            return outputs

        out_path = self.outdir / "failed_simulations.parquet"
        df.to_parquet(out_path, index=False)
        log.info("Wrote failures Parquet: %s (%d rows)", out_path, len(df))
        outputs["parquet"] = out_path

        csv_path = self.outdir / "failed_simulations.csv"
        df.to_csv(csv_path, index=False)
        outputs["csv"] = csv_path

        return outputs

    def export_timeseries(
        self,
        timeseries_parquet: Path | None = None,
    ) -> dict[str, Path]:
        """Export aggregated time-series Parquet to R-readable format.

        Parameters
        ----------
        timeseries_parquet
            Path to ``timeseries_aggregated.parquet`` (from
            ``aggregate_results.py`` with ``--ts_resolution monthly``).

        Returns
        -------
        dict[str, Path]
            Mapping of output path(s) written.
        """
        self.outdir.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, Path] = {}

        if timeseries_parquet is None or not timeseries_parquet.exists():
            log.warning("No timeseries_aggregated.parquet found — skipping time-series export")
            return outputs

        try:
            df = pd.read_parquet(timeseries_parquet)
        except Exception as exc:
            log.warning("Could not read timeseries_aggregated.parquet: %s", exc)
            return outputs

        out_path = self.outdir / "timeseries_aggregated.parquet"
        df.to_parquet(out_path, index=False)
        log.info(
            "Wrote time-series Parquet: %s (%d rows, %d cols)", out_path, len(df), len(df.columns)
        )
        outputs["parquet"] = out_path

        csv_path = self.outdir / "timeseries_aggregated.csv"
        df.to_csv(csv_path, index=False)
        outputs["csv"] = csv_path

        return outputs

    def export_all(
        self,
        work_dir: Path,
    ) -> dict[str, Path]:
        """Export all result files from a campaign work directory.

        Scans ``work_dir`` for the standard OSimFlow output files and
        exports them to R-readable formats.

        Parameters
        ----------
        work_dir
            Campaign output directory (contains ``work/sim/`` subdir).

        Returns
        -------
        dict[str, Path]
            All output paths written, keyed by file type.
        """
        outputs: dict[str, Path] = {}

        # Aggregated results — prefer Parquet (already pyarrow)
        agg_parquet = work_dir / "aggregated_results.parquet"
        agg_csv = work_dir / "aggregated_results.csv"
        if agg_parquet.exists():
            result = self.export_results(aggregated_parquet=agg_parquet)
            outputs["aggregated_parquet"] = result["parquet"]
            outputs["aggregated_csv"] = result["csv"]
        elif agg_csv.exists():
            result = self.export_results(aggregated_csv=agg_csv)
            outputs["aggregated_parquet"] = result["parquet"]
            outputs["aggregated_csv"] = result["csv"]

        # Failed simulations
        failed_csv = work_dir / "failed_simulations.csv"
        if failed_csv.exists():
            result = self.export_failures(failed_csv=failed_csv)
            outputs["failures_parquet"] = result["parquet"]
            outputs["failures_csv"] = result["csv"]

        # Time-series
        ts_parquet = work_dir / "timeseries_aggregated.parquet"
        if ts_parquet.exists():
            result = self.export_timeseries(timeseries_parquet=ts_parquet)
            outputs["timeseries_parquet"] = result["parquet"]
            outputs["timeseries_csv"] = result["csv"]

        return outputs

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_aggregated(
        csv: Path | None,
        parquet: Path | None,
    ) -> pd.DataFrame | None:
        """Load aggregated results from CSV or Parquet source."""
        if parquet is not None and parquet.exists():
            try:
                return pd.read_parquet(parquet)
            except Exception as exc:
                log.warning("Could not read Parquet %s: %s — falling back to CSV", parquet, exc)

        if csv is not None and csv.exists():
            try:
                return pd.read_csv(csv)
            except Exception as exc:
                log.warning("Could not read CSV %s: %s", csv, exc)

        return None


# ---------------------------------------------------------------------------
# R code snippets (for documentation and CLI --show-r-code flag)
# ---------------------------------------------------------------------------

R_CODE_SNIPPETS: dict[str, str] = {
    "install": """\
# Install required R packages (one-time setup)
install.packages("arrow")   # Parquet reader — also installs libcurl, ssl, etc.
install.packages("dplyr")   # Data manipulation (optional but recommended)
""",
    "read_parquet": """\
library(arrow)

# Read aggregated campaign results
results <- read_parquet("campaign_results.parquet")

# Read failed simulations
failures <- read_parquet("failed_simulations.parquet")

# Read time-series (if --ts_resolution was used)
ts <- read_parquet("timeseries_aggregated.parquet")
""",
    "read_csv": """\
# Fallback when arrow is not available
results <- read.csv("campaign_results.csv", stringsAsFactors = FALSE)
failures <- read.csv("failed_simulations.csv", stringsAsFactors = FALSE)
ts <- read.csv("timeseries_aggregated.csv", stringsAsFactors = FALSE)
""",
    "dplyr_eda": """\
library(dplyr)

# Basic EDA — summary statistics per sample
summary(results)

# Group-wise aggregation
results |>
  group_by(pack) |>
  summarise(
    mean_eui = mean(eui, na.rm = TRUE),
    sd_eui = sd(eui, na.rm = TRUE),
    .groups = "drop"
  )
""",
    "ggplot2": """\
library(ggplot2)

# EUI distribution
ggplot(results, aes(x = eui)) +
  geom_histogram(bins = 30, fill = "steelblue") +
  labs(title = "Energy Use Intensity Distribution", x = "EUI (kWh/m²/yr)")

# Pareto front (if multi-objective optimization was run)
ggplot(pareto, aes(x = objective1, y = objective2)) +
  geom_point(aes(color = rank)) +
  scale_color_viridis_c() +
  labs(title = "Pareto Front", x = "Objective 1", y = "Objective 2")
""",
}


def get_r_code_snippet(key: str) -> str:
    """Return an R code snippet by key.

    Parameters
    ----------
    key
        One of: ``install``, ``read_parquet``, ``read_csv``,
        ``dplyr_eda``, ``ggplot2``.

    Returns
    -------
    str
        The R source code.
    """
    snippet = R_CODE_SNIPPETS.get(key)
    if snippet is None:
        raise KeyError(f"Unknown snippet key {key!r}. Available: {sorted(R_CODE_SNIPPETS.keys())}")
    return snippet
