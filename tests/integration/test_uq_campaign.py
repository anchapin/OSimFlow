"""Integration tests for UQ (uncertainty quantification) algorithm campaign (issue #530).

These tests cover the ``COMPUTE_UQ_INDICES`` step that runs when
``cfg.algorithm == "uq"``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(
        yaml.safe_dump(
            {
                "variables": [
                    {"name": "u1", "distribution": "uniform", "min": 0.0, "max": 1.0},
                    {"name": "u2", "distribution": "uniform", "min": 10.0, "max": 20.0},
                ]
            }
        )
    )
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    pkg.mkdir()
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"u1": 0.5, "u2": 15.0}}))
    (pkg / "workflow.osw").write_text(json.dumps({"name": "stub"}))
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


@pytest.fixture
def custom_kpi_extractor_file(workdir: Path) -> Path:
    f = workdir / "stub_kpis.py"
    f.write_text(
        "from pathlib import Path\n"
        "import json\n"
        "import random\n"
        "\n"
        "def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:\n"
        "    out.mkdir(parents=True, exist_ok=True)\n"
        "    kpi_path = out / f'kpi_{sample_id}.json'\n"
        "    x = 0.5 + 0.3 * (hash(sample_id) % 1000) / 1000.0\n"
        "    eui = 100.0 + 20.0 * x + random.gauss(0, 1)\n"
        "    kpi_path.write_text(json.dumps({\n"
        "        'sample_id': sample_id,\n"
        "        'kpis': {\n"
        "            'eui': round(eui, 3),\n"
        "            'total_site_energy_kwh': round(eui * 100, 3),\n"
        "            'peak_demand_kw': round(50 + 10 * x, 2),\n"
        "        }\n"
        "    }))\n"
        "    return kpi_path\n"
    )
    return f


@pytest.fixture
def uq_cfg(
    workdir: Path, template_pkg: Path, outdir: Path, custom_kpi_extractor_file: Path
) -> CampaignConfig:
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        algorithm="uq",
        custom_kpi_extractor=custom_kpi_extractor_file,
        archive_intermediates=False,
        skip_preflight=True,
    )


def test_uq_campaign_produces_uq_results_json(uq_cfg: CampaignConfig) -> None:
    """A campaign with algorithm=uq must produce uq_results.json."""
    campaign = Campaign(cfg=uq_cfg, executor=LocalExecutor(max_workers=2))
    result = campaign.run()

    uq_file = uq_cfg.outdir / "uq" / "uq_results.json"
    assert uq_file.is_file(), (
        f"uq_results.json not found at {uq_file}; run.json: {uq_cfg.outdir / 'run.json'}"
    )
    data = json.loads(uq_file.read_text())
    assert data.get("algorithm") == "uq"
    assert "distributions" in data or "confidence_intervals" in data


def test_uq_step_appears_in_run_json(uq_cfg: CampaignConfig, outdir: Path) -> None:
    """Verify COMPUTE_UQ_INDICES step appears in run.json."""
    campaign = Campaign(cfg=uq_cfg, executor=LocalExecutor(max_workers=2))
    campaign.run()

    run_json = outdir / "run.json"
    assert run_json.is_file()
    run_data = json.loads(run_json.read_text())
    step_names = {s["step"] for s in run_data["steps"]}
    assert "COMPUTE_UQ_INDICES" in step_names


def test_uq_with_failure_threshold(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    custom_kpi_extractor_file: Path,
) -> None:
    """UQ with failure_threshold should include probability-of-failure in output."""
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=5,
        outdir=outdir,
        openstudio_version="3.11.0",
        algorithm="uq",
        custom_kpi_extractor=custom_kpi_extractor_file,
        uq_failure_thresholds=["eui=150.0"],
        archive_intermediates=False,
        skip_preflight=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    campaign.run()

    uq_file = outdir / "uq" / "uq_results.json"
    assert uq_file.is_file()
    data = json.loads(uq_file.read_text())
    assert "probability_of_failure" in data
