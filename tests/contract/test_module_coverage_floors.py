"""Contract tests pinning the per-module coverage floor table (issue #1571).

The aggregate 82% gate cannot detect a wholly untested newly-extracted
collaborator (the failure mode #1462/#1463/#1464 opened up). Issue
#1571 closes that gap with a per-module floor dict in
``tools/check_module_coverage.py``; this contract pins the table so a
silent shrink of a floor (e.g. "I'll just lower the floor a bit so
the PR lands") fails here immediately.

Three invariants — all hermetic file reads:

  1. The script exists and runs to completion with ``.coverage`` present.
  2. Every in-scope file (``osimflow/_campaign_*.py``,
     ``osimflow/executors/*.py``) has a ``FLOORS`` entry — new
     modules landing without a floor fail here before the gate.
  3. No floor is raised above the seed value (current measurement
     on ``origin/main`` commit ``30f3c79``) — the floor is a regression
     guard, not an aspirational target (issue #1571 acceptance).
     Lowering a floor is also flagged: the only sanctioned reason is
     the documented one (a module legitimately shrank; that is a
     refactor, not a coverage change). See #1571 for the ratchet-up
     procedure.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# Project root is the parent of this package.
REPO_ROOT = Path(__file__).resolve().parents[2]

_SCRIPT = REPO_ROOT / "tools" / "check_module_coverage.py"

# Seed measurements on origin/main commit 30f3c79, captured from a
# canonical `make test-cov` run.  The script's `FLOORS` dict carries
# `measured - 1.0%` — the regression guard epsilon.  These are the
# ratchet-up ceilings; bumping a floor above this requires a fresh
# measurement comment in the script's docstring (issue #1571).
_SEED_PCT: dict[str, float] = {
    "osimflow/_campaign_artifacts.py": 91.28,
    "osimflow/_campaign_baseline.py": 98.44,
    "osimflow/_campaign_chaos.py": 29.33,
    "osimflow/_campaign_code_hashes.py": 92.99,
    "osimflow/_campaign_cost_tracker.py": 91.75,
    "osimflow/_campaign_epw.py": 89.52,
    "osimflow/_campaign_hooks.py": 72.17,
    "osimflow/_campaign_lifecycle.py": 78.81,
    "osimflow/_campaign_observability.py": 91.39,
    "osimflow/_campaign_quota.py": 97.94,
    "osimflow/_campaign_sample_trace.py": 90.43,
    "osimflow/_campaign_sharding.py": 86.05,
    "osimflow/executors/__init__.py": 91.91,
    "osimflow/executors/aws_batch_executor.py": 95.15,
    "osimflow/executors/azure_batch_executor.py": 83.19,
    "osimflow/executors/base.py": 94.57,
    "osimflow/executors/dask_jobqueue_executor.py": 81.73,
    "osimflow/executors/docker_swarm_executor.py": 50.41,
    "osimflow/executors/google_batch_executor.py": 74.88,
    "osimflow/executors/kubernetes_executor.py": 71.43,
    "osimflow/executors/local_executor.py": 93.94,
    "osimflow/executors/nomad_executor.py": 85.62,
    "osimflow/executors/pbs_executor.py": 90.34,
    "osimflow/executors/slurm_executor.py": 100.00,
    "osimflow/executors/transport.py": 83.84,
}

# Expected epsilon: floor = measured - EPSILON.
_EPSILON = 1.0

# Approximate lower-bound on seed measurements to permit a floor
# shrink when a module legitimately shrank (e.g. code was moved out
# to a sibling collaborator, reducing the module's total statements).
# This is the documented exception; any shrink below it must come
# with a docstring note in tools/check_module_coverage.py.
_MIN_EPSILON_PCT = 0.0


def _parse_floors() -> dict[str, float]:
    """Parse the ``FLOORS`` dict literal from the script source.

    We parse rather than import the script so a syntax error in the
    script (which the floor check itself would surface only at
    runtime, after ``.coverage`` exists) fails fast at the contract
    layer where pre-commit picks it up. The dict is the only mutable
    surface the gate reads, so this is sufficient.

    Accepts both ``FLOORS = {...}`` and ``FLOORS: dict[str, float] =
    {...}`` (AnnotatedAssign) — the script uses the annotated form
    because mypy --strict on the script's own call sites prefers it.
    """
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        is_floors_assign = False
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "FLOORS" for t in node.targets
        ):
            is_floors_assign = True
            value = node.value
        elif isinstance(node, ast.AnnAssign) and (
            isinstance(node.target, ast.Name) and node.target.id == "FLOORS"
        ):
            is_floors_assign = True
            value = node.value
        if not is_floors_assign or value is None:
            continue
        assert isinstance(value, ast.Dict), (
            f"FLOORS must be a dict literal in {_SCRIPT}, got {type(value).__name__} (issue #1571)."
        )
        out: dict[str, float] = {}
        for k_node, v_node in zip(value.keys, value.values, strict=True):
            assert isinstance(k_node, ast.Constant) and isinstance(k_node.value, str), (
                "FLOORS keys must be string literals (issue #1571)."
            )
            assert isinstance(v_node, ast.Constant) and isinstance(v_node.value, (int, float)), (
                f"FLOORS[{k_node.value!r}] must be a numeric literal, "
                f"got {type(v_node.value).__name__} (issue #1571)."
            )
            out[k_node.value] = float(v_node.value)
        return out
    raise AssertionError(f"No FLOORS dict found in {_SCRIPT} (issue #1571).")


def _in_scope_files() -> set[str]:
    """Return the set of in-scope file paths per the globs in the script.

    Mirrors the patterns the floor script declares (issue #1571 +
    #1557 extension): every ``_campaign_*.py`` collaborator, every
    ``executors/*.py`` module, and — since the subprocess coverage
    bootstrap turned on in #1557 — every ``_work_scripts/*.py`` per-step
    script. The ``test_every_in_scope_file_has_floor`` /
    ``test_no_extra_floors`` invariants below rely on this set being
    exactly the union of the floor script's ``GLOBS``.
    """
    out: set[str] = set()
    for pattern in (
        "osimflow/_campaign_*.py",
        "osimflow/executors/*.py",
        "osimflow/_work_scripts/*.py",
    ):
        out.update(
            str(p.relative_to(REPO_ROOT)).replace("\\", "/")
            for p in (REPO_ROOT).glob(pattern)
            if p.is_file()
        )
    return out


def test_script_exists() -> None:
    """The floor check script must exist at the documented path (issue #1571)."""
    assert _SCRIPT.is_file(), (
        f"{_SCRIPT} not found — the per-module floor check is wired into "
        "`make test-cov`, so a missing script breaks the CI gate."
    )


def test_floors_dict_present() -> None:
    """FLOORS must be a top-level dict literal in the script (issue #1571)."""
    floors = _parse_floors()
    assert floors, "FLOORS dict is empty (issue #1571)."


def test_every_in_scope_file_has_floor() -> None:
    """Every ``osimflow/_campaign_*.py``, ``osimflow/executors/*.py``
    and ``osimflow/_work_scripts/*.py`` file must have a ``FLOORS``
    entry (issue #1571 + #1557 extension).

    A freshly-extracted collaborator that lands without a floor
    fails here, closing the "wholly untested new module under
    aggregate gate" gap the issue describes. The ``_work_scripts``
    branch was added when subprocess coverage turned on (issue #1557):
    the scripts were previously invisible, and a future regression in
    the severe-error classifier or pre-flight check must still fail
    the merge gate.
    """
    in_scope = _in_scope_files()
    floors = _parse_floors()
    missing = sorted(in_scope - set(floors))
    assert not missing, (
        f"{_SCRIPT.name} has no FLOORS entry for in-scope module(s): "
        f"{missing}. Issue #1571 + #1557 require every "
        "``osimflow/_campaign_*.py``, ``osimflow/executors/*.py`` and "
        "``osimflow/_work_scripts/*.py`` file to carry one — set the "
        "floor to ``measured - 1.0%`` and add the measured value as a "
        "comment + docstring update."
    )


def test_no_extra_floors() -> None:
    """FLOORS must not carry entries for paths that no longer exist on disk.

    Catches stale floors referencing deleted/renamed modules — they
    silently degrade the check's coverage report. Covers the
    ``_campaign_*.py``, ``executors/*.py`` and ``_work_scripts/*.py``
    globs (issue #1557 extension).
    """
    in_scope = _in_scope_files()
    floors = _parse_floors()
    stale = sorted(set(floors) - in_scope)
    assert not stale, (
        f"FLOORS references module(s) that no longer match the "
        f"``osimflow/_campaign_*.py`` / ``osimflow/executors/*.py`` / "
        f"``osimflow/_work_scripts/*.py`` globs: {stale}. Either the "
        "glob moved (update GLOBS in the script) or the module was "
        "deleted/renamed (drop the FLOORS row)."
    )


def test_no_floor_above_seed() -> None:
    """No floor may exceed the seed measurement on commit 30f3c79 (issue #1571).

    The floor is a regression guard; raising it above the current
    measured value makes the check aspirational and turns the gate
    into a flake magnet. To ratchet up, take a fresh measurement,
    paste the new value into the script's docstring seed table,
    and update this contract's ``_SEED_PCT`` in the same change.
    """
    floors = _parse_floors()
    violations: list[tuple[str, float, float]] = []
    for module, floor in floors.items():
        seed = _SEED_PCT.get(module)
        if seed is None:
            continue  # new module — covered by other test
        if floor > seed - _EPSILON + 1e-9:  # tolerate fp noise on the +epsilon side
            # Allow floor = seed exactly (the docstring notes some
            # modules are 100%-covered and the floor mirrors that);
            # anything strictly above is the violation.
            if floor > seed + 1e-9:
                violations.append((module, floor, seed))
    assert not violations, (
        "FLOORS raises the floor above the seed measurement (issue #1571 "
        "says the floor is a regression guard, not a target): "
        + ", ".join(f"{m}: floor {f:.2f}% > seed {s:.2f}%" for m, f, s in violations)
    )


def test_floor_within_epsilon_of_seed() -> None:
    """Each floor must equal ``seed - EPSILON`` (±0.01% rounding).

    Guards against ad-hoc drift (someone bumping the floor by 0.5%
    "just because"). The only sanctioned shrink is a module that
    legitimately shrank — those should also update ``_SEED_PCT`` and
    the docstring seed table in the same change. This test
    intentionally fails on shrink so the change is explicit.
    """
    floors = _parse_floors()
    drifts: list[tuple[str, float, float]] = []
    for module, floor in floors.items():
        seed = _SEED_PCT.get(module)
        if seed is None:
            continue
        expected = seed - _EPSILON
        if expected < _MIN_EPSILON_PCT:
            # Floor pinned at 0.0% (module legitimately shrank below
            # the epsilon). Allow that; anything below 0 is impossible.
            continue
        # Tolerate fp noise in either direction within 0.05% (the
        # the script stores the value as a float literal). Anything
        # larger than that is a deliberate edit, not noise.
        if abs(floor - expected) > 0.05:
            drifts.append((module, floor, expected))
    assert not drifts, (
        "FLOORS drifted from the documented `seed - 1.0%` pattern "
        "(issue #1571). Each floor must be the seed measurement minus "
        "the 1.0% epsilon. If a module legitimately shrank, drop the "
        "floor to the new measured - 1.0% AND update _SEED_PCT + the "
        "docstring seed table in tools/check_module_coverage.py in the "
        "same change. Drifts: "
        + ", ".join(f"{m}: floor {f:.2f}% vs expected {e:.2f}%" for m, f, e in drifts)
    )


def test_makefile_test_cov_invokes_script() -> None:
    """The Makefile ``test-cov`` target must invoke the floor check (issue #1571)."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    # Match the recipe line(s) under `test-cov:`. Greedy match stops
    # at the next blank line / next target (lines starting at column 0).
    match = re.search(
        r"^test-cov:\s*[^\n]*\n((?:\t[^\n]*\n)+)",
        makefile,
        re.MULTILINE,
    )
    assert match is not None, "Makefile has no `test-cov:` target."
    recipe = match.group(1)
    assert "check_module_coverage.py" in recipe, (
        "Makefile `test-cov:` recipe no longer invokes "
        "`tools/check_module_coverage.py` (issue #1571). Restore it so "
        "the per-module floor check runs as part of the merge gate."
    )
