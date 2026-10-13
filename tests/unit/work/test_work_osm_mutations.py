"""Unit tests for osimflow/_work_osm_mutations.py using a fake openstudio module."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from osimflow._work_osm_mutations import (
    _apply_osm_mutations,
    _apply_osw_parameters,
    _resolve_osm_object,
    _set_osm_attribute,
    _set_thermal_zone_schedule,
)
from osimflow.apply_params import OSMAttributeError


class FakeObj:
    def __init__(self, name: str, model: Any = None) -> None:
        self._name = name
        self._model = model
        self.calls: list[tuple[str, Any]] = []

    def nameString(self) -> str:
        return self._name

    def model(self) -> Any:
        return self._model

    def setLightingPowerPerFloorArea(self, v: float) -> None:
        self.calls.append(("lpd", v))

    def setThermalConductance(self, v: float) -> None:
        self.calls.append(("u", v))

    def setLightingLevel(self, v: float) -> None:
        self.calls.append(("ll", v))

    def setPeopleperSpaceFloorArea(self, v: float) -> None:
        self.calls.append(("ppa", v))

    def setCoolingSetpointTemperatureSchedule(self, s: Any) -> None:
        self.calls.append(("cool", s.value))

    def setHeatingSetpointTemperatureSchedule(self, s: Any) -> None:
        self.calls.append(("heat", s.value))

    def setString(self, attr: str, v: str) -> None:
        self.calls.append(("str:" + attr, v))


class FakeSchedule:
    def __init__(self, model: Any) -> None:
        self.model = model
        self.value: float | None = None

    def setValue(self, v: float) -> None:
        self.value = v


class FakeModel:
    def __init__(self) -> None:
        self.space_type = FakeObj("Office")
        self.zone = FakeObj("Z1", model=self)
        self.attr_calls: list[tuple[str, Any]] = []

    def getSpaceTypes(self) -> list[FakeObj]:
        return [self.space_type]

    def getThermalZones(self) -> list[FakeObj]:
        return [self.zone]

    def setString(self, attr: str, v: str) -> None:
        self.attr_calls.append((attr, v))


@pytest.fixture
def fake_os() -> SimpleNamespace:
    return SimpleNamespace(openstudiomodelcore=SimpleNamespace(ScheduleConstant=FakeSchedule))


def test_resolve_object_found_missing_and_unsupported() -> None:
    model = FakeModel()
    assert _resolve_osm_object(model, None, "SpaceType", "Office") is model.space_type
    assert _resolve_osm_object(model, None, "SpaceType", "Nope") is None
    assert _resolve_osm_object(model, None, "Bogus", "x") is None


def test_dotted_typed_setter(fake_os: SimpleNamespace) -> None:
    model = FakeModel()
    _apply_osm_mutations(model, fake_os, {"SpaceType_Office.lighting_power_density": 3})
    assert model.space_type.calls == [("lpd", 3.0)]


def test_dotted_missing_object_raises(fake_os: SimpleNamespace) -> None:
    with pytest.raises(OSMAttributeError, match="does not exist"):
        _apply_osm_mutations(FakeModel(), fake_os, {"SpaceType_Ghost.lighting_power_density": 1.0})


def test_simple_name_sets_string_on_model(fake_os: SimpleNamespace) -> None:
    model = FakeModel()
    _apply_osm_mutations(model, fake_os, {"foo": 2.5, "bar": "x", "flag": True})
    assert ("foo", "2.5") in model.attr_calls
    assert ("bar", "x") in model.attr_calls
    assert ("flag", "True") in model.attr_calls


def test_unsupported_value_type_raises(fake_os: SimpleNamespace) -> None:
    with pytest.raises(TypeError, match="Cannot coerce"):
        _apply_osm_mutations(FakeModel(), fake_os, {"foo": [1, 2]})


def test_thermal_zone_schedules(fake_os: SimpleNamespace) -> None:
    model = FakeModel()
    _set_osm_attribute(model.zone, fake_os, "ThermalZone", "cooling_setpoint", 24.0)
    _set_osm_attribute(model.zone, fake_os, "ThermalZone", "heating_setpoint", 20.0)
    assert model.zone.calls == [("cool", 24.0), ("heat", 20.0)]


def test_thermal_zone_schedule_direct(fake_os: SimpleNamespace) -> None:
    zone = FakeObj("Z", model=FakeModel())
    _set_thermal_zone_schedule(zone, fake_os, 18.0, "heating")
    assert zone.calls == [("heat", 18.0)]


@pytest.mark.parametrize(
    ("otype", "attr", "tag"),
    [
        ("Construction", "u_value", "u"),
        ("Lights", "lighting_level", "ll"),
        ("People", "people_per_floor_area", "ppa"),
    ],
)
def test_other_typed_setters(fake_os: SimpleNamespace, otype: str, attr: str, tag: str) -> None:
    obj = FakeObj("o")
    _set_osm_attribute(obj, fake_os, otype, attr, 1.5)
    assert obj.calls == [(tag, 1.5)]


def test_typed_setter_failure_wrapped(fake_os: SimpleNamespace) -> None:
    obj = SimpleNamespace(
        setThermalConductance=lambda v: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(OSMAttributeError, match="boom"):
        _set_osm_attribute(obj, fake_os, "Construction", "u_value", 1.0)


def test_unknown_attr_on_known_type_falls_back_to_string(fake_os: SimpleNamespace) -> None:
    obj = FakeObj("o")
    _set_osm_attribute(obj, fake_os, "Construction", "other", 4)
    assert obj.calls == [("str:other", "4")]


def test_non_scalar_value_raises(fake_os: SimpleNamespace) -> None:
    with pytest.raises(OSMAttributeError, match="No setter"):
        _set_osm_attribute(FakeObj("o"), fake_os, None, "attr", [1])


def _write_osw(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "measure_dir_name": "M",
                        "arguments": {"alpha": 1.0, "mode": "a"},
                    }
                ]
            }
        )
    )


def test_osw_parameters_missing_file_ignores_epw(tmp_path: Path) -> None:
    out = _apply_osw_parameters(tmp_path / "workflow.osw", {"__epw_file__": "x.epw", "k": 1})
    assert out == {"k": 1}


def test_osw_parameters_mutates_measure_args_and_returns_rest(tmp_path: Path) -> None:
    osw = tmp_path / "workflow.osw"
    _write_osw(osw)
    out = _apply_osw_parameters(
        osw,
        {"alpha": 5.0, "mode": {"label": "b", "index": 1}, "SpaceType_X.attr": 2.0},
    )
    assert out == {"SpaceType_X.attr": 2.0}
    args = json.loads(osw.read_text())["steps"][0]["arguments"]
    assert args["alpha"] == 5.0
    assert args["mode"] == "b"


def test_osw_parameters_epw_and_no_remaining(tmp_path: Path) -> None:
    osw = tmp_path / "workflow.osw"
    _write_osw(osw)
    out = _apply_osw_parameters(osw, {"__epw_file__": "w.epw"})
    assert out == {}
    assert "w.epw" in osw.read_text()
