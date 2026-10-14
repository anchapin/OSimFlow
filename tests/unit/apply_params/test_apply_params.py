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
import logging
import shutil
from pathlib import Path

import pytest

from osimflow.apply_params import (
    AmbiguousParameterError,
    CrossMeasureConflictError,
    MappedParameter,
    UnmappedParameterError,
    _build_mappings_without_osm_bindings,
    _infer_osm_attribute_mapping,
    _load_custom_apply,
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
        assert "__SKIP__" in result
        assert "SetThermostatSchedule.__SKIP__" in result
        # 3 plain (heating_setpoint, cooling_setpoint, __SKIP__) + 3 dotted = 6 total
        assert len(result) == 6


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
        """2 measures, 2+3 args + __SKIP__ per step with 2 collisions.

        Plain keys (first-match wins): heating_setpoint, cooling_setpoint,
        __SKIP__, wwr, wall_r_value = 5.
        Dotted keys: SetThermostatSchedule.heating_setpoint,
        SetThermostatSchedule.cooling_setpoint,
        SetThermostatSchedule.__SKIP__,
        SetEnvelopePerformance.heating_setpoint, SetEnvelopePerformance.wwr,
        SetEnvelopePerformance.wall_r_value,
        SetEnvelopePerformance.__SKIP__ = 7.
        Total = 12.
        """
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        result = parse_osw_arguments(osw)
        assert len(result) == 12


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
# Pre-flight: cross-measure conflict detection (issue #431)
# ---------------------------------------------------------------------------
class TestPreflightCrossMeasureConflict:
    """CrossMeasureConflictError fires when the same arg is set for multiple measures."""

    def test_both_dotted_names_raises(self, tmp_path: Path) -> None:
        """Specifying both dotted names for the same argument raises."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        with pytest.raises(
            CrossMeasureConflictError,
            match="heating_setpoint.*SetThermostatSchedule.*SetEnvelopePerformance",
        ):
            preflight_check(
                {
                    "SetThermostatSchedule.heating_setpoint": 19.0,
                    "SetEnvelopePerformance.heating_setpoint": 21.0,
                },
                mappings,
            )

    def test_single_dotted_name_no_conflict(self, tmp_path: Path) -> None:
        """Specifying only one dotted name (even if arg exists in other measure) passes."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        # Should not raise — only one measure is targeted
        preflight_check(
            {"SetThermostatSchedule.heating_setpoint": 19.0},
            mappings,
        )

    def test_plain_ambiguous_vs_dotted_conflict(self, tmp_path: Path) -> None:
        """Plain ambiguous name raises AmbiguousParameterError (not CrossMeasureConflictError).

        The ambiguous check runs before the conflict check and catches plain names first.
        """
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        # Plain name heating_setpoint is ambiguous — AmbiguousParameterError
        with pytest.raises(AmbiguousParameterError, match="heating_setpoint"):
            preflight_check({"heating_setpoint": 19.0}, mappings)

    def test_three_measure_all_specified(self, tmp_path: Path) -> None:
        """When all three measures share an arg, specifying two dotted forms raises."""
        osw = _write_osw(tmp_path, THREE_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        with pytest.raises(
            CrossMeasureConflictError,
            match="shared.*MeasureA.*MeasureB",
        ):
            preflight_check(
                {
                    "MeasureA.shared": 10.0,
                    "MeasureB.shared": 20.0,
                },
                mappings,
            )

    def test_conflict_error_message_suggests_single_measure(self, tmp_path: Path) -> None:
        """Error message suggests using just one dotted form."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        with pytest.raises(
            CrossMeasureConflictError,
            match=r"Choose ONE.*dotted form",
        ):
            preflight_check(
                {
                    "SetThermostatSchedule.heating_setpoint": 19.0,
                    "SetEnvelopePerformance.heating_setpoint": 21.0,
                },
                mappings,
            )

    def test_no_conflict_mixed_dotted_unique_args(self, tmp_path: Path) -> None:
        """Mixed dotted names for unique args (no overlap) passes."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        # No conflict: SetThermostatSchedule.cooling_setpoint and
        # SetEnvelopePerformance.wwr are different args
        preflight_check(
            {
                "SetThermostatSchedule.cooling_setpoint": 24.0,
                "SetEnvelopePerformance.wwr": 0.45,
            },
            mappings,
        )

    # -- OSIMFLOW_ALLOW_CROSS_MEASURE_CONFLICT escape hatch (issue #1638) --

    _CONFLICTING_PARAMS = {
        "SetThermostatSchedule.heating_setpoint": 19.0,
        "SetEnvelopePerformance.heating_setpoint": 21.0,
    }

    def test_env_bypass_downgrades_to_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Setting the env var (any non-empty value) downgrades the error to a warning."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        # Pin current truthiness semantics: any non-empty string bypasses.
        monkeypatch.setenv("OSIMFLOW_ALLOW_CROSS_MEASURE_CONFLICT", "1")
        with caplog.at_level(logging.WARNING, logger="osimflow.apply_params"):
            preflight_check(self._CONFLICTING_PARAMS, mappings)  # should NOT raise
        assert "OSIMFLOW_ALLOW_CROSS_MEASURE_CONFLICT" in caplog.text
        assert "heating_setpoint" in caplog.text

    def test_env_bypass_empty_value_still_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty-string value is falsy — the bypass needs a non-empty value."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        monkeypatch.setenv("OSIMFLOW_ALLOW_CROSS_MEASURE_CONFLICT", "")
        with pytest.raises(CrossMeasureConflictError, match="heating_setpoint"):
            preflight_check(self._CONFLICTING_PARAMS, mappings)

    def test_env_bypass_unset_still_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset env var keeps the hard error (guards the ambient-environment case)."""
        osw = _write_osw(tmp_path, TWO_MEASURE_OSW)
        mappings = parse_osw_arguments(osw)
        monkeypatch.delenv("OSIMFLOW_ALLOW_CROSS_MEASURE_CONFLICT", raising=False)
        with pytest.raises(CrossMeasureConflictError, match="heating_setpoint"):
            preflight_check(self._CONFLICTING_PARAMS, mappings)


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
    """Error and resolution paths in resolve_template_file."""

    def test_empty_directory_raises(self, tmp_path: Path) -> None:
        """Directory with no .osw and no .osm raises ValueError."""
        empty_dir = tmp_path / "empty_template"
        empty_dir.mkdir()
        with pytest.raises(ValueError, match="contains neither workflow.osw nor model.osm"):
            resolve_template_file(empty_dir)

    def test_file_path_returned_directly(self, tmp_path: Path) -> None:
        """Non-directory path is returned as-is."""
        osw = _write_osw(tmp_path, SINGLE_MEASURE_OSW)
        assert resolve_template_file(osw) == osw

    def test_directory_prefers_osw_or_falls_back_to_osm(self, tmp_path: Path) -> None:
        """Directory with workflow.osw returns it; directory with only model.osm returns model.osm."""
        osm_only = tmp_path / "osm_only"
        osm_only.mkdir()
        osm = osm_only / "model.osm"
        osm.write_text("{}")
        assert resolve_template_file(osm_only) == osm

        both = tmp_path / "both"
        both.mkdir()
        osw = _write_osw(both, SINGLE_MEASURE_OSW)
        (both / "model.osm").write_text("{}")
        assert resolve_template_file(both) == osw


class TestSelectTemplateFile:
    """Error paths in _select_template_file (line 317)."""

    def test_empty_directory_raises(self, tmp_path: Path) -> None:
        """Line 317: directory with no .osw and no .osm raises ValueError."""
        empty_dir = tmp_path / "empty_template"
        empty_dir.mkdir()
        with pytest.raises(ValueError, match="contains neither workflow.osw nor model.osm"):
            _select_template_file(empty_dir)


class TestInferAndBuildMappingsWithoutOsmBindings:
    """Issue #2024: _infer_osm_attribute_mapping and _build_mappings_without_osm_bindings."""

    def test_infer_simple_known_and_unknown_attributes(self) -> None:
        mapped = _infer_osm_attribute_mapping("lighting_power_density")
        assert mapped is not None
        assert mapped.kind == "attribute"
        assert mapped.name == "lighting_power_density"
        assert _infer_osm_attribute_mapping("unknown_simple_attr") is None

    def test_infer_dotted_known_invalid_and_unknown_object_type(self) -> None:
        mapped = _infer_osm_attribute_mapping("SpaceType_Office.lighting_power_density")
        assert mapped is not None
        assert mapped.kind == "attribute"
        assert mapped.object_type == "SpaceType"
        assert mapped.object_name == "Office"
        # No underscore on left side -> OSMAttributeError caught -> None
        assert _infer_osm_attribute_mapping("MeasureName.arg_name") is None
        # Unknown object type -> None
        assert _infer_osm_attribute_mapping("UnknownType_Obj.attr") is None

    def test_build_mappings_dir_osw_only_and_osm_only(self, tmp_path: Path) -> None:
        osw_only = tmp_path / "osw_only"
        osw_only.mkdir()
        _write_osw(osw_only, SINGLE_MEASURE_OSW)
        m_osw = _build_mappings_without_osm_bindings(
            osw_only, ["heating_setpoint", "lighting_power_density"]
        )
        assert "heating_setpoint" in m_osw
        assert "lighting_power_density" not in m_osw

        osm_only = tmp_path / "osm_only"
        osm_only.mkdir()
        (osm_only / "model.osm").write_text("OS:Version,3.11.0;")
        m_osm = _build_mappings_without_osm_bindings(
            osm_only,
            [
                "lighting_power_density",
                "SpaceType_Office.lighting_power_density",
                "nonexistent_var",
            ],
        )
        assert "lighting_power_density" in m_osm
        assert "SpaceType_Office.lighting_power_density" in m_osm
        assert "nonexistent_var" not in m_osm

    def test_build_mappings_single_file_osw_and_osm(self, tmp_path: Path) -> None:
        osw_file = _write_osw(tmp_path, SINGLE_MEASURE_OSW)
        m_osw = _build_mappings_without_osm_bindings(osw_file, ["heating_setpoint"])
        assert "heating_setpoint" in m_osw

        osm_file = tmp_path / "single.osm"
        osm_file.write_text("OS:Version,3.11.0;")
        m_osm = _build_mappings_without_osm_bindings(
            osm_file, ["cooling_setpoint", "unknown_param"]
        )
        assert "cooling_setpoint" in m_osm
        assert "unknown_param" not in m_osm


def test_load_custom_apply_deprecated_raises(tmp_path: Path) -> None:
    """Deprecated _load_custom_apply warns and raises RuntimeError."""
    with pytest.deprecated_call(), pytest.raises(RuntimeError, match="has been removed"):
        _load_custom_apply(tmp_path / "custom.py")


# ---------------------------------------------------------------------------
# Issue #2067: measure.xml argument + __SKIP__ discovery & Boolean coercion
# ---------------------------------------------------------------------------
def _write_measure_xml(measure_dir: Path, arguments_xml: str) -> Path:
    measure_dir.mkdir(parents=True, exist_ok=True)
    xml_path = measure_dir / "measure.xml"
    xml_path.write_text(
        f"<measure><arguments>{arguments_xml}</arguments></measure>",
        encoding="utf-8",
    )
    return xml_path


class TestIssue2067MeasureXmlAndSkip:
    """Issue #2067: index measure.xml args + __SKIP__ and coerce Boolean strings."""

    def test_parse_osw_indexes_measure_xml_args_and_skip_with_empty_step_arguments(
        self, tmp_path: Path
    ) -> None:
        _write_measure_xml(
            tmp_path / "measures" / "SetLighting",
            """
            <argument>
              <name>lpd_multiplier</name>
              <type>Double</type>
              <required>true</required>
              <default_value>1.0</default_value>
            </argument>
            <argument>
              <name>enable_daylighting</name>
              <type>Boolean</type>
              <required>true</required>
              <default_value>false</default_value>
            </argument>
            """,
        )
        osw = _write_osw(
            tmp_path,
            {"steps": [{"measure_dir_name": "SetLighting", "arguments": {}}]},
        )

        result = parse_osw_arguments(osw)
        assert "lpd_multiplier" in result
        assert "SetLighting.lpd_multiplier" in result
        assert result["SetLighting.lpd_multiplier"].step_index == 0
        assert result["SetLighting.lpd_multiplier"].default == "1.0"

        assert "enable_daylighting" in result
        assert "SetLighting.enable_daylighting" in result
        assert result["SetLighting.enable_daylighting"].default is False

        assert "__SKIP__" in result
        assert "SetLighting.__SKIP__" in result
        assert result["SetLighting.__SKIP__"].default is False
        assert result["SetLighting.__SKIP__"].step_index == 0

    def test_parse_osw_resolves_measure_paths_from_workflow_osw(self, tmp_path: Path) -> None:
        custom_measures = tmp_path / "external_measures"
        _write_measure_xml(
            custom_measures / "CustomHvacMeasure",
            """
            <argument>
              <name>cop</name>
              <type>Double</type>
              <default_value>3.5</default_value>
            </argument>
            """,
        )
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        osw = _write_osw(
            pkg_dir,
            {
                "measure_paths": ["../external_measures"],
                "steps": [{"measure_dir_name": "CustomHvacMeasure", "arguments": {}}],
            },
        )

        result = parse_osw_arguments(osw)
        assert "cop" in result
        assert "CustomHvacMeasure.cop" in result
        assert result["CustomHvacMeasure.cop"].step_index == 0

    def test_preflight_and_default_apply_parameters_succeed_for_xml_arg_and_skip(
        self, tmp_path: Path
    ) -> None:
        from osimflow.work import default_apply_parameters

        pkg = tmp_path / "template"
        _write_measure_xml(
            pkg / "measures" / "SetLighting",
            """
            <argument>
              <name>lpd_multiplier</name>
              <type>Double</type>
              <default_value>1.0</default_value>
            </argument>
            <argument>
              <name>enable_daylighting</name>
              <type>Boolean</type>
              <default_value>false</default_value>
            </argument>
            """,
        )
        osw = _write_osw(
            pkg,
            {"steps": [{"measure_dir_name": "SetLighting", "arguments": {}}]},
        )
        (pkg / "model.osm").write_text(json.dumps({"attributes": {}}))

        mappings = parse_osw_arguments(osw)
        params = {
            "SetLighting.lpd_multiplier": 0.85,
            "SetLighting.__SKIP__": "TRUE",
            "SetLighting.enable_daylighting": "false",
        }
        preflight_check(params, mappings)

        sample_out = tmp_path / "sample_0001"
        shutil.copytree(pkg, sample_out)
        default_apply_parameters(pkg, dict(params), "0001", sample_out)

        mutated = json.loads((sample_out / "workflow.osw").read_text())
        step_args = mutated["steps"][0]["arguments"]
        assert step_args["lpd_multiplier"] == 0.85
        assert step_args["__SKIP__"] is True
        assert step_args["enable_daylighting"] is False

    @pytest.mark.parametrize(
        ("raw_skip", "raw_bool", "expected_skip", "expected_bool"),
        [
            ("TRUE", "FALSE", True, False),
            ("FALSE", "TRUE", False, True),
            ("true", "false", True, False),
            ("false", "true", False, True),
        ],
    )
    def test_mutate_osw_coerces_boolean_strings_for_skip_and_boolean_measure_xml_args(
        self,
        tmp_path: Path,
        raw_skip: str,
        raw_bool: str,
        expected_skip: bool,
        expected_bool: bool,
    ) -> None:
        _write_measure_xml(
            tmp_path / "measures" / "MyBoolMeasure",
            """
            <argument>
              <name>flag_arg</name>
              <type>Boolean</type>
              <default_value>true</default_value>
            </argument>
            <argument>
              <name>str_arg</name>
              <type>String</type>
              <default_value>true</default_value>
            </argument>
            """,
        )
        osw = _write_osw(
            tmp_path,
            {"steps": [{"measure_dir_name": "MyBoolMeasure", "arguments": {}}]},
        )
        mappings = parse_osw_arguments(osw)
        _mutate_osw(
            osw,
            {
                "MyBoolMeasure.__SKIP__": raw_skip,
                "MyBoolMeasure.flag_arg": raw_bool,
                "MyBoolMeasure.str_arg": "TRUE",
            },
            mappings,
        )
        mutated = json.loads(osw.read_text())
        step_args = mutated["steps"][0]["arguments"]
        assert step_args["__SKIP__"] is expected_skip
        assert step_args["flag_arg"] is expected_bool
        # Non-Boolean string arguments keep their string literal untouched
        assert step_args["str_arg"] == "TRUE"
