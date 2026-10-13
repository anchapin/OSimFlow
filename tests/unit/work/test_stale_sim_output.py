"""Regression test for issue #1961 — stale sim/<sid>/eplusout.sql must not be reused."""

from pathlib import Path
from unittest.mock import patch

from osimflow import work as work_mod


def test_stale_eplusout_sql_is_cleared_before_simulating(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    out = tmp_path / "sim"
    sim_out = out / "0001"
    sim_out.mkdir(parents=True)
    stale = sim_out / "eplusout.sql"
    stale.write_text("STALE")

    with (
        patch.dict("os.environ", {"OSIMFLOW_STUB_SIM": "1"}),
        patch("osimflow.work._is_openstudio_available", return_value=False),
        patch("osimflow.work._raise_missing_sql") as missing,
        patch("osimflow.work.run_subprocess") as run_sub,
    ):
        work_mod._run_openstudio_sim_impl(
            modified_sim_package=pkg,
            sample_id="0001",
            openstudio_version="3.11.0",
            out=out,
            simulate_work_s=0.0,
        )

    assert not stale.exists() or stale.read_bytes() != b"STALE"
    assert run_sub.called or missing.called


def test_reuse_returns_true_when_sql_already_in_sim_out(tmp_path: Path) -> None:
    from osimflow._work_artifacts import _reuse_existing_simulation_output

    sim_out = tmp_path / "sim"
    sim_out.mkdir()
    (sim_out / "eplusout.sql").write_text("x")
    assert _reuse_existing_simulation_output(tmp_path / "pkg", sim_out, "0001")


def test_clear_stale_keeps_directories_and_other_files(tmp_path: Path) -> None:
    from osimflow._work_artifacts import _clear_stale_simulation_output

    (tmp_path / "eplusout.d").mkdir()
    (tmp_path / "eplusout.err").write_text("x")
    (tmp_path / "keep.txt").write_text("x")
    _clear_stale_simulation_output(tmp_path)
    assert (tmp_path / "eplusout.d").is_dir()
    assert not (tmp_path / "eplusout.err").exists()
    assert (tmp_path / "keep.txt").exists()
