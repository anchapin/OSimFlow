"""Fargate vs EC2 resource overrides for AWS Batch (issue #1808).

Requests are validated offline against the real botocore ``SubmitJob``
input schema.
"""

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from osimflow.executors import DEFAULT_STEP_RESOURCES
from osimflow.executors.aws_batch_executor import AWSBatchExecutor


def _executor(platform_caps: list[str] | None) -> tuple[AWSBatchExecutor, Any, Stubber]:
    client = boto3.client(
        "batch",
        region_name="us-east-1",
        aws_access_key_id="x",
        aws_secret_access_key="x",  # noqa: S106
    )
    stub = Stubber(client)
    ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
    ex.job_queue = "q"
    ex.job_definition = "jd"
    ex._client = client
    ex._get_client = lambda: client  # type: ignore[method-assign]
    ex._submit_job_with_retry = lambda kw: client.submit_job(**kw)  # type: ignore[method-assign]
    definition: dict[str, Any] = {
        "jobDefinitionName": "jd",
        "jobDefinitionArn": "arn:aws:batch:us-east-1:123456789012:job-definition/jd:1",
        "revision": 1,
        "type": "container",
        "status": "ACTIVE",
    }
    if platform_caps is not None:
        definition["platformCapabilities"] = platform_caps
    stub.add_response(
        "describe_job_definitions",
        {"jobDefinitions": [definition]},
        {"jobDefinitionName": "jd", "status": "ACTIVE"},
    )
    return ex, client, stub


def _submit(ex: AWSBatchExecutor, cpus: float, mem: int) -> str:
    return ex._submit_job(
        name="n", cpus=cpus, memory_mb=mem, time_min=5, environment=[{"name": "A", "value": "b"}]
    )


def test_fargate_uses_resource_requirements_validated_by_botocore() -> None:
    ex, _, stub = _executor(["FARGATE"])
    expected = {
        "jobName": "n",
        "jobQueue": "q",
        "jobDefinition": "jd",
        "containerOverrides": {
            "resourceRequirements": [
                {"type": "VCPU", "value": "0.5"},
                {"type": "MEMORY", "value": "1024"},
            ],
            "environment": [{"name": "A", "value": "b"}],
        },
        "timeout": {"attemptDurationSeconds": 300},
    }
    stub.add_response("submit_job", {"jobName": "n", "jobId": "j1"}, expected)
    with stub:
        assert _submit(ex, 0.5, 1024) == "j1"
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(("cpus", "mem"), [(1, 512), (0.5, 512), (3, 4096), (1, 2049), (2, 2048)])
def test_fargate_invalid_pair_fails_before_submit(cpus: float, mem: int) -> None:
    ex, _, stub = _executor(["FARGATE"])
    with stub:
        with pytest.raises(ValueError, match="Fargate"):
            _submit(ex, cpus, mem)
    # No submit_job response queued: stub would raise if it were called.


def test_invalid_pair_message_is_useful() -> None:
    ex, _, stub = _executor(["FARGATE"])
    with stub, pytest.raises(ValueError, match=r"1 vCPU / 512 MiB.*2048-8192"):
        _submit(ex, 1, 512)


def test_ec2_keeps_legacy_overrides() -> None:
    ex, _, stub = _executor(["EC2"])
    expected = {
        "jobName": "n",
        "jobQueue": "q",
        "jobDefinition": "jd",
        "containerOverrides": {
            "vcpus": 1,
            "memory": 512,
            "environment": [{"name": "A", "value": "b"}],
        },
        "timeout": {"attemptDurationSeconds": 300},
    }
    stub.add_response("submit_job", {"jobName": "n", "jobId": "j2"}, expected)
    with stub:
        assert _submit(ex, 1, 512) == "j2"


def test_missing_platform_capabilities_defaults_to_ec2() -> None:
    ex, _, stub = _executor(None)
    with stub:
        assert ex._job_definition_platform("jd") == "EC2"


@pytest.mark.parametrize("step", sorted(DEFAULT_STEP_RESOURCES))
def test_default_step_resources_are_legal_on_fargate(step: str) -> None:
    from osimflow.executors.aws_batch_executor import _validate_fargate_resources

    res = DEFAULT_STEP_RESOURCES[step]
    _validate_fargate_resources(res["cpus"], res["memory_mb"])
