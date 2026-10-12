"""Tests for failed_simulations.csv extraction in aggregation module (issue #1161).

These tests verify that the distributed aggregation path in osimflow.aggregation
produces the same failed_simulations.csv output as the local bin/aggregate_results.py
path, including correct column order, error_summary extraction, and failure_category
classification.
"""

import pytest

from osimflow._work_scripts.aggregate_results import CATEGORY_SUGGESTIONS, _classify_line
from osimflow.aggregation import (
    FAILED_SIMULATIONS_COLUMNS,
    AggregatedManifest,
    compile_aggregation,
    parse_manifest,
)


class TestFailedSimulationsCSVEtraction:
    """Tests for failed_simulations.csv generation in compile_aggregation."""

    def test_empty_failure_list_produces_header_only_csv(self):
        """Empty failure list should produce header-only CSV matching FAILED_SIMULATIONS_COLUMNS."""
        manifests = []
        result = compile_aggregation(manifests, lambda k: None)

        # Should have header-only CSV with all expected columns
        lines = result.failed_simulations_csv.strip().splitlines()
        assert len(lines) == 1
        assert lines[0] == ",".join(FAILED_SIMULATIONS_COLUMNS)

    def test_single_failure_produces_correct_columns(self):
        """Single failed manifest should produce row with all FAILED_SIMULATIONS_COLUMNS."""
        manifest = AggregatedManifest(
            sample_id="sample_001",
            index=0,
            status="failed",
            kpis_key=None,
            exit_code=1,
            first_severe_error="  * Severe  Some EnergyPlus error message",
            finished_at=1234567890.0,
        )
        manifests = [manifest]

        def kpi_fetcher(key):
            return None

        result = compile_aggregation(manifests, kpi_fetcher)

        lines = result.failed_simulations_csv.strip().splitlines()
        assert len(lines) == 2  # header + 1 data row

        header = lines[0]
        data = lines[1]
        assert header == ",".join(FAILED_SIMULATIONS_COLUMNS)

        # Parse data row
        cols = data.split(",")
        assert len(cols) == len(FAILED_SIMULATIONS_COLUMNS)

        # Check specific columns
        col_dict = dict(zip(FAILED_SIMULATIONS_COLUMNS, cols, strict=False))
        assert col_dict["sample_id"] == "sample_001"
        assert col_dict["root_cause_line"] == "  * Severe  Some EnergyPlus error message"
        assert col_dict["error_summary"] == "  * Severe  Some EnergyPlus error message"
        assert col_dict["exit_code"] == "1"
        assert col_dict["total_severe_errors"] == "1"
        assert col_dict["log_path"] == ""

        # failure_category should be classified
        expected_category = _classify_line("  * Severe  Some EnergyPlus error message")
        assert col_dict["failure_category"] == expected_category

        # diagnosis_suggestion should come from CATEGORY_SUGGESTIONS
        expected_suggestion = CATEGORY_SUGGESTIONS.get(
            expected_category, CATEGORY_SUGGESTIONS["generic_severe"]
        )
        assert col_dict["diagnosis_suggestion"] == expected_suggestion

    def test_multiple_failures_all_classified(self):
        """Multiple failed manifests should all be classified correctly."""
        manifests = [
            AggregatedManifest(
                sample_id="sample_001",
                index=0,
                status="failed",
                kpis_key=None,
                exit_code=1,
                first_severe_error="  * Severe  ** Node temperature out of bounds **",
                finished_at=1234567890.0,
            ),
            AggregatedManifest(
                sample_id="sample_002",
                index=1,
                status="failed",
                kpis_key=None,
                exit_code=2,
                first_severe_error="  * Severe  ** RootFinder did not converge **",
                finished_at=1234567891.0,
            ),
            AggregatedManifest(
                sample_id="sample_003",
                index=2,
                status="failed",
                kpis_key=None,
                exit_code=1,
                first_severe_error="  * Severe  Some generic error",
                finished_at=1234567892.0,
            ),
        ]

        def kpi_fetcher(key):
            return None

        result = compile_aggregation(manifests, kpi_fetcher)

        lines = result.failed_simulations_csv.strip().splitlines()
        assert len(lines) == 4  # header + 3 data rows

        # Verify each failure has correct classification
        for i, line in enumerate(lines[1:]):
            cols = line.split(",")
            col_dict = dict(zip(FAILED_SIMULATIONS_COLUMNS, cols, strict=False))
            manifest = manifests[i]
            expected_category = _classify_line(manifest.first_severe_error)
            assert col_dict["failure_category"] == expected_category
            assert col_dict["error_summary"] == manifest.first_severe_error
            assert col_dict["root_cause_line"] == manifest.first_severe_error
            assert col_dict["sample_id"] == manifest.sample_id
            assert col_dict["exit_code"] == str(manifest.exit_code)

    def test_ok_manifest_with_missing_kpis_becomes_failure(self):
        """Manifest with status=ok but missing kpis.json should become failure (criterion #5)."""
        manifest = AggregatedManifest(
            sample_id="sample_001",
            index=0,
            status="ok",  # Claims success
            kpis_key="s3://bucket/kpis.json",
            exit_code=0,
            first_severe_error=None,
            finished_at=1234567890.0,
        )

        def kpi_fetcher(key):
            # Simulate missing kpis.json
            return None

        result = compile_aggregation([manifest], kpi_fetcher)

        # Should have 0 ok, 1 failed (degraded)
        assert result.ok_count == 0
        assert result.failed_count == 1
        assert result.degraded_ok_samples == ["sample_001"]

        # Check failure row has the special error summary
        lines = result.failed_simulations_csv.strip().splitlines()
        assert len(lines) == 2

        col_dict = dict(zip(FAILED_SIMULATIONS_COLUMNS, lines[1].split(","), strict=False))
        assert col_dict["sample_id"] == "sample_001"
        assert (
            col_dict["error_summary"]
            == "kpis.json missing (manifest claimed status=ok but no KPIs retrievable)"
        )
        assert col_dict["failure_category"] == "generic_severe"
        assert col_dict["total_severe_errors"] == "0"  # No severe error recorded

    def test_ok_manifest_with_valid_kpis_stays_ok(self):
        """Manifest with status=ok and valid kpis.json should stay in aggregated_results."""
        manifest = AggregatedManifest(
            sample_id="sample_001",
            index=0,
            status="completed",
            kpis_key="s3://bucket/kpis.json",
            exit_code=0,
            first_severe_error=None,
            finished_at=1234567890.0,
        )

        def kpi_fetcher(key):
            return {"kpis": {"eui": 150.5, "peak_cooling": 12.3}}

        result = compile_aggregation([manifest], kpi_fetcher)

        # Should have 1 ok, 0 failed
        assert result.ok_count == 1
        assert result.failed_count == 0
        assert result.degraded_ok_samples == []

        # aggregated_results.csv should have the sample
        ok_lines = result.aggregated_results_csv.strip().splitlines()
        assert len(ok_lines) == 2  # header + 1 data row
        assert "sample_001" in ok_lines[1]
        assert "150.5" in ok_lines[1]

        # failed_simulations.csv should be header-only
        fail_lines = result.failed_simulations_csv.strip().splitlines()
        assert len(fail_lines) == 1
        assert fail_lines[0] == ",".join(FAILED_SIMULATIONS_COLUMNS)

    def test_mixed_ok_and_failed(self):
        """Mix of successful and failed samples should produce both CSVs correctly."""
        manifests = [
            AggregatedManifest(
                sample_id="sample_001",
                index=0,
                status="completed",
                kpis_key="s3://bucket/kpis1.json",
                exit_code=0,
                first_severe_error=None,
                finished_at=1234567890.0,
            ),
            AggregatedManifest(
                sample_id="sample_002",
                index=1,
                status="failed",
                kpis_key=None,
                exit_code=1,
                first_severe_error="  * Severe  Temperature out of range",
                finished_at=1234567891.0,
            ),
            AggregatedManifest(
                sample_id="sample_003",
                index=2,
                status="completed",
                kpis_key="s3://bucket/kpis3.json",
                exit_code=0,
                first_severe_error=None,
                finished_at=1234567892.0,
            ),
        ]

        def kpi_fetcher(key):
            if "kpis1" in key:
                return {"kpis": {"eui": 100.0}}
            if "kpis3" in key:
                return {"kpis": {"eui": 200.0}}
            return None

        result = compile_aggregation(manifests, kpi_fetcher)

        assert result.ok_count == 2
        assert result.failed_count == 1
        assert result.total_count == 3

        # aggregated_results.csv should have 2 rows + header
        ok_lines = result.aggregated_results_csv.strip().splitlines()
        assert len(ok_lines) == 3

        # failed_simulations.csv should have 1 failure + header
        fail_lines = result.failed_simulations_csv.strip().splitlines()
        assert len(fail_lines) == 2

        # Verify the failed sample
        col_dict = dict(zip(FAILED_SIMULATIONS_COLUMNS, fail_lines[1].split(","), strict=False))
        assert col_dict["sample_id"] == "sample_002"
        assert col_dict["error_summary"] == "  * Severe  Temperature out of range"

    def test_failure_category_consistency_with_local_classifier(self):
        """Failure categories should match _classify_line from aggregate_results.py."""
        test_cases = [
            ("  * Severe  ** Node temperature out of bounds **", "timestep_instability"),
            ("  * Severe  ** RootFinder did not converge **", "convergence"),
            ("  * Severe  ** EnergyPlus Warmup Error **", "generic_severe"),
            ("  * Severe  ** Sizing calculation failed **", "hvac_sizing"),
            ("  * Severe  Some completely unknown error", "generic_severe"),
        ]

        for error_line, expected_category in test_cases:
            manifest = AggregatedManifest(
                sample_id="sample_001",
                index=0,
                status="failed",
                kpis_key=None,
                exit_code=1,
                first_severe_error=error_line,
                finished_at=1234567890.0,
            )

            result = compile_aggregation([manifest], lambda k: None)
            lines = result.failed_simulations_csv.strip().splitlines()
            col_dict = dict(zip(FAILED_SIMULATIONS_COLUMNS, lines[1].split(","), strict=False))
            assert col_dict["failure_category"] == expected_category, f"Failed for: {error_line}"

    def test_parse_manifest_handles_various_status_values(self):
        """parse_manifest should accept both 'ok' and 'completed' status values."""
        for status in ["ok", "completed", "success", "succeeded", "failed", "error"]:
            raw = {
                "sample_id": "sample_001",
                "index": 0,
                "status": status,
                "kpis_key": "s3://bucket/kpis.json",
                "exit_code": 0 if status != "failed" else 1,
                "first_severe_error": "  * Severe  Test error" if status == "failed" else None,
                "finished_at": 1234567890.0,
            }
            manifest = parse_manifest(raw)
            if status in ("ok", "completed", "success", "succeeded"):
                assert manifest.status.lower() in ("ok", "completed", "success", "succeeded")
            else:
                assert manifest.status == status

    def test_multi_objective_algorithm_still_produces_failure_csv(self):
        """Multi-objective algorithms (nsga2, pso) should still produce correct failure CSV."""
        manifests = [
            AggregatedManifest(
                sample_id="sample_001",
                index=0,
                status="failed",
                kpis_key=None,
                exit_code=1,
                first_severe_error="  * Severe  Convergence failed",
                finished_at=1234567890.0,
            ),
        ]

        def kpi_fetcher(key):
            return None

        result = compile_aggregation(manifests, kpi_fetcher, algorithm="nsga2")

        # Should still produce failed_simulations.csv
        lines = result.failed_simulations_csv.strip().splitlines()
        assert len(lines) == 2
        assert lines[0] == ",".join(FAILED_SIMULATIONS_COLUMNS)

        col_dict = dict(zip(FAILED_SIMULATIONS_COLUMNS, lines[1].split(","), strict=False))
        assert col_dict["sample_id"] == "sample_001"
        assert col_dict["failure_category"] == "generic_severe"

        # Pareto JSON should be None since no successful samples
        assert result.pareto_json is None


class TestParseManifest:
    """Tests for parse_manifest function."""

    def test_parse_manifest_with_all_fields(self):
        raw = {
            "sample_id": "sample_001",
            "index": 5,
            "status": "completed",
            "kpis_key": "s3://bucket/kpis.json",
            "exit_code": 0,
            "first_severe_error": "  * Severe  Test",
            "finished_at": 1234567890.5,
        }
        manifest = parse_manifest(raw)
        assert manifest.sample_id == "sample_001"
        assert manifest.index == 5
        assert manifest.status == "completed"
        assert manifest.kpis_key == "s3://bucket/kpis.json"
        assert manifest.exit_code == 0
        assert manifest.first_severe_error == "  * Severe  Test"
        assert manifest.finished_at == 1234567890.5

    def test_parse_manifest_with_missing_optional_fields(self):
        raw = {"sample_id": "sample_002"}
        manifest = parse_manifest(raw)
        assert manifest.sample_id == "sample_002"
        assert manifest.index == 0
        assert manifest.status == "failed"
        assert manifest.kpis_key is None
        assert manifest.exit_code == 0
        assert manifest.first_severe_error is None
        assert manifest.finished_at is None

    def test_parse_manifest_with_string_finished_at(self):
        raw = {"sample_id": "sample_001", "finished_at": "2024-01-01T00:00:00Z"}
        manifest = parse_manifest(raw)
        assert manifest.finished_at is None  # Non-numeric becomes None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
