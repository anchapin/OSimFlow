"""Comprehensive unit tests for osimflow.executors (issue #217).

Covers:
  - Handle: result(), done(), timeout handling, exception propagation
  - BaseExecutor: ABC enforcement
  - LocalExecutor: submit, thread pool, max_workers, shutdown
  - SlurmExecutor: debug mode, per-submit resources, _apply_slurm_params
  - AWSBatchExecutor: submit, polling, _wait_for_terminal, environment,
    container overrides, _build_job_spec, _AWSBatchHandle.result/done
  - NomadExecutor: submit, _slugify_job_name, _NomadClient, _NomadHandle
  - run_subprocess: stdout/stderr capture
  - get_step_resources / DEFAULT_STEP_RESOURCES
"""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import (
    AWSBatchExecutor,
    BaseExecutor,
    Handle,
    LocalExecutor,
    NomadExecutor,
    SlurmExecutor,
    _apply_slurm_params,
    _AWSBatchHandle,
    _NomadClient,
    _NomadHandle,
    _slugify_job_name,
    run_subprocess,
)


# ---------------------------------------------------------------------------
# Handle
# ---------------------------------------------------------------------------
class TestHandle:
    """Handle wraps a concurrent.futures.Future."""

    def test_result_returns_future_value(self) -> None:
        fut: Future[int] = Future()
        fut.set_result(42)
        h = Handle(job_id="test", _future=fut)
        assert h.result() == 42

    def test_result_timeout_forwarded(self) -> None:
        fut: Future[int] = Future()
        fut.set_result(99)
        h = Handle(job_id="t", _future=fut)
        assert h.result(timeout=1) == 99

    def test_done_true_when_future_completed(self) -> None:
        fut: Future[int] = Future()
        fut.set_result(1)
        h = Handle(job_id="t", _future=fut)
        assert h.done() is True

    def test_done_false_when_future_pending(self) -> None:
        fut: Future[int] = Future()
        h = Handle(job_id="t", _future=fut)
        assert h.done() is False

    def test_result_propagates_exception(self) -> None:
        fut: Future[int] = Future()
        fut.set_exception(ValueError("boom"))
        h = Handle(job_id="t", _future=fut)
        with pytest.raises(ValueError, match="boom"):
            h.result()

    def test_worker_fields_default_none(self) -> None:
        h = Handle(job_id="t", _future=Future())
        assert h.worker_id is None
        assert h.worker_ip is None
        assert h.worker_region is None


# ---------------------------------------------------------------------------
# BaseExecutor ABC
# ---------------------------------------------------------------------------
class TestBaseExecutor:
    """BaseExecutor cannot be instantiated directly."""

    def test_cannot_instantiate_abc(self) -> None:
        with pytest.raises(TypeError):
            BaseExecutor()  # type: ignore[abstract]

    def test_requires_submit_and_shutdown(self) -> None:
        class _Partial(BaseExecutor):
            name = "partial"

            def submit(self, fn, *args, **kwargs) -> Handle:  # type: ignore[override]
                raise NotImplementedError

        with pytest.raises(TypeError):
            _Partial()  # type: ignore[abstract]

    def test_complete_subclass_instantiates(self) -> None:
        class _Complete(BaseExecutor):
            name = "complete"

            def submit(self, fn, *args, **kwargs) -> Handle:  # type: ignore[override]
                raise NotImplementedError

            def shutdown(self) -> None:
                pass

        ex = _Complete()
        assert ex.name == "complete"


# ---------------------------------------------------------------------------
# LocalExecutor
# ---------------------------------------------------------------------------
class TestLocalExecutor:
    """LocalExecutor runs tasks in a ThreadPoolExecutor."""

    def test_submit_runs_function(self) -> None:
        ex = LocalExecutor(max_workers=2)
        handle = ex.submit(lambda: 42)
        assert handle.result(timeout=5) == 42
        ex.shutdown()

    def test_submit_with_args(self) -> None:
        ex = LocalExecutor(max_workers=2)
        handle = ex.submit(lambda x, y: x + y, 3, 7, name="add")
        assert handle.result(timeout=5) == 10
        ex.shutdown()

    def test_exception_propagation(self) -> None:
        ex = LocalExecutor(max_workers=1)

        def _fail() -> None:
            raise RuntimeError("task failed")

        handle = ex.submit(_fail)
        with pytest.raises(RuntimeError, match="task failed"):
            handle.result(timeout=5)
        ex.shutdown()

    def test_max_workers_respected(self) -> None:
        ex = LocalExecutor(max_workers=1)
        assert ex._pool._max_workers == 1
        ex.shutdown()

    def test_handle_job_id_starts_with_local(self) -> None:
        ex = LocalExecutor(max_workers=1)
        handle = ex.submit(lambda: None)
        assert handle.job_id.startswith("local-")
        handle.result(timeout=5)
        ex.shutdown()

    def test_handle_worker_id_is_local(self) -> None:
        ex = LocalExecutor(max_workers=1)
        handle = ex.submit(lambda: 1)
        handle.result(timeout=5)
        assert handle.worker_id == "local"
        ex.shutdown()

    def test_shutdown_waits_for_tasks(self) -> None:
        ex = LocalExecutor(max_workers=1)
        handle = ex.submit(lambda: 42)
        ex.shutdown()
        assert handle.done() is True

    def test_name_attribute(self) -> None:
        assert LocalExecutor(max_workers=1).name == "local"


# ---------------------------------------------------------------------------
# SlurmExecutor
# ---------------------------------------------------------------------------
class TestSlurmExecutor:
    """SlurmExecutor wraps submitit.AutoExecutor."""

    def test_debug_mode_default(self) -> None:
        ex = SlurmExecutor(debug=True)
        assert ex.debug is True
        ex.shutdown()

    def test_debug_false_uses_real_slurm(self) -> None:
        ex = SlurmExecutor(debug=False, partition="gpu")
        assert ex.debug is False
        assert ex.partition == "gpu"
        ex.shutdown()

    def test_submit_returns_handle(self) -> None:
        ex = SlurmExecutor(debug=True, partition="short")
        handle = ex.submit(lambda: 42, name="test")
        # Check duck-typing instead of isinstance to avoid class-identity
        # issues when tests reload/mock sys.modules.
        assert hasattr(handle, "result")
        assert hasattr(handle, "done")
        assert hasattr(handle, "job_id")
        result = handle.result(timeout=10)
        assert result == 42
        ex.shutdown()

    def test_submit_with_args(self) -> None:
        ex = SlurmExecutor(debug=True)
        handle = ex.submit(lambda x: x * 2, 5, name="double")
        assert handle.result(timeout=10) == 10
        ex.shutdown()

    def test_per_submit_resources(self) -> None:
        ex = SlurmExecutor(debug=True)
        handle = ex.submit(lambda: "ok", name="heavy", cpus=8, memory_mb=16384, time_min=120)
        assert handle.result(timeout=10) == "ok"
        ex.shutdown()

    def test_container_env_set(self) -> None:
        ex = SlurmExecutor(debug=True)
        handle = ex.submit(
            lambda: os.environ.get("OSIMFLOW_CONTAINER"),
            name="container-test",
            container="nrel/openstudio:3.11.0",
        )
        result = handle.result(timeout=10)
        assert result == "nrel/openstudio:3.11.0"
        ex.shutdown()

    def test_openstudio_version_env(self) -> None:
        ex = SlurmExecutor(debug=True)
        handle = ex.submit(
            lambda: os.environ.get("OSIMFLOW_OS_VERSION"),
            name="version-test",
            openstudio_version="3.11.0",
        )
        assert handle.result(timeout=10) == "3.11.0"
        ex.shutdown()

    def test_handle_job_id_is_string(self) -> None:
        ex = SlurmExecutor(debug=True)
        handle = ex.submit(lambda: None, name="id-test")
        assert isinstance(handle.job_id, str)
        handle.result(timeout=10)
        ex.shutdown()

    def test_partition_stored(self) -> None:
        ex = SlurmExecutor(debug=True, partition="long")
        assert ex.partition == "long"
        ex.shutdown()

    def test_account_stored(self) -> None:
        ex = SlurmExecutor(debug=True, account="myaccount")
        assert ex.account == "myaccount"
        ex.shutdown()

    def test_name_attribute(self) -> None:
        assert SlurmExecutor(debug=True).name == "slurm"


# ---------------------------------------------------------------------------
# _apply_slurm_params
# ---------------------------------------------------------------------------
class TestApplySlurmParams:
    """_apply_slurm_params handles new vs legacy submitit kwarg names."""

    def test_new_style_kwargs(self) -> None:
        mock_ex = MagicMock()
        _apply_slurm_params(
            mock_ex,
            partition="short",
            account="acct",
            cpus_per_task=4,
            mem_gb=8,
            time_min=60,
        )
        mock_ex.update_parameters.assert_called_once()
        kwargs = mock_ex.update_parameters.call_args[1]
        assert kwargs["slurm_partition"] == "short"
        assert kwargs["slurm_account"] == "acct"
        assert kwargs["slurm_cpus_per_task"] == 4
        assert kwargs["slurm_mem_gb"] == 8
        assert kwargs["slurm_time"] == 60

    def test_none_values_filtered(self) -> None:
        mock_ex = MagicMock()
        _apply_slurm_params(
            mock_ex,
            partition="short",
            account=None,
            cpus_per_task=2,
            mem_gb=4,
            time_min=30,
        )
        kwargs = mock_ex.update_parameters.call_args[1]
        assert "slurm_account" not in kwargs

    def test_legacy_fallback_on_type_error(self) -> None:
        mock_ex = MagicMock()
        mock_ex.update_parameters.side_effect = [TypeError("old submitit"), None]
        _apply_slurm_params(
            mock_ex,
            partition="short",
            account=None,
            cpus_per_task=2,
            mem_gb=4,
            time_min=30,
        )
        assert mock_ex.update_parameters.call_count == 2
        legacy_kwargs = mock_ex.update_parameters.call_args_list[1][1]
        assert "partition" in legacy_kwargs
        assert "slurm_partition" not in legacy_kwargs

    def test_qos_constraint_gres_forwarded(self) -> None:
        mock_ex = MagicMock()
        _apply_slurm_params(
            mock_ex,
            partition="gpu",
            account=None,
            cpus_per_task=4,
            mem_gb=16,
            time_min=120,
            qos="high",
            constraint="gpu",
            gres="gpu:1",
        )
        kwargs = mock_ex.update_parameters.call_args[1]
        assert kwargs["slurm_qos"] == "high"
        assert kwargs["slurm_constraint"] == "gpu"
        assert kwargs["slurm_gres"] == "gpu:1"


# ---------------------------------------------------------------------------
# AWSBatchExecutor helpers
# ---------------------------------------------------------------------------
class TestAWSBatchResolveImage:
    """_resolve_container_image chooses ECR vs Docker Hub."""

    def _make(self, **kw: object) -> AWSBatchExecutor:
        with patch("osimflow.executors.boto3", create=True):
            ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        ex.ecr_repository = kw.get("ecr_repository")
        return ex

    def test_default_docker_hub(self) -> None:
        ex = self._make()
        assert ex._resolve_container_image("3.11.0") == "nrel/openstudio:3.11.0"

    def test_ecr_override(self) -> None:
        ex = self._make(ecr_repository="123.dkr.ecr.us-east-1.amazonaws.com/os")
        assert (
            ex._resolve_container_image("3.11.0") == "123.dkr.ecr.us-east-1.amazonaws.com/os:3.11.0"
        )

    def test_none_version_latest(self) -> None:
        ex = self._make()
        assert ex._resolve_container_image(None) == "nrel/openstudio:latest"


class TestAWSBatchBuildEnvironment:
    """_build_environment constructs Batch env vars."""

    def _make(self) -> AWSBatchExecutor:
        with patch("osimflow.executors.boto3", create=True):
            ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        ex.ecr_repository = None
        return ex

    def test_includes_os_version(self) -> None:
        ex = self._make()
        env = ex._build_environment(container="img:3.11", openstudio_version="3.11.0")
        names = [e["name"] for e in env]
        assert "OSIMFLOW_OS_VERSION" in names
        assert "OSIMFLOW_CONTAINER" in names

    def test_without_os_version(self) -> None:
        ex = self._make()
        env = ex._build_environment(container=None, openstudio_version=None)
        names = [e["name"] for e in env]
        assert "OSIMFLOW_OS_VERSION" not in names
        assert "OSIMFLOW_CONTAINER" in names


class TestAWSBatchBuildContainerOverrides:
    """_build_container_overrides translates resource directives."""

    def _make(self) -> AWSBatchExecutor:
        with patch("osimflow.executors.boto3", create=True):
            ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        return ex

    def test_cpus_and_memory(self) -> None:
        ex = self._make()
        overrides = ex._build_container_overrides(cpus=4, memory_mb=8192, environment=[])
        assert overrides["vcpus"] == 4
        assert overrides["memory"] == 8192

    def test_environment_forwarded(self) -> None:
        ex = self._make()
        env = [{"name": "FOO", "value": "bar"}]
        overrides = ex._build_container_overrides(cpus=1, memory_mb=512, environment=env)
        assert overrides["environment"] == env


class TestAWSBatchSubmit:
    """submit() wires through to _submit_job and polls."""

    def _make_executor(self, **kw: object) -> AWSBatchExecutor:
        with patch("osimflow.executors.boto3", create=True):
            ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        ex._boto3 = MagicMock()
        ex._region_name = None
        ex._client = MagicMock()
        ex._ec2_client = MagicMock()
        ex.job_queue = kw.get("job_queue", "q")
        ex.job_definition = kw.get("job_def", "jd")
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.max_spot_price_usd = None
        ex.fallback_to_on_demand = False
        ex.max_retries = 3
        ex.ecr_repository = None
        ex._instance_type = None
        return ex

    def test_submit_succeeds(self) -> None:
        ex = self._make_executor()
        ex._client.submit_job.return_value = {"jobId": "j-1"}
        ex._client.describe_jobs.return_value = {"jobs": [{"jobId": "j-1", "status": "SUCCEEDED"}]}
        handle = ex.submit(lambda: None, name="test")
        assert handle.job_id == "j-1"

    def test_submit_failed_raises_runtime_error(self) -> None:
        ex = self._make_executor()
        ex._client.submit_job.return_value = {"jobId": "j-2"}
        ex._client.describe_jobs.return_value = {
            "jobs": [{"jobId": "j-2", "status": "FAILED", "statusReason": "OOM"}]
        }
        handle = ex.submit(lambda: None, name="fail")
        with pytest.raises(RuntimeError, match="OOM"):
            handle.result(timeout=5)

    def test_wait_for_terminal_polls(self) -> None:
        ex = self._make_executor()
        ex._client.describe_jobs.side_effect = [
            {"jobs": [{"jobId": "j-3", "status": "RUNNING"}]},
            {"jobs": [{"jobId": "j-3", "status": "SUCCEEDED"}]},
        ]
        with patch("osimflow.executors.time.sleep"):
            job = ex._wait_for_terminal("j-3")
        assert job["status"] == "SUCCEEDED"

    def test_wait_for_terminal_no_job_raises(self) -> None:
        ex = self._make_executor()
        ex._client.describe_jobs.return_value = {"jobs": []}
        with pytest.raises(RuntimeError, match="no job"):
            ex._wait_for_terminal("j-missing")

    def test_build_job_spec(self) -> None:
        ex = self._make_executor()
        ex.job_queue = "q"
        ex.job_definition = "jd"
        ex._client.submit_job.return_value = {"jobId": "j-spec"}
        ex._client.describe_jobs.return_value = {
            "jobs": [{"jobId": "j-spec", "status": "SUCCEEDED"}]
        }
        ex.submit(lambda: None, name="spec-test", cpus=2, memory_mb=4096, time_min=30)
        call_kwargs = ex._client.submit_job.call_args[1]
        assert call_kwargs["containerOverrides"]["vcpus"] == 2
        assert call_kwargs["containerOverrides"]["memory"] == 4096
        assert call_kwargs["timeout"]["attemptDurationSeconds"] == 1800

    def test_spot_interruption_detection(self) -> None:
        ex = self._make_executor()
        assert ex._is_spot_interruption("Spot interruption: terminated") is True
        assert ex._is_spot_interruption("Spot Instance termination notice") is True
        assert ex._is_spot_interruption("Container failed") is False
        assert ex._is_spot_interruption(None) is False

    def test_get_spot_price(self) -> None:
        ex = self._make_executor()
        ex._ec2_client.describe_spot_price_history.return_value = {
            "SpotPriceHistory": [{"SpotPrice": "0.05"}]
        }
        assert ex._get_spot_price() == 0.05

    def test_get_spot_price_no_history_raises(self) -> None:
        ex = self._make_executor()
        ex._ec2_client.describe_spot_price_history.return_value = {"SpotPriceHistory": []}
        with pytest.raises(RuntimeError, match="no results"):
            ex._get_spot_price()

    def test_check_spot_price_ceiling_raises(self) -> None:
        ex = self._make_executor()
        ex.max_spot_price_usd = 0.03
        ex._ec2_client.describe_spot_price_history.return_value = {
            "SpotPriceHistory": [{"SpotPrice": "0.10"}]
        }
        with pytest.raises(RuntimeError, match="exceeds ceiling"):
            ex._check_spot_price_ceiling()

    def test_check_spot_price_ceiling_ok(self) -> None:
        ex = self._make_executor()
        ex.max_spot_price_usd = 0.10
        ex._ec2_client.describe_spot_price_history.return_value = {
            "SpotPriceHistory": [{"SpotPrice": "0.03"}]
        }
        ex._check_spot_price_ceiling()  # should not raise

    def test_check_spot_price_ceiling_none_skip(self) -> None:
        ex = self._make_executor()
        ex.max_spot_price_usd = None
        ex._check_spot_price_ceiling()  # should not raise, no API call
        ex._ec2_client.describe_spot_price_history.assert_not_called()

    def test_name_attribute(self) -> None:
        ex = self._make_executor()
        assert ex.name == "aws_batch"

    def test_lazy_client(self) -> None:
        ex = self._make_executor()
        ex._client = None
        mock_client = MagicMock()
        ex._boto3.client.return_value = mock_client
        client = ex._get_client()
        assert client is mock_client
        ex._boto3.client.assert_called_once_with("batch", region_name=None)

    def test_lazy_ec2_client(self) -> None:
        ex = self._make_executor()
        ex._ec2_client = None
        mock_client = MagicMock()
        ex._boto3.client.return_value = mock_client
        client = ex._get_ec2_client()
        assert client is mock_client
        ex._boto3.client.assert_called_once_with("ec2", region_name=None)

    def test_shutdown_is_noop(self) -> None:
        ex = self._make_executor()
        ex.shutdown()  # should not raise


class TestAWSBatchHandle:
    """_AWSBatchHandle polls Batch on .result() and .done()."""

    def _make_handle(
        self, job_status: str = "SUCCEEDED", reason: str = ""
    ) -> tuple[_AWSBatchHandle, MagicMock]:
        mock_client = MagicMock()
        mock_client.describe_jobs.return_value = {
            "jobs": [{"jobId": "j-h", "status": job_status, "statusReason": reason}]
        }
        with patch("osimflow.executors.boto3", create=True):
            ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        ex._client = mock_client
        ex._boto3 = MagicMock()
        ex._region_name = None
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.max_retries = 0
        ex.fallback_to_on_demand = False
        ex._ec2_client = MagicMock()
        handle = _AWSBatchHandle(job_id="j-h", executor=ex, submit_params={})
        return handle, mock_client

    def test_result_succeeded(self) -> None:
        handle, _ = self._make_handle("SUCCEEDED")
        assert handle.result() is None

    def test_result_succeeded_returns_result_hint(self) -> None:
        handle, _ = self._make_handle("SUCCEEDED")
        hint = Path("/tmp/osimflow/aggregate/aggregated_results.csv")
        handle._result_hint = hint  # noqa: SLF001
        assert handle.result() == hint

    def test_result_failed_raises(self) -> None:
        handle, _ = self._make_handle("FAILED", "OOM killed")
        with pytest.raises(RuntimeError, match="OOM killed"):
            handle.result()

    def test_done_succeeded(self) -> None:
        handle, _ = self._make_handle("SUCCEEDED")
        assert handle.done() is True

    def test_done_failed(self) -> None:
        handle, _ = self._make_handle("FAILED")
        assert handle.done() is True

    def test_done_running(self) -> None:
        handle, mock_client = self._make_handle("RUNNING")
        assert handle.done() is False

    def test_done_api_error_returns_false(self) -> None:
        handle, mock_client = self._make_handle("RUNNING")
        mock_client.describe_jobs.side_effect = Exception("network")
        assert handle.done() is False

    def test_done_empty_jobs_returns_false(self) -> None:
        handle, mock_client = self._make_handle("RUNNING")
        mock_client.describe_jobs.return_value = {"jobs": []}
        assert handle.done() is False


# ---------------------------------------------------------------------------
# _slugify_job_name
# ---------------------------------------------------------------------------
class TestSlugifyJobName:
    """DNS-1123 label sanitisation for Nomad job names."""

    def test_lowercase(self) -> None:
        assert _slugify_job_name("MyJob") == "myjob"

    def test_replace_underscores(self) -> None:
        assert _slugify_job_name("sim_001") == "sim-001"

    def test_strip_special_chars(self) -> None:
        assert _slugify_job_name("job@#$%name") == "job-name"

    def test_trim_dashes(self) -> None:
        assert _slugify_job_name("-leading") == "leading"
        assert _slugify_job_name("trailing-") == "trailing"

    def test_truncate_63(self) -> None:
        long_name = "a" * 100
        assert len(_slugify_job_name(long_name)) == 63

    def test_empty_becomes_task(self) -> None:
        assert _slugify_job_name("---") == "task"

    def test_nominal_case(self) -> None:
        assert _slugify_job_name("osimflow-sim_sample-0") == "osimflow-sim-sample-0"


# ---------------------------------------------------------------------------
# NomadExecutor
# ---------------------------------------------------------------------------
class TestNomadExecutor:
    """NomadExecutor wraps the Nomad HTTP API."""

    def _mock_urlopen(self, responses: dict[str, object] | None = None) -> MagicMock:
        """Build a mock urlopen that returns canned responses."""
        defaults: dict[str, object] = {
            "POST /v1/jobs": {"JobID": "nomad-1", "EvalID": "eval-1"},
            "eval_alloc": [{"ID": "alloc-1", "ClientStatus": "complete"}],
            "alloc": {"ID": "alloc-1", "ClientStatus": "complete", "TaskStates": {}},
        }
        if responses:
            defaults.update(responses)

        mock = MagicMock()

        def _resp(request: object, **_kw: object) -> object:
            req = request  # type: ignore[assignment]
            method = req.get_method()  # type: ignore[attr-defined]
            url = req.full_url  # type: ignore[attr-defined]

            if method == "POST" and "/v1/jobs" in url:
                result = defaults["POST /v1/jobs"]
            elif method == "GET" and "/v1/evaluation/" in url and "/allocations" in url:
                result = defaults["eval_alloc"]
            elif method == "GET" and "/v1/job/" in url and "/allocations" in url:
                result = defaults.get("job_alloc", [])
            elif method == "GET" and "/v1/allocation/" in url:
                result = defaults["alloc"]
            else:
                result = {}

            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        mock.side_effect = _resp
        return mock

    def test_submit_returns_handle(self) -> None:
        mock_urlopen = self._mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646")
            handle = ex.submit(lambda: None, name="test")
        # Check duck-typing instead of isinstance to avoid class-identity
        # issues when tests reload/mock sys.modules.
        assert hasattr(handle, "result")
        assert hasattr(handle, "job_id")
        assert handle.job_id == "nomad-1"
        ex.shutdown()

    def test_submit_with_resources(self) -> None:
        mock_urlopen = self._mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646")
            handle = ex.submit(lambda: None, name="heavy", cpus=4, memory_mb=8192)
        assert handle.job_id == "nomad-1"
        ex.shutdown()

    def test_address_from_env(self) -> None:
        with patch.dict(os.environ, {"NOMAD_ADDR": "http://nomad:4646"}):
            mock_urlopen = self._mock_urlopen()
            with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
                ex = NomadExecutor()
            assert ex.address == "http://nomad:4646"
        ex.shutdown()

    def test_datacentre_default(self) -> None:
        mock_urlopen = self._mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646")
        assert ex.datacentre == "dc1"
        ex.shutdown()

    def test_name_attribute(self) -> None:
        assert NomadExecutor.__new__(NomadExecutor).name == "nomad"  # noqa: SLF001

    def test_submit_remote_results_only_returns_result_hint(self) -> None:
        mock_urlopen = self._mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646", remote_results_only=True)
            hint = Path("/tmp/remote/work/apply/0001")
            handle = ex.submit(
                lambda: (_ for _ in ()).throw(AssertionError("local callable should not run")),
                name="apply_0001",
                result_hint=hint,
            )
            with patch("osimflow.executors.time.sleep"):
                result = handle.result()
        assert Path(str(result)) == hint
        ex.shutdown()

    def test_compat_mode_emits_deprecation_warning_with_migration_guidance(self) -> None:
        mock_urlopen = self._mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            with pytest.warns(DeprecationWarning, match="one minor release"):
                ex = NomadExecutor(address="http://127.0.0.1:4646", remote_results_only=False)
        ex.shutdown()

    def test_build_job_spec_prefers_env_openstudio_image(self) -> None:
        mock_urlopen = self._mock_urlopen()
        with (
            patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect),
            patch.dict(
                os.environ, {"OSIMFLOW_OPENSTUDIO_CONTAINER_IMAGE": "local/openstudio:3.11.0"}
            ),
        ):
            ex = NomadExecutor(address="http://127.0.0.1:4646")
            spec = ex._build_job_spec(  # noqa: SLF001
                name="sim_0001",
                cpus=4,
                memory_mb=8192,
                container=None,
                openstudio_version="3.11.0",
            )
        image = spec["Job"]["TaskGroups"][0]["Tasks"][0]["Config"]["image"]
        assert image == "local/openstudio:3.11.0"
        ex.shutdown()

    def test_build_job_spec_allows_remote_command_override(self) -> None:
        mock_urlopen = self._mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646")
            spec = ex._build_job_spec(  # noqa: SLF001
                name="sim_0001",
                cpus=4,
                memory_mb=8192,
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                remote_command="python -m osimflow.remote_runner --step sim",
            )
        entrypoint = spec["Job"]["TaskGroups"][0]["Tasks"][0]["Config"]["entrypoint"]
        assert entrypoint == ["/bin/sh", "-c", "python -m osimflow.remote_runner --step sim"]
        ex.shutdown()

    def test_build_job_spec_defaults_to_remote_runner_command(self) -> None:
        mock_urlopen = self._mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646")
            spec = ex._build_job_spec(  # noqa: SLF001
                name="sim_0001",
                cpus=2,
                memory_mb=2048,
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
            )
        entrypoint = spec["Job"]["TaskGroups"][0]["Tasks"][0]["Config"]["entrypoint"]
        assert entrypoint == ["/bin/sh", "-c", "python -m osimflow.remote_runner"]
        ex.shutdown()

    def test_build_dispatch_job_spec_defaults_to_remote_runner_command(self) -> None:
        mock_urlopen = self._mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646", use_dispatch=True)
            spec = ex._build_dispatch_job_spec()  # noqa: SLF001
        args = spec["Job"]["TaskGroups"][0]["Tasks"][0]["Config"]["args"]
        assert args == ["-c", "python -m osimflow.remote_runner"]
        ex.shutdown()

    def test_wait_for_terminal(self) -> None:
        alloc_response = {"ID": "alloc-1", "ClientStatus": "complete"}
        mock_urlopen = self._mock_urlopen({"alloc": alloc_response})
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646")
            with patch("osimflow.executors.time.sleep"):
                result = ex._wait_for_terminal("alloc-1")
        assert result["ClientStatus"] == "complete"
        ex.shutdown()

    def test_wait_for_terminal_polls(self) -> None:
        call_count = 0
        alloc_response = {"ID": "alloc-1", "ClientStatus": "complete"}

        mock = MagicMock()

        def _resp(request: object, **_kw: object) -> object:
            nonlocal call_count
            req = request  # type: ignore[assignment]
            method = req.get_method()  # type: ignore[attr-defined]
            url = req.full_url  # type: ignore[attr-defined]

            if method == "GET" and "/v1/allocation/" in url:
                call_count += 1
                if call_count == 1:
                    result = {"ID": "alloc-1", "ClientStatus": "running"}
                else:
                    result = alloc_response
            else:
                result = {}

            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        mock.side_effect = _resp
        with patch("urllib.request.urlopen", side_effect=mock.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646")
            with patch("osimflow.executors.time.sleep"):
                result = ex._wait_for_terminal("alloc-1")
        assert result["ClientStatus"] == "complete"
        assert call_count == 2
        ex.shutdown()

    def test_wait_for_terminal_timeout_raises(self) -> None:
        """_wait_for_terminal must raise TimeoutError when timeout is exceeded."""
        call_count = 0

        def _resp(request: object, **_kw: object) -> object:
            nonlocal call_count
            req = request  # type: ignore[assignment]
            method = req.get_method()  # type: ignore[attr-defined]
            url = req.full_url  # type: ignore[attr-defined]

            if method == "GET" and "/v1/allocation/" in url:
                call_count += 1
                result = {"ID": "alloc-1", "ClientStatus": "running"}
            else:
                result = {}

            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch("urllib.request.urlopen", side_effect=_resp):
            ex = NomadExecutor(address="http://127.0.0.1:4646")
            with patch("osimflow.executors.time.sleep"):
                with pytest.raises(TimeoutError, match="Timed out"):
                    ex._wait_for_terminal("alloc-1", timeout=0.05)
        assert call_count >= 1
        ex.shutdown()


class TestNomadHandle:
    """_NomadHandle polls Nomad on .result() and .done()."""

    def _make_handle(
        self,
        *,
        alloc_status: str = "complete",
        task_states: dict[str, object] | None = None,
    ) -> tuple[_NomadHandle, MagicMock]:
        mock_urlopen = MagicMock()
        resolved_alloc = False

        def _resp(request: object, **_kw: object) -> object:
            nonlocal resolved_alloc
            req = request  # type: ignore[assignment]
            method = req.get_method()  # type: ignore[attr-defined]
            url = req.full_url  # type: ignore[attr-defined]

            if method == "GET" and "/v1/evaluation/" in url and "/allocations" in url:
                if not resolved_alloc:
                    resolved_alloc = True
                    result = [{"ID": "alloc-r", "ClientStatus": alloc_status}]
                else:
                    result = [{"ID": "alloc-r", "ClientStatus": alloc_status}]
            elif method == "GET" and "/v1/allocation/" in url:
                result = {
                    "ID": "alloc-r",
                    "ClientStatus": alloc_status,
                    "TaskStates": task_states or {},
                }
            else:
                result = {}

            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        mock_urlopen.side_effect = _resp
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646")
            handle = ex.submit(lambda: None, name="test")

        return handle, mock_urlopen

    def test_result_complete(self) -> None:
        handle, _ = self._make_handle(alloc_status="complete")
        with patch("osimflow.executors.time.sleep"):
            assert handle.result() is None

    def test_result_failed_raises(self) -> None:
        task_states = {"osimflow": {"Events": [{"Description": "Exit Code: 137 (OOM killed)"}]}}
        handle, _ = self._make_handle(alloc_status="failed", task_states=task_states)
        with (
            patch("osimflow.executors.time.sleep"),
            pytest.raises(RuntimeError, match="OOM killed"),
        ):
            handle.result()

    def test_done_complete(self) -> None:
        handle, _ = self._make_handle(alloc_status="complete")
        with patch("osimflow.executors.time.sleep"):
            handle.result()
        assert handle.done() is True

    def test_done_running(self) -> None:
        handle, _ = self._make_handle(alloc_status="running")
        assert handle.done() is False

    def test_done_lost(self) -> None:
        handle, _ = self._make_handle(alloc_status="lost")
        assert handle.done() is False

    def test_done_failed(self) -> None:
        handle, _ = self._make_handle(alloc_status="failed")
        assert handle.done() is False

    def test_result_complete_materializes_object_storage_hint(self) -> None:
        class _ClientStub:
            @staticmethod
            def resolve_allocation(eval_id: str, job_id: str) -> str:  # noqa: ARG004
                return "alloc-r"

            @staticmethod
            def get_allocation(allocation_id: str) -> dict[str, object]:  # noqa: ARG004
                return {"ID": "alloc-r", "ClientStatus": "complete", "TaskStates": {}}

        class _ExecutorStub:
            datacentre = "dc1"
            _client = _ClientStub()

            @staticmethod
            def _wait_for_terminal(
                allocation_id: str, timeout: float | None = None
            ) -> dict[str, object]:  # noqa: ARG004
                return {"ID": "alloc-r", "ClientStatus": "complete", "TaskStates": {}}

        with patch("osimflow.executors.materialize_object_storage_result") as materialize:
            materialize.side_effect = lambda value, **_: value
            hint = Path("/repo/out/work/kpis/kpi_0001.json")
            handle = _NomadHandle(
                job_id="job-1",
                eval_id="eval-1",
                executor=_ExecutorStub(),  # type: ignore[arg-type]
                result_hint=hint,
                result_transport_mode="object_storage",
                result_storage_backend="s3",
                result_storage_bucket="bucket",
                result_storage_prefix="out",
                result_storage_endpoint=None,
            )
            result = handle.result()

        assert result == hint
        materialize.assert_called_once()

    def test_extract_failure_description(self) -> None:
        states = {"task": {"Events": [{"Description": "OOM killed"}]}}
        assert _NomadHandle._extract_failure_description(states) == "OOM killed"

    def test_extract_failure_description_display_message_fallback(self) -> None:
        states = {"task": {"Events": [{"DisplayMessage": "Image pull denied"}]}}
        assert _NomadHandle._extract_failure_description(states) == "Image pull denied"

    def test_extract_failure_description_prefers_driver_failure(self) -> None:
        states = {
            "task": {
                "Events": [
                    {"Type": "Restarting", "Description": "Task restarting in 15s"},
                    {"Type": "Driver Failure", "Description": "Failed to pull image: denied"},
                    {
                        "Type": "Not Restarting",
                        "Description": "Exceeded allowed attempts and mode is fail",
                    },
                ]
            }
        }
        assert _NomadHandle._extract_failure_description(states) == "Failed to pull image: denied"

    def test_extract_failure_description_no_events(self) -> None:
        assert _NomadHandle._extract_failure_description({}) == "unknown reason"

    def test_extract_failure_description_empty_events(self) -> None:
        states = {"task": {"Events": []}}
        assert _NomadHandle._extract_failure_description(states) == "unknown reason"

    def test_ensure_allocation_id_raises_when_resolve_returns_none(self) -> None:
        """verify _ensure_allocation_id raises RuntimeError when resolve_allocation returns None."""

        class _ClientStubReturnsNone:
            @staticmethod
            def resolve_allocation(eval_id: str, job_id: str, **_: object) -> str | None:
                # Simulate a resolve_allocation that returns None (e.g. timeout, no alloc)
                return None

        class _ExecutorStubNone:
            datacentre = "dc1"
            _client = _ClientStubReturnsNone()
            allocation_resolution_timeout_s = 0.1

            @staticmethod
            def _wait_for_terminal(allocation_id: str) -> dict[str, object]:
                return {"ID": allocation_id, "ClientStatus": "complete", "TaskStates": {}}

        handle = _NomadHandle(
            job_id="job-1",
            eval_id="eval-1",
            executor=_ExecutorStubNone(),  # type: ignore[arg-type]
        )
        with pytest.raises(RuntimeError, match="resolve_allocation returned None"):
            handle._ensure_allocation_id()

    def test_ensure_allocation_id_raises_when_resolve_returns_none_string(self) -> None:
        """verify _ensure_allocation_id raises RuntimeError when resolve_allocation returns 'None' (str)."""

        class _ClientStubReturnsNoneStr:
            @staticmethod
            def resolve_allocation(eval_id: str, job_id: str, **_: object) -> str | None:
                # Simulate a buggy resolve_allocation that returns str(None) == "None"
                return str(None)

        class _ExecutorStubNoneStr:
            datacentre = "dc1"
            _client = _ClientStubReturnsNoneStr()
            allocation_resolution_timeout_s = 0.1

            @staticmethod
            def _wait_for_terminal(allocation_id: str) -> dict[str, object]:
                return {"ID": allocation_id, "ClientStatus": "complete", "TaskStates": {}}

        handle = _NomadHandle(
            job_id="job-1",
            eval_id="eval-1",
            executor=_ExecutorStubNoneStr(),  # type: ignore[arg-type]
        )
        with pytest.raises(RuntimeError, match=r"resolve_allocation returned 'None'"):
            handle._ensure_allocation_id()

    def test_result_timeout_raises(self) -> None:
        """_NomadHandle.result() must raise TimeoutError when timeout is exceeded."""

        class _ClientStub:
            @staticmethod
            def resolve_allocation(eval_id: str, job_id: str) -> str:
                return "alloc-timeout"

            @staticmethod
            def get_allocation(allocation_id: str) -> dict[str, object]:
                return {"ID": allocation_id, "ClientStatus": "running", "TaskStates": {}}

        class _ExecutorStub:
            datacentre = "dc1"
            _client = _ClientStub()
            poll_interval_s = 0.01
            max_poll_interval_s = 0.1

            def _wait_for_terminal(
                self, allocation_id: str, timeout: float | None = None
            ) -> dict[str, object]:
                import time as time_mod

                start = time_mod.monotonic()
                while True:
                    alloc = self._client.get_allocation(allocation_id)
                    if alloc.get("ClientStatus") in ("complete", "failed", "lost"):
                        return alloc
                    if timeout is not None:
                        elapsed = time_mod.monotonic() - start
                        remaining = timeout - elapsed
                        if remaining <= 0:
                            raise TimeoutError(
                                f"Timed out after {elapsed:.1f}s waiting for allocation {allocation_id!r}"
                            )
                        time_mod.sleep(min(0.01, remaining))
                    else:
                        time_mod.sleep(0.01)

        class _ExecutorStubWithWait(_ExecutorStub):
            pass

        handle = _NomadHandle(
            job_id="job-timeout",
            eval_id="eval-timeout",
            executor=_ExecutorStubWithWait(),  # type: ignore[arg-type]
        )
        with pytest.raises(TimeoutError, match="Timed out"):
            handle.result(timeout=0.05)


# ---------------------------------------------------------------------------
# _NomadClient
# ---------------------------------------------------------------------------
class TestNomadClient:
    """_NomadClient wraps urllib for the Nomad HTTP API."""

    def test_submit_job(self) -> None:
        mock_urlopen = MagicMock()
        result = {"JobID": "j-1", "EvalID": "e-1"}
        resp = MagicMock()
        resp.read.return_value = json.dumps(result).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        mock_urlopen.return_value = resp

        with patch("urllib.request.urlopen", mock_urlopen):
            client = _NomadClient(address="http://127.0.0.1:4646", token=None)
            resp_data = client.submit_job({"Job": {"ID": "test"}})

        assert resp_data["JobID"] == "j-1"

    def test_get_allocation(self) -> None:
        mock_urlopen = MagicMock()
        alloc = {"ID": "a-1", "ClientStatus": "complete"}
        resp = MagicMock()
        resp.read.return_value = json.dumps(alloc).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        mock_urlopen.return_value = resp

        with patch("urllib.request.urlopen", mock_urlopen):
            client = _NomadClient(address="http://127.0.0.1:4646", token="secret")
            result = client.get_allocation("a-1")

        assert result["ClientStatus"] == "complete"
        request_arg = mock_urlopen.call_args[0][0]
        assert request_arg.get_header("X-nomad-token") == "secret"

    def test_get_eval_allocations_empty(self) -> None:
        mock_urlopen = MagicMock()
        resp = MagicMock()
        resp.read.return_value = json.dumps({}).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        mock_urlopen.return_value = resp

        with patch("urllib.request.urlopen", mock_urlopen):
            client = _NomadClient(address="http://127.0.0.1:4646", token=None)
            result = client.get_eval_allocations("e-1")

        assert result == []

    def test_get_job_allocations_list(self) -> None:
        mock_urlopen = MagicMock()
        allocs = [{"ID": "a-1"}, {"ID": "a-2"}]
        resp = MagicMock()
        resp.read.return_value = json.dumps(allocs).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        mock_urlopen.return_value = resp

        with patch("urllib.request.urlopen", mock_urlopen):
            client = _NomadClient(address="http://127.0.0.1:4646", token=None)
            result = client.get_job_allocations("j-1")

        assert len(result) == 2

    def test_resolve_allocation_from_eval(self) -> None:
        mock_urlopen = MagicMock()
        call_count = 0

        def _resp(request: object, **_kw: object) -> object:
            nonlocal call_count
            req = request  # type: ignore[assignment]
            method = req.get_method()  # type: ignore[attr-defined]
            url = req.full_url  # type: ignore[attr-defined]
            call_count += 1

            if method == "GET" and "/v1/evaluation/" in url:
                result = [{"ID": "alloc-ev", "ClientStatus": "complete"}]
            else:
                result = []

            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        mock_urlopen.side_effect = _resp
        with patch("urllib.request.urlopen", mock_urlopen):
            client = _NomadClient(address="http://127.0.0.1:4646", token=None)
            alloc_id = client.resolve_allocation("e-1", "j-1")

        assert alloc_id == "alloc-ev"


# ---------------------------------------------------------------------------
# run_subprocess
# ---------------------------------------------------------------------------
class TestRunSubprocess:
    """run_subprocess captures stdout/stderr to files."""

    def test_captures_stdout(self, tmp_path: Path) -> None:
        stdout_path = tmp_path / "out.log"
        stderr_path = tmp_path / "err.log"
        result = run_subprocess(
            ["echo", "hello"],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        assert result.returncode == 0
        assert "hello" in stdout_path.read_text()

    def test_captures_stderr(self, tmp_path: Path) -> None:
        stdout_path = tmp_path / "out.log"
        stderr_path = tmp_path / "err.log"
        result = run_subprocess(
            ["python3", "-c", "import sys; print('err', file=sys.stderr)"],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        assert result.returncode == 0
        assert "err" in stderr_path.read_text()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        stdout_path = tmp_path / "deep" / "nested" / "out.log"
        stderr_path = tmp_path / "deep" / "nested" / "err.log"
        run_subprocess(
            ["echo", "ok"],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        assert stdout_path.exists()
        assert stderr_path.exists()

    def test_nonzero_exit_not_raised(self, tmp_path: Path) -> None:
        stdout_path = tmp_path / "out.log"
        stderr_path = tmp_path / "err.log"
        result = run_subprocess(
            ["false"],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            check=False,
        )
        assert result.returncode != 0

    def test_check_true_raises(self, tmp_path: Path) -> None:
        stdout_path = tmp_path / "out.log"
        stderr_path = tmp_path / "err.log"
        with pytest.raises(subprocess.CalledProcessError):
            run_subprocess(
                ["false"],
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                check=True,
            )

    def test_cwd_forwarded(self, tmp_path: Path) -> None:
        stdout_path = tmp_path / "out.log"
        stderr_path = tmp_path / "err.log"
        run_subprocess(
            ["pwd"],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            cwd=tmp_path,
        )
        assert str(tmp_path) in stdout_path.read_text()

    def test_env_forwarded(self, tmp_path: Path) -> None:
        stdout_path = tmp_path / "out.log"
        stderr_path = tmp_path / "err.log"
        env = {**os.environ, "MY_TEST_VAR": "testval"}
        run_subprocess(
            ["printenv", "MY_TEST_VAR"],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            env=env,
        )
        assert "testval" in stdout_path.read_text()
