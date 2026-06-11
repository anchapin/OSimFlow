"""Unit tests for --sample N mode (issue #59).

Verifies that --sample:
- Runs only the selected sample through steps 2-4
- Skips GENERATE_LHS_SAMPLES (reuses existing samples.json)
- Raises FileNotFoundError when no samples.json exists
- Raises IndexError when sample index is out of range
- Produces KPI file for exactly the selected sample
"""

import json
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"
EXAMPLE_VARS_YML = REPO_ROOT / "variables.yml"

# workdir, template_pkg, and outdir fixtures come from conftest.py.


@pytest.fixture
def preseeded_outdir(workdir: Path, template_pkg: Path, outdir: Path) -> Path:
    """Run a 3-sample campaign first so samples.json exists."""
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    campaign.run()
    return outdir


def test_sample_raises_when_no_samples_json(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        sample=0,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    with pytest.raises(FileNotFoundError, match="samples.json not found"):
        campaign.run()


def test_sample_raises_on_out_of_range_index(
    workdir: Path, template_pkg: Path, preseeded_outdir: Path
) -> None:
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=preseeded_outdir,
        openstudio_version="3.11.0",
        sample=99,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    with pytest.raises(IndexError, match="out of range"):
        campaign.run()


def test_sample_0_produces_one_kpi(
    workdir: Path, template_pkg: Path, preseeded_outdir: Path
) -> None:
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=preseeded_outdir,
        openstudio_version="3.11.0",
        sample=0,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    result = campaign.run()

    assert result["elapsed_s"] > 0
    assert result["aggregated"]["csv"] is None
    assert result["plots"] == []
    assert len(result["samples"]) == 1
    assert len(result["kpis"]) >= 1


def test_sample_selects_correct_sample(
    workdir: Path, template_pkg: Path, preseeded_outdir: Path
) -> None:
    samples_json = preseeded_outdir / "work" / "samples.json"
    all_samples = json.loads(samples_json.read_text())["samples"]
    target_id = all_samples[1]["sample_id"]

    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=preseeded_outdir,
        openstudio_version="3.11.0",
        sample=1,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    result = campaign.run()

    assert result["samples"][0]["sample_id"] == target_id


def test_sample_negative_index_raises(
    workdir: Path, template_pkg: Path, preseeded_outdir: Path
) -> None:
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=preseeded_outdir,
        openstudio_version="3.11.0",
        sample=-1,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    with pytest.raises(IndexError, match="out of range"):
        campaign.run()
