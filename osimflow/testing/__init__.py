"""Reusable test harness for third-party executor plug-in authors (issue #1478).

Third-party packages can ship executors via the ``osimflow.executors`` entry
point, and every executor must conform to the ``submit()`` ``Handle`` contract
from :mod:`osimflow.executors.base`. This sub-package exposes a one-import
conformance suite so a plug-in author can verify their implementation against
the contract without reverse-engineering the in-repo test suite.

Quickstart::

    # tests/test_my_executor.py
    from osimflow.testing import ExecutorConformanceSuite
    from my_pkg.executors import MyExecutor


    class TestMyExecutorConformance(ExecutorConformanceSuite):
        executor_factory = staticmethod(lambda: MyExecutor(endpoint="..."))

        # The full 3-sample stub campaign is opt-in (marked ``slow``).
        # Set to ``False`` if your executor is genuinely remote-only.
        run_stub_campaign: bool = True

The suite is intentionally a pytest-friendly mixin class (not a single
function) so plug-in authors can override individual checks, add their own
substrate-specific assertions, and reuse the same fixtures across all their
executor variants.

For non-pytest usage (CI scripting, ``pre-commit``-style one-liners) see
:func:`run_executor_conformance` which returns a structured
:class:`ConformanceReport` instead of raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .executor_conformance import (
    ConformanceCheck,
    ConformanceReport,
    ExecutorConformanceSuite,
    run_executor_conformance,
)

__all__ = [
    "ConformanceCheck",
    "ConformanceReport",
    "ExecutorConformanceSuite",
    "run_executor_conformance",
]

if TYPE_CHECKING:
    # Re-exported for type checkers only — importing eagerly would pull
    # the heavy ``osimflow.executors`` module into every plug-in test
    # file's import graph.
    from osimflow.executors.base import BaseExecutor, Handle  # noqa: F401
