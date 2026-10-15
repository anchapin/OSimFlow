"""Parity: apply_params.apply_parameters vs work.default_apply_parameters (issue #2112)."""

import json
import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from osimflow.apply_params import (
    UnmappedParameterError,
    apply_parameters,
    load_variable_defs,
)
from osimflow.work import _apply_parameters_with_osm_deferral, default_apply_parameters

VARIABLE_DEFS: list[dict[str, Any]] = [
    {"name": "wall_r", "measure_argument": "RubyMeasure.r_value"},
    {"name": "window_type", "type": "categorical"},
    {
        "name": "climate",
        "target": "epw_file",
        "mapping": {"cold": "files/cold.epw", "hot": "files/hot.epw"},
    },
]


def _template(root: Path) -> Path:
    pkg = root / "template"
    (pkg / "measures" / "RubyMeasure").mkdir(parents=True)
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "weather_file": "",
                "measure_paths": ["measures"],
                "seed_file": "model.osm",
                "steps": [
                    {
                        "measure_dir_name": "RubyMeasure",
                        "arguments": {"window_type": "single", "r_value": 1.0},
                    },
                    {"measure_dir_name": "Other", "arguments": {"r_value": 9.0}},
                ],
            }
        )
    )
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"lighting_power_density": 8.0}}))
    return pkg


def _run_both(
    tmp_path: Path, params: dict[str, Any], *, variable_defs: list[dict[str, Any]] | None = None
) -> tuple[Path, Path]:
    template = _template(tmp_path)
    standalone = tmp_path / "standalone"
    apply_parameters(template, dict(params), "0001", standalone, variable_defs=variable_defs)

    work_out = tmp_path / "work"
    shutil.copytree(template, work_out)
    work_params = dict(params)
    if variable_defs:
        from osimflow.apply_params import (
            build_measure_argument_alias_map,
            resolve_measure_argument_keys,
        )

        for var in variable_defs:
            if var.get("target") == "epw_file" and var["name"] in work_params:
                work_params["__epw_file__"] = var["mapping"][work_params.pop(var["name"])]
        work_params = resolve_measure_argument_keys(
            work_params, build_measure_argument_alias_map(variable_defs)
        )
    with patch.dict(sys.modules, {"openstudio": None}):
        _apply_parameters_with_osm_deferral(template, work_params, "0001", work_out)
    return standalone, work_out


def _assert_same_package(a: Path, b: Path) -> None:
    assert json.loads((a / "workflow.osw").read_text()) == json.loads(
        (b / "workflow.osw").read_text()
    )
    assert json.loads((a / "model.osm").read_text()) == json.loads((b / "model.osm").read_text())


def test_categorical_label_unpacked(tmp_path: Path) -> None:
    standalone, work_out = _run_both(
        tmp_path, {"window_type": {"label": "double", "index": 1}, "RubyMeasure.r_value": 3.5}
    )
    steps = json.loads((standalone / "workflow.osw").read_text())["steps"]
    assert steps[0]["arguments"] == {"window_type": "double", "r_value": 3.5}
    _assert_same_package(standalone, work_out)


def test_epw_file_reserved_key(tmp_path: Path) -> None:
    standalone, work_out = _run_both(tmp_path, {"__epw_file__": "files/a.epw"})
    assert json.loads((standalone / "workflow.osw").read_text())["weather_file"] == "files/a.epw"
    _assert_same_package(standalone, work_out)


def test_variables_yml_alias_and_epw_target(tmp_path: Path) -> None:
    standalone, work_out = _run_both(
        tmp_path,
        {"wall_r": 4.0, "climate": "hot", "window_type": {"label": "triple", "index": 2}},
        variable_defs=VARIABLE_DEFS,
    )
    osw = json.loads((standalone / "workflow.osw").read_text())
    assert osw["weather_file"] == "files/hot.epw"
    assert osw["steps"][0]["arguments"] == {"window_type": "triple", "r_value": 4.0}
    _assert_same_package(standalone, work_out)


def test_osm_json_stub_attributes_match(tmp_path: Path) -> None:
    standalone, work_out = _run_both(
        tmp_path, {"lighting_power_density": 11.5, "RubyMeasure.r_value": 2.0}
    )
    assert json.loads((standalone / "model.osm").read_text())["attributes"] == {
        "lighting_power_density": 11.5
    }
    _assert_same_package(standalone, work_out)


def test_no_osm_params_leaves_osm_untouched(tmp_path: Path) -> None:
    template = _template(tmp_path)
    before = (template / "model.osm").read_text()
    out = tmp_path / "out"
    apply_parameters(template, {"RubyMeasure.r_value": 2.0}, "0001", out)
    assert (out / "model.osm").read_text() == before


def test_default_apply_parameters_matches_on_oswonly_categorical(tmp_path: Path) -> None:
    template = _template(tmp_path)
    out = tmp_path / "work"
    shutil.copytree(template, out)
    params = {"window_type": {"label": "double", "index": 1}}
    default_apply_parameters(out, params, "0001", out)
    standalone = tmp_path / "standalone"
    apply_parameters(template, params, "0001", standalone)
    assert (out / "workflow.osw").read_text() == (standalone / "workflow.osw").read_text()


def test_caller_parameters_not_mutated(tmp_path: Path) -> None:
    params: dict[str, Any] = {"__epw_file__": "files/a.epw", "RubyMeasure.r_value": 2.0}
    apply_parameters(_template(tmp_path), params, "0001", tmp_path / "out")
    assert "__epw_file__" in params


def test_preflight_still_rejects_unmapped(tmp_path: Path) -> None:
    with pytest.raises(UnmappedParameterError):
        apply_parameters(_template(tmp_path), {"nope": 1.0}, "0001", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_load_variable_defs(tmp_path: Path) -> None:
    yml = tmp_path / "variables.yml"
    yml.write_text("variables:\n  - name: wall_r\n    measure_argument: RubyMeasure.r_value\n")
    assert load_variable_defs(yml) == [
        {"name": "wall_r", "measure_argument": "RubyMeasure.r_value"}
    ]
    yml.write_text("variables: 3\n")
    with pytest.raises(ValueError):
        load_variable_defs(yml)
    yml.write_text("a: [\n")
    with pytest.raises(ValueError):
        load_variable_defs(yml)


def test_cli_script_input_variables(tmp_path: Path) -> None:
    from osimflow._work_scripts import apply_params_to_model

    yml = tmp_path / "variables.yml"
    yml.write_text(
        "variables:\n  - name: wall_r\n    measure_argument: RubyMeasure.r_value\n"
        "  - name: climate\n    target: epw_file\n    mapping: {hot: files/hot.epw}\n"
    )
    pset = tmp_path / "p.json"
    pset.write_text(json.dumps({"wall_r": 5.0, "climate": "hot"}))
    out = tmp_path / "out"
    argv = [
        "apply_params_to_model.py",
        "--template",
        str(_template(tmp_path)),
        "--parameter_set",
        str(pset),
        "--sample_id",
        "0001",
        "--out",
        str(out),
        "--input_variables",
        str(yml),
    ]
    with patch.object(sys, "argv", argv):
        assert apply_params_to_model.main() == 0
    osw = json.loads((out / "workflow.osw").read_text())
    assert osw["weather_file"] == "files/hot.epw"
    assert osw["steps"][0]["arguments"]["r_value"] == 5.0


@pytest.mark.parametrize(
    "content",
    [None, "<OSM/>", "{not json", "[1, 2]", '{"attributes": 3}'],
)
def test_json_stub_helper_rejects_non_stub(tmp_path: Path, content: str | None) -> None:
    from osimflow._work_osm_mutations import _apply_json_stub_osm_attributes

    osm = tmp_path / "model.osm"
    if content is not None:
        osm.write_text(content)
    assert _apply_json_stub_osm_attributes(osm, {"x": 1.0}) is False


def _collision_template(root: Path) -> Path:
    pkg = _template(root)
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"r_value": 5.0, "lpd": 8.0}}))
    return pkg


def test_collision_key_osw_only_matches_work(tmp_path: Path) -> None:
    template = _collision_template(tmp_path)
    standalone = tmp_path / "standalone"
    apply_parameters(template, {"RubyMeasure.r_value": 2.0, "lpd": 9.0}, "1", standalone)
    work_out = tmp_path / "work"
    shutil.copytree(template, work_out)
    with patch.dict(sys.modules, {"openstudio": None}):
        _apply_parameters_with_osm_deferral(
            template, {"RubyMeasure.r_value": 2.0, "lpd": 9.0}, "1", work_out
        )
    _assert_same_package(standalone, work_out)
    assert json.loads((standalone / "model.osm").read_text())["attributes"] == {
        "r_value": 5.0,
        "lpd": 9.0,
    }


def test_default_apply_parameters_collision_osw_only(tmp_path: Path) -> None:
    template = _collision_template(tmp_path)
    standalone = tmp_path / "standalone"
    apply_parameters(template, {"RubyMeasure.r_value": 2.0}, "1", standalone)
    work_out = tmp_path / "work"
    shutil.copytree(template, work_out)
    default_apply_parameters(template, {"RubyMeasure.r_value": 2.0}, "1", work_out)
    _assert_same_package(standalone, work_out)


def test_stub_mode_difference_documented(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
    template = _template(tmp_path)
    standalone = tmp_path / "standalone"
    apply_parameters(template, {"lighting_power_density": 11.5}, "1", standalone)
    work_out = tmp_path / "work"
    shutil.copytree(template, work_out)
    default_apply_parameters(template, {"lighting_power_density": 11.5}, "1", work_out)
    assert json.loads((standalone / "model.osm").read_text())["attributes"] == {
        "lighting_power_density": 11.5
    }
    assert json.loads((work_out / "model.osm").read_text())["attributes"] == {
        "lighting_power_density": 8.0
    }
