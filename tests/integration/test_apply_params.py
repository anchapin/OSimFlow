"""Tests for the apply_params_to_model logic.

These tests cover the Pre-flight Parameter Applicability Validation
(PRD §1.4) and the BYOS contract for `bin/apply_params_to_model.py`.

The core logic lives in `osimflow.apply_params` so it can be exercised
without the OpenStudio Python bindings installed. The CLI entry point
(`bin/apply_params_to_model.py`) is responsible for the `import openstudio`
try/except guard per AGENTS.md §6.

Test strategy
-------------
The .osw file format is JSON. The .osm file is XML in production, but for
unit tests we use the convention that an .osm file starting with ``{`` is
treated as a JSON representation. This lets us exercise the parameter
mapping and pre-flight logic deterministically without the OpenStudio
binding stack.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from osimflow.apply_params import (
    MappedParameter,
    UnmappedParameterError,
    apply_parameters,
    detect_template_type,
    parse_osm_attributes,
    parse_osw_arguments,
    preflight_check,
    resolve_template_file,
)


# ---------------------------------------------------------------------------
# Fixtures: tiny .osm (JSON representation) and .osw templates
# ---------------------------------------------------------------------------
@pytest.fixture
def osm_template(tmp_path: Path) -> Path:
    """A minimal .osm template as a JSON document.

    The real .osm format is XML, but the test convention is that any .osm
    file starting with ``{`` is a JSON attribute map. This lets the same
    pre-flight + apply logic run without the OpenStudio bindings.
    """
    p = tmp_path / "model.osm"
    p.write_text(
        json.dumps(
            {
                "attributes": {
                    "window_u_value": 0.3,
                    "hvac_setpoint": 21.0,
                    "lighting_power_density": 8.0,
                }
            }
        )
    )
    return p


@pytest.fixture
def osw_template(tmp_path: Path) -> Path:
    """A minimal .osw workflow with one measure and named arguments."""
    p = tmp_path / "workflow.osw"
    p.write_text(
        json.dumps(
            {
                "name": "test workflow",
                "steps": [
                    {
                        "measure_dir_name": "SetWindowToWallRatioByStory",
                        "arguments": {
                            "wwr": 0.4,
                            "sill_height": 0.8,
                        },
                    },
                ],
            }
        )
    )
    return p


# ---------------------------------------------------------------------------
# Pre-flight: the core acceptance criterion (PRD §1.4)
# ---------------------------------------------------------------------------
def test_preflight_check_passes_when_all_parameters_mapped(
    osm_template: Path,
) -> None:
    """All LHS variables that exist in the template attributes must pass."""
    mappings = parse_osm_attributes(osm_template)
    params = {"window_u_value": 0.5, "hvac_setpoint": 22.0}
    # No exception expected.
    preflight_check(params, mappings)


def test_preflight_check_fails_with_clear_error_on_unmapped_variable(
    osm_template: Path,
) -> None:
    """An LHS variable that does NOT map to a template attribute must fail fast.

    This is the core acceptance criterion: list the unmapped names so
    users can fix the variable name in variables.yml.
    """
    mappings = parse_osm_attributes(osm_template)
    params = {
        "window_u_value": 0.5,  # mapped
        "hvac_setpoint": 22.0,  # mapped
        "wall_insultion_r": 3.5,  # typo + unmapped
        "roof_thickness": 0.2,  # unmapped
    }
    with pytest.raises(UnmappedParameterError) as excinfo:
        preflight_check(params, mappings)
    msg = str(excinfo.value)
    assert "wall_insultion_r" in msg, f"unmapped name missing from error: {msg}"
    assert "roof_thickness" in msg, f"unmapped name missing from error: {msg}"
    # Mapped names should NOT be in the error.
    assert "window_u_value" not in msg
    assert "hvac_setpoint" not in msg


def test_preflight_check_error_lists_all_unmapped_at_once(
    osm_template: Path,
) -> None:
    """The error must list ALL unmapped names in one pass — not just the first."""
    mappings = parse_osm_attributes(osm_template)
    params = {"a_unmapped": 1, "b_unmapped": 2, "c_mapped": 3}
    # Patch the template to have only c_mapped.
    mappings["c_mapped"] = MappedParameter(name="c_mapped", kind="attribute")
    with pytest.raises(UnmappedParameterError) as excinfo:
        preflight_check(params, mappings)
    msg = str(excinfo.value)
    assert "a_unmapped" in msg
    assert "b_unmapped" in msg


# ---------------------------------------------------------------------------
# Template type detection
# ---------------------------------------------------------------------------
def test_detect_template_type_osm(osm_template: Path) -> None:
    assert detect_template_type(osm_template) == "osm"


def test_detect_template_type_osw(osw_template: Path) -> None:
    assert detect_template_type(osw_template) == "osw"


def test_detect_template_type_rejects_unknown(tmp_path: Path) -> None:
    p = tmp_path / "stuff.txt"
    p.write_text("hi")
    with pytest.raises(ValueError, match="Unsupported template type"):
        detect_template_type(p)


# ---------------------------------------------------------------------------
# .osm parsing
# ---------------------------------------------------------------------------
def test_parse_osm_attributes_returns_mapped_parameters(osm_template: Path) -> None:
    mappings = parse_osm_attributes(osm_template)
    assert "window_u_value" in mappings
    assert "hvac_setpoint" in mappings
    assert mappings["window_u_value"].kind == "attribute"
    assert mappings["window_u_value"].default == 0.3


# ---------------------------------------------------------------------------
# .osw parsing
# ---------------------------------------------------------------------------
def test_parse_osw_arguments_returns_mapped_parameters(osw_template: Path) -> None:
    mappings = parse_osw_arguments(osw_template)
    assert "wwr" in mappings
    assert "sill_height" in mappings
    assert mappings["wwr"].kind == "measure_argument"
    assert mappings["wwr"].default == 0.4


# ---------------------------------------------------------------------------
# apply_parameters: end-to-end on a per-sample directory
# ---------------------------------------------------------------------------
def test_apply_parameters_osm_writes_per_sample_dir(osm_template: Path, tmp_path: Path) -> None:
    """A valid .osm template + mapped parameters produces a per-sample copy
    with the attribute mutated in place.
    """
    sample_out = tmp_path / "out" / "0001"
    result = apply_parameters(
        template=osm_template,
        parameters={"window_u_value": 0.7, "hvac_setpoint": 23.0},
        sample_id="0001",
        out=sample_out,
    )
    assert result == sample_out
    assert sample_out.is_dir()
    # The .osm was modified in place: it must be inside the per-sample dir.
    out_osm = sample_out / "model.osm"
    assert out_osm.is_file()
    data = json.loads(out_osm.read_text())
    assert data["attributes"]["window_u_value"] == 0.7
    assert data["attributes"]["hvac_setpoint"] == 23.0
    # Untouched attribute keeps its default.
    assert data["attributes"]["lighting_power_density"] == 8.0


def test_apply_parameters_osw_updates_measure_arguments(osw_template: Path, tmp_path: Path) -> None:
    """A valid .osw template + mapped parameters updates the workflow args."""
    sample_out = tmp_path / "out" / "0002"
    apply_parameters(
        template=osw_template,
        parameters={"wwr": 0.6, "sill_height": 1.0},
        sample_id="0002",
        out=sample_out,
    )
    out_osw = sample_out / "workflow.osw"
    assert out_osw.is_file()
    data = json.loads(out_osw.read_text())
    assert data["steps"][0]["arguments"]["wwr"] == 0.6
    assert data["steps"][0]["arguments"]["sill_height"] == 1.0


def test_apply_parameters_fails_fast_on_unmapped_variable(
    osm_template: Path, tmp_path: Path
) -> None:
    """The pre-flight check fires BEFORE any output is written."""
    sample_out = tmp_path / "out" / "0003"
    with pytest.raises(UnmappedParameterError):
        apply_parameters(
            template=osm_template,
            parameters={
                "window_u_value": 0.5,
                "totally_made_up_param": 42.0,  # does not exist
            },
            sample_id="0003",
            out=sample_out,
        )
    # The per-sample directory should NOT have been created, since the
    # pre-flight check failed before any writes.
    assert not sample_out.exists()


def test_apply_parameters_does_not_mutate_input_template(
    osm_template: Path, tmp_path: Path
) -> None:
    """The input template must be left untouched (we copy then mutate)."""
    original = osm_template.read_text()
    sample_out = tmp_path / "out" / "0004"
    apply_parameters(
        template=osm_template,
        parameters={"window_u_value": 99.0, "hvac_setpoint": 99.0},
        sample_id="0004",
        out=sample_out,
    )
    assert osm_template.read_text() == original


# ---------------------------------------------------------------------------
# BYOS contract: custom_apply_script override
# ---------------------------------------------------------------------------
def test_apply_parameters_with_custom_script_uses_user_function(
    osm_template: Path, tmp_path: Path
) -> None:
    """When a custom script is provided, the framework calls it via the
    documented interface. The user function receives a `ctx` dict and
    returns a result dict (per user_scripts/README.md)."""
    user_script = tmp_path / "user_apply.py"
    user_script.write_text(
        textwrap.dedent(
            """\
            def apply(ctx):
                # Custom user logic — write a sentinel file.
                out = ctx["out_dir"]
                out.mkdir(parents=True, exist_ok=True)
                (out / "user_was_here.txt").write_text(
                    f"sample_id={ctx['sample_id']} w={ctx['parameters']['window_u_value']}"
                )
                return {
                    "osm_path": None,
                    "osw_path": None,
                    "extra": [out / "user_was_here.txt"],
                    "warnings": [],
                }
            """
        )
    )
    sample_out = tmp_path / "out" / "0005"
    apply_parameters(
        template=osm_template,
        parameters={"window_u_value": 0.42},
        sample_id="0005",
        out=sample_out,
        custom_apply_script=user_script,
    )
    sentinel = sample_out / "user_was_here.txt"
    assert sentinel.is_file()
    assert "sample_id=0005" in sentinel.read_text()
    assert "w=0.42" in sentinel.read_text()


def test_apply_parameters_custom_script_missing_apply_function_raises(
    osm_template: Path, tmp_path: Path
) -> None:
    """A user script without an `apply(ctx)` function must fail clearly,
    not silently fall through to the default logic."""
    bad = tmp_path / "bad.py"
    bad.write_text("def not_apply():\n    pass\n")
    with pytest.raises(ValueError, match="apply"):
        apply_parameters(
            template=osm_template,
            parameters={"window_u_value": 0.5},
            sample_id="0006",
            out=tmp_path / "out" / "0006",
            custom_apply_script=bad,
        )


# ---------------------------------------------------------------------------
# CLI surface: bin/apply_params_to_model.py
# ---------------------------------------------------------------------------
def test_cli_runs_end_to_end_with_valid_params(osm_template: Path, tmp_path: Path) -> None:
    """The CLI entry point must work with valid params, even on a host
    WITHOUT the OpenStudio bindings (the test-mode path)."""
    import subprocess
    import sys

    sample_out = tmp_path / "out" / "0007"
    param_file = tmp_path / "params.json"
    param_file.write_text(json.dumps({"window_u_value": 0.5, "hvac_setpoint": 22.0}))
    result = subprocess.run(
        [
            sys.executable,
            "bin/apply_params_to_model.py",
            "--template",
            str(osm_template),
            "--parameter_set",
            str(param_file),
            "--sample_id",
            "0007",
            "--out",
            str(sample_out),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    out_osm = sample_out / "model.osm"
    assert out_osm.is_file()


def test_cli_preflight_failure_returns_nonzero_exit_code(
    osm_template: Path, tmp_path: Path
) -> None:
    """When the pre-flight check fails, the CLI must exit non-zero
    so the work layer's subprocess.run can detect the failure."""
    import subprocess
    import sys

    sample_out = tmp_path / "out" / "0008"
    param_file = tmp_path / "params.json"
    param_file.write_text(json.dumps({"window_u_value": 0.5, "not_a_real_param": 1.0}))
    result = subprocess.run(
        [
            sys.executable,
            "bin/apply_params_to_model.py",
            "--template",
            str(osm_template),
            "--parameter_set",
            str(param_file),
            "--sample_id",
            "0008",
            "--out",
            str(sample_out),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not_a_real_param" in result.stderr
    # The per-sample dir should NOT exist.
    assert not sample_out.exists()


# ---------------------------------------------------------------------------
# Coverage for additional branches: parse errors, directory templates,
# nested directory copy, custom-script copy path.
# ---------------------------------------------------------------------------
def test_parse_osw_arguments_rejects_invalid_json(tmp_path: Path) -> None:
    """An .osw file that is not valid JSON must raise a clear ValueError."""
    p = tmp_path / "broken.osw"
    p.write_text("this is not json at all")
    with pytest.raises(ValueError, match="Invalid .osw JSON"):
        parse_osw_arguments(p)


def test_parse_osm_attributes_rejects_invalid_json_repr(tmp_path: Path) -> None:
    """An .osm file with ``{`` prefix but invalid JSON must raise clearly."""
    p = tmp_path / "broken.osm"
    p.write_text("{ this is not valid json")
    with pytest.raises(ValueError, match="Failed to parse"):
        parse_osm_attributes(p)


def test_parse_osm_attributes_binary_without_bindings(tmp_path: Path) -> None:
    """An .osm file with XML/binary content (and no bindings) must fail clearly.

    Without the OpenStudio Python bindings installed, we cannot parse a
    real .osm. The error message must be actionable.
    """
    p = tmp_path / "binary.osm"
    p.write_text("<OSMModel>...</OSMModel>")
    with pytest.raises(RuntimeError, match="OpenStudio Python bindings"):
        parse_osm_attributes(p)


def test_resolve_template_file_rejects_empty_directory(tmp_path: Path) -> None:
    """A directory with neither workflow.osw nor model.osm must raise."""
    empty = tmp_path / "empty_pkg"
    empty.mkdir()
    with pytest.raises(ValueError, match="contains neither"):
        resolve_template_file(empty)


def test_apply_parameters_with_directory_template_merges_mappings(
    tmp_path: Path,
) -> None:
    """When the template is a directory with BOTH .osw and .osm, the
    pre-flight check must consider mappings from BOTH files."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "name": "demo",
                "steps": [{"measure_dir_name": "M1", "arguments": {"wwr": 0.4}}],
            }
        )
    )
    (pkg / "model.osm").write_text(
        json.dumps({"attributes": {"window_u_value": 0.3, "hvac_setpoint": 21.0}})
    )
    out = tmp_path / "out" / "000A"
    apply_parameters(
        template=pkg,
        parameters={"wwr": 0.7, "window_u_value": 0.5},
        sample_id="000A",
        out=out,
    )
    # Both files were copied into the per-sample dir.
    assert (out / "workflow.osw").is_file()
    assert (out / "model.osm").is_file()
    # The .osw was mutated (workflow takes precedence for write-back).
    osw_data = json.loads((out / "workflow.osw").read_text())
    assert osw_data["steps"][0]["arguments"]["wwr"] == 0.7
    # The .osm is also mutated: every parameter that maps to an .osm
    # attribute is applied. The hvac_setpoint attribute is left alone
    # (no LHS var for it).
    osm_data = json.loads((out / "model.osm").read_text())
    assert osm_data["attributes"]["window_u_value"] == 0.5
    assert osm_data["attributes"]["hvac_setpoint"] == 21.0


def test_apply_parameters_directory_template_with_subdirectory(
    tmp_path: Path,
) -> None:
    """A template directory with a nested subdirectory (e.g. measures/)
    must be copied recursively into the per-sample output."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"x": 1.0}}))
    measures = pkg / "measures"
    measures.mkdir()
    (measures / "my_measure.py").write_text("# measure code")
    out = tmp_path / "out" / "000B"
    apply_parameters(
        template=pkg,
        parameters={"x": 2.0},
        sample_id="000B",
        out=out,
    )
    assert (out / "model.osm").is_file()
    assert (out / "measures").is_dir()
    assert (out / "measures" / "my_measure.py").is_file()


def test_apply_parameters_with_directory_template_unmapped_param_fails(
    tmp_path: Path,
) -> None:
    """When the template is a directory and an LHS var is in NEITHER file,
    the pre-flight check fails with the canonical error."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"a": 1.0}}))
    (pkg / "workflow.osw").write_text(json.dumps({"steps": []}))
    out = tmp_path / "out" / "000C"
    with pytest.raises(UnmappedParameterError, match="not_in_either"):
        apply_parameters(
            template=pkg,
            parameters={"a": 1.0, "not_in_either": 2.0},
            sample_id="000C",
            out=out,
        )
    assert not out.exists()


def test_custom_script_dir_template_passes_template_dir_in_ctx(
    tmp_path: Path,
) -> None:
    """When the template is a directory AND a custom script is provided,
    the ctx dict's template_dir must point to the original directory."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"x": 1.0}}))
    user_script = tmp_path / "user.py"
    user_script.write_text(
        textwrap.dedent(
            """\
            def apply(ctx):
                out = ctx["out_dir"]
                out.mkdir(parents=True, exist_ok=True)
                (out / "ctx_snapshot.txt").write_text(
                    f"template_dir={ctx['template_dir']} sample={ctx['sample_id']}"
                )
            """
        )
    )
    out = tmp_path / "out" / "000D"
    apply_parameters(
        template=pkg,
        parameters={"x": 1.5},
        sample_id="000D",
        out=out,
        custom_apply_script=user_script,
    )
    assert (out / "ctx_snapshot.txt").is_file()
    snapshot = (out / "ctx_snapshot.txt").read_text()
    assert "template_dir=" in snapshot
    assert "sample=000D" in snapshot
