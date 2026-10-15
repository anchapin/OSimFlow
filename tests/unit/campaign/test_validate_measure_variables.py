"""Unit tests for Campaign.step_validate_measure_variables and code_hashes["bin"]
sensitivity to osimflow/measures.py (issue #2052).
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

import osimflow
from osimflow.campaign import Campaign, _transitive_import_closure
from osimflow.config import CampaignConfig
from osimflow.executors.local_executor import LocalExecutor
from osimflow.measures import UnmappedVariableError


def _make_campaign_cfg(
    template_dir: Path,
    variables_path: Path,
    outdir: Path,
) -> CampaignConfig:
    return CampaignConfig(
        template_sim_package=template_dir,
        input_variables=variables_path,
        outdir=outdir,
        n_samples=2,
        openstudio_version="3.10.0",
        skip_preflight=True,
    )


def test_step_validate_measure_variables_template_package_and_mixed_osm(tmp_path: Path) -> None:
    """step_validate_measure_variables accepts XML args, Ruleset args, __SKIP__, __epw_file__, and .osm attrs."""
    pkg = tmp_path / "template_pkg"
    pkg.mkdir(parents=True)
    (pkg / "workflow.osw").write_text('{"steps": []}\n', encoding="utf-8")
    (pkg / "model.osm").write_text("OS:Version,\n  {00000000-0000-0000-0000-000000000000};\n")

    # 1. Measure with measure.xml only (dynamic loop style)
    cleanup_dir = pkg / "measures" / "CleanupRunDirectory"
    cleanup_dir.mkdir(parents=True)
    (cleanup_dir / "measure.xml").write_text(
        """<?xml version="1.0"?>
<measure>
  <arguments>
    <argument>
      <name>Sizing Run Directories</name>
      <type>Boolean</type>
      <required>true</required>
    </argument>
  </arguments>
</measure>
""",
        encoding="utf-8",
    )

    # 2. Measure with OpenStudio::Ruleset::OSArgument and nested choice call
    sim_dir = pkg / "measures" / "simulation_settings"
    sim_dir.mkdir(parents=True)
    (sim_dir / "measure.rb").write_text(
        """
class SimulationSettings < OpenStudio::Measure::ModelMeasure
  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    dst_start = OpenStudio::Ruleset::OSArgument.makeStringArgument('dst_start', true)
    climate_zone = OpenStudio::Measure::OSArgument.makeChoiceArgument(
      'climate_zone',
      OpenstudioStandards::CreateTypical.get_climate_zones(false, 'Lookup From Model'),
      true
    )
    args
  end
end
""",
        encoding="utf-8",
    )

    variables_yml = tmp_path / "variables.yml"
    variables_data: dict[str, Any] = {
        "algorithm": "lhs",
        "variables": [
            {"name": "__epw_file__", "distribution": "categorical", "values": ["a.epw"]},
            {
                "name": "simulation_settings.__SKIP__",
                "measure_argument": "simulation_settings.__SKIP__",
                "distribution": "categorical",
                "values": [True, False],
            },
            {
                "name": "CleanupRunDirectory.Sizing Run Directories",
                "measure_argument": "CleanupRunDirectory.Sizing Run Directories",
                "distribution": "categorical",
                "values": [True, False],
            },
            {
                "name": "simulation_settings.dst_start",
                "measure_argument": "simulation_settings.dst_start",
                "distribution": "categorical",
                "values": ["3/12"],
            },
            {
                "name": "simulation_settings.climate_zone",
                "measure_argument": "simulation_settings.climate_zone",
                "distribution": "categorical",
                "values": ["ASHRAE 169-2013-4A"],
            },
            {
                "name": "lighting_power_density",
                "distribution": "uniform",
                "min": 5.0,
                "max": 15.0,
            },
            {
                "name": "SpaceType_General_Office.lighting_power_density",
                "distribution": "uniform",
                "min": 5.0,
                "max": 15.0,
            },
        ],
    }
    variables_yml.write_text(yaml.safe_dump(variables_data), encoding="utf-8")

    cfg = _make_campaign_cfg(pkg, variables_yml, tmp_path / "out_ok")
    campaign = Campaign(cfg, LocalExecutor(max_workers=1))
    campaign.step_validate_measure_variables()

    # Removing model.osm causes the .osm attribute variables to be rejected.
    (pkg / "model.osm").unlink()
    cfg_no_osm = _make_campaign_cfg(pkg, variables_yml, tmp_path / "out_no_osm")
    campaign_no_osm = Campaign(cfg_no_osm, LocalExecutor(max_workers=1))
    with pytest.raises(UnmappedVariableError, match="lighting_power_density"):
        campaign_no_osm.step_validate_measure_variables()


@pytest.mark.xdist_group(name="code_hash_repo_tree")
def test_measures_py_in_import_closure_and_invalidates_bin_hash() -> None:
    """osimflow/measures.py is in _transitive_import_closure and code_hashes['bin']."""
    package_root = Path(osimflow.__file__).resolve().parent
    measures_py = package_root / "measures.py"
    assert measures_py.is_file()

    closure = _transitive_import_closure(package_root)
    assert measures_py in closure

    class _Stub:
        pass

    baseline = Campaign._compute_code_hashes(_Stub())
    original_content = measures_py.read_text(encoding="utf-8")
    try:
        with measures_py.open("a", encoding="utf-8") as fh:
            fh.write("# no-op touch for issue #2052 regression test\n")

        after = Campaign._compute_code_hashes(_Stub())
        assert baseline["bin"] != after["bin"], (
            "Editing osimflow/measures.py must invalidate code_hashes['bin']."
        )
        assert baseline["work"] == after["work"], (
            "Editing osimflow/measures.py must not change code_hashes['work']."
        )
    finally:
        measures_py.write_text(original_content, encoding="utf-8")
