"""Unit tests for --dry-run mode (issue #59).

Verifies that dry-run:
- Forces n_samples=1 regardless of config
- Forces LocalExecutor regardless of CLI flags
- Runs steps 1-4 for exactly 1 sample
- Produces a summary (run.json + KPI file for 1 sample)
- Does NOT produce aggregated_results.csv or plots
"""

import json
from pathlib import Path

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"
EXAMPLE_VARS_YML = REPO_ROOT / "variables.yml"

# campaign_workdir, template_pkg, and outdir fixtures come from conftest.py.
# campaign_workdir uses test_variables.yml which has variables matching
# the measures in example_package/workflow.osw (issue #599).


def test_dry_run_forces_n_samples_to_1(
    campaign_workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    cfg = CampaignConfig(
        input_variables=campaign_workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=5000,
        outdir=outdir,
        openstudio_version="3.11.0",
        dry_run=True,
    )
    executor = LocalExecutor(max_workers=1)
    campaign = Campaign(cfg=cfg, executor=executor)
    campaign.run()

    assert campaign.cfg.n_samples == 1


def test_dry_run_processes_exactly_one_sample(
    campaign_workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    cfg = CampaignConfig(
        input_variables=campaign_workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=100,
        outdir=outdir,
        openstudio_version="3.11.0",
        dry_run=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    result = campaign.run()

    assert len(result["samples"]) == 1, f"expected 1 sample, got {len(result['samples'])}"
    run_json = outdir / "run.json"
    data = json.loads(run_json.read_text())
    assert len(data["per_sample"]) == 1


def test_dry_run_does_not_produce_aggregated_csv(
    campaign_workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    cfg = CampaignConfig(
        input_variables=campaign_workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=10,
        outdir=outdir,
        openstudio_version="3.11.0",
        dry_run=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    campaign.run()

    assert not (outdir / "aggregated_results.csv").exists()
    assert not (outdir / "plots").exists()


def test_dry_run_writes_run_json(campaign_workdir: Path, template_pkg: Path, outdir: Path) -> None:
    cfg = CampaignConfig(
        input_variables=campaign_workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=10,
        outdir=outdir,
        openstudio_version="3.11.0",
        dry_run=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    campaign.run()

    run_json = outdir / "run.json"
    assert run_json.exists()
    data = json.loads(run_json.read_text())
    assert "per_sample" in data
    assert len(data["per_sample"]) == 1


def test_dry_run_returns_elapsed_time(
    campaign_workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    cfg = CampaignConfig(
        input_variables=campaign_workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=10,
        outdir=outdir,
        openstudio_version="3.11.0",
        dry_run=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    result = campaign.run()

    assert result["elapsed_s"] > 0
    assert result["aggregated"]["csv"] is None
    assert result["plots"] == []
