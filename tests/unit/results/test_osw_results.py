"""Unit tests for osimflow.osw_results and its KPI/aggregation wiring (issue #1871)."""

import json
from pathlib import Path

from osimflow import work
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


def test_extract_kpis_opt_in(tmp_path: Path) -> None:
    (tmp_path / "out.osw").write_text(json.dumps(OK_OSW))
    out = tmp_path / "k.json"
    run_extract_kpis(tmp_path, "0001", out)
    assert not any("." in k for k in json.loads(out.read_text())["kpis"])
    run_extract_kpis(tmp_path, "0001", out, kpis=["reporting_179_d.out_*"])
    assert json.loads(out.read_text())["kpis"] == {
        "reporting_179_d.out_total_electricity_179_d_gj": 12.5
    }
    run_extract_kpis(tmp_path, "0001", out, kpis=["measure_results"])
    assert "reporting_179_d.label" in json.loads(out.read_text())["kpis"]


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
