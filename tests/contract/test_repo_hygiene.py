"""Repo-hygiene contract test guarding against orphaned non-test modules (issue #1631).

``pyproject.toml`` pins ``python_files = ["test_*.py"]``, so any ``*.py``
module under ``tests/`` whose name does not match ``test_*`` (and is not a
``conftest.py`` / ``__init__.py``) is *never collected* by pytest. Such a
module is only meaningful if some test imports it — otherwise it is dead
code that shadows real class names and pollutes grep audits (the exact
defect class of #1572 and #1631: ``tests/unit/azure_batch_executor.py`` and
``tests/unit/google_batch_executor.py`` were never-imported duplicates of
``osimflow/executors/{azure,google}_batch_executor.py``).

This test fails when a non-test Python module exists under ``tests/``
unless it is explicitly allowlisted below with a justification comment.
It is deliberately FAST: pure filesystem walking, no subprocesses, no
imports of the scanned modules — safe for the pre-commit ``pytest-fast``
path. It also fails on stale allowlist entries so pruned helpers cannot
linger here unnoticed.
"""

from pathlib import Path

# Project root is the parent of this package.
REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"

# Modules that pytest never collects but that ARE legitimately imported
# (or invoked) elsewhere. Paths are relative to ``tests/`` (POSIX form).
# Every entry MUST carry a justification comment.
ALLOWLISTED_NON_TEST_MODULES: dict[str, str] = {
    # Perf-smoke entry point: imported by tests/benchmarks/test_bench_regression.py
    # and invoked as ``python -m tests.benchmarks.bench_campaign`` by
    # .github/workflows/bench.yml and docs/benchmarks.md.
    "benchmarks/bench_campaign.py": (
        "perf benchmark entry point (bench CI workflow + test_bench_regression.py)"
    ),
    # Shared resource-contract helpers for the real-substrate E2E tests
    # (imported by test_aws_batch_real.py, test_azure_batch_real.py,
    # test_google_batch_real.py, test_real_dask_campaign.py,
    # test_real_docker_swarm_campaign.py, test_real_nomad_ha_campaign.py,
    # test_real_pbs_campaign.py, test_slurm_real_cluster.py).
    "integration/_resource_contract.py": (
        "shared helper module imported by the real-substrate integration tests"
    ),
}

_ALLOWED_BASENAMES = {"conftest.py", "__init__.py"}


def _non_test_modules() -> set[str]:
    """Return POSIX-relative paths of every non-test ``*.py`` under tests/."""
    strays: set[str] = set()
    for path in TESTS_ROOT.rglob("*.py"):
        if path.name in _ALLOWED_BASENAMES or path.name.startswith("test_"):
            continue
        strays.add(path.relative_to(TESTS_ROOT).as_posix())
    return strays


def test_no_orphaned_non_test_modules_under_tests() -> None:
    strays = _non_test_modules()
    orphans = strays - set(ALLOWLISTED_NON_TEST_MODULES)
    assert not orphans, (
        "Orphaned non-test Python module(s) under tests/ — pytest never "
        "collects them (python_files = ['test_*.py']) and nothing imports "
        "them. Delete them, or add a justified allowlist entry in "
        "tests/contract/test_repo_hygiene.py if they are genuinely imported "
        f"fixtures/helpers: {sorted(orphans)}"
    )


def test_allowlist_entries_still_exist() -> None:
    stale = sorted(rel for rel in ALLOWLISTED_NON_TEST_MODULES if not (TESTS_ROOT / rel).is_file())
    assert not stale, (
        "Stale allowlist entries in ALLOWLISTED_NON_TEST_MODULES (files no "
        "longer exist under tests/) — prune them: "
        f"{stale}"
    )
