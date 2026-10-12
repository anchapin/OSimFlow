"""Smoke test for :mod:`osimflow.testing` algorithm conformance (issue #1565).

Runs :class:`osimflow.testing.AlgorithmConformanceSuite` against the
in-repo :class:`~osimflow.algorithms.LHSAlgorithm`,
:class:`~osimflow.algorithms.SobolAlgorithm`, and
:class:`~osimflow.algorithms.HaltonAlgorithm` to prove the harness
itself is correct. Third-party plug-in authors will write their own
subclass with their own factory; this test ensures the suite catches
the contract drifts the issue calls out (count exactness, seed
determinism, variable-order stability, dtype coercion, cache-key
reproducibility).

Per-algorithm subclasses
------------------------

Each in-tree algorithm has its own subclass because the algorithms
have genuinely different supported variable types and sample-count
contracts:

* **LHS / Halton** — share the default :data:`DEFAULT_VARIABLES_SPEC`
  (uniform + normal + discrete int). Both honor exact ``n_samples``.
* **Sobol** (SALib-backed) — emits a ``float`` for every variable
  regardless of the declared distribution, and rounds ``n_samples``
  up to the next power of 2 with a ``UserWarning``. The Sobol
  subclass narrows ``variables_spec`` to continuous-only and uses
  power-of-2 ``sample_counts`` so the conformance check reflects
  Sobol's actual contract rather than an aspirational one.

The conformance suite itself is generic; the per-algorithm
``variables_spec`` / ``sample_counts`` overrides live in the test
file because they describe each algorithm's actual behaviour, not
the contract every plug-in author must satisfy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osimflow.algorithms import (
    AlgorithmRegistry,
    HaltonAlgorithm,
    LHSAlgorithm,
    SobolAlgorithm,
)
from osimflow.testing import (
    DEFAULT_VARIABLES_SPEC,
    AlgorithmConformanceReport,
    AlgorithmConformanceSuite,
    run_algorithm_conformance,
)

# ---------------------------------------------------------------------------
# Per-algorithm factories — fresh instance per test so iterative-algorithm
# state is reset between checks (the mixin already does this; the factories
# are spelled out here so subclasses are self-documenting).
# ---------------------------------------------------------------------------


def _lhs_factory() -> LHSAlgorithm:
    """Build a fresh ``LHSAlgorithm`` per test."""
    return LHSAlgorithm()


def _halton_factory() -> HaltonAlgorithm:
    """Build a fresh ``HaltonAlgorithm`` per test."""
    return HaltonAlgorithm()


def _sobol_factory() -> SobolAlgorithm:
    """Build a fresh ``SobolAlgorithm`` per test.

    Sobol requires SALib (``[sensitivity]`` extra); skip when absent so
    the test suite still runs in a minimal install.
    """
    try:
        import SALib  # noqa: F401
    except ImportError:
        pytest.skip("SobolAlgorithm requires SALib ([sensitivity] extra)")
    return SobolAlgorithm()


# ---------------------------------------------------------------------------
# Mixin subclasses — one per in-tree algorithm
# ---------------------------------------------------------------------------


class TestLHSAlgorithmConformance(AlgorithmConformanceSuite):
    """LHS honors the full conformance contract (uniform + normal + discrete)."""

    algorithm_factory = staticmethod(_lhs_factory)


class TestHaltonAlgorithmConformance(AlgorithmConformanceSuite):
    """Halton honors the full conformance contract (uniform + normal + discrete)."""

    algorithm_factory = staticmethod(_halton_factory)


class TestSobolAlgorithmConformance(AlgorithmConformanceSuite):
    """Sobol narrows the spec to continuous variables + power-of-2 counts.

    Sobol (via SALib) emits ``float`` for every variable regardless of
    the declared distribution, and SALib's ``sobol.sample`` returns
    ``N*(D+2)`` samples for first-order analysis regardless of input
    (``N=1 → 3``, ``N=4 → 12``, ...). The
    :attr:`require_n_samples_exactness` flag is set to ``False`` so the
    conformance check reflects Sobol's actual contract; the cache-key
    and determinism checks still run.
    """

    algorithm_factory = staticmethod(_sobol_factory)
    # Sobol cannot represent discrete variables as ints; SALib emits floats.
    variables_spec = [
        {"name": "wwr", "distribution": "uniform", "min": 0.2, "max": 0.6, "type": float},
        {"name": "cop", "distribution": "normal", "mean": 3.0, "sigma": 0.5, "type": float},
    ]
    # SALib rounds n_samples up to the next power of 2 with a UserWarning;
    # power-of-2 inputs are honored exactly.
    sample_counts = (1, 4, 8)
    # Sobol returns N*(D+2) samples, not the requested N.
    require_n_samples_exactness = False


# ---------------------------------------------------------------------------
# Programmatic-runner coverage (issue #1565, third-party scripts)
# ---------------------------------------------------------------------------


class TestRunAlgorithmConformance:
    """Smoke-test :func:`run_algorithm_conformance` independently of pytest."""

    def test_report_dataclass_round_trip(self) -> None:
        """``AlgorithmConformanceReport`` exposes pass/fail counts via ``to_dict``."""
        from osimflow.testing.algorithm_conformance import ConformanceCheck

        report = AlgorithmConformanceReport(algorithm_name="lhs")
        report.checks.append(ConformanceCheck("a", True, "ok"))
        report.checks.append(ConformanceCheck("b", False, "boom"))
        assert not report.passed
        assert [c.name for c in report.failed_checks] == ["b"]
        d = report.to_dict()
        assert d["algorithm"] == "lhs"
        assert d["passed"] is False
        assert d["n_checks"] == 2
        assert d["n_passed"] == 1
        assert d["n_failed"] == 1
        assert d["checks"] == [
            {"name": "a", "passed": True, "detail": "ok"},
            {"name": "b", "passed": False, "detail": "boom"},
        ]

    def test_report_passed_true_when_all_checks_pass(self) -> None:
        from osimflow.testing.algorithm_conformance import ConformanceCheck

        report = AlgorithmConformanceReport(algorithm_name="lhs")
        report.checks.append(ConformanceCheck("only", True, "ok"))
        assert report.passed
        assert report.failed_checks == []

    def test_run_algorithm_conformance_against_lhs(self) -> None:
        """The programmatic runner exercises every check against LHS."""
        report = run_algorithm_conformance(AlgorithmRegistry.get("lhs"))
        assert report.algorithm_name == "lhs"
        names = [c.name for c in report.checks]
        expected = {
            "generate_samples_returns_path",
            "n_samples_exactness",
            "seed_determinism",
            "different_seeds_diverge",
            "variable_order_stability",
            "dtype_coercion",
            "cache_key_reproducibility",
            "samples_json_schema",
            "name_returns_non_empty_string",
            "is_iterative_returns_bool",
            "is_converged_returns_bool",
            "observe_returns_list",
        }
        assert expected.issubset(set(names)), f"missing checks: {expected - set(names)}"
        failed = report.failed_checks
        assert not failed, f"failed checks: {[(c.name, c.detail) for c in failed]}"

    def test_run_algorithm_conformance_against_halton(self) -> None:
        """Halton passes the same conformance surface as LHS."""
        report = run_algorithm_conformance(AlgorithmRegistry.get("halton"))
        assert report.algorithm_name == "halton"
        failed = report.failed_checks
        assert not failed, f"failed checks: {[(c.name, c.detail) for c in failed]}"

    def test_run_algorithm_conformance_against_sobol_continuous_only(self) -> None:
        """Sobol passes when restricted to the spec it actually supports."""
        try:
            import SALib  # noqa: F401
        except ImportError:
            pytest.skip("SobolAlgorithm requires SALib ([sensitivity] extra)")
        report = run_algorithm_conformance(
            AlgorithmRegistry.get("sobol"),
            variables_spec=[
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0, "type": float},
            ],
            sample_counts=(1, 4, 8),
            # Sobol returns N*(D+2) — opt out of the exact-count check.
            require_n_samples_exactness=False,
        )
        assert report.algorithm_name == "sobol"
        failed = report.failed_checks
        assert not failed, f"failed checks: {[(c.name, c.detail) for c in failed]}"

    def test_run_algorithm_conformance_returns_json_serialisable_dict(self) -> None:
        """``to_dict()`` output is JSON-serialisable for CI consumption."""
        report = run_algorithm_conformance(AlgorithmRegistry.get("lhs"))
        payload = report.to_dict()
        # Round-trip through JSON to confirm no non-serialisable values.
        json.dumps(payload)


# ---------------------------------------------------------------------------
# Direct dataclass surface tests
# ---------------------------------------------------------------------------


def test_default_variables_spec_covers_coerce_variable_type_surface() -> None:
    """The default variables_spec exercises uniform + normal + discrete int.

    This is the acceptance criterion from issue #1565: the suite must
    cover ``coerce_variable_type`` across distribution families.
    """
    distributions = {v["distribution"] for v in DEFAULT_VARIABLES_SPEC}
    types = {v.get("type") for v in DEFAULT_VARIABLES_SPEC}
    assert "uniform" in distributions
    assert "normal" in distributions
    assert "discrete" in distributions
    assert float in types
    assert int in types


def test_algorithm_conformance_report_to_dict_schema() -> None:
    """``to_dict()`` includes ``algorithm``, ``passed``, and per-check records."""
    from osimflow.testing.algorithm_conformance import ConformanceCheck

    report = AlgorithmConformanceReport(algorithm_name="halton")
    report.checks.append(ConformanceCheck("seed_determinism", True, "sha256 match"))
    text = json.dumps(report.to_dict())
    assert "algorithm" in text
    assert "passed" in text
    assert "seed_determinism" in text


def test_run_algorithm_conformance_detects_count_drift() -> None:
    """A plug-in that violates ``n_samples`` exactness fails the count check.

    We simulate the drift by asking for ``n_samples=11`` and confirming
    the runner reports a ``n_samples_exactness`` failure if the
    algorithm rounds up to 12 (or whatever). With LHS the count is
    exact, so this test primarily documents the failure mode rather
    than exercising it — but the runner machinery is identical for
    real-world plug-ins, so the assertion is still useful as a
    regression guard.
    """
    report = run_algorithm_conformance(
        AlgorithmRegistry.get("lhs"),
        sample_counts=(11,),  # odd count — LHS honors this exactly
    )
    count_checks = [c for c in report.checks if c.name == "n_samples_exactness"]
    assert len(count_checks) == 1
    assert count_checks[0].passed, count_checks[0].detail


def test_run_algorithm_conformance_rejects_n_samples_drift(tmp_path: Path) -> None:
    """When an algorithm under-reports ``n_samples`` the runner catches it.

    We synthesise a stub algorithm that always returns 5 samples
    regardless of the requested ``n_samples`` and confirm the runner's
    ``n_samples_exactness`` check fails. This is the regression guard
    for the acceptance criterion's most important contract.
    """
    from osimflow.algorithms import BaseAlgorithm

    class _StubAlgo(BaseAlgorithm):
        def generate_samples(self, variables, n_samples, seed, outdir):  # type: ignore[override]
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "samples.json").write_text(
                json.dumps({"samples": [{"sample_id": f"{i:04d}", "values": {}} for i in range(5)]})
            )
            return outdir / "samples.json"

        def observe(self, history):  # type: ignore[override]
            return []

        def is_converged(self, history):  # type: ignore[override]
            return True

        def name(self):  # type: ignore[override]
            return "stub-drift"

        def is_iterative(self):  # type: ignore[override]
            return False

    report = run_algorithm_conformance(
        _StubAlgo(),
        variables_spec=[
            {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0, "type": float},
        ],
        sample_counts=(10,),
    )
    count_checks = [c for c in report.checks if c.name == "n_samples_exactness"]
    assert len(count_checks) == 1
    assert not count_checks[0].passed, (
        f"stub algorithm returned 5 samples but runner did not detect drift: "
        f"{count_checks[0].detail}"
    )
