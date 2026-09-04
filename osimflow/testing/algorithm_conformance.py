"""Algorithm conformance suite (issue #1565).

Exercises the :class:`~osimflow.algorithms.BaseAlgorithm` contract that
every community plug-in author must satisfy when shipping a sampling
algorithm through the ``osimflow.algorithms`` entry-point group
(issue #432). Mirrors :mod:`osimflow.testing.executor_conformance`
exactly — same mixin + runner shape, same ``ConformanceCheck``
dataclass, same ``algorithm_factory`` factory pattern.

Why a conformance harness for algorithms
---------------------------------------

A bad sampling plug-in silently corrupts campaign reproducibility:
nondeterministic or count-violating samples invalidate both cache hits
and the resume-by-replay guarantee, which is harder to detect than an
executor failure and lands exactly on the reproducibility promise.
This suite catches the four most common contract drifts before a
plug-in ever reaches a real campaign:

* **Sample-count exactness** — ``len(samples) == n_samples`` for
  several values of ``n_samples``. Plug-ins that internally round to a
  power of two (Sobol via SALib) violate this contract; the suite's
  ``sample_counts`` class attribute lets plug-in authors opt into the
  counts they actually honor (or fix the algorithm).
* **Seed determinism** — same ``seed`` must yield byte-identical
  ``samples.json`` output. Different seeds must yield different
  samples.
* **Variable-name / order stability** — the keys of every sample's
  ``values`` dict must match the ``variables_spec`` names *in order*,
  not just as a set. Order stability is what the per-sample measure
  argument lookups downstream depend on.
* **dtype coercion via :func:`osimflow.config.coerce_variable_type`**
  — each sample's value must round-trip through the project's
  canonical YAML→Python coercion helper for its declared type. This
  catches string-from-YAML pollution (``"3.0"`` leaking into a numeric
  column) before the OpenStudio work function crashes.
* **Cache-key reproducibility** — running the algorithm twice with
  the same inputs must produce samples that compare deep-equal after
  ``json.loads``. The cache key (issue #1021) hashes this output, so
  any nondeterminism breaks cache hits across workers.

Design choices (mirrored from the executor suite)
-------------------------------------------------

* **Mixin class, not a single function.** Plug-in authors subclass
  :class:`AlgorithmConformanceSuite` in their test module and override
  :attr:`algorithm_factory` (and optionally :attr:`variables_spec`,
  :attr:`sample_counts`, :attr:`seed`). Pytest discovers the ``test_*``
  methods on the subclass; individual checks can be overridden or
  supplemented without copying the whole suite.
* **Non-pytest runner** :func:`run_algorithm_conformance` returns an
  :class:`AlgorithmConformanceReport` so a ``pre-commit``-style
  one-liner (``python -c "from osimflow.testing import
  run_algorithm_conformance; ..."``) can verify a plug-in without a
  test runner.
* **No pytest-internals coupling.** :class:`AlgorithmConformanceReport`
  is a plain dataclass with :meth:`to_dict`; ``json.dumps`` round-trips
  cleanly for CI consumption.

Quickstart::

    # tests/test_my_algorithm.py
    from osimflow.algorithms import AlgorithmRegistry
    from osimflow.testing import AlgorithmConformanceSuite


    class TestMyAlgorithmConformance(AlgorithmConformanceSuite):
        algorithm_factory = staticmethod(
            lambda: AlgorithmRegistry.get("my_plugin_name")
        )
        # Optional: override the default variables_spec or sample_counts
        # if your algorithm has constraints the in-tree defaults don't cover.
        variables_spec = [
            {"name": "x", "distribution": "uniform",
             "min": 0.0, "max": 1.0, "type": float},
        ]
        sample_counts = (1, 4, 16)
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from osimflow.config import coerce_variable_type

if TYPE_CHECKING:
    from osimflow.algorithms import BaseAlgorithm

# Re-use the executor suite's generic check dataclass — same shape,
# same JSON serialisation, no duplication.
from .executor_conformance import ConformanceCheck  # noqa: PLC0415

# ---------------------------------------------------------------------------
# Default variables_spec — exercises coerce_variable_type across distribution
# families (issue #1565 acceptance criterion).
# ---------------------------------------------------------------------------

#: Default :attr:`AlgorithmConformanceSuite.variables_spec` covering the three
#: distribution families the project supports, so the suite exercises
#: :func:`osimflow.config.coerce_variable_type` end-to-end:
#:
#: * ``uniform`` → ``float``
#: * ``normal`` → ``float``
#: * ``discrete`` (with integer ``values``) → ``int``
#:
#: Plug-in authors whose algorithms cannot represent one of these
#: (e.g. SALib-backed Sobol, which emits floats for every variable)
#: override ``variables_spec`` on the subclass with the spec that
#: matches their actual semantics.
DEFAULT_VARIABLES_SPEC: list[dict[str, Any]] = [
    {"name": "wwr", "distribution": "uniform", "min": 0.2, "max": 0.6, "type": float},
    {"name": "cop", "distribution": "normal", "mean": 3.0, "sigma": 0.5, "type": float},
    {"name": "occupancy", "distribution": "discrete", "values": [10, 20, 30, 40], "type": int},
]

#: Default :attr:`AlgorithmConformanceSuite.sample_counts` — three
#: sample sizes spanning the small/typical/large regime. The power-of-2
#: consumer (Sobol via SALib) overrides this to (1, 4, 16).
DEFAULT_SAMPLE_COUNTS: tuple[int, ...] = (1, 10, 100)

#: Default :attr:`AlgorithmConformanceSuite.seed` for determinism checks.
DEFAULT_SEED: int = 42


# ---------------------------------------------------------------------------
# Report dataclass (mirrors ConformanceReport — separate field name keeps
# the type honest about what it reports on)
# ---------------------------------------------------------------------------


@dataclass
class AlgorithmConformanceReport:
    """Aggregated result from :func:`run_algorithm_conformance`.

    Attributes:
        algorithm_name: ``algorithm.name()`` of the algorithm under test.
        checks: Ordered list of every check that ran.
    """

    algorithm_name: str
    checks: list[ConformanceCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """``True`` if every check passed."""
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> list[ConformanceCheck]:
        """List of checks that failed (empty if all passed)."""
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict for JSON output."""
        return {
            "algorithm": self.algorithm_name,
            "passed": self.passed,
            "n_checks": len(self.checks),
            "n_passed": sum(1 for c in self.checks if c.passed),
            "n_failed": sum(1 for c in self.checks if not c.passed),
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# Programmatic runner (no pytest required) — mirrors run_executor_conformance
# ---------------------------------------------------------------------------


def run_algorithm_conformance(
    algorithm: BaseAlgorithm,
    *,
    variables_spec: list[dict[str, Any]] | None = None,
    sample_counts: tuple[int, ...] = DEFAULT_SAMPLE_COUNTS,
    seed: int = DEFAULT_SEED,
    require_n_samples_exactness: bool = True,
) -> AlgorithmConformanceReport:
    """Run every conformance check against *algorithm* and return a report.

    Non-pytest equivalent of subclassing :class:`AlgorithmConformanceSuite`.
    Intended for plug-in authors who want a single ``python -c`` verification
    flow::

        python -c "from osimflow.testing import run_algorithm_conformance; \\
                  from osimflow.algorithms import AlgorithmRegistry; \\
                  print(run_algorithm_conformance(AlgorithmRegistry.get('lhs')).to_dict())"

    Set ``require_n_samples_exactness=False`` for plug-ins whose
    sample-count contract is "I return a derived count, not the
    requested ``n_samples``" (e.g. Sobol via SALib returns
    ``N*(D+2)`` regardless of input). The cache-key, determinism,
    order-stability, and dtype-coercion checks still run.

    Returns:
        :class:`AlgorithmConformanceReport` with one
        :class:`ConformanceCheck` per contract area.
    """
    report = AlgorithmConformanceReport(algorithm_name=algorithm.name())
    spec = variables_spec if variables_spec is not None else DEFAULT_VARIABLES_SPEC
    variables = {"variables": [_strip_type(v) for v in spec]}

    # Pick a representative n for the per-sample checks. Falls back to
    # the first count when the caller passes a 1-tuple (which is the
    # natural shape for a single-size probe).
    n_repr = sample_counts[1] if len(sample_counts) > 1 else sample_counts[0]

    with _TempOutdir() as tmp_root:
        _check_generate_samples_returns_path(
            algorithm, variables, sample_counts[0], seed, tmp_root, report
        )
        if require_n_samples_exactness:
            _check_n_samples_exactness(algorithm, variables, sample_counts, seed, tmp_root, report)
            _check_samples_json_is_valid(algorithm, variables, n_repr, seed, tmp_root, report)
        else:
            report.checks.append(
                ConformanceCheck(
                    "n_samples_exactness",
                    True,
                    "skipped: require_n_samples_exactness=False "
                    "(algorithm returns a derived count, e.g. Sobol via SALib returns N*(D+2))",
                )
            )
            report.checks.append(
                ConformanceCheck(
                    "samples_json_schema",
                    True,
                    "skipped: require_n_samples_exactness=False "
                    "(per-sample length assertion gated by n_samples_exactness)",
                )
            )
        _check_seed_determinism(algorithm, variables, n_repr, seed, tmp_root, report)
        _check_different_seeds_diverge(algorithm, variables, n_repr, seed, tmp_root, report)
        _check_variable_order_stability(algorithm, spec, n_repr, seed, tmp_root, report)
        _check_dtype_coercion_via_coerce_variable_type(
            algorithm, spec, n_repr, seed, tmp_root, report
        )
        _check_cache_key_reproducibility(algorithm, variables, n_repr, seed, tmp_root, report)
        _check_name_returns_non_empty_string(algorithm, report)
        _check_is_iterative_returns_bool(algorithm, report)
        _check_is_converged_returns_bool(algorithm, report)
        _check_observe_returns_list(algorithm, report)

    return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_type(var: dict[str, Any]) -> dict[str, Any]:
    """Strip the conformance-only ``type`` field before handing to the algorithm.

    The base ``BaseAlgorithm.generate_samples`` contract takes a parsed
    ``variables.yml`` dict — it does not understand a ``type`` field, and
    the in-tree algorithms that read other keys (``min``, ``mean``,
    ``values``, …) ignore it. Keeping the field in the spec lets the
    conformance suite assert dtype per variable without polluting the
    algorithm contract.
    """
    return {k: v for k, v in var.items() if k != "type"}


class _TempOutdir:
    """Tiny RAII helper for runner checks — clean up between checks."""

    def __init__(self) -> None:
        import tempfile  # noqa: PLC0415

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def __enter__(self) -> Path:
        return self.root

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


def _run_one(
    algorithm: BaseAlgorithm,
    variables: dict[str, Any],
    n_samples: int,
    seed: int,
    tmp_root: Path,
    *,
    sub: str,
) -> Path:
    """Invoke ``generate_samples`` and return the ``samples.json`` path."""
    outdir = tmp_root / sub
    outdir.mkdir(parents=True, exist_ok=True)
    samples_path = algorithm.generate_samples(variables, n_samples, seed, outdir)
    return Path(samples_path)


def _load_samples(path: Path) -> list[dict[str, Any]]:
    """Load and return the ``samples`` list from a ``samples.json`` file."""
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or "samples" not in payload:
        raise ValueError(f"samples.json at {path} missing top-level 'samples' key")
    samples = payload["samples"]
    if not isinstance(samples, list):
        raise ValueError(f"samples.json at {path} has non-list 'samples'")
    return samples


# ---------------------------------------------------------------------------
# Individual checks (shared by runner and pytest mixin)
# ---------------------------------------------------------------------------


def _check_generate_samples_returns_path(
    algorithm: BaseAlgorithm,
    variables: dict[str, Any],
    n_samples: int,
    seed: int,
    tmp_root: Path,
    report: AlgorithmConformanceReport,
) -> None:
    """``generate_samples`` returns a :class:`Path` to a real file."""
    try:
        samples_path = _run_one(algorithm, variables, n_samples, seed, tmp_root, sub="returns_path")
        ok = isinstance(samples_path, Path) and samples_path.is_file()
        detail = (
            f"returned {samples_path!s} (exists={samples_path.is_file()})"
            if ok
            else f"expected Path to existing file, got {type(samples_path).__name__} "
            f"at {samples_path!s}"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("generate_samples_returns_path", ok, detail))


def _check_n_samples_exactness(
    algorithm: BaseAlgorithm,
    variables: dict[str, Any],
    sample_counts: tuple[int, ...],
    seed: int,
    tmp_root: Path,
    report: AlgorithmConformanceReport,
) -> None:
    """``len(samples) == n_samples`` for every count in *sample_counts*."""
    try:
        mismatches: list[str] = []
        for n in sample_counts:
            samples_path = _run_one(algorithm, variables, n, seed, tmp_root, sub=f"n_samples_{n}")
            samples = _load_samples(samples_path)
            if len(samples) != n:
                mismatches.append(f"n={n} -> len(samples)={len(samples)}")
        ok = not mismatches
        detail = (
            f"counts={list(sample_counts)} satisfied"
            if ok
            else "count drift: " + "; ".join(mismatches)
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("n_samples_exactness", ok, detail))


def _check_seed_determinism(
    algorithm: BaseAlgorithm,
    variables: dict[str, Any],
    n_samples: int,
    seed: int,
    tmp_root: Path,
    report: AlgorithmConformanceReport,
) -> None:
    """Same seed twice → byte-identical ``samples.json``."""
    try:
        p1 = _run_one(algorithm, variables, n_samples, seed, tmp_root, sub="seed_a")
        p2 = _run_one(algorithm, variables, n_samples, seed, tmp_root, sub="seed_b")
        h1 = hashlib.sha256(p1.read_bytes()).hexdigest()
        h2 = hashlib.sha256(p2.read_bytes()).hexdigest()
        ok = h1 == h2
        detail = f"sha256_a={h1[:12]}… sha256_b={h2[:12]}… (n={n_samples}, seed={seed})"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("seed_determinism", ok, detail))


def _check_different_seeds_diverge(
    algorithm: BaseAlgorithm,
    variables: dict[str, Any],
    n_samples: int,
    seed: int,
    tmp_root: Path,
    report: AlgorithmConformanceReport,
) -> None:
    """Different seeds must yield different samples (spot-check)."""
    try:
        p_a = _run_one(algorithm, variables, n_samples, seed, tmp_root, sub="diff_seed_a")
        p_b = _run_one(algorithm, variables, n_samples, seed + 1, tmp_root, sub="diff_seed_b")
        s_a = _load_samples(p_a)
        s_b = _load_samples(p_b)
        ok = s_a != s_b
        detail = (
            f"seed={seed} vs seed={seed + 1} diverged (n={n_samples})"
            if ok
            else f"seed={seed} and seed={seed + 1} produced identical samples (n={n_samples})"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("different_seeds_diverge", ok, detail))


def _check_variable_order_stability(
    algorithm: BaseAlgorithm,
    spec: list[dict[str, Any]],
    n_samples: int,
    seed: int,
    tmp_root: Path,
    report: AlgorithmConformanceReport,
) -> None:
    """Every sample's ``values`` dict preserves ``variables_spec`` order."""
    expected_order = [str(v["name"]) for v in spec]
    try:
        samples_path = _run_one(
            algorithm,
            {"variables": [_strip_type(v) for v in spec]},
            n_samples,
            seed,
            tmp_root,
            sub="var_order",
        )
        samples = _load_samples(samples_path)
        ok = True
        bad_sample = ""
        bad_order: list[str] = []
        for s in samples:
            values = s.get("values")
            if not isinstance(values, dict):
                ok = False
                bad_sample = str(s.get("sample_id", "?"))
                bad_order = ["<not a dict>"]
                break
            keys = list(values.keys())
            if keys != expected_order:
                ok = False
                bad_sample = str(s.get("sample_id", "?"))
                bad_order = keys
                break
        detail = (
            f"all {len(samples)} samples have keys in order {expected_order}"
            if ok
            else f"sample {bad_sample} has order {bad_order} (expected {expected_order})"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("variable_order_stability", ok, detail))


def _check_dtype_coercion_via_coerce_variable_type(
    algorithm: BaseAlgorithm,
    spec: list[dict[str, Any]],
    n_samples: int,
    seed: int,
    tmp_root: Path,
    report: AlgorithmConformanceReport,
) -> None:
    """Every sample's value round-trips through ``coerce_variable_type``."""
    try:
        samples_path = _run_one(
            algorithm,
            {"variables": [_strip_type(v) for v in spec]},
            n_samples,
            seed,
            tmp_root,
            sub="dtype",
        )
        samples = _load_samples(samples_path)
        type_by_name = {str(v["name"]): v.get("type", float) for v in spec}
        mismatches: list[str] = []
        for s in samples:
            values = s.get("values", {})
            sid = str(s.get("sample_id", "?"))
            for name, value in values.items():
                expected_type = type_by_name.get(name, float)
                try:
                    coerced = coerce_variable_type(value, expected_type)
                except (ValueError, TypeError) as exc:
                    mismatches.append(
                        f"sample {sid} var {name}: coerce({value!r}, {expected_type!r}) "
                        f"raised {type(exc).__name__}: {exc}"
                    )
                    continue
                # The point of coerce_variable_type is that an already-typed
                # value passes through unchanged; assert that. We accept
                # int-coerced-to-int even if the algorithm emitted 10.0 (a
                # common float-but-integer case) — that is a legitimate
                # coercion, not a contract violation.
                if coerced != value and not _lossless_int_coercion(value, expected_type):
                    mismatches.append(
                        f"sample {sid} var {name}: coerce({value!r}, {expected_type!r}) "
                        f"produced {coerced!r}"
                    )
        ok = not mismatches
        detail = (
            f"all {len(samples)} samples pass coerce_variable_type for {len(spec)} vars"
            if ok
            else "dtype drift: " + "; ".join(mismatches[:3]) + (" …" if len(mismatches) > 3 else "")
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("dtype_coercion", ok, detail))


def _lossless_int_coercion(value: Any, expected_type: Any) -> bool:
    """True when a ``float`` like ``10.0`` is a lossless ``int`` coercion."""
    if expected_type is not int:
        return False
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return float(value) == int(float(value))


def _check_cache_key_reproducibility(
    algorithm: BaseAlgorithm,
    variables: dict[str, Any],
    n_samples: int,
    seed: int,
    tmp_root: Path,
    report: AlgorithmConformanceReport,
) -> None:
    """Re-running with the same inputs produces deep-equal samples.

    Cache keys (issue #1021) hash ``samples.json``; any nondeterminism
    breaks cross-worker cache hits. We compare both the byte-level
    hash and the parsed JSON to catch ordering anomalies that would
    still hash differently even when semantically equivalent.
    """
    try:
        p1 = _run_one(algorithm, variables, n_samples, seed, tmp_root, sub="cache_a")
        p2 = _run_one(algorithm, variables, n_samples, seed, tmp_root, sub="cache_b")
        h1 = hashlib.sha256(p1.read_bytes()).hexdigest()
        h2 = hashlib.sha256(p2.read_bytes()).hexdigest()
        s1 = _load_samples(p1)
        s2 = _load_samples(p2)
        ok = h1 == h2 and s1 == s2
        detail = (
            f"byte-equal ({h1[:12]}…) and deep-equal across {len(s1)} samples "
            f"(n={n_samples}, seed={seed})"
            if ok
            else f"sha256_a={h1[:12]}… sha256_b={h2[:12]}… deep_equal={s1 == s2}"
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("cache_key_reproducibility", ok, detail))


def _check_samples_json_is_valid(
    algorithm: BaseAlgorithm,
    variables: dict[str, Any],
    n_samples: int,
    seed: int,
    tmp_root: Path,
    report: AlgorithmConformanceReport,
) -> None:
    """``samples.json`` is JSON, has the documented schema."""
    try:
        samples_path = _run_one(algorithm, variables, n_samples, seed, tmp_root, sub="schema")
        payload = json.loads(samples_path.read_text())
        samples = payload["samples"]
        bad: list[str] = []
        for i, s in enumerate(samples):
            if not isinstance(s, dict):
                bad.append(f"sample[{i}] not a dict")
                continue
            if "sample_id" not in s or not isinstance(s["sample_id"], str):
                bad.append(f"sample[{i}] missing str sample_id")
            if "values" not in s or not isinstance(s["values"], dict):
                bad.append(f"sample[{i}] missing dict values")
        ok = not bad
        detail = (
            f"all {len(samples)} samples have {{sample_id, values}} schema"
            if ok
            else "; ".join(bad[:3])
        )
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("samples_json_schema", ok, detail))


def _check_name_returns_non_empty_string(
    algorithm: BaseAlgorithm, report: AlgorithmConformanceReport
) -> None:
    """``algorithm.name()`` returns a non-empty string (entry-point discovery key)."""
    try:
        name = algorithm.name()
        ok = isinstance(name, str) and bool(name)
        detail = f"name()={name!r}"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("name_returns_non_empty_string", ok, detail))


def _check_is_iterative_returns_bool(
    algorithm: BaseAlgorithm, report: AlgorithmConformanceReport
) -> None:
    """``algorithm.is_iterative()`` returns a ``bool`` (Campaign dispatch key)."""
    try:
        flag = algorithm.is_iterative()
        ok = isinstance(flag, bool)
        detail = f"is_iterative()={flag!r}"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("is_iterative_returns_bool", ok, detail))


def _check_is_converged_returns_bool(
    algorithm: BaseAlgorithm, report: AlgorithmConformanceReport
) -> None:
    """``algorithm.is_converged([])`` returns a ``bool``."""
    try:
        flag = algorithm.is_converged([])
        ok = isinstance(flag, bool)
        detail = f"is_converged([])={flag!r}"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("is_converged_returns_bool", ok, detail))


def _check_observe_returns_list(
    algorithm: BaseAlgorithm, report: AlgorithmConformanceReport
) -> None:
    """``algorithm.observe([])`` returns a ``list`` (single-shot no-op contract)."""
    try:
        result = algorithm.observe([])
        ok = isinstance(result, list)
        detail = f"observe([]) returned {type(result).__name__} (len={len(result)})"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    report.checks.append(ConformanceCheck("observe_returns_list", ok, detail))


# ---------------------------------------------------------------------------
# Pytest mixin suite — mirrors ExecutorConformanceSuite
# ---------------------------------------------------------------------------


class AlgorithmConformanceSuite:
    """Mixin pytest suite for algorithm plug-in conformance (issue #1565).

    Subclass this in your plug-in's test module and override
    :attr:`algorithm_factory` (and optionally :attr:`variables_spec`,
    :attr:`sample_counts`, :attr:`seed`). Every ``test_*`` method on the
    mixin will be discovered by pytest and run against the algorithm
    your factory returns.

    Example::

        # tests/test_my_algorithm.py
        from osimflow.algorithms import AlgorithmRegistry
        from osimflow.testing import AlgorithmConformanceSuite


        class TestMyAlgorithmConformance(AlgorithmConformanceSuite):
            algorithm_factory = staticmethod(
                lambda: AlgorithmRegistry.get("my_plugin_name")
            )

    Required overrides:
        ``algorithm_factory``: Zero-argument callable returning a fresh
        :class:`~osimflow.algorithms.BaseAlgorithm` instance. The suite
        calls ``algorithm_factory()`` once per test, so any per-test
        state (population for iterative algorithms, etc.) is reset
        between checks.

    Optional overrides:
        ``variables_spec``: Variable spec the conformance checks run
        against. Defaults to :data:`DEFAULT_VARIABLES_SPEC`, which
        covers ``uniform`` / ``normal`` / ``discrete`` (int). Override
        when your algorithm cannot represent one of those — e.g.
        Sobol (via SALib) emits floats for every variable, so its
        subclass should pass a continuous-only spec.
        ``sample_counts``: ``tuple[int, ...]`` of ``n_samples`` values
        the ``n_samples_exactness`` check asserts against. Defaults to
        ``(1, 10, 100)``. Power-of-2 algorithms should override to
        ``(1, 4, 16)`` (or whichever power-of-2 sizes they honor).
        ``seed``: Seed for the determinism and reproducibility checks.
        Defaults to ``42``.

    Notes for plug-in authors
    -------------------------
    * Each test calls ``algorithm_factory()`` independently, so any
      iterative algorithm's population / convergence state is fresh
      per check.
    * The suite is a *mixin*: pytest requires a concrete class on disk,
      so you must subclass it (you cannot use it directly).
    * For non-pytest usage see :func:`run_algorithm_conformance`,
      which returns an :class:`AlgorithmConformanceReport` instead of
      raising.
    """

    # ---- Required overrides ----
    algorithm_factory: ClassVar[Callable[..., BaseAlgorithm]]

    # ---- Optional overrides ----
    variables_spec: ClassVar[list[dict[str, Any]]] = DEFAULT_VARIABLES_SPEC
    sample_counts: ClassVar[tuple[int, ...]] = DEFAULT_SAMPLE_COUNTS
    seed: ClassVar[int] = DEFAULT_SEED
    #: Plug-in algorithms whose sample-count contract is "I return a
    #: derived count, not the requested ``n_samples``" (e.g. Sobol via
    #: SALib, which returns ``N*(D+2)`` regardless of input) set this to
    #: ``False`` to skip the exactness checks. The cache-key and
    #: determinism checks still run.
    require_n_samples_exactness: ClassVar[bool] = True

    # ---- Fixtures ----

    @pytest.fixture
    def conformance_algorithm(self) -> BaseAlgorithm:
        """Fresh algorithm instance per test (population / state reset)."""
        return self.algorithm_factory()

    @pytest.fixture
    def variables_for_algo(self) -> dict[str, Any]:
        """Parsed ``variables.yml`` dict the algorithm consumes."""
        return {"variables": [_strip_type(v) for v in self.variables_spec]}

    @pytest.fixture
    def repr_n_samples(self) -> int:
        """``n_samples`` value used by the per-sample checks.

        Falls back to the first entry of :attr:`sample_counts` when the
        caller passes a 1-tuple, so single-size probes work without
        index gymnastics.
        """
        return self.sample_counts[1] if len(self.sample_counts) > 1 else self.sample_counts[0]

    # ---- File / Path contract ----

    def test_generate_samples_returns_existing_path(
        self,
        conformance_algorithm: BaseAlgorithm,
        variables_for_algo: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """``generate_samples`` returns a :class:`Path` to a real file."""
        out = tmp_path / "returns_path"
        samples_path = conformance_algorithm.generate_samples(
            variables_for_algo, self.sample_counts[0], self.seed, out
        )
        assert isinstance(samples_path, Path), f"expected Path, got {type(samples_path).__name__}"
        assert samples_path.is_file(), f"samples.json missing at {samples_path}"

    def test_samples_json_has_documented_schema(
        self,
        conformance_algorithm: BaseAlgorithm,
        variables_for_algo: dict[str, Any],
        repr_n_samples: int,
        tmp_path: Path,
    ) -> None:
        """``samples.json`` has the ``{"samples": [{"sample_id", "values"}, ...]}`` schema."""
        if not self.require_n_samples_exactness:
            pytest.skip(
                "require_n_samples_exactness=False on this subclass; "
                "sample-count-dependent assertions skipped"
            )
        out = tmp_path / "schema"
        samples_path = conformance_algorithm.generate_samples(
            variables_for_algo, repr_n_samples, self.seed, out
        )
        payload = json.loads(samples_path.read_text())
        assert isinstance(payload, dict), "samples.json must be a JSON object"
        assert "samples" in payload, "samples.json missing top-level 'samples' key"
        samples = payload["samples"]
        assert isinstance(samples, list), "'samples' must be a list"
        assert len(samples) == repr_n_samples, (
            f"expected {repr_n_samples} samples, got {len(samples)}"
        )
        for i, s in enumerate(samples):
            assert isinstance(s, dict), f"sample[{i}] not a dict"
            assert "sample_id" in s and isinstance(s["sample_id"], str), (
                f"sample[{i}] missing str sample_id"
            )
            assert "values" in s and isinstance(s["values"], dict), (
                f"sample[{i}] missing dict values"
            )

    # ---- Sample-count exactness ----

    def test_n_samples_exactness(
        self,
        conformance_algorithm: BaseAlgorithm,
        variables_for_algo: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """``len(samples) == n_samples`` for every count in ``sample_counts``."""
        if not self.require_n_samples_exactness:
            pytest.skip(
                "require_n_samples_exactness=False on this subclass; "
                "algorithm returns a derived count (e.g. Sobol via SALib returns N*(D+2))"
            )
        for n in self.sample_counts:
            out = tmp_path / f"n_{n}"
            samples_path = conformance_algorithm.generate_samples(
                variables_for_algo, n, self.seed, out
            )
            samples = _load_samples(samples_path)
            assert len(samples) == n, f"n_samples={n}: algorithm returned {len(samples)} samples"

    # ---- Seed determinism ----

    def test_seed_determinism(
        self,
        conformance_algorithm: BaseAlgorithm,
        variables_for_algo: dict[str, Any],
        repr_n_samples: int,
        tmp_path: Path,
    ) -> None:
        """Same seed twice → byte-identical ``samples.json`` (cache-key contract)."""
        out_a = tmp_path / "det_a"
        out_b = tmp_path / "det_b"
        p_a = conformance_algorithm.generate_samples(
            variables_for_algo, repr_n_samples, self.seed, out_a
        )
        p_b = conformance_algorithm.generate_samples(
            variables_for_algo, repr_n_samples, self.seed, out_b
        )
        h_a = hashlib.sha256(p_a.read_bytes()).hexdigest()
        h_b = hashlib.sha256(p_b.read_bytes()).hexdigest()
        assert h_a == h_b, f"sha256 drift under fixed seed: {h_a[:12]}… vs {h_b[:12]}…"

    def test_different_seeds_diverge(
        self,
        conformance_algorithm: BaseAlgorithm,
        variables_for_algo: dict[str, Any],
        repr_n_samples: int,
        tmp_path: Path,
    ) -> None:
        """Different seeds → different samples (regression guard for trivially-constant algorithms)."""
        out_a = tmp_path / "div_a"
        out_b = tmp_path / "div_b"
        p_a = conformance_algorithm.generate_samples(
            variables_for_algo, repr_n_samples, self.seed, out_a
        )
        p_b = conformance_algorithm.generate_samples(
            variables_for_algo, repr_n_samples, self.seed + 1, out_b
        )
        s_a = _load_samples(p_a)
        s_b = _load_samples(p_b)
        assert s_a != s_b, f"seed={self.seed} and seed={self.seed + 1} produced identical samples"

    # ---- Variable-name / order stability ----

    def test_variable_order_stability(
        self,
        conformance_algorithm: BaseAlgorithm,
        variables_for_algo: dict[str, Any],
        repr_n_samples: int,
        tmp_path: Path,
    ) -> None:
        """Every sample's ``values`` dict preserves ``variables_spec`` name order."""
        expected_order = [str(v["name"]) for v in self.variables_spec]
        out = tmp_path / "order"
        samples_path = conformance_algorithm.generate_samples(
            variables_for_algo, repr_n_samples, self.seed, out
        )
        samples = _load_samples(samples_path)
        for s in samples:
            values = s["values"]
            assert isinstance(values, dict), f"sample {s.get('sample_id')}: values not a dict"
            keys = list(values.keys())
            assert keys == expected_order, (
                f"sample {s.get('sample_id')}: keys={keys} (expected {expected_order})"
            )

    # ---- dtype coercion via coerce_variable_type ----

    def test_dtype_coercion_via_coerce_variable_type(
        self,
        conformance_algorithm: BaseAlgorithm,
        variables_for_algo: dict[str, Any],
        repr_n_samples: int,
        tmp_path: Path,
    ) -> None:
        """Each sample's value passes through ``coerce_variable_type`` unchanged."""
        out = tmp_path / "dtype"
        samples_path = conformance_algorithm.generate_samples(
            variables_for_algo, repr_n_samples, self.seed, out
        )
        samples = _load_samples(samples_path)
        type_by_name = {str(v["name"]): v.get("type", float) for v in self.variables_spec}
        for s in samples:
            values = s["values"]
            sid = s.get("sample_id", "?")
            for name, value in values.items():
                expected_type = type_by_name.get(name, float)
                coerced = coerce_variable_type(value, expected_type)
                assert coerced == value or _lossless_int_coercion(value, expected_type), (
                    f"sample {sid} var {name}: coerce({value!r}, {expected_type!r}) "
                    f"produced {coerced!r}"
                )

    # ---- Cache-key reproducibility ----

    def test_cache_key_reproducibility(
        self,
        conformance_algorithm: BaseAlgorithm,
        variables_for_algo: dict[str, Any],
        repr_n_samples: int,
        tmp_path: Path,
    ) -> None:
        """Re-running with the same inputs produces deep-equal samples."""
        out_a = tmp_path / "cache_a"
        out_b = tmp_path / "cache_b"
        p_a = conformance_algorithm.generate_samples(
            variables_for_algo, repr_n_samples, self.seed, out_a
        )
        p_b = conformance_algorithm.generate_samples(
            variables_for_algo, repr_n_samples, self.seed, out_b
        )
        h_a = hashlib.sha256(p_a.read_bytes()).hexdigest()
        h_b = hashlib.sha256(p_b.read_bytes()).hexdigest()
        s_a = _load_samples(p_a)
        s_b = _load_samples(p_b)
        assert h_a == h_b, f"byte-level drift under fixed seed: {h_a[:12]}… vs {h_b[:12]}…"
        assert s_a == s_b, "parsed samples.json drift under fixed seed"

    # ---- Base-class dispatch contract (Campaign integration surface) ----

    def test_name_returns_non_empty_string(self, conformance_algorithm: BaseAlgorithm) -> None:
        """``algorithm.name()`` returns a non-empty string."""
        name = conformance_algorithm.name()
        assert isinstance(name, str), f"name() must return str, got {type(name).__name__}"
        assert name, "name() must be non-empty"

    def test_is_iterative_returns_bool(self, conformance_algorithm: BaseAlgorithm) -> None:
        """``is_iterative()`` returns a ``bool``."""
        flag = conformance_algorithm.is_iterative()
        assert isinstance(flag, bool), f"is_iterative() must return bool, got {type(flag).__name__}"

    def test_is_converged_returns_bool(self, conformance_algorithm: BaseAlgorithm) -> None:
        """``is_converged([])`` returns a ``bool``."""
        flag = conformance_algorithm.is_converged([])
        assert isinstance(flag, bool), f"is_converged() must return bool, got {type(flag).__name__}"

    def test_observe_empty_history_returns_list(self, conformance_algorithm: BaseAlgorithm) -> None:
        """``observe([])`` returns a ``list`` (single-shot no-op contract)."""
        result = conformance_algorithm.observe([])
        assert isinstance(result, list), (
            f"observe([]) must return list, got {type(result).__name__}"
        )
