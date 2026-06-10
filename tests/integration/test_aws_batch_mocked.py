"""Moto-based mocked integration tests for AWSBatchExecutor (issue #149).

G16a acceptance criteria:

  - submit -> poll -> SUCCEEDED: executor returns None (clean completion).
  - submit -> poll -> FAILED: executor raises RuntimeError with statusReason.
  - Exponential backoff: sleep intervals start at 5 s, double each poll,
    and cap at 60 s.
  - Job parameters: vcpus, memory, container image, environment variables
    are all passed correctly to submit_job.

Uses ``moto`` (mock AWS library) so the tests exercise the real boto3 wire
format against a local mock server — no AWS credentials needed.  The
``@mock_aws`` decorator sets up a fully functional mock AWS account with
IAM, Batch, and EC2 resources.

The tests are **unit-level** for the executor (no Campaign, no 6-step DAG)
to keep them fast and focused.  The Campaign-level integration test lives
in ``test_aws_batch_executor_stub.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from osimflow.executors import AWSBatchExecutor

# ---------------------------------------------------------------------------
# Helpers: set up the Batch mock infrastructure (VPC, compute env, job queue,
# job definition) so that submit_job / describe_jobs work end-to-end.
# ---------------------------------------------------------------------------

_REGION = "us-east-1"
_QUEUE_NAME = "osimflow-test-queue"
_JOB_DEF_NAME = "osimflow-test-job-def"


def _setup_batch_infra(client: object) -> tuple[str, str]:
    """Create the minimal Batch infra (compute env + queue + job def).

    Returns ``(job_queue_arn, job_definition_arn)`` so tests can submit
    against them.  This mirrors what an AWS account admin would do via
    CloudFormation / Terraform.

    We use ``object`` for the client type because moto's stub client is
    not a real ``boto3.client`` instance — it is a dynamically generated
    proxy that satisfies the same call interface.
    """
    batch = client  # type: ignore[assignment]

    # 1. Networking: VPC + subnets + security group (required by compute env).
    ec2 = boto3.client("ec2", region_name=_REGION)
    vpc_resp = ec2.create_vpc(CidrBlock="10.0.0.0/16")
    vpc_id = vpc_resp["Vpc"]["VpcId"]
    subnet_resp = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.1.0/24")
    subnet_id = subnet_resp["Subnet"]["SubnetId"]
    sg_resp = ec2.create_security_group(
        GroupName="osimflow-test-sg",
        Description="Test SG for moto Batch",
        VpcId=vpc_id,
    )
    sg_id = sg_resp["GroupId"]

    # 2. IAM role for the compute environment (Batch requires it).
    iam = boto3.client("iam", region_name=_REGION)
    iam.create_role(
        RoleName="osimflow-batch-role",
        AssumeRolePolicyDocument="{}",
        Path="/",
    )
    # ``create_instance_profile`` requires a path in some moto versions.
    try:
        iam.create_instance_profile(InstanceProfileName="osimflow-batch-profile", Path="/")
    except Exception:  # noqa: BLE001 — already exists in some moto versions
        pass

    # 3. Compute environment.
    ce_resp = batch.create_compute_environment(
        computeEnvironmentName="osimflow-test-ce",
        type="MANAGED",
        serviceRole="arn:aws:iam::123456789012:role/osimflow-batch-role",
        computeResources={
            "type": "EC2",
            "minvCpus": 0,
            "maxvCpus": 256,
            "desiredvCpus": 0,
            "instanceTypes": ["m5.large"],
            "subnets": [subnet_id],
            "securityGroupIds": [sg_id],
            "instanceRole": "arn:aws:iam::123456789012:instance-profile/osimflow-batch-profile",
        },
    )
    ce_arn = ce_resp["computeEnvironmentArn"]

    # 4. Job queue.
    jq_resp = batch.create_job_queue(
        jobQueueName=_QUEUE_NAME,
        state="ENABLED",
        priority=1,
        computeEnvironmentOrder=[
            {"order": 1, "computeEnvironment": ce_arn},
        ],
    )
    jq_arn = jq_resp["jobQueueArn"]

    # 5. Job definition (container-based).
    jd_resp = batch.register_job_definition(
        jobDefinitionName=_JOB_DEF_NAME,
        type="container",
        containerProperties={
            "image": "public.ecr.aws/docker/library/alpine:latest",
            "vcpus": 1,
            "memory": 512,
            "command": ["echo", "hello"],
        },
    )
    jd_arn = jd_resp["jobDefinitionArn"]

    return jq_arn, jd_arn


# ---------------------------------------------------------------------------
# Test: submit -> poll -> SUCCEEDED
# ---------------------------------------------------------------------------
@mock_aws
def test_submit_poll_succeeded() -> None:
    """Submit a job, mock-describe it as SUCCEEDED on first poll, and
    verify the executor returns None (clean completion signal)."""
    batch_client = boto3.client("batch", region_name=_REGION)
    jq_arn, jd_arn = _setup_batch_infra(batch_client)

    executor = AWSBatchExecutor(
        job_queue=_QUEUE_NAME,
        job_definition=_JOB_DEF_NAME,
        poll_interval_s=0.01,
        max_poll_interval_s=0.02,
        region_name=_REGION,
    )
    # Patch the lazy client so the executor uses the moto-backed one.
    executor._client = batch_client  # noqa: SLF001

    handle = executor.submit(
        lambda: "result",  # noqa: ARG005 — not run locally
        name="test-succeed",
        cpus=2,
        memory_mb=1024,
        time_min=10,
        container="openstudio_cli_image:3.5.0",
        openstudio_version="3.5.0",
    )

    # The handle should report done (moto submits the job and it can be
    # described).  moto by default does not advance job status, so we
    # need to manually mark the job as SUCCEEDED via the Batch mock.
    # In moto, jobs submitted via submit_job start in SUBMITTED/RUNNABLE
    # state.  We use describe_jobs to confirm the job exists, then
    # directly set the status.
    #
    # However, moto's Batch mock may or may not auto-advance job states.
    # The most reliable approach is to patch describe_jobs to return
    # SUCCEEDED on the first call, which is the same pattern the
    # existing tests use.  This is intentional: we are testing the
    # executor's polling logic, not moto's state machine.
    original_describe = batch_client.describe_jobs

    def _describe_then_succeed(**kwargs: object) -> dict[str, object]:
        # First call: let moto return the real job (RUNNING or SUBMITTED).
        resp = original_describe(**kwargs)
        jobs = resp.get("jobs", [])
        if jobs and jobs[0].get("status") not in ("SUCCEEDED", "FAILED"):
            # Force SUCCEEDED for the test.
            jobs[0]["status"] = "SUCCEEDED"
            jobs[0]["statusReason"] = "All tasks completed"
        return resp  # type: ignore[return-value]

    with patch.object(batch_client, "describe_jobs", side_effect=_describe_then_succeed):
        result = handle.result(timeout=5)
        # done() should return True while the patch is active (the mock
        # returns SUCCEEDED).
        assert handle.done() is True

    assert result is None
    executor.shutdown()


# ---------------------------------------------------------------------------
# Test: submit -> poll -> FAILED
# ---------------------------------------------------------------------------
@mock_aws
def test_submit_poll_failed_raises_runtime_error() -> None:
    """Submit a job, force FAILED status, and verify RuntimeError is
    raised with the statusReason in the message."""
    batch_client = boto3.client("batch", region_name=_REGION)
    _setup_batch_infra(batch_client)

    executor = AWSBatchExecutor(
        job_queue=_QUEUE_NAME,
        job_definition=_JOB_DEF_NAME,
        poll_interval_s=0.01,
        max_poll_interval_s=0.02,
        region_name=_REGION,
    )
    executor._client = batch_client  # noqa: SLF001

    handle = executor.submit(
        lambda: None,
        name="test-fail",
        cpus=1,
        memory_mb=512,
    )

    # Patch describe_jobs to return FAILED with a statusReason.
    original_describe = batch_client.describe_jobs

    def _describe_then_fail(**kwargs: object) -> dict[str, object]:
        resp = original_describe(**kwargs)
        jobs = resp.get("jobs", [])
        if jobs:
            jobs[0]["status"] = "FAILED"
            jobs[0]["statusReason"] = "Essential container exited with code 137"
        return resp  # type: ignore[return-value]

    with (
        patch.object(batch_client, "describe_jobs", side_effect=_describe_then_fail),
        pytest.raises(RuntimeError, match="Essential container exited with code 137"),
    ):
        handle.result(timeout=5)

    executor.shutdown()


# ---------------------------------------------------------------------------
# Test: exponential backoff
# ---------------------------------------------------------------------------
@mock_aws
def test_exponential_backoff_starts_at_5s_caps_at_60s() -> None:
    """Verify polling interval starts at poll_interval_s (5 s), doubles
    each iteration, and caps at max_poll_interval_s (60 s).

    Uses ``unittest.mock.patch("time.sleep")`` to avoid actually waiting.
    The test asserts the exact sleep durations: 5, 10, 20, 40, 60, 60, …
    """
    batch_client = boto3.client("batch", region_name=_REGION)
    _setup_batch_infra(batch_client)

    executor = AWSBatchExecutor(
        job_queue=_QUEUE_NAME,
        job_definition=_JOB_DEF_NAME,
        poll_interval_s=5.0,
        max_poll_interval_s=60.0,
        region_name=_REGION,
    )
    executor._client = batch_client  # noqa: SLF001

    handle = executor.submit(
        lambda: None,
        name="test-backoff",
        cpus=1,
        memory_mb=512,
    )

    # Describe_jobs returns RUNNING 6 times, then SUCCEEDED on the 7th.
    call_count = 0
    original_describe = batch_client.describe_jobs

    def _describe_running_then_succeed(**kwargs: object) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        resp = original_describe(**kwargs)
        jobs = resp.get("jobs", [])
        if jobs:
            if call_count <= 6:
                jobs[0]["status"] = "RUNNING"
                jobs[0]["statusReason"] = "Job running"
            else:
                jobs[0]["status"] = "SUCCEEDED"
                jobs[0]["statusReason"] = "OK"
        return resp  # type: ignore[return-value]

    sleep_durations: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        sleep_durations.append(seconds)

    with (
        patch.object(batch_client, "describe_jobs", side_effect=_describe_running_then_succeed),
        patch("time.sleep", side_effect=_fake_sleep),
    ):
        result = handle.result(timeout=120)

    assert result is None

    # 6 RUNNING polls = 6 sleeps before the 7th (SUCCEEDED) poll.
    # Expected: 5.0, 10.0, 20.0, 40.0, 60.0, 60.0
    assert len(sleep_durations) == 6, (
        f"expected 6 sleeps, got {len(sleep_durations)}: {sleep_durations}"
    )
    expected = [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    for actual, exp in zip(sleep_durations, expected):  # noqa: B905
        assert actual == exp, (
            f"sleep mismatch at index {sleep_durations.index(actual)}: "
            f"expected {exp}, got {actual}. Full: {sleep_durations}"
        )

    executor.shutdown()


# ---------------------------------------------------------------------------
# Test: job parameters (vcpus, memory, container image, env vars)
# ---------------------------------------------------------------------------
@mock_aws
def test_submit_job_carries_correct_parameters() -> None:
    """Verify the executor passes correct parameters to submit_job:
    vcpus, memory, containerOverrides (environment), and timeout."""
    batch_client = boto3.client("batch", region_name=_REGION)
    _setup_batch_infra(batch_client)

    executor = AWSBatchExecutor(
        job_queue=_QUEUE_NAME,
        job_definition=_JOB_DEF_NAME,
        poll_interval_s=0.01,
        max_poll_interval_s=0.02,
        region_name=_REGION,
    )
    executor._client = batch_client  # noqa: SLF001

    handle = executor.submit(
        lambda: None,
        name="test-params",
        cpus=4,
        memory_mb=8192,
        time_min=240,
        container="openstudio_cli_image:3.5.0",
        openstudio_version="3.5.0",
    )

    # Now inspect the submitted job via describe_jobs to verify the
    # parameters were passed correctly through the Batch API.
    resp = batch_client.describe_jobs(jobs=[handle.job_id])
    jobs = resp.get("jobs", [])
    assert len(jobs) == 1, f"expected 1 job, got {len(jobs)}"

    job = jobs[0]

    # The job name should match what we passed.
    assert job["jobName"] == "test-params"

    # The job should be associated with our queue and job definition.
    assert _QUEUE_NAME in job.get("jobQueue", "")
    assert _JOB_DEF_NAME in job.get("jobDefinition", "")

    # Container overrides should carry vcpus and memory.
    container = job.get("container", {})
    # Note: moto may structure containerOverrides differently than the
    # raw API.  We check the submitted parameters via the job's
    # container block.
    assert container.get("vcpus") == 4, f"vcpus mismatch: {container}"
    assert container.get("memory") == 8192, f"memory mismatch: {container}"

    # The environment should carry OSIMFLOW_OS_VERSION and OSIMFLOW_CONTAINER.
    env_list = container.get("environment", [])
    env_dict = {e["name"]: e["value"] for e in env_list}
    assert env_dict.get("OSIMFLOW_OS_VERSION") == "3.5.0", (
        f"OSIMFLOW_OS_VERSION missing or wrong: {env_dict}"
    )
    assert env_dict.get("OSIMFLOW_CONTAINER") == "openstudio_cli_image:3.5.0", (
        f"OSIMFLOW_CONTAINER missing or wrong: {env_dict}"
    )

    # Timeout should be time_min * 60 seconds.
    timeout = job.get("timeout", {})
    assert timeout.get("attemptDurationSeconds") == 240 * 60, f"timeout mismatch: {timeout}"

    executor.shutdown()


# ---------------------------------------------------------------------------
# Test: done() returns False for in-progress jobs, True for terminal states
# ---------------------------------------------------------------------------
@mock_aws
def test_done_reflects_job_status() -> None:
    """Verify that Handle.done() returns False for RUNNING and True for
    SUCCEEDED / FAILED terminal states."""
    batch_client = boto3.client("batch", region_name=_REGION)
    _setup_batch_infra(batch_client)

    executor = AWSBatchExecutor(
        job_queue=_QUEUE_NAME,
        job_definition=_JOB_DEF_NAME,
        poll_interval_s=0.01,
        max_poll_interval_s=0.02,
        region_name=_REGION,
    )
    executor._client = batch_client  # noqa: SLF001

    handle = executor.submit(
        lambda: None,
        name="test-done",
        cpus=1,
        memory_mb=512,
    )

    # The job starts in a non-terminal state (SUBMITTED or RUNNABLE in moto).
    # done() should return False.
    assert handle.done() is False, "expected done()=False for non-terminal job"

    # Force SUCCEEDED via describe_jobs patch and check done().
    original_describe = batch_client.describe_jobs

    def _describe_succeeded(**kwargs: object) -> dict[str, object]:
        resp = original_describe(**kwargs)
        jobs = resp.get("jobs", [])
        if jobs:
            jobs[0]["status"] = "SUCCEEDED"
            jobs[0]["statusReason"] = "OK"
        return resp  # type: ignore[return-value]

    with patch.object(batch_client, "describe_jobs", side_effect=_describe_succeeded):
        assert handle.done() is True, "expected done()=True for SUCCEEDED job"

    executor.shutdown()


# ---------------------------------------------------------------------------
# Test: describe_jobs returns no jobs → RuntimeError
# ---------------------------------------------------------------------------
@mock_aws
def test_describe_jobs_no_jobs_raises_runtime_error() -> None:
    """If describe_jobs returns an empty jobs list, the executor should
    raise RuntimeError (the jobId is unknown)."""
    batch_client = boto3.client("batch", region_name=_REGION)
    _setup_batch_infra(batch_client)

    executor = AWSBatchExecutor(
        job_queue=_QUEUE_NAME,
        job_definition=_JOB_DEF_NAME,
        poll_interval_s=0.01,
        max_poll_interval_s=0.02,
        region_name=_REGION,
    )
    executor._client = batch_client  # noqa: SLF001

    # Submit a real job to get a handle, then patch describe_jobs to
    # return empty.
    handle = executor.submit(
        lambda: None,
        name="test-no-job",
        cpus=1,
        memory_mb=512,
    )

    with (
        patch.object(batch_client, "describe_jobs", return_value={"jobs": []}),
        pytest.raises(RuntimeError, match="returned no job"),
    ):
        handle.result(timeout=5)

    executor.shutdown()
