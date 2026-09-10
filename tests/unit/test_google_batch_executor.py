"""Unit tests for GoogleBatchExecutor (issue #254, #352, #1676)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors.base import PollOutcome
from osimflow.executors.google_batch_executor import (
    GoogleBatchExecutor,
    _google_error_code,
    _GoogleBatchHandle,
)
from osimflow.executors.transport import ResultTransportConfig
from osimflow.task_payload_hmac import (
    RESULT_TRANSPORT_SIG_ENV,
    TASK_PAYLOAD_SECRET_ENV,
    TASK_PAYLOAD_SIG_ENV,
    sign_task_payload,
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

        with patch("osimflow.testing.patch_targets.time.sleep"):
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

    def test_build_environment_signs_task_payload_when_secret_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #1177/#1384: the Batch task env must carry the HMAC over payload bytes."""
        secret = "google-shared-secret"
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

        with patch("osimflow.testing.patch_targets.time.sleep"):
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
                "osimflow.testing.patch_targets.time.sleep",
                side_effect=sleep_durations.append,
            ),
            patch(
                "osimflow.testing.patch_targets.random.uniform",
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

        with patch("osimflow.testing.patch_targets.time.sleep"):
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

        with patch("osimflow.testing.patch_targets.time.sleep"):
            result = handle.result()
        assert result is None


class TestGoogleBatchHelperFunctions:
    """``_google_error_code`` (issue #1676 ratchet)."""

    def test_returns_status_code_when_set(self) -> None:
        """A Google API error with ``status_code`` returns it as an int."""

        class _ApiErr(Exception):
            status_code = 429

        assert _google_error_code(_ApiErr("rate limit")) == 429

    def test_returns_zero_when_status_code_is_none(self) -> None:
        """A non-API ``Exception`` (no ``status_code`` attr) returns 0."""

        class _Bare(Exception):
            pass

        assert _google_error_code(_Bare("weird")) == 0

    def test_swallows_attribute_lookup_exceptions(self) -> None:
        """``Exception`` whose ``status_code`` accessor raises returns 0."""

        class _BadStatus(Exception):
            @property
            def status_code(self) -> int:
                raise RuntimeError("nope")

        assert _google_error_code(_BadStatus("flap")) == 0


class TestGoogleBatchHandleHooks:
    """``_poll_job_id``, ``_classify``, ``_resubmit``, ``_submit_on_demand``,
    ``_cancel_job``, ``_failure_error``, ``_fallback_failure_error``,
    ``done()`` exception branches, ``_resolve_success_result``.

    The shared ``PollingHandle`` base owns ``result()`` (issue #1464),
    so the substrate hooks are the only Google-specific coverage.
    """

    @staticmethod
    def _make_executor(
        *,
        job_state: str = "SUCCEEDED",
        details: str | None = None,
    ) -> GoogleBatchExecutor:
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._batch_v1 = MagicMock()
        ex.project_id = "test-project"
        ex.region = "us-central1"
        ex.batch_service_account = None
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = False
        ex.fallback_to_on_demand = False
        ex.max_retries = 3
        ex._client = MagicMock()
        ex._submit_job = MagicMock(return_value="osimflow-resubmit")

        mock_job = MagicMock()
        mock_job.status.state = getattr(ex._batch_v1.JobStatus.State, job_state)
        mock_job.status.status_details = details
        ex._client.get_job.return_value = mock_job
        return ex

    def test_poll_job_id_returns_job_name(self) -> None:
        """``_poll_job_id`` returns the handle's job_name."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="abc/projects/p/locations/l/jobs/osimflow-x",
            executor=ex,
            submit_params={"name": "x", "cpus": 1, "memory_mb": 1024, "time_min": 60, "environment": []},
        )
        assert handle._poll_job_id() == "abc/projects/p/locations/l/jobs/osimflow-x"  # noqa: SLF001

    def test_classify_succeeded(self) -> None:
        """A SUCCEEDED job maps to ``PollOutcome.SUCCEEDED``."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="test-job",
            executor=ex,
            submit_params={},
        )
        outcome, reason = handle._classify(ex._client.get_job.return_value)  # noqa: SLF001
        assert outcome is PollOutcome.SUCCEEDED
        assert reason is None

    def test_classify_failed(self) -> None:
        """A FAILED job maps to ``PollOutcome.FAILED`` with the details reason."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="test-job",
            executor=ex,
            submit_params={},
        )
        mock_job = MagicMock()
        mock_job.status.state = ex._batch_v1.JobStatus.State.FAILED
        mock_job.status.status_details = "VM failed to start"
        outcome, reason = handle._classify(mock_job)  # noqa: SLF001
        assert outcome is PollOutcome.FAILED
        assert reason == "VM failed to start"

    def test_classify_running_is_indeterminate(self) -> None:
        """A RUNNING job (non-terminal) maps to ``PollOutcome.INDETERMINATE``."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="test-job",
            executor=ex,
            submit_params={},
        )
        mock_job = MagicMock()
        mock_job.status.state = ex._batch_v1.JobStatus.State.RUNNING
        outcome, reason = handle._classify(mock_job)  # noqa: SLF001
        assert outcome is PollOutcome.INDETERMINATE
        assert reason is None

    def test_resubmit_updates_handle_job_name(self) -> None:
        """``_resubmit`` delegates to ``_submit_job`` and updates ``job_name``/``worker_id``."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="old-job",
            executor=ex,
            submit_params={"name": "x"},
        )
        handle._resubmit()  # noqa: SLF001
        assert handle.job_name == "osimflow-resubmit"
        assert handle.worker_id == "osimflow-resubmit"
        ex._submit_job.assert_called_once_with(name="x")

    def test_submit_on_demand_sets_use_spot_false(self) -> None:
        """``_submit_on_demand`` calls ``_submit_job`` with ``use_spot=False``."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="old-job",
            executor=ex,
            submit_params={
                "name": "x",
                "cpus": 1,
                "memory_mb": 1024,
                "time_min": 60,
                "environment": [],
            },
        )
        handle._submit_on_demand()  # noqa: SLF001
        ex._submit_job.assert_called_once_with(
            name="x",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            environment=[],
            use_spot=False,
        )

    def test_cancel_job_calls_delete_job(self) -> None:
        """``_cancel_job`` issues ``BatchServiceClient.delete_job``."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="kill-me",
            executor=ex,
            submit_params={"name": "kill-me", "cpus": 1, "memory_mb": 1024, "time_min": 60, "environment": []},
        )
        assert handle._cancel_job() is True  # noqa: SLF001
        ex._client.delete_job.assert_called_once_with(name="kill-me")

    def test_failure_error_includes_job_name_and_status_details(self) -> None:
        """``_failure_error`` produces a ``RuntimeError`` with the job name + details."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="jobs/osimflow-x",
            executor=ex,
            submit_params={},
        )
        mock_job = MagicMock()
        mock_job.status.status_details = "OOM killed"
        err = handle._failure_error(mock_job)  # noqa: SLF001
        assert isinstance(err, RuntimeError)
        assert "jobs/osimflow-x" in str(err)
        assert "OOM killed" in str(err)

    def test_failure_error_with_no_status_details(self) -> None:
        """A FAILED job with ``status_details = None`` does not crash the formatter."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="jobs/osimflow-x",
            executor=ex,
            submit_params={},
        )
        mock_job = MagicMock()
        mock_job.status.status_details = None
        err = handle._failure_error(mock_job)  # noqa: SLF001
        assert "failed" in str(err)

    def test_fallback_failure_error_includes_state_and_details(self) -> None:
        """The fallback failure error names both the state and the status details."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="jobs/osimflow-x",
            executor=ex,
            submit_params={},
        )
        mock_job = MagicMock()
        mock_job.status.state = ex._batch_v1.JobStatus.State.FAILED
        mock_job.status.status_details = "spot retries exhausted"
        err = handle._fallback_failure_error(mock_job)  # noqa: SLF001
        assert "FAILED" in str(err)
        assert "spot retries exhausted" in str(err)

    def test_fallback_failure_error_with_no_status_details(self) -> None:
        """``None`` ``status_details`` falls back to ``"unknown reason"`` in the message."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="jobs/osimflow-x",
            executor=ex,
            submit_params={},
        )
        mock_job = MagicMock()
        mock_job.status.state = ex._batch_v1.JobStatus.State.FAILED
        mock_job.status.status_details = None
        err = handle._fallback_failure_error(mock_job)  # noqa: SLF001
        assert "unknown reason" in str(err)

    def test_resolve_success_result_delegates_to_transport_helpers(self) -> None:
        """``_resolve_success_result`` forwards the hint + transport to ``resolve_and_materialize``."""

        ex = self._make_executor()
        sentinel_hint = Path("/tmp/sentinel-google")
        cfg = ResultTransportConfig(
            mode="object_storage",
            backend="gcs",
            bucket="google-resolve-bucket",
        )
        handle = _GoogleBatchHandle(
            job_name="jobs/x",
            executor=ex,
            submit_params={},
            result_hint=sentinel_hint,
            transport=cfg,
        )
        with patch(
            "osimflow.executors.google_batch_executor.resolve_and_materialize",
            return_value="GB-RESOLVED",
        ) as patched:
            result = handle._resolve_success_result()  # noqa: SLF001
        assert result == "GB-RESOLVED"
        patched.assert_called_once_with(sentinel_hint, cfg)

    def test_done_returns_true_when_future_done(self) -> None:
        """``done()`` short-circuits when the local-mirror future is already set."""

        ex = self._make_executor()
        handle = _GoogleBatchHandle(
            job_name="jobs/quick",
            executor=ex,
            submit_params={},
        )
        handle._future.set_result(None)  # noqa: SLF001
        assert handle.done() is True

    def test_done_handles_timeout_error(self) -> None:
        """``TimeoutError`` from the probe is caught and done() returns False."""

        ex = self._make_executor()

        def _raise_timeout(*_args: object, **_kwargs: object) -> object:
            raise TimeoutError("API read timeout")

        ex._client.get_job.side_effect = _raise_timeout  # type: ignore[method-assign]
        handle = _GoogleBatchHandle(
            job_name="jobs/timeout",
            executor=ex,
            submit_params={},
        )
        assert handle.done() is False

    def test_done_handles_connection_error(self) -> None:
        """``ConnectionError`` from the probe is caught and done() returns False."""

        ex = self._make_executor()

        def _raise_conn(*_args: object, **_kwargs: object) -> object:
            raise ConnectionError("network blip")

        ex._client.get_job.side_effect = _raise_conn  # type: ignore[method-assign]
        handle = _GoogleBatchHandle(
            job_name="jobs/conn",
            executor=ex,
            submit_params={},
        )
        assert handle.done() is False

    def test_done_permanent_401_sets_exception_and_raises(self) -> None:
        """A Google API 401 sets the future exception and re-raises."""

        ex = self._make_executor()

        class _ApiErr(Exception):
            status_code = 401

        err = _ApiErr("auth failed")

        def _raise_auth(*_args: object, **_kwargs: object) -> object:
            raise err

        ex._client.get_job.side_effect = _raise_auth  # type: ignore[method-assign]
        handle = _GoogleBatchHandle(
            job_name="jobs/auth",
            executor=ex,
            submit_params={},
        )
        with pytest.raises(_ApiErr):
            handle.done()
        assert handle._future.exception() is err  # noqa: SLF001

    def test_done_permanent_404_sets_exception_and_raises(self) -> None:
        """A Google API 404 (job not found) sets the future exception and re-raises."""

        ex = self._make_executor()

        class _ApiErr(Exception):
            status_code = 404

        err = _ApiErr("job vanished")

        def _raise_missing(*_args: object, **_kwargs: object) -> object:
            raise err

        ex._client.get_job.side_effect = _raise_missing  # type: ignore[method-assign]
        handle = _GoogleBatchHandle(
            job_name="jobs/gone",
            executor=ex,
            submit_params={},
        )
        with pytest.raises(_ApiErr):
            handle.done()
        assert handle._future.exception() is err  # noqa: SLF001

    def test_done_transient_error_returns_false(self) -> None:
        """Any other (non-401/403/404) exception returns False — poll loop retries."""

        ex = self._make_executor()

        def _raise_weird(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("transient gcs hiccup")

        ex._client.get_job.side_effect = _raise_weird  # type: ignore[method-assign]
        handle = _GoogleBatchHandle(
            job_name="jobs/weird",
            executor=ex,
            submit_params={},
        )
        assert handle.done() is False
        # Future must NOT be poisoned — transient errors do not finalize the handle.
        assert handle._future.done() is False  # noqa: SLF001


class TestGoogleBatchExecutorBuildEnvironment:
    """``GoogleBatchExecutor._build_environment`` covers the per-call env emission:

    * ``OSIMFLOW_OS_VERSION`` + ``OSIMFLOW_CONTAINER`` resolution (with
      ``_container_digest`` precedence, issue #1081)
    * the literal-secret / no-secret-name warning trio (issue #1633)
    * ``OSIMFLOW_RESULT_*`` transport env vars + their HMAC signature
    * ``OSIMFLOW_STUB_SIM`` propagation
    """

    def _make_executor(self, **kw: object) -> GoogleBatchExecutor:
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._batch_v1 = MagicMock()
        ex.project_id = "test-project"
        ex.region = "us-central1"
        ex.batch_service_account = None
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = kw.get("use_spot", False)
        ex.fallback_to_on_demand = kw.get("fallback_to_on_demand", False)
        ex.max_retries = 3
        ex._client = MagicMock()
        ex._container_digest = kw.get("_container_digest", None)
        ex.payload_secret_name = kw.get("payload_secret_name", None)
        return ex

    def test_build_environment_uses_container_digest_when_pinned(self) -> None:
        """Issue #1081: ``_container_digest`` wins over the mutable ``container`` value."""

        ex = self._make_executor(_container_digest="nrel/openstudio@sha256:abc123")
        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
        )
        env_map = {e["name"]: e["value"] for e in env}
        assert env_map["OSIMFLOW_CONTAINER"] == "nrel/openstudio@sha256:abc123"

    def test_build_environment_falls_back_to_container_when_no_digest(self) -> None:
        """When ``_container_digest`` is unset, the ``container`` value is used."""

        ex = self._make_executor(_container_digest=None)
        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
        )
        env_map = {e["name"]: e["value"] for e in env}
        assert env_map["OSIMFLOW_CONTAINER"] == "nrel/openstudio:3.11.0"

    def test_build_environment_emits_os_version_when_set(self) -> None:
        """``OSIMFLOW_OS_VERSION`` is emitted only when ``openstudio_version`` is set."""

        ex = self._make_executor()
        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
        )
        env_map = {e["name"]: e["value"] for e in env}
        assert env_map["OSIMFLOW_OS_VERSION"] == "3.11.0"

    def test_build_environment_emits_result_transport_env_vars(self) -> None:
        """``_build_environment`` writes ``OSIMFLOW_RESULT_*`` for the transport."""

        ex = self._make_executor()
        cfg = ResultTransportConfig(
            mode="object_storage",
            backend="gcs",
            bucket="gb-bucket",
            prefix="campaign-y",
            endpoint="https://storage.googleapis.com",
        )
        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            transport=cfg,
        )
        env_map = {e["name"]: e["value"] for e in env}
        assert env_map["OSIMFLOW_RESULT_TRANSPORT_MODE"] == "object_storage"
        assert env_map["OSIMFLOW_RESULT_STORAGE_BACKEND"] == "gcs"
        assert env_map["OSIMFLOW_RESULT_STORAGE_BUCKET"] == "gb-bucket"
        assert env_map["OSIMFLOW_RESULT_STORAGE_PREFIX"] == "campaign-y"
        assert env_map["OSIMFLOW_RESULT_STORAGE_ENDPOINT"] == "https://storage.googleapis.com"

    def test_build_environment_emits_transport_signature_when_secret_set(self) -> None:
        """The ``OSIMFLOW_RESULT_TRANSPORT_SIG`` env var is emitted when a secret is set.

        The HMAC signature is rebuilt from the canonical
        :func:`canonical_result_transport_settings` for the same transport
        fields, so the emitted value matches a fresh signing call.
        """

        secret = "google-shared-secret"
        cfg = ResultTransportConfig(
            mode="object_storage",
            backend="gcs",
            bucket="gb-sig-bucket",
            endpoint="https://storage.googleapis.com",
        )
        ex = self._make_executor()
        with patch.dict(os.environ, {TASK_PAYLOAD_SECRET_ENV: secret}):
            env = ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload='{"step":"sim"}',
                transport=cfg,
            )
        env_map = {e["name"]: e["value"] for e in env}
        assert RESULT_TRANSPORT_SIG_ENV in env_map
        # The canonical JSON shape from ``canonical_result_transport_settings``:
        # sorted keys, no spaces, ``None`` → ``null``, ``allow_insecure`` bool.
        canonical = (
            '{"allow_insecure":false,"backend":"gcs","bucket":"gb-sig-bucket",'
            '"endpoint":"https://storage.googleapis.com","mode":"object_storage",'
            '"prefix":null}'
        )
        assert env_map[RESULT_TRANSPORT_SIG_ENV] == sign_task_payload(canonical, secret)

    def test_build_environment_with_payload_secret_name_no_orchestrator_secret(
        self,
    ) -> None:
        """``payload_secret_name`` set but no orchestrator secret → unsigned warning.

        Issue #1633: the executor logs the missing-secret warning when a
        Secret Manager secret name is referenced but no
        ``OSIMFLOW_TASK_PAYLOAD_SECRET`` env var is set on the orchestrator.
        """

        secret_name = "gb-hmac-secret"
        ex = self._make_executor(payload_secret_name=secret_name)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(TASK_PAYLOAD_SECRET_ENV, None)
            env = ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload='{"step":"sim"}',
            )
        # No literal secret in the env (delivery is via secret_variables
        # at submit time, not via the env list).
        env_map = {e["name"]: e["value"] for e in env}
        assert TASK_PAYLOAD_SECRET_ENV not in env_map
        assert TASK_PAYLOAD_SIG_ENV not in env_map

    def test_build_environment_warns_on_literal_secret_when_no_secret_name(
        self,
    ) -> None:
        """When ``payload_secret_name`` is unset but the orchestrator has a secret,
        the executor logs the literal-secret warning (issue #1633) and ships
        the raw secret in the env (legacy unsafe path)."""

        ex = self._make_executor(payload_secret_name=None)
        secret = "gb-orch-shared-secret"
        with patch.dict(os.environ, {TASK_PAYLOAD_SECRET_ENV: secret}):
            env = ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload='{"step":"sim"}',
            )
        env_map = {e["name"]: e["value"] for e in env}
        assert env_map[TASK_PAYLOAD_SECRET_ENV] == secret
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload('{"step":"sim"}', secret)


class TestGoogleBatchExecutorInternals:
    """``_is_spot_interruption``, ``_get_client``, ``_get_job``, ``_submit_job``,
    ``shutdown``, ``_do_submit`` integration with the transport contract.
    """

    def _make_executor(
        self,
        *,
        use_spot: bool = False,
        fallback_to_on_demand: bool = False,
        max_retries: int = 3,
        payload_secret_name: str | None = None,
    ) -> GoogleBatchExecutor:
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._batch_v1 = MagicMock()
        ex.project_id = "test-project"
        ex.region = "us-central1"
        ex.batch_service_account = None
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = use_spot
        ex.fallback_to_on_demand = fallback_to_on_demand
        ex.max_retries = max_retries
        ex._client = MagicMock()
        ex._container_digest = None
        ex.payload_secret_name = payload_secret_name
        return ex

    def test_is_spot_interruption_each_marker(self) -> None:
        """Each marker in ``_SPOT_INTERRUPTION_MARKERS`` triggers a True result."""

        ex = self._make_executor()
        for marker in (
            "preempted",
            "preempt",
            "spot",
            "instance was preempted",
        ):
            assert ex._is_spot_interruption(marker) is True  # noqa: SLF001

    def test_is_spot_interruption_substring_match(self) -> None:
        """The check is substring-based: a longer reason containing the marker counts."""

        ex = self._make_executor()
        assert ex._is_spot_interruption("VM was preempted due to spot reclaim") is True  # noqa: SLF001

    def test_is_spot_interruption_empty_returns_false(self) -> None:
        """Empty / falsy strings return False."""

        ex = self._make_executor()
        assert ex._is_spot_interruption("") is False  # noqa: SLF001
        assert ex._is_spot_interruption(None) is False  # noqa: SLF001

    def test_get_client_lazy_constructs(self) -> None:
        """``_get_client`` constructs the BatchServiceClient lazily."""

        ex = self._make_executor()
        ex._client = None
        with patch.object(ex._batch_v1, "BatchServiceClient") as client_cls:
            client_cls.return_value = MagicMock(name="client")
            client = ex._get_client()  # noqa: SLF001
        assert client is not None
        client_cls.assert_called_once_with()

    def test_get_client_returns_cached_instance(self) -> None:
        """Subsequent calls return the same cached client."""

        ex = self._make_executor()
        cached = MagicMock()
        ex._client = cached
        with patch.object(ex._batch_v1, "BatchServiceClient") as client_cls:
            client = ex._get_client()  # noqa: SLF001
        assert client is cached
        client_cls.assert_not_called()

    def test_get_job_delegates_to_client(self) -> None:
        """``_get_job`` calls the cached client with the supplied job_name."""

        ex = self._make_executor()
        ex._client.get_job.return_value = MagicMock(
            status=MagicMock(state=ex._batch_v1.JobStatus.State.SUCCEEDED)
        )
        result = ex._get_job("projects/p/locations/l/jobs/j")  # noqa: SLF001
        ex._client.get_job.assert_called_once_with(
            "projects/p/locations/l/jobs/j"
        )
        assert result is not None

    def test_submit_job_happy_path_constructs_full_payload(self) -> None:
        """``_submit_job`` builds the full Job payload via the ``_batch_v1`` schema."""

        ex = self._make_executor()
        env = [
            {"name": "OSIMFLOW_CONTAINER", "value": "nrel/openstudio:3.11.0"},
        ]
        result = ex._submit_job(  # noqa: SLF001
            name="unit-test-job",
            cpus=2,
            memory_mb=2048,
            time_min=10,
            environment=env,
        )
        # The job name is the full resource path.
        assert result == (
            "projects/test-project/locations/us-central1/jobs/osimflow-unit-test-job"
        )
        # The BatchServiceClient.create_job was called.
        ex._client.create_job.assert_called_once()

    def test_submit_job_with_spot_sets_preemptible_scheduling(self) -> None:
        """``use_spot=True`` populates ``scheduling.preemptible=True`` on the instance policy."""

        ex = self._make_executor(use_spot=True)
        env = [
            {"name": "OSIMFLOW_CONTAINER", "value": "nrel/openstudio:3.11.0"},
        ]
        ex._submit_job(  # noqa: SLF001
            name="spot-job",
            cpus=1,
            memory_mb=1024,
            time_min=10,
            environment=env,
        )
        ex._client.create_job.assert_called_once()

    def test_submit_job_overrides_use_spot_with_explicit_kwarg(self) -> None:
        """An explicit ``use_spot=False`` overrides the executor's default."""

        ex = self._make_executor(use_spot=True)
        env = [
            {"name": "OSIMFLOW_CONTAINER", "value": "nrel/openstudio:3.11.0"},
        ]
        ex._submit_job(  # noqa: SLF001
            name="no-spot",
            cpus=1,
            memory_mb=1024,
            time_min=10,
            environment=env,
            use_spot=False,
        )
        ex._client.create_job.assert_called_once()

    def test_submit_job_with_payload_secret_name_emits_secret_variables(self) -> None:
        """``payload_secret_name`` causes the job spec to carry ``environment.secret_variables``.

        The raw HMAC secret is delivered out-of-band via the
        ``environment.secret_variables`` block — the Batch agent resolves
        it through Secret Manager at task start.
        """

        ex = self._make_executor(payload_secret_name="gb-payload-secret")
        env = [
            {"name": "OSIMFLOW_CONTAINER", "value": "nrel/openstudio:3.11.0"},
        ]
        ex._submit_job(  # noqa: SLF001
            name="secret-job",
            cpus=1,
            memory_mb=1024,
            time_min=10,
            environment=env,
        )
        # The ``TaskSpec`` payload carries the ``environment`` mapping with
        # both the user-visible variables block and the secret_variables block.
        task_spec_kwargs = ex._batch_v1.TaskSpec.call_args.kwargs  # type: ignore[attr-defined]
        environment_map = task_spec_kwargs["environment"]
        assert "secret_variables" in environment_map
        assert environment_map["secret_variables"] == {
            TASK_PAYLOAD_SECRET_ENV: "gb-payload-secret"
        }

    def test_submit_job_custom_command_overrides_default(self) -> None:
        """A caller-supplied ``command`` is plumbed into the ``ContainerSpec``."""

        ex = self._make_executor()
        env = [
            {"name": "OSIMFLOW_CONTAINER", "value": "nrel/openstudio:3.11.0"},
        ]
        ex._submit_job(  # noqa: SLF001
            name="cmd-job",
            cpus=1,
            memory_mb=1024,
            time_min=10,
            environment=env,
            command=["/bin/sh", "-c", "echo hi"],
        )
        ex._client.create_job.assert_called_once()
        # The ContainerSpec receives the explicit command.
        container_spec_kwargs = ex._batch_v1.ContainerSpec.call_args.kwargs  # type: ignore[attr-defined]
        assert container_spec_kwargs["command"] == ["/bin/sh", "-c", "echo hi"]

    def test_shutdown_is_noop(self) -> None:
        """``shutdown`` is a documented no-op for the Batch substrate."""

        ex = self._make_executor()
        ex.shutdown()  # Should not raise.

    def test_attributes(self) -> None:
        """The class attributes that govern substrate dispatch are correct."""

        ex = self._make_executor()
        assert ex.name == "google_batch"  # noqa: SLF001
        assert ex.requires_remote_runner_payload is True  # noqa: SLF001
        assert ex.signs_task_payload is True  # noqa: SLF001

    def test_dispatcher_attributes(self) -> None:
        """The dispatcher attributes are set per the Google Batch substrate."""

        ex = self._make_executor()
        assert ex.supports_spot_market is True  # noqa: SLF001
        assert ex.default_submit_rps == 10.0  # noqa: SLF001

    def test_container_digest_overrides_container_for_osimflow_container(self) -> None:
        """``_build_environment`` should emit ``_container_digest`` as the OSIMFLOW_CONTAINER value.

        This is a tighter assertion than the prior fallback test — it
        confirms the documented precedence (issue #1081).
        """

        ex = self._make_executor()
        ex._container_digest = "nrel/openstudio@sha256:deadbeef"  # noqa: SLF001
        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
        )
        env_map = {e["name"]: e["value"] for e in env}
        assert env_map["OSIMFLOW_CONTAINER"] == "nrel/openstudio@sha256:deadbeef"

    def test_constructor_runs_via_real_init(self) -> None:
        """``GoogleBatchExecutor.__init__`` exercises the documented attribute setup.

        Most of the executor's tests bypass ``__init__`` via
        ``__new__`` + attribute injection (no real construction);
        this test calls the real ``__init__`` with the
        ``google.cloud.batch_v1`` import mocked out so every
        attribute assignment + the rate-limiter init runs.
        """

        fake_batch_v1 = MagicMock(name="google.cloud.batch_v1")
        with patch.dict(
            "sys.modules",
            {"google.cloud": MagicMock(batch_v1=fake_batch_v1)},
        ):
            ex = GoogleBatchExecutor(
                project_id="boot-test-project",
                region="europe-west1",
                poll_interval_s=3.0,
                max_poll_interval_s=20.0,
                use_spot=True,
                fallback_to_on_demand=True,
                max_retries=7,
                submit_rps=2.5,
                payload_secret_name="boot-payload-secret",
            )
        assert ex._batch_v1 is fake_batch_v1  # noqa: SLF001
        assert ex.project_id == "boot-test-project"  # noqa: SLF001
        assert ex.region == "europe-west1"  # noqa: SLF001
        assert ex.poll_interval_s == 3.0  # noqa: SLF001
        assert ex.max_poll_interval_s == 20.0  # noqa: SLF001
        assert ex.use_spot is True  # noqa: SLF001
        assert ex.fallback_to_on_demand is True  # noqa: SLF001
        assert ex.max_retries == 7  # noqa: SLF001
        assert ex.payload_secret_name == "boot-payload-secret"  # noqa: SLF001
        assert ex._container_digest is None  # noqa: SLF001
        assert ex._client is None  # noqa: SLF001
        # Rate limiter was initialised.
        assert ex._rate_limiter is not None  # noqa: SLF001

    def test_build_environment_propagates_stub_sim(self) -> None:
        """``OSIMFLOW_STUB_SIM`` from the orchestrator env is propagated to the Batch env."""

        ex = self._make_executor()
        with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            env = ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
            )
        env_map = {e["name"]: e["value"] for e in env}
        assert env_map["OSIMFLOW_STUB_SIM"] == "1"

    def test_build_environment_skips_unspecified_transport_fields(self) -> None:
        """``_build_environment`` emits only the transport fields that are set.

        The per-field ``if .is not None`` branches in the executor must
        each be reachable with a None field value so the env list
        stays minimal when the caller leaves a field unset.
        """

        ex = self._make_executor()
        cfg = ResultTransportConfig(
            mode="auto",
            backend=None,
            bucket=None,
            prefix=None,
            endpoint=None,
        )
        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            transport=cfg,
        )
        env_names = [e["name"] for e in env]
        # Mode is always emitted; per-field storage vars are not.
        assert "OSIMFLOW_RESULT_TRANSPORT_MODE" in env_names
        assert "OSIMFLOW_RESULT_STORAGE_BACKEND" not in env_names
        assert "OSIMFLOW_RESULT_STORAGE_BUCKET" not in env_names
        assert "OSIMFLOW_RESULT_STORAGE_PREFIX" not in env_names
        assert "OSIMFLOW_RESULT_STORAGE_ENDPOINT" not in env_names

    def test_build_environment_contract_version(self) -> None:
        """``OSIMFLOW_CONTRACT_VERSION`` is emitted alongside the task payload."""

        from osimflow.byos_contract import BYOS_CONTRACT_VERSION

        ex = self._make_executor()
        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload='{"step":"sim"}',
        )
        env_map = {e["name"]: e["value"] for e in env}
        assert env_map["OSIMFLOW_CONTRACT_VERSION"] == BYOS_CONTRACT_VERSION
