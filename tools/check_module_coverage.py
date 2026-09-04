#!/usr/bin/env python3
"""check_module_coverage.py — per-module coverage floor for refactor hot-spots.

Issue #1571: the 82% aggregate gate (Makefile ``PYTEST_COV_FLAGS
--cov-fail-under=82``) cannot detect a wholly untested new module. The
#1462/#1463/#1464 refactors extracted twelve ``_campaign_*.py``
collaborators out of ``osimflow/campaign.py`` and the ten
per-executor modules live as separate files under
``osimflow/executors/``; with an aggregate-only threshold, a brand-new
collaborator extracted with 0% direct test coverage (tests still pass
through the ``Campaign`` facade) can land while the total stays above
82%. The gate structurally cannot notice a wholly untested new module —
exactly the window where behavior silently changes on an extraction PR.

This script closes that gap by reading the ``.coverage`` data file
produced by ``make test-cov`` (via ``coverage json``) and asserting
each in-scope module is at or above a seed floor (= current measured
coverage minus a small epsilon). The floor is a regression guard, not
an aspirational target — only ratchet up after a real measurement
shows the module is stably above the previous floor. Do not lower the
existing 82% aggregate gate to compensate; this check is additive.

In-scope modules (issue #1571 acceptance criteria):

  * every ``osimflow/_campaign_*.py`` collaborator
  * every ``osimflow/executors/*.py`` module

The seed floors below were captured on ``origin/main`` (commit
``30f3c79``, "refactor: resolve #1574 — explicit testing surface for
executor patch seams") with the canonical ``make test-cov`` invocation
(``PYTEST_CI_FLAGS -m "not nomad_e2e and not slow and not chaos"
tests/integration tests/unit`` + ``PYTEST_COV_FLAGS
--cov=osimflow --cov-report=xml --cov-report=term-missing
--cov-fail-under=82``). Aggregate ``TOTAL`` was 85.20% (above the 82%
gate) and the per-module readings were::

    osimflow/_campaign_artifacts.py           91.28%
    osimflow/_campaign_baseline.py            98.44%
    osimflow/_campaign_chaos.py               29.33%
    osimflow/_campaign_code_hashes.py         92.99%
    osimflow/_campaign_cost_tracker.py        91.75%
    osimflow/_campaign_epw.py                 89.52%
    osimflow/_campaign_hooks.py               72.17%
    osimflow/_campaign_lifecycle.py           78.81%
    osimflow/_campaign_observability.py       91.39%
    osimflow/_campaign_quota.py               97.94%
    osimflow/_campaign_sample_trace.py        90.43%
    osimflow/_campaign_sharding.py            86.05%
    osimflow/executors/__init__.py            91.91%
    osimflow/executors/aws_batch_executor.py  95.15%
    osimflow/executors/azure_batch_executor.py 83.19%
    osimflow/executors/base.py                94.57%
    osimflow/executors/dask_jobqueue_executor.py 81.73%
    osimflow/executors/docker_swarm_executor.py 50.41%
    osimflow/executors/google_batch_executor.py 74.88%
    osimflow/executors/kubernetes_executor.py 71.43%
    osimflow/executors/local_executor.py      93.94%
    osimflow/executors/nomad_executor.py      85.62%
    osimflow/executors/pbs_executor.py        90.34%
    osimflow/executors/slurm_executor.py     100.00%
    osimflow/executors/transport.py           83.84%

Each ``FLOORS`` entry below is the corresponding reading minus a 1.0
epsilon. When ``_campaign_chaos.py`` and the uncovered executor
modules (``docker_swarm_executor``, ``google_batch_executor``,
``kubernetes_executor``) accrue direct tests, raise the floor to the
new measured value.

Hook:

  * Makefile ``test-cov`` target (after pytest, before reporting).
  * local: ``python tools/check_module_coverage.py``
  * CI:    same as ``make test-cov`` — ``.github/workflows/ci.yml``
    runs ``make test-cov``.

Exit code 0 on success, 1 if any module falls below its floor (or if
the coverage data file is missing / unreadable).
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Path to the .coverage data file written by `make test-cov`. The
# pytest-cov plugin drops it next to the `pyproject.toml` cwd; we keep
# the lookup explicit so a future test runner that changes cwd still
# resolves correctly.
COVERAGE_DATA = REPO_ROOT / ".coverage"

# Seed floor per in-scope module (issue #1571). Keys are POSIX-style
# paths (forward slashes) to match the `coverage json` output and the
# `osimflow/_campaign_*.py` / `osimflow/executors/*.py` globs that
# scope the check. Each value is the measured coverage % on
# commit 30f3c79 minus a 1.0% epsilon — a regression guard, not a
# target. Bump values together with a coverage measurement comment in
# the docstring above; do not raise them above the freshly measured %.
FLOORS: dict[str, float] = {
    # --- osimflow/_campaign_*.py collaborators (#1462/#1463/#1464) ---
    "osimflow/_campaign_artifacts.py": 90.28,  # measured 91.28%
    "osimflow/_campaign_baseline.py": 97.44,  # measured 98.44%
    "osimflow/_campaign_chaos.py": 28.33,  # measured 29.33% (ratchet up as #1013 chaos tests directly cover)
    "osimflow/_campaign_code_hashes.py": 91.99,  # measured 92.99%
    "osimflow/_campaign_cost_tracker.py": 90.75,  # measured 91.75%
    "osimflow/_campaign_epw.py": 88.52,  # measured 89.52%
    "osimflow/_campaign_hooks.py": 71.17,  # measured 72.17%
    "osimflow/_campaign_lifecycle.py": 77.81,  # measured 78.81%
    "osimflow/_campaign_observability.py": 90.39,  # measured 91.39%
    "osimflow/_campaign_quota.py": 96.94,  # measured 97.94%
    "osimflow/_campaign_sample_trace.py": 89.43,  # measured 90.43%
    "osimflow/_campaign_sharding.py": 85.05,  # measured 86.05%
    # --- osimflow/executors/*.py ---
    "osimflow/executors/__init__.py": 90.91,  # measured 91.91%
    "osimflow/executors/aws_batch_executor.py": 94.15,  # measured 95.15%
    "osimflow/executors/azure_batch_executor.py": 82.19,  # measured 83.19%
    "osimflow/executors/base.py": 93.57,  # measured 94.57%
    "osimflow/executors/dask_jobqueue_executor.py": 80.73,  # measured 81.73%
    "osimflow/executors/docker_swarm_executor.py": 49.41,  # measured 50.41%
    "osimflow/executors/google_batch_executor.py": 73.88,  # measured 74.88%
    "osimflow/executors/kubernetes_executor.py": 70.43,  # measured 71.43%
    "osimflow/executors/local_executor.py": 92.94,  # measured 93.94%
    "osimflow/executors/nomad_executor.py": 84.62,  # measured 85.62%
    "osimflow/executors/pbs_executor.py": 89.34,  # measured 90.34%
    "osimflow/executors/slurm_executor.py": 99.00,  # measured 100.00%
    "osimflow/executors/transport.py": 82.84,  # measured 83.84%
}

# Glob patterns that scope the check (issue #1571). A future addition
# of a new ``osimflow/_campaign_<name>.py`` or
# ``osimflow/executors/<name>.py`` file must add an entry to FLOORS;
# the assertion below flags any in-scope file missing from the table.
GLOBS = (
    "osimflow/_campaign_*.py",
    "osimflow/executors/*.py",
)


def _in_scope_files() -> dict[str, Path]:
    """Return {relative_posix_path: absolute_path} for every in-scope module.

    Mirrors the ``osimflow/_campaign_*.py`` and
    ``osimflow/executors/*.py`` patterns that issue #1571 names
    explicitly. New modules matching these globs must be added to
    FLOORS or the check fails — that is the point of the assertion
    (catches a freshly-extracted collaborator sneaking in with 0%
    coverage the way the issue describes).
    """
    found: dict[str, Path] = {}
    for pattern in GLOBS:
        for path in (REPO_ROOT).glob(pattern):
            if not path.is_file():
                continue
            found[path.relative_to(REPO_ROOT).as_posix()] = path
    return found


def _coverage_json_path() -> Path:
    """Render the .coverage data file into a JSON report.

    ``coverage json`` is the supported way to extract per-file
    percentages from a ``.coverage`` SQLite file; it honours the same
    ``pyproject.toml [tool.coverage.*]`` config (omit list, branch
    setting, source filters) as the XML output pytest-cov writes
    alongside it. We render into a tempfile because ``make test-cov``
    does not currently emit a JSON artifact, and adding one to
    ``PYTEST_COV_FLAGS`` would mean editing two places on every
    coverage flag change.
    """
    if not COVERAGE_DATA.exists():
        raise SystemExit(
            f"{COVERAGE_DATA} not found — run `make test-cov` first "
            "(pytest-cov writes the data file under the repo root)."
        )
    out_path = Path(tempfile.gettempdir()) / "osimflow_module_coverage.json"
    subprocess.run(  # noqa: S603  — fixed argv, no shell
        [
            sys.executable,
            "-m",
            "coverage",
            "json",
            "-o",
            str(out_path),
        ],
        cwd=str(REPO_ROOT),
        check=True,
    )
    return out_path


def _load_file_pcts(json_path: Path) -> dict[str, float]:
    """Read coverage.json and return {posix_path: percent_covered}."""
    with json_path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    files = data.get("files", {})
    return {
        path: float(summary["percent_covered"])
        for path, payload in files.items()
        if (summary := payload.get("summary"))
    }


def main() -> int:
    in_scope = _in_scope_files()
    missing_from_floors = sorted(set(in_scope) - set(FLOORS))
    if missing_from_floors:
        print(
            "FAIL: in-scope module(s) missing from FLOORS — add a floor entry (issue #1571):",
            file=sys.stderr,
        )
        for path in missing_from_floors:
            print(f"  - {path}", file=sys.stderr)
        return 1

    cov_json = _coverage_json_path()
    measured = _load_file_pcts(cov_json)

    failures: list[tuple[str, float, float]] = []
    for module, floor in sorted(FLOORS.items()):
        actual = measured.get(module)
        if actual is None:
            failures.append((module, floor, float("nan")))
            continue
        if actual < floor:
            failures.append((module, floor, actual))

    if failures:
        print(
            "FAIL: per-module coverage floor (issue #1571) — "
            f"{len(failures)} module(s) below their floor:",
            file=sys.stderr,
        )
        for module, floor, actual in failures:
            actual_str = "missing" if math.isnan(actual) else f"{actual:.2f}%"
            print(f"  - {module}: {actual_str} < floor {floor:.2f}%", file=sys.stderr)
        return 1

    rows = sorted(FLOORS.items())
    width = max(len(m) for m, _ in rows)
    print(f"OK: {len(rows)} module(s) at or above their floor (issue #1571):")
    for module, floor in rows:
        actual = measured.get(module, float("nan"))
        print(f"  {module:<{width}}  floor={floor:6.2f}%  measured={actual:6.2f}%")
    return 0


if __name__ == "__main__":
    # Avoid argparse — this script's only "argument" is whether the
    # data file exists, which is determined above. CI invocation is
    # `make test-cov` which chains `pytest` then this script.
    # Strip the conventional ``__pycache__`` artefacts that
    # ``coverage json`` writes under the cwd on each run.
    sys.exit(main())
