"""Integration tests for the ``--archive_intermediates`` flag (issue #34).

Acceptance criteria:

1. When ``archive_intermediates=True``, the campaign produces:
   - ``${outdir}/archive/inputs/`` with copies of ``template_sim_package/``
     and ``variables.yml``
   - ``${outdir}/archive/apply/<sample_id>/`` with modified ``.osw``/``.osm``
   - ``${outdir}/archive/sim/<sample_id>/`` with ``eplusout.sql``
2. When ``archive_intermediates=False`` (default), no ``${outdir}/archive/``
   directory is created.
"""

import json
import shutil
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor

# ---------------------------------------------------------------------------
# Fixtures: reuse the same pattern as test_local_executor.py
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    # Use a variable that maps to a vendored measure argument
    # (SetEnvelopePerformance.wwr). #1486 added the measures/ directory
    # under example_package, so the preflight measure-validation step now
    # runs strictly and rejects plain .osm-attribute-only variable names
    # like window_u_value. The archive-intermediates test exercises the
    # Campaign's archive plumbing, not parameter resolution, so any valid
    # measure argument is acceptable here.
    (wd / "variables.yml").write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: wwr\n"
        "    distribution: uniform\n"
        "    min: 0.2\n"
        "    max: 0.6\n"
        "    measure_argument: SetEnvelopePerformance.wwr\n"
    )
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


def _make_cfg(workdir: Path, template_pkg: Path, outdir: Path, *, archive: bool) -> CampaignConfig:
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=archive,
    )


# ---------------------------------------------------------------------------
# Test 1: archive_intermediates=True produces all archive directories
# ---------------------------------------------------------------------------
def test_archive_intermediates_true_produces_archive_dirs(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """A 3-sample campaign with ``archive_intermediates=True`` must create
    the archive directory tree with the correct contents."""
    cfg = _make_cfg(workdir, template_pkg, outdir, archive=True)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    campaign.run()

    archive_root = outdir / "archive"
    assert archive_root.is_dir(), f"archive root missing: {archive_root}"

    # --- archive/inputs/ ---
    inputs_dir = archive_root / "inputs"
    assert inputs_dir.is_dir(), f"archive/inputs missing: {inputs_dir}"

    # template_sim_package copied
    pkg_copy = inputs_dir / template_pkg.name
    assert pkg_copy.is_dir(), f"template_sim_package copy missing: {pkg_copy}"
    # Verify contents: model.osm and workflow.osw from example_package
    assert (pkg_copy / "model.osm").is_file(), "model.osm missing from archived template"
    assert (pkg_copy / "workflow.osw").is_file(), "workflow.osw missing from archived template"

    # input_variables file copied
    vars_copy = inputs_dir / "variables.yml"
    assert vars_copy.is_file(), f"variables.yml copy missing: {vars_copy}"
    # Content must match the original
    assert vars_copy.read_text() == (workdir / "variables.yml").read_text()

    # --- archive/apply/<sample_id>/ ---
    apply_dir = archive_root / "apply"
    assert apply_dir.is_dir(), f"archive/apply missing: {apply_dir}"

    # There should be 3 sample dirs
    apply_samples = sorted(apply_dir.iterdir())
    assert len(apply_samples) == 3, f"expected 3 apply sample dirs, got {len(apply_samples)}"

    for sample_dir in apply_samples:
        assert sample_dir.is_dir()
        # Each must contain .osw and .osm files (from the stub apply)
        osw_files = list(sample_dir.glob("*.osw"))
        osm_files = list(sample_dir.glob("*.osm"))
        assert len(osw_files) >= 1, f"no .osw in {sample_dir}"
        assert len(osm_files) >= 1, f"no .osm in {sample_dir}"

    # --- archive/sim/<sample_id>/ ---
    sim_dir = archive_root / "sim"
    assert sim_dir.is_dir(), f"archive/sim missing: {sim_dir}"

    sim_samples = sorted(sim_dir.iterdir())
    assert len(sim_samples) == 3, f"expected 3 sim sample dirs, got {len(sim_samples)}"

    for sample_dir in sim_samples:
        assert sample_dir.is_dir()
        sql = sample_dir / "eplusout.sql"
        assert sql.is_file(), f"eplusout.sql missing from {sample_dir}"

    # --- Verify sample IDs match across apply and sim ---
    apply_ids = {p.name for p in apply_samples}
    sim_ids = {p.name for p in sim_samples}
    assert apply_ids == sim_ids, f"apply/sim sample ID mismatch: {apply_ids} vs {sim_ids}"

    # --- Verify the run.json was still produced (no regression) ---
    run_json = outdir / "run.json"
    assert run_json.is_file(), "run.json missing after archiving campaign"
    trace = json.loads(run_json.read_text())
    assert trace["summary"]["n_samples"] == 3
    assert trace["summary"]["n_succeeded"] == 3
    assert trace["config"]["archive_intermediates"] is True


# ---------------------------------------------------------------------------
# Test 2: archive_intermediates=False (default) produces no archive dir
# ---------------------------------------------------------------------------
def test_archive_intermediates_false_no_archive_dir(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """A campaign with ``archive_intermediates=False`` (default) must not
    create any ``archive/`` directory."""
    cfg = _make_cfg(workdir, template_pkg, outdir, archive=False)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    campaign.run()

    archive_root = outdir / "archive"
    assert not archive_root.exists(), (
        f"archive/ directory must NOT exist when archive_intermediates=False, "
        f"but found: {archive_root}"
    )

    # Standard artifacts must still exist (no regression)
    assert (outdir / "aggregated_results.csv").is_file()
    assert (outdir / "failed_simulations.csv").is_file()
    assert (outdir / "run.json").is_file()
