"""Unit tests for GoogleBatchExecutor (issue #254, #352)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors.google_batch_executor import (
    GoogleBatchExecutor,
    _GoogleBatchHandle,
)


class TestGoogleBatchExecutor:
    """GoogleBatchExecutor wraps the Google Cloud Batch SDK."""

    def _make_executor(self, **kw: object) -> GoogleBatchExecutor:
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._batch_v1 = MagicMock()
        ex.project_id = kw.get("project_id", "test-project")
        ex.region = kw.get("region", "us-central1")
        ex.batch_service_account = kw.get("batch_service_account", None)
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = kw.get("use_spot", False)
        ex.fallback_to_on_demand = kw.get("fallback_to_on_demand", False)
        ex.max_retries = kw.get("max_retries", 3)
        ex._client = MagicMock()
        ex._submit_job = MagicMock(return_value="osimflow-test")
        return ex

    def test_name_attribute(self) -> None:
        ex = self._make_executor()
        assert ex.name == "google_batch"

    def test_submit_succeeds(self) -> None:
        ex = self._make_executor()
        mock_job = MagicMock()
        mock_job.status.state.name = "SUCCEEDED"
        ex._client.get_job.return_value = mock_job
        ex._client.create_job.return_value = None

        handle = ex.submit(lambda: None, name="test")
        assert "osimflow-test" in handle.job_name
        ex.shutdown()

    def test_submit_failed_raises_runtime_error(self) -> None:
        ex = self._make_executor()
        mock_job = MagicMock()
        mock_job.status.state = ex._batch_v1.JobStatus.State.FAILED
        mock_job.status.status_details = "resource not found"
        ex._client.get_job.return_value = mock_job
        ex._client.create_job.return_value = None

        handle = ex.submit(lambda: None, name="fail")
        with pytest.raises(RuntimeError, match="failed"):
            handle.result(timeout=5)

    def test_wait_for_terminal_polls(self) -> None:
        ex = self._make_executor()
        mock_job_running = MagicMock()
        mock_job_running.status.state = ex._batch_v1.JobStatus.State.RUNNING
        mock_job_succeeded = MagicMock()
        mock_job_succeeded.status.state = ex._batch_v1.JobStatus.State.SUCCEEDED
        ex._client.get_job.side_effect = [mock_job_running, mock_job_succeeded]

        with patch("osimflow.executors.google_batch_executor.time.sleep"):
            job = ex._wait_for_terminal("test-job")
        assert job.status.state == ex._batch_v1.JobStatus.State.SUCCEEDED

    def test_build_environment(self) -> None:
        ex = self._make_executor()
        env = ex._build_environment(container="nrel/openstudio:3.11", openstudio_version="3.11.0")
        names = [e["name"] for e in env]
        assert "OSIMFLOW_OS_VERSION" in names
        assert "OSIMFLOW_CONTAINER" in names

    def test_build_environment_without_version(self) -> None:
        ex = self._make_executor()
        env = ex._build_environment(container=None, openstudio_version=None)
        names = [e["name"] for e in env]
        assert "OSIMFLOW_OS_VERSION" not in names
        assert "OSIMFLOW_CONTAINER" in names

    def test_shutdown_is_noop(self) -> None:
        ex = self._make_executor()
        ex.shutdown()

    def test_is_spot_interruption_preempted(self) -> None:
        ex = self._make_executor()
        assert ex._is_spot_interruption("instance was preempted") is True
        assert ex._is_spot_interruption("preempted VM") is True
        assert ex._is_spot_interruption("spot instance was preempted") is True

    def test_is_spot_interruption_non_spot(self) -> None:
        ex = self._make_executor()
        assert ex._is_spot_interruption("exit code 137") is False
        assert ex._is_spot_interruption("OOM killed") is False
        assert ex._is_spot_interruption(None) is False
        assert ex._is_spot_interruption("") is False


class TestGoogleBatchHandle:
    """_GoogleBatchHandle polls Google Cloud Batch on .result() and .done()."""

    def _make_handle(self, **kw: object) -> tuple[_GoogleBatchHandle, MagicMock]:
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._batch_v1 = MagicMock()
        ex.project_id = "test-project"
        ex.region = "us-central1"
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = False
        ex.fallback_to_on_demand = False
        ex.max_retries = 3
        ex._client = MagicMock()

        state = kw.get("state", "SUCCEEDED")
        mock_job = MagicMock()
        mock_job.status.state = getattr(ex._batch_v1.JobStatus.State, state)
        mock_job.status.status_details = kw.get("status_details", None)
        ex._client.get_job.return_value = mock_job

        submit_params = {
            "name": "test",
            "cpus": 1,
            "memory_mb": 1024,
            "time_min": 60,
            "environment": [],
        }
        handle = _GoogleBatchHandle(
            job_name="test-job",
            executor=ex,
            submit_params=submit_params,
        )
        return handle, ex._client

    def test_result_succeeded(self) -> None:
        handle, _ = self._make_handle(state="SUCCEEDED")
        assert handle.result() is None

    def test_result_succeeded_returns_result_hint(self) -> None:
        handle, _ = self._make_handle(state="SUCCEEDED")
        hint = Path("/tmp/osimflow/sim/0001")
        handle._result_hint = hint  # noqa: SLF001
        assert handle.result() == hint

    def test_result_failed_raises(self) -> None:
        handle, _ = self._make_handle(state="FAILED", status_details="resource not found")
        with pytest.raises(RuntimeError, match="failed"):
            handle.result()

    def test_done_succeeded(self) -> None:
        handle, _ = self._make_handle(state="SUCCEEDED")
        assert handle.done() is True

    def test_done_running(self) -> None:
        handle, _ = self._make_handle(state="RUNNING")
        assert handle.done() is False

    def test_done_api_error_returns_false(self) -> None:
        handle, mock_client = self._make_handle(state="RUNNING")
        mock_client.get_job.side_effect = Exception("network")
        assert handle.done() is False

    def test_spot_interruption_retries_and_succeeds(self) -> None:
        """When a preemptible VM interruption occurs, handle retries and succeeds."""
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._batch_v1 = MagicMock()
        ex.project_id = "test-project"
        ex.region = "us-central1"
        ex.batch_service_account = None
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = True
        ex.fallback_to_on_demand = False
        ex.max_retries = 3
        ex._client = MagicMock()
        ex._submit_job = MagicMock(return_value="osimflow-test")

        submit_params = {
            "name": "test",
            "cpus": 1,
            "memory_mb": 1024,
            "time_min": 60,
            "environment": [],
        }

        # First call: preempted, second call: success
        mock_job_preempted = MagicMock()
        mock_job_preempted.status.state = ex._batch_v1.JobStatus.State.FAILED
        mock_job_preempted.status.status_details = "instance was preempted"

        mock_job_success = MagicMock()
        mock_job_success.status.state = ex._batch_v1.JobStatus.State.SUCCEEDED

        ex._client.get_job.side_effect = [mock_job_preempted, mock_job_success]
        ex._client.create_job.return_value = None

        handle = _GoogleBatchHandle(
            job_name="test-job",
            executor=ex,
            submit_params=submit_params,
        )

        with patch("osimflow.executors.google_batch_executor.time.sleep"):
            result = handle.result()
        assert result is None

    def test_spot_retry_backoff_applies_jitter(self) -> None:
        """Spot retry sleeps a jittered duration, not the raw deterministic backoff (#1108)."""
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._batch_v1 = MagicMock()
        ex.project_id = "test-project"
        ex.region = "us-central1"
        ex.batch_service_account = None
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = True
        ex.fallback_to_on_demand = False
        ex.max_retries = 3
        ex._client = MagicMock()
        ex._submit_job = MagicMock(return_value="osimflow-test")

        submit_params = {
            "name": "test",
            "cpus": 1,
            "memory_mb": 1024,
            "time_min": 60,
            "environment": [],
        }

        mock_job_preempted = MagicMock()
        mock_job_preempted.status.state = ex._batch_v1.JobStatus.State.FAILED
        mock_job_preempted.status.status_details = "instance was preempted"

        mock_job_success = MagicMock()
        mock_job_success.status.state = ex._batch_v1.JobStatus.State.SUCCEEDED

        ex._client.get_job.side_effect = [mock_job_preempted, mock_job_success]
        ex._client.create_job.return_value = None

        handle = _GoogleBatchHandle(
            job_name="test-job",
            executor=ex,
            submit_params=submit_params,
        )

        sleep_durations: list[float] = []
        with (
            patch(
                "osimflow.executors.google_batch_executor.time.sleep",
                side_effect=sleep_durations.append,
            ),
            patch(
                "osimflow.executors.google_batch_executor.random.uniform",
                side_effect=lambda lo, hi: lo + (hi - lo) * 0.5,
            ),
        ):
            handle.result()

        # First attempt backoff = min(5 * 2**0, 60) = 5.0; full jitter at midpoint => 2.5.
        assert len(sleep_durations) >= 1
        assert sleep_durations[0] == pytest.approx(2.5)

    def test_spot_interruption_exhausted_retries_raises(self) -> None:
        """When preemptible retries are exhausted, raises RuntimeError."""
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._batch_v1 = MagicMock()
        ex.project_id = "test-project"
        ex.region = "us-central1"
        ex.batch_service_account = None
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = True
        ex.fallback_to_on_demand = False
        ex.max_retries = 1  # Only 1 retry
        ex._client = MagicMock()
        ex._submit_job = MagicMock(return_value="osimflow-test")

        submit_params = {
            "name": "test",
            "cpus": 1,
            "memory_mb": 1024,
            "time_min": 60,
            "environment": [],
        }

        # All calls: preempted
        mock_job_preempted = MagicMock()
        mock_job_preempted.status.state = ex._batch_v1.JobStatus.State.FAILED
        mock_job_preempted.status.status_details = "instance was preempted"

        ex._client.get_job.return_value = mock_job_preempted
        ex._client.create_job.return_value = None

        handle = _GoogleBatchHandle(
            job_name="test-job",
            executor=ex,
            submit_params=submit_params,
        )

        with patch("osimflow.executors.google_batch_executor.time.sleep"):
            with pytest.raises(RuntimeError, match="Spot retries exhausted"):
                handle.result()

    def test_spot_interruption_fallback_to_on_demand(self) -> None:
        """When fallback_to_on_demand is True, retries then falls back to on-demand."""
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._batch_v1 = MagicMock()
        ex.project_id = "test-project"
        ex.region = "us-central1"
        ex.batch_service_account = None
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = True
        ex.fallback_to_on_demand = True
        ex.max_retries = 1
        ex._client = MagicMock()
        ex._submit_job = MagicMock(return_value="osimflow-test")

        submit_params = {
            "name": "test",
            "cpus": 1,
            "memory_mb": 1024,
            "time_min": 60,
            "environment": [],
        }

        # First: preempted, second: on-demand success
        mock_job_preempted = MagicMock()
        mock_job_preempted.status.state = ex._batch_v1.JobStatus.State.FAILED
        mock_job_preempted.status.status_details = "instance was preempted"

        mock_job_on_demand = MagicMock()
        mock_job_on_demand.status.state = ex._batch_v1.JobStatus.State.SUCCEEDED

        ex._client.get_job.side_effect = [mock_job_preempted, mock_job_on_demand]
        ex._client.create_job.return_value = None

        handle = _GoogleBatchHandle(
            job_name="test-job",
            executor=ex,
            submit_params=submit_params,
        )

        with patch("osimflow.executors.google_batch_executor.time.sleep"):
            result = handle.result()
        assert result is None
