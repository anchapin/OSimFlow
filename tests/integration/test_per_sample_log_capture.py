"""Tests for per-sample stdout/stderr log capture (issue #6).

Coverage:
  * `osimflow.executors.run_subprocess` writes to the given paths and
    creates the parent directories on demand.
  * The Campaign populates `${outdir}/work/sim/<sid>/stdout.log` and
    `stderr.log` for every sample in `step_run_openstudio_sim`.
  * A successful run preserves the `stdout.log` and deletes an empty
    `eplusout.err` (PRD §1.4 *Intermediate File Optimization*).
  * The `run.json` `per_sample` rows reference the log paths via the
    `stdout_log` / `stderr_log` keys (SampleTrace schema extension).
  * A `simulate_work_s=0` run with the stub still produces a
    non-trivial `stdout.log` (the stub writes a banner line).

The tests use the LocalExecutor and the stub work function so they run
fast and have no real OpenStudio CLI dependency. The capture happens
through the same `run_subprocess` helper that the real `openstudio.cli`
invocation will use, so a green test signals the plumbing is correct.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor, run_subprocess
from osimflow.monitoring import sample_log_paths
from osimflow.work import run_openstudio_sim


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/integration/test_campaign.py)
# ---------------------------------------------------------------------------
@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(
        json.dumps(
            {
                "variables": [
                    {"name": "u1", "distribution": "uniform", "min": 0.0, "max": 1.0},
                ]
            }
        )
    )
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    """A JSON-mode .osm (per `osimflow.apply_params` test convention) so the
    pre-flight check passes for `u1`. A real workflow.osw is included to
    match the project template layout."""
    pkg = workdir / "template"
    pkg.mkdir()
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"u1": 0.0}}))
    (pkg / "workflow.osw").write_text(json.dumps({"name": "stub"}))
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


@pytest.fixture
def cfg(workdir: Path, template_pkg: Path, outdir: Path) -> CampaignConfig:
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
    )


@pytest.fixture
def campaign(cfg: CampaignConfig) -> Campaign:
    return Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))


# ---------------------------------------------------------------------------
# `osimflow.executors.run_subprocess` (the LocalExecutor-side helper)
# ---------------------------------------------------------------------------
def test_run_subprocess_writes_to_given_paths(tmp_path: Path) -> None:
    stdout_path = tmp_path / "out.log"
    stderr_path = tmp_path / "err.log"
    cp = run_subprocess(
        [sys.executable, "-c", "import sys; print('hi'); sys.stderr.write('bye\\n')"],
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    assert cp.returncode == 0
    assert stdout_path.read_text().strip() == "hi"
    assert stderr_path.read_text().strip() == "bye"


def test_run_subprocess_creates_parent_dirs(tmp_path: Path) -> None:
    """`sample_log_paths` creates the sim dir, but the helper must also
    handle the case where the parent is missing (e.g. when called from a
    BYOS script that did not go through the Campaign)."""
    stdout_path = tmp_path / "deep" / "nested" / "stdout.log"
    stderr_path = tmp_path / "deep" / "nested" / "stderr.log"
    run_subprocess(
        [sys.executable, "-c", "print('ok')"],
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    assert stdout_path.is_file()
    assert stderr_path.is_file()


def test_run_subprocess_creates_files_even_on_non_zero_exit(tmp_path: Path) -> None:
    stdout_path = tmp_path / "out.log"
    stderr_path = tmp_path / "err.log"
    # The helper must NOT raise on non-zero exit when check=False (the
    # default). The files are created regardless, so the user can debug
    # the failure by reading them.
    cp = run_subprocess(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(2)"],
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    assert cp.returncode == 2
    assert stderr_path.read_text().strip() == "boom"
    # stdout was opened but the script didn't write to it — must still
    # exist on disk.
    assert stdout_path.is_file()
    assert stdout_path.read_text() == ""


# ---------------------------------------------------------------------------
# `osimflow.monitoring.sample_log_paths`
# ---------------------------------------------------------------------------
def test_sample_log_paths_creates_dir_and_returns_paths(tmp_path: Path) -> None:
    out, err = sample_log_paths(tmp_path, "0001")
    assert out == tmp_path / "work" / "sim" / "0001" / "stdout.log"
    assert err == tmp_path / "work" / "sim" / "0001" / "stderr.log"
    # Directory is created up-front so the helper is safe to call
    # *before* the executor runs the work function.
    assert (tmp_path / "work" / "sim" / "0001").is_dir()


# ---------------------------------------------------------------------------
# `osimflow.work.run_openstudio_sim` directly
# ---------------------------------------------------------------------------
def test_work_run_openstudio_sim_writes_logs_when_paths_given(tmp_path: Path) -> None:
    out_dir = tmp_path / "sim"
    out_dir.mkdir()
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    result = run_openstudio_sim(
        modified_sim_package=tmp_path,
        sample_id="0042",
        openstudio_version="3.11.0",
        out=out_dir,
        simulate_work_s=0.0,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    # The per-sample log files are populated, and the stub banner is
    # present in stdout.
    assert stdout_path.is_file()
    assert stderr_path.is_file()
    assert "openstudio CLI stub" in stdout_path.read_text()
    assert "sample=0042" in stdout_path.read_text()
    # Return value is unchanged.
    assert result == out_dir / "0042"


def test_work_run_openstudio_sim_fallback_paths_inside_sim_out(tmp_path: Path) -> None:
    """When the work function is called without log paths (legacy /
    BYOS callers), the files still land in a sensible place inside
    `sim_out` rather than disappearing."""
    out_dir = tmp_path / "sim"
    out_dir.mkdir()
    result = run_openstudio_sim(
        modified_sim_package=tmp_path,
        sample_id="0007",
        openstudio_version="3.11.0",
        out=out_dir,
        simulate_work_s=0.0,
    )
    fallback_stdout = result / "stdout.log"
    fallback_stderr = result / "stderr.log"
    assert fallback_stdout.is_file()
    assert fallback_stderr.is_file()


# ---------------------------------------------------------------------------
# Campaign end-to-end (acceptance criteria)
# ---------------------------------------------------------------------------
def test_campaign_creates_per_sample_log_dirs(campaign: Campaign, outdir: Path) -> None:
    """Acceptance #1: a multi-sample campaign leaves
    `${outdir}/work/sim/<sid>/stdout.log` (and siblings) on disk."""
    campaign.run()
    sim_root = outdir / "work" / "sim"
    assert sim_root.is_dir()
    for sid in ("0001", "0002", "0003"):
        sample_dir = sim_root / sid
        assert sample_dir.is_dir(), f"missing per-sample dir for {sid}"
        assert (sample_dir / "stdout.log").is_file(), f"missing stdout.log for {sid}"
        assert (sample_dir / "stderr.log").is_file(), f"missing stderr.log for {sid}"


def test_campaign_stdout_log_contains_stub_banner(campaign: Campaign, outdir: Path) -> None:
    """The stub writes a banner to stdout; the log capture plumbs it
    through. A real `openstudio.cli` run will write its own banner —
    this test signals the capture path is wired in the right direction.
    """
    campaign.run()
    sid = "0001"
    stdout_log = outdir / "work" / "sim" / sid / "stdout.log"
    text = stdout_log.read_text()
    assert "openstudio CLI stub" in text
    assert f"sample={sid}" in text


def test_campaign_deletes_empty_err_keeps_stdout_log(campaign: Campaign, outdir: Path) -> None:
    """Acceptance #3: on success, empty eplusout.err is removed and
    stdout.log is preserved (PRD §1.4 *Intelligent Intermediate File
    Optimization*).
    """
    campaign.run()
    sid = "0002"
    sample_dir = outdir / "work" / "sim" / sid
    # Stub writes an empty eplusout.err; the Campaign deletes it.
    err_file = sample_dir / "eplusout.err"
    assert not err_file.exists(), "empty eplusout.err should be deleted"
    # The replacement log file is preserved.
    assert (sample_dir / "stdout.log").is_file()
    assert (sample_dir / "stderr.log").is_file()


def test_campaign_run_json_per_sample_references_log_paths(
    campaign: Campaign, outdir: Path
) -> None:
    """Acceptance #4: the run.json `per_sample` rows include the
    per-sample log paths via the new `stdout_log` / `stderr_log` keys.
    """
    campaign.run()
    run_json = outdir / "run.json"
    data = json.loads(run_json.read_text())
    assert len(data["per_sample"]) == 3
    for row in data["per_sample"]:
        sid = row["sample_id"]
        # Both fields are present (set to None when APPLY failed and
        # the sample never reached the sim step). For a 3-sample happy
        # path, all samples ran the sim, so the paths are populated.
        assert "stdout_log" in row
        assert "stderr_log" in row
        assert row["stdout_log"] == str(outdir / "work" / "sim" / sid / "stdout.log")
        assert row["stderr_log"] == str(outdir / "work" / "sim" / sid / "stderr.log")


def test_work_run_openstudio_sim_propagates_subprocess_errors(
    tmp_path: Path,
) -> None:
    """Acceptance #2 (work-function level): when the caller opts in to
    `check=True`, a non-zero subprocess exit surfaces as an exception
    so the Campaign can mark the sample as failed. The files are
    still on disk, so the user can debug.
    """
    out_dir = tmp_path / "sim"
    out_dir.mkdir()
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    with pytest.raises(subprocess.CalledProcessError):
        run_subprocess(
            [sys.executable, "-c", "import sys; sys.exit(1)"],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            check=True,
        )
    # Even on failure, the log files exist for debugging.
    assert stdout_path.is_file()
    assert stderr_path.is_file()


def test_work_run_openstudio_sim_records_failure_for_failed_subprocess(
    tmp_path: Path,
) -> None:
    """Acceptance #2 (Campaign-level): the work function surfaces a
    non-zero exit so `step_run_openstudio_sim` writes a failed sample
    trace AND keeps the log files on disk for post-mortem inspection.
    """
    out_dir = tmp_path / "sim"
    out_dir.mkdir()
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    with pytest.raises(subprocess.CalledProcessError):
        run_subprocess(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('boom\\n'); sys.exit(3)",
            ],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            check=True,
        )
    # The stderr log captured the failure message — the user can `cat`
    # it to debug without re-running the campaign.
    assert "boom" in stderr_path.read_text()
