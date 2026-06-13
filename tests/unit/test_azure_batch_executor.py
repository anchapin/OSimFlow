"""Unit tests for AzureBatchExecutor (issue #254, #352)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors.azure_batch_executor import (
    AzureBatchExecutor,
    _AzureBatchHandle,
)


class TestAzureBatchExecutor:
    """AzureBatchExecutor wraps the Azure Batch SDK."""

    def _make_executor(self, **kw: object) -> AzureBatchExecutor:
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex._azure_identity = MagicMock()
        ex._azure_mgmt_batch = MagicMock()
        ex.account_name = kw.get("account_name", "testaccount")
        ex.account_url = kw.get("account_url", "https://testaccount.eastus.batch.azure.com")
        ex.pool_id = kw.get("pool_id", "test-pool")
        ex.job_schedule_id = kw.get("job_schedule_id")
        ex.location = kw.get("location", "eastus")
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = kw.get("use_spot", False)
        ex.fallback_to_on_demand = kw.get("fallback_to_on_demand", False)
        ex.max_retries = kw.get("max_retries", 3)
        ex._client = MagicMock()
        return ex

    def test_name_attribute(self) -> None:
        ex = self._make_executor()
        assert ex.name == "azure_batch"

    def test_submit_succeeds(self) -> None:
        ex = self._make_executor()
        mock_job = MagicMock()
        mock_job.properties.execution_info.end_time = "2024-01-01T00:00:00Z"
        ex._client.job.get.return_value = mock_job
        ex._client.job.add.return_value = None
        ex._client.task.add.return_value = None

        handle = ex.submit(lambda: None, name="test")
        assert handle.job_id == "osimflow-test"
        ex.shutdown()

    def test_submit_failed_raises_runtime_error(self) -> None:
        ex = self._make_executor()
        mock_job = MagicMock()
        mock_job.properties.execution_info.end_time = "2024-01-01T00:00:00Z"
        mock_job.properties.execution_info.exit_code = 137
        ex._client.job.get.return_value = mock_job
        ex._client.job.add.return_value = None
        ex._client.task.add.return_value = None

        handle = ex.submit(lambda: None, name="fail")
        with pytest.raises(RuntimeError, match="exit code 137"):
            handle.result(timeout=5)

    def test_wait_for_terminal_polls(self) -> None:
        ex = self._make_executor()
        mock_job_running = MagicMock()
        mock_job_running.properties.execution_info.end_time = None
        mock_job_succeeded = MagicMock()
        mock_job_succeeded.properties.execution_info.end_time = "2024-01-01T00:00:00Z"
        ex._client.job.get.side_effect = [mock_job_running, mock_job_succeeded]

        with patch("osimflow.executors.azure_batch_executor.time.sleep"):
            job = ex._wait_for_terminal("test-job")
        assert job.properties.execution_info.end_time is not None

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

    def test_is_spot_interruption_spot_termination(self) -> None:
        ex = self._make_executor()
        assert ex._is_spot_interruption("SpotNodeTermination") is True
        assert ex._is_spot_interruption("Preemption") is True
        assert ex._is_spot_interruption("Preempted VM") is True
        assert ex._is_spot_interruption("low priority node was preempted") is True

    def test_is_spot_interruption_non_spot(self) -> None:
        ex = self._make_executor()
        assert ex._is_spot_interruption("exit code 137") is False
        assert ex._is_spot_interruption("OOM killed") is False
        assert ex._is_spot_interruption(None) is False
        assert ex._is_spot_interruption("") is False


class TestAzureBatchHandle:
    """_AzureBatchHandle polls Azure Batch on .result() and .done()."""

    def _make_handle(self, **kw: object) -> tuple[_AzureBatchHandle, MagicMock]:
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex._client = MagicMock()
        ex.account_name = "testaccount"
        ex.location = "eastus"
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = False
        ex.fallback_to_on_demand = False
        ex.max_retries = 3
        ex.pool_id = "test-pool"

        mock_job = MagicMock()
        mock_job.properties.execution_info.end_time = kw.get("end_time", "2024-01-01T00:00:00Z")
        mock_job.properties.execution_info.exit_code = kw.get("exit_code", None)
        mock_job.properties.execution_info.failure_reason = kw.get("failure_reason", None)
        ex._client.job.get.return_value = mock_job
        ex._client.job.add.return_value = None
        ex._client.task.add.return_value = None

        submit_params = {
            "name": "test",
            "cpus": 1,
            "memory_mb": 1024,
            "time_min": 60,
            "environment": [],
        }
        handle = _AzureBatchHandle(
            job_id="test-job",
            executor=ex,
            submit_params=submit_params,
        )
        if kw.get("end_time"):
            handle._future._completed = True
        return handle, ex._client

    def test_result_succeeded(self) -> None:
        handle, _ = self._make_handle()
        assert handle.result() is None

    def test_result_failed_raises(self) -> None:
        handle, _ = self._make_handle(exit_code=137)
        with pytest.raises(RuntimeError, match="exit code 137"):
            handle.result()

    def test_done_succeeded(self) -> None:
        handle, _ = self._make_handle()
        assert handle.done() is True

    def test_done_running(self) -> None:
        handle, _ = self._make_handle(end_time=None)
        assert handle.done() is False

    def test_done_api_error_returns_false(self) -> None:
        handle, mock_client = self._make_handle(end_time=None)
        mock_client.job.get.side_effect = Exception("network")
        assert handle.done() is False

    def test_spot_interruption_retries_and_succeeds(self) -> None:
        """When a Spot interruption occurs, handle retries and succeeds on second attempt."""
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex._client = MagicMock()
        ex.account_name = "testaccount"
        ex.location = "eastus"
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = True
        ex.fallback_to_on_demand = False
        ex.max_retries = 3
        ex.pool_id = "test-pool"

        submit_params = {
            "name": "test",
            "cpus": 1,
            "memory_mb": 1024,
            "time_min": 60,
            "environment": [],
        }

        # First call: spot interruption, second call: success
        mock_job_spot = MagicMock()
        mock_job_spot.properties.execution_info.end_time = "2024-01-01T00:00:00Z"
        mock_job_spot.properties.execution_info.exit_code = 137
        mock_job_spot.properties.execution_info.failure_reason = "SpotNodeTermination"

        mock_job_success = MagicMock()
        mock_job_success.properties.execution_info.end_time = "2024-01-01T00:00:01Z"
        mock_job_success.properties.execution_info.exit_code = 0

        ex._client.job.get.side_effect = [mock_job_spot, mock_job_success]
        ex._client.job.add.return_value = None
        ex._client.task.add.return_value = None

        handle = _AzureBatchHandle(
            job_id="test-job",
            executor=ex,
            submit_params=submit_params,
        )

        with patch("osimflow.executors.azure_batch_executor.time.sleep"):
            result = handle.result()
        assert result is None

    def test_spot_interruption_exhausted_retries_raises(self) -> None:
        """When Spot retries are exhausted, raises RuntimeError."""
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex._client = MagicMock()
        ex.account_name = "testaccount"
        ex.location = "eastus"
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = True
        ex.fallback_to_on_demand = False
        ex.max_retries = 1  # Only 1 retry
        ex.pool_id = "test-pool"

        submit_params = {
            "name": "test",
            "cpus": 1,
            "memory_mb": 1024,
            "time_min": 60,
            "environment": [],
        }

        # All calls: spot interruption
        mock_job_spot = MagicMock()
        mock_job_spot.properties.execution_info.end_time = "2024-01-01T00:00:00Z"
        mock_job_spot.properties.execution_info.exit_code = 137
        mock_job_spot.properties.execution_info.failure_reason = "SpotNodeTermination"

        ex._client.job.get.return_value = mock_job_spot
        ex._client.job.add.return_value = None
        ex._client.task.add.return_value = None

        handle = _AzureBatchHandle(
            job_id="test-job",
            executor=ex,
            submit_params=submit_params,
        )

        with patch("osimflow.executors.azure_batch_executor.time.sleep"):
            with pytest.raises(RuntimeError, match="Spot retries exhausted"):
                handle.result()

    def test_spot_interruption_fallback_to_on_demand(self) -> None:
        """When fallback_to_on_demand is True, retries then falls back to on-demand."""
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex._client = MagicMock()
        ex.account_name = "testaccount"
        ex.location = "eastus"
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = True
        ex.fallback_to_on_demand = True
        ex.max_retries = 1
        ex.pool_id = "test-pool"

        submit_params = {
            "name": "test",
            "cpus": 1,
            "memory_mb": 1024,
            "time_min": 60,
            "environment": [],
        }

        # First: spot interruption, second: on-demand success
        mock_job_spot = MagicMock()
        mock_job_spot.properties.execution_info.end_time = "2024-01-01T00:00:00Z"
        mock_job_spot.properties.execution_info.exit_code = 137
        mock_job_spot.properties.execution_info.failure_reason = "SpotNodeTermination"

        mock_job_on_demand = MagicMock()
        mock_job_on_demand.properties.execution_info.end_time = "2024-01-01T00:00:01Z"
        mock_job_on_demand.properties.execution_info.exit_code = 0

        ex._client.job.get.side_effect = [mock_job_spot, mock_job_on_demand]
        ex._client.job.add.return_value = None
        ex._client.task.add.return_value = None

        handle = _AzureBatchHandle(
            job_id="test-job",
            executor=ex,
            submit_params=submit_params,
        )

        with patch("osimflow.executors.azure_batch_executor.time.sleep"):
            result = handle.result()
        assert result is None
