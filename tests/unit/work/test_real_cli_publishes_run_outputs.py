"""The real-CLI path must publish <package>/run/eplusout.* into sim_out."""

from pathlib import Path
from unittest.mock import patch

from osimflow import work


def test_real_cli_copies_run_outputs_to_sim_out(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text("{}")
    sim_out = tmp_path / "sim" / "0001"
    sim_out.mkdir(parents=True)

    def fake_run(cmd: list[str], **kwargs: object) -> None:
        run = pkg / "run"
        run.mkdir()
        (run / "eplusout.sql").write_bytes(b"sql")
        (run / "eplusout.err").write_text("EnergyPlus Completed Successfully")

    with (
        patch.object(work, "run_subprocess", side_effect=fake_run),
        patch.object(work, "_get_openstudio_cmd", return_value="openstudio"),
    ):
        work._run_real_openstudio(
            modified_sim_package=pkg,
            sample_id="0001",
            sim_out=sim_out,
            stdout_path=sim_out / "stdout.log",
            stderr_path=sim_out / "stderr.log",
        )

    assert (sim_out / "eplusout.sql").read_bytes() == b"sql"
    assert "Completed Successfully" in (sim_out / "eplusout.err").read_text()


def test_has_valid_osw_completion_all_cases(tmp_path: Path) -> None:
    """Issue #2051: 5 cases for has_valid_osw_completion."""
    import json

    from osimflow.osw_results import has_valid_osw_completion

    # 1. Missing or malformed file -> False
    missing = tmp_path / "missing.osw"
    assert has_valid_osw_completion(missing) is False
    malformed = tmp_path / "malformed.osw"
    malformed.write_text("{not valid json")
    assert has_valid_osw_completion(malformed) is False

    # 2. Non-Success completed_status -> False
    non_success = tmp_path / "fail_status.osw"
    non_success.write_text(
        json.dumps(
            {
                "completed_status": "Fail",
                "steps": [
                    {
                        "measure_dir_name": "openstudio_results",
                        "result": {
                            "step_result": "Success",
                            "step_values": [{"name": "eui", "value": 42.0}],
                        },
                    }
                ],
            }
        )
    )
    assert has_valid_osw_completion(non_success) is False

    # 3. Failed step even when earlier step registered values -> False
    step_fail = tmp_path / "step_fail.osw"
    step_fail.write_text(
        json.dumps(
            {
                "completed_status": "Success",
                "steps": [
                    {
                        "measure_dir_name": "openstudio_results",
                        "result": {
                            "step_result": "Success",
                            "step_values": [{"name": "eui", "value": 42.0}],
                        },
                    },
                    {
                        "measure_dir_name": "ServerDirectoryCleanup",
                        "result": {
                            "step_result": "Fail",
                            "step_errors": ["cleanup crashed"],
                        },
                    },
                ],
            }
        )
    )
    assert has_valid_osw_completion(step_fail) is False

    # 4. Empty step values -> False
    empty_vals = tmp_path / "empty_vals.osw"
    empty_vals.write_text(
        json.dumps(
            {
                "completed_status": "Success",
                "steps": [
                    {
                        "measure_dir_name": "openstudio_results",
                        "result": {"step_result": "Success", "step_values": []},
                    }
                ],
            }
        )
    )
    assert has_valid_osw_completion(empty_vals) is False

    # 5. Valid Success + no failed steps + non-empty step values -> True
    valid_osw = tmp_path / "valid.osw"
    valid_osw.write_text(
        json.dumps(
            {
                "completed_status": "Success",
                "steps": [
                    {
                        "measure_dir_name": "openstudio_results",
                        "result": {
                            "step_result": "Success",
                            "step_values": [{"name": "total_site_energy_kbtu", "value": 1234.5}],
                        },
                    },
                    {
                        "measure_dir_name": "ServerDirectoryCleanup",
                        "result": {"step_result": "Success", "step_values": []},
                    },
                ],
            }
        )
    )
    assert has_valid_osw_completion(valid_osw) is True


def test_real_cli_succeeds_with_valid_out_osw_when_sql_deleted(tmp_path: Path) -> None:
    """Issue #2051: _run_real_openstudio succeeds when ServerDirectoryCleanup deleted eplusout.sql but out.osw is valid."""
    import json

    import pytest

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text("{}")
    sim_out = tmp_path / "sim" / "0001"
    sim_out.mkdir(parents=True)

    def fake_run_osw_only(cmd: list[str], **kwargs: object) -> None:
        (pkg / "out.osw").write_text(
            json.dumps(
                {
                    "completed_status": "Success",
                    "steps": [
                        {
                            "measure_dir_name": "openstudio_results",
                            "result": {
                                "step_result": "Success",
                                "step_values": [{"name": "site_eui", "value": 55.2}],
                            },
                        }
                    ],
                }
            )
        )

    with (
        patch.object(work, "run_subprocess", side_effect=fake_run_osw_only),
        patch.object(work, "_get_openstudio_cmd", return_value="openstudio"),
    ):
        res = work._run_real_openstudio(
            modified_sim_package=pkg,
            sample_id="0001",
            sim_out=sim_out,
            stdout_path=sim_out / "stdout.log",
            stderr_path=sim_out / "stderr.log",
        )
    assert res == sim_out
    assert (sim_out / "out.osw").is_file()
    assert not (sim_out / "eplusout.sql").exists()

    # Still raises RuntimeError when both eplusout.sql and valid out.osw completion are absent
    pkg_empty = tmp_path / "pkg_empty"
    pkg_empty.mkdir()
    (pkg_empty / "workflow.osw").write_text("{}")
    sim_empty = tmp_path / "sim" / "0002"
    sim_empty.mkdir(parents=True)

    def fake_run_empty(cmd: list[str], **kwargs: object) -> None:
        (pkg_empty / "out.osw").write_text(json.dumps({"completed_status": "Success", "steps": []}))

    with (
        patch.object(work, "run_subprocess", side_effect=fake_run_empty),
        patch.object(work, "_get_openstudio_cmd", return_value="openstudio"),
        pytest.raises(RuntimeError, match="produced no eplusout.sql"),
    ):
        work._run_real_openstudio(
            modified_sim_package=pkg_empty,
            sample_id="0002",
            sim_out=sim_empty,
            stdout_path=sim_empty / "stdout.log",
            stderr_path=sim_empty / "stderr.log",
        )
