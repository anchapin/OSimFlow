"""Unit tests for osimflow.importers.gem_osa (issue #1870)."""

import json
from pathlib import Path

import pytest
import yaml

from osimflow.__main__ import main
from osimflow.importers.gem_osa import (
    GemImportError,
    discover_batches,
    import_gem_project,
    parametric_space_to_variables,
)

SPACE = {
    "algorithm_setting": {"seed": 7, "number_of_samples": 12},
    "bar": {"floor_area": [1000, 5000], "stories": [1, 2, 3], "type": ["A", "B"]},
    "win": {"r": {"min": 1.0, "max": 2.0, "samplecount": 3}},
}


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "gem"
    proj.mkdir()
    (proj / "parametric_space_Batch1_base.json").write_text(json.dumps(SPACE))
    (proj / "parametric_space_Batch2_alt.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    (proj / "measure_space_Batch1_base.json").write_text(
        json.dumps(
            {"measure_space": {"bar": {"flag": "true", "n": "3", "floor_area": "9"}, "zz": {}}}
        )
    )
    (proj / "osa_workflowBatch1_base.json").write_text(
        json.dumps({"analysis": {"problem": {"algorithm": {"sample_method": "all_variables"}}}})
    )
    (proj / "configs.yml").write_text(
        yaml.safe_dump({"osa_settings": {"analysis_settings": {"analysis_type": "lhs"}}})
    )
    return proj


def _package(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "measure_dir_name": "bar",
                        "arguments": {"flag": False, "n": 1, "floor_area": 5},
                    }
                ]
            }
        )
    )
    return pkg


def test_discover_orders_batches(tmp_path: Path) -> None:
    ids = [b.batch_id for b in discover_batches(_project(tmp_path))]
    assert ids == ["Batch1_base", "Batch2_alt"]


def test_variable_mapping() -> None:
    by_name = {v["name"]: v for v in parametric_space_to_variables(SPACE)}
    assert by_name["bar.floor_area"]["distribution"] == "uniform"
    assert by_name["bar.stories"]["distribution"] == "discrete"
    assert by_name["bar.type"]["values"] == ["A", "B"]
    assert by_name["win.r"]["max"] == 2.0
    assert "algorithm_setting" not in " ".join(by_name)


def test_import_writes_campaigns_and_manifest(tmp_path: Path) -> None:
    out = tmp_path / "out"
    manifest = import_gem_project(_project(tmp_path), out, template_package=_package(tmp_path))
    entry = manifest["batches"]["Batch1_base"]
    assert entry["n_samples"] == 12 and entry["seed"] == 7
    assert entry["sample_method"] == "all_variables"
    assert entry["unmatched_measures"] == ["zz"]
    assert json.loads((out / "batches.json").read_text()) == manifest
    osw = json.loads((out / "Batch1_base/template/workflow.osw").read_text())
    args = osw["steps"][0]["arguments"]
    assert args["flag"] is True and args["n"] == 3
    assert args["floor_area"] == 5  # varied argument is left to the sweep
    variables = yaml.safe_load((out / "Batch1_base/variables.yml").read_text())
    assert variables["variables"][0]["name"] == "bar.floor_area"


def test_batch_filter_and_errors(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    manifest = import_gem_project(proj, tmp_path / "o", batches=["Batch2_alt"])
    assert list(manifest["batches"]) == ["Batch2_alt"]
    with pytest.raises(GemImportError, match="unknown batch"):
        import_gem_project(proj, tmp_path / "o2", batches=["nope"])
    with pytest.raises(GemImportError):
        discover_batches(tmp_path / "missing")
    with pytest.raises(GemImportError):
        parametric_space_to_variables({"m": {"a": "bad"}})


def test_cli(tmp_path: Path) -> None:
    rc = main(["import-gem-osa", str(_project(tmp_path)), "--output-dir", str(tmp_path / "c")])
    assert rc == 0
    assert (tmp_path / "c/batches.json").is_file()


def test_shared_and_reporting_measure_space(tmp_path: Path) -> None:
    proj = tmp_path / "g"
    proj.mkdir()
    (proj / "parametric_space_Batch1_a.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    (proj / "measure_space.json").write_text(
        json.dumps(
            {"measure_space": {"bar": {"n": "4"}}, "measure_space_reporting": {"rep": {"k": "2"}}}
        )
    )
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "steps": [
                    {"measure_dir_name": "bar", "arguments": {"n": 1}},
                    {"measure_dir_name": "rep", "arguments": {"k": 1}},
                ]
            }
        )
    )
    out = tmp_path / "o"
    import_gem_project(proj, out, template_package=pkg)
    steps = json.loads((out / "Batch1_a/template/workflow.osw").read_text())["steps"]
    assert steps[0]["arguments"]["n"] == 4 and steps[1]["arguments"]["k"] == 2


def test_bool_choices_and_bad_range() -> None:
    v = parametric_space_to_variables({"m": {"a": [True, False]}})[0]
    assert v["values"] == [True, False]
    with pytest.raises(GemImportError):
        parametric_space_to_variables({"m": {"a": {"min": True, "max": 5}}})
    with pytest.raises(GemImportError):
        parametric_space_to_variables({"m": {"a": {"min": "x", "max": 5}}})


def test_partial_reimport_preserves_manifest(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    import_gem_project(proj, tmp_path / "o")
    manifest = import_gem_project(proj, tmp_path / "o", batches=["Batch2_alt"])
    assert set(manifest["batches"]) == {"Batch1_base", "Batch2_alt"}
