"""Tests for UncertaintyQuantification algorithm (issue #530).

Covers:
- AlgorithmRegistry.get("uq") returns UncertaintyQuantification
- UncertaintyQuantification interface contracts
- generate_samples produces correct number of samples
- compute_uq_indices output structure
- probability of failure computation
- confidence interval computation
- distribution summaries
- failure threshold parsing
"""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from osimflow.algorithms import AlgorithmRegistry, BaseAlgorithm
from osimflow.algorithms.uq import (
    UncertaintyQuantification,
    _compute_confidence_interval,
    _compute_distribution_summary,
    _compute_pof,
    _parse_failure_threshold,
)

_VARIABLES_2D: dict[str, Any] = {
    "variables": [
        {"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0},
        {"name": "window_shgc", "distribution": "uniform", "min": 0.1, "max": 0.9},
    ]
}


class TestUQRegistry:
    """Registry discovery tests for UncertaintyQuantification."""

    def test_get_uq_returns_uq_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("uq")
        assert isinstance(algo, UncertaintyQuantification)

    def test_get_uq_returns_base_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("uq")
        assert isinstance(algo, BaseAlgorithm)

    def test_list_available_includes_uq(self) -> None:
        assert "uq" in AlgorithmRegistry.list_available()


class TestUQInterface:
    """Contract tests for UncertaintyQuantification."""

    def test_name(self) -> None:
        assert UncertaintyQuantification().name() == "uq"

    def test_is_iterative_false(self) -> None:
        assert UncertaintyQuantification().is_iterative() is False

    def test_is_converged_empty_history(self) -> None:
        assert UncertaintyQuantification().is_converged([]) is True

    def test_is_converged_with_history(self) -> None:
        assert UncertaintyQuantification().is_converged([{"samples": []}]) is True

    def test_observe_empty_history(self) -> None:
        assert UncertaintyQuantification().observe([]) == []

    def test_observe_returns_last_samples(self) -> None:
        algo = UncertaintyQuantification()
        history: list[dict[str, Any]] = [
            {"samples": [{"sample_id": "s0000"}]},
            {"samples": [{"sample_id": "s0001"}]},
        ]
        result = algo.observe(history)
        assert len(result) == 1
        assert result[0]["sample_id"] == "s0001"


class TestUQGenerateSamples:
    """Generate-samples tests for UncertaintyQuantification."""

    def test_creates_samples_json(self, tmp_path: Path) -> None:
        algo = UncertaintyQuantification()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=8, seed=42, outdir=tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        assert len(data["samples"]) == 8

    def test_sample_structure(self, tmp_path: Path) -> None:
        algo = UncertaintyQuantification()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=4, seed=0, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert "sample_id" in sample
            assert "values" in sample
            assert "wall_r" in sample["values"]
            assert "window_shgc" in sample["values"]

    def test_creates_outdir(self, tmp_path: Path) -> None:
        algo = UncertaintyQuantification()
        nested = tmp_path / "deep" / "nested"
        algo.generate_samples(_VARIABLES_2D, n_samples=2, seed=0, outdir=nested)
        assert nested.is_dir()

    def test_seed_reproducible(self, tmp_path: Path) -> None:
        algo = UncertaintyQuantification()
        r1 = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r1")
        r2 = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r2")
        assert json.loads(r1.read_text()) == json.loads(r2.read_text())

    def test_empty_variables(self, tmp_path: Path) -> None:
        algo = UncertaintyQuantification()
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_values_in_range(self, tmp_path: Path) -> None:
        algo = UncertaintyQuantification()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=50, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert 1.0 <= sample["values"]["wall_r"] <= 10.0
            assert 0.1 <= sample["values"]["window_shgc"] <= 0.9


class TestFailureThresholdParsing:
    """Tests for _parse_failure_threshold helper."""

    def test_parses_valid_threshold(self) -> None:
        kpi_name, threshold = _parse_failure_threshold("eui=150")
        assert kpi_name == "eui"
        assert threshold == 150.0

    def test_parses_threshold_with_spaces(self) -> None:
        kpi_name, threshold = _parse_failure_threshold("cooling = 5000")
        assert kpi_name == "cooling"
        assert threshold == 5000.0

    def test_parses_float_threshold(self) -> None:
        kpi_name, threshold = _parse_failure_threshold("temp=23.5")
        assert kpi_name == "temp"
        assert threshold == 23.5

    def test_raises_on_invalid_format(self) -> None:
        with pytest.raises(ValueError, match="must be 'kpi_name=value'"):
            _parse_failure_threshold("invalid_format")

    def test_raises_on_non_numeric_value(self) -> None:
        with pytest.raises(ValueError, match="must be numeric"):
            _parse_failure_threshold("eui=abc")


class TestComputePOF:
    """Tests for _compute_pof helper."""

    def test_pof_greater_direction(self) -> None:
        kpi_values = {"s1": 100.0, "s2": 200.0, "s3": 50.0, "s4": 180.0}
        result = _compute_pof(kpi_values, kpi_name="eui", threshold=150.0, direction="greater")
        assert result["pof"] == 0.5
        assert result["n_failed"] == 2
        assert result["n_total"] == 4
        assert result["threshold"] == 150.0
        assert result["direction"] == "greater"

    def test_pof_less_direction(self) -> None:
        kpi_values = {"s1": 100.0, "s2": 200.0, "s3": 250.0, "s4": 180.0}
        result = _compute_pof(kpi_values, kpi_name="eui", threshold=150.0, direction="less")
        assert result["pof"] == 0.25
        assert result["n_failed"] == 1

    def test_pof_no_failures(self) -> None:
        kpi_values = {"s1": 100.0, "s2": 120.0, "s3": 80.0}
        result = _compute_pof(kpi_values, kpi_name="eui", threshold=200.0, direction="greater")
        assert result["pof"] == 0.0
        assert result["n_failed"] == 0

    def test_pof_all_failures(self) -> None:
        kpi_values = {"s1": 200.0, "s2": 250.0, "s3": 300.0}
        result = _compute_pof(kpi_values, kpi_name="eui", threshold=150.0, direction="greater")
        assert result["pof"] == 1.0
        assert result["n_failed"] == 3


class TestComputeCI:
    """Tests for _compute_confidence_interval helper."""

    def test_ci_basic(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = _compute_confidence_interval(values, confidence=0.95)
        assert "mean" in result
        assert "ci_lower" in result
        assert "ci_upper" in result
        assert "std" in result
        assert result["n"] == 5

    def test_ci_single_value(self) -> None:
        values = np.array([5.0])
        result = _compute_confidence_interval(values)
        assert result["mean"] == 5.0
        assert result["ci_lower"] == 5.0
        assert result["ci_upper"] == 5.0

    def test_ci_order(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = _compute_confidence_interval(values)
        assert result["ci_lower"] <= result["mean"]
        assert result["ci_upper"] >= result["mean"]


class TestComputeDistributionSummary:
    """Tests for _compute_distribution_summary helper."""

    def test_summary_stats(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = _compute_distribution_summary(values)
        assert result["mean"] == 3.0
        assert result["median"] == 3.0
        assert result["min"] == 1.0
        assert result["max"] == 5.0
        assert result["n"] == 5

    def test_histogram_data(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        result = _compute_distribution_summary(values)
        assert "histogram" in result
        assert len(result["histogram"]) > 0
        for bin_data in result["histogram"]:
            assert "bin_start" in bin_data
            assert "bin_end" in bin_data
            assert "count" in bin_data

    def test_percentiles(self) -> None:
        values = np.linspace(0, 100, 101)
        result = _compute_distribution_summary(values)
        assert "percentiles" in result
        assert "p5" in result["percentiles"]
        assert "p50" in result["percentiles"]
        assert "p95" in result["percentiles"]
        assert result["percentiles"]["p50"] == 50.0


class TestComputeUQIndices:
    """Tests for UncertaintyQuantification.compute_uq_indices()."""

    @pytest.fixture
    def algo(self) -> UncertaintyQuantification:
        return UncertaintyQuantification()

    @pytest.fixture
    def samples(self, tmp_path: Path) -> list[dict[str, Any]]:
        algo = UncertaintyQuantification()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=8, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        return data["samples"]

    @pytest.fixture
    def kpi_values(self) -> dict[str, dict[str, float]]:
        return {f"{(i + 1):04d}": {"eui": float(i + 1) * 10.0} for i in range(8)}

    def test_compute_uq_indices_creates_file(
        self,
        algo: UncertaintyQuantification,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        uq_path = algo.compute_uq_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
        )
        assert uq_path is not None
        assert uq_path.exists()
        assert uq_path.name == "uq_results.json"

    def test_compute_uq_indices_output_structure(
        self,
        algo: UncertaintyQuantification,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        uq_path = algo.compute_uq_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
        )
        data = json.loads(uq_path.read_text())
        assert data["algorithm"] == "uq"
        assert data["confidence_level"] == 0.95
        assert data["n_samples"] == len(samples)
        assert "distributions" in data
        assert "confidence_intervals" in data

    def test_compute_uq_indices_with_pof(
        self,
        algo: UncertaintyQuantification,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        failure_thresholds = {"eui": (25.0, "greater")}
        uq_path = algo.compute_uq_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
            failure_thresholds=failure_thresholds,
        )
        data = json.loads(uq_path.read_text())
        assert "probability_of_failure" in data
        assert "eui" in data["probability_of_failure"]
        pof = data["probability_of_failure"]["eui"]
        assert pof["pof"] == 0.75
        assert pof["n_failed"] == 6
        assert pof["n_total"] == 8

    def test_compute_uq_indices_distribution_keys(
        self,
        algo: UncertaintyQuantification,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        uq_path = algo.compute_uq_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
        )
        data = json.loads(uq_path.read_text())
        dist = data["distributions"]["eui"]
        assert "mean" in dist
        assert "std" in dist
        assert "median" in dist
        assert "min" in dist
        assert "max" in dist
        assert "percentiles" in dist
        assert "histogram" in dist

    def test_compute_uq_indices_ci_keys(
        self,
        algo: UncertaintyQuantification,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        uq_path = algo.compute_uq_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
        )
        data = json.loads(uq_path.read_text())
        ci = data["confidence_intervals"]["eui"]
        assert "mean" in ci
        assert "ci_lower" in ci
        assert "ci_upper" in ci
        assert "std" in ci

    def test_compute_uq_indices_empty_kpi_values_raises(
        self,
        algo: UncertaintyQuantification,
        samples: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        with pytest.raises(RuntimeError, match="no KPI values provided"):
            algo.compute_uq_indices(
                variables=_VARIABLES_2D,
                samples=samples,
                kpi_values={},
                outdir=tmp_path,
            )

    def test_compute_uq_indices_non_numeric_kpis_produces_empty_results(
        self,
        algo: UncertaintyQuantification,
        samples: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        kpi_values: dict[str, dict[str, float]] = {
            f"{(i + 1):04d}": {"eui": "not_a_number"} for i in range(8)
        }
        uq_path = algo.compute_uq_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
        )
        data = json.loads(uq_path.read_text())
        assert data["distributions"] == {}
        assert data["confidence_intervals"] == {}
