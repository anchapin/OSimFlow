"""``.osa`` import of multiple workflow steps sharing a measure dir (issue #2117)."""

from typing import Any

from osimflow.importers.osa import (
    _build_step_alias_map,
    _collect_varied_measure_args,
    _extract_workflow_variables,
    _measure_step_name,
    _overlay_osa_workflow_onto_osw,
)


def _wf() -> list[dict[str, Any]]:
    return [
        {
            "name": "heating_setpoint",
            "measure_dir_name": "SetPoint",
            "arguments": {"mode": "heat", "limit": 5},
            "variables": [
                {
                    "argument": {"name": "temp"},
                    "uncertainty_description": {"type": "uniform"},
                }
            ],
        },
        {
            "name": "cooling_setpoint",
            "measure_dir_name": "SetPoint",
            "arguments": {"mode": "cool", "limit": 7},
        },
    ]


def _osa() -> dict[str, Any]:
    return {"problem": {"workflow": _wf()}}


def test_measure_step_name_stays_directory() -> None:
    assert _measure_step_name(_wf()[0]) == "SetPoint"


def test_workflow_variables_use_step_name_when_dir_shared() -> None:
    (var,) = _extract_workflow_variables(_wf())
    assert var["measure"] == {"name": "heating_setpoint", "argument": "temp"}


def test_workflow_variables_single_instance_keeps_dir_name() -> None:
    (var,) = _extract_workflow_variables(_wf()[:1])
    assert var["measure"]["name"] == "SetPoint"


def test_alias_map_maps_each_instance() -> None:
    wf = _wf()
    wf[0]["display_name"] = "Heat"
    amap = _build_step_alias_map(wf)
    assert amap["heating_setpoint"] == "heating_setpoint"
    assert amap["Heat"] == "heating_setpoint"
    assert amap["cooling_setpoint"] == "cooling_setpoint"
    assert "SetPoint" not in amap


def test_alias_map_drops_shared_identifiers() -> None:
    wf = _wf()
    for step in wf:
        step["measure_definition_class_name"] = "SetPointClass"
    assert "SetPointClass" not in _build_step_alias_map(wf)


def test_collect_varied_pairs_use_step_name() -> None:
    assert _collect_varied_measure_args(_osa()) == {("heating_setpoint", "temp")}


def test_overlay_targets_named_instances() -> None:
    osw: dict[str, Any] = {
        "steps": [
            {"measure_dir_name": "SetPoint", "name": "cooling_setpoint", "arguments": {}},
            {"measure_dir_name": "SetPoint", "name": "heating_setpoint", "arguments": {}},
        ]
    }
    _overlay_osa_workflow_onto_osw(osw, _osa(), _collect_varied_measure_args(_osa()))
    cool, heat = osw["steps"]
    assert cool["arguments"] == {"mode": "cool", "limit": 7}
    assert heat["arguments"] == {"mode": "heat", "limit": 5}


def test_overlay_pairs_unnamed_steps_by_occurrence() -> None:
    osw: dict[str, Any] = {
        "steps": [
            {"measure_dir_name": "SetPoint", "arguments": {}},
            {"measure_dir_name": "SetPoint", "arguments": {}},
        ]
    }
    _overlay_osa_workflow_onto_osw(osw, _osa(), set())
    assert [s["arguments"]["mode"] for s in osw["steps"]] == ["heat", "cool"]


def test_overlay_appends_missing_instance_with_name() -> None:
    osw: dict[str, Any] = {
        "steps": [{"measure_dir_name": "SetPoint", "name": "heating_setpoint", "arguments": {}}]
    }
    varied = {("heating_setpoint", "limit")}
    _overlay_osa_workflow_onto_osw(osw, _osa(), varied)
    heat, cool = osw["steps"]
    assert heat["arguments"] == {"mode": "heat"}
    assert cool["name"] == "cooling_setpoint"
    assert cool["arguments"] == {"mode": "cool", "limit": 7}
