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

# workdir, template_pkg, outdir, and preseeded_outdir fixtures come from
# conftest.py.  The preseeded_outdir fixture is session-scoped (runs a
# 3-sample campaign once per xdist worker) to avoid ~36s of repeated
# campaign setup.


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
    with pytest.raises(FileNotFoundError, match="(?i)samples.json not found"):
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
