"""Custom ``measure_paths`` and multi-instance step names (issue #2099)."""

import json
from pathlib import Path
from typing import Any

import pytest

from osimflow.apply_params import apply_parameters
from osimflow.measures import MeasureRegistry, UnmappedVariableError
from osimflow.osw_results import parse_out_osw

MEASURE_RB = 'OpenStudio::Measure::OSArgument.makeDoubleArgument("wwr", true)\n'


def _measure(root: Path, name: str) -> None:
    mdir = root / name
    mdir.mkdir(parents=True)
    (mdir / "measure.rb").write_text(MEASURE_RB, encoding="utf-8")


def _package(tmp_path: Path, osw: dict[str, Any]) -> Path:
    pkg = tmp_path / "pkg"
    _measure(pkg / "measures", "LocalMeasure")
    _measure(pkg / "shared_measures", "SetWindowToWallRatio")
    (pkg / "workflow.osw").write_text(json.dumps(osw))
    return pkg


MULTI_OSW: dict[str, Any] = {
    "measure_paths": ["shared_measures"],
    "steps": [
        {"measure_dir_name": "SetWindowToWallRatio", "name": "WWR North", "arguments": {}},
        {"measure_dir_name": "SetWindowToWallRatio", "name": "WWR South", "arguments": {}},
    ],
}


def test_index_measures_scans_osw_measure_paths(tmp_path: Path) -> None:
    registry = MeasureRegistry()
    registry.index_measures(_package(tmp_path, MULTI_OSW))
    names = {m["name"] for m in registry.list_available_measures()}
    assert names == {"LocalMeasure", "SetWindowToWallRatio"}


def test_index_measures_absolute_measure_path_and_local_precedence(tmp_path: Path) -> None:
    external = tmp_path / "external"
    _measure(external, "ExternalMeasure")
    _measure(external, "LocalMeasure")
    pkg = _package(tmp_path, {"measure_paths": [str(external)], "steps": []})
    registry = MeasureRegistry()
    registry.index_measures(pkg)
    by_name = {m["name"]: m["path"] for m in registry.list_available_measures()}
    assert "ExternalMeasure" in by_name
    assert by_name["LocalMeasure"] == str(pkg / "measures" / "LocalMeasure")


def test_index_measures_without_osw_still_works(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    _measure(pkg / "measures", "OnlyLocal")
    registry = MeasureRegistry()
    registry.index_measures(pkg)
    assert [m["name"] for m in registry.list_available_measures()] == ["OnlyLocal"]


def test_validate_variables_accepts_step_name_aliases(tmp_path: Path) -> None:
    registry = MeasureRegistry()
    registry.index_measures(_package(tmp_path, MULTI_OSW))
    registry.validate_variables_mapping(
        [
            {"name": "wwr_n", "measure_argument": "WWR North.wwr"},
            {"name": "wwr_s", "measure_argument": "WWR South.wwr"},
            {"name": "WWR South.wwr"},
        ],
        registry,
    )
    with pytest.raises(UnmappedVariableError):
        registry.validate_variables_mapping(
            [{"name": "x", "measure_argument": "WWR East.wwr"}], registry
        )


def test_conflicting_step_name_alias_is_not_usable(tmp_path: Path) -> None:
    osw = {
        "measure_paths": ["shared_measures"],
        "steps": [
            {"measure_dir_name": "SetWindowToWallRatio", "name": "Shared", "arguments": {}},
            {"measure_dir_name": "LocalMeasure", "name": "Shared", "arguments": {}},
        ],
    }
    registry = MeasureRegistry()
    registry.index_measures(_package(tmp_path, osw))
    with pytest.raises(UnmappedVariableError):
        registry.validate_variables_mapping(
            [{"name": "x", "measure_argument": "Shared.wwr"}], registry
        )


def test_apply_parameters_targets_step_by_name_with_measure_paths(tmp_path: Path) -> None:
    osw = {
        "measure_paths": ["shared_measures"],
        "steps": [
            {"measure_dir_name": "SetWindowToWallRatio", "name": "WWR North", "arguments": {}},
            {"measure_dir_name": "SetWindowToWallRatio", "name": "WWR South", "arguments": {}},
        ],
    }
    pkg = _package(tmp_path, osw)
    for step in osw["steps"]:
        step["arguments"] = {"wwr": 0.2}
    (pkg / "workflow.osw").write_text(json.dumps(osw))
    out = tmp_path / "out"
    apply_parameters(
        template=pkg,
        parameters={"WWR North.wwr": 0.5, "WWR South.wwr": 0.7},
        sample_id="0001",
        out=out,
    )
    mutated = json.loads((out / "workflow.osw").read_text())
    assert mutated["steps"][0]["arguments"]["wwr"] == 0.5
    assert mutated["steps"][1]["arguments"]["wwr"] == 0.7


def _out_osw(tmp_path: Path, steps: list[dict[str, Any]]) -> Path:
    path = tmp_path / "out.osw"
    path.write_text(json.dumps({"steps": steps}))
    return path


def _step(dir_name: str, name: str | None, value: float) -> dict[str, Any]:
    step: dict[str, Any] = {
        "measure_dir_name": dir_name,
        "result": {"step_result": "Success", "step_values": [{"name": "area", "value": value}]},
    }
    if name is not None:
        step["name"] = name
    return step


def test_parse_out_osw_preserves_multi_instance_steps(tmp_path: Path) -> None:
    path = _out_osw(
        tmp_path,
        [
            _step("AddOverhangs", "Overhang North", 1.0),
            _step("AddOverhangs", "Overhang South", 2.0),
            _step("Other", "Other", 9.0),
        ],
    )
    assert parse_out_osw(path) == {
        "AddOverhangs.area": 2.0,
        "Overhang North.area": 1.0,
        "Overhang South.area": 2.0,
        "Other.area": 9.0,
    }


def test_parse_out_osw_single_instance_unchanged(tmp_path: Path) -> None:
    path = _out_osw(tmp_path, [_step("AddOverhangs", "Pretty Name", 1.0)])
    assert parse_out_osw(path) == {"AddOverhangs.area": 1.0}


def test_parse_out_osw_ambiguous_step_names_not_emitted(tmp_path: Path) -> None:
    path = _out_osw(
        tmp_path,
        [
            _step("MeasureA", "dup", 1.0),
            _step("MeasureA", "MeasureB", 2.0),
            _step("MeasureB", "other", 3.0),
            _step("MeasureC", "dup", 4.0),
            _step("MeasureC", "dup", 5.0),
        ],
    )
    values = parse_out_osw(path)
    assert values["MeasureB.area"] == 3.0
    assert "dup.area" not in values


def test_step_name_colliding_with_measure_name_or_xml_alias_is_rejected(tmp_path: Path) -> None:
    pkg = _package(
        tmp_path,
        {
            "measure_paths": ["shared_measures"],
            "steps": [
                {"measure_dir_name": "SetWindowToWallRatio", "name": "LocalMeasure"},
                {"measure_dir_name": "SetWindowToWallRatio", "name": "xml_alias"},
            ],
        },
    )
    (pkg / "measures" / "LocalMeasure" / "measure.xml").write_text(
        "<measure><name>xml_alias</name><arguments>"
        "<argument><name>wwr</name><type>Double</type></argument></arguments></measure>"
    )
    registry = MeasureRegistry()
    registry.index_measures(pkg)
    for ref in ("LocalMeasure.wwr", "xml_alias.wwr"):
        # resolves only to the canonical LocalMeasure, never the step's measure
        registry.validate_variables_mapping([{"name": "x", "measure_argument": ref}], registry)
    assert registry._measures["SetWindowToWallRatio"].path.parent.name == "shared_measures"
