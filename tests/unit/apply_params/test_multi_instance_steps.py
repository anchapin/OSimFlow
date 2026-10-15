"""Multi-instance measure steps sharing ``measure_dir_name`` (issue #2117)."""

import json
from pathlib import Path

import pytest

from osimflow._work_osm_mutations import _apply_osw_parameters
from osimflow.apply_params import (
    AmbiguousParameterError,
    _mutate_osw,
    parse_osw_arguments,
    preflight_check,
)


def _osw(tmp_path: Path, steps: list[dict]) -> Path:
    path = tmp_path / "workflow.osw"
    path.write_text(json.dumps({"steps": steps}))
    return path


def _two_steps() -> list[dict]:
    return [
        {"measure_dir_name": "SetPoint", "name": "heat", "arguments": {"temp": 20.0}},
        {"measure_dir_name": "SetPoint", "name": "cool", "arguments": {"temp": 26.0}},
    ]


def test_mapping_preserves_step_name(tmp_path: Path) -> None:
    mappings = parse_osw_arguments(_osw(tmp_path, _two_steps()))
    assert mappings["heat.temp"].step_name == "heat"
    assert mappings["cool.temp"].step_name == "cool"
    assert mappings["heat.temp"].measure_name == "SetPoint"
    assert mappings["heat.temp"].ambiguous_steps == ()


def test_step_name_key_mutates_only_target(tmp_path: Path) -> None:
    osw = _osw(tmp_path, _two_steps())
    mappings = parse_osw_arguments(osw)
    _mutate_osw(osw, {"cool.temp": 24.0}, mappings)
    steps = json.loads(osw.read_text())["steps"]
    assert steps[0]["arguments"]["temp"] == 20.0
    assert steps[1]["arguments"]["temp"] == 24.0


def test_mutation_follows_step_name_after_reorder(tmp_path: Path) -> None:
    osw = _osw(tmp_path, _two_steps())
    mappings = parse_osw_arguments(osw)
    osw.write_text(json.dumps({"steps": list(reversed(_two_steps()))}))
    _mutate_osw(osw, {"heat.temp": 18.0}, mappings)
    steps = json.loads(osw.read_text())["steps"]
    assert steps[1]["name"] == "heat"
    assert steps[1]["arguments"]["temp"] == 18.0
    assert steps[0]["arguments"]["temp"] == 26.0


def test_unqualified_dir_name_is_ambiguous(tmp_path: Path) -> None:
    osw = _osw(tmp_path, _two_steps())
    mappings = parse_osw_arguments(osw)
    assert mappings["SetPoint.temp"].ambiguous_steps == ("heat", "cool")
    with pytest.raises(AmbiguousParameterError, match=r"SetPoint\.temp.*heat, cool"):
        preflight_check({"SetPoint.temp": 22.0}, mappings)
    with pytest.raises(AmbiguousParameterError):
        _mutate_osw(osw, {"SetPoint.temp": 22.0}, mappings)


def test_unnamed_duplicates_labelled_by_index(tmp_path: Path) -> None:
    steps = [
        {"measure_dir_name": "M", "arguments": {"x": 1}},
        {"measure_dir_name": "M", "arguments": {"x": 2}},
    ]
    mappings = parse_osw_arguments(_osw(tmp_path, steps))
    assert mappings["M.x"].ambiguous_steps == ("step[0]", "step[1]")


def test_ambiguity_only_when_arg_defined_on_several_steps(tmp_path: Path) -> None:
    steps = [
        {"measure_dir_name": "M", "name": "a", "arguments": {"x": 1}},
        {"measure_dir_name": "M", "name": "b", "arguments": {"y": 2}},
    ]
    mappings = parse_osw_arguments(_osw(tmp_path, steps))
    assert mappings["M.x"].ambiguous_steps == ()
    preflight_check({"M.x": 3, "b.y": 4}, mappings)


def test_single_instance_unchanged(tmp_path: Path) -> None:
    steps = [{"measure_dir_name": "M", "name": "only", "arguments": {"x": 1}}]
    osw = _osw(tmp_path, steps)
    mappings = parse_osw_arguments(osw)
    preflight_check({"M.x": 5}, mappings)
    _mutate_osw(osw, {"M.x": 5, "only.x": 6}, mappings)
    assert json.loads(osw.read_text())["steps"][0]["arguments"]["x"] == 6


def test_default_apply_parameters_shares_step_matching(tmp_path: Path) -> None:
    osw = _osw(tmp_path, _two_steps())
    remaining = _apply_osw_parameters(osw, {"heat.temp": 19.0, "other": 1})
    assert remaining == {"other": 1}
    steps = json.loads(osw.read_text())["steps"]
    assert [s["arguments"]["temp"] for s in steps] == [19.0, 26.0]
    with pytest.raises(AmbiguousParameterError):
        _apply_osw_parameters(osw, {"SetPoint.temp": 1.0})
