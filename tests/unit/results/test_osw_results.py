"""Unit tests for osimflow.osw_results and its KPI/aggregation wiring (issue #1871)."""

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from osimflow import work
from osimflow._work_scripts import extract_kpis as extract_kpis_mod
from osimflow._work_scripts.aggregate_results import extract_failure
from osimflow._work_scripts.extract_kpis import run_extract_kpis
from osimflow.osw_results import (
    collect_failure_message_from_run_log,
    extract_failure_message_from_osw,
    failure_message,
    parse_out_osw,
)

OK_OSW = {
    "steps": [
        {"measure_dir_name": "bar", "result": {"step_result": "Success", "step_values": []}},
        {
            "measure_dir_name": "reporting_179_d",
            "result": {
                "step_result": "Success",
                "step_values": [
                    {"name": "out_total_electricity_179_d_gj", "value": 12.5},
                    {"name": "label", "value": "x"},
                ],
            },
        },
    ]
}
FAIL_OSW = {
    "steps": [
        {
            "measure_dir_name": "win",
            "result": {"step_result": "Fail", "step_errors": ["bad R\nbacktrace line"]},
        }
    ]
}
RUN_LOG = "\n".join(
    [
        "[10:00:00 INFO] [openstudio.workflow.OSWorkflow] Measure 'win' started",
        "[10:00:01 ERROR] [openstudio.model.Model] Simulation did not finish; errors:",
        "[10:00:01 ERROR] zone too small",
        "[10:00:01 ERROR] UseWeatherFile' is selected in YearDescription",
        "[10:00:02 INFO] done",
    ]
)


def test_parse_out_osw(tmp_path: Path) -> None:
    p = tmp_path / "out.osw"
    p.write_text(json.dumps(OK_OSW))
    assert parse_out_osw(p) == {
        "reporting_179_d.out_total_electricity_179_d_gj": 12.5,
        "reporting_179_d.label": "x",
    }
    assert parse_out_osw(tmp_path / "nope.osw") == {}
    p.write_text("{not json")
    assert parse_out_osw(p) == {}


def test_osw_failure_message() -> None:
    assert extract_failure_message_from_osw(json.dumps(FAIL_OSW)) == "win: bad R"
    assert extract_failure_message_from_osw(json.dumps(OK_OSW)) is None
    assert extract_failure_message_from_osw("garbage") is None


def test_run_log_failure_message_filters_benign() -> None:
    msg = collect_failure_message_from_run_log(RUN_LOG)
    assert msg is not None and msg.startswith("win: ")
    assert "zone too small" in msg and "UseWeatherFile" not in msg
    assert collect_failure_message_from_run_log(
        "[t ERROR] x [openstudio.measure.OSRunner] boom"
    ) == ("x [openstudio.measure.OSRunner] boom")
    assert collect_failure_message_from_run_log("[t FATAL] crash") == "crash"
    assert collect_failure_message_from_run_log("[t INFO] fine") is None


def test_failure_message_prefers_run_log(tmp_path: Path) -> None:
    (tmp_path / "out.osw").write_text(json.dumps(FAIL_OSW))
    assert failure_message(tmp_path) == "win: bad R"
    (tmp_path / "run.log").write_text(RUN_LOG)
    assert "zone too small" in (failure_message(tmp_path) or "")


def _write_minimal_eplusout_sql(sql_path: Path) -> None:
    conn = sqlite3.connect(str(sql_path))
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE TabularDataWithStrings (
            ReportName TEXT,
            ReportForString TEXT,
            TableName TEXT,
            RowName TEXT,
            ColumnName TEXT,
            Units TEXT,
            Value TEXT
        )
        """
    )
    cur.executemany(
        "INSERT INTO TabularDataWithStrings VALUES (?,?,?,?,?,?,?)",
        [
            (
                "AnnualBuildingUtilityPerformanceSummary",
                "Entire Facility",
                "Site and Source Energy",
                "Total Site Energy",
                "Energy Per Total Building Area",
                "MJ/m2",
                "360.0",
            ),
            (
                "AnnualBuildingUtilityPerformanceSummary",
                "Entire Facility",
                "Site and Source Energy",
                "Total Site Energy",
                "Total Energy",
                "MJ",
                "36000.0",
            ),
        ],
    )
    cur.execute("CREATE TABLE Zones (ZoneIndex INTEGER, Floor_Area REAL)")
    cur.execute("INSERT INTO Zones VALUES (0, 100.0)")
    conn.commit()
    conn.close()


def test_extract_kpis_opt_in(tmp_path: Path) -> None:
    (tmp_path / "out.osw").write_text(json.dumps(OK_OSW))
    out = tmp_path / "k.json"
    # Default (kpis=None, custom_kpi_extractor=None) includes out.osw step values (issue #2028).
    run_extract_kpis(tmp_path, "0001", out)
    default_kpis = json.loads(out.read_text())["kpis"]
    assert default_kpis["reporting_179_d.out_total_electricity_179_d_gj"] == 12.5
    assert default_kpis["reporting_179_d.label"] == "x"

    # Explicit SQL-only allow-list excludes unrequested out.osw step values.
    run_extract_kpis(tmp_path, "0001", out, kpis=["eui_kwh_m2_yr"])
    assert json.loads(out.read_text())["kpis"] == {}

    # Explicit glob allow-list filters out.osw step values.
    run_extract_kpis(tmp_path, "0001", out, kpis=["reporting_179_d.out_*"])
    assert json.loads(out.read_text())["kpis"] == {
        "reporting_179_d.out_total_electricity_179_d_gj": 12.5
    }

    # Explicit measure_results token includes all out.osw step values.
    run_extract_kpis(tmp_path, "0001", out, kpis=["measure_results"])
    assert json.loads(out.read_text())["kpis"] == {
        "reporting_179_d.out_total_electricity_179_d_gj": 12.5,
        "reporting_179_d.label": "x",
    }


def test_extract_kpis_default_with_sql_and_out_osw(tmp_path: Path) -> None:
    _write_minimal_eplusout_sql(tmp_path / "eplusout.sql")
    (tmp_path / "out.osw").write_text(json.dumps(OK_OSW))
    out = tmp_path / "k.json"
    run_extract_kpis(tmp_path, "0001", out)
    payload = json.loads(out.read_text())
    kpis = payload["kpis"]
    assert kpis["eui_kwh_m2_yr"] == 100.0
    assert kpis["total_site_energy_kwh"] == 10000.0
    assert kpis["floor_area_m2"] == 100.0
    assert kpis["reporting_179_d.out_total_electricity_179_d_gj"] == 12.5
    assert kpis["reporting_179_d.label"] == "x"
    assert payload["quality"]["valid"] is True


def test_extract_kpis_collision_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_minimal_eplusout_sql(tmp_path / "eplusout.sql")
    (tmp_path / "out.osw").write_text(json.dumps(OK_OSW))
    monkeypatch.setattr(
        extract_kpis_mod,
        "parse_out_osw",
        lambda _p: {
            "eui_kwh_m2_yr": 999.0,
            "sample_id": "hijacked",
            "openstudio_version": "0.0.0",
            "units": "bad",
            "quality": "bad",
            "reporting_179_d.out_total_electricity_179_d_gj": 12.5,
        },
    )
    out = tmp_path / "k.json"
    with caplog.at_level(logging.WARNING, logger="extract_kpis"):
        run_extract_kpis(tmp_path, "0001", out, openstudio_version="3.10.0")
    payload = json.loads(out.read_text())
    kpis = payload["kpis"]
    assert kpis["eui_kwh_m2_yr"] == 100.0
    assert kpis["reporting_179_d.out_total_electricity_179_d_gj"] == 12.5
    for reserved in ("sample_id", "openstudio_version", "units", "quality"):
        assert reserved not in kpis
    assert payload["sample_id"] == "0001"
    assert payload["openstudio_version"] == "3.10.0"
    assert any("collides with built-in KPI or metadata key" in r.message for r in caplog.records)


def test_extract_kpis_custom_extractor_preserves_schema(tmp_path: Path) -> None:
    (tmp_path / "out.osw").write_text(json.dumps(OK_OSW))
    extractor = tmp_path / "custom_ext.py"
    extractor.write_text(
        "def extract_kpis(ctx):\n    return {'custom_eui': 77.7}\n",
        encoding="utf-8",
    )
    out = tmp_path / "k.json"
    run_extract_kpis(tmp_path, "0001", out, custom_kpi_extractor=extractor)
    assert json.loads(out.read_text())["kpis"] == {"custom_eui": 77.7}

    run_extract_kpis(
        tmp_path,
        "0001",
        out,
        custom_kpi_extractor=extractor,
        kpis=["custom_eui", "reporting_179_d.label"],
    )
    assert json.loads(out.read_text())["kpis"] == {
        "custom_eui": 77.7,
        "reporting_179_d.label": "x",
    }


def test_failed_simulations_uses_measure_message(tmp_path: Path) -> None:
    sim = tmp_path / "0001"
    sim.mkdir()
    (sim / "out.osw").write_text(json.dumps(FAIL_OSW))
    row = extract_failure(sim)
    assert row is not None and row["error_summary"] == "win: bad R"
    plain = tmp_path / "0002"
    plain.mkdir()
    row = extract_failure(plain)
    assert row is not None and row["error_summary"] == "eplusout.sql missing"


def test_publish_run_artifacts(tmp_path: Path) -> None:
    pkg, sim = tmp_path / "pkg", tmp_path / "sim"
    (pkg / "run").mkdir(parents=True)
    (pkg / "out.osw").write_text("{}")
    (pkg / "run" / "run.log").write_text("log")
    work._publish_run_artifacts(pkg, sim)
    assert (sim / "out.osw").read_text() == "{}" and (sim / "run.log").read_text() == "log"
    work._publish_run_artifacts(tmp_path / "empty", tmp_path / "sim2")
    assert not (tmp_path / "sim2").exists()


def test_publish_nested_run_log_fallback(tmp_path: Path) -> None:
    pkg, sim = tmp_path / "pkg", tmp_path / "sim"
    (pkg / "run" / "a" / "b" / "run").mkdir(parents=True)
    (pkg / "run" / "a" / "run").mkdir(parents=True)
    (pkg / "run" / "a" / "b" / "run" / "run.log").write_text("deep")
    (pkg / "run" / "a" / "run" / "run.log").write_text("shallow")
    work._publish_run_artifacts(pkg, sim)
    assert (sim / "run.log").read_text() == "shallow"


def test_reused_simulation_publishes_artifacts(tmp_path: Path) -> None:
    pkg, sim = tmp_path / "pkg", tmp_path / "sim"
    (pkg / "run").mkdir(parents=True)
    (pkg / "workflow.osw").write_text('{"steps": []}')
    (pkg / "run" / "eplusout.sql").write_text("x")
    (pkg / "out.osw").write_text(json.dumps(OK_OSW))
    work.run_openstudio_sim(pkg, "0001", "3.10.0", sim)
    assert (sim / "0001" / "out.osw").is_file()
