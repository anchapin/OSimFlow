"""Integration tests for sobol algorithm campaign (issue #346).

These tests cover the ``COMPUTE_SENSITIVITY_INDICES`` step that runs when
``cfg.algorithm == "sobol"``.
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
                    {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
                ]
            }
        )
    )
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    pkg.mkdir()
    (pkg / "model.osm").write_text(
        json.dumps({"attributes": {"x": 0.5}})
    )
    (pkg / "workflow.osw").write_text(json.dumps({"name": "stub"}))
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


@pytest.fixture
def custom_kpi_extractor_file(workdir: Path) -> Path:
    """A BYOS KPI extractor that returns valid numeric KPIs (required for
    Sobol sensitivity analysis, which needs numeric Y values)."""
    f = workdir / "stub_kpis.py"
    f.write_text(
        "from pathlib import Path\n"
        "import random\n"
        "\n"
        "def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:\n"
        "    out.mkdir(parents=True, exist_ok=True)\n"
        "    kpi_path = out / f'kpi_{sample_id}.json'\n"
        "    # Sobol needs numeric KPIs; return eui that varies with x.\n"
        "    x = 0.5 + 0.3 * (hash(sample_id) % 1000) / 1000.0\n"
        "    eui = 100.0 + 20.0 * x + random.gauss(0, 1)\n"
        "    import json\n"
        "    kpi_path.write_text(json.dumps({\n"
        "        'sample_id': sample_id,\n"
        "        'kpis': {\n"
        "            'eui': round(eui, 3),\n"
        "            'total_site_energy_kwh': round(eui * 100, 3),\n"
        "        }\n"
        "    }))\n"
        "    return kpi_path\n"
    )
    return f


@pytest.fixture
def sobol_cfg(
    workdir: Path, template_pkg: Path, outdir: Path, custom_kpi_extractor_file: Path
) -> CampaignConfig:
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=4,
        outdir=outdir,
        openstudio_version="3.11.0",
        algorithm="sobol",
        custom_kpi_extractor=custom_kpi_extractor_file,
        archive_intermediates=False,
        skip_preflight=True,
    )


def test_sobol_campaign_produces_sensitivity_indices_json(
    sobol_cfg: CampaignConfig,
) -> None:
    """A campaign with algorithm=sobol must produce sensitivity_indices.json."""
    campaign = Campaign(cfg=sobol_cfg, executor=LocalExecutor(max_workers=2))
    result = campaign.run()

    indices_file = sobol_cfg.outdir / "sensitivity" / "sensitivity_indices.json"
    assert indices_file.is_file(), (
        f"sensitivity_indices.json not found at {indices_file}; "
        f"run.json: {sobol_cfg.outdir / 'run.json'}"
    )
    data = json.loads(indices_file.read_text())
    assert "indices" in data
    indices = data["indices"]
    assert indices.get("S1") or indices.get("ST")


def test_sobol_step_runs_after_kpi_extraction(
    sobol_cfg: CampaignConfig,
    outdir: Path,
) -> None:
    """Verify COMPUTE_SENSITIVITY_INDICES step appears in run.json."""
    campaign = Campaign(cfg=sobol_cfg, executor=LocalExecutor(max_workers=2))
    campaign.run()

    run_json = outdir / "run.json"
    assert run_json.is_file()
    run_data = json.loads(run_json.read_text())
    step_names = {s["step"] for s in run_data["steps"]}
    assert "COMPUTE_SENSITIVITY_INDICES" in step_names


def test_sobol_algorithm_is_not_iterative(
    workdir: Path, template_pkg: Path, outdir: Path, custom_kpi_extractor_file: Path
) -> None:
    """Sobol is single-shot; verify a second generation does not run."""
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=4,
        outdir=outdir,
        openstudio_version="3.11.0",
        algorithm="sobol",
        custom_kpi_extractor=custom_kpi_extractor_file,
        max_generations=2,
        archive_intermediates=False,
        skip_preflight=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    campaign.run()

    run_json = outdir / "run.json"
    run_data = json.loads(run_json.read_text())
    gen_counts = [g["generation"] for g in run_data["generations"]]
    assert max(gen_counts or [0]) <= 1, "Sobol should not run multiple generations"
