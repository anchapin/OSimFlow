"""Tests for CustomDOEAlgorithm (issue #406).

Covers:
- AlgorithmRegistry.get("custom") returns CustomDOEAlgorithm
- File mode: CSV loading with correct column matching
- File mode: missing columns raises ValueError
- File mode: missing file raises FileNotFoundError
- File mode: fewer rows than requested raises ValueError
- File mode: empty value in CSV row raises ValueError
- Function mode: callable import and invocation
- Function mode: non-callable raises TypeError
- Function mode: wrong return type raises TypeError
- Function mode: function returns dict with wrong values type raises TypeError
- Function mode: function returns list containing non-dict raises TypeError
- Function mode: function spec without ":" raises ValueError
- Function mode: function with extra keys logs warning and filters
- Interface: name, is_iterative, is_converged, observe
- Neither file nor function configured logs error and returns empty
- Non-custom algorithm type returns empty samples
- Empty algorithm config returns empty samples
"""

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from osimflow.algorithms import AlgorithmRegistry
from osimflow.algorithms.custom import CustomDOEAlgorithm


# ======================================================================
# Registry tests
# ======================================================================


class TestCustomRegistry:
    """Registry discovery tests for CustomDOEAlgorithm."""

    def test_get_custom_returns_custom_doe_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("custom")
        assert isinstance(algo, CustomDOEAlgorithm)

    def test_list_available_includes_custom(self) -> None:
        assert "custom" in AlgorithmRegistry.list_available()


# ======================================================================
# Interface tests
# ======================================================================


class TestCustomInterface:
    """Contract tests for CustomDOEAlgorithm."""

    def test_name(self) -> None:
        assert CustomDOEAlgorithm().name() == "custom"

    def test_is_iterative_false(self) -> None:
        assert CustomDOEAlgorithm().is_iterative() is False

    def test_is_converged_empty_history(self) -> None:
        assert CustomDOEAlgorithm().is_converged([]) is True

    def test_is_converged_with_history(self) -> None:
        assert CustomDOEAlgorithm().is_converged([{"samples": []}]) is True

    def test_observe_empty_history(self) -> None:
        assert CustomDOEAlgorithm().observe([]) == []

    def test_observe_returns_last_samples(self) -> None:
        algo = CustomDOEAlgorithm()
        history: list[dict[str, Any]] = [
            {"samples": [{"sample_id": "s0000"}]},
            {"samples": [{"sample_id": "s0001"}]},
        ]
        result = algo.observe(history)
        assert len(result) == 1
        assert result[0]["sample_id"] == "s0001"


# ======================================================================
# File mode tests
# ======================================================================


_VARIABLES_2D: dict[str, Any] = {
    "algorithm": {"type": "custom"},
    "variables": [
        {"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0},
        {"name": "window_shgc", "distribution": "uniform", "min": 0.1, "max": 0.9},
    ],
}


class TestCustomFileMode:
    """Tests for CSV file loading mode."""

    def test_loads_csv_samples(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "samples.csv"
        csv_file.write_text("wall_r,window_shgc\n5.0,0.5\n3.0,0.3\n")

        algo = CustomDOEAlgorithm()
        variables = {**_VARIABLES_2D, "algorithm": {"type": "custom", "samples_file": str(csv_file)}}
        result = algo.generate_samples(variables, n_samples=2, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 2
        assert data["samples"][0]["values"]["wall_r"] == 5.0
        assert data["samples"][0]["values"]["window_shgc"] == 0.5

    def test_creates_outdir(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "samples.csv"
        csv_file.write_text("wall_r,window_shgc\n5.0,0.5\n")

        algo = CustomDOEAlgorithm()
        variables = {**_VARIABLES_2D, "algorithm": {"type": "custom", "samples_file": str(csv_file)}}
        nested = tmp_path / "deep" / "nested"
        algo.generate_samples(variables, n_samples=1, seed=None, outdir=nested)
        assert nested.is_dir()

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        algo = CustomDOEAlgorithm()
        variables = {
            **_VARIABLES_2D,
            "algorithm": {"type": "custom", "samples_file": "/nonexistent/samples.csv"},
        }
        with pytest.raises(FileNotFoundError, match="samples_file not found"):
            algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)

    def test_missing_columns_raises(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "samples.csv"
        csv_file.write_text("wall_r\n5.0\n")

        algo = CustomDOEAlgorithm()
        variables = {**_VARIABLES_2D, "algorithm": {"type": "custom", "samples_file": str(csv_file)}}
        with pytest.raises(ValueError, match="missing.*window_shgc"):
            algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)

    def test_fewer_rows_than_requested_raises(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "samples.csv"
        csv_file.write_text("wall_r,window_shgc\n5.0,0.5\n")

        algo = CustomDOEAlgorithm()
        variables = {**_VARIABLES_2D, "algorithm": {"type": "custom", "samples_file": str(csv_file)}}
        with pytest.raises(ValueError, match="has only 1 row"):
            algo.generate_samples(variables, n_samples=5, seed=None, outdir=tmp_path)

    def test_empty_value_raises(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "samples.csv"
        csv_file.write_text("wall_r,window_shgc\n5.0,\n")

        algo = CustomDOEAlgorithm()
        variables = {**_VARIABLES_2D, "algorithm": {"type": "custom", "samples_file": str(csv_file)}}
        with pytest.raises(ValueError, match="empty value"):
            algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)

    def test_string_values_accepted(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "samples.csv"
        csv_file.write_text("wall_r,window_shgc\n5.0,high\n")

        algo = CustomDOEAlgorithm()
        variables = {**_VARIABLES_2D, "algorithm": {"type": "custom", "samples_file": str(csv_file)}}
        result = algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"][0]["values"]["window_shgc"] == "high"

    def test_trims_column_whitespace(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "samples.csv"
        csv_file.write_text(" wall_r , window_shgc \n5.0,0.5\n")

        algo = CustomDOEAlgorithm()
        variables = {**_VARIABLES_2D, "algorithm": {"type": "custom", "samples_file": str(csv_file)}}
        result = algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert "wall_r" in data["samples"][0]["values"]


# ======================================================================
# Function mode tests
# ======================================================================


class TestCustomFunctionMode:
    """Tests for callable function mode."""

    def test_calls_function_and_loads_result(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        def fake_func(**kwargs: Any) -> list[dict[str, Any]]:
            return [
                {"sample_id": "0001", "values": {"wall_r": 7.0, "window_shgc": 0.8}},
                {"sample_id": "0002", "values": {"wall_r": 3.0, "window_shgc": 0.2}},
            ]

        import types
        fake_module = types.ModuleType("fake")
        fake_module.custom_func = fake_func
        monkeypatch.setitem(sys.modules, "fake", fake_module)

        algo = CustomDOEAlgorithm()
        variables = {
            **_VARIABLES_2D,
            "algorithm": {"type": "custom", "samples_function": "fake:custom_func"},
        }
        result = algo.generate_samples(variables, n_samples=2, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 2
        assert data["samples"][0]["values"]["wall_r"] == 7.0

    def test_function_spec_without_colon_raises(self, tmp_path: Path) -> None:
        algo = CustomDOEAlgorithm()
        variables = {
            **_VARIABLES_2D,
            "algorithm": {"type": "custom", "samples_function": "nofunctionformat"},
        }
        with pytest.raises(ValueError, match="module:function"):
            algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)

    def test_non_callable_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        fake_module = type("fake_module", (), {"not_callable": "a string"})()
        monkeypatch.setitem(sys.modules, "fake", fake_module)
        algo = CustomDOEAlgorithm()
        variables = {
            **_VARIABLES_2D,
            "algorithm": {"type": "custom", "samples_function": "fake:not_callable"},
        }
        with pytest.raises(TypeError, match="not callable"):
            algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)

    def test_wrong_return_type_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        fake_module = type("fake_module", (), {"bad_func": lambda *a: "not a list"})()
        monkeypatch.setitem(sys.modules, "fake", fake_module)
        algo = CustomDOEAlgorithm()
        variables = {
            **_VARIABLES_2D,
            "algorithm": {"type": "custom", "samples_function": "fake:bad_func"},
        }
        with pytest.raises(TypeError, match="must return list\\[dict\\]"):
            algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)

    def test_list_containing_non_dict_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        fake_module = type("fake_module", (), {"bad_func": lambda *a: ["not a dict"]})()
        monkeypatch.setitem(sys.modules, "fake", fake_module)
        algo = CustomDOEAlgorithm()
        variables = {
            **_VARIABLES_2D,
            "algorithm": {"type": "custom", "samples_function": "fake:bad_func"},
        }
        with pytest.raises(TypeError, match="expected dict"):
            algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)

    def test_non_dict_values_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        fake_module = type(
            "fake_module", (), {"bad_func": lambda *a: [{"sample_id": "0001", "values": "not a dict"}]}
        )()
        monkeypatch.setitem(sys.modules, "fake", fake_module)
        algo = CustomDOEAlgorithm()
        variables = {
            **_VARIABLES_2D,
            "algorithm": {"type": "custom", "samples_function": "fake:bad_func"},
        }
        with pytest.raises(TypeError, match="non-dict 'values'"):
            algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)

    def test_extra_values_filtered_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        fake_module = type(
            "fake_module", (),
            {
                "bad_func": lambda *a: [
                    {"sample_id": "0001", "values": {"wall_r": 5.0, "window_shgc": 0.5, "extra_var": 99.0}}
                ]
            },
        )()
        monkeypatch.setitem(sys.modules, "fake", fake_module)
        algo = CustomDOEAlgorithm()
        variables = {
            **_VARIABLES_2D,
            "algorithm": {"type": "custom", "samples_function": "fake:bad_func"},
        }
        result = algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert "extra_var" not in data["samples"][0]["values"]
        assert "unknown variable" in caplog.text.lower()

    def test_falls_back_to_positional_args(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        called = {}

        def positional_func(**kwargs: Any) -> list[dict[str, Any]]:
            called["vars"] = kwargs.get("variables")
            called["n"] = kwargs.get("n_samples")
            return [{"sample_id": "0001", "values": {"wall_r": 5.0, "window_shgc": 0.5}}]

        import types
        fake_module = types.ModuleType("fake")
        fake_module.pos_func = positional_func
        monkeypatch.setitem(sys.modules, "fake", fake_module)

        algo = CustomDOEAlgorithm()
        variables = {
            **_VARIABLES_2D,
            "algorithm": {"type": "custom", "samples_function": "fake:pos_func"},
        }
        result = algo.generate_samples(variables, n_samples=1, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 1


# ======================================================================
# Edge cases
# ======================================================================


class TestCustomEdgeCases:
    """Edge case tests for CustomDOEAlgorithm."""

    def test_no_file_or_function_logs_error(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        algo = CustomDOEAlgorithm()
        variables = {**_VARIABLES_2D, "algorithm": {"type": "custom"}}
        result = algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []
        assert "requires either" in caplog.text

    def test_non_custom_type_returns_empty(self, tmp_path: Path) -> None:
        algo = CustomDOEAlgorithm()
        variables = {**_VARIABLES_2D, "algorithm": {"type": "lhs"}}
        result = algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_empty_algorithm_config_returns_empty(self, tmp_path: Path) -> None:
        algo = CustomDOEAlgorithm()
        variables = {**_VARIABLES_2D, "algorithm": {}}
        result = algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_non_dict_algorithm_config_returns_empty(self, tmp_path: Path) -> None:
        algo = CustomDOEAlgorithm()
        variables = {**_VARIABLES_2D, "algorithm": "not a dict"}
        result = algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_empty_variables_returns_empty(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "samples.csv"
        csv_file.write_text("wall_r,window_shgc\n5.0,0.5\n")
        algo = CustomDOEAlgorithm()
        variables = {"algorithm": {"type": "custom", "samples_file": str(csv_file)}, "variables": []}
        result = algo.generate_samples(variables, n_samples=5, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []
