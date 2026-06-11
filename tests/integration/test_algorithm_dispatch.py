"""Integration tests for the ``--algorithm`` CLI flag dispatch.

Acceptance criteria (issue #128, G1b):

1. ``--algorithm lhs`` produces identical output to the default (no flag).
2. ``--algorithm nonexistent`` raises ``ValueError`` listing available algorithms.
3. ``AlgorithmRegistry.list_available()`` returns at least ``["lhs"]``.
4. ``--algorithm`` is threaded through ``CampaignConfig`` → ``Campaign.run()``
   and appears in ``run.json``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from osimflow import AlgorithmRegistry, Campaign, CampaignConfig
from osimflow.executors import LocalExecutor

# ---------------------------------------------------------------------------
# Fixtures — mirrors test_local_executor.py pattern
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"
EXAMPLE_VARS_YML = REPO_ROOT / "variables.yml"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(EXAMPLE_VARS_YML.read_text())
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


def _make_cfg(
    workdir: Path, template_pkg: Path, outdir: Path, *, algorithm: str = "lhs"
) -> CampaignConfig:
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.4.0",
        archive_intermediates=False,
        algorithm=algorithm,
    )


# ---------------------------------------------------------------------------
# Test 1: --algorithm lhs produces same output as default
# ---------------------------------------------------------------------------
def test_algorithm_lhs_matches_default(workdir: Path, template_pkg: Path, tmp_path: Path) -> None:
    """Running with ``--algorithm lhs`` must produce the same artifacts as
    running without the flag (the default is ``lhs``). Both campaigns should
    succeed and produce identical structural output (3 samples, all OK).
    """
    out_default = tmp_path / "out_default"
    out_lhs = tmp_path / "out_lhs"
    out_default.mkdir()
    out_lhs.mkdir()

    # Default (algorithm="lhs" by default)
    cfg_default = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=out_default,
        openstudio_version="3.4.0",
        archive_intermediates=False,
    )
    campaign_default = Campaign(cfg=cfg_default, executor=LocalExecutor(max_workers=3))
    result_default = campaign_default.run()

    # Explicit --algorithm lhs
    cfg_lhs = _make_cfg(workdir, template_pkg, out_lhs, algorithm="lhs")
    campaign_lhs = Campaign(cfg=cfg_lhs, executor=LocalExecutor(max_workers=3))
    result_lhs = campaign_lhs.run()

    # Both must succeed with 3 samples
    assert len(result_default["samples"]) == 3
    assert len(result_lhs["samples"]) == 3

    # Both must produce the 4 artifacts
    for outdir in (out_default, out_lhs):
        assert (outdir / "aggregated_results.csv").is_file()
        assert (outdir / "failed_simulations.csv").is_file()
        assert (outdir / "run.json").is_file()
        kpis = list((outdir / "work" / "kpis").glob("kpi_*.json"))
        assert len(kpis) == 3

    # run.json summaries must match
    trace_default = json.loads((out_default / "run.json").read_text())
    trace_lhs = json.loads((out_lhs / "run.json").read_text())

    assert trace_default["summary"]["n_samples"] == 3
    assert trace_lhs["summary"]["n_samples"] == 3
    assert trace_lhs["summary"]["n_succeeded"] == 3
    assert trace_lhs["summary"]["n_failed"] == 0

    # Both must have the same step names (GENERATE_LHS_SAMPLES)
    steps_default = {s["step"] for s in trace_default["steps"]}
    steps_lhs = {s["step"] for s in trace_lhs["steps"]}
    assert steps_default == steps_lhs
    assert "GENERATE_LHS_SAMPLES" in steps_lhs


# ---------------------------------------------------------------------------
# Test 2: --algorithm nonexistent raises ValueError with helpful message
# ---------------------------------------------------------------------------
def test_algorithm_nonexistent_raises_value_error(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Passing ``algorithm="nonexistent"`` must raise ``ValueError`` with a
    message listing available algorithms, not a generic import or config error.
    """
    cfg = _make_cfg(workdir, template_pkg, outdir, algorithm="nonexistent")
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))

    with pytest.raises(ValueError, match=r"unknown algorithm 'nonexistent'"):
        campaign.run()

    # Also verify the error path through AlgorithmRegistry directly
    with pytest.raises(ValueError, match=r"unknown algorithm 'no_such_algo'") as exc_info:
        AlgorithmRegistry.get("no_such_algo")

    error_msg = str(exc_info.value)
    # The error must list available algorithms (at least "lhs")
    assert "lhs" in error_msg


# ---------------------------------------------------------------------------
# Test 3: AlgorithmRegistry.list_available() returns at least ["lhs"]
# ---------------------------------------------------------------------------
def test_registry_lists_at_least_lhs() -> None:
    """``AlgorithmRegistry.list_available()`` must return at least ``["lhs"]``,
    since the built-in ``LHSAlgorithm`` is auto-registered at module load.
    """
    available = AlgorithmRegistry.list_available()
    assert "lhs" in available, f"expected 'lhs' in available algorithms, got {available}"
    # list_available returns sorted list
    assert available == sorted(available)


# ---------------------------------------------------------------------------
# Test 4: algorithm name appears in run.json config section
# ---------------------------------------------------------------------------
def test_algorithm_appears_in_run_json(workdir: Path, template_pkg: Path, outdir: Path) -> None:
    """The algorithm name must be recorded in ``run.json`` so users can
    audit which sampling strategy was used.
    """
    cfg = _make_cfg(workdir, template_pkg, outdir, algorithm="lhs")
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    campaign.run()

    trace = json.loads((outdir / "run.json").read_text())
    # The config section should carry the algorithm name
    assert trace["config"].get("algorithm") == "lhs"


# ---------------------------------------------------------------------------
# Test 5: algorithm field is threaded through CampaignConfig
# ---------------------------------------------------------------------------
def test_campaign_config_algorithm_default() -> None:
    """``CampaignConfig.algorithm`` must default to ``"lhs"``."""
    cfg = CampaignConfig(
        input_variables=Path("/tmp/vars.yml"),
        template_sim_package=Path("/tmp/pkg"),
        n_samples=1,
        outdir=Path("/tmp/out"),
        openstudio_version="3.4.0",
    )
    assert cfg.algorithm == "lhs"


def test_campaign_config_algorithm_explicit() -> None:
    """``CampaignConfig`` must accept an explicit ``algorithm`` value."""
    cfg = CampaignConfig(
        input_variables=Path("/tmp/vars.yml"),
        template_sim_package=Path("/tmp/pkg"),
        n_samples=1,
        outdir=Path("/tmp/out"),
        openstudio_version="3.4.0",
        algorithm="lhs",
    )
    assert cfg.algorithm == "lhs"
