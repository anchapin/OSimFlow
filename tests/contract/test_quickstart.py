"""Quickstart staleness test (issue #191).

Runs the exact README quickstart command in stub mode and asserts the
expected output artifacts exist. This ensures the README never drifts
from the actual CLI surface — if a flag is renamed or an output path
changes, this test fails.

The test uses ``OSIMFLOW_STUB_SIM=1`` so it runs without the real
OpenStudio CLI installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.contract
def test_readme_quickstart_produces_expected_artifacts(tmp_path: Path) -> None:
    """The README quickstart command must exit 0 and produce the three
    expected output artifacts: ``aggregated_results.csv``, ``run.json``,
    and the ``plots/`` directory.
    """
    outdir = tmp_path / "results"
    outdir.mkdir()

    env = {**os.environ, "OSIMFLOW_STUB_SIM": "1"}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "osimflow",
            "run",
            "--executor",
            "local",
            "--input_variables",
            str(REPO_ROOT / "variables.yml"),
            "--template_sim_package",
            str(REPO_ROOT / "example_package"),
            "--n_samples",
            "5",
            "--outdir",
            str(outdir),
            "--openstudio_version",
            "3.11.0",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )

    assert result.returncode == 0, (
        f"Quickstart command failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # Expected output artifacts from the README quickstart table.
    csv_path = outdir / "aggregated_results.csv"
    run_json = outdir / "run.json"
    plots_dir = outdir / "plots"

    assert csv_path.is_file(), f"Missing expected artifact: {csv_path}"
    assert run_json.is_file(), f"Missing expected artifact: {run_json}"
    assert plots_dir.is_dir(), f"Missing expected artifact: {plots_dir}"
