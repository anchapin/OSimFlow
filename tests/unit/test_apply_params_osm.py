"""Tests for production .osm mutation via OpenStudio Python bindings.

These tests mock the OpenStudio bindings (they don't require a real
installation) and verify:
  * Dotted name parsing (ObjectType_InstanceName.attribute)
  * Type coercion (int→float, str, bool, unsupported types)
  * JSON stub mode still works (no regression)
  * Pre-flight path validation catches invalid object references
  * Production .osm mutation via mocked bindings
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow.apply_params import (
    DottedName,
    MappedParameter,
    OpenStudioBindingsMissingError,
    OSMAttributeError,
    _apply_osm_mutation,
    _coerce_value,
    _mutate_osm,
    _mutate_osm_production,
    _resolve_model_object,
    apply_parameters,
    parse_dotted_name,
    parse_osm_attributes,
    preflight_validate_osm_paths,
)


# ---------------------------------------------------------------------------
# Dotted name parsing
# ---------------------------------------------------------------------------
class TestParseDottedName:
    """Tests for parse_dotted_name()."""

    def test_simple_name_no_dot(self) -> None:
        """A name without a dot returns attribute-only DottedName."""
        result = parse_dotted_name("lighting_power_density")
        assert result == DottedName(
            object_type=None,
            object_name=None,
            attribute="lighting_power_density",
        )

    def test_dotted_name_with_underscore(self) -> None:
        """ObjectType_InstanceName.attribute parses correctly."""
        result = parse_dotted_name("SpaceType_Office.lighting_power_density")
        assert result == DottedName(
            object_type="SpaceType",
            object_name="Office",
            attribute="lighting_power_density",
        )

    def test_dotted_name_with_multiple_underscores(self) -> None:
        """Instance names may contain underscores themselves."""
        result = parse_dotted_name("ThermalZone_Core_Zone_1.cooling_setpoint")
        assert result.object_type == "ThermalZone"
        assert result.object_name == "Core_Zone_1"
        assert result.attribute == "cooling_setpoint"

    def test_dotted_name_without_underscore_raises(self) -> None:
        """A dotted name without an underscore in the object spec is invalid."""
        with pytest.raises(OSMAttributeError, match="underscore"):
            parse_dotted_name("InvalidSpec.attribute")

    def test_empty_attribute_raises(self) -> None:
        """A name ending with a dot (empty attribute) raises."""
        with pytest.raises(ValueError):
            # "Type_Name." splits to ["Type_Name", ""] which has empty attribute
            parse_dotted_name("Type_Name.")

    def test_empty_object_spec_raises(self) -> None:
        """A name starting with a dot (empty object spec) raises."""
        with pytest.raises(ValueError):
            parse_dotted_name(".attribute")


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------
class TestCoerceValue:
    """Tests for _coerce_value()."""

    def test_int_to_float(self) -> None:
        assert _coerce_value(42) == 42.0
        assert isinstance(_coerce_value(42), float)

    def test_float_passthrough(self) -> None:
        assert _coerce_value(3.14) == 3.14

    def test_string_passthrough(self) -> None:
        assert _coerce_value("hello") == "hello"

    def test_bool_passthrough(self) -> None:
        assert _coerce_value(True) is True
        assert _coerce_value(False) is False

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(TypeError, match="Cannot coerce"):
            _coerce_value([1, 2, 3])

    def test_none_raises(self) -> None:
        with pytest.raises(TypeError, match="Cannot coerce"):
            _coerce_value(None)


# ---------------------------------------------------------------------------
# JSON stub mode (no regression)
# ---------------------------------------------------------------------------
class TestJsonStubMode:
    """Verify that JSON-mode .osm stubs still work correctly."""

    def test_json_stub_mutates_attributes(self, tmp_path: Path) -> None:
        """JSON-mode .osm with simple attributes mutates correctly."""
        osm = tmp_path / "model.osm"
        osm.write_text(json.dumps({"attributes": {"window_u_value": 0.3, "hvac_setpoint": 21.0}}))
        mappings = {
            "window_u_value": MappedParameter(name="window_u_value", kind="attribute", default=0.3),
            "hvac_setpoint": MappedParameter(name="hvac_setpoint", kind="attribute", default=21.0),
        }
        _mutate_osm(osm, {"window_u_value": 0.7, "hvac_setpoint": 23.0}, mappings)
        data = json.loads(osm.read_text())
        assert data["attributes"]["window_u_value"] == 0.7
        assert data["attributes"]["hvac_setpoint"] == 23.0

    def test_json_stub_ignores_unmapped_parameters(self, tmp_path: Path) -> None:
        """Parameters not in mappings are silently skipped."""
        osm = tmp_path / "model.osm"
        osm.write_text(json.dumps({"attributes": {"x": 1.0}}))
        mappings = {"x": MappedParameter(name="x", kind="attribute", default=1.0)}
        _mutate_osm(osm, {"x": 2.0, "y": 3.0}, mappings)
        data = json.loads(osm.read_text())
        assert data["attributes"]["x"] == 2.0
        assert "y" not in data["attributes"]

    def test_json_stub_via_apply_parameters(self, tmp_path: Path) -> None:
        """End-to-end: apply_parameters with JSON .osm works as before."""
        osm = tmp_path / "model.osm"
        osm.write_text(json.dumps({"attributes": {"lpd": 8.0, "wwr": 0.4}}))
        out = tmp_path / "out" / "0001"
        apply_parameters(
            template=osm,
            parameters={"lpd": 10.0},
            sample_id="0001",
            out=out,
        )
        result = json.loads((out / "model.osm").read_text())
        assert result["attributes"]["lpd"] == 10.0
        assert result["attributes"]["wwr"] == 0.4  # unchanged

    def test_binary_osm_without_bindings_raises(self, tmp_path: Path) -> None:
        """Non-JSON .osm without bindings raises OpenStudioBindingsMissingError."""
        osm = tmp_path / "model.osm"
        osm.write_text("<OSMModel>...</OSMModel>")
        with pytest.raises(OpenStudioBindingsMissingError):
            _mutate_osm(osm, {}, {})


# ---------------------------------------------------------------------------
# Mocked OpenStudio bindings for production tests
# ---------------------------------------------------------------------------
def _make_mock_openstudio() -> types.ModuleType:
    """Create a mock openstudio module with the expected structure.

    The mock mimics the openstudio package layout:
      openstudio.openstudiomodelcore.Model
      openstudio.openstudiomodelcore.SpaceType
      openstudio.openstudiomodelcore.ThermalZone
      openstudio.openstudiomodelcore.Construction
      openstudio.openstudiomodelcore.Lights
      openstudio.openstudiomodelcore.People
      openstudio.openstudiomodelcore.ScheduleConstant
    """
    mock_os = types.ModuleType("openstudio")
    core = types.ModuleType("openstudio.openstudiomodelcore")
    mock_os.openstudiomodelcore = core  # type: ignore[attr-defined]

    # Model.load returns a mock model
    mock_model = MagicMock()
    mock_model.save = MagicMock()
    core.Model = MagicMock()
    core.Model.load = MagicMock(return_value=mock_model)

    # SpaceType mock
    st_mock = MagicMock()
    st_mock.nameString.return_value = "Office"
    lpd_result = MagicMock()
    lpd_result.is_initialized.return_value = True
    lpd_result.get.return_value = 8.0
    st_mock.lightingPowerPerFloorArea.return_value = lpd_result
    st_mock.setLightingPowerPerFloorArea = MagicMock(return_value=True)
    core.SpaceType = MagicMock()
    core.SpaceType.getSpaceTypes = MagicMock(return_value=[st_mock])

    # ThermalZone mock
    tz_mock = MagicMock()
    tz_mock.nameString.return_value = "Core Zone"
    tz_mock.model.return_value = mock_model
    clg_result = MagicMock()
    clg_result.is_initialized.return_value = True
    tz_mock.coolingSetpointTemperatureSchedule.return_value = clg_result
    htg_result = MagicMock()
    htg_result.is_initialized.return_value = True
    tz_mock.heatingSetpointTemperatureSchedule.return_value = htg_result
    core.ThermalZone = MagicMock()
    core.ThermalZone.getThermalZones = MagicMock(return_value=[tz_mock])

    # Construction mock
    c_mock = MagicMock()
    c_mock.nameString.return_value = "Exterior Wall"
    uf_result = MagicMock()
    uf_result.is_initialized.return_value = True
    uf_result.get.return_value = 0.5
    c_mock.thermalConductance.return_value = uf_result
    c_mock.setThermalConductance = MagicMock(return_value=True)
    core.Construction = MagicMock()
    core.Construction.getConstructions = MagicMock(return_value=[c_mock])

    # Lights mock
    lt_mock = MagicMock()
    lt_mock.nameString.return_value = "Office Lights"
    ll_result = MagicMock()
    ll_result.is_initialized.return_value = True
    ll_result.get.return_value = 100.0
    lt_mock.lightingLevel.return_value = ll_result
    lt_mock.setLightingLevel = MagicMock(return_value=True)
    core.Lights = MagicMock()
    core.Lights.getLights = MagicMock(return_value=[lt_mock])

    # People mock
    p_mock = MagicMock()
    p_mock.nameString.return_value = "Office People"
    ppd_result = MagicMock()
    ppd_result.is_initialized.return_value = True
    ppd_result.get.return_value = 0.05
    p_mock.peopleperSpaceFloorArea.return_value = ppd_result
    p_mock.setPeopleperSpaceFloorArea = MagicMock(return_value=True)
    core.People = MagicMock()
    core.People.getPeople = MagicMock(return_value=[p_mock])

    # ScheduleConstant mock
    schedule_mock = MagicMock()
    schedule_mock.setValue = MagicMock()
    core.ScheduleConstant = MagicMock(return_value=schedule_mock)
    core.BuildingStory = MagicMock()

    return mock_os


@pytest.fixture
def mock_openstudio() -> types.ModuleType:
    """Fixture that patches openstudio into sys.modules."""
    mock_os = _make_mock_openstudio()
    with patch.dict(
        sys.modules,
        {
            "openstudio": mock_os,
            "openstudio.openstudiomodelcore": mock_os.openstudiomodelcore,  # type: ignore[attr-defined]
        },
    ):
        yield mock_os


# ---------------------------------------------------------------------------
# Production .osm parsing with mocked bindings
# ---------------------------------------------------------------------------
class TestParseOsmProduction:
    """Tests for _parse_osm_production() with mocked bindings."""

    def test_discovers_space_type_attributes(
        self, tmp_path: Path, mock_openstudio: types.ModuleType
    ) -> None:
        """SpaceType attributes are discovered with object_type/object_name."""
        osm = tmp_path / "model.osm"
        osm.write_text("<OSMModel>not json</OSMModel>")
        # Need to patch find_spec too so it returns non-None
        with patch("osimflow.apply_params.importlib.util.find_spec", return_value=True):
            result = parse_osm_attributes(osm)
        assert "SpaceType_Office.lighting_power_density" in result
        m = result["SpaceType_Office.lighting_power_density"]
        assert m.object_type == "SpaceType"
        assert m.object_name == "Office"
        assert m.kind == "attribute"
        assert m.default == 8.0

    def test_discovers_construction_attributes(
        self, tmp_path: Path, mock_openstudio: types.ModuleType
    ) -> None:
        """Construction attributes are discovered correctly."""
        osm = tmp_path / "model.osm"
        osm.write_text("<OSMModel>not json</OSMModel>")
        with patch("osimflow.apply_params.importlib.util.find_spec", return_value=True):
            result = parse_osm_attributes(osm)
        assert "Construction_Exterior Wall.u_value" in result
        m = result["Construction_Exterior Wall.u_value"]
        assert m.object_type == "Construction"
        assert m.object_name == "Exterior Wall"


# ---------------------------------------------------------------------------
# Production .osm mutation with mocked bindings
# ---------------------------------------------------------------------------
class TestMutateOsmProduction:
    """Tests for _mutate_osm_production() with mocked bindings."""

    def test_mutates_space_type_lpd(
        self, tmp_path: Path, mock_openstudio: types.ModuleType
    ) -> None:
        """Mutating SpaceType_Office.lighting_power_density calls the setter."""
        osm = tmp_path / "model.osm"
        osm.write_text("<OSMModel>not json</OSMModel>")
        mappings = {
            "SpaceType_Office.lighting_power_density": MappedParameter(
                name="SpaceType_Office.lighting_power_density",
                kind="attribute",
                default=8.0,
                object_type="SpaceType",
                object_name="Office",
            )
        }
        _mutate_osm_production(osm, {"SpaceType_Office.lighting_power_density": 12.0}, mappings)

        # Verify the setter was called with the coerced value
        core = mock_openstudio.openstudiomodelcore
        st = core.SpaceType.getSpaceTypes(MagicMock())[0]
        st.setLightingPowerPerFloorArea.assert_called_once_with(12.0)

    def test_mutates_construction_u_value(
        self, tmp_path: Path, mock_openstudio: types.ModuleType
    ) -> None:
        """Mutating Construction u_value calls the setter."""
        osm = tmp_path / "model.osm"
        osm.write_text("<OSMModel>not json</OSMModel>")
        mappings = {
            "Construction_Exterior Wall.u_value": MappedParameter(
                name="Construction_Exterior Wall.u_value",
                kind="attribute",
                default=0.5,
                object_type="Construction",
                object_name="Exterior Wall",
            )
        }
        _mutate_osm_production(
            osm,
            {"Construction_Exterior Wall.u_value": 0.3},
            mappings,
        )
        core = mock_openstudio.openstudiomodelcore
        c = core.Construction.getConstructions(MagicMock())[0]
        c.setThermalConductance.assert_called_once_with(0.3)

    def test_int_coerced_to_float_for_numeric_setter(
        self, tmp_path: Path, mock_openstudio: types.ModuleType
    ) -> None:
        """An int parameter is coerced to float before calling the setter."""
        osm = tmp_path / "model.osm"
        osm.write_text("<OSMModel>not json</OSMModel>")
        mappings = {
            "SpaceType_Office.lighting_power_density": MappedParameter(
                name="SpaceType_Office.lighting_power_density",
                kind="attribute",
                default=8.0,
                object_type="SpaceType",
                object_name="Office",
            )
        }
        _mutate_osm_production(
            osm,
            {"SpaceType_Office.lighting_power_density": 15},
            mappings,
        )
        core = mock_openstudio.openstudiomodelcore
        st = core.SpaceType.getSpaceTypes(MagicMock())[0]
        # Int 15 should be coerced to 15.0
        st.setLightingPowerPerFloorArea.assert_called_once_with(15.0)

    def test_model_saved_after_mutation(
        self, tmp_path: Path, mock_openstudio: types.ModuleType
    ) -> None:
        """The model is saved back to the file after all mutations."""
        osm = tmp_path / "model.osm"
        osm.write_text("<OSMModel>not json</OSMModel>")
        _mutate_osm_production(
            osm,
            {},
            {},
        )
        core = mock_openstudio.openstudiomodelcore
        model = core.Model.load.return_value
        model.save.assert_called_once_with(str(osm), overwrite=True)

    def test_unresolved_object_raises_osm_attribute_error(
        self, tmp_path: Path, mock_openstudio: types.ModuleType
    ) -> None:
        """A dotted path to a non-existent object raises OSMAttributeError."""
        osm = tmp_path / "model.osm"
        osm.write_text("<OSMModel>not json</OSMModel>")
        mappings = {
            "SpaceType_NonExistent.lpd": MappedParameter(
                name="SpaceType_NonExistent.lpd",
                kind="attribute",
                object_type="SpaceType",
                object_name="NonExistent",
            )
        }
        with pytest.raises(OSMAttributeError, match="Cannot resolve"):
            _mutate_osm_production(
                osm,
                {"SpaceType_NonExistent.lpd": 10.0},
                mappings,
            )


# ---------------------------------------------------------------------------
# _mutate_osm dispatch (JSON vs production)
# ---------------------------------------------------------------------------
class TestMutateOsmDispatch:
    """Tests for _mutate_osm routing between JSON and production paths."""

    def test_dispatches_to_production_for_xml(
        self, tmp_path: Path, mock_openstudio: types.ModuleType
    ) -> None:
        """Non-JSON .osm with bindings available calls _mutate_osm_production."""
        osm = tmp_path / "model.osm"
        osm.write_text("<OSMModel>data</OSMModel>")
        mappings = {
            "SpaceType_Office.lighting_power_density": MappedParameter(
                name="SpaceType_Office.lighting_power_density",
                kind="attribute",
                object_type="SpaceType",
                object_name="Office",
            )
        }
        # Patch find_spec so _mutate_osm sees the mock openstudio as available
        with patch("osimflow.apply_params.importlib.util.find_spec", return_value=True):
            _mutate_osm(osm, {"SpaceType_Office.lighting_power_density": 10.0}, mappings)
        core = mock_openstudio.openstudiomodelcore
        st = core.SpaceType.getSpaceTypes(MagicMock())[0]
        st.setLightingPowerPerFloorArea.assert_called_once_with(10.0)


# ---------------------------------------------------------------------------
# Pre-flight path validation
# ---------------------------------------------------------------------------
class TestPreflightValidateOsmPaths:
    """Tests for preflight_validate_osm_paths()."""

    def test_no_validation_when_model_is_none(self) -> None:
        """When model is None, validation is a no-op."""
        result = preflight_validate_osm_paths(
            {"x": 1.0},
            {
                "x": MappedParameter(
                    name="x", kind="attribute", object_type="SpaceType", object_name="Office"
                )
            },
            model=None,
        )
        assert result == []

    def test_valid_dotted_path_passes(self, mock_openstudio: types.ModuleType) -> None:
        """A path referencing an existing model object passes validation."""
        mock_model = MagicMock()
        mappings = {
            "SpaceType_Office.lpd": MappedParameter(
                name="SpaceType_Office.lpd",
                kind="attribute",
                object_type="SpaceType",
                object_name="Office",
            )
        }
        # The mock SpaceType.getSpaceTypes returns an object with nameString="Office"
        result = preflight_validate_osm_paths(
            {"SpaceType_Office.lpd": 10.0},
            mappings,
            model=mock_model,
        )
        assert result == []

    def test_invalid_object_name_raises(self, mock_openstudio: types.ModuleType) -> None:
        """A path referencing a non-existent object name raises."""
        mock_model = MagicMock()
        mappings = {
            "SpaceType_Ghost.lpd": MappedParameter(
                name="SpaceType_Ghost.lpd",
                kind="attribute",
                object_type="SpaceType",
                object_name="Ghost",
            )
        }
        with pytest.raises(OSMAttributeError, match="object references do not exist"):
            preflight_validate_osm_paths(
                {"SpaceType_Ghost.lpd": 10.0},
                mappings,
                model=mock_model,
            )

    def test_simple_names_skip_validation(self, mock_openstudio: types.ModuleType) -> None:
        """Simple attribute names (no object_type) are not validated."""
        mock_model = MagicMock()
        mappings = {
            "lighting_power_density": MappedParameter(
                name="lighting_power_density", kind="attribute"
            )
        }
        result = preflight_validate_osm_paths(
            {"lighting_power_density": 10.0},
            mappings,
            model=mock_model,
        )
        assert result == []

    def test_measure_arguments_skip_validation(self, mock_openstudio: types.ModuleType) -> None:
        """Measure arguments (kind='measure_argument') are not validated."""
        mock_model = MagicMock()
        mappings = {"wwr": MappedParameter(name="wwr", kind="measure_argument", step_index=0)}
        result = preflight_validate_osm_paths({"wwr": 0.6}, mappings, model=mock_model)
        assert result == []


# ---------------------------------------------------------------------------
# _resolve_model_object
# ---------------------------------------------------------------------------
class TestResolveModelObject:
    """Tests for _resolve_model_object()."""

    def test_resolves_existing_object(self, mock_openstudio: types.ModuleType) -> None:
        """Finds an object by type and name."""
        mock_model = MagicMock()
        # SpaceType mock has nameString="Office"
        result = _resolve_model_object(mock_model, mock_openstudio, "SpaceType", "Office")
        assert result is not None

    def test_returns_none_for_missing_object(self, mock_openstudio: types.ModuleType) -> None:
        """Returns None when the named object does not exist."""
        mock_model = MagicMock()
        result = _resolve_model_object(mock_model, mock_openstudio, "SpaceType", "DoesNotExist")
        assert result is None

    def test_returns_none_for_unsupported_type(self, mock_openstudio: types.ModuleType) -> None:
        """Returns None for an object type not in the dispatch table."""
        mock_model = MagicMock()
        result = _resolve_model_object(mock_model, mock_openstudio, "UnsupportedType", "Foo")
        assert result is None


# ---------------------------------------------------------------------------
# _apply_osm_mutation
# ---------------------------------------------------------------------------
class TestApplyOsmMutation:
    """Tests for _apply_osm_mutation()."""

    def test_applies_dotted_name_mutation(self, mock_openstudio: types.ModuleType) -> None:
        """A dotted name resolves to the correct object and calls its setter."""
        mock_model = MagicMock()
        mapping = MappedParameter(
            name="SpaceType_Office.lighting_power_density",
            kind="attribute",
            default=8.0,
            object_type="SpaceType",
            object_name="Office",
        )
        _apply_osm_mutation(mock_model, mock_openstudio, mapping, 12.0)
        core = mock_openstudio.openstudiomodelcore
        st = core.SpaceType.getSpaceTypes(MagicMock())[0]
        st.setLightingPowerPerFloorArea.assert_called_once_with(12.0)

    def test_raises_for_missing_object(self, mock_openstudio: types.ModuleType) -> None:
        """Raises OSMAttributeError if the object cannot be resolved."""
        mock_model = MagicMock()
        mapping = MappedParameter(
            name="SpaceType_Missing.lpd",
            kind="attribute",
            object_type="SpaceType",
            object_name="Missing",
        )
        with pytest.raises(OSMAttributeError, match="Cannot resolve"):
            _apply_osm_mutation(mock_model, mock_openstudio, mapping, 10.0)


# ---------------------------------------------------------------------------
# Integration test gated by OSIMFLOW_HAS_OPENSTUDIO
# ---------------------------------------------------------------------------

_HAS_OPENSTUDIO = os.environ.get("OSIMFLOW_HAS_OPENSTUDIO", "0") == "1"
_skip_reason = (
    "Set OSIMFLOW_HAS_OPENSTUDIO=1 and install openstudio bindings to run"
)


@pytest.mark.skipif(not _HAS_OPENSTUDIO, reason=_skip_reason)
class TestProductionOpenStudioBindings:
    """Integration tests that require real OpenStudio Python bindings.

    These tests are only executed when the environment variable
    ``OSIMFLOW_HAS_OPENSTUDIO=1`` is set **and** the ``openstudio`` package
    is importable. They exercise the real production code-path end-to-end
    with a minimal OpenStudio model, confirming that:

    * ``parse_osm_attributes`` can walk a real model and return a non-empty
      mapping.
    * ``_mutate_osm`` can write a modified .osm file via the bindings.
    * JSON-mode stubs continue to work unchanged (no regression).
    """

    @staticmethod
    def _create_minimal_osm(tmp_path: Path) -> Path:
        """Create a minimal valid .osm file using the OpenStudio SDK.

        The model contains a single ``SpaceType`` named "Office" so that
        ``parse_osm_attributes`` has something to discover.
        """
        import openstudio  # noqa: PLC0415

        model = openstudio.openstudiomodelcore.Model()
        st = openstudio.openstudiomodelcore.SpaceType(model)
        st.setName("Office")
        osm_path = tmp_path / "model.osm"
        model.save(str(osm_path), overwrite=True)
        return osm_path

    def test_parse_osm_attributes_returns_mapping(self, tmp_path: Path) -> None:
        """parse_osm_attributes discovers attributes from a real .osm."""
        osm_path = self._create_minimal_osm(tmp_path)
        mappings = parse_osm_attributes(osm_path)
        assert isinstance(mappings, dict)
        # At minimum, we expect SpaceType-related attributes.
        assert len(mappings) > 0

    def test_mutate_osm_production_writes_file(self, tmp_path: Path) -> None:
        """_mutate_osm writes a modified .osm for production files."""
        osm_path = self._create_minimal_osm(tmp_path)

        # Discover what's available
        mappings = parse_osm_attributes(osm_path)

        # Pick the first attribute mapping to mutate
        first_name = next(iter(mappings))
        params = {first_name: mappings[first_name].default}

        _mutate_osm(osm_path, params, mappings)

        mutated_text = osm_path.read_text()
        # The file should still be valid (non-empty) and may differ
        assert len(mutated_text) > 0
        # The file must be a real .osm (XML/text), not JSON
        assert not mutated_text.lstrip().startswith("{")

    def test_json_stub_still_works(self, tmp_path: Path) -> None:
        """JSON-mode stub files work without the production path."""
        stub = tmp_path / "stub.osm"
        stub.write_text(json.dumps({"attributes": {"lpd": 10.0}}))

        mappings = parse_osm_attributes(stub)
        assert "lpd" in mappings

        _mutate_osm(stub, {"lpd": 15.0}, mappings)

        data = json.loads(stub.read_text())
        assert data["attributes"]["lpd"] == 15.0
