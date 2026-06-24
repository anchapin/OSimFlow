"""Tests for osimflow.exporters.r_dataframe."""

from pathlib import Path

import pandas as pd
import pytest

from osimflow.exporters.r_dataframe import (
    R_CODE_SNIPPETS,
    SUPPORTED_FORMATS,
    RDataFrameExporter,
    _validate_paths,
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

    def test_export_all_with_only_aggregated_parquet(self, tmp_path: Path) -> None:
        """Lines 286-288: only aggregated parquet exists (not CSV)."""
        exporter = RDataFrameExporter(outdir=tmp_path / "r_out", format="parquet")

        work_dir = tmp_path / "campaign"
        work_dir.mkdir(exist_ok=True)

        # Only aggregated_results.parquet exists, not CSV
        agg_pq = work_dir / "aggregated_results.parquet"
        df = pd.DataFrame({"sample_id": ["0001"], "eui": [150.0]})
        df.to_parquet(agg_pq, index=False)

        outputs = exporter.export_all(work_dir=work_dir)

        assert "aggregated_parquet" in outputs
        assert "aggregated_csv" in outputs

    def test_export_all_with_only_aggregated_csv_no_failures_no_ts(self, tmp_path: Path) -> None:
        """Lines 289-295: only aggregated CSV exists (parquet missing)."""
        exporter = RDataFrameExporter(outdir=tmp_path / "r_out", format="parquet")

        work_dir = tmp_path / "campaign"
        work_dir.mkdir(exist_ok=True)

        # Only CSV exists
        agg_csv = work_dir / "aggregated_results.csv"
        df = pd.DataFrame({"sample_id": ["0001"], "eui": [150.0]})
        df.to_csv(agg_csv, index=False)

        outputs = exporter.export_all(work_dir=work_dir)

        assert "aggregated_parquet" in outputs
        assert "aggregated_csv" in outputs

    def test_export_all_only_failed_simulations(self, tmp_path: Path) -> None:
        """Lines 303-308: only failed_simulations.csv exists."""
        exporter = RDataFrameExporter(outdir=tmp_path / "r_out", format="parquet")

        work_dir = tmp_path / "campaign"
        work_dir.mkdir(exist_ok=True)

        # Only failed_simulations.csv
        failed_csv = work_dir / "failed_simulations.csv"
        df_fail = pd.DataFrame({"sample_id": ["0002"], "failure_category": ["convergence"]})
        df_fail.to_csv(failed_csv, index=False)

        outputs = exporter.export_all(work_dir=work_dir)

        assert "failures_parquet" in outputs
        assert "failures_csv" in outputs


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


class TestValidatePaths:
    """Tests for _validate_paths (lines 72-83)."""

    def test_raises_when_no_files_exist(self, tmp_path: Path) -> None:
        """Lines 72-83: FileNotFoundError when no input files exist."""
        with pytest.raises(FileNotFoundError, match="No result files found"):
            _validate_paths(
                aggregated_csv=None,
                aggregated_parquet=None,
                failed_csv=None,
                timeseries_parquet=None,
            )

    def test_passes_when_aggregated_csv_exists(self, tmp_path: Path) -> None:
        """No error when aggregated_csv exists."""
        csv_path = tmp_path / "results.csv"
        csv_path.write_text("sample_id,eui\n0001,150\n")
        # Should not raise
        _validate_paths(
            aggregated_csv=csv_path,
            aggregated_parquet=None,
            failed_csv=None,
            timeseries_parquet=None,
        )


class TestExportResultsCorruptedSource:
    """Tests for _load_aggregated error paths (lines 322-324, 329-330)."""

    def test_load_aggregated_parquet_error_falls_back_to_csv(self, tmp_path: Path) -> None:
        """Lines 322-324: pd.read_parquet error falls back to CSV."""
        exporter = RDataFrameExporter(outdir=tmp_path, format="parquet")

        # Create a corrupted parquet file that exists but can't be read
        bad_parquet = tmp_path / "bad.parquet"
        bad_parquet.write_bytes(b"this is not a parquet file")

        good_csv = tmp_path / "good.csv"
        df = pd.DataFrame({"sample_id": ["0001"], "eui": [150.0]})
        df.to_csv(good_csv, index=False)

        # Should not raise — falls back to CSV
        result = exporter._load_aggregated(csv=good_csv, parquet=bad_parquet)
        assert result is not None
        assert len(result) == 1

    def test_load_aggregated_csv_error_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lines 329-330: pd.read_csv error returns None."""
        import pandas as pd

        exporter = RDataFrameExporter(outdir=tmp_path, format="parquet")

        bad_csv = tmp_path / "bad.csv"
        bad_csv.write_text("sample_id,eui\n0001,150\n")

        def _raise_csv_error(*args: object, **kwargs: object):
            raise pd.errors.ParserError("simulated CSV parse error")

        monkeypatch.setattr(pd, "read_csv", _raise_csv_error)
        result = exporter._load_aggregated(csv=bad_csv, parquet=None)
        assert result is None


class TestExportFailuresCorruptedSource:
    """Tests for export_failures error paths (lines 203-205)."""

    def test_export_failures_csv_read_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lines 203-205: pd.read_csv error is logged and returns empty outputs."""
        import logging

        import pandas as pd

        exporter = RDataFrameExporter(outdir=tmp_path, format="parquet")

        bad_csv = tmp_path / "bad_failed.csv"
        bad_csv.write_text("sample_id,error\n0001,fail\n")

        def _raise_csv_error(*args: object, **kwargs: object):
            raise pd.errors.ParserError("simulated CSV parse error")

        monkeypatch.setattr(pd, "read_csv", _raise_csv_error)

        with caplog.at_level(logging.WARNING, logger="osimflow.exporters.r_dataframe"):
            outputs = exporter.export_failures(failed_csv=bad_csv)

        assert outputs == {}
        assert any("Could not read failed_simulations.csv" in r.message for r in caplog.records)


class TestExportTimeseriesCorruptedSource:
    """Tests for export_timeseries error paths (lines 244-246)."""

    def test_export_timeseries_parquet_read_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Lines 244-246: pd.read_parquet error is logged and returns empty outputs."""
        import logging

        exporter = RDataFrameExporter(outdir=tmp_path, format="parquet")

        bad_parquet = tmp_path / "bad_ts.parquet"
        bad_parquet.write_bytes(b"not a parquet file")

        with caplog.at_level(logging.WARNING, logger="osimflow.exporters.r_dataframe"):
            outputs = exporter.export_timeseries(timeseries_parquet=bad_parquet)

        assert outputs == {}
        assert any("Could not read timeseries" in r.message for r in caplog.records)
