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
