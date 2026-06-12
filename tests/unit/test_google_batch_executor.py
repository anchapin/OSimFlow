"""Unit tests for GoogleBatchExecutor (issue #254)."""

from __future__ import annotations

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
        ex._client = MagicMock()
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


class TestGoogleBatchHandle:
    """_GoogleBatchHandle polls Google Cloud Batch on .result() and .done()."""

    def _make_handle(self, **kw: object) -> tuple[_GoogleBatchHandle, MagicMock]:
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._batch_v1 = MagicMock()
        ex.region = "us-central1"
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex._client = MagicMock()

        state = kw.get("state", "SUCCEEDED")
        mock_job = MagicMock()
        mock_job.status.state = getattr(ex._batch_v1.JobStatus.State, state)
        ex._client.get_job.return_value = mock_job

        handle = _GoogleBatchHandle(job_name="test-job", executor=ex)
        return handle, ex._client

    def test_result_succeeded(self) -> None:
        handle, _ = self._make_handle(state="SUCCEEDED")
        assert handle.result() is None

    def test_result_failed_raises(self) -> None:
        handle, _ = self._make_handle(state="FAILED")
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
