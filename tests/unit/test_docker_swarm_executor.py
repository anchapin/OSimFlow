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

import os
from unittest.mock import MagicMock, patch

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


class TestDockerSwarmExecutorFailDense:
    """Regression tests for issue #944 — fail-dense by default.

    When Docker is unavailable or not in Swarm mode, submit() must raise
    RuntimeError instead of silently falling back to LocalExecutor. The fallback
    path is only available when explicitly opted in via:
    - OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1  (general dev/CI opt-in)
    - OSIMFLOW_DOCKER_SWARM_DRY_RUN=1       (dry-run mode, set by Campaign)
    """

    @pytest.fixture(autouse=True)
    def _clear_fallback_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ensure no fallback env vars leak from other tests (e.g. dry-run campaign tests).

        The Campaign class sets OSIMFLOW_DOCKER_SWARM_DRY_RUN=1 process-globally
        during --dry-run execution (added in #944). Under pytest-xdist (-n 2),
        if a dry-run campaign test runs in the same worker process before these
        fail-dense tests, the env var leaks and _is_dev_fallback_enabled() returns
        True, causing submit() to fall back instead of raising.
        """
        monkeypatch.delenv("OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK", raising=False)
        monkeypatch.delenv("OSIMFLOW_DOCKER_SWARM_DRY_RUN", raising=False)

    def _make_executor(self) -> DockerSwarmExecutor:
        """Create a DockerSwarmExecutor without calling __init__."""
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        ex.image = "nrel/openstudio:latest"
        ex.network = None
        ex._client = None
        ex._stub_executor = None
        return ex

    def test_submit_raises_when_docker_not_in_swarm_mode_no_fallback(self) -> None:
        """Without dev-fallback flag, submit() raises when Docker is not in Swarm mode."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with pytest.raises(RuntimeError, match="not in Swarm mode"):
                ex.submit(lambda: None)

    def test_submit_raises_when_docker_unavailable_no_fallback(self) -> None:
        """Without dev-fallback flag, submit() raises when Docker is unreachable."""
        ex = self._make_executor()
        with patch.object(
            ex, "_check_docker_available", side_effect=RuntimeError("daemon not reachable")
        ):
            with pytest.raises(RuntimeError, match="not reachable"):
                ex.submit(lambda: None)

    def test_submit_raises_when_docker_import_error_no_fallback(self) -> None:
        """Without dev-fallback flag, submit() raises when docker package is absent."""
        ex = self._make_executor()
        with patch.object(
            ex,
            "_check_docker_available",
            side_effect=ImportError("docker package not installed"),
        ):
            with pytest.raises(RuntimeError, match="not reachable"):
                ex.submit(lambda: None)

    def test_submit_falls_back_when_dev_fallback_env_set(self) -> None:
        """With OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1, submit() falls back to LocalExecutor."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with patch.dict(os.environ, {"OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK": "1"}):
                handle = ex.submit(lambda: None)
                # Should return a Handle from the LocalExecutor fallback
                assert handle is not None
                # The stub executor should now be set
                assert ex._stub_executor is not None

    def test_submit_falls_back_when_dry_run_env_set(self) -> None:
        """With OSIMFLOW_DOCKER_SWARM_DRY_RUN=1, submit() falls back to LocalExecutor."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with patch.dict(os.environ, {"OSIMFLOW_DOCKER_SWARM_DRY_RUN": "1"}):
                handle = ex.submit(lambda: None)
                assert handle is not None
                assert ex._stub_executor is not None

    def test_submit_falls_back_when_both_envs_set(self) -> None:
        """Both env vars set — should still fall back (both are opt-ins)."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with patch.dict(
                os.environ,
                {
                    "OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK": "1",
                    "OSIMFLOW_DOCKER_SWARM_DRY_RUN": "1",
                },
            ):
                handle = ex.submit(lambda: None)
                assert handle is not None

    def test_submit_raises_when_docker_unavailable_dev_fallback_false(self) -> None:
        """OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=0 should NOT enable fallback."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with patch.dict(os.environ, {"OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK": "0"}):
                with pytest.raises(RuntimeError, match="not in Swarm mode"):
                    ex.submit(lambda: None)

    def test_submit_raises_when_docker_unavailable_dry_run_false(self) -> None:
        """OSIMFLOW_DOCKER_SWARM_DRY_RUN=0 should NOT enable fallback."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with patch.dict(os.environ, {"OSIMFLOW_DOCKER_SWARM_DRY_RUN": "0"}):
                with pytest.raises(RuntimeError, match="not in Swarm mode"):
                    ex.submit(lambda: None)

    def test_submit_raises_when_exception_and_no_fallback(self) -> None:
        """RuntimeError from _check_docker_available should raise when no fallback flag."""
        ex = self._make_executor()
        with patch.object(
            ex,
            "_check_docker_available",
            side_effect=RuntimeError("connection refused"),
        ):
            with pytest.raises(RuntimeError, match="not reachable"):
                ex.submit(lambda: None)

    def test_submit_succeeds_when_swarm_available(self) -> None:
        """When Docker is in Swarm mode, submit() should NOT fall back or raise."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=True):
            with patch.object(ex, "_submit_service", return_value="test-service"):
                handle = ex.submit(lambda: None)
                # _stub_executor should remain None (real mode)
                assert ex._stub_executor is None
                assert handle is not None
