"""Tests for issue #1077: Cloud executors must marshal fn/args into task payload.

These tests verify that AWS Batch, Azure Batch, Google Batch, and Docker Swarm
executors properly serialize the callable and arguments into OSIMFLOW_TASK_PAYLOAD
and use the remote_runner command, instead of discarding fn/args and running
'sleep infinity'.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from osimflow.executors import AWSBatchExecutor
from osimflow.executors.azure_batch_executor import AzureBatchExecutor
from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor
from osimflow.executors.google_batch_executor import GoogleBatchExecutor
from osimflow.executors.transport import decode_transport_value


def _make_mock_aws_executor():
    ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
    ex._boto3 = MagicMock()
    ex._region_name = "us-east-1"
    ex._client = MagicMock()
    ex._ec2_client = MagicMock()
    ex.job_queue = "test-queue"
    ex.job_definition = "test-job-def"
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex.max_spot_price_usd = None
    ex.fallback_to_on_demand = False
    ex.max_retries = 3
    ex.ecr_repository = None
    ex._instance_type = None
    ex._submit_rps = None
    ex._container_digest = None
    # Mock the rate limiter and spot price cache
    ex._submit_limiter = MagicMock()
    ex._spot_price_cache = MagicMock()
    ex._retry_config = MagicMock()
    return ex


def _make_mock_azure_executor():
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
    ex._container_digest = None
    return ex


def _make_mock_google_executor():
    ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
    # Mock the batch_v1 module to avoid needing google-cloud-batch installed
    mock_batch_v1 = MagicMock()
    mock_batch_v1.JobStatus = MagicMock()
    mock_batch_v1.JobStatus.State = MagicMock()
    mock_batch_v1.JobStatus.State.SUCCEEDED = "SUCCEEDED"
    mock_batch_v1.JobStatus.State.FAILED = "FAILED"
    ex._batch_v1 = mock_batch_v1
    ex.project_id = "test-project"
    ex.region = "us-central1"
    ex.batch_service_account = None
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex.use_spot = False
    ex.fallback_to_on_demand = False
    ex.max_retries = 3
    ex._client = MagicMock()
    ex._container_digest = None
    return ex


def _make_mock_docker_swarm_executor():
    ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex.image = "nrel/openstudio:latest"
    ex.network = None
    ex._client = MagicMock()
    ex._stub_executor = None
    ex._container_digest = None
    return ex


class TestAWSBatchExecutorFnArgsMarshal:
    """AWS Batch executor must marshal fn/args into OSIMFLOW_TASK_PAYLOAD."""

    def test_submit_builds_task_payload_with_correct_step(self):
        ex = _make_mock_aws_executor()
        # Mock the spot price check and _submit_job
        ex._get_spot_price = MagicMock(return_value=0.03)
        ex._submit_job = MagicMock(return_value="test-job-id")
        ex._wait_for_terminal = MagicMock(return_value={"status": "SUCCEEDED"})

        handle = ex.submit(lambda x, y: x + y, 1, 2, name="sim_s0")

        # Verify _submit_job was called with command and task_payload
        ex._submit_job.assert_called_once()
        call_kwargs = ex._submit_job.call_args.kwargs
        assert "command" in call_kwargs
        assert call_kwargs["command"] == ["python", "-m", "osimflow.remote_runner"]
        assert "environment" in call_kwargs

        # Verify OSIMFLOW_TASK_PAYLOAD is in environment
        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        assert "OSIMFLOW_TASK_PAYLOAD" in env

        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])
        assert payload["schema_version"] == 1
        assert payload["name"] == "sim_s0"
        assert payload["step"] == "sim"
        assert payload["args"] == [1, 2]
        assert payload["kwargs"] == {}

    def test_submit_apply_step_name_mapping(self):
        ex = _make_mock_aws_executor()
        ex._get_spot_price = MagicMock(return_value=0.03)
        ex._submit_job = MagicMock(return_value="test-job-id")

        ex.submit(lambda: None, name="apply_s0")

        call_kwargs = ex._submit_job.call_args.kwargs
        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])
        assert payload["step"] == "apply"

    def test_submit_kpi_step_name_mapping(self):
        ex = _make_mock_aws_executor()
        ex._get_spot_price = MagicMock(return_value=0.03)
        ex._submit_job = MagicMock(return_value="test-job-id")

        ex.submit(lambda: None, name="kpi_s0")

        call_kwargs = ex._submit_job.call_args.kwargs
        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])
        assert payload["step"] == "extract"

    def test_submit_aggregate_step_name_mapping(self):
        ex = _make_mock_aws_executor()
        ex._get_spot_price = MagicMock(return_value=0.03)
        ex._submit_job = MagicMock(return_value="test-job-id")

        ex.submit(lambda: None, name="aggregate")

        call_kwargs = ex._submit_job.call_args.kwargs
        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])
        assert payload["step"] == "aggregate"

    def test_submit_plots_step_name_mapping(self):
        ex = _make_mock_aws_executor()
        ex._get_spot_price = MagicMock(return_value=0.03)
        ex._submit_job = MagicMock(return_value="test-job-id")

        ex.submit(lambda: None, name="plots")

        call_kwargs = ex._submit_job.call_args.kwargs
        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])
        assert payload["step"] == "plots"

    def test_submit_remote_command_override(self):
        ex = _make_mock_aws_executor()
        ex._get_spot_price = MagicMock(return_value=0.03)
        ex._submit_job = MagicMock(return_value="test-job-id")

        ex.submit(lambda: None, name="sim_s0", remote_command="custom command")

        call_kwargs = ex._submit_job.call_args.kwargs
        assert call_kwargs["command"] == ["/bin/sh", "-c", "custom command"]

    def test_submit_propagates_result_transport_env(self):
        ex = _make_mock_aws_executor()
        ex._get_spot_price = MagicMock(return_value=0.03)
        ex._submit_job = MagicMock(return_value="test-job-id")

        ex.submit(
            lambda: None,
            name="sim_s0",
            result_transport_mode="object_storage",
            result_storage_backend="s3",
            result_storage_bucket="test-bucket",
            result_storage_prefix="out",
            result_storage_endpoint="http://minio:9000",
        )

        call_kwargs = ex._submit_job.call_args.kwargs
        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        assert env["OSIMFLOW_RESULT_TRANSPORT_MODE"] == "object_storage"
        assert env["OSIMFLOW_RESULT_STORAGE_BACKEND"] == "s3"
        assert env["OSIMFLOW_RESULT_STORAGE_BUCKET"] == "test-bucket"
        assert env["OSIMFLOW_RESULT_STORAGE_PREFIX"] == "out"
        assert env["OSIMFLOW_RESULT_STORAGE_ENDPOINT"] == "http://minio:9000"

    def test_submit_omits_result_storage_env_when_unset(self):
        ex = _make_mock_aws_executor()
        ex._get_spot_price = MagicMock(return_value=0.03)
        ex._submit_job = MagicMock(return_value="test-job-id")

        ex.submit(lambda: None, name="sim_s0", result_transport_mode="shared_fs")

        call_kwargs = ex._submit_job.call_args.kwargs
        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        assert env["OSIMFLOW_RESULT_TRANSPORT_MODE"] == "shared_fs"
        for var in (
            "OSIMFLOW_RESULT_STORAGE_BACKEND",
            "OSIMFLOW_RESULT_STORAGE_BUCKET",
            "OSIMFLOW_RESULT_STORAGE_PREFIX",
            "OSIMFLOW_RESULT_STORAGE_ENDPOINT",
        ):
            assert var not in env


class TestAzureBatchExecutorFnArgsMarshal:
    """Azure Batch executor must marshal fn/args into OSIMFLOW_TASK_PAYLOAD."""

    def test_submit_builds_task_payload_with_correct_step(self):
        ex = _make_mock_azure_executor()
        ex._submit_job = MagicMock(return_value="test-job-id")

        handle = ex.submit(lambda x, y: x + y, 1, 2, name="sim_s0")

        ex._submit_job.assert_called_once()
        call_kwargs = ex._submit_job.call_args.kwargs
        assert "command" in call_kwargs
        assert call_kwargs["command"] == "python -m osimflow.remote_runner"
        assert "environment" in call_kwargs

        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        assert "OSIMFLOW_TASK_PAYLOAD" in env

        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])
        assert payload["schema_version"] == 1
        assert payload["name"] == "sim_s0"
        assert payload["step"] == "sim"
        assert payload["args"] == [1, 2]

    def test_submit_remote_command_override(self):
        ex = _make_mock_azure_executor()
        ex._submit_job = MagicMock(return_value="test-job-id")

        ex.submit(lambda: None, name="sim_s0", remote_command="custom command")

        call_kwargs = ex._submit_job.call_args.kwargs
        assert call_kwargs["command"] == "/bin/sh -c 'custom command'"


class TestGoogleBatchExecutorFnArgsMarshal:
    """Google Batch executor must marshal fn/args into OSIMFLOW_TASK_PAYLOAD."""

    def test_submit_builds_task_payload_with_correct_step(self):
        ex = _make_mock_google_executor()
        ex._submit_job = MagicMock(return_value="test-job-id")

        handle = ex.submit(lambda x, y: x + y, 1, 2, name="sim_s0")

        ex._submit_job.assert_called_once()
        call_kwargs = ex._submit_job.call_args.kwargs
        assert "command" in call_kwargs
        assert call_kwargs["command"] == ["python", "-m", "osimflow.remote_runner"]
        assert "environment" in call_kwargs

        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        assert "OSIMFLOW_TASK_PAYLOAD" in env

        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])
        assert payload["schema_version"] == 1
        assert payload["name"] == "sim_s0"
        assert payload["step"] == "sim"
        assert payload["args"] == [1, 2]

    def test_submit_remote_command_override(self):
        ex = _make_mock_google_executor()
        ex._submit_job = MagicMock(return_value="test-job-id")

        ex.submit(lambda: None, name="sim_s0", remote_command="custom command")

        call_kwargs = ex._submit_job.call_args.kwargs
        assert call_kwargs["command"] == ["/bin/sh", "-c", "custom command"]


class TestDockerSwarmExecutorFnArgsMarshal:
    """Docker Swarm executor must marshal fn/args into OSIMFLOW_TASK_PAYLOAD."""

    def test_submit_builds_task_payload_with_correct_step(self):
        ex = _make_mock_docker_swarm_executor()
        ex._check_docker_available = MagicMock(return_value=True)
        ex._submit_service = MagicMock(return_value="test-service")

        handle = ex.submit(lambda x, y: x + y, 1, 2, name="sim_s0")

        ex._submit_service.assert_called_once()
        call_kwargs = ex._submit_service.call_args.kwargs
        assert "command" in call_kwargs
        assert call_kwargs["command"] == ["python", "-m", "osimflow.remote_runner"]
        assert "task_payload" in call_kwargs

        payload = json.loads(call_kwargs["task_payload"])
        assert payload["schema_version"] == 1
        assert payload["name"] == "sim_s0"
        assert payload["step"] == "sim"
        assert payload["args"] == [1, 2]

    def test_submit_remote_command_override(self):
        ex = _make_mock_docker_swarm_executor()
        ex._check_docker_available = MagicMock(return_value=True)
        ex._submit_service = MagicMock(return_value="test-service")

        ex.submit(lambda: None, name="sim_s0", remote_command="custom command")

        call_kwargs = ex._submit_service.call_args.kwargs
        assert call_kwargs["command"] == ["/bin/sh", "-c", "custom command"]

    def test_submit_propagates_result_transport_env(self):
        ex = _make_mock_docker_swarm_executor()
        ex._check_docker_available = MagicMock(return_value=True)
        ex._submit_service = MagicMock(return_value="test-service")

        ex.submit(
            lambda: None,
            name="sim_s0",
            result_transport_mode="object_storage",
            result_storage_backend="s3",
            result_storage_bucket="test-bucket",
        )

        call_kwargs = ex._submit_service.call_args.kwargs
        assert call_kwargs["result_transport_mode"] == "object_storage"
        assert call_kwargs["result_storage_backend"] == "s3"
        assert call_kwargs["result_storage_bucket"] == "test-bucket"


class TestAllExecutorsRequiresRemoteRunnerPayload:
    """All four cloud executors must have requires_remote_runner_payload=True."""

    def test_aws_batch_requires_remote_runner_payload(self):
        ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        assert ex.requires_remote_runner_payload is True

    def test_azure_batch_requires_remote_runner_payload(self):
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        assert ex.requires_remote_runner_payload is True

    def test_google_batch_requires_remote_runner_payload(self):
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        assert ex.requires_remote_runner_payload is True

    def test_docker_swarm_requires_remote_runner_payload(self):
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
        assert ex.requires_remote_runner_payload is True


class TestTaskPayloadSerialization:
    """Test that task payload serialization matches remote_runner expectations."""

    def test_path_encoding_in_payload(self):
        """Paths in args should be encoded with transport path marker."""
        ex = _make_mock_aws_executor()
        ex._get_spot_price = MagicMock(return_value=0.03)
        ex._submit_job = MagicMock(return_value="test-job-id")

        path_arg = Path("/campaign/out/work/apply/s0")
        ex.submit(lambda p: p, path_arg, name="sim_s0")

        call_kwargs = ex._submit_job.call_args.kwargs
        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])

        # Path should be encoded with __osimflow_type__ marker
        assert payload["args"][0] == {
            "__osimflow_type__": "path",
            "value": "/campaign/out/work/apply/s0",
        }

    def test_result_hint_encoding_in_payload(self):
        """result_hint should be encoded in payload."""
        ex = _make_mock_aws_executor()
        ex._get_spot_price = MagicMock(return_value=0.03)
        ex._submit_job = MagicMock(return_value="test-job-id")

        hint = Path("/campaign/out/work/sim/s0")
        ex.submit(lambda: None, name="sim_s0", result_hint=hint)

        call_kwargs = ex._submit_job.call_args.kwargs
        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])

        assert payload["result_hint"] == {
            "__osimflow_type__": "path",
            "value": "/campaign/out/work/sim/s0",
        }

    def test_payload_decoding_roundtrip(self):
        """Payload should be decodable by remote_runner's decode_transport_value."""
        ex = _make_mock_aws_executor()
        ex._get_spot_price = MagicMock(return_value=0.03)
        ex._submit_job = MagicMock(return_value="test-job-id")

        path_arg = Path("/campaign/out/work/apply/s0")
        ex.submit(lambda p: p, path_arg, name="sim_s0")

        call_kwargs = ex._submit_job.call_args.kwargs
        env = {e["name"]: e["value"] for e in call_kwargs["environment"]}
        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])

        # Simulate remote_runner decoding
        decoded_args = [decode_transport_value(v) for v in payload["args"]]
        assert decoded_args[0] == Path("/campaign/out/work/apply/s0")
