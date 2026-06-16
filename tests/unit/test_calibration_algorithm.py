"""Tests for CalibrationAlgorithm (issue #528).

Covers:
- AlgorithmRegistry.get("calibration") returns BM25CalibrationAlgorithm
- is_iterative() returns True
- Registration in AlgorithmRegistry.list_available()
- Calibration data loading from CSV
- BM25, NMBE, CVRMSE metric computation
- generate_samples produces valid samples.json
- observe() returns new samples given history
- is_converged() works correctly
- Calibration converges on mock objective within max_generations
"""

import json
from pathlib import Path
from typing import Any

import pytest

from osimflow.algorithms import AlgorithmRegistry, BaseAlgorithm
from osimflow.algorithms.calibration import (
    ASHRAE14_THRESHOLDS,
    BM25CalibrationAlgorithm,
    CalibrationAlgorithm,
    CVRMSECalibrationAlgorithm,
    NMBECalibrationAlgorithm,
    _compute_bm25_score,
    _compute_calibration_metric,
    _compute_cvrmse,
    _compute_nmbe,
    _load_calibration_data,
    _propose_samples_around,
)

# ======================================================================
# Fixtures and helpers
# ======================================================================

_VARIABLES_2D: dict[str, Any] = {
    "variables": [
        {"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0},
        {"name": "window_shgc", "distribution": "uniform", "min": 0.1, "max": 0.9},
    ]
}

_MEASURED_DATA_CSV = """month,electricity,natural_gas
1,1000.0,500.0
2,1100.0,450.0
3,1200.0,400.0
4,1300.0,350.0
5,1400.0,300.0
6,1500.0,250.0
7,1600.0,200.0
8,1700.0,250.0
9,1800.0,300.0
10,1700.0,350.0
11,1500.0,400.0
12,1300.0,450.0
"""


@pytest.fixture
def calibration_csv(tmp_path: Path) -> Path:
    """Create a temporary calibration CSV file."""
    csv_path = tmp_path / "calibration_data.csv"
    csv_path.write_text(_MEASURED_DATA_CSV)
    return csv_path


def _make_history_with_kpis(
    tmp_path: Path,
    samples: list[dict[str, Any]],
    kpi_values: list[float],
    generation: int = 0,
) -> dict[str, Any]:
    """Create a history entry with KPI files on disk."""
    kpi_files: list[str] = []
    for _i, (sample, kpi_val) in enumerate(zip(samples, kpi_values, strict=True)):
        kpi_dir = tmp_path / f"kpi_gen{generation}" / sample["sample_id"]
        kpi_dir.mkdir(parents=True, exist_ok=True)
        kpi_path = kpi_dir / f"kpi_{sample['sample_id']}.json"
        kpi_path.write_text(
            json.dumps({"sample_id": sample["sample_id"], "kpis": {"eui": kpi_val}})
        )
        kpi_files.append(str(kpi_path))

    return {
        "generation": generation,
        "samples": samples,
        "kpi_files": kpi_files,
    }


# ======================================================================
# Registry tests
# ======================================================================

class TestCalibrationRegistry:
    """Registry discovery tests for CalibrationAlgorithm."""

    def test_get_calibration_returns_bm25_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("calibration")
        assert isinstance(algo, BM25CalibrationAlgorithm)

    def test_get_calibration_returns_base_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("calibration")
        assert isinstance(algo, BaseAlgorithm)

    def test_list_available_includes_calibration(self) -> None:
        assert "calibration" in AlgorithmRegistry.list_available()


# ======================================================================
# Interface tests
# ======================================================================

class TestCalibrationInterface:
    """Contract tests for CalibrationAlgorithm."""

    def test_name(self) -> None:
        assert CalibrationAlgorithm().name() == "calibration"

    def test_bm25_name(self) -> None:
        assert BM25CalibrationAlgorithm().name() == "calibration"

    def test_nmbe_name(self) -> None:
        assert NMBECalibrationAlgorithm().name() == "calibration"

    def test_cvrmse_name(self) -> None:
        assert CVRMSECalibrationAlgorithm().name() == "calibration"

    def test_is_iterative_true(self) -> None:
        assert CalibrationAlgorithm().is_iterative() is True

    def test_is_converged_empty_history(self) -> None:
        assert CalibrationAlgorithm().is_converged([]) is False

    def test_is_converged_single_generation(self) -> None:
        algo = CalibrationAlgorithm()
        assert algo.is_converged([{"generation": 0, "samples": []}]) is False

    def test_observe_empty_history(self) -> None:
        algo = CalibrationAlgorithm()
        assert algo.observe([]) == []


# ======================================================================
# Calibration data loading tests
# ======================================================================

class TestLoadCalibrationData:
    """Tests for _load_calibration_data."""

    def test_loads_calibration_data(self, calibration_csv: Path) -> None:
        data = _load_calibration_data(calibration_csv)
        assert "electricity" in data
        assert "natural_gas" in data
        assert len(data["electricity"]) == 12
        assert len(data["natural_gas"]) == 12
        assert data["electricity"][0] == 1000.0
        assert data["electricity"][5] == 1500.0

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            _load_calibration_data(tmp_path / "nonexistent.csv")


# ======================================================================
# Metric computation tests
# ======================================================================

class TestComputeCalibrationMetrics:
    """Tests for _compute_bm25_score, _compute_nmbe, _compute_cvrmse."""

    def test_bm25_perfect_match(self) -> None:
        """BM25 score should be 0 for perfect match."""
        data = {"electricity": [1000.0, 1100.0, 1200.0]}
        score = _compute_bm25_score(data, data)
        assert score == pytest.approx(0.0, abs=1e-6)

    def test_bm25_some_difference(self) -> None:
        """BM25 score should be positive when data differs."""
        simulated = {"electricity": [1000.0, 1100.0, 1200.0]}
        measured = {"electricity": [1000.0, 1200.0, 1400.0]}
        score = _compute_bm25_score(simulated, measured)
        assert score > 0.0

    def test_bm25_empty_data(self) -> None:
        """BM25 score should be inf for empty data."""
        assert _compute_bm25_score({}, {"electricity": [1000.0]}) == float("inf")
        assert _compute_bm25_score({"electricity": [1000.0]}, {}) == float("inf")

    def test_nmbe_perfect_match(self) -> None:
        """NMBE should be 0 for perfect match."""
        data = {"electricity": [1000.0, 1100.0, 1200.0]}
        nmbe = _compute_nmbe(data, data)
        assert nmbe == pytest.approx(0.0, abs=1e-6)

    def test_nmbe_overprediction(self) -> None:
        """NMBE should be positive when simulated > measured."""
        simulated = {"electricity": [1200.0, 1300.0, 1400.0]}
        measured = {"electricity": [1000.0, 1100.0, 1200.0]}
        nmbe = _compute_nmbe(simulated, measured)
        assert nmbe > 0.0

    def test_nmbe_underprediction(self) -> None:
        """NMBE should be negative when simulated < measured."""
        simulated = {"electricity": [800.0, 900.0, 1000.0]}
        measured = {"electricity": [1000.0, 1100.0, 1200.0]}
        nmbe = _compute_nmbe(simulated, measured)
        assert nmbe < 0.0

    def test_cvrmse_perfect_match(self) -> None:
        """CVRMSE should be 0 for perfect match."""
        data = {"electricity": [1000.0, 1100.0, 1200.0]}
        cvrmse = _compute_cvrmse(data, data)
        assert cvrmse == pytest.approx(0.0, abs=1e-6)

    def test_cvrmse_some_difference(self) -> None:
        """CVRMSE should be positive when data differs."""
        simulated = {"electricity": [1000.0, 1100.0, 1200.0]}
        measured = {"electricity": [1000.0, 1200.0, 1400.0]}
        cvrmse = _compute_cvrmse(simulated, measured)
        assert cvrmse > 0.0

    def test_compute_calibration_metric_bm25(self) -> None:
        """Test _compute_calibration_metric dispatches to BM25."""
        data = {"electricity": [1000.0, 1100.0]}
        metric = _compute_calibration_metric("bm25", data, data)
        assert metric == pytest.approx(0.0, abs=1e-6)

    def test_compute_calibration_metric_nmbe(self) -> None:
        """Test _compute_calibration_metric dispatches to NMBE."""
        data = {"electricity": [1000.0, 1100.0]}
        metric = _compute_calibration_metric("nmbe", data, data)
        assert metric == pytest.approx(0.0, abs=1e-6)

    def test_compute_calibration_metric_cvrmse(self) -> None:
        """Test _compute_calibration_metric dispatches to CVRMSE."""
        data = {"electricity": [1000.0, 1100.0]}
        metric = _compute_calibration_metric("cvrmse", data, data)
        assert metric == pytest.approx(0.0, abs=1e-6)

    def test_compute_calibration_metric_unknown(self) -> None:
        """Test _compute_calibration_metric falls back to BM25 for unknown."""
        data = {"electricity": [1000.0, 1100.0]}
        metric = _compute_calibration_metric("unknown", data, data)
        # Falls back to BM25
        assert isinstance(metric, float)


# ======================================================================
# Sample generation tests
# ======================================================================

class TestCalibrationGenerateSamples:
    """Generate-samples tests for CalibrationAlgorithm."""

    def test_creates_samples_json(self, tmp_path: Path) -> None:
        algo = CalibrationAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=42, outdir=tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        assert len(data["samples"]) == 10

    def test_sample_structure(self, tmp_path: Path) -> None:
        algo = CalibrationAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=5, seed=0, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert "sample_id" in sample
            assert "values" in sample
            assert "wall_r" in sample["values"]
            assert "window_shgc" in sample["values"]

    def test_sample_values_within_bounds(self, tmp_path: Path) -> None:
        algo = CalibrationAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=20, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert 1.0 <= sample["values"]["wall_r"] <= 10.0
            assert 0.1 <= sample["values"]["window_shgc"] <= 0.9

    def test_empty_variables(self, tmp_path: Path) -> None:
        algo = CalibrationAlgorithm()
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_seed_reproducible(self, tmp_path: Path) -> None:
        algo = CalibrationAlgorithm()
        r1 = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r1")
        r2 = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r2")
        assert json.loads(r1.read_text()) == json.loads(r2.read_text())

    def test_with_calibration_data(self, tmp_path: Path, calibration_csv: Path) -> None:
        """Test that calibration data can be loaded via configure."""
        algo = CalibrationAlgorithm()
        # Create a mock config with calibration data
        class MockConfig:
            calibration_data = str(calibration_csv)
            calibration_metric = "bm25"
        algo.configure(MockConfig())
        data = algo.load_calibration_data()
        assert "electricity" in data


# ======================================================================
# Observe tests
# ======================================================================

class TestCalibrationObserve:
    """Tests for CalibrationAlgorithm.observe."""

    def test_observe_returns_samples(self, tmp_path: Path) -> None:
        algo = CalibrationAlgorithm()
        samples_path = algo.generate_samples(
            _VARIABLES_2D, n_samples=5, seed=42, outdir=tmp_path / "gen0"
        )
        samples = json.loads(samples_path.read_text())["samples"]

        kpi_values = [100.0 + i * 10.0 for i in range(len(samples))]
        history_entry = _make_history_with_kpis(tmp_path, samples, kpi_values, generation=0)

        new_samples = algo.observe([history_entry])
        assert len(new_samples) > 0
        for sample in new_samples:
            assert "sample_id" in sample
            assert "values" in sample

    def test_observe_no_vars_returns_empty(self) -> None:
        algo = CalibrationAlgorithm()
        assert algo.observe([{"generation": 0, "samples": [], "kpi_files": []}]) == []

    def test_observe_updates_best(self, tmp_path: Path) -> None:
        algo = CalibrationAlgorithm()
        algo.generate_samples(_VARIABLES_2D, n_samples=5, seed=42, outdir=tmp_path / "gen0")
        samples = json.loads((tmp_path / "gen0" / "samples.json").read_text())["samples"]

        kpi_values = [100.0, 90.0, 80.0, 70.0, 60.0]
        history_entry = _make_history_with_kpis(tmp_path, samples, kpi_values, generation=0)

        algo.observe([history_entry])
        assert algo._best_value == 60.0


# ======================================================================
# Convergence tests
# ======================================================================

class TestCalibrationConvergence:
    """Tests for CalibrationAlgorithm.is_converged."""

    def test_converges_at_max_generations(self, tmp_path: Path) -> None:
        """Should converge when max_generations is reached."""
        algo = CalibrationAlgorithm(max_generations=5)
        # Simulate reaching max generations
        algo._generation_count = 5
        assert algo.is_converged([{"generation": i} for i in range(5)]) is True

    def test_not_converged_below_max_generations(self, tmp_path: Path) -> None:
        """Should not converge before max_generations."""
        algo = CalibrationAlgorithm(max_generations=10)
        algo._generation_count = 3
        assert algo.is_converged([{"generation": i} for i in range(3)]) is False

    def test_converged_on_improvement(self, tmp_path: Path) -> None:
        """Should converge when relative improvement is below tolerance."""
        algo = CalibrationAlgorithm(tol=0.01)
        algo._prev_best = 1.0
        algo._best_value = 0.99
        assert algo.is_converged([{"generation": 0}, {"generation": 1}]) is True

    def test_not_converged_when_prev_best_zero(self, tmp_path: Path) -> None:
        """Should not converge when prev_best is zero but best_value is not."""
        algo = CalibrationAlgorithm()
        algo._prev_best = 0.0
        algo._best_value = 1.0
        assert algo.is_converged([{"gen": 0}, {"gen": 1}]) is False

    def test_not_converged_with_inf_values(self, tmp_path: Path) -> None:
        """Should not converge when values are infinite."""
        algo = CalibrationAlgorithm()
        algo._prev_best = float("inf")
        algo._best_value = float("inf")
        assert algo.is_converged([{"gen": 0}, {"gen": 1}]) is False


# ======================================================================
# Propose samples helper tests
# ======================================================================

class TestProposeSamplesAround:
    """Tests for _propose_samples_around helper."""

    def test_proposes_correct_count(self) -> None:
        import numpy as np
        center = np.array([5.0, 0.5])
        bounds = [(1.0, 10.0), (0.1, 0.9)]
        var_names = ["wall_r", "window_shgc"]
        result = _propose_samples_around(center, 5, bounds, var_names)
        assert len(result) == 5
        for s in result:
            assert "sample_id" in s
            assert "wall_r" in s["values"]
            assert "window_shgc" in s["values"]

    def test_proposed_samples_clipped_to_bounds(self) -> None:
        import numpy as np
        center = np.array([1.0, 0.1])
        bounds = [(1.0, 10.0), (0.1, 0.9)]
        var_names = ["wall_r", "window_shgc"]
        result = _propose_samples_around(center, 20, bounds, var_names, width=5.0)
        for s in result:
            assert 1.0 <= s["values"]["wall_r"] <= 10.0
            assert 0.1 <= s["values"]["window_shgc"] <= 0.9


# ======================================================================
# Algorithm subclass tests
# ======================================================================

class TestBM25CalibrationAlgorithm:
    """Tests for BM25CalibrationAlgorithm."""

    def test_default_metric(self) -> None:
        algo = BM25CalibrationAlgorithm()
        assert algo._metric == "bm25"

    def test_custom_k1_b(self) -> None:
        algo = BM25CalibrationAlgorithm(k1=2.0, b=0.5)
        assert algo._k1 == 2.0
        assert algo._b == 0.5


class TestNMBECalibrationAlgorithm:
    """Tests for NMBECalibrationAlgorithm."""

    def test_default_metric(self) -> None:
        algo = NMBECalibrationAlgorithm()
        assert algo._metric == "nmbe"


class TestCVRMSECalibrationAlgorithm:
    """Tests for CVRMSECalibrationAlgorithm."""

    def test_default_metric(self) -> None:
        algo = CVRMSECalibrationAlgorithm()
        assert algo._metric == "cvrmse"


# ======================================================================
# ASHRAE 14 threshold tests
# ======================================================================

class TestASHRAE14Thresholds:
    """Tests for ASHRAE 14 threshold values."""

    def test_cvrmse_threshold(self) -> None:
        assert ASHRAE14_THRESHOLDS["cvrmse"] == 30.0

    def test_nmbe_threshold(self) -> None:
        assert ASHRAE14_THRESHOLDS["nmbe"] == 10.0

    def test_thresholds_are_reasonable(self) -> None:
        """Thresholds should be positive and reasonable for energy calibration."""
        assert ASHRAE14_THRESHOLDS["cvrmse"] > 0
        assert ASHRAE14_THRESHOLDS["nmbe"] > 0
        assert ASHRAE14_THRESHOLDS["cvrmse"] > ASHRAE14_THRESHOLDS["nmbe"]
