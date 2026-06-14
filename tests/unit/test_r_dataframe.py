"""Tests for osimflow.exporters.r_dataframe."""

from pathlib import Path

import pandas as pd
import pytest

from osimflow.exporters.r_dataframe import (
    R_CODE_SNIPPETS,
    SUPPORTED_FORMATS,
    RDataFrameExporter,
    get_r_code_snippet,
)


class TestRDataFrameExporterInit:
    def test_default_format_is_parquet(self, tmp_path: Path) -> None:
        exporter = RDataFrameExporter(outdir=tmp_path)
        assert exporter.format == "parquet"

    def test_accepts_parquet_format(self, tmp_path: Path) -> None:
        exporter = RDataFrameExporter(outdir=tmp_path, format="parquet")
        assert exporter.format == "parquet"

    def test_accepts_csv_format(self, tmp_path: Path) -> None:
        exporter = RDataFrameExporter(outdir=tmp_path, format="csv")
        assert exporter.format == "csv"

    def test_rejects_invalid_format(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid format"):
            RDataFrameExporter(outdir=tmp_path, format="xlsx")


class TestExportResults:
    def test_writes_parquet_and_csv_when_parquet_format(self, tmp_path: Path) -> None:
        """When format=parquet, both Parquet and CSV are written."""
        exporter = RDataFrameExporter(outdir=tmp_path, format="parquet")

        # Create a source CSV (simulating aggregated_results.csv)
        src_csv = tmp_path / "in" / "aggregated_results.csv"
        src_csv.parent.mkdir(exist_ok=True)
        df = pd.DataFrame({"sample_id": ["0001", "0002"], "eui": [150.5, 200.0]})
        df.to_csv(src_csv, index=False)

        outputs = exporter.export_results(aggregated_csv=src_csv)

        assert "parquet" in outputs
        assert "csv" in outputs
        assert outputs["parquet"].suffix == ".parquet"
        assert outputs["csv"].suffix == ".csv"
        assert outputs["parquet"].exists()
        assert outputs["csv"].exists()

    def test_writes_only_csv_when_csv_format(self, tmp_path: Path) -> None:
        exporter = RDataFrameExporter(outdir=tmp_path, format="csv")

        src_csv = tmp_path / "in" / "aggregated_results.csv"
        src_csv.parent.mkdir(exist_ok=True)
        df = pd.DataFrame({"sample_id": ["0001"], "eui": [150.5]})
        df.to_csv(src_csv, index=False)

        outputs = exporter.export_results(aggregated_csv=src_csv)

        assert "csv" in outputs
        assert "parquet" not in outputs

    def test_prefers_parquet_over_csv_source(self, tmp_path: Path) -> None:
        """When both CSV and Parquet sources exist, Parquet is used."""
        exporter = RDataFrameExporter(outdir=tmp_path, format="parquet")

        src_parquet = tmp_path / "in" / "aggregated_results.parquet"
        src_csv = tmp_path / "in" / "aggregated_results.csv"
        src_parquet.parent.mkdir(exist_ok=True)
        src_csv.parent.mkdir(exist_ok=True)

        df = pd.DataFrame({"sample_id": ["0001"], "eui": [150.5]})
        df.to_parquet(src_parquet, index=False)
        df.to_csv(src_csv, index=False)

        outputs = exporter.export_results(aggregated_csv=src_csv, aggregated_parquet=src_parquet)

        # Should have written a new Parquet from the source Parquet
        assert outputs["parquet"].exists()
        result = pd.read_parquet(outputs["parquet"])
        assert len(result) == 1

    def test_skips_when_no_source(self, tmp_path: Path) -> None:
        exporter = RDataFrameExporter(outdir=tmp_path, format="parquet")
        outputs = exporter.export_results()
        assert outputs == {}

    def test_parquet_output_round_trips_correctly(self, tmp_path: Path) -> None:
        """Written Parquet can be read back with correct values."""
        exporter = RDataFrameExporter(outdir=tmp_path, format="parquet")

        src_csv = tmp_path / "in" / "aggregated_results.csv"
        src_csv.parent.mkdir(exist_ok=True)
        df = pd.DataFrame(
            {
                "sample_id": ["0001", "0002"],
                "eui": [150.5, 200.0],
                "peak_pwr": [15000.0, 22000.0],
            }
        )
        df.to_csv(src_csv, index=False)

        outputs = exporter.export_results(aggregated_csv=src_csv)
        result = pd.read_parquet(outputs["parquet"])

        assert list(result.columns) == ["sample_id", "eui", "peak_pwr"]
        assert result["sample_id"].tolist() == ["0001", "0002"]
        assert result["eui"].tolist() == [150.5, 200.0]


class TestExportFailures:
    def test_writes_parquet_and_csv(self, tmp_path: Path) -> None:
        exporter = RDataFrameExporter(outdir=tmp_path, format="parquet")

        failed_csv = tmp_path / "in" / "failed_simulations.csv"
        failed_csv.parent.mkdir(exist_ok=True)
        df = pd.DataFrame(
            {
                "sample_id": ["0003"],
                "failure_category": ["convergence"],
                "error_summary": ["Exceeded max iterations"],
            }
        )
        df.to_csv(failed_csv, index=False)

        outputs = exporter.export_failures(failed_csv=failed_csv)

        assert "parquet" in outputs
        assert "csv" in outputs
        assert outputs["parquet"].exists()

    def test_skips_when_file_missing(self, tmp_path: Path) -> None:
        exporter = RDataFrameExporter(outdir=tmp_path)
        outputs = exporter.export_failures()
        assert outputs == {}


class TestExportTimeseries:
    def test_writes_parquet_and_csv(self, tmp_path: Path) -> None:
        exporter = RDataFrameExporter(outdir=tmp_path, format="parquet")

        ts_parquet = tmp_path / "in" / "timeseries_aggregated.parquet"
        ts_parquet.parent.mkdir(exist_ok=True)
        df = pd.DataFrame(
            {
                "sample_id": ["0001", "0001"],
                "Month": [1, 2],
                "avg_value": [100.0, 110.0],
            }
        )
        df.to_parquet(ts_parquet, index=False)

        outputs = exporter.export_timeseries(timeseries_parquet=ts_parquet)

        assert "parquet" in outputs
        assert "csv" in outputs
        assert outputs["parquet"].exists()

    def test_skips_when_file_missing(self, tmp_path: Path) -> None:
        exporter = RDataFrameExporter(outdir=tmp_path)
        outputs = exporter.export_timeseries()
        assert outputs == {}


class TestExportAll:
    def test_scans_work_dir_and_exports_all(self, tmp_path: Path) -> None:
        exporter = RDataFrameExporter(outdir=tmp_path / "r_out", format="parquet")

        # Create standard OSimFlow output files
        work_dir = tmp_path / "campaign"
        work_dir.mkdir(exist_ok=True)
        agg = work_dir / "aggregated_results.csv"
        df_agg = pd.DataFrame({"sample_id": ["0001"], "eui": [150.0]})
        df_agg.to_csv(agg, index=False)

        failed = work_dir / "failed_simulations.csv"
        df_fail = pd.DataFrame({"sample_id": ["0002"], "failure_category": ["convergence"]})
        df_fail.to_csv(failed, index=False)

        ts = work_dir / "timeseries_aggregated.parquet"
        df_ts = pd.DataFrame({"sample_id": ["0001"], "Month": [1], "avg_value": [100.0]})
        df_ts.to_parquet(ts, index=False)

        outputs = exporter.export_all(work_dir=work_dir)

        assert len(outputs) >= 3  # at least one output per input type


class TestGetRCodeSnippet:
    def test_install_snippet(self) -> None:
        snippet = get_r_code_snippet("install")
        assert "install.packages" in snippet
        assert "arrow" in snippet

    def test_read_parquet_snippet(self) -> None:
        snippet = get_r_code_snippet("read_parquet")
        assert "read_parquet" in snippet
        assert "library(arrow)" in snippet

    def test_read_csv_snippet(self) -> None:
        snippet = get_r_code_snippet("read_csv")
        assert "read.csv" in snippet

    def test_dplyr_eda_snippet(self) -> None:
        snippet = get_r_code_snippet("dplyr_eda")
        assert "dplyr" in snippet
        assert "group_by" in snippet

    def test_ggplot2_snippet(self) -> None:
        snippet = get_r_code_snippet("ggplot2")
        assert "ggplot2" in snippet or "ggplot" in snippet

    def test_unknown_key_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown snippet key"):
            get_r_code_snippet("nonexistent")


class TestRCodeSnippetsAvailable:
    def test_all_expected_keys_present(self) -> None:
        expected = {"install", "read_parquet", "read_csv", "dplyr_eda", "ggplot2"}
        assert expected.issubset(R_CODE_SNIPPETS.keys())


class TestSupportedFormats:
    def test_supported_formats_contains_csv_and_parquet(self) -> None:
        assert "csv" in SUPPORTED_FORMATS
        assert "parquet" in SUPPORTED_FORMATS
