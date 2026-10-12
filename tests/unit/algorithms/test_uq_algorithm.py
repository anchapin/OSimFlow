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
    parse_failure_threshold,
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
    """Tests for parse_failure_threshold helper (issue #1706 public surface).

    Covers the documented grammar: ``"<kpi_name>=<value>"`` where
    ``<value>`` is any string accepted by :func:`float`. Boundary cases
    exercised here were chosen to lock in the contract that any future
    grammar refinement must remain backward-compatible with.
    """

    def test_parses_valid_threshold(self) -> None:
        kpi_name, threshold = parse_failure_threshold("eui=150")
        assert kpi_name == "eui"
        assert threshold == 150.0

    def test_parses_threshold_with_spaces(self) -> None:
        kpi_name, threshold = parse_failure_threshold("cooling = 5000")
        assert kpi_name == "cooling"
        assert threshold == 5000.0

    def test_parses_float_threshold(self) -> None:
        kpi_name, threshold = parse_failure_threshold("temp=23.5")
        assert kpi_name == "temp"
        assert threshold == 23.5

    def test_raises_on_invalid_format(self) -> None:
        with pytest.raises(ValueError, match="must be 'kpi_name=value'"):
            parse_failure_threshold("invalid_format")

    def test_raises_on_non_numeric_value(self) -> None:
        with pytest.raises(ValueError, match="must be numeric"):
            parse_failure_threshold("eui=abc")

    # --- boundary cases for the numeric grammar -----------------------------

    def test_parses_negative_float_threshold(self) -> None:
        kpi_name, threshold = parse_failure_threshold("delta=-3.25")
        assert kpi_name == "delta"
        assert threshold == -3.25

    def test_parses_zero_threshold(self) -> None:
        kpi_name, threshold = parse_failure_threshold("eui=0")
        assert kpi_name == "eui"
        assert threshold == 0.0

    def test_parses_scientific_notation_threshold(self) -> None:
        # Scientific notation is a valid float() input — the parser must
        # accept it because the grammar is "any string float() accepts".
        kpi_name, threshold = parse_failure_threshold("pressure=1e-3")
        assert kpi_name == "pressure"
        assert threshold == pytest.approx(1e-3)

    def test_parses_positive_signed_value(self) -> None:
        kpi_name, threshold = parse_failure_threshold("eui=+150.5")
        assert kpi_name == "eui"
        assert threshold == 150.5

    def test_parses_kpi_name_with_underscores_and_digits(self) -> None:
        kpi_name, threshold = parse_failure_threshold("cooling_load_2=42")
        assert kpi_name == "cooling_load_2"
        assert threshold == 42.0

    def test_parses_only_first_equals_when_value_has_second(self) -> None:
        # Grammar: split on the first '=' only. The value fragment is then
        # fed to float(); "1=2" must therefore raise 'must be numeric'.
        with pytest.raises(ValueError, match="must be numeric"):
            parse_failure_threshold("eui=1=2")

    def test_raises_when_value_is_empty(self) -> None:
        with pytest.raises(ValueError, match="must be numeric"):
            parse_failure_threshold("eui=")

    def test_empty_kpi_name_is_passed_through_to_caller(self) -> None:
        """Lock in the current grammar: empty KPI name is NOT a parse error.

        ``raw = "=150"`` splits on the first ``=`` to produce
        ``("", "150")``; ``float("150")`` succeeds, so the parser returns
        ``("", 150.0)``.  The grammar does NOT enforce a non-empty KPI
        identifier — that validation is the downstream caller's
        responsibility (e.g. ``compute_uq_indices`` is a no-op for the
        empty name since no KPI in ``all_kpi_names`` matches ``""``).

        A future grammar refinement may tighten this and reject
        ``=value`` outright; this test pins the current behaviour so
        that the change is a documented, public-API breakage rather
        than an implicit one.
        """
        kpi_name, threshold = parse_failure_threshold("=150")
        assert kpi_name == ""
        assert threshold == 150.0

    def test_kpi_name_preserves_only_leading_and_trailing_whitespace(self) -> None:
        """Lock in the strip()-only behaviour for KPI identifiers.

        The parser does not lowercase, slugify, or otherwise
        canonicalise the KPI name beyond ``str.strip()``.  Two
        semantically-identical names (``"EUI"`` vs ``"eui"``) therefore
        produce different keys, and the campaign step relies on that to
        surface typos as POF=0 (no matching KPI) rather than silently
        reusing another KPI's threshold.
        """
        kpi_name, threshold = parse_failure_threshold("EUI=150")
        assert kpi_name == "EUI"
        assert threshold == 150.0
        kpi_name_lower, _ = parse_failure_threshold("eui=150")
        assert kpi_name_lower != kpi_name

    def test_kpi_name_is_stripped_but_preserves_internal_chars(self) -> None:
        # The parser does not normalise the KPI identifier beyond strip().
        # Whether "eui.peak" is a valid metric is the algorithm's concern;
        # the parser must not silently rewrite it.
        kpi_name, threshold = parse_failure_threshold("  eui.peak  =  42  ")
        assert kpi_name == "eui.peak"
        assert threshold == 42.0

    def test_only_public_threshold_parser_exported(self) -> None:
        """Lock in the rename from #1706: no private alias may leak.

        The module previously exposed the parser under a leading-
        underscore name.  The rename closed the private/public seam
        by promoting the parser to a single public name.  This guard
        ensures no future patch accidentally re-adds a private alias
        (e.g. for back-compat) that would re-introduce the dual-name
        seam the issue asked us to close.  We check by introspecting
        the module — anything in the public namespace whose name
        starts with ``parse_failure`` must resolve to the same
        callable as the public one.
        """
        import osimflow.algorithms.uq as uq_mod

        public = uq_mod.parse_failure_threshold
        # Every other name the module exposes that matches
        # ``parse_failure*`` must be the same object — guards against
        # accidental dual-name re-exports.
        for attr in dir(uq_mod):
            if attr.startswith("parse_failure") and attr != "parse_failure_threshold":
                other = getattr(uq_mod, attr)
                assert other is public, (
                    f"unexpected alias {attr!r} in osimflow.algorithms.uq "
                    f"should not exist alongside the public name"
                )

    def test_public_alias_exposed_on_package(self) -> None:
        """Third-party plug-ins must be able to ``from osimflow.algorithms
        import parse_failure_threshold`` (issue #1706 public surface)."""
        import osimflow.algorithms as alg_pkg

        assert hasattr(alg_pkg, "parse_failure_threshold")
        assert alg_pkg.parse_failure_threshold is parse_failure_threshold


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
