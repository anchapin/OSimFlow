"""Unit tests for conditional/dependent sampling in bin/generate_lhs.py.

Tests cover:
  - Simple conditional sampling (1 level of dependency)
  - Nested conditional sampling (2 levels)
  - Circular dependency detection
  - Missing dependency error
  - Integration with LHS (end-to-end CLI)
  - Unmatched parent value error
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
from generate_lhs import (  # type: ignore[import-untyped]
    _resolve_conditional,
    _resolve_label,
    _validate_dependency_graph,
)


def _run_cli(variables: list[dict[str, Any]], n_samples: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        var_path = Path(tmpdir) / "variables.yml"
        out_path = Path(tmpdir) / "lhs_samples.json"
        var_path.write_text(yaml.dump({"variables": variables}))
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[2] / "bin" / "generate_lhs.py"),
                "--variables_yml",
                str(var_path),
                "--n_samples",
                str(n_samples),
                "--out",
                str(out_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"generate_lhs.py exited {result.returncode}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return json.loads(out_path.read_text())


# ---------------------------------------------------------------------------
# _resolve_label
# ---------------------------------------------------------------------------


class TestResolveLabel:
    def test_string_passthrough(self) -> None:
        assert _resolve_label("vav") == "vav"

    def test_int_to_string(self) -> None:
        assert _resolve_label(42) == "42"

    def test_categorical_dict(self) -> None:
        val = {"label": "wshp", "index": 1, "mapping": {"type": "WSHP"}}
        assert _resolve_label(val) == "wshp"

    def test_dict_without_label(self) -> None:
        assert _resolve_label({"key": "val"}) == "{'key': 'val'}"


# ---------------------------------------------------------------------------
# _validate_dependency_graph
# ---------------------------------------------------------------------------


class TestValidateDependencyGraph:
    def test_independent_variables_pass(self) -> None:
        variables = [
            {"name": "a", "distribution": "uniform", "min": 0, "max": 1},
            {"name": "b", "distribution": "uniform", "min": 0, "max": 1},
        ]
        ordered = _validate_dependency_graph(variables)
        names = [v["name"] for v in ordered]
        assert set(names) == {"a", "b"}

    def test_simple_dependency_order(self) -> None:
        variables = [
            {"name": "parent", "distribution": "categorical", "values": ["x", "y"]},
            {
                "name": "child",
                "distribution": "conditional",
                "depends_on": "parent",
                "conditions": {
                    "x": {"distribution": "uniform", "min": 1, "max": 2},
                    "y": {"distribution": "uniform", "min": 3, "max": 4},
                },
            },
        ]
        ordered = _validate_dependency_graph(variables)
        names = [v["name"] for v in ordered]
        assert names.index("parent") < names.index("child")

    def test_nested_dependency_order(self) -> None:
        variables = [
            {"name": "grandparent", "distribution": "categorical", "values": ["a", "b"]},
            {
                "name": "parent",
                "distribution": "conditional",
                "depends_on": "grandparent",
                "conditions": {
                    "a": {"distribution": "uniform", "min": 1, "max": 2},
                    "b": {"distribution": "uniform", "min": 3, "max": 4},
                },
            },
            {
                "name": "child",
                "distribution": "conditional",
                "depends_on": "parent",
                "conditions": {},
            },
        ]
        ordered = _validate_dependency_graph(variables)
        names = [v["name"] for v in ordered]
        assert names.index("grandparent") < names.index("parent")
        assert names.index("parent") < names.index("child")

    def test_circular_dependency_raises(self) -> None:
        variables = [
            {
                "name": "a",
                "distribution": "conditional",
                "depends_on": "b",
                "conditions": {},
            },
            {
                "name": "b",
                "distribution": "conditional",
                "depends_on": "a",
                "conditions": {},
            },
        ]
        with pytest.raises(ValueError, match="circular dependency"):
            _validate_dependency_graph(variables)

    def test_missing_parent_raises(self) -> None:
        variables = [
            {
                "name": "child",
                "distribution": "conditional",
                "depends_on": "nonexistent",
                "conditions": {},
            },
        ]
        with pytest.raises(ValueError, match="not defined"):
            _validate_dependency_graph(variables)

    def test_conditional_without_depends_on_raises(self) -> None:
        variables = [
            {"name": "child", "distribution": "conditional", "conditions": {}},
        ]
        with pytest.raises(ValueError, match="no 'depends_on'"):
            _validate_dependency_graph(variables)

    def test_three_node_cycle_raises(self) -> None:
        variables = [
            {
                "name": "a",
                "distribution": "conditional",
                "depends_on": "c",
                "conditions": {},
            },
            {
                "name": "b",
                "distribution": "conditional",
                "depends_on": "a",
                "conditions": {},
            },
            {
                "name": "c",
                "distribution": "conditional",
                "depends_on": "b",
                "conditions": {},
            },
        ]
        with pytest.raises(ValueError, match="circular dependency"):
            _validate_dependency_graph(variables)


# ---------------------------------------------------------------------------
# _resolve_conditional
# ---------------------------------------------------------------------------


class TestResolveConditional:
    def test_string_parent_value(self) -> None:
        var = {
            "name": "efficiency",
            "distribution": "conditional",
            "depends_on": "system_type",
            "conditions": {
                "vav": {"distribution": "uniform", "min": 3.0, "max": 5.0},
                "ptx": {"distribution": "uniform", "min": 4.0, "max": 8.0},
            },
        }
        resolved = {"system_type": "vav"}
        val = _resolve_conditional(0.5, var, resolved)
        assert isinstance(val, float)
        assert 3.0 <= val <= 5.0

    def test_categorical_parent_value(self) -> None:
        var = {
            "name": "efficiency",
            "distribution": "conditional",
            "depends_on": "system_type",
            "conditions": {
                "vav": {"distribution": "uniform", "min": 3.0, "max": 5.0},
                "ptx": {"distribution": "uniform", "min": 4.0, "max": 8.0},
            },
        }
        resolved = {"system_type": {"label": "ptx", "index": 1}}
        val = _resolve_conditional(0.5, var, resolved)
        assert isinstance(val, float)
        assert 4.0 <= val <= 8.0

    def test_unmatched_parent_value_raises(self) -> None:
        var = {
            "name": "efficiency",
            "distribution": "conditional",
            "depends_on": "system_type",
            "conditions": {
                "vav": {"distribution": "uniform", "min": 3.0, "max": 5.0},
            },
        }
        resolved = {"system_type": "ptx"}
        with pytest.raises(ValueError, match="no matching condition key"):
            _resolve_conditional(0.5, var, resolved)

    def test_empty_conditions_raises(self) -> None:
        var = {
            "name": "efficiency",
            "distribution": "conditional",
            "depends_on": "system_type",
        }
        resolved = {"system_type": "vav"}
        with pytest.raises(ValueError, match="non-empty 'conditions' dict"):
            _resolve_conditional(0.5, var, resolved)

    def test_sub_dist_discrete(self) -> None:
        var = {
            "name": "depth",
            "distribution": "conditional",
            "depends_on": "system_type",
            "conditions": {
                "wshp": {"distribution": "discrete", "values": [50, 100, 150]},
            },
        }
        resolved = {"system_type": "wshp"}
        val = _resolve_conditional(0.0, var, resolved)
        assert val == 50

    def test_sub_dist_categorical(self) -> None:
        var = {
            "name": "sub_type",
            "distribution": "conditional",
            "depends_on": "main_type",
            "conditions": {
                "a": {
                    "distribution": "categorical",
                    "values": ["a1", "a2"],
                },
            },
        }
        resolved = {"main_type": "a"}
        val = _resolve_conditional(0.5, var, resolved)
        assert isinstance(val, dict)
        assert val["label"] == "a2"


# ---------------------------------------------------------------------------
# End-to-end CLI tests
# ---------------------------------------------------------------------------


class TestConditionalCLI:
    def test_simple_conditional_e2e(self) -> None:
        variables = [
            {
                "name": "hvac_type",
                "distribution": "categorical",
                "values": ["vav", "cv", "ptx"],
            },
            {
                "name": "cooling_efficiency",
                "distribution": "conditional",
                "depends_on": "hvac_type",
                "conditions": {
                    "vav": {"distribution": "uniform", "min": 3.0, "max": 5.0},
                    "cv": {"distribution": "uniform", "min": 2.5, "max": 4.0},
                    "ptx": {"distribution": "uniform", "min": 4.0, "max": 8.0},
                },
            },
        ]
        data = _run_cli(variables, 50)
        assert data["n_samples"] == 50
        for sample in data["samples"]:
            vals = sample["values"]
            hvac = vals["hvac_type"]
            assert isinstance(hvac, dict)
            label = hvac["label"]
            eff = vals["cooling_efficiency"]
            assert isinstance(eff, float), f"Expected float, got {type(eff).__name__}"
            if label == "vav":
                assert 3.0 <= eff <= 5.0, f"VAV efficiency {eff} out of [3.0, 5.0]"
            elif label == "cv":
                assert 2.5 <= eff <= 4.0, f"CV efficiency {eff} out of [2.5, 4.0]"
            elif label == "ptx":
                assert 4.0 <= eff <= 8.0, f"PTX efficiency {eff} out of [4.0, 8.0]"

    def test_nested_conditional_e2e(self) -> None:
        variables = [
            {
                "name": "system_type",
                "distribution": "categorical",
                "values": ["type_a", "type_b"],
            },
            {
                "name": "sub_type",
                "distribution": "conditional",
                "depends_on": "system_type",
                "conditions": {
                    "type_a": {
                        "distribution": "categorical",
                        "values": ["a1", "a2"],
                    },
                    "type_b": {
                        "distribution": "categorical",
                        "values": ["b1", "b2"],
                    },
                },
            },
            {
                "name": "efficiency",
                "distribution": "conditional",
                "depends_on": "sub_type",
                "conditions": {
                    "a1": {"distribution": "uniform", "min": 1.0, "max": 3.0},
                    "a2": {"distribution": "uniform", "min": 3.0, "max": 5.0},
                    "b1": {"distribution": "uniform", "min": 5.0, "max": 7.0},
                    "b2": {"distribution": "uniform", "min": 7.0, "max": 10.0},
                },
            },
        ]
        data = _run_cli(variables, 50)
        for sample in data["samples"]:
            vals = sample["values"]
            assert "system_type" in vals
            assert "sub_type" in vals
            assert "efficiency" in vals
            sub = vals["sub_type"]
            assert isinstance(sub, dict)
            label = sub["label"]
            eff = vals["efficiency"]
            assert isinstance(eff, float)
            if label == "a1":
                assert 1.0 <= eff <= 3.0
            elif label == "a2":
                assert 3.0 <= eff <= 5.0
            elif label == "b1":
                assert 5.0 <= eff <= 7.0
            elif label == "b2":
                assert 7.0 <= eff <= 10.0

    def test_conditional_with_independent_mixed(self) -> None:
        variables = [
            {"name": "wwr", "distribution": "uniform", "min": 0.2, "max": 0.6},
            {
                "name": "hvac_type",
                "distribution": "categorical",
                "values": ["vav", "ptx"],
            },
            {
                "name": "efficiency",
                "distribution": "conditional",
                "depends_on": "hvac_type",
                "conditions": {
                    "vav": {"distribution": "uniform", "min": 3.0, "max": 5.0},
                    "ptx": {"distribution": "uniform", "min": 4.0, "max": 8.0},
                },
            },
        ]
        data = _run_cli(variables, 30)
        assert data["n_samples"] == 30
        for sample in data["samples"]:
            vals = sample["values"]
            assert 0.2 <= vals["wwr"] <= 0.6
            assert isinstance(vals["hvac_type"], dict)
            assert isinstance(vals["efficiency"], float)

    def test_circular_dependency_cli_error(self) -> None:
        variables = [
            {
                "name": "a",
                "distribution": "conditional",
                "depends_on": "b",
                "conditions": {"x": {"distribution": "uniform", "min": 0, "max": 1}},
            },
            {
                "name": "b",
                "distribution": "conditional",
                "depends_on": "a",
                "conditions": {"x": {"distribution": "uniform", "min": 0, "max": 1}},
            },
        ]
        with pytest.raises(RuntimeError, match="circular dependency"):
            _run_cli(variables, 10)

    def test_missing_depends_on_cli_error(self) -> None:
        variables = [
            {
                "name": "orphan",
                "distribution": "conditional",
                "depends_on": "nonexistent",
                "conditions": {"x": {"distribution": "uniform", "min": 0, "max": 1}},
            },
        ]
        with pytest.raises(RuntimeError, match="not defined"):
            _run_cli(variables, 10)

    def test_backward_compatible_no_conditionals(self) -> None:
        variables = [
            {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            {"name": "y", "distribution": "normal", "mean": 0.0, "sigma": 1.0},
            {"name": "z", "distribution": "categorical", "values": ["a", "b"]},
        ]
        data = _run_cli(variables, 20)
        assert data["n_samples"] == 20
        for sample in data["samples"]:
            assert isinstance(sample["values"]["x"], float)
            assert isinstance(sample["values"]["y"], float)
            assert isinstance(sample["values"]["z"], dict)

    def test_per_sample_param_file_flattens_conditional(self) -> None:
        variables = [
            {
                "name": "hvac",
                "distribution": "categorical",
                "values": ["vav", "ptx"],
            },
            {
                "name": "eff",
                "distribution": "conditional",
                "depends_on": "hvac",
                "conditions": {
                    "vav": {"distribution": "uniform", "min": 3.0, "max": 5.0},
                    "ptx": {"distribution": "uniform", "min": 4.0, "max": 8.0},
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            var_path = Path(tmpdir) / "variables.yml"
            out_path = Path(tmpdir) / "lhs_samples.json"
            var_path.write_text(yaml.dump({"variables": variables}))
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[2] / "bin" / "generate_lhs.py"),
                    "--variables_yml",
                    str(var_path),
                    "--n_samples",
                    "5",
                    "--out",
                    str(out_path),
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
            for i in range(1, 6):
                param_file = out_path.parent / f"{i:04d}.params.json"
                assert param_file.exists(), f"Missing param file {param_file}"
                flat = json.loads(param_file.read_text())
                assert flat["hvac"] in ["vav", "ptx"]
                assert isinstance(flat["eff"], float)
