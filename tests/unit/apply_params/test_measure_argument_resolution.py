"""``measure_argument`` resolution in preflight and OSW mutation (issue #2078)."""

import json
from pathlib import Path
from typing import Any

import pytest

from osimflow.apply_params import (
    CrossMeasureConflictError,
    UnmappedParameterError,
    _mutate_osw,
    build_measure_argument_alias_map,
    parse_osw_arguments,
    preflight_check,
    resolve_measure_argument_keys,
)
from osimflow.measures import MeasureRegistry, UnmappedVariableError

VARS: list[dict[str, Any]] = [
    {"name": "wall_r", "measure_argument": "WallInsulation.r_value"},
    {"name": "roof_r", "measure_argument": "RoofInsulation.r_value"},
    {"name": "plain", "measure_argument": "plain"},
    {"name": "no_target"},
]


def _write_measure(base: Path, dir_name: str, xml_name: str, class_name: str) -> None:
    d = base / "measures" / dir_name
    d.mkdir(parents=True)
    (d / "measure.xml").write_text(
        f"<measure><name>{xml_name}</name><class_name>{class_name}</class_name>"
        "<arguments><argument><name>r_value</name><type>Double</type>"
        "<required>true</required><default_value>1.0</default_value></argument></arguments>"
        "</measure>"
    )


@pytest.fixture
def osw(tmp_path: Path) -> Path:
    _write_measure(tmp_path, "WallInsulation", "wall_insulation", "WallInsulationClass")
    _write_measure(tmp_path, "RoofInsulation", "roof_insulation", "RoofInsulationClass")
    path = tmp_path / "workflow.osw"
    path.write_text(
        json.dumps(
            {
                "measure_paths": ["measures"],
                "steps": [
                    {"measure_dir_name": "WallInsulation", "arguments": {"r_value": 1.0}},
                    {"measure_dir_name": "RoofInsulation", "arguments": {"r_value": 2.0}},
                ],
            }
        )
    )
    return path


def test_alias_map_skips_identity_and_missing() -> None:
    assert build_measure_argument_alias_map(VARS) == {
        "wall_r": "WallInsulation.r_value",
        "roof_r": "RoofInsulation.r_value",
    }


def test_alias_map_skips_epw_file_variables() -> None:
    defs = [{"name": "wx", "target": "epw_file", "measure_argument": "M.x"}]
    assert build_measure_argument_alias_map(defs) == {}


def test_alias_map_rejects_duplicate_targets() -> None:
    defs = [
        {"name": "a", "measure_argument": "M.x"},
        {"name": "b", "measure_argument": "M.x"},
    ]
    with pytest.raises(CrossMeasureConflictError):
        build_measure_argument_alias_map(defs)


def test_resolve_keys_rewrites_and_detects_collision() -> None:
    alias = {"wall_r": "WallInsulation.r_value"}
    assert resolve_measure_argument_keys({"wall_r": 3, "other": 1}, alias) == {
        "WallInsulation.r_value": 3,
        "other": 1,
    }
    with pytest.raises(CrossMeasureConflictError):
        resolve_measure_argument_keys({"wall_r": 3, "WallInsulation.r_value": 4}, alias)


def test_unaliased_custom_name_fails_preflight(osw: Path) -> None:
    with pytest.raises(UnmappedParameterError):
        preflight_check({"wall_r": 1.0}, parse_osw_arguments(osw))


def test_aliased_names_disambiguate_duplicate_argument(osw: Path) -> None:
    mappings = parse_osw_arguments(osw)
    params = resolve_measure_argument_keys(
        {"wall_r": 11.0, "roof_r": 22.0}, build_measure_argument_alias_map(VARS)
    )
    preflight_check(params, mappings)
    _mutate_osw(osw, params, mappings)
    steps = json.loads(osw.read_text())["steps"]
    assert steps[0]["arguments"]["r_value"] == 11.0
    assert steps[1]["arguments"]["r_value"] == 22.0


@pytest.mark.parametrize("ident", ["wall_insulation", "WallInsulationClass"])
def test_measure_xml_name_and_class_name_resolve(osw: Path, ident: str) -> None:
    mappings = parse_osw_arguments(osw)
    preflight_check({f"{ident}.r_value": 5.0}, mappings)
    _mutate_osw(osw, {f"{ident}.r_value": 5.0}, mappings)
    steps = json.loads(osw.read_text())["steps"]
    assert steps[0]["arguments"]["r_value"] == 5.0
    assert steps[1]["arguments"]["r_value"] == 2.0


def test_registry_accepts_xml_name_and_class_name(osw: Path) -> None:
    reg = MeasureRegistry()
    reg.index_measures(osw.parent)
    reg.validate_variables_mapping(
        [
            {"name": "a", "measure_argument": "wall_insulation.r_value"},
            {"name": "b", "measure_argument": "RoofInsulationClass.r_value"},
        ],
        has_osm=False,
    )
    with pytest.raises(UnmappedVariableError):
        reg.validate_variables_mapping(
            [{"name": "c", "measure_argument": "Nope.r_value"}], has_osm=False
        )


def test_colliding_aliases_are_not_resolved(tmp_path: Path) -> None:
    # Two measures claim the same <name>, and one claims the other's directory name.
    _write_measure(tmp_path, "MeasureA", "shared_name", "ClassA")
    _write_measure(tmp_path, "MeasureB", "shared_name", "MeasureA")
    path = tmp_path / "workflow.osw"
    path.write_text(
        json.dumps(
            {
                "measure_paths": ["measures"],
                "steps": [
                    {"measure_dir_name": "MeasureA", "arguments": {"r_value": 1.0}},
                    {"measure_dir_name": "MeasureB", "arguments": {"r_value": 2.0}},
                ],
            }
        )
    )
    mappings = parse_osw_arguments(path)
    assert "shared_name.r_value" not in mappings
    assert mappings["MeasureA.r_value"].step_index == 0
    assert mappings["ClassA.r_value"].step_index == 0

    reg = MeasureRegistry()
    reg.index_measures(tmp_path)
    with pytest.raises(UnmappedVariableError):
        reg.validate_variables_mapping(
            [{"name": "x", "measure_argument": "shared_name.r_value"}], has_osm=False
        )
