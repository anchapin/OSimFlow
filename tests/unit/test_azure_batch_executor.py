"""Unit tests for AzureBatchExecutor (issue #254, #352, #1396)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors.azure_batch_executor import (
    _THROTTLE_ERROR_CODES,
    AzureBatchExecutor,
    _azure_throttle_code,
    _AzureBatchHandle,
    _retry_azure_submit,
)
from osimflow.task_payload_hmac import (
    TASK_PAYLOAD_SECRET_ENV,
    TASK_PAYLOAD_SIG_ENV,
    sign_task_payload,
)


class _FakeBatchError(Exception):
    """Duck-typed stand-in for ``azure.batch.models.BatchErrorException``.

    Carries the ``.error.code`` attribute the retry helper reads. We
    avoid importing the real SDK so these tests run in environments
    where ``azure-batch`` isn't installed.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"azure batch error code={code}")
        self.error = MagicMock()
        self.error.code = code


class TestAzureBatchExecutor:
    """AzureBatchExecutor wraps the Azure Batch SDK."""

    def _make_executor(self, **kw: object) -> AzureBatchExecutor:
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex._azure_identity = MagicMock()
        ex._azure_batch = MagicMock()
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

    def test_build_environment_signs_task_payload_when_secret_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #1177/#1384: the Batch task env must carry the HMAC over payload bytes."""
        secret = "azure-shared-secret"
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, secret)
        task_payload = json.dumps({"step": "sim", "args": [], "kwargs": {}})
        ex = self._make_executor()
        env = ex._build_environment(
            container="nrel/openstudio:3.11",
            openstudio_version="3.11.0",
            task_payload=task_payload,
        )
        env_map = {e["name"]: e["value"] for e in env}
        assert env_map["OSIMFLOW_TASK_PAYLOAD"] == task_payload
        assert env_map[TASK_PAYLOAD_SECRET_ENV] == secret
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(task_payload, secret)

    def test_build_environment_omits_signature_env_without_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy unsigned mode must leave the Batch task env unchanged (issue #1177)."""
        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        task_payload = json.dumps({"step": "sim", "args": [], "kwargs": {}})
        ex = self._make_executor()
        env = ex._build_environment(
            container="nrel/openstudio:3.11",
            openstudio_version="3.11.0",
            task_payload=task_payload,
        )
        env_map = {e["name"]: e["value"] for e in env}
        assert env_map["OSIMFLOW_TASK_PAYLOAD"] == task_payload
        assert TASK_PAYLOAD_SIG_ENV not in env_map
        assert TASK_PAYLOAD_SECRET_ENV not in env_map

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

    def test_result_succeeded_returns_result_hint(self) -> None:
        handle, _ = self._make_handle()
        hint = Path("/tmp/osimflow/kpis/kpi_0001.json")
        handle._result_hint = hint  # noqa: SLF001
        assert handle.result() == hint

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
        ex._azure_batch = MagicMock()
        ex._azure_identity = MagicMock()
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

    def test_spot_retry_backoff_applies_jitter(self) -> None:
        """Spot retry sleeps a jittered duration, not the raw deterministic backoff (#1108)."""
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex._client = MagicMock()
        ex._azure_batch = MagicMock()
        ex._azure_identity = MagicMock()
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

        sleep_durations: list[float] = []
        with (
            patch(
                "osimflow.executors.azure_batch_executor.time.sleep",
                side_effect=sleep_durations.append,
            ),
            patch(
                "osimflow.executors.azure_batch_executor.random.uniform",
                side_effect=lambda lo, hi: lo + (hi - lo) * 0.5,
            ),
        ):
            handle.result()

        # First attempt backoff = min(5 * 2**0, 60) = 5.0; full jitter at midpoint => 2.5.
        assert len(sleep_durations) >= 1
        assert sleep_durations[0] == pytest.approx(2.5)

    def test_spot_interruption_exhausted_retries_raises(self) -> None:
        """When Spot retries are exhausted, raises RuntimeError."""
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex._client = MagicMock()
        ex._azure_batch = MagicMock()
        ex._azure_identity = MagicMock()
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
        ex._azure_batch = MagicMock()
        ex._azure_identity = MagicMock()
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


class TestAzureBatchThrottleRetry:
    """Throttle / network retry on client.job.add and client.task.add (issue #1396)."""

    def test_throttle_error_codes_contains_expected_codes(self) -> None:
        """The documented Azure Batch throttle set is locked in (issue #1396)."""
        assert "TooManyRequests" in _THROTTLE_ERROR_CODES
        assert "ServerBusy" in _THROTTLE_ERROR_CODES
        assert "RequestTimeout" in _THROTTLE_ERROR_CODES
        assert isinstance(_THROTTLE_ERROR_CODES, frozenset)

    def test_azure_throttle_code_recognizes_throttle(self) -> None:
        """Recognizes BatchErrorException with a throttle code."""
        assert _azure_throttle_code(_FakeBatchError("TooManyRequests")) == "TooManyRequests"
        assert _azure_throttle_code(_FakeBatchError("ServerBusy")) == "ServerBusy"
        assert _azure_throttle_code(_FakeBatchError("RequestTimeout")) == "RequestTimeout"

    def test_azure_throttle_code_ignores_non_throttle(self) -> None:
        """Non-throttle BatchErrorException codes return None (no retry)."""
        assert _azure_throttle_code(_FakeBatchError("AuthenticationFailed")) is None
        assert _azure_throttle_code(_FakeBatchError("NotFound")) is None
        assert _azure_throttle_code(RuntimeError("boom")) is None

    def test_retry_returns_success_after_transient_throttle(self) -> None:
        """Regression: a transient 429 (TooManyRequests) is retried, second attempt wins (issue #1396)."""
        call_count = {"n": 0}

        def flaky() -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _FakeBatchError("TooManyRequests")
            return "ok"

        with patch("osimflow.executors.azure_batch_executor.time.sleep"):
            result = _retry_azure_submit(flaky)

        assert result == "ok"
        assert call_count["n"] == 2

    def test_retry_handles_each_throttle_code(self) -> None:
        """All three documented throttle codes trigger a retry, then succeed."""
        for code in ("TooManyRequests", "ServerBusy", "RequestTimeout"):

            def make_flaky(throttle_code: str) -> Callable[[], str]:
                state = {"n": 0}

                def flaky() -> str:
                    state["n"] += 1
                    if state["n"] == 1:
                        raise _FakeBatchError(throttle_code)
                    return "ok"

                return flaky

            flaky_fn = make_flaky(code)
            with patch("osimflow.executors.azure_batch_executor.time.sleep"):
                result = _retry_azure_submit(flaky_fn)

            assert result == "ok"

    def test_retry_exhausts_after_max_attempts(self) -> None:
        """Persistent throttle exhausts retries and re-raises the last exception."""
        always_fail = MagicMock(side_effect=_FakeBatchError("TooManyRequests"))

        with patch("osimflow.executors.azure_batch_executor.time.sleep"):
            with pytest.raises(_FakeBatchError, match="TooManyRequests"):
                _retry_azure_submit(always_fail, max_attempts=5)

        assert always_fail.call_count == 5

    def test_retry_does_not_swallow_permanent_errors(self) -> None:
        """Non-throttle exceptions propagate immediately without retry."""
        permanent = MagicMock(side_effect=RuntimeError("not throttled"))

        with patch("osimflow.executors.azure_batch_executor.time.sleep") as sleep_mock:
            with pytest.raises(RuntimeError, match="not throttled"):
                _retry_azure_submit(permanent)

        assert permanent.call_count == 1
        assert sleep_mock.call_count == 0

    def test_retry_caps_total_backoff_seconds(self) -> None:
        """The cap argument bounds the per-attempt jitter sleep."""
        sleeps: list[float] = []

        def fail_then_succeed() -> str:
            if sleeps:
                return "ok"
            raise _FakeBatchError("TooManyRequests")

        with patch(
            "osimflow.executors.azure_batch_executor.time.sleep",
            side_effect=sleeps.append,
        ):
            _retry_azure_submit(
                fail_then_succeed,
                max_attempts=5,
                total_cap_seconds=2.0,
            )

        for duration in sleeps:
            assert duration <= 2.0

    def test_retry_logs_warning_on_each_throttle(self) -> None:
        """Each retry emits a log.warning carrying the throttle code (issue #1396)."""
        call_count = {"n": 0}

        def flaky() -> str:
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise _FakeBatchError("ServerBusy")
            return "ok"

        with (
            patch("osimflow.executors.azure_batch_executor.time.sleep"),
            patch("osimflow.executors.azure_batch_executor.log") as mock_log,
        ):
            _retry_azure_submit(flaky)

        assert call_count["n"] == 3
        warning_codes = [
            call.args[0]
            for call in mock_log.warning.call_args_list
            if call.args and "ServerBusy" in str(call.args)
        ]
        assert len(warning_codes) == 2

    def test_submit_retries_on_throttled_job_add(self) -> None:
        """Regression: AzureBatchExecutor._submit_job retries a throttled client.job.add (issue #1396)."""
        ex = self._executor_with_throttle([_FakeBatchError("TooManyRequests"), None])

        with patch("osimflow.executors.azure_batch_executor.time.sleep"):
            job_id = ex._submit_job(
                name="throttled-job",
                cpus=1,
                memory_mb=1024,
                time_min=60,
                environment=[],
            )

        assert job_id == "osimflow-throttled-job"
        assert ex._client.job.add.call_count == 2
        assert ex._client.task.add.call_count == 1

    def test_submit_propagates_after_throttle_exhaustion(self) -> None:
        """If every retry is throttled, the throttle exception propagates (issue #1396)."""
        ex = self._executor_with_throttle(_FakeBatchError("TooManyRequests"))

        with (
            patch("osimflow.executors.azure_batch_executor.time.sleep"),
            pytest.raises(_FakeBatchError, match="TooManyRequests"),
        ):
            ex._submit_job(
                name="perma-throttle",
                cpus=1,
                memory_mb=1024,
                time_min=60,
                environment=[],
            )

        assert ex._client.job.add.call_count == 5

    def _executor_with_throttle(
        self, exc: Exception | list[Exception | None]
    ) -> AzureBatchExecutor:
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex._azure_batch = MagicMock()
        ex._azure_identity = MagicMock()
        ex.account_name = "testaccount"
        ex.account_url = "https://testaccount.eastus.batch.azure.com"
        ex.pool_id = "test-pool"
        ex.location = "eastus"
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = False
        ex.fallback_to_on_demand = False
        ex.max_retries = 3
        ex._client = MagicMock()
        ex._client.job.add.side_effect = exc
        ex._client.task.add.return_value = None
        return ex
