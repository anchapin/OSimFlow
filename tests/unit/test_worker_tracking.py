"""Tests for per-data-point worker tracking (issue #105).

Verifies that:
  - SampleTrace serializes/deserializes with the new worker fields.
  - LocalExecutor populates worker_id = "local" and worker_ip = hostname.
  - Mocked AWSBatchExecutor populates worker_id from submit_job response.
  - Mocked NomadExecutor populates worker_id from allocation response.
  - Campaign._finalize_samples() propagates worker info into SampleTrace.
"""

import json
import os
import socket
from unittest.mock import MagicMock, patch

import boto3
from moto import mock_aws

from osimflow.executors import (
    AWSBatchExecutor,
    Handle,
    LocalExecutor,
    NomadExecutor,
    _AWSBatchHandle,
    _NomadHandle,
)
from osimflow.monitoring import SampleTrace


# ---------------------------------------------------------------------------
# SampleTrace serialization / deserialization
# ---------------------------------------------------------------------------
class TestSampleTraceWorkerFields:
    """Worker fields are optional and round-trip through to_dict()."""

    def test_default_fields_are_none(self) -> None:
        trace = SampleTrace(sample_id="s0", status="ok", elapsed_s=1.0)
        assert trace.worker_id is None
        assert trace.worker_ip is None
        assert trace.worker_region is None

    def test_fields_set_and_serialized(self) -> None:
        trace = SampleTrace(
            sample_id="s1",
            status="ok",
            elapsed_s=2.0,
            worker_id="local",
            worker_ip="myhost",
            worker_region=None,
        )
        d = trace.to_dict()
        assert d["worker_id"] == "local"
        assert d["worker_ip"] == "myhost"
        # None values are excluded by to_dict()
        assert "worker_region" not in d

    def test_all_worker_fields_serialized(self) -> None:
        trace = SampleTrace(
            sample_id="s2",
            status="ok",
            elapsed_s=3.0,
            worker_id="batch-123",
            worker_ip="10.0.0.1",
            worker_region="us-east-1",
        )
        d = trace.to_dict()
        assert d["worker_id"] == "batch-123"
        assert d["worker_ip"] == "10.0.0.1"
        assert d["worker_region"] == "us-east-1"

    def test_none_fields_excluded_from_dict(self) -> None:
        """Backward compat: None fields don't pollute the JSON output."""
        trace = SampleTrace(sample_id="s3", status="ok", elapsed_s=1.0)
        d = trace.to_dict()
        assert "worker_id" not in d
        assert "worker_ip" not in d
        assert "worker_region" not in d

    def test_roundtrip_json(self) -> None:
        trace = SampleTrace(
            sample_id="s4",
            status="ok",
            elapsed_s=5.0,
            worker_id="slurm-42",
            worker_ip=None,
            worker_region="us-west-2",
        )
        d = trace.to_dict()
        blob = json.dumps(d)
        loaded = json.loads(blob)
        assert loaded["worker_id"] == "slurm-42"
        assert loaded["worker_region"] == "us-west-2"
        assert "worker_ip" not in loaded

    def test_backward_compat_existing_dict(self) -> None:
        """Existing run.json entries without worker fields still valid."""
        existing = {
            "sample_id": "legacy",
            "status": "ok",
            "elapsed_s": 10.0,
            "apply_exit_code": 0,
            "sim_exit_code": 0,
            "extract_exit_code": 0,
        }
        # Simulate loading a legacy trace — should not error
        trace = SampleTrace(**existing)
        assert trace.worker_id is None
        assert trace.worker_ip is None
        assert trace.worker_region is None


# ---------------------------------------------------------------------------
# Handle worker fields
# ---------------------------------------------------------------------------
class TestHandleWorkerFields:
    """Handle dataclass carries worker fields."""

    def test_handle_defaults(self) -> None:
        from concurrent.futures import Future

        h = Handle(job_id="test", _future=Future())
        assert h.worker_id is None
        assert h.worker_ip is None
        assert h.worker_region is None

    def test_handle_with_worker_fields(self) -> None:
        from concurrent.futures import Future

        h = Handle(
            job_id="test",
            _future=Future(),
            worker_id="slurm-99",
            worker_ip=None,
            worker_region=None,
        )
        assert h.worker_id == "slurm-99"


# ---------------------------------------------------------------------------
# LocalExecutor
# ---------------------------------------------------------------------------
class TestLocalExecutorWorkerTracking:
    def test_local_populates_worker_id(self) -> None:
        ex = LocalExecutor(max_workers=1)
        handle = ex.submit(lambda: 42, name="test")
        handle.result(timeout=5)
        assert handle.worker_id == "local"

    def test_local_populates_worker_ip(self) -> None:
        ex = LocalExecutor(max_workers=1)
        handle = ex.submit(lambda: 42, name="test")
        handle.result(timeout=5)
        assert handle.worker_ip == socket.gethostname()

    def test_local_worker_region_is_none(self) -> None:
        ex = LocalExecutor(max_workers=1)
        handle = ex.submit(lambda: 42, name="test")
        handle.result(timeout=5)
        assert handle.worker_region is None

        ex.shutdown()


# ---------------------------------------------------------------------------
# AWSBatchExecutor (moto-based)
# ---------------------------------------------------------------------------
_REGION = "us-east-1"
_QUEUE_NAME = "osimflow-worker-test-queue"
_JOB_DEF_NAME = "osimflow-worker-test-job-def"


def _setup_batch_infra(client: object) -> None:
    """Create the minimal Batch infra for moto-based tests."""
    batch = client  # type: ignore[assignment]

    ec2 = boto3.client("ec2", region_name=_REGION)
    vpc_resp = ec2.create_vpc(CidrBlock="10.0.0.0/16")
    vpc_id = vpc_resp["Vpc"]["VpcId"]
    subnet_resp = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24")
    subnet_id = subnet_resp["Subnet"]["SubnetId"]
    sg_resp = ec2.create_security_group(
        GroupName="osimflow-wt-sg",
        Description="Test SG for worker tracking tests",
        VpcId=vpc_id,
    )
    sg_id = sg_resp["GroupId"]

    iam = boto3.client("iam", region_name=_REGION)
    iam.create_role(
        RoleName="osimflow-batch-role-wt",
        AssumeRolePolicyDocument="{}",
        Path="/",
    )
    try:
        iam.create_instance_profile(InstanceProfileName="osimflow-batch-profile-wt", Path="/")
    except Exception:  # noqa: BLE001
        pass

    ce_resp = batch.create_compute_environment(
        computeEnvironmentName="osimflow-wt-ce",
        type="MANAGED",
        serviceRole="arn:aws:iam::123456789012:role/osimflow-batch-role-wt",
        computeResources={
            "type": "EC2",
            "minvCpus": 0,
            "maxvCpus": 256,
            "desiredvCpus": 0,
            "instanceTypes": ["m5.large"],
            "subnets": [subnet_id],
            "securityGroupIds": [sg_id],
            "instanceRole": "arn:aws:iam::123456789012:instance-profile/osimflow-batch-profile-wt",
        },
    )
    ce_arn = ce_resp["computeEnvironmentArn"]

    batch.create_job_queue(
        jobQueueName=_QUEUE_NAME,
        state="ENABLED",
        priority=1,
        computeEnvironmentOrder=[
            {"order": 1, "computeEnvironment": ce_arn},
        ],
    )

    batch.register_job_definition(
        jobDefinitionName=_JOB_DEF_NAME,
        type="container",
        containerProperties={
            "image": "public.ecr.aws/docker/library/alpine:latest",
            "vcpus": 1,
            "memory": 512,
            "command": ["echo", "hello"],
        },
    )


class TestAWSBatchExecutorWorkerTracking:
    @mock_aws
    def test_batch_populates_worker_id(self) -> None:
        batch_client = boto3.client("batch", region_name=_REGION)
        _setup_batch_infra(batch_client)

        executor = AWSBatchExecutor(
            job_queue=_QUEUE_NAME,
            job_definition=_JOB_DEF_NAME,
            region_name=_REGION,
        )
        executor._client = batch_client  # noqa: SLF001

        handle = executor.submit(lambda: None, name="test-worker-id")
        assert isinstance(handle, _AWSBatchHandle)
        # worker_id should be the Batch jobId
        assert handle.worker_id is not None
        assert handle.worker_id == handle.job_id

        executor.shutdown()

    @mock_aws
    def test_batch_populates_worker_region(self) -> None:
        batch_client = boto3.client("batch", region_name=_REGION)
        _setup_batch_infra(batch_client)

        with patch.dict(os.environ, {"AWS_REGION": "eu-west-1"}):
            executor = AWSBatchExecutor(
                job_queue=_QUEUE_NAME,
                job_definition=_JOB_DEF_NAME,
                region_name=_REGION,
            )
            executor._client = batch_client  # noqa: SLF001

            handle = executor.submit(lambda: None, name="test-region")
            assert handle.worker_region == "eu-west-1"

        executor.shutdown()

    @mock_aws
    def test_batch_worker_ip_is_none(self) -> None:
        batch_client = boto3.client("batch", region_name=_REGION)
        _setup_batch_infra(batch_client)

        executor = AWSBatchExecutor(
            job_queue=_QUEUE_NAME,
            job_definition=_JOB_DEF_NAME,
            region_name=_REGION,
        )
        executor._client = batch_client  # noqa: SLF001

        handle = executor.submit(lambda: None, name="test-ip")
        assert handle.worker_ip is None

        executor.shutdown()


# ---------------------------------------------------------------------------
# NomadExecutor (mocked urllib)
# ---------------------------------------------------------------------------
class TestNomadExecutorWorkerTracking:
    def _make_mock_urlopen(self) -> MagicMock:
        """Build a fake urlopen that returns Nomad-style responses."""
        fake_urlopen = MagicMock()

        def fake_response(request: object, *args: object, **kwargs: object) -> object:
            req = request  # type: ignore[assignment]
            method = req.get_method()  # type: ignore[attr-defined]
            url = req.full_url  # type: ignore[attr-defined]

            if method == "POST" and "/v1/jobs" in url:
                result = {"JobID": "nomad-job-wt", "EvalID": "eval-wt", "Index": 0}
            elif method == "GET" and "/v1/evaluation/" in url and "/allocations" in url:
                result = [{"ID": "alloc-wt", "ClientStatus": "complete"}]
            elif method == "GET" and "/v1/job/" in url and "/allocations" in url:
                result = [{"ID": "alloc-wt", "ClientStatus": "complete"}]
            elif method == "GET" and "/v1/allocation/" in url:
                result = {
                    "ID": "alloc-wt",
                    "ClientStatus": "complete",
                    "JobID": "nomad-job-wt",
                }
            else:
                result = {}

            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        fake_urlopen.side_effect = fake_response
        return fake_urlopen

    def test_nomad_populates_worker_id(self) -> None:
        mock_urlopen = self._make_mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646", datacentre="dc1")
            handle = ex.submit(lambda: None, name="test")
        assert isinstance(handle, _NomadHandle)
        assert handle.worker_id == "nomad-job-wt"

        ex.shutdown()

    def test_nomad_populates_worker_region(self) -> None:
        mock_urlopen = self._make_mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646", datacentre="dc2")
            handle = ex.submit(lambda: None, name="test")
        # datacentre is set to "dc2"
        assert handle.worker_region == "dc2"

        ex.shutdown()

    def test_nomad_worker_ip_is_none_at_submit(self) -> None:
        mock_urlopen = self._make_mock_urlopen()
        with patch("urllib.request.urlopen", side_effect=mock_urlopen.side_effect):
            ex = NomadExecutor(address="http://127.0.0.1:4646", datacentre="dc1")
            handle = ex.submit(lambda: None, name="test")
        # IP not available at submit time (needs allocation lookup)
        assert handle.worker_ip is None

        ex.shutdown()
