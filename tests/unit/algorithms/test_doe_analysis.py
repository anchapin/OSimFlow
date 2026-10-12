"""Tests for DOE analysis module (issue #405).

Covers:
- DOEAnalysis.load() parses results CSV correctly
- DOEAnalysis.compute_main_effects() returns MainEffect per factor
- DOEAnalysis.compute_interaction_effects() returns InteractionEffect per pair
- DOEAnalysis.compute_factor_sensitivity() returns ranked FactorSensitivity
- DOEAnalysis.to_dict() serializes all results
- DOEAnalysis.write_json() writes valid JSON to disk
- run_doe_analysis() convenience function end-to-end
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from osimflow.algorithms.doe_analysis import (
    DOEAnalysis,
    FactorSensitivity,
    InteractionEffect,
    MainEffect,
    run_doe_analysis,
)


@pytest.fixture
def synthetic_results_csv(tmp_path: Path) -> Path:
    """Create a synthetic DOE results CSV with 2 factors and a response."""
    np.random.seed(42)
    n_samples = 50

    data = {
        "sample_id": [f"s{i:04d}" for i in range(n_samples)],
        "wall_r_value": np.random.choice([10.0, 20.0, 30.0], size=n_samples),
        "roof_r_value": np.random.choice([30.0, 40.0, 50.0], size=n_samples),
        "eui_kwh_m2_yr": (
            100
            + 5 * np.random.choice([10.0, 20.0, 30.0], size=n_samples)
            + 3 * np.random.choice([30.0, 40.0, 50.0], size=n_samples)
            + np.random.normal(0, 5, size=n_samples)
        ),
    }
    df = pd.DataFrame(data)
    csv_path = tmp_path / "results.csv"
    df.to_csv(csv_path, index=False)
    return csv_path


@pytest.fixture
def empty_results_csv(tmp_path: Path) -> Path:
    """Create an empty results CSV."""
    df = pd.DataFrame(columns=["sample_id", "eui_kwh_m2_yr"])
    csv_path = tmp_path / "empty.csv"
    df.to_csv(csv_path, index=False)
    return csv_path


class TestDOEAnalysisLoad:
    """Tests for DOEAnalysis.load()."""

    def test_load_parses_factors(self, synthetic_results_csv: Path) -> None:
        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.load()
        assert len(analyzer._factors) == 2
        assert "wall_r_value" in analyzer._factors
        assert "roof_r_value" in analyzer._factors

    def test_load_parses_n_samples(self, synthetic_results_csv: Path) -> None:
        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.load()
        assert analyzer._df is not None
        assert len(analyzer._df) == 50


class TestDOEAnalysisMainEffects:
    """Tests for DOEAnalysis.compute_main_effects()."""

    def test_main_effects_returns_list(self, synthetic_results_csv: Path) -> None:
        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.load()
        effects = analyzer.compute_main_effects()
        assert isinstance(effects, list)
        assert len(effects) == 2

    def test_main_effects_are_main_effect(self, synthetic_results_csv: Path) -> None:
        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.load()
        effects = analyzer.compute_main_effects()
        for me in effects:
            assert isinstance(me, MainEffect)
            assert me.factor in ("wall_r_value", "roof_r_value")
            assert hasattr(me, "effect_size")
            assert hasattr(me, "p_value")
            assert hasattr(me, "levels")
            assert hasattr(me, "means")

    def test_main_effects_empty_on_empty_csv(self, empty_results_csv: Path) -> None:
        analyzer = DOEAnalysis(empty_results_csv)
        analyzer.load()
        effects = analyzer.compute_main_effects()
        assert effects == []


class TestDOEAnalysisInteractionEffects:
    """Tests for DOEAnalysis.compute_interaction_effects()."""

    def test_interaction_effects_returns_list(self, synthetic_results_csv: Path) -> None:
        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.load()
        effects = analyzer.compute_interaction_effects()
        assert isinstance(effects, list)

    def test_interaction_effects_are_interaction_effect(self, synthetic_results_csv: Path) -> None:
        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.load()
        effects = analyzer.compute_interaction_effects()
        for ie in effects:
            assert isinstance(ie, InteractionEffect)
            assert ie.factor_a != ie.factor_b
            assert hasattr(ie, "f_statistic")
            assert hasattr(ie, "p_value")


class TestDOEAnalysisFactorSensitivity:
    """Tests for DOEAnalysis.compute_factor_sensitivity()."""

    def test_factor_sensitivity_returns_ranked_list(self, synthetic_results_csv: Path) -> None:
        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.load()
        analyzer.compute_main_effects()
        analyzer.compute_interaction_effects()
        sensitivity = analyzer.compute_factor_sensitivity()
        assert isinstance(sensitivity, list)
        assert len(sensitivity) == 2

    def test_factor_sensitivity_are_factor_sensitivity(self, synthetic_results_csv: Path) -> None:
        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.load()
        analyzer.compute_main_effects()
        analyzer.compute_interaction_effects()
        sensitivity = analyzer.compute_factor_sensitivity()
        for fs in sensitivity:
            assert isinstance(fs, FactorSensitivity)
            assert hasattr(fs, "percent_contribution")
            assert fs.percent_contribution >= 0

    def test_factor_sensitivity_sorted_descending(self, synthetic_results_csv: Path) -> None:
        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.load()
        analyzer.compute_main_effects()
        analyzer.compute_interaction_effects()
        sensitivity = analyzer.compute_factor_sensitivity()
        contributions = [fs.percent_contribution for fs in sensitivity]
        assert contributions == sorted(contributions, reverse=True)


class TestDOEAnalysisSerialization:
    """Tests for DOEAnalysis.to_dict() and write_json()."""

    def test_to_dict_returns_dict(self, synthetic_results_csv: Path) -> None:
        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.compute_main_effects()
        analyzer.compute_interaction_effects()
        analyzer.compute_factor_sensitivity()
        result = analyzer.to_dict()
        assert isinstance(result, dict)
        assert "main_effects" in result
        assert "interaction_effects" in result
        assert "factor_sensitivity" in result

    def test_to_dict_serializable(self, synthetic_results_csv: Path) -> None:
        import json

        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.compute_main_effects()
        analyzer.compute_interaction_effects()
        analyzer.compute_factor_sensitivity()
        result = analyzer.to_dict()
        json.dumps(result)

    def test_write_json_creates_file(self, synthetic_results_csv: Path, tmp_path: Path) -> None:
        analyzer = DOEAnalysis(synthetic_results_csv)
        analyzer.compute_main_effects()
        analyzer.compute_interaction_effects()
        analyzer.compute_factor_sensitivity()
        path = analyzer.write_json(tmp_path)
        assert path.exists()
        assert path.name == "doe_analysis.json"


class TestRunDOEAnalysis:
    """Tests for run_doe_analysis() convenience function."""

    def test_run_doe_analysis_returns_path(
        self, synthetic_results_csv: Path, tmp_path: Path
    ) -> None:
        path = run_doe_analysis(synthetic_results_csv, tmp_path)
        assert isinstance(path, Path)
        assert path.exists()
        assert path.name == "doe_analysis.json"

    def test_run_doe_analysis_json_valid(self, synthetic_results_csv: Path, tmp_path: Path) -> None:
        import json

        path = run_doe_analysis(synthetic_results_csv, tmp_path)
        data = json.loads(path.read_text())
        assert "main_effects" in data
        assert "factor_sensitivity" in data
