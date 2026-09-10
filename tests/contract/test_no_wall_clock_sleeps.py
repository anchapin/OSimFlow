"""Contract guard: no wall-clock sleeps in merge-gated tests (issue #1544).

Issue #1481 injected a controllable clock into ``CircuitBreaker`` tests;
issue #1544 propagates the pattern and locks it in. Timing-assumption
sleeps (``time.sleep(d)`` / ``await asyncio.sleep(d)`` followed by an
assertion that the world advanced) are the classic source of flakes under
loaded CI runners with ``-n 2 --dist loadgroup``.

This guard scans every pytest-collected test file under ``tests/`` for
direct ``time.sleep(...)`` / ``asyncio.sleep(...)`` call sites (found via
``ast`` — string literals such as BYOS fixture scripts and
``patch("...time.sleep")`` targets are never flagged) and fails when a
new one appears outside the documented exemption list below.

Issue #1693 adds a sibling guard against **sub-second wall-clock
upper-bound assertions** (``assert elapsed < 0.05`` and similar).  These
flake for the same reason: a 50 ms GC pause or scheduler preemption on a
shared 2-core runner fails the assert despite the code being correct.
The sibling guard detects ``assert X < T`` / ``assert X <= T`` patterns
where ``T`` is a numeric literal strictly below 0.1 s, mirroring the
existing per-file sleep-budget mechanism.

The sanctioned alternatives (see the fixed tests for examples):

* ``threading.Event`` set from a wrapper/mock hook — wait for the exact
  observable (render completed, 3rd heartbeat, 2nd mocked backoff) with
  a generous failure-bound timeout.
* Controllable clocks (the #1481 ``FakeClock`` pattern) — ``patch`` the
  module's ``time`` reference or ``time.monotonic``/``time.time`` and
  advance the clock instead of waiting.
* Structural assertions on state transitions / mock call counts when
  the property under test is "did this dispatch the side effect?" rather
  than "how fast did it return?".
* Join the thread / await mock call counts.

Out of scope by design
----------------------
* ``tests/contract/`` — the merge gate runs with ``--ignore=tests/contract``
  (see ``PYTEST_CI_FLAGS`` in the Makefile).
* ``nomad_e2e/`` — deselected by ``-m "not nomad_e2e"``.
* ``tests/integration/test_observability_real_sinks.py`` — intentionally
  real-substrate suite, excluded per issue #1544's acceptance criteria.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = _REPO_ROOT / "tests"

# Files excluded entirely, keyed by path relative to the repo root.
# Every entry needs a justification comment referencing an issue.
_EXEMPT_FILES: frozenset[str] = frozenset(
    {
        # Real-substrate observability suite, named exclusion in issue #1544.
        "tests/integration/test_observability_real_sinks.py",
    }
)

# Directory prefixes excluded entirely (not collected / deselected by the
# merge gate's PYTEST_CI_FLAGS).
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "tests/contract/",  # gate runs with --ignore=tests/contract
    "tests/integration/nomad_e2e/",  # gate runs with -m "not nomad_e2e"
)

# Per-file budget of allowed literal sleep calls, keyed by path relative
# to the repo root. Existing sleeps must carry an inline justification in
# the source file; NEW sleeps exceed the budget and fail this guard, so
# shrink the budget when you remove one.
_EXEMPT_MAX_CALLS: dict[str, int] = {
    # 4 deadline-bounded condition-poll loops from issue #1389: each
    # ``time.sleep`` re-checks an externally observable condition
    # (fakeredis ``PUBSUB NUMSUB`` or ``worker_b.stats()``) inside a
    # ``time.monotonic()`` deadline loop — the test fails on timeout, not
    # on an elapsed-time assumption, and the SUBSCRIBE-confirmation wait
    # has no synchronizable hook without refactoring osimflow internals.
    "tests/integration/test_distributed_cache_invalidation.py": 4,
    # 1 deadline-bounded join wait from issue #1538: the bounded-drain
    # integration test waits for the (real) fan-out threads to observe
    # the cancel flag inside a ``time.monotonic()`` deadline loop — the
    # observable is genuine thread scheduling, which has no
    # synchronizable hook without patching the scheduler under test.
    "tests/integration/test_cancel_substrate_jobs.py": 1,
    # 3 short park-and-release setups from issue #1538: the
    # executor-cancel unit tests park a stub job in ``result()`` so the
    # cancel sweep has something live to kill; each ``time.sleep`` keeps
    # the stub parked (and is interrupted by the kill), not asserting on
    # elapsed wall-clock.
    "tests/unit/test_executor_cancel.py": 3,
}

# Per-file budget of allowed sub-second wall-clock upper-bound assertions
# (issue #1693).  Keyed by path relative to the repo root; a default of
# ``0`` applies to unlisted files.  Existing assertions must carry an
# inline justification in the source file; NEW assertions exceed the
# budget and fail this guard, so shrink the budget when you remove one.
_EXEMPT_WALL_CLOCK_ASSERTS: dict[str, int] = {
    # Empty by design: issue #1693 removed the only sub-second wall-clock
    # upper-bound assertion in the merge-gated suite (the
    # ``elapsed < 0.05`` flake at tests/unit/test_distributed_jobqueue.py
    # had no structural replacement).  Any future per-file budget entry
    # needs an issue-referenced justification, mirroring
    # ``_EXEMPT_MAX_CALLS`` above.
}

_SLEEP_MODULES = frozenset({"time", "asyncio"})

# Sub-second threshold (seconds) below which a wall-clock upper-bound
# assert is treated as a flake risk under loaded CI runners (issue #1693).
# ``0.1`` seconds = 100 ms.
_WALL_CLOCK_THRESHOLD_S: float = 0.1

# Substrings (case-insensitive) that mark a name as wall-clock-derived.
# Used by ``_wall_clock_upper_bound_asserts`` to filter out false positives
# like ``assert abs(...) < 1e-6`` (numerical tolerance) and
# ``assert sobol_disc <= 0.01`` (Sobol discrepancy bound) where the
# literal-threshold comparison is unrelated to wall-clock measurement.
# Keep entries broad enough to cover the canonical timer variable names
# used in this repo: ``elapsed``, ``elapsed_s``, ``wall_time``,
# ``deadline_start``, ``duration``, ``latency``, ``response_time``,
# ``dt_s`` — issue #1693.
_TIME_NAME_HINTS: tuple[str, ...] = (
    "elapsed",
    "duration",
    "wall",
    "took",
    "runtime",
    "delay",
    "latency",
    "response",
    "_time",
    "time_",
    "_dt",
    "dt_",
    "deadline",
)


def _looks_like_time_name(name: str) -> bool:
    """Return ``True`` if ``name`` (the LHS identifier) reads as a wall-clock var.

    Match is case-insensitive substring against ``_TIME_NAME_HINTS``.
    Pure numerical-tolerance assertions (e.g. ``sobol_disc``,
    ``discrepancy``, ``hv``, ``mean``) intentionally do NOT match any
    hint so they bypass the detector.
    """
    lowered = name.lower()
    return any(hint in lowered for hint in _TIME_NAME_HINTS)


def _literal_sleep_lines(path: Path) -> list[int]:
    """Return line numbers of direct ``time.sleep``/``asyncio.sleep`` calls.

    Uses the AST, so sleeps inside string literals (BYOS fixture
    scripts), docstrings, comments, and ``patch("...time.sleep")``
    targets are never false-flagged.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sleep"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in _SLEEP_MODULES
        ):
            continue
        lines.append(node.lineno)
    return sorted(lines)


def _wall_clock_upper_bound_asserts(path: Path) -> list[tuple[int, str]]:
    """Return ``(line, threshold_repr)`` for sub-second upper-bound assertions.

    Detects ``assert X < T`` / ``assert X <= T`` patterns where ``T`` is
    a positive numeric literal strictly below ``_WALL_CLOCK_THRESHOLD_S``
    seconds (default 100 ms — issue #1693).  The LHS must be a plain
    ``ast.Name`` whose identifier matches a wall-clock naming hint from
    ``_TIME_NAME_HINTS`` — this filters out numerical-tolerance
    assertions like ``assert abs(...) < 1e-6`` (the LHS is a ``Call``)
    and ``assert sobol_disc <= 0.01`` (the LHS name has no time hint).

    Uses the AST so asserts inside string literals (BYOS fixture
    scripts), docstrings, and comments are never false-flagged.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        if len(test.ops) != 1 or len(test.comparators) != 1:
            continue
        op = test.ops[0]
        if not isinstance(op, (ast.Lt, ast.LtE)):
            continue
        rhs = test.comparators[0]
        if not isinstance(rhs, ast.Constant) or not isinstance(rhs.value, (int, float)):
            continue
        threshold = rhs.value
        # Only positive, strictly sub-threshold bounds count.  ``0.1``
        # itself is allowed; ``0`` is degenerate; negative literals are
        # never upper bounds.
        if not (0.0 < threshold < _WALL_CLOCK_THRESHOLD_S):
            continue
        # LHS must be a plain name (filters out ``abs(...)`` calls and
        # ``X - expected`` subtractions used in pure-math tolerance asserts)
        # AND the name must look like a wall-clock variable so we don't
        # mis-flag numerical- discrepancy/ hypervolume / cost-tolerance
        # asserts.
        lhs = test.left
        if not isinstance(lhs, ast.Name):
            continue
        if not _looks_like_time_name(lhs.id):
            continue
        results.append((node.lineno, repr(threshold)))
    return sorted(results)


def _collected_test_files() -> list[Path]:
    # Mirrors pyproject.toml [tool.pytest.ini_options] python_files.
    return sorted(p for p in _TESTS_DIR.rglob("test_*.py") if p.is_file())


def test_no_wall_clock_sleeps_in_merge_gated_tests() -> None:
    offenders: list[str] = []

    for path in _collected_test_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _EXEMPT_FILES:
            continue
        if rel.startswith(_EXEMPT_PREFIXES):
            continue
        allowed = _EXEMPT_MAX_CALLS.get(rel, 0)
        lines = _literal_sleep_lines(path)
        if len(lines) > allowed:
            offenders.append(f"{rel}: lines {lines} ({len(lines)} > {allowed} allowed)")

    assert not offenders, (
        "Merge-gated tests must not contain literal time.sleep/asyncio.sleep "
        "calls — they flake under loaded CI runners (issue #1544).\n"
        "Replace them with deterministic synchronization:\n"
        "  - threading.Event set from a wrapper/mock hook (wait for the "
        "observable, not the clock)\n"
        "  - a controllable clock patched over the module's `time` reference "
        "(the #1481 FakeClock pattern)\n"
        "  - joining the background thread / awaiting mock call counts\n"
        f"Offenders:\n  {'\n  '.join(offenders)}\n"
        "If a sleep is genuinely irreducible, add a per-file budget entry to "
        "_EXEMPT_MAX_CALLS with an issue-referenced justification."
    )


def test_no_subsecond_wall_clock_upper_bounds_in_merge_gated_tests() -> None:
    """Sub-second (``<100 ms``) wall-clock upper-bound asserts are flake risks.

    A ``50 ms`` GC pause or scheduler preemption on a shared 2-core
    runner under ``-n 2 --dist loadgroup`` is routine and can fail a
    tight ``assert elapsed < 0.05`` even when the production code is
    correct (issue #1693).  Detect new sub-second literal-threshold
    upper-bound assertions and fail the guard unless the file has a
    per-file budget entry in ``_EXEMPT_WALL_CLOCK_ASSERTS``.
    """
    offenders: list[str] = []

    for path in _collected_test_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _EXEMPT_FILES:
            continue
        if rel.startswith(_EXEMPT_PREFIXES):
            continue
        allowed = _EXEMPT_WALL_CLOCK_ASSERTS.get(rel, 0)
        hits = _wall_clock_upper_bound_asserts(path)
        if len(hits) > allowed:
            offenders.append(f"{rel}: lines {hits} ({len(hits)} > {allowed} allowed)")

    assert not offenders, (
        f"Merge-gated tests must not assert sub-second (<{int(_WALL_CLOCK_THRESHOLD_S * 1000)} ms) "
        "wall-clock upper bounds — they flake under loaded CI runners "
        "(issue #1693).\n"
        "Replace with structural checks (state transitions, mock call "
        "counts, controllable-clock patterns from issue #1481) instead "
        "of wall-clock measurements.\n"
        f"Offenders:\n  {'\n  '.join(offenders)}\n"
        "If an upper bound is genuinely irreducible, add a per-file "
        "budget entry to `_EXEMPT_WALL_CLOCK_ASSERTS` with an "
        "issue-referenced justification."
    )


def test_exemption_list_has_no_stale_entries() -> None:
    """Exempted files must actually exist and use at most their budget.

    Keeps the exemption list honest: removing sleeps from an exempt file
    should also shrink (or drop) its budget entry, and deleted files must
    not linger in the list.
    """
    for rel in _EXEMPT_MAX_CALLS:
        path = _REPO_ROOT / rel
        assert path.is_file(), f"exempted test file no longer exists: {rel}"
    for rel in _EXEMPT_WALL_CLOCK_ASSERTS:
        path = _REPO_ROOT / rel
        assert path.is_file(), f"exempted test file no longer exists: {rel}"
    for rel in _EXEMPT_FILES:
        assert (_REPO_ROOT / rel).is_file(), f"exempted test file no longer exists: {rel}"
