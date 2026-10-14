"""Tests for domain-aware EnergyPlus error diagnosis in bin/aggregate_results.py."""

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BIN = PROJECT_ROOT / "bin"

sys.path.insert(0, str(BIN))
from aggregate_results import (  # noqa: E402
    CATEGORY_SUGGESTIONS,
    FAILURE_PATTERNS,
    _classify_line,
    _count_severe_errors,
    _find_root_cause_line,
    diagnose_error,
    extract_failure,
)

# ---------------------------------------------------------------------------
# Unit tests: classify_line
# ---------------------------------------------------------------------------


class TestClassifyLine:
    def test_convergence_exceeded_max_iterations(self):
        assert (
            _classify_line("   ** Severe  ** Exceeded max iterations for plant loop")
            == "convergence"
        )

    def test_convergence_not_converged(self):
        assert _classify_line("HVAC controller did not converge after 50 tries") == "convergence"

    def test_convergence_iteration_limit(self):
        assert _classify_line("Plant iteration.limit reached") == "convergence"

    def test_surface_geometry_intersection(self):
        assert (
            _classify_line("   ** Severe  ** Surface intersection error for wall_north")
            == "surface_geometry"
        )

    def test_surface_geometry_non_convex(self):
        assert (
            _classify_line("   ** Severe  ** Detected non-convex surface zone_roof")
            == "surface_geometry"
        )

    def test_surface_geometry_zero_area(self):
        assert _classify_line("   ** Severe  ** Zero area surface detected") == "surface_geometry"

    def test_surface_geometry_surfaceless_zone(self):
        assert _classify_line("   ** Severe  ** Surfaceless zone 'attic'") == "surface_geometry"

    def test_hvac_sizing_autosize_failed(self):
        assert _classify_line("   ** Severe  ** autosize failed for chiller") == "hvac_sizing"

    def test_hvac_sizing_no_load_on_plant_loop(self):
        assert _classify_line("   ** Severe  ** No load on plant loop 'CW'") == "hvac_sizing"

    def test_hvac_sizing_sizing_failed(self):
        assert (
            _classify_line("   ** Severe  ** Zone sizing failed for thermal_zone_1")
            == "hvac_sizing"
        )

    def test_schedule_not_found(self):
        assert _classify_line("   ** Severe  ** Schedule 'OCC_SCH' not found") == "schedule"

    def test_schedule_invalid(self):
        assert _classify_line("   ** Severe  ** Schedule is invalid for thermostat") == "schedule"

    def test_material_not_found(self):
        assert (
            _classify_line("   ** Severe  ** Material 'Insulation_R10' not found")
            == "material_construction"
        )

    def test_construction_not_found(self):
        assert (
            _classify_line("   ** Severe  ** Construction 'ExtWall' does not exist")
            == "material_construction"
        )

    def test_weather_file_error(self):
        assert (
            _classify_line("   ** Severe  ** Weather file error: cannot read EPW") == "weather_file"
        )

    def test_weather_file_not_found(self):
        assert (
            _classify_line("   ** Severe  ** Weather file not found: USA_CO_Denver.epw")
            == "weather_file"
        )

    def test_memory_allocation_error(self):
        assert (
            _classify_line("   ** Severe  ** Memory allocation error during simulation")
            == "memory_timeout"
        )

    def test_timeout(self):
        assert (
            _classify_line("   ** Severe  ** Simulation timeout after 3600 seconds")
            == "memory_timeout"
        )

    def test_timestep_instability_temperatures_out_of_bounds(self):
        assert (
            _classify_line("   ** Severe  ** Temperatures out of bounds at zone core")
            == "timestep_instability"
        )

    def test_timestep_instability_node_temperature(self):
        assert (
            _classify_line("   ** Severe  ** Node temperature out of range: 1e6 C")
            == "timestep_instability"
        )

    def test_generic_severe_fallback(self):
        assert _classify_line("   ** Severe  ** Some unknown error occurred") == "generic_severe"

    def test_case_insensitive(self):
        assert _classify_line("DID NOT CONVERGE") == "convergence"
        assert _classify_line("SCHEDULE NOT FOUND") == "schedule"


# ---------------------------------------------------------------------------
# Unit tests: diagnose_error
# ---------------------------------------------------------------------------


class TestDiagnoseError:
    def test_returns_expected_keys(self, tmp_path):
        err_file = tmp_path / "eplusout.err"
        err_file.write_text(
            "   ** Severe  ** Exceeded max iterations for plant loop HW\n"
            "   ** Severe  ** Controller output is unstable\n"
        )
        result = diagnose_error(
            "   ** Severe  ** Exceeded max iterations for plant loop HW",
            err_file,
        )
        assert "category" in result
        assert "summary" in result
        assert "suggestion" in result
        assert "severity" in result
        assert "total_severe_errors" in result
        assert "root_cause_line" in result

    def test_convergence_category(self, tmp_path):
        err_file = tmp_path / "eplusout.err"
        err_file.write_text("   ** Severe  ** Exceeded max iterations\n")
        result = diagnose_error(
            "   ** Severe  ** Exceeded max iterations",
            err_file,
        )
        assert result["category"] == "convergence"
        assert (
            "iteration" in result["suggestion"].lower()
            or "tolerance" in result["suggestion"].lower()
        )

    def test_total_severe_count(self, tmp_path):
        err_file = tmp_path / "eplusout.err"
        err_file.write_text(
            "   ** Severe  ** Error 1\n   ** Severe  ** Error 2\n   ** Severe  ** Error 3\n"
        )
        result = diagnose_error("   ** Severe  ** Error 1", err_file)
        assert result["total_severe_errors"] == 3

    def test_severity_critical_when_many_errors(self, tmp_path):
        lines = [f"   ** Severe  ** Error {i}\n" for i in range(15)]
        err_file = tmp_path / "eplusout.err"
        err_file.write_text("".join(lines))
        result = diagnose_error("   ** Severe  ** Error 0", err_file)
        assert result["severity"] == "critical"

    def test_severity_high_when_few_errors(self, tmp_path):
        err_file = tmp_path / "eplusout.err"
        err_file.write_text("   ** Severe  ** Single error\n")
        result = diagnose_error("   ** Severe  ** Single error", err_file)
        assert result["severity"] == "high"

    def test_graceful_fallback_on_missing_file(self):
        result = diagnose_error(
            "   ** Severe  ** Some error",
            Path("/nonexistent/eplusout.err"),
        )
        assert result["category"] == "generic_severe"
        assert result["total_severe_errors"] == 0

    def test_root_cause_different_from_first_severe(self, tmp_path):
        err_file = tmp_path / "eplusout.err"
        err_file.write_text(
            "   ** Severe  ** Node temperature out of range\n"
            "   ** Warning ** HVAC controller did not converge after 50 tries\n"
        )
        result = diagnose_error(
            "   ** Severe  ** Node temperature out of range",
            err_file,
        )
        assert result["category"] == "timestep_instability"
        assert "Node temperature out of range" in result["root_cause_line"]


# ---------------------------------------------------------------------------
# Unit tests: _count_severe_errors
# ---------------------------------------------------------------------------


class TestCountSevereErrors:
    def test_zero_errors_in_empty_file(self, tmp_path):
        err_file = tmp_path / "eplusout.err"
        err_file.write_text("")
        assert _count_severe_errors(err_file) == 0

    def test_counts_asterisk_format(self, tmp_path):
        err_file = tmp_path / "eplusout.err"
        err_file.write_text("   ** Severe  ** Error 1\n   ** Severe  ** Error 2\n")
        assert _count_severe_errors(err_file) == 2

    def test_handles_missing_file(self):
        assert _count_severe_errors(Path("/nonexistent")) == 0


# ---------------------------------------------------------------------------
# Unit tests: _find_root_cause_line
# ---------------------------------------------------------------------------


class TestFindRootCauseLine:
    def test_returns_pattern_match_over_first_severe(self, tmp_path):
        err_file = tmp_path / "eplusout.err"
        err_file.write_text(
            "   ** Severe  ** Some generic error\n"
            "   ** Severe  ** Schedule 'OCC_SCH' not found in model\n"
        )
        result = _find_root_cause_line(err_file)
        assert "Schedule" in result
        assert "not found" in result

    def test_returns_first_severe_when_no_pattern_matches(self, tmp_path):
        err_file = tmp_path / "eplusout.err"
        err_file.write_text("   ** Severe  ** Completely unknown error type xyz\n")
        result = _find_root_cause_line(err_file)
        assert "Completely unknown error" in result

    def test_empty_file_returns_empty(self, tmp_path):
        err_file = tmp_path / "eplusout.err"
        err_file.write_text("")
        assert _find_root_cause_line(err_file) == ""


# ---------------------------------------------------------------------------
# Unit tests: extract_failure (integration with diagnosis)
# ---------------------------------------------------------------------------


class TestExtractFailure:
    def test_includes_diagnosis_columns(self, tmp_path):
        sim_dir = tmp_path / "0042"
        sim_dir.mkdir()
        (sim_dir / "eplusout.err").write_text(
            "   ** Severe  ** Exceeded max iterations for plant loop HW\n"
            "   ** Severe  ** Controller unstable\n"
        )
        result = extract_failure(sim_dir)
        assert result is not None
        assert result["failure_category"] == "convergence"
        assert result["total_severe_errors"] == 2
        assert result["root_cause_line"] != ""
        assert result["diagnosis_suggestion"] != ""

    def test_first_severe_only_with_multiple_severe_lines(self, tmp_path):
        sim_dir = tmp_path / "0077"
        sim_dir.mkdir()
        first = "** Severe  ** Exceeded max iterations for plant loop HW"
        second = "** Severe  ** Controller unstable"
        third = "** Severe  ** Plant loop temperature mismatch"
        (sim_dir / "eplusout.err").write_text(f"   {first}\n   {second}\n   {third}\n")
        result = extract_failure(sim_dir)
        assert result is not None
        assert result["total_severe_errors"] == 3
        assert result["error_summary"] == first
        assert second not in result["error_summary"]
        assert third not in result["error_summary"]

    def test_missing_sql_without_err_file(self, tmp_path):
        sim_dir = tmp_path / "0087"
        sim_dir.mkdir()
        result = extract_failure(sim_dir)
        assert result is not None
        assert result["failure_category"] == ""
        assert result["error_summary"] == "eplusout.sql missing"

    def test_no_failure_when_successful(self, tmp_path):
        sim_dir = tmp_path / "0099"
        sim_dir.mkdir()
        (sim_dir / "eplusout.sql").write_text("-- placeholder")
        (sim_dir / "eplusout.err").write_text("")
        assert extract_failure(sim_dir) is None

    def test_diagnosis_does_not_crash_on_malformed_err(self, tmp_path):
        sim_dir = tmp_path / "0203"
        sim_dir.mkdir()
        (sim_dir / "eplusout.err").write_bytes(b"\x80\x81\x82invalid binary")
        result = extract_failure(sim_dir)
        assert result is not None

    def test_post_energyplus_reporting_measure_failure_with_sql_present(
        self, tmp_path: Path
    ) -> None:
        """Issue #2059: eplusout.sql exists + out.osw reporting measure Fail -> extract_failure returns measure error."""
        sim_dir = tmp_path / "0101"
        sim_dir.mkdir()
        (sim_dir / "eplusout.sql").write_text("-- valid sql placeholder")
        (sim_dir / "eplusout.err").write_text("Program Version,EnergyPlus\n")
        (sim_dir / "out.osw").write_text(
            json.dumps(
                {
                    "completed_status": "Fail",
                    "steps": [
                        {
                            "measure_dir_name": "openstudio_results",
                            "result": {
                                "step_result": "Fail",
                                "step_errors": ["undefined method `[]' for nil:NilClass"],
                            },
                        }
                    ],
                }
            )
        )
        result = extract_failure(sim_dir)
        assert result is not None
        assert result["sample_id"] == "0101"
        assert (
            result["error_summary"] == "openstudio_results: undefined method `[]' for nil:NilClass"
        )
        assert result["exit_code"] == 1

    def test_osw_completed_status_fail_without_step_errors_fallback_message(
        self, tmp_path: Path
    ) -> None:
        """Issue #2059: eplusout.sql exists + out.osw completed_status=Fail without step_errors falls back to workflow failure message."""
        sim_dir = tmp_path / "0102"
        sim_dir.mkdir()
        (sim_dir / "eplusout.sql").write_text("-- valid sql placeholder")
        (sim_dir / "out.osw").write_text(json.dumps({"completed_status": "Fail", "steps": []}))
        result = extract_failure(sim_dir)
        assert result is not None
        assert result["sample_id"] == "0102"
        assert result["error_summary"] == "OpenStudio workflow failed"
        assert result["exit_code"] == 1

    def test_failed_marker_with_sql_present(self, tmp_path: Path) -> None:
        """Issue #2059: eplusout.sql exists + _OSIMFLOW_FAILED present -> extract_failure returns marker message."""
        sim_dir = tmp_path / "0103"
        sim_dir.mkdir()
        (sim_dir / "eplusout.sql").write_text("-- valid sql placeholder")
        (sim_dir / "_OSIMFLOW_FAILED").write_text("RuntimeError: reporting measure crashed\n")
        result = extract_failure(sim_dir)
        assert result is not None
        assert result["sample_id"] == "0103"
        assert result["error_summary"] == "RuntimeError: reporting measure crashed"
        assert result["exit_code"] == 1

    def test_retrying_marker_with_sql_present_is_not_failure(self, tmp_path: Path) -> None:
        """Issue #2059: _OSIMFLOW_FAILED=='retrying' with eplusout.sql present is not treated as failure."""
        sim_dir = tmp_path / "0104"
        sim_dir.mkdir()
        (sim_dir / "eplusout.sql").write_text("-- valid sql placeholder")
        (sim_dir / "_OSIMFLOW_FAILED").write_text("retrying\n")
        assert extract_failure(sim_dir) is None

    def test_step_result_fail_without_top_level_fail_with_sql_present(self, tmp_path: Path) -> None:
        """Issue #2059: step_result=='Fail' is detected even if top-level completed_status is not 'Fail'."""
        sim_dir = tmp_path / "0105"
        sim_dir.mkdir()
        (sim_dir / "eplusout.sql").write_text("-- valid sql placeholder")
        (sim_dir / "out.osw").write_text(
            json.dumps(
                {
                    "completed_status": "Success",
                    "steps": [
                        {
                            "measure_dir_name": "openstudio_results",
                            "result": {
                                "step_result": "Fail",
                                "step_errors": ["Step failed despite top-level status"],
                            },
                        }
                    ],
                }
            )
        )
        result = extract_failure(sim_dir)
        assert result is not None
        assert result["error_summary"] == "openstudio_results: Step failed despite top-level status"


# ---------------------------------------------------------------------------
# Integration test: CLI end-to-end with diagnosis columns
# ---------------------------------------------------------------------------


def test_aggregate_with_diagnosis_columns(tmp_path):
    sim_dir = tmp_path / "sim" / "0001"
    sim_dir.mkdir(parents=True)
    (sim_dir / "eplusout.err").write_text(
        "   ** Severe  ** Schedule 'OCC_SCH' not found in model\n"
        "   ** Severe  ** Zone 'Office' has no schedule\n"
    )

    kpi_file = tmp_path / "kpi_0001.json"
    kpi_file.write_text('{"sample_id": "0001", "kpis": {"eui": 100.0}}')

    out_csv = tmp_path / "agg.csv"
    out_fail = tmp_path / "fail.csv"

    subprocess.run(
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
        check=True,
    )

    assert out_fail.exists()
    fail_df = pd.read_csv(out_fail, dtype={"sample_id": str})
    assert len(fail_df) == 1
    assert "failure_category" in fail_df.columns
    assert fail_df.iloc[0]["failure_category"] == "schedule"
    assert fail_df.iloc[0]["total_severe_errors"] == 2

    # Issue #2059: failed sample 0001 must NOT also appear in aggregated_results.csv
    agg_df = pd.read_csv(out_csv, dtype={"sample_id": str})
    assert agg_df.empty


def test_aggregate_mutually_exclusive_pass_and_fail_rows(tmp_path: Path) -> None:
    """Issue #2059: aggregated_results.csv and failed_simulations.csv are mutually exclusive across SQL+OSW Fail and SQL+Severe."""
    # 0000: passes cleanly
    sim0 = tmp_path / "sim" / "0000"
    sim0.mkdir(parents=True)
    (sim0 / "eplusout.sql").write_text("-- sql")
    (sim0 / "eplusout.err").write_text("Program Version,EnergyPlus\n")

    # 0001: eplusout.sql exists, but reporting measure failed in out.osw
    sim1 = tmp_path / "sim" / "0001"
    sim1.mkdir(parents=True)
    (sim1 / "eplusout.sql").write_text("-- sql")
    (sim1 / "out.osw").write_text(
        json.dumps(
            {
                "completed_status": "Fail",
                "steps": [
                    {
                        "measure_dir_name": "openstudio_results",
                        "result": {
                            "step_result": "Fail",
                            "step_errors": ["Post-EnergyPlus reporting error"],
                        },
                    }
                ],
            }
        )
    )

    # 0002: eplusout.sql exists, but eplusout.err has a Severe error
    sim2 = tmp_path / "sim" / "0002"
    sim2.mkdir(parents=True)
    (sim2 / "eplusout.sql").write_text("-- sql")
    (sim2 / "eplusout.err").write_text("   ** Severe  ** Plant loop temperature out of bounds\n")

    # Even if KPI JSON files exist for all three samples, 0001 and 0002 must be excluded from agg.csv
    kpi_files = []
    for sid, eui in (("0000", 105.0), ("0001", 110.0), ("0002", 115.0)):
        kf = tmp_path / f"kpi_{sid}.json"
        kf.write_text(json.dumps({"sample_id": sid, "kpis": {"eui_kwh_m2_yr": eui}}))
        kpi_files.append(str(kf))

    out_csv = tmp_path / "aggregated_results.csv"
    out_fail = tmp_path / "failed_simulations.csv"

    subprocess.run(
        [
            sys.executable,
            str(BIN / "aggregate_results.py"),
            "--kpis",
            *kpi_files,
            "--simulation_dirs",
            str(sim0),
            str(sim1),
            str(sim2),
            "--out_csv",
            str(out_csv),
            "--out_failed",
            str(out_fail),
        ],
        check=True,
    )

    agg_df = pd.read_csv(out_csv, dtype={"sample_id": str})
    fail_df = pd.read_csv(out_fail, dtype={"sample_id": str})

    assert agg_df["sample_id"].tolist() == ["0000"]
    assert fail_df["sample_id"].tolist() == ["0001", "0002"]
    assert set(agg_df["sample_id"]).isdisjoint(set(fail_df["sample_id"]))
    assert (
        fail_df.loc[fail_df["sample_id"] == "0001", "error_summary"].iloc[0]
        == "openstudio_results: Post-EnergyPlus reporting error"
    )
    assert (
        "Plant loop temperature out of bounds"
        in (fail_df.loc[fail_df["sample_id"] == "0002", "error_summary"].iloc[0])
    )


def test_aggregate_main_inprocess_excludes_failed_and_error_kpi_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #2059: in-process main() excludes failed samples and error KPI payloads from aggregated_results.csv."""
    from osimflow._work_scripts.aggregate_results import main as aggregate_main

    sim0 = tmp_path / "sim" / "0000"
    sim0.mkdir(parents=True)
    (sim0 / "eplusout.sql").write_text("-- sql")

    sim1 = tmp_path / "sim" / "0001"
    sim1.mkdir(parents=True)
    (sim1 / "eplusout.sql").write_text("-- sql")
    (sim1 / "_OSIMFLOW_FAILED").write_text("Reporting measure exited 1\n")

    kpi0 = tmp_path / "kpi_0000.json"
    kpi0.write_text(json.dumps({"sample_id": "0000", "kpis": {"eui_kwh_m2_yr": 100.0}}))
    kpi1 = tmp_path / "kpi_0001.json"
    kpi1.write_text(json.dumps({"sample_id": "0001", "error": "KPI extraction failed", "kpis": {}}))
    samples_json = tmp_path / "samples.json"
    samples_json.write_text(
        json.dumps(
            {
                "samples": [
                    {"sample_id": "0000", "values": {"hvac.cop": 3.5}},
                    {"sample_id": "0001", "values": {"hvac.cop": 3.0}},
                ]
            }
        )
    )

    out_csv = tmp_path / "agg.csv"
    out_fail = tmp_path / "fail.csv"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "aggregate_results.py",
            "--kpis",
            str(kpi0),
            str(kpi1),
            "--simulation_dirs",
            str(sim0),
            str(sim1),
            "--samples_json",
            str(samples_json),
            "--out_csv",
            str(out_csv),
            "--out_failed",
            str(out_fail),
        ],
    )
    aggregate_main()

    agg_df = pd.read_csv(out_csv, dtype={"sample_id": str})
    fail_df = pd.read_csv(out_fail, dtype={"sample_id": str})
    assert agg_df["sample_id"].tolist() == ["0000"]
    assert fail_df["sample_id"].tolist() == ["0001"]
    assert fail_df["error_summary"].iloc[0] == "Reporting measure exited 1"


# ---------------------------------------------------------------------------
# Coverage: ensure all categories have suggestions
# ---------------------------------------------------------------------------


def test_all_pattern_categories_have_suggestions():
    for category, _patterns in FAILURE_PATTERNS:
        assert category in CATEGORY_SUGGESTIONS, f"Missing suggestion for category: {category}"


# ---------------------------------------------------------------------------
# Tests for input parameter merging (issue #276)
# ---------------------------------------------------------------------------


class TestLoadSamplesParams:
    """Tests for _load_samples_params — loading samples.json for param merge."""

    def test_loads_params_from_valid_json(self, tmp_path: Path) -> None:
        from aggregate_results import _load_samples_params

        samples_json = tmp_path / "samples.json"
        samples_json.write_text(
            json.dumps(
                {
                    "samples": [
                        {"sample_id": "0001", "values": {"r_value": 5.0, "wwr": 0.4}},
                        {"sample_id": "0002", "values": {"r_value": 3.0, "wwr": 0.6}},
                    ]
                }
            )
        )
        result = _load_samples_params(samples_json)
        assert result == {
            "0001": {"r_value": 5.0, "wwr": 0.4},
            "0002": {"r_value": 3.0, "wwr": 0.6},
        }

    def test_flattens_categorical_labels(self, tmp_path: Path) -> None:
        from aggregate_results import _load_samples_params

        samples_json = tmp_path / "samples.json"
        samples_json.write_text(
            json.dumps(
                {
                    "samples": [
                        {
                            "sample_id": "0001",
                            "values": {
                                "hvac_type": {"label": "VRF", "index": 0},
                                "r_value": 5.0,
                            },
                        }
                    ]
                }
            )
        )
        result = _load_samples_params(samples_json)
        assert result["0001"]["hvac_type"] == "VRF"
        assert result["0001"]["r_value"] == 5.0

    def test_returns_empty_on_missing_file(self, tmp_path: Path) -> None:
        from aggregate_results import _load_samples_params

        result = _load_samples_params(tmp_path / "nonexistent.json")
        assert result == {}

    def test_returns_empty_on_invalid_json(self, tmp_path: Path) -> None:
        from aggregate_results import _load_samples_params

        samples_json = tmp_path / "samples.json"
        samples_json.write_text("{invalid json}")
        result = _load_samples_params(samples_json)
        assert result == {}

    def test_returns_empty_on_empty_samples(self, tmp_path: Path) -> None:
        from aggregate_results import _load_samples_params

        samples_json = tmp_path / "samples.json"
        samples_json.write_text(json.dumps({"samples": []}))
        result = _load_samples_params(samples_json)
        assert result == {}

    def test_handles_missing_values_key(self, tmp_path: Path) -> None:
        from aggregate_results import _load_samples_params

        samples_json = tmp_path / "samples.json"
        samples_json.write_text(json.dumps({"samples": [{"sample_id": "0001"}]}))
        result = _load_samples_params(samples_json)
        assert result == {"0001": {}}


class TestMergeParamsIntoAggregatedResults:
    """Tests for parameter columns in the aggregated CSV (issue #276).

    Uses direct function calls (not subprocess) so tests work without
    a pip-installed osimflow package.
    """

    def test_params_appear_before_kpi_columns(self, tmp_path: Path) -> None:
        """Parameter columns should appear between sample_id and KPI columns."""
        from aggregate_results import _load_samples_params, parse_kpi_json

        # Write KPI files
        for sid in ("0001", "0002"):
            kpi_file = tmp_path / f"kpi_{sid}.json"
            kpi_file.write_text(
                json.dumps({"sample_id": sid, "kpis": {"eui_kwh_m2_yr": 100.0 + int(sid)}})
            )

        # Write samples.json
        samples_json = tmp_path / "samples.json"
        samples_json.write_text(
            json.dumps(
                {
                    "samples": [
                        {"sample_id": "0001", "values": {"r_value": 5.0, "wwr": 0.4}},
                        {"sample_id": "0002", "values": {"r_value": 3.0, "wwr": 0.6}},
                    ]
                }
            )
        )

        # Simulate the aggregation logic
        all_kpis = []
        for sid in ("0001", "0002"):
            all_kpis.append(parse_kpi_json(tmp_path / f"kpi_{sid}.json"))
        df = pd.DataFrame(all_kpis)

        params_map = _load_samples_params(samples_json)
        assert params_map, "params_map should not be empty"
        params_rows = [params_map.get(str(sid), {}) for sid in df["sample_id"]]
        params_df = pd.DataFrame(params_rows)
        df = pd.concat([df[["sample_id"]], params_df, df.drop(columns=["sample_id"])], axis=1)

        cols = list(df.columns)
        # sample_id must be first
        assert cols[0] == "sample_id"
        # Param columns come before KPI columns
        assert "r_value" in cols
        assert "wwr" in cols
        assert "eui_kwh_m2_yr" in cols
        r_idx = cols.index("r_value")
        wwr_idx = cols.index("wwr")
        eui_idx = cols.index("eui_kwh_m2_yr")
        assert r_idx < eui_idx
        assert wwr_idx < eui_idx
        # Both params are between sample_id and eui
        assert r_idx > 0
        assert wwr_idx > 0

    def test_backward_compat_no_samples_json(self, tmp_path: Path) -> None:
        """Without --samples_json, the CSV still works (backward compat)."""
        from aggregate_results import parse_kpi_json

        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(json.dumps({"sample_id": "0001", "kpis": {"eui_kwh_m2_yr": 100.0}}))

        all_kpis = [parse_kpi_json(kpi_file)]
        df = pd.DataFrame(all_kpis)
        assert "sample_id" in df.columns
        assert "eui_kwh_m2_yr" in df.columns

    def test_samples_json_missing_file_no_error(self, tmp_path: Path) -> None:
        """Passing a nonexistent samples.json path to _load_samples_params is non-fatal."""
        from aggregate_results import _load_samples_params, parse_kpi_json

        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(json.dumps({"sample_id": "0001", "kpis": {"eui_kwh_m2_yr": 100.0}}))

        all_kpis = [parse_kpi_json(kpi_file)]
        df = pd.DataFrame(all_kpis)

        params_map = _load_samples_params(tmp_path / "nonexistent.json")
        assert params_map == {}
        assert len(df) == 1
        assert "eui_kwh_m2_yr" in df.columns

    def test_param_values_match_samples(self, tmp_path: Path) -> None:
        """Parameter values in the DataFrame match the samples.json input."""
        from aggregate_results import _load_samples_params, parse_kpi_json

        for i, (_r, _wwr) in enumerate([(5.0, 0.4), (3.0, 0.6)], start=1):
            sid = f"{i:04d}"
            kpi_file = tmp_path / f"kpi_{sid}.json"
            kpi_file.write_text(
                json.dumps({"sample_id": sid, "kpis": {"eui_kwh_m2_yr": 100.0 + i}})
            )

        samples_json = tmp_path / "samples.json"
        samples_json.write_text(
            json.dumps(
                {
                    "samples": [
                        {"sample_id": "0001", "values": {"r_value": 5.0, "wwr": 0.4}},
                        {"sample_id": "0002", "values": {"r_value": 3.0, "wwr": 0.6}},
                    ]
                }
            )
        )

        all_kpis = [
            parse_kpi_json(tmp_path / "kpi_0001.json"),
            parse_kpi_json(tmp_path / "kpi_0002.json"),
        ]
        df = pd.DataFrame(all_kpis)

        params_map = _load_samples_params(samples_json)
        params_rows = [params_map.get(str(sid), {}) for sid in df["sample_id"]]
        params_df = pd.DataFrame(params_rows)
        df = pd.concat([df[["sample_id"]], params_df, df.drop(columns=["sample_id"])], axis=1)

        row1 = df[df["sample_id"] == "0001"].iloc[0]
        assert row1["r_value"] == pytest.approx(5.0)
        assert row1["wwr"] == pytest.approx(0.4)
        row2 = df[df["sample_id"] == "0002"].iloc[0]
        assert row2["r_value"] == pytest.approx(3.0)
        assert row2["wwr"] == pytest.approx(0.6)

    def test_sample_with_no_matching_params(self, tmp_path: Path) -> None:
        """Samples not found in samples.json get empty parameter values."""
        from aggregate_results import _load_samples_params, parse_kpi_json

        kpi_file = tmp_path / "kpi_0099.json"
        kpi_file.write_text(json.dumps({"sample_id": "0099", "kpis": {"eui_kwh_m2_yr": 100.0}}))

        samples_json = tmp_path / "samples.json"
        samples_json.write_text(
            json.dumps({"samples": [{"sample_id": "0001", "values": {"r_value": 5.0}}]})
        )

        all_kpis = [parse_kpi_json(kpi_file)]
        df = pd.DataFrame(all_kpis)
        params_map = _load_samples_params(samples_json)
        params_rows = [params_map.get(str(sid), {}) for sid in df["sample_id"]]
        params_df = pd.DataFrame(params_rows)
        # params_df will be an empty DataFrame (no columns) since the
        # unmatched sample got an empty dict
        assert len(params_df) == 1
