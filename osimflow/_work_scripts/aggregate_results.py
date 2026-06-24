#!/usr/bin/env python3
"""aggregate_results.py — Collect per-sample KPIs and identify failures.

See docs/OSimFlow.md §4.2 (PROCESS_AGGREGATE_RESULTS) for the contract.

Time-series aggregation (issue #40):
    When ``--ts_resolution`` is set (default ``monthly``), the script
    scans each per-sample ``eplusout.sql`` for EnergyPlus time-series
    tables and aggregates them to the requested resolution using
    SQL-based GROUP BY.  Raw hourly data stays in the per-sample
    ``.sql`` files behind ``--archive_intermediates``.
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aggregate_results")


# ---------------------------------------------------------------------------
# Error classification patterns (issue #56)
# ---------------------------------------------------------------------------
# Each entry maps an EnergyPlus error category to a list of regex patterns.
# The first matching category wins. Order matters: more specific patterns
# should come before more general ones.

FAILURE_PATTERNS: list[tuple[str, list[re.Pattern[str]]]] = [
    (
        "convergence",
        [
            re.compile(r"did not converge", re.IGNORECASE),
            re.compile(r"exceeded max iterations", re.IGNORECASE),
            re.compile(r"not converged", re.IGNORECASE),
            re.compile(r"iteration.?limit", re.IGNORECASE),
        ],
    ),
    (
        "surface_geometry",
        [
            re.compile(r"surface.*(?:intersection|non.convex)", re.IGNORECASE),
            re.compile(r"non.convex\s*surface", re.IGNORECASE),
            re.compile(r"zero.area\s*surface", re.IGNORECASE),
            re.compile(r"surfaceless\s*zone", re.IGNORECASE),
            re.compile(r"detected.*zero.area", re.IGNORECASE),
        ],
    ),
    (
        "hvac_sizing",
        [
            re.compile(r"autosize.*(?:failed|out.of.range)", re.IGNORECASE),
            re.compile(r"plant\s*loop.*(?:not.converged|no.demand|no.load)", re.IGNORECASE),
            re.compile(r"no\s*load\s*on\s*plant\s*loop", re.IGNORECASE),
            re.compile(r"sizing.*(?:failed|error)", re.IGNORECASE),
        ],
    ),
    (
        "schedule",
        [
            re.compile(r"schedule.*(?:not.found|invalid|does not exist)", re.IGNORECASE),
            re.compile(r"(?:missing|unknown)\s*schedule", re.IGNORECASE),
        ],
    ),
    (
        "material_construction",
        [
            re.compile(r"material.*(?:not.found|does not exist)", re.IGNORECASE),
            re.compile(r"construction.*(?:not.found|does not exist|invalid)", re.IGNORECASE),
            re.compile(r"(?:missing|unknown)\s*(?:material|construction)", re.IGNORECASE),
        ],
    ),
    (
        "weather_file",
        [
            re.compile(r"weather\s*file.*(?:error|not.found|invalid|missing)", re.IGNORECASE),
            re.compile(r"cannot\s*(?:open|find|read).*\.epw", re.IGNORECASE),
        ],
    ),
    (
        "memory_timeout",
        [
            re.compile(r"(?:allocation|memory).*error", re.IGNORECASE),
            re.compile(r"timeout", re.IGNORECASE),
            re.compile(r"out\s*of\s*memory", re.IGNORECASE),
        ],
    ),
    (
        "timestep_instability",
        [
            re.compile(r"temperatures?\s*out\s*of\s*bounds?", re.IGNORECASE),
            re.compile(r"node.*temperature\s*out\s*of\s*range", re.IGNORECASE),
            re.compile(r"facsimile.*failed", re.IGNORECASE),
            re.compile(r"timestep.*(?:unstable|error)", re.IGNORECASE),
        ],
    ),
]

CATEGORY_SUGGESTIONS: dict[str, str] = {
    "convergence": "Consider increasing iteration limits or relaxing convergence tolerances in the HVAC controller settings.",
    "surface_geometry": "Simplify geometry or fix non-convex surfaces. Check for coincident/overlapping surfaces.",
    "hvac_sizing": "Review autosizing parameters or provide manual sizing values. Verify design-day definitions.",
    "schedule": "Check schedule names in the model match those referenced by objects (e.g., thermostat, lights).",
    "material_construction": "Verify all materials and constructions are defined and referenced correctly in the model.",
    "weather_file": "Verify the EPW weather file exists, is readable, and has the expected format.",
    "memory_timeout": "Reduce model complexity or increase available compute resources (memory/timestep count).",
    "timestep_instability": "Reduce the simulation timestep (e.g., from 60 to 10 minutes) or relax convergence criteria.",
    "generic_severe": "Review the full eplusout.err for additional context around this error.",
}


# ---------------------------------------------------------------------------
# Time-series aggregation (issue #40)
# ---------------------------------------------------------------------------

TS_RESOLUTIONS = ("hourly", "daily", "monthly", "annual")

_ENERGYPLUS_TS_TABLES: list[str] = [
    "ReportData",
    "ReportDataDictionary",
    "Time",
    "ZoneSizing",
    "ComponentSizing",
]

_MONTHLY_GROUP = """
    SELECT
        rd.DictionaryIndex,
        ddd.IndexGroup,
        ddd.Name,
        ddd.ReportingFrequency,
        SUM(rd.Value) AS sum_value,
        AVG(rd.Value) AS avg_value,
        MIN(rd.Value) AS min_value,
        MAX(rd.Value) AS max_value,
        COUNT(rd.Value) AS n_points
    FROM ReportData rd
    JOIN ReportDataDictionary ddd ON rd.DictionaryIndex = ddd.ReportDataDictionaryIndex
    JOIN Time t ON rd.TimeIndex = t.TimeIndex
    GROUP BY rd.DictionaryIndex, t.Month
"""

_DAILY_GROUP = """
    SELECT
        rd.DictionaryIndex,
        ddd.IndexGroup,
        ddd.Name,
        ddd.ReportingFrequency,
        SUM(rd.Value) AS sum_value,
        AVG(rd.Value) AS avg_value,
        MIN(rd.Value) AS min_value,
        MAX(rd.Value) AS max_value,
        COUNT(rd.Value) AS n_points
    FROM ReportData rd
    JOIN ReportDataDictionary ddd ON rd.DictionaryIndex = ddd.ReportDataDictionaryIndex
    JOIN Time t ON rd.TimeIndex = t.TimeIndex
    GROUP BY rd.DictionaryIndex, t.Month, t.Day
"""

_ANNUAL_GROUP = """
    SELECT
        rd.DictionaryIndex,
        ddd.IndexGroup,
        ddd.Name,
        ddd.ReportingFrequency,
        SUM(rd.Value) AS sum_value,
        AVG(rd.Value) AS avg_value,
        MIN(rd.Value) AS min_value,
        MAX(rd.Value) AS max_value,
        COUNT(rd.Value) AS n_points
    FROM ReportData rd
    JOIN ReportDataDictionary ddd ON rd.DictionaryIndex = ddd.ReportDataDictionaryIndex
    GROUP BY rd.DictionaryIndex
"""

_HOURLY_GROUP = """
    SELECT
        rd.DictionaryIndex,
        ddd.IndexGroup,
        ddd.Name,
        ddd.ReportingFrequency,
        rd.Value,
        t.Month,
        t.Day,
        t.Hour
    FROM ReportData rd
    JOIN ReportDataDictionary ddd ON rd.DictionaryIndex = ddd.ReportDataDictionaryIndex
    JOIN Time t ON rd.TimeIndex = t.TimeIndex
"""

_TS_QUERIES: dict[str, str] = {
    "monthly": _MONTHLY_GROUP,
    "daily": _DAILY_GROUP,
    "annual": _ANNUAL_GROUP,
    "hourly": _HOURLY_GROUP,
}


def detect_timeseries_tables(sql_path: Path) -> list[str]:
    """Return list of EnergyPlus time-series tables found in *sql_path*."""
    if not sql_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(sql_path))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        conn.close()
    except sqlite3.DatabaseError:
        log.warning("Could not open %s for table detection", sql_path)
        return []
    return [t for t in _ENERGYPLUS_TS_TABLES if t in tables]


def estimate_ts_size_bytes(
    n_samples: int,
    hours_per_year: int = 8760,
    n_variables: int = 50,
    bytes_per_value: int = 8,
) -> int:
    """Estimate raw (hourly) time-series data size in bytes.

    Formula: ``n_samples * hours_per_year * n_variables * bytes_per_value``
    """
    return n_samples * hours_per_year * n_variables * bytes_per_value


class TimeSeriesAggregator:
    """Aggregate EnergyPlus time-series data from per-sample SQL files.

    Parameters
    ----------
    resolution : str
        Aggregation granularity: ``hourly``, ``daily``, ``monthly`` (default),
        or ``annual``.
    """

    def __init__(self, resolution: str = "monthly") -> None:
        if resolution not in TS_RESOLUTIONS:
            raise ValueError(
                f"Invalid ts_resolution '{resolution}'. Must be one of {TS_RESOLUTIONS}"
            )
        self.resolution = resolution

    def aggregate_sql(self, sql_path: Path, sample_id: str) -> pd.DataFrame:
        """Read and aggregate time-series data from a single eplusout.sql.

        Returns an empty DataFrame when the SQL file lacks the required
        EnergyPlus time-series tables (``ReportData``, ``ReportDataDictionary``,
        ``Time``).
        """
        if not sql_path.exists():
            return pd.DataFrame()

        try:
            conn = sqlite3.connect(str(sql_path))
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            required = {"ReportData", "ReportDataDictionary", "Time"}
            if not required.issubset(tables):
                conn.close()
                return pd.DataFrame()

            query = _TS_QUERIES[self.resolution]
            df = pd.read_sql_query(query, conn)
            conn.close()
        except (sqlite3.DatabaseError, pd.io.sql.DatabaseError) as exc:
            log.warning("Time-series aggregation failed for %s: %s", sql_path, exc)
            return pd.DataFrame()

        if df.empty:
            return df

        df["sample_id"] = sample_id
        return df

    def aggregate_campaign(self, simulation_dirs: list[Path]) -> pd.DataFrame:
        """Aggregate time-series across all simulation directories.

        Returns a combined DataFrame with a ``sample_id`` column.
        """
        frames: list[pd.DataFrame] = []
        for sim_dir in simulation_dirs:
            sql_path = sim_dir / "eplusout.sql"
            if not sql_path.exists():
                continue
            sample_id = sim_dir.name
            df = self.aggregate_sql(sql_path, sample_id)
            if not df.empty:
                frames.append(df)
                log.info(
                    "time-series aggregation: sample=%s resolution=%s rows=%d",
                    sample_id,
                    self.resolution,
                    len(df),
                )
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)


def _write_ts_output(
    ts_df: pd.DataFrame,
    out_dir: Path,
    parquet: bool = False,
) -> None:
    """Write the time-series DataFrame to CSV (and optionally Parquet)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_csv = out_dir / "timeseries_aggregated.csv"
    ts_df.to_csv(ts_csv, index=False)
    log.info("Wrote time-series CSV: %s (%d rows)", ts_csv, len(ts_df))
    if parquet:
        ts_parquet = out_dir / "timeseries_aggregated.parquet"
        ts_df.to_parquet(ts_parquet, index=False)
        log.info("Wrote time-series Parquet: %s", ts_parquet)


# ---------------------------------------------------------------------------
# Error classification patterns (issue #56) — continued below
# ---------------------------------------------------------------------------


def _scan_err_file(err_path: Path) -> tuple[int, str]:
    """Scan .err file in a single pass, returning severe count and root-cause line.

    Counts all "  * Severe" / "** Severe" lines and identifies the first
    root-cause line matching a FAILURE_PATTERNS entry, falling back to the
    first severe error line if no pattern matches.

    Returns:
        Tuple of (total_severe_count, root_cause_line).
    """
    count = 0
    first_severe = ""
    root_cause = ""
    try:
        with err_path.open() as f:
            for line in f:
                is_severe = bool(re.search(r"  \*+\sSevere", line))
                if is_severe:
                    count += 1
                    if not first_severe:
                        first_severe = line.strip()
                stripped = line.strip()
                if not root_cause:
                    for _cat, patterns in FAILURE_PATTERNS:
                        for pat in patterns:
                            if pat.search(stripped):
                                root_cause = stripped
                                break
                        if root_cause:
                            break
    except (OSError, UnicodeDecodeError):
        log.warning("Could not read error file: %s", err_path, exc_info=True)
    # Fall back to first severe line if no pattern matched
    if not root_cause:
        root_cause = first_severe
    return count, root_cause


def _count_severe_errors(err_path: Path) -> int:
    """Count total severe error lines in an EnergyPlus error file.

    This is a compatibility wrapper around _scan_err_file.
    """
    count, _ = _scan_err_file(err_path)
    return count


def _find_root_cause_line(err_path: Path) -> str:
    """Find the earliest root-cause line from an EnergyPlus error file.

    This is a compatibility wrapper around _scan_err_file.
    """
    _, root_cause = _scan_err_file(err_path)
    return root_cause


def _classify_line(line: str) -> str:
    """Classify an error line into a failure category.

    Args:
        line: The error line text (typically the first Severe Error).

    Returns:
        Category string (e.g., "convergence", "generic_severe").
    """
    for category, patterns in FAILURE_PATTERNS:
        for pat in patterns:
            if pat.search(line):
                return category
    return "generic_severe"


def diagnose_error(error_line: str, err_file_path: Path) -> dict[str, Any]:
    """Diagnose an EnergyPlus error with domain-aware categorization.

    Takes the severe error line and the full .err file path, categorizes
    the error, and returns actionable guidance.

    Args:
        error_line: The first severe error line from eplusout.err.
        err_file_path: Path to the eplusout.err file for additional context.

    Returns:
        Dict with keys: category, summary, suggestion, severity,
        total_severe_errors, root_cause_line.
    """
    try:
        category = _classify_line(error_line)
        total_severe, root_cause = _scan_err_file(err_file_path)
        suggestion = CATEGORY_SUGGESTIONS.get(category, CATEGORY_SUGGESTIONS["generic_severe"])

        return {
            "category": category,
            "summary": error_line[:500],
            "suggestion": suggestion,
            "severity": "critical" if total_severe > 10 else "high",
            "total_severe_errors": total_severe,
            "root_cause_line": root_cause[:500] if root_cause else error_line[:500],
        }
    except Exception:
        log.warning("Error diagnosis failed for %s", err_file_path, exc_info=True)
        return {
            "category": "generic_severe",
            "summary": error_line[:500],
            "suggestion": CATEGORY_SUGGESTIONS["generic_severe"],
            "severity": "high",
            "total_severe_errors": 0,
            "root_cause_line": error_line[:500],
        }


def parse_kpi_json(kpi_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(kpi_path.read_text())
        res = {"sample_id": data.get("sample_id", kpi_path.stem.replace("kpi_", ""))}
        kpis = data.get("kpis", {})
        res.update(kpis)
        return res
    except Exception as e:
        log.warning("Failed to parse KPI JSON %s: %s", kpi_path, e)
        return {"sample_id": kpi_path.stem.replace("kpi_", "")}


def _load_samples_params(samples_json: Path) -> dict[str, dict[str, object]]:
    """Load per-sample parameter values from samples.json.

    Returns a dict mapping sample_id -> {param_name: value, ...}.
    Categorical values with a ``label`` key are flattened to the label string.
    Returns an empty dict if the file doesn't exist or is unreadable.
    """
    if not samples_json.exists():
        log.info("samples.json not found at %s — skipping input parameter merge", samples_json)
        return {}
    try:
        data = json.loads(samples_json.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read samples.json at %s: %s", samples_json, exc)
        return {}

    samples_list = data.get("samples", [])
    result: dict[str, dict[str, object]] = {}
    for sample in samples_list:
        sid = sample.get("sample_id", "")
        values = sample.get("values", {})
        # Flatten categorical dicts with a "label" key to plain strings
        flat: dict[str, object] = {}
        for k, v in values.items():
            if isinstance(v, dict) and "label" in v:
                flat[k] = v["label"]
            else:
                flat[k] = v
        result[sid] = flat
    return result


def extract_failure(sim_dir: Path) -> dict[str, Any] | None:
    err_path = sim_dir / "eplusout.err"
    err_summary = None
    if err_path.exists() and err_path.stat().st_size > 0:
        try:
            with err_path.open() as f:
                for line in f:
                    if re.search(r"  \*+\sSevere", line):
                        err_summary = line.strip()
                        break
        except (OSError, UnicodeDecodeError):
            log.warning("Could not read error file: %s", err_path)

    sql_path = sim_dir / "eplusout.sql"
    if err_summary or not sql_path.exists():
        diagnosis: dict[str, Any] | None = None
        if err_summary and err_path.exists():
            diagnosis = diagnose_error(err_summary, err_path)

        result = {
            "sample_id": sim_dir.name,
            "error_summary": err_summary or "eplusout.sql missing",
            "exit_code": 1 if err_summary or not sql_path.exists() else 0,
            "log_path": str(err_path) if err_path.exists() else "",
        }
        if diagnosis:
            result["failure_category"] = diagnosis["category"]
            result["root_cause_line"] = diagnosis["root_cause_line"]
            result["total_severe_errors"] = diagnosis["total_severe_errors"]
            result["diagnosis_suggestion"] = diagnosis["suggestion"]
        else:
            result["failure_category"] = ""
            result["root_cause_line"] = ""
            result["total_severe_errors"] = 0
            result["diagnosis_suggestion"] = ""
        return result
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kpis", required=True, nargs="+", type=Path)
    parser.add_argument("--simulation_dirs", required=True, nargs="+", type=Path)
    parser.add_argument("--out_csv", required=True, type=Path)
    parser.add_argument("--out_parquet", type=Path, default=None)
    parser.add_argument("--out_failed", required=True, type=Path)
    parser.add_argument(
        "--baseline_sample_id",
        default=None,
        help="Sample ID of the baseline (for pct improvement columns).",
    )
    parser.add_argument(
        "--ts_resolution",
        default="monthly",
        choices=TS_RESOLUTIONS,
        help=(
            "Time-series aggregation resolution. Default: 'monthly'. "
            "Use 'hourly' only with --archive_intermediates to avoid "
            "very large CSV output. See docs/time-series-management.md."
        ),
    )
    parser.add_argument(
        "--ts_outdir",
        type=Path,
        default=None,
        help=(
            "Directory for aggregated time-series output. "
            "Defaults to the same directory as --out_csv."
        ),
    )
    parser.add_argument(
        "--samples_json",
        type=Path,
        default=None,
        help=(
            "Path to samples.json containing per-sample input parameter values. "
            "When provided, parameter columns are merged into the aggregated results "
            "CSV before KPI columns. Missing file is non-fatal (backward compatible)."
        ),
    )
    args = parser.parse_args()

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    args.out_failed.parent.mkdir(parents=True, exist_ok=True)

    # 1. Read KPIs into wide-form DataFrame
    all_kpis = []
    for kpi_path in args.kpis:
        if kpi_path.exists():
            all_kpis.append(parse_kpi_json(kpi_path))

    if all_kpis:
        df = pd.DataFrame(all_kpis)

        # Baseline comparison (issue #64): compute pct improvement columns
        if args.baseline_sample_id and args.baseline_sample_id in df["sample_id"].values:
            baseline_row = df[df["sample_id"] == args.baseline_sample_id].iloc[0]
            numeric_cols = df.select_dtypes(include="number").columns
            for col in numeric_cols:
                baseline_val = baseline_row[col]
                if pd.notna(baseline_val) and baseline_val != 0:
                    improvement_col = f"{col}_pct_improvement"
                    df[improvement_col] = ((baseline_val - df[col]) / baseline_val * 100.0).round(2)
            log.info(
                "computed baseline comparison columns against sample_id=%s",
                args.baseline_sample_id,
            )

        # Merge input parameters from samples.json (issue #276).
        # Parameter columns are placed before KPI columns so users can
        # trace each result back to its inputs at a glance.
        if args.samples_json is not None:
            params_map = _load_samples_params(args.samples_json)
            if params_map:
                params_rows: list[dict[str, object]] = []
                for sid in df["sample_id"]:
                    params_rows.append(params_map.get(str(sid), {}))
                params_df = pd.DataFrame(params_rows)
                # Reorder: sample_id, then parameter columns, then KPI columns
                df = pd.concat(
                    [df[["sample_id"]], params_df, df.drop(columns=["sample_id"])], axis=1
                )
                log.info(
                    "merged %d input parameter columns from %s",
                    len(params_df.columns),
                    args.samples_json,
                )
            else:
                log.info("no input parameters loaded — writing KPIs only")
        else:
            log.info("no --samples_json provided — writing KPIs only")

        df.to_csv(args.out_csv, index=False)
        if args.out_parquet:
            df.to_parquet(args.out_parquet, index=False)
    else:
        df = pd.DataFrame(columns=["sample_id"])
        df.to_csv(args.out_csv, index=False)
        if args.out_parquet:
            df.to_parquet(args.out_parquet, index=False)

    # 2. Extract failures
    failures = []
    for sim_dir in args.simulation_dirs:
        if sim_dir.exists():
            f = extract_failure(sim_dir)
            if f:
                failures.append(f)

    if failures:
        fail_df = pd.DataFrame(failures)
        cols = [
            "sample_id",
            "failure_category",
            "root_cause_line",
            "total_severe_errors",
            "error_summary",
            "exit_code",
            "log_path",
            "diagnosis_suggestion",
        ]
        for c in cols:
            if c not in fail_df.columns:
                fail_df[c] = None
        fail_df = fail_df[cols]
        fail_df.to_csv(args.out_failed, index=False)
    else:
        args.out_failed.write_text(
            "sample_id,failure_category,root_cause_line,total_severe_errors,"
            "error_summary,exit_code,log_path,diagnosis_suggestion\n"
        )

    # 3. Time-series aggregation (issue #40)
    ts_agg = TimeSeriesAggregator(resolution=args.ts_resolution)
    ts_df = ts_agg.aggregate_campaign(args.simulation_dirs)
    if not ts_df.empty:
        ts_outdir = args.ts_outdir or args.out_csv.parent
        _write_ts_output(ts_df, ts_outdir, parquet=args.out_parquet is not None)
    else:
        log.info("No time-series data found in simulation directories.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
