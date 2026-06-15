"""Tests for entry_points-based plug-in discovery (issue #432).

Covers:
- AlgorithmRegistry.discover_plugins() registers valid BaseAlgorithm subclasses
- AlgorithmRegistry.discover_plugins() returns 0 when no entry points exist
- AlgorithmRegistry.discover_plugins() skips non-BaseAlgorithm entry points
- AlgorithmRegistry.discover_plugins() logs + skips on import errors
- ExecutorRegistry basic operations (register, get, list_available)
- ExecutorRegistry.discover_plugins() registers valid BaseExecutor subclasses
- ExecutorRegistry.discover_plugins() returns 0 when no entry points exist
- ExecutorRegistry.discover_plugins() skips non-BaseExecutor entry points
- ExecutorRegistry.discover_plugins() logs + skips on import errors
- Built-in executors are registered at import time
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow.algorithms import (
    ALGORITHM_ENTRY_POINT_GROUP,
    AlgorithmRegistry,
    BaseAlgorithm,
)
from osimflow.executors import (
    EXECUTOR_ENTRY_POINT_GROUP,
    BaseExecutor,
    ExecutorRegistry,
    Handle,
    LocalExecutor,
)

# These tests mutate the registries; run on the same xdist worker.
pytestmark = pytest.mark.xdist_group("algorithm_registry")


# ======================================================================
# Helpers: minimal stub algorithm and executor for testing
# ======================================================================


class StubAlgorithm(BaseAlgorithm):
    """Minimal concrete algorithm for testing."""

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        return Path("stub")

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return []

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        return True

    def name(self) -> str:
        return "stub"

    def is_iterative(self) -> bool:
        return False


class StubExecutor(BaseExecutor):
    """Minimal concrete executor for testing."""

    name = "stub"

    def submit(
        self,
        fn: Any,
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        raise NotImplementedError

    def shutdown(self) -> None:
        pass


def _make_entry_point(name: str, value: str, target: Any) -> MagicMock:
    """Create a mock ``importlib.metadata.EntryPoint``.

    *target* is the object returned by ``.load()``.  *name* and *value*
    mirror the real entry-point attributes (e.g. ``lhs`` / ``osimflow.algorithms:LHSAlgorithm``).
    """
    ep = MagicMock()
    ep.name = name
    ep.value = value
    ep.load = MagicMock(return_value=target)
    return ep


# ======================================================================
# AlgorithmRegistry plugin discovery tests
# ======================================================================


class TestAlgorithmDiscovery:
    """Tests for AlgorithmRegistry.discover_plugins()."""

    def test_discover_no_plugins_returns_zero(self) -> None:
        """When no entry points exist, discovery returns 0 silently."""
        with patch(
            "osimflow.algorithms.entry_points",
            return_value=[],
        ):
            assert AlgorithmRegistry.discover_plugins() == 0

    def test_discover_valid_plugin_registers(self) -> None:
        """A valid BaseAlgorithm entry point is registered."""
        ep = _make_entry_point(
            "stub_plugin",
            "test_module:StubAlgorithm",
            StubAlgorithm,
        )
        with patch(
            "osimflow.algorithms.entry_points",
            return_value=[ep],
        ):
            count = AlgorithmRegistry.discover_plugins()

        try:
            assert count == 1
            assert "stub_plugin" in AlgorithmRegistry.list_available()
            algo = AlgorithmRegistry.get("stub_plugin")
            assert isinstance(algo, StubAlgorithm)
        finally:
            AlgorithmRegistry._registry.pop("stub_plugin", None)

    def test_discover_skips_non_algorithm(self) -> None:
        """Entry points that are not BaseAlgorithm subclasses are skipped."""
        ep = _make_entry_point(
            "not_an_algo",
            "test_module:SomeRandomClass",
            str,  # str is a type but not a BaseAlgorithm
        )
        with patch(
            "osimflow.algorithms.entry_points",
            return_value=[ep],
        ):
            count = AlgorithmRegistry.discover_plugins()

        assert count == 0
        assert "not_an_algo" not in AlgorithmRegistry.list_available()

    def test_discover_skips_non_type(self) -> None:
        """Entry points that load to non-type objects are skipped."""
        ep = _make_entry_point(
            "not_a_type",
            "test_module:some_instance",
            42,  # an int, not a type
        )
        with patch(
            "osimflow.algorithms.entry_points",
            return_value=[ep],
        ):
            count = AlgorithmRegistry.discover_plugins()

        assert count == 0

    def test_discover_handles_import_error(self) -> None:
        """Import errors during entry-point load are caught and skipped."""
        ep = _make_entry_point("broken", "broken_module:Broken", None)
        ep.load = MagicMock(side_effect=ImportError("module not found"))
        with patch(
            "osimflow.algorithms.entry_points",
            return_value=[ep],
        ):
            count = AlgorithmRegistry.discover_plugins()

        assert count == 0
        assert "broken" not in AlgorithmRegistry.list_available()

    def test_discover_handles_arbitrary_exception(self) -> None:
        """Any exception during entry-point load is caught and skipped."""
        ep = _make_entry_point("crashy", "crash_module:Crash", None)
        ep.load = MagicMock(side_effect=RuntimeError("boom"))
        with patch(
            "osimflow.algorithms.entry_points",
            return_value=[ep],
        ):
            count = AlgorithmRegistry.discover_plugins()

        assert count == 0

    def test_discover_multiple_plugins(self) -> None:
        """Multiple valid entry points are all registered."""

        class StubB(StubAlgorithm):
            pass

        ep1 = _make_entry_point("stub_a", "mod:StubAlgorithm", StubAlgorithm)
        ep2 = _make_entry_point("stub_b", "mod:StubB", StubB)

        with patch(
            "osimflow.algorithms.entry_points",
            return_value=[ep1, ep2],
        ):
            count = AlgorithmRegistry.discover_plugins()

        try:
            assert count == 2
            assert "stub_a" in AlgorithmRegistry.list_available()
            assert "stub_b" in AlgorithmRegistry.list_available()
        finally:
            AlgorithmRegistry._registry.pop("stub_a", None)
            AlgorithmRegistry._registry.pop("stub_b", None)

    def test_discover_mixed_valid_and_broken(self) -> None:
        """Valid and broken entry points can coexist; only valid ones register."""

        class StubC(StubAlgorithm):
            pass

        valid_ep = _make_entry_point("stub_ok", "mod:StubC", StubC)
        broken_ep = _make_entry_point("stub_bad", "mod:Broken", None)
        broken_ep.load = MagicMock(side_effect=ImportError("nope"))

        with patch(
            "osimflow.algorithms.entry_points",
            return_value=[valid_ep, broken_ep],
        ):
            count = AlgorithmRegistry.discover_plugins()

        try:
            assert count == 1
            assert "stub_ok" in AlgorithmRegistry.list_available()
            assert "stub_bad" not in AlgorithmRegistry.list_available()
        finally:
            AlgorithmRegistry._registry.pop("stub_ok", None)

    def test_entry_point_group_constant(self) -> None:
        """The entry point group string is the documented value."""
        assert ALGORITHM_ENTRY_POINT_GROUP == "osimflow.algorithms"


# ======================================================================
# ExecutorRegistry tests
# ======================================================================


class TestExecutorRegistry:
    """Tests for ExecutorRegistry and its discover_plugins()."""

    def test_register_and_get(self) -> None:
        """register() then get() returns the class."""
        ExecutorRegistry.register("test_stub_exec", StubExecutor)
        try:
            cls = ExecutorRegistry.get("test_stub_exec")
            assert cls is StubExecutor
        finally:
            ExecutorRegistry._registry.pop("test_stub_exec", None)

    def test_get_unknown_raises(self) -> None:
        """Unknown executor raises ValueError with helpful message."""
        with pytest.raises(ValueError, match="unknown executor 'definitely_not_real'"):
            ExecutorRegistry.get("definitely_not_real")

    def test_get_unknown_error_lists_available(self) -> None:
        """Error message lists available executors."""
        with pytest.raises(ValueError, match="Available executors:.*local"):
            ExecutorRegistry.get("nonexistent_executor_12345")

    def test_list_available_includes_builtins(self) -> None:
        """All built-in executors are registered."""
        available = ExecutorRegistry.list_available()
        for name in ("local", "slurm", "aws_batch", "nomad"):
            assert name in available

    def test_list_available_returns_sorted(self) -> None:
        available = ExecutorRegistry.list_available()
        assert available == sorted(available)


# ======================================================================
# ExecutorRegistry plugin discovery tests
# ======================================================================


class TestExecutorDiscovery:
    """Tests for ExecutorRegistry.discover_plugins()."""

    def test_discover_no_plugins_returns_zero(self) -> None:
        """When no entry points exist, discovery returns 0 silently."""
        with patch(
            "osimflow.executors.entry_points",
            return_value=[],
        ):
            assert ExecutorRegistry.discover_plugins() == 0

    def test_discover_valid_plugin_registers(self) -> None:
        """A valid BaseExecutor entry point is registered."""
        ep = _make_entry_point(
            "stub_exec_plugin",
            "test_module:StubExecutor",
            StubExecutor,
        )
        with patch(
            "osimflow.executors.entry_points",
            return_value=[ep],
        ):
            count = ExecutorRegistry.discover_plugins()

        try:
            assert count == 1
            assert "stub_exec_plugin" in ExecutorRegistry.list_available()
            cls = ExecutorRegistry.get("stub_exec_plugin")
            assert cls is StubExecutor
        finally:
            ExecutorRegistry._registry.pop("stub_exec_plugin", None)

    def test_discover_skips_non_executor(self) -> None:
        """Entry points that are not BaseExecutor subclasses are skipped."""
        ep = _make_entry_point(
            "not_an_exec",
            "test_module:SomeRandomClass",
            str,  # str is a type but not a BaseExecutor
        )
        with patch(
            "osimflow.executors.entry_points",
            return_value=[ep],
        ):
            count = ExecutorRegistry.discover_plugins()

        assert count == 0
        assert "not_an_exec" not in ExecutorRegistry.list_available()

    def test_discover_skips_non_type(self) -> None:
        """Entry points that load to non-type objects are skipped."""
        ep = _make_entry_point(
            "exec_not_a_type",
            "test_module:some_instance",
            object(),  # not a type
        )
        with patch(
            "osimflow.executors.entry_points",
            return_value=[ep],
        ):
            count = ExecutorRegistry.discover_plugins()

        assert count == 0

    def test_discover_handles_import_error(self) -> None:
        """Import errors during entry-point load are caught and skipped."""
        ep = _make_entry_point("broken_exec", "broken_module:Broken", None)
        ep.load = MagicMock(side_effect=ImportError("module not found"))
        with patch(
            "osimflow.executors.entry_points",
            return_value=[ep],
        ):
            count = ExecutorRegistry.discover_plugins()

        assert count == 0
        assert "broken_exec" not in ExecutorRegistry.list_available()

    def test_discover_handles_arbitrary_exception(self) -> None:
        """Any exception during entry-point load is caught and skipped."""
        ep = _make_entry_point("crashy_exec", "crash_module:Crash", None)
        ep.load = MagicMock(side_effect=RuntimeError("boom"))
        with patch(
            "osimflow.executors.entry_points",
            return_value=[ep],
        ):
            count = ExecutorRegistry.discover_plugins()

        assert count == 0

    def test_discover_multiple_plugins(self) -> None:
        """Multiple valid entry points are all registered."""

        class StubExecutorB(StubExecutor):
            pass

        ep1 = _make_entry_point("stub_exec_a", "mod:StubExecutor", StubExecutor)
        ep2 = _make_entry_point("stub_exec_b", "mod:StubExecutorB", StubExecutorB)

        with patch(
            "osimflow.executors.entry_points",
            return_value=[ep1, ep2],
        ):
            count = ExecutorRegistry.discover_plugins()

        try:
            assert count == 2
            assert "stub_exec_a" in ExecutorRegistry.list_available()
            assert "stub_exec_b" in ExecutorRegistry.list_available()
        finally:
            ExecutorRegistry._registry.pop("stub_exec_a", None)
            ExecutorRegistry._registry.pop("stub_exec_b", None)

    def test_discover_mixed_valid_and_broken(self) -> None:
        """Valid and broken entry points can coexist."""

        class StubExecutorC(StubExecutor):
            pass

        valid_ep = _make_entry_point("exec_ok", "mod:StubExecutorC", StubExecutorC)
        broken_ep = _make_entry_point("exec_bad", "mod:Broken", None)
        broken_ep.load = MagicMock(side_effect=ImportError("nope"))

        with patch(
            "osimflow.executors.entry_points",
            return_value=[valid_ep, broken_ep],
        ):
            count = ExecutorRegistry.discover_plugins()

        try:
            assert count == 1
            assert "exec_ok" in ExecutorRegistry.list_available()
            assert "exec_bad" not in ExecutorRegistry.list_available()
        finally:
            ExecutorRegistry._registry.pop("exec_ok", None)

    def test_entry_point_group_constant(self) -> None:
        """The entry point group string is the documented value."""
        assert EXECUTOR_ENTRY_POINT_GROUP == "osimflow.executors"


# ======================================================================
# Integration: built-in executors in registry
# ======================================================================


class TestExecutorRegistryBuiltins:
    """Verify that built-in executors are registered at import time."""

    def test_local_executor_registered(self) -> None:
        cls = ExecutorRegistry.get("local")
        assert cls is LocalExecutor

    def test_all_builtins_present(self) -> None:
        """All documented built-in executors should be in the registry."""
        builtins = [
            "local",
            "slurm",
            "aws_batch",
            "nomad",
            "azure_batch",
            "google_batch",
            "kubernetes",
            "pbs",
            "dask_jobqueue",
        ]
        available = set(ExecutorRegistry.list_available())
        for name in builtins:
            assert name in available, f"executor '{name}' not in registry"
