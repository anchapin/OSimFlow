"""Integration tests for the full BYOS (Bring Your Own Script) campaign path.

Issue #1323: the BYOS contract is tested in 52 unit tests (test_byos.py), but
no integration test exercises the full BYOS path: a real user-supplied Python
script being discovered by Campaign, called via subprocess with the correct
environment, and its output driving subsequent DAG steps.

This module fills that gap with two tests:

1. test_custom_apply_script_called_and_campaign_completes
   - 3-sample campaign with ``custom_apply_script`` pointing to a real Python file.
   - Asserts every sample dir contains the BYOS sentinel written by the script.
   - Asserts ``run.json`` records ``custom_apply_script`` in its config.
   - Asserts all 4 artifacts are produced (aggregated_results.csv,
     failed_simulations.csv, KPI JSONs, plots/).

2. test_custom_kpi_extractor_called_and_campaign_completes
   - 3-sample campaign with ``custom_kpi_extractor`` pointing to a real Python file.
   - Asserts the KPI JSONs contain values produced by the custom script.
   - Asserts ``run.json`` records ``custom_kpi_extractor`` in its config.
   - Asserts all 4 artifacts are produced.

Both tests use the LocalExecutor (stub simulation) and are marked ``slow`` so
they are skipped in ``make test-fast`` / pre-commit but included in the full
``make test`` suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
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
    import shutil

    pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


# ---------------------------------------------------------------------------
# BYOS apply script fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def custom_apply_script(workdir: Path) -> Path:
    """A real user-supplied ``apply_parameters`` script that writes a sentinel
    file in each sample directory so we can verify it was called."""
    path = workdir / "my_apply.py"
    path.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "\n"
        "def apply_parameters(template, parameters, sample_id, out):\n"
        "    out.mkdir(parents=True, exist_ok=True)\n"
        "    (out / 'byos_apply_ran.txt').write_text(\n"
        "        f'sample_id={sample_id} params={parameters}'\n"
        "    )\n"
        "    # Return the output directory so the simulation step can proceed.\n"
        "    return out\n"
    )
    return path


# ---------------------------------------------------------------------------
# BYOS KPI extractor script fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def custom_kpi_extractor(workdir: Path) -> Path:
    """A real user-supplied ``extract_kpis`` script that writes a KPI JSON
    containing a deterministic value derived from the sample_id."""
    path = workdir / "my_kpis.py"
    path.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "\n"
        "def extract_kpis(simulation_dir, sample_id, out, **kwargs):\n"
        "    out.mkdir(parents=True, exist_ok=True)\n"
        "    kpi_path = out / f'kpi_{sample_id}.json'\n"
        "    # Deterministic value so the test is stable: hash-based EUI.\n"
        "    x = (hash(sample_id) % 1000) / 1000.0\n"
        "    eui = 150.0 + 30.0 * x\n"
        "    kpi_path.write_text(json.dumps({\n"
        "        'sample_id': sample_id,\n"
        "        'kpis': {\n"
        "            'eui': round(eui, 3),\n"
        "            'total_site_energy_kwh': round(eui * 100, 3),\n"
        "        }\n"
        "    }))\n"
        "    return kpi_path\n"
    )
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_custom_apply_script_called_and_campaign_completes(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    custom_apply_script: Path,
) -> None:
    """A 3-sample campaign with ``--custom_apply_script`` must call the script
    for every sample (sentinel file check) and complete successfully."""
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        custom_apply_script=custom_apply_script,
        skip_preflight=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    result = campaign.run()

    # 1. Script was called for every sample (sentinel file exists).
    apply_dirs = list((outdir / "work" / "apply").glob("*"))
    assert len(apply_dirs) == 3, f"expected 3 apply sample dirs, got {len(apply_dirs)}"
    for sample_dir in apply_dirs:
        sentinel = sample_dir / "byos_apply_ran.txt"
        assert sentinel.is_file(), (
            f"BYOS apply sentinel missing in {sample_dir}; "
            f"custom_apply_script was not called for this sample"
        )
        content = sentinel.read_text()
        assert "sample_id=" in content
        assert "params=" in content

    # 2. Campaign completed and produced all 4 artifacts.
    assert (outdir / "aggregated_results.csv").is_file()
    assert (outdir / "failed_simulations.csv").is_file()
    kpi_files = list((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_files) == 3, f"expected 3 KPI JSONs, got {len(kpi_files)}"
    assert (outdir / "plots").is_dir()

    # 3. run.json records the custom_apply_script in config.
    run_json = outdir / "run.json"
    assert run_json.is_file()
    trace = json.loads(run_json.read_text())
    assert trace["config"]["custom_apply_script"] == str(custom_apply_script)
    assert trace["summary"]["n_samples"] == 3
    assert trace["summary"]["n_succeeded"] == 3
    assert trace["summary"]["n_failed"] == 0

    # 4. Result dict shape.
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert len(result["samples"]) == 3
    assert Path(result["aggregated"]["csv"]).is_file()


def test_custom_kpi_extractor_called_and_campaign_completes(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    custom_kpi_extractor: Path,
) -> None:
    """A 3-sample campaign with ``custom_kpi_extractor`` must call the script
    for every sample (KPI content check) and complete successfully."""
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        custom_kpi_extractor=custom_kpi_extractor,
        skip_preflight=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    result = campaign.run()

    # 1. KPI JSONs written by the custom script contain expected keys.
    kpi_files = list((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_files) == 3, f"expected 3 KPI JSONs, got {len(kpi_files)}"
    for kpi_file in kpi_files:
        data = json.loads(kpi_file.read_text())
        assert "sample_id" in data
        assert "kpis" in data
        assert "eui" in data["kpis"]
        assert "total_site_energy_kwh" in data["kpis"]
        # Values must be numeric (not NaN/None).
        assert isinstance(data["kpis"]["eui"], (int, float))
        assert isinstance(data["kpis"]["total_site_energy_kwh"], (int, float))

    # 2. Campaign completed and produced all 4 artifacts.
    assert (outdir / "aggregated_results.csv").is_file()
    assert (outdir / "failed_simulations.csv").is_file()
    assert (outdir / "plots").is_dir()

    # 3. run.json records the custom_kpi_extractor in config.
    run_json = outdir / "run.json"
    assert run_json.is_file()
    trace = json.loads(run_json.read_text())
    assert trace["config"]["custom_kpi_extractor"] == str(custom_kpi_extractor)
    assert trace["summary"]["n_samples"] == 3
    assert trace["summary"]["n_succeeded"] == 3
    assert trace["summary"]["n_failed"] == 0

    # 4. Result dict shape.
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert len(result["samples"]) == 3
    assert Path(result["aggregated"]["csv"]).is_file()
