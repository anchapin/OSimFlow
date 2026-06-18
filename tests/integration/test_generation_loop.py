"""Integration tests for the generation loop in Campaign.run().

Covers:
  * Non-iterative algorithm (LHS) runs exactly 1 generation even with
    ``max_generations > 1``.
  * Iterative algorithm (DE) runs all requested generations.
  * Generation trace is correctly recorded in run.json.
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
                ]
            }
        )
    )
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    pkg.mkdir()
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"u1": 0.5}}))
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
        "        }\n"
        "    }))\n"
        "    return kpi_path\n"
    )
    return f


def test_lhs_non_iterative_runs_one_generation_regardless_of_max_generations(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    custom_kpi_extractor_file: Path,
) -> None:
    """LHS is not iterative; the generation loop must break after gen 0."""
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
        algorithm="lhs",
        custom_kpi_extractor=custom_kpi_extractor_file,
        max_generations=3,
        archive_intermediates=False,
        skip_preflight=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    campaign.run()

    run_json = outdir / "run.json"
    assert run_json.is_file()
    data = json.loads(run_json.read_text())

    assert "generations" in data, "run.json should have generations key"
    gen_nums = [g["generation"] for g in data["generations"]]
    assert gen_nums == [0], f"LHS should run only generation 0, got {gen_nums}"


def test_de_iterative_runs_all_requested_generations(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    custom_kpi_extractor_file: Path,
) -> None:
    """DE is iterative; it must run all max_generations when tol is not met."""
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=4,
        outdir=outdir,
        openstudio_version="3.11.0",
        algorithm="de",
        custom_kpi_extractor=custom_kpi_extractor_file,
        max_generations=2,
        archive_intermediates=False,
        skip_preflight=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    campaign.run()

    run_json = outdir / "run.json"
    assert run_json.is_file()
    data = json.loads(run_json.read_text())

    gen_nums = [g["generation"] for g in data.get("generations", [])]
    assert len(gen_nums) == 2, f"DE should run 2 generations, got {gen_nums}"
    assert gen_nums == [0, 1]


def test_generation_loop_cancel_during_execution_raises(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    custom_kpi_extractor_file: Path,
) -> None:
    """Writing .stop before run() should gracefully cancel the campaign.

    The campaign catches KeyboardInterrupt internally and returns
    ``{"status": "cancelled", ...}`` instead of re-raising, so that
    concurrent executors can shut down cleanly (issue #602).
    """
    stop_file = outdir / ".stop"
    stop_file.write_text("")

    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
        algorithm="lhs",
        custom_kpi_extractor=custom_kpi_extractor_file,
        max_generations=2,
        archive_intermediates=False,
        skip_preflight=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))

    result = campaign.run()
    assert result["status"] == "cancelled"

    stop_file.unlink()


def test_algorithm_observe_feedback_loop(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    custom_kpi_extractor_file: Path,
) -> None:
    """Verify observe() is called and generation history is accumulated."""
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        algorithm="lhs",
        custom_kpi_extractor=custom_kpi_extractor_file,
        max_generations=1,
        archive_intermediates=False,
        skip_preflight=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    campaign.run()

    run_json = outdir / "run.json"
    data = json.loads(run_json.read_text())
    assert len(data.get("generations", [])) == 1
    assert data["generations"][0]["generation"] == 0
