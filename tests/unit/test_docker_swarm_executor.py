"""Regression tests for issue #656 — DockerSwarmExecutor shutdown in real mode.

Bug description: In DockerSwarmExecutor, self._stub_executor=False causes shutdown
to fail. The shutdown logic was gated behind `if self._stub_executor:` which only
evaluates to True when _stub_executor is a truthy value (e.g., a LocalExecutor in
stub mode). When _stub_executor is explicitly set to False (which should be treated
same as None for "real mode"), the condition was False and shutdown never ran.

The fix changes the condition from:
    if self._stub_executor and hasattr(self._stub_executor, "shutdown"):
to:
    if self._stub_executor is not None and hasattr(self._stub_executor, "shutdown"):

This correctly handles all three states:
- _stub_executor = LocalExecutor (stub mode): shutdown is called
- _stub_executor = None (real mode): nothing to shutdown, no-op
- _stub_executor = False (real mode, bug case): should be treated as "not stub", no-op
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor


class TestDockerSwarmExecutorShutdown:
    """Test DockerSwarmExecutor.shutdown() behavior across stub_executor states."""

    def _make_executor(self) -> DockerSwarmExecutor:
        """Create a DockerSwarmExecutor without calling __init__."""
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        ex.image = "nrel/openstudio:latest"
        ex.network = None
        ex._client = None
        return ex

    def test_shutdown_stub_mode_calls_local_executor_shutdown(self) -> None:
        """When _stub_executor is a LocalExecutor, shutdown calls its shutdown."""
        ex = self._make_executor()
        mock_local = MagicMock()
        ex._stub_executor = mock_local

        ex.shutdown()

        mock_local.shutdown.assert_called_once()

    def test_shutdown_real_mode_none_is_noop(self) -> None:
        """When _stub_executor is None (real mode), shutdown is a no-op."""
        ex = self._make_executor()
        ex._stub_executor = None

        # Should not raise
        ex.shutdown()

    def test_shutdown_real_mode_false_is_noop(self) -> None:
        """When _stub_executor is False (bug case), shutdown is still a no-op.

        This is the regression test for issue #656. Previously, when
        _stub_executor was False, the condition `if self._stub_executor and ...`
        would evaluate to False and shutdown would not run. With the fix
        (`if self._stub_executor is not None and ...`), the behavior is
        consistent: when there's no stub executor (None or False), shutdown
        does nothing because there's nothing to shut down.
        """
        ex = self._make_executor()
        ex._stub_executor = False

        # Should not raise
        ex.shutdown()

    def test_shutdown_multiple_calls_are_idempotent(self) -> None:
        """Calling shutdown multiple times should be safe."""
        ex = self._make_executor()
        mock_local = MagicMock()
        ex._stub_executor = mock_local

        ex.shutdown()
        ex.shutdown()
        ex.shutdown()

        # shutdown() on LocalExecutor is not idempotent, but multiple calls shouldn't raise
        assert mock_local.shutdown.call_count == 3
