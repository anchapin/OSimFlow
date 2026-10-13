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
