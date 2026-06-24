"""Tests for time-series aggregation in bin/aggregate_results.py (issue #40)."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BIN = PROJECT_ROOT / "bin"

sys.path.insert(0, str(BIN))
from aggregate_results import (  # noqa: E402
    TS_RESOLUTIONS,
    TimeSeriesAggregator,
    detect_timeseries_tables,
    estimate_ts_size_bytes,
)

# ---------------------------------------------------------------------------
# Helpers: synthetic eplusout.sql with time-series tables
# ---------------------------------------------------------------------------


def _make_ts_sql(
    path: Path,
    *,
    n_hours: int = 24,
    n_variables: int = 2,
) -> Path:
    """Create a minimal eplusout.sql with ReportData/ReportDataDictionary/Time."""
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE ReportDataDictionary (
            ReportDataDictionaryIndex INTEGER PRIMARY KEY,
            IndexGroup TEXT,
            Name TEXT,
            ReportingFrequency TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE Time (
            TimeIndex INTEGER PRIMARY KEY,
            Month INTEGER,
            Day INTEGER,
            Hour INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE ReportData (
            ReportDataIndex INTEGER PRIMARY KEY,
            TimeIndex INTEGER,
            DictionaryIndex INTEGER,
            Value REAL
        )
    """)

    for i in range(n_variables):
        cur.execute(
            "INSERT INTO ReportDataDictionary VALUES (?, ?, ?, ?)",
            (i, f"Zone {i}", f"Variable_{i}", "Hourly"),
        )

    row_idx = 0
    for hour in range(n_hours):
        month = 1 + hour // 730
        day = 1 + (hour % 730) // 24
        hr = hour % 24
        cur.execute("INSERT INTO Time VALUES (?, ?, ?, ?)", (hour, month, day, hr))
        for var_idx in range(n_variables):
            value = (var_idx + 1) * (hour + 1) * 0.5
            cur.execute(
                "INSERT INTO ReportData VALUES (?, ?, ?, ?)",
                (row_idx, hour, var_idx, value),
            )
            row_idx += 1

    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Tests: detect_timeseries_tables
# ---------------------------------------------------------------------------


class TestDetectTimeseriesTables:
    def test_detects_all_ts_tables(self, tmp_path: Path) -> None:
        sql_path = _make_ts_sql(tmp_path / "eplusout.sql")
        tables = detect_timeseries_tables(sql_path)
        assert "ReportData" in tables
        assert "ReportDataDictionary" in tables
        assert "Time" in tables

    def test_empty_on_missing_file(self, tmp_path: Path) -> None:
        tables = detect_timeseries_tables(tmp_path / "nonexistent.sql")
        assert tables == []

    def test_empty_on_sql_without_ts_tables(self, tmp_path: Path) -> None:
        sql_path = tmp_path / "eplusout.sql"
        conn = sqlite3.connect(str(sql_path))
        conn.execute("CREATE TABLE TabularDataWithStrings (Value TEXT)")
        conn.commit()
        conn.close()
        tables = detect_timeseries_tables(sql_path)
        assert "ReportData" not in tables


# ---------------------------------------------------------------------------
# Tests: estimate_ts_size_bytes
# ---------------------------------------------------------------------------


class TestEstimateTsSizeBytes:
    def test_basic_estimate(self) -> None:
        size = estimate_ts_size_bytes(n_samples=100, hours_per_year=8760, n_variables=50)
        assert size == 100 * 8760 * 50 * 8

    def test_zero_samples(self) -> None:
        assert estimate_ts_size_bytes(n_samples=0) == 0

    def test_realistic_campaign(self) -> None:
        size = estimate_ts_size_bytes(n_samples=1000, n_variables=50)
        assert size == 3_504_000_000  # ~3.5 GB


# ---------------------------------------------------------------------------
# Tests: TimeSeriesAggregator
# ---------------------------------------------------------------------------


class TestTimeSeriesAggregator:
    def test_invalid_resolution_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid ts_resolution"):
            TimeSeriesAggregator(resolution="minutely")

    def test_valid_resolutions(self) -> None:
        for res in TS_RESOLUTIONS:
            agg = TimeSeriesAggregator(resolution=res)
            assert agg.resolution == res

    def test_aggregate_monthly(self, tmp_path: Path) -> None:
        sql_path = _make_ts_sql(tmp_path / "eplusout.sql", n_hours=48, n_variables=2)
        agg = TimeSeriesAggregator(resolution="monthly")
        df = agg.aggregate_sql(sql_path, sample_id="test_001")
        assert not df.empty
        assert "sample_id" in df.columns
        assert "avg_value" in df.columns
        assert "sum_value" in df.columns
        assert (df["sample_id"] == "test_001").all()

    def test_aggregate_daily(self, tmp_path: Path) -> None:
        sql_path = _make_ts_sql(tmp_path / "eplusout.sql", n_hours=48, n_variables=2)
        agg = TimeSeriesAggregator(resolution="daily")
        df = agg.aggregate_sql(sql_path, sample_id="test_001")
        assert not df.empty
        assert "avg_value" in df.columns

    def test_aggregate_annual(self, tmp_path: Path) -> None:
        sql_path = _make_ts_sql(tmp_path / "eplusout.sql", n_hours=48, n_variables=2)
        agg = TimeSeriesAggregator(resolution="annual")
        df = agg.aggregate_sql(sql_path, sample_id="test_001")
        assert not df.empty
        assert "avg_value" in df.columns

    def test_aggregate_hourly(self, tmp_path: Path) -> None:
        sql_path = _make_ts_sql(tmp_path / "eplusout.sql", n_hours=48, n_variables=2)
        agg = TimeSeriesAggregator(resolution="hourly")
        df = agg.aggregate_sql(sql_path, sample_id="test_001")
        assert not df.empty
        assert "Value" in df.columns
        assert "Month" in df.columns
        assert "Hour" in df.columns

    def test_missing_sql_returns_empty(self, tmp_path: Path) -> None:
        agg = TimeSeriesAggregator(resolution="monthly")
        df = agg.aggregate_sql(tmp_path / "nonexistent.sql", sample_id="x")
        assert df.empty

    def test_sql_without_ts_tables_returns_empty(self, tmp_path: Path) -> None:
        sql_path = tmp_path / "eplusout.sql"
        conn = sqlite3.connect(str(sql_path))
        conn.execute("CREATE TABLE TabularDataWithStrings (Value TEXT)")
        conn.commit()
        conn.close()
        agg = TimeSeriesAggregator(resolution="monthly")
        df = agg.aggregate_sql(sql_path, sample_id="x")
        assert df.empty

    def test_corrupt_sql_returns_empty(self, tmp_path: Path) -> None:
        sql_path = tmp_path / "eplusout.sql"
        sql_path.write_text("not a sqlite database")
        agg = TimeSeriesAggregator(resolution="monthly")
        df = agg.aggregate_sql(sql_path, sample_id="x")
        assert df.empty


class TestAggregateCampaign:
    def test_multi_sample_campaign(self, tmp_path: Path) -> None:
        for i in range(3):
            sim_dir = tmp_path / f"sim_{i:04d}"
            sim_dir.mkdir()
            _make_ts_sql(sim_dir / "eplusout.sql", n_hours=24, n_variables=2)

        agg = TimeSeriesAggregator(resolution="monthly")
        sim_dirs = [tmp_path / f"sim_{i:04d}" for i in range(3)]
        df = agg.aggregate_campaign(sim_dirs)
        assert not df.empty
        assert set(df["sample_id"].unique()) == {
            "sim_0000",
            "sim_0001",
            "sim_0002",
        }

    def test_skips_dirs_without_sql(self, tmp_path: Path) -> None:
        (tmp_path / "sim_0000").mkdir()
        sim_dir = tmp_path / "sim_0001"
        sim_dir.mkdir()
        _make_ts_sql(sim_dir / "eplusout.sql", n_hours=24, n_variables=1)

        agg = TimeSeriesAggregator(resolution="monthly")
        df = agg.aggregate_campaign([tmp_path / "sim_0000", sim_dir])
        assert not df.empty
        assert set(df["sample_id"].unique()) == {"sim_0001"}

    def test_empty_dirs_returns_empty(self, tmp_path: Path) -> None:
        agg = TimeSeriesAggregator(resolution="monthly")
        df = agg.aggregate_campaign([tmp_path / "nonexistent"])
        assert df.empty


# ---------------------------------------------------------------------------
# Integration: CLI with --ts_resolution
# ---------------------------------------------------------------------------


def test_cli_ts_resolution_monthly(tmp_path: Path) -> None:
    sim_dir = tmp_path / "sim" / "0001"
    sim_dir.mkdir(parents=True)
    _make_ts_sql(sim_dir / "eplusout.sql", n_hours=48, n_variables=2)

    kpi_file = tmp_path / "kpi_0001.json"
    kpi_file.write_text('{"sample_id": "0001", "kpis": {"eui": 100.0}}')

    out_csv = tmp_path / "agg.csv"
    out_fail = tmp_path / "fail.csv"

    result = subprocess.run(
        [
            sys.executable,
            str(BIN / "aggregate_results.py"),
            "--kpis",
            str(kpi_file),
            "--simulation_dirs",
            str(sim_dir),
            "--out_csv",
            str(out_csv),
            "--out_failed",
            str(out_fail),
            "--ts_resolution",
            "monthly",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    ts_csv = tmp_path / "timeseries_aggregated.csv"
    assert ts_csv.exists()
    ts_df = pd.read_csv(ts_csv)
    assert not ts_df.empty
    assert "avg_value" in ts_df.columns


def test_cli_ts_resolution_daily(tmp_path: Path) -> None:
    sim_dir = tmp_path / "sim" / "0002"
    sim_dir.mkdir(parents=True)
    _make_ts_sql(sim_dir / "eplusout.sql", n_hours=48, n_variables=1)

    kpi_file = tmp_path / "kpi_0002.json"
    kpi_file.write_text('{"sample_id": "0002", "kpis": {"eui": 80.0}}')

    out_csv = tmp_path / "agg2.csv"
    out_fail = tmp_path / "fail2.csv"

    result = subprocess.run(
        [
            sys.executable,
            str(BIN / "aggregate_results.py"),
            "--kpis",
            str(kpi_file),
            "--simulation_dirs",
            str(sim_dir),
            "--out_csv",
            str(out_csv),
            "--out_failed",
            str(out_fail),
            "--ts_resolution",
            "daily",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0

    ts_csv = tmp_path / "timeseries_aggregated.csv"
    assert ts_csv.exists()


def test_cli_ts_resolution_annual(tmp_path: Path) -> None:
    sim_dir = tmp_path / "sim" / "0003"
    sim_dir.mkdir(parents=True)
    _make_ts_sql(sim_dir / "eplusout.sql", n_hours=100, n_variables=3)

    kpi_file = tmp_path / "kpi_0003.json"
    kpi_file.write_text('{"sample_id": "0003", "kpis": {"eui": 120.0}}')

    out_csv = tmp_path / "agg3.csv"
    out_fail = tmp_path / "fail3.csv"

    result = subprocess.run(
        [
            sys.executable,
            str(BIN / "aggregate_results.py"),
            "--kpis",
            str(kpi_file),
            "--simulation_dirs",
            str(sim_dir),
            "--out_csv",
            str(out_csv),
            "--out_failed",
            str(out_fail),
            "--ts_resolution",
            "annual",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0

    ts_csv = tmp_path / "timeseries_aggregated.csv"
    assert ts_csv.exists()
    ts_df = pd.read_csv(ts_csv)
    assert not ts_df.empty


def test_cli_no_ts_tables_produces_no_ts_output(tmp_path: Path) -> None:
    sim_dir = tmp_path / "sim" / "0004"
    sim_dir.mkdir(parents=True)
    (sim_dir / "eplusout.sql").write_text("-- placeholder sql without TS tables")
    (sim_dir / "eplusout.err").write_text("")

    kpi_file = tmp_path / "kpi_0004.json"
    kpi_file.write_text('{"sample_id": "0004", "kpis": {"eui": 90.0}}')

    out_csv = tmp_path / "agg4.csv"
    out_fail = tmp_path / "fail4.csv"

    result = subprocess.run(
        [
            sys.executable,
            str(BIN / "aggregate_results.py"),
            "--kpis",
            str(kpi_file),
            "--simulation_dirs",
            str(sim_dir),
            "--out_csv",
            str(out_csv),
            "--out_failed",
            str(out_fail),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    ts_csv = tmp_path / "timeseries_aggregated.csv"
    assert not ts_csv.exists()


def test_cli_ts_outdir_flag(tmp_path: Path) -> None:
    sim_dir = tmp_path / "sim" / "0005"
    sim_dir.mkdir(parents=True)
    _make_ts_sql(sim_dir / "eplusout.sql", n_hours=24, n_variables=1)

    kpi_file = tmp_path / "kpi_0005.json"
    kpi_file.write_text('{"sample_id": "0005", "kpis": {"eui": 95.0}}')

    out_csv = tmp_path / "results" / "agg5.csv"
    ts_outdir = tmp_path / "ts_output"
    out_fail = tmp_path / "results" / "fail5.csv"

    result = subprocess.run(
        [
            sys.executable,
            str(BIN / "aggregate_results.py"),
            "--kpis",
            str(kpi_file),
            "--simulation_dirs",
            str(sim_dir),
            "--out_csv",
            str(out_csv),
            "--out_failed",
            str(out_fail),
            "--ts_resolution",
            "monthly",
            "--ts_outdir",
            str(ts_outdir),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0

    ts_csv = ts_outdir / "timeseries_aggregated.csv"
    assert ts_csv.exists()
    assert not ts_csv.stat().st_size == 0
