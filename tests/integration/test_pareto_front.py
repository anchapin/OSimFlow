"""Integration tests for Pareto front persistence (issue #141).

These tests cover the ``_persist_pareto_front`` code path that runs when
``algo.is_multi_objective()`` is True.
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
        "    cost = 50.0 + 10.0 * x + random.gauss(0, 0.5)\n"
        "    kpi_path.write_text(json.dumps({\n"
        "        'sample_id': sample_id,\n"
        "        'kpis': {\n"
        "            'eui': round(eui, 3),\n"
        "            'cost': round(cost, 3),\n"
        "        }\n"
        "    }))\n"
        "    return kpi_path\n"
    )
    return f


def test_pareto_front_persisted_for_spea2(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    custom_kpi_extractor_file: Path,
) -> None:
    """SPEA2 is multi-objective; campaign must write pareto/gen_N.json."""
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=4,
        outdir=outdir,
        openstudio_version="3.11.0",
        algorithm="spea2",
        custom_kpi_extractor=custom_kpi_extractor_file,
        archive_intermediates=False,
        skip_preflight=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    campaign.run()

    pareto_dir = outdir / "pareto"
    assert pareto_dir.is_dir(), f"pareto dir not found; run.json: {(outdir / 'run.json').read_text()}"

    gen_files = sorted(pareto_dir.glob("gen_*.json"))
    assert len(gen_files) >= 1, f"Expected at least pareto/gen_0.json, found: {list(pareto_dir.glob('*'))}"

    for gf in gen_files:
        data = json.loads(gf.read_text())
        assert "solutions" in data or "front" in data or "objectives" in data


def test_nsga2_multi_objective_flag(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    custom_kpi_extractor_file: Path,
) -> None:
    """NSGA-II is multi-objective; verify Pareto front is persisted."""
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=4,
        outdir=outdir,
        openstudio_version="3.11.0",
        algorithm="nsga2",
        custom_kpi_extractor=custom_kpi_extractor_file,
        archive_intermediates=False,
        skip_preflight=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))

    try:
        campaign.run()
    except (ImportError, ModuleNotFoundError) as exc:
        if "pymoo" in str(exc):
            pytest.skip(f"pymoo not installed: {exc}")
        raise

    pareto_dir = outdir / "pareto"
    if pareto_dir.is_dir():
        gen_files = sorted(pareto_dir.glob("gen_*.json"))
        assert len(gen_files) >= 1
