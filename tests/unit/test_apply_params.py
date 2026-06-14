"""Tests for .osw measure-argument mutation with step disambiguation.

Covers:
  * Single-measure .osw (existing backward-compatible behaviour)
  * Multi-measure name collision detection
  * Dotted-name resolution (MeasureName.argument_name)
  * Missing measure name error
  * Pre-flight ambiguous name detection
  * _mutate_osw writes to the correct step when disambiguated
  * MappedParameter.step_index resolved by measure name
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osimflow.apply_params import (
    AmbiguousParameterError,
    MappedParameter,
    UnmappedParameterError,
    _mutate_osw,
    _resolve_dir_template,
    _select_template_file,
    apply_parameters,
    detect_template_type,
    parse_osw_arguments,
    preflight_check,
    resolve_template_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _write_osw(tmp_path: Path, data: dict) -> Path:
    """Write a JSON .osw file and return its path."""
    osw = tmp_path / "workflow.osw"
    osw.write_text(json.dumps(data, indent=2))
    return osw


SINGLE_MEASURE_OSW = {
    "steps": [
        {
            "measure_dir_name": "SetThermostatSchedule",
            "arguments": {"heating_setpoint": 20.0, "cooling_setpoint": 25.0},
        }
    ]
}

TWO_MEASURE_OSW = {
    "steps": [
        {
            "measure_dir_name": "SetThermostatSchedule",
            "arguments": {"heating_setpoint": 20.0, "cooling_setpoint": 25.0},
        },
        {
            "measure_dir_name": "SetEnvelopePerformance",
            "arguments": {
                "heating_setpoint": 18.0,
                "wwr": 0.4,
                "wall_r_value": 3.5,
            },
        },
    ]
}

THREE_MEASURE_OSW = {
    "steps": [
        {
            "measure_dir_name": "MeasureA",
            "arguments": {"alpha": 1.0, "shared": 10.0},
        },
        {
            "measure_dir_name": "MeasureB",
            "arguments": {"beta": 2.0, "shared": 20.0},
        },
        {
            "measure_dir_name": "MeasureC",
            "arguments": {"gamma": 3.0},
        },
    ]
}


# ---------------------------------------------------------------------------
# parse_osw_arguments — single measure (backward compat)
# ---------------------------------------------------------------------------
class TestParseOswSingleMeasure:
    """Single-measure .osw: plain names still work."""

    def test_plain_name_mapped(self, tmp_path: Path) -> None:
        osw = _write_osw(tmp_path, SINGLE_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        assert "heating_setpoint" in result
        m = result["heating_setpoint"]
        assert m.kind == "measure_argument"
        assert m.default == 20.0
        assert m.step_index == 0
        assert m.measure_name == "SetThermostatSchedule"

    def test_dotted_name_also_registered(self, tmp_path: Path) -> None:
        """Dotted form is registered alongside plain name."""
        osw = _write_osw(tmp_path, SINGLE_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        assert "SetThermostatSchedule.heating_setpoint" in result
        dotted = result["SetThermostatSchedule.heating_setpoint"]
        assert dotted.step_index == 0
        assert dotted.measure_name == "SetThermostatSchedule"
        assert dotted.name == "heating_setpoint"

    def test_all_arguments_parsed(self, tmp_path: Path) -> None:
        osw = _write_osw(tmp_path, SINGLE_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        assert "heating_setpoint" in result
        assert "cooling_setpoint" in result
        # 2 plain + 2 dotted = 4 total
        assert len(result) == 4


# ---------------------------------------------------------------------------
# parse_osw_arguments — multi-measure with collision
# ---------------------------------------------------------------------------
class TestParseOswMultiMeasure:
    """Multi-measure .osw: plain name is first-match, dotted resolves."""

    def test_plain_name_first_match(self, tmp_path: Path) -> None:
        """Plain 'heating_setpoint' maps to the first step that has it."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        m = result["heating_setpoint"]
        assert m.step_index == 0
        assert m.measure_name == "SetThermostatSchedule"

    def test_dotted_name_targets_step_0(self, tmp_path: Path) -> None:
        """Dotted form resolves to step 0 (SetThermostatSchedule)."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        dotted = result["SetThermostatSchedule.heating_setpoint"]
        assert dotted.step_index == 0
        assert dotted.measure_name == "SetThermostatSchedule"

    def test_dotted_name_targets_step_1(self, tmp_path: Path) -> None:
        """Dotted form resolves to step 1 (SetEnvelopePerformance)."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        dotted = result["SetEnvelopePerformance.heating_setpoint"]
        assert dotted.step_index == 1
        assert dotted.measure_name == "SetEnvelopePerformance"

    def test_unique_arg_only_in_one_step(self, tmp_path: Path) -> None:
        """Arguments unique to one step get both plain and dotted keys."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        # wwr is only in step 1
        assert "wwr" in result
        assert result["wwr"].step_index == 1
        assert "SetEnvelopePerformance.wwr" in result

    def test_measure_name_field_populated(self, tmp_path: Path) -> None:
        """Each mapping carries its measure_name."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        assert result["cooling_setpoint"].measure_name == "SetThermostatSchedule"
        assert result["wall_r_value"].measure_name == "SetEnvelopePerformance"

    def test_total_key_count(self, tmp_path: Path) -> None:
        """2 measures, 2+3 args with 1 collision.

        Plain keys (first-match wins): heating_setpoint, cooling_setpoint,
        wwr, wall_r_value = 4.
        Dotted keys: SetThermostatSchedule.heating_setpoint,
        SetThermostatSchedule.cooling_setpoint,
        SetEnvelopePerformance.heating_setpoint, SetEnvelopePerformance.wwr,
        SetEnvelopePerformance.wall_r_value = 5.
        Total = 9.
        """
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        assert len(result) == 9


# ---------------------------------------------------------------------------
# parse_osw_arguments — measure_dir_name fallback to name
# ---------------------------------------------------------------------------
class TestParseOswMeasureNameFallback:
    """measure_dir_name absent → fall back to 'name' field."""

    def test_falls_back_to_name_field(self, tmp_path: Path) -> None:
        osw = _write_osw(
            tmp_path,
            {
                "steps": [
                    {
                        "name": "MyMeasure",
                        "arguments": {"x": 1.0},
                    }
                ]
            },
        )
        result = parse_osw_arguments(osw)
        assert "MyMeasure.x" in result
        assert result["MyMeasure.x"].measure_name == "MyMeasure"

    def test_no_measure_name_graceful(self, tmp_path: Path) -> None:
        """Step without measure_dir_name or name: no dotted key registered."""
        osw = _write_osw(
            tmp_path,
            {"steps": [{"arguments": {"x": 1.0}}]},
        )
        result = parse_osw_arguments(osw)
        assert "x" in result
        assert result["x"].measure_name is None
        # No dotted key possible (no measure name)
        dotted_keys = [k for k in result if "." in k]
        assert dotted_keys == []


# ---------------------------------------------------------------------------
# parse_osw_arguments — three measures, multiple collisions
# ---------------------------------------------------------------------------
class TestParseOswThreeMeasure:
    """Three measures with shared args across two of them."""

    def test_shared_arg_plain_first_match(self, tmp_path: Path) -> None:
        osw = _write_osw(tmp_path, THREE_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        assert result["shared"].step_index == 0
        assert result["shared"].measure_name == "MeasureA"

    def test_shared_arg_dotted_to_measure_b(self, tmp_path: Path) -> None:
        osw = _write_osw(tmp_path, THREE_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        m = result["MeasureB.shared"]
        assert m.step_index == 1
        assert m.measure_name == "MeasureB"
        assert m.default == 20.0


# ---------------------------------------------------------------------------
# _mutate_osw — correct step mutation
# ---------------------------------------------------------------------------
class TestMutateOsw:
    """_mutate_osw writes to the correct step via measure_name."""

    def test_single_measure_mutation(self, tmp_path: Path) -> None:
        """Single measure: plain key mutates step 0."""
        osw = _write_osw(tmp_path, SINGLE_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        _mutate_osw(osw, {"heating_setpoint": 22.0}, mappings)
        data = json.loads(osw.read_text())
        assert data["steps"][0]["arguments"]["heating_setpoint"] == 22.0

    def test_dotted_name_writes_correct_step(self, tmp_path: Path) -> None:
        """Dotted key targets step 1, not step 0."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        _mutate_osw(osw, {"SetEnvelopePerformance.heating_setpoint": 15.0}, mappings)
        data = json.loads(osw.read_text())
        assert data["steps"][1]["arguments"]["heating_setpoint"] == 15.0
        # Step 0 is untouched
        assert data["steps"][0]["arguments"]["heating_setpoint"] == 20.0

    def test_dotted_name_step_0(self, tmp_path: Path) -> None:
        """Dotted key targets step 0 explicitly."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        _mutate_osw(osw, {"SetThermostatSchedule.heating_setpoint": 23.0}, mappings)
        data = json.loads(osw.read_text())
        assert data["steps"][0]["arguments"]["heating_setpoint"] == 23.0
        # Step 1 untouched
        assert data["steps"][1]["arguments"]["heating_setpoint"] == 18.0

    def test_multiple_params_different_steps(self, tmp_path: Path) -> None:
        """Two dotted params write to different steps simultaneously."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        _mutate_osw(
            osw,
            {
                "SetThermostatSchedule.heating_setpoint": 19.0,
                "SetEnvelopePerformance.wwr": 0.6,
            },
            mappings,
        )
        data = json.loads(osw.read_text())
        assert data["steps"][0]["arguments"]["heating_setpoint"] == 19.0
        assert data["steps"][1]["arguments"]["wwr"] == 0.6

    def test_preserves_unmutated_arguments(self, tmp_path: Path) -> None:
        """Other arguments in the same step are not touched."""
        osw = _write_osw(tmp_path, SINGLE_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        _mutate_osw(osw, {"heating_setpoint": 22.0}, mappings)
        data = json.loads(osw.read_text())
        assert data["steps"][0]["arguments"]["cooling_setpoint"] == 25.0

    def test_uses_plain_arg_name_from_mapping(self, tmp_path: Path) -> None:
        """The mapping.name (plain) is the key written into arguments."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        _mutate_osw(osw, {"SetEnvelopePerformance.wall_r_value": 5.0}, mappings)
        data = json.loads(osw.read_text())
        # The key in the arguments dict is "wall_r_value" (plain), not dotted
        assert "wall_r_value" in data["steps"][1]["arguments"]
        assert data["steps"][1]["arguments"]["wall_r_value"] == 5.0


# ---------------------------------------------------------------------------
# Pre-flight: unmapped parameter detection
# ---------------------------------------------------------------------------
class TestPreflightUnmapped:
    """UnmappedParameterError fires for unknown parameter names."""

    def test_unmapped_name_raises(self) -> None:
        mappings = {
            "x": MappedParameter(name="x", kind="measure_argument", step_index=0, measure_name="M")
        }
        with pytest.raises(UnmappedParameterError, match="not found"):
            preflight_check({"y": 1.0}, mappings)

    def test_all_mapped_passes(self) -> None:
        mappings = {
            "x": MappedParameter(name="x", kind="measure_argument", step_index=0, measure_name="M")
        }
        # Should not raise
        preflight_check({"x": 1.0}, mappings)

    def test_dotted_name_mapped_passes(self) -> None:
        mappings = {
            "M.x": MappedParameter(
                name="x", kind="measure_argument", step_index=0, measure_name="M"
            )
        }
        preflight_check({"M.x": 1.0}, mappings)


# ---------------------------------------------------------------------------
# Pre-flight: ambiguous parameter detection
# ---------------------------------------------------------------------------
class TestPreflightAmbiguous:
    """AmbiguousParameterError fires when a plain name matches multiple measures."""

    def test_ambiguous_plain_name_raises(self, tmp_path: Path) -> None:
        """Plain 'heating_setpoint' that appears in 2 measures is rejected."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        with pytest.raises(AmbiguousParameterError, match="heating_setpoint"):
            preflight_check({"heating_setpoint": 19.0}, mappings)

    def test_dotted_form_bypasses_ambiguity(self, tmp_path: Path) -> None:
        """Using dotted form avoids the ambiguous-name error."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        # Should not raise — dotted form is unambiguous
        preflight_check({"SetThermostatSchedule.heating_setpoint": 19.0}, mappings)

    def test_unique_name_no_ambiguity(self, tmp_path: Path) -> None:
        """An argument unique to one measure is not flagged as ambiguous."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        preflight_check({"wwr": 0.5}, mappings)

    def test_mixed_dotted_and_plain(self, tmp_path: Path) -> None:
        """Mix of dotted and non-ambiguous plain names passes."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        preflight_check(
            {
                "SetThermostatSchedule.heating_setpoint": 19.0,
                "wwr": 0.55,
                "wall_r_value": 4.0,
            },
            mappings,
        )

    def test_three_measure_shared_collision(self, tmp_path: Path) -> None:
        """'shared' in MeasureA and MeasureB is ambiguous."""
        osw = _write_osw(tmp_path, THREE_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        with pytest.raises(AmbiguousParameterError, match="shared"):
            preflight_check({"shared": 15.0}, mappings)

    def test_error_message_lists_measures(self, tmp_path: Path) -> None:
        """Error message names the colliding measures."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        with pytest.raises(
            AmbiguousParameterError,
            match=r"SetEnvelopePerformance.*SetThermostatSchedule|SetThermostatSchedule.*SetEnvelopePerformance",
        ):
            preflight_check({"heating_setpoint": 19.0}, mappings)


# ---------------------------------------------------------------------------
# MappedParameter.step_index resolved by measure name (not iteration order)
# ---------------------------------------------------------------------------
class TestStepIndexResolution:
    """step_index is resolved by matching measure_dir_name, not position."""

    def test_step_index_matches_measure_dir_name(self, tmp_path: Path) -> None:
        """Dotted key always gets the correct step_index regardless of order."""
        osw = _write_osw(
            tmp_path,
            {
                "steps": [
                    {"measure_dir_name": "ZebraMeasure", "arguments": {"x": 1}},
                    {"measure_dir_name": "AlphaMeasure", "arguments": {"y": 2}},
                ]
            },
        )
        result = parse_osw_arguments(osw)
        assert result["ZebraMeasure.x"].step_index == 0
        assert result["AlphaMeasure.y"].step_index == 1

    def test_reorder_preserves_resolution(self, tmp_path: Path) -> None:
        """If the .osw steps are reordered, dotted names still map correctly."""
        osw = _write_osw(
            tmp_path,
            {
                "steps": [
                    {"measure_dir_name": "BetaMeasure", "arguments": {"b": 2}},
                    {"measure_dir_name": "AlphaMeasure", "arguments": {"a": 1}},
                ]
            },
        )
        result = parse_osw_arguments(osw)
        assert result["AlphaMeasure.a"].step_index == 1
        assert result["BetaMeasure.b"].step_index == 0


# ---------------------------------------------------------------------------
# End-to-end: apply_parameters with .osw disambiguation
# ---------------------------------------------------------------------------
class TestApplyParametersOsw:
    """End-to-end apply_parameters with multi-measure .osw."""

    def test_e2e_dotted_name_mutation(self, tmp_path: Path) -> None:
        """apply_parameters with dotted key mutates correct step."""
        osw_dir = tmp_path / "template"
        osw_dir.mkdir()
        (osw_dir / "workflow.osw").write_text(json.dumps(TWO_MEASURE_OSW))
        out = tmp_path / "out" / "0001"
        apply_parameters(
            template=osw_dir,
            parameters={"SetEnvelopePerformance.wwr": 0.6},
            sample_id="0001",
            out=out,
        )
        mutated = json.loads((out / "workflow.osw").read_text())
        assert mutated["steps"][1]["arguments"]["wwr"] == 0.6
        assert mutated["steps"][0]["arguments"]["heating_setpoint"] == 20.0

    def test_e2e_unique_arg_no_disambiguation(self, tmp_path: Path) -> None:
        """Unique arg works with plain name, no disambiguation needed."""
        osw_dir = tmp_path / "template"
        osw_dir.mkdir()
        (osw_dir / "workflow.osw").write_text(json.dumps(TWO_MEASURE_OSW))
        out = tmp_path / "out" / "0002"
        apply_parameters(
            template=osw_dir,
            parameters={"wwr": 0.55},
            sample_id="0002",
            out=out,
        )
        mutated = json.loads((out / "workflow.osw").read_text())
        assert mutated["steps"][1]["arguments"]["wwr"] == 0.55

    def test_e2e_ambiguous_raises(self, tmp_path: Path) -> None:
        """apply_parameters raises AmbiguousParameterError for colliding plain name."""
        osw_dir = tmp_path / "template"
        osw_dir.mkdir()
        (osw_dir / "workflow.osw").write_text(json.dumps(TWO_MEASURE_OSW))
        out = tmp_path / "out" / "0003"
        with pytest.raises(AmbiguousParameterError):
            apply_parameters(
                template=osw_dir,
                parameters={"heating_setpoint": 19.0},
                sample_id="0003",
                out=out,
            )


# ---------------------------------------------------------------------------
# Pre-flight: fuzzy match suggestions
# ---------------------------------------------------------------------------
class TestPreflightFuzzyMatch:
    """UnmappedParameterError includes fuzzy-match suggestions for typos."""

    def test_typo_produces_suggestion(self) -> None:
        """A close miss gets a 'Did you mean?' suggestion."""
        mappings = {
            "window_to_wall_ratio": MappedParameter(
                name="window_to_wall_ratio", kind="measure_argument", step_index=0, measure_name="M"
            )
        }
        with pytest.raises(UnmappedParameterError, match="Did you mean") as exc_info:
            preflight_check({"windw_to_wall_ratio": 0.4}, mappings)
        assert "window_to_wall_ratio" in str(exc_info.value)

    def test_no_suggestion_when_nothing_close(self) -> None:
        """Totally unrelated name: no suggestion, just unmapped error."""
        mappings = {
            "alpha": MappedParameter(
                name="alpha", kind="measure_argument", step_index=0, measure_name="M"
            )
        }
        with pytest.raises(UnmappedParameterError, match="not found") as exc_info:
            preflight_check({"zzzzzzzzz": 1.0}, mappings)
        # Should NOT contain "Did you mean"
        assert "Did you mean" not in str(exc_info.value)

    def test_multiple_unmapped_all_get_suggestions(self) -> None:
        """Each unmapped name gets its own suggestion line."""
        mappings = {
            "window_to_wall_ratio": MappedParameter(
                name="window_to_wall_ratio", kind="measure_argument", step_index=0, measure_name="M"
            ),
            "wall_r_value": MappedParameter(
                name="wall_r_value", kind="measure_argument", step_index=0, measure_name="M"
            ),
        }
        with pytest.raises(UnmappedParameterError) as exc_info:
            preflight_check(
                {"windw_to_wall_ratio": 0.4, "wall_r_valu": 3.0},
                mappings,
            )
        msg = str(exc_info.value)
        assert "windw_to_wall_ratio" in msg
        assert "wall_r_valu" in msg
        assert msg.count("Did you mean") >= 2

    def test_suggests_dotted_name(self) -> None:
        """Fuzzy matching can suggest dotted names from the available keys."""
        mappings = {
            "SetEnvelopePerformance.wwr": MappedParameter(
                name="wwr",
                kind="measure_argument",
                step_index=0,
                measure_name="SetEnvelopePerformance",
            ),
        }
        with pytest.raises(UnmappedParameterError, match="Did you mean") as exc_info:
            preflight_check({"SetEnvelopePerformance.ww": 0.4}, mappings)
        assert "SetEnvelopePerformance.wwr" in str(exc_info.value)

    def test_error_message_starts_with_banner(self) -> None:
        """Error message starts with the banner for clear visibility."""
        mappings = {
            "x": MappedParameter(name="x", kind="measure_argument", step_index=0, measure_name="M")
        }
        with pytest.raises(UnmappedParameterError) as exc_info:
            preflight_check({"bad_name": 1.0}, mappings)
        assert str(exc_info.value).startswith("PRE-FLIGHT VALIDATION FAILED")

    def test_osw_based_fuzzy_match(self, tmp_path: Path) -> None:
        """Fuzzy matching works with real .osw parsed mappings."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        # "wall_r_valu" is close to "wall_r_value" — should produce a suggestion
        with pytest.raises(UnmappedParameterError, match="Did you mean") as exc_info:
            preflight_check({"wall_r_valu": 3.0}, mappings)
        msg = str(exc_info.value)
        assert "wall_r_value" in msg


# ===========================================================================
# Error-path tests for template-resolution helpers
# ===========================================================================


class TestDetectTemplateType:
    """Error paths in detect_template_type (lines 204-209)."""

    def test_unsupported_extension_raises(self, tmp_path: Path) -> None:
        """Line 209: unsupported extension raises ValueError."""
        bad = tmp_path / "model.txt"
        bad.write_text("not an osm or osw file")
        with pytest.raises(ValueError, match="Unsupported template type"):
            detect_template_type(bad)


class TestResolveDirTemplate:
    """Error paths in _resolve_dir_template (lines 225-226)."""

    def test_file_instead_of_directory_raises(self, tmp_path: Path) -> None:
        """Line 226: passing a file instead of a directory raises ValueError."""
        file_path = tmp_path / "not_a_dir.txt"
        file_path.write_text("hello")
        with pytest.raises(ValueError, match="_resolve_dir_template requires a directory"):
            _resolve_dir_template(file_path)

    def test_empty_directory_returns_none_none(self, tmp_path: Path) -> None:
        """Empty directory returns (None, None) - valid, not an error."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        osw, osm = _resolve_dir_template(empty_dir)
        assert osw is None
        assert osm is None


class TestResolveTemplateFile:
    """Error paths in resolve_template_file (line 275)."""

    def test_empty_directory_raises(self, tmp_path: Path) -> None:
        """Line 275: directory with no .osw and no .osm raises ValueError."""
        empty_dir = tmp_path / "empty_template"
        empty_dir.mkdir()
        with pytest.raises(ValueError, match="contains neither workflow.osw nor model.osm"):
            resolve_template_file(empty_dir)


class TestSelectTemplateFile:
    """Error paths in _select_template_file (line 317)."""

    def test_empty_directory_raises(self, tmp_path: Path) -> None:
        """Line 317: directory with no .osw and no .osm raises ValueError."""
        empty_dir = tmp_path / "empty_template"
        empty_dir.mkdir()
        with pytest.raises(ValueError, match="contains neither workflow.osw nor model.osm"):
            _select_template_file(empty_dir)
