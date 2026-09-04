"""Reusable test harness for third-party executor and algorithm plug-in authors
(issues #1478 and #1565).

Third-party packages can ship executors via the ``osimflow.executors`` entry
point and algorithms via the ``osimflow.algorithms`` entry point. Every
executor must conform to the ``submit()`` ``Handle`` contract from
:mod:`osimflow.executors.base`; every algorithm must satisfy the
:class:`~osimflow.algorithms.BaseAlgorithm` sampling contract. This
sub-package exposes a one-import conformance suite per plug-in surface
so a plug-in author can verify their implementation against the contract
without reverse-engineering the in-repo test suite.

Quickstart for executors::

    # tests/test_my_executor.py
    from osimflow.testing import ExecutorConformanceSuite
    from my_pkg.executors import MyExecutor


    class TestMyExecutorConformance(ExecutorConformanceSuite):
        executor_factory = staticmethod(lambda: MyExecutor(endpoint="..."))

        # The full 3-sample stub campaign is opt-in (marked ``slow``).
        # Set to ``False`` if your executor is genuinely remote-only.
        run_stub_campaign: bool = True

Quickstart for algorithms::

    # tests/test_my_algorithm.py
    from osimflow.algorithms import AlgorithmRegistry
    from osimflow.testing import AlgorithmConformanceSuite


    class TestMyAlgorithmConformance(AlgorithmConformanceSuite):
        algorithm_factory = staticmethod(
            lambda: AlgorithmRegistry.get("my_plugin_name")
        )

Both suites are intentionally pytest-friendly mixin classes (not single
functions) so plug-in authors can override individual checks, add their
own substrate-specific assertions, and reuse the same fixtures across all
their plug-in variants.

For non-pytest usage (CI scripting, ``pre-commit``-style one-liners) see
:func:`run_executor_conformance` / :func:`run_algorithm_conformance` which
return structured :class:`ConformanceReport` /
:class:`AlgorithmConformanceReport` dataclasses instead of raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import patch_targets as patch_targets
from .algorithm_conformance import (
    DEFAULT_SAMPLE_COUNTS,
    DEFAULT_SEED,
    DEFAULT_VARIABLES_SPEC,
    AlgorithmConformanceReport,
    AlgorithmConformanceSuite,
    run_algorithm_conformance,
)
from .executor_conformance import (
    ConformanceCheck,
    ConformanceReport,
    ExecutorConformanceSuite,
    run_executor_conformance,
)

__all__ = [
    "AlgorithmConformanceReport",
    "AlgorithmConformanceSuite",
    "ConformanceCheck",
    "ConformanceReport",
    "DEFAULT_SAMPLE_COUNTS",
    "DEFAULT_SEED",
    "DEFAULT_VARIABLES_SPEC",
    "ExecutorConformanceSuite",
    "patch_targets",
    "run_algorithm_conformance",
    "run_executor_conformance",
]

if TYPE_CHECKING:
    # Re-exported for type checkers only — importing eagerly would pull
    # the heavy ``osimflow.executors`` module into every plug-in test
    # file's import graph.
    from osimflow.executors.base import BaseExecutor, Handle  # noqa: F401
