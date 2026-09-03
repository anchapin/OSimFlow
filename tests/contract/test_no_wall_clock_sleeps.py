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

The sanctioned alternatives (see the fixed tests for examples):

* ``threading.Event`` set from a wrapper/mock hook — wait for the exact
  observable (render completed, 3rd heartbeat, 2nd mocked backoff) with
  a generous failure-bound timeout.
* Controllable clocks (the #1481 ``FakeClock`` pattern) — ``patch`` the
  module's ``time`` reference or ``time.monotonic``/``time.time`` and
  advance the clock instead of waiting.
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
}

_SLEEP_MODULES = frozenset({"time", "asyncio"})


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


def test_exemption_list_has_no_stale_entries() -> None:
    """Exempted files must actually exist and use at most their budget.

    Keeps the exemption list honest: removing sleeps from an exempt file
    should also shrink (or drop) its budget entry, and deleted files must
    not linger in the list.
    """
    for rel in _EXEMPT_MAX_CALLS:
        path = _REPO_ROOT / rel
        assert path.is_file(), f"exempted test file no longer exists: {rel}"
    for rel in _EXEMPT_FILES:
        assert (_REPO_ROOT / rel).is_file(), f"exempted test file no longer exists: {rel}"
