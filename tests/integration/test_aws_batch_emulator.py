"""AWSBatchExecutor and the S3 detach store against AWS Emulator, unmocked.

Only runs when ``OSIMFLOW_AWS_EMULATOR=1`` with the AWS SDK already pointed at
a running AWS Emulator (https://github.com/aws/aws-emulator) through the
standard ``AWS_ENDPOINT_URL`` / ``AWS_SHARED_CREDENTIALS_FILE`` /
``AWS_CA_BUNDLE`` variables. See ``.github/workflows/aws-emulator.yml``.

This sits between the mocked-boto3 suites (which assert the calls the code
*intends* to make) and ``test_aws_batch_real.py`` (real account, nightly): real
botocore serialization, real Batch/S3 validation errors, no account.

Scope rules:

* Never assert on a job's outcome, exit code or duration. Emulated Batch does
  not run the worker image; jobs here sit RUNNABLE on a DISABLED compute
  environment, so read-back is deterministic.
* Never draw IAM conclusions: the emulator does not evaluate policies.
* Emulator credentials are static keys, so the executor needs
  ``allow_long_lived_credentials=True``, and only in this module.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_AWS_EMULATOR") != "1",
    reason="Set OSIMFLOW_AWS_EMULATOR=1 (with the SDK pointed at AWS Emulator) to run",
)

EMULATOR_ACCOUNT = "000000000000"


@pytest.fixture(scope="module")
def emulator() -> dict[str, Any]:
    """Fail closed unless this is the emulator, then build a Batch queue."""
    import boto3

    endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    if not re.match(r"^https?://(127\.0\.0\.1|localhost)[:/]", endpoint):
        pytest.fail(f"AWS_ENDPOINT_URL must be a loopback emulator endpoint, got {endpoint!r}")
    account = boto3.client("sts").get_caller_identity()["Account"]
    if account != EMULATOR_ACCOUNT:
        pytest.fail(f"refusing to run: account {account} is not the emulator's {EMULATOR_ACCOUNT}")

    tag = uuid.uuid4().hex[:8]
    ec2, batch = boto3.client("ec2"), boto3.client("batch")
    # A real emulated subnet; a made-up one makes the emulator simulate job outcomes.
    vpc = ec2.create_vpc(CidrBlock="10.43.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.43.1.0/24")["Subnet"]["SubnetId"]
    sg = ec2.create_security_group(GroupName=f"osf-{tag}", Description="osf", VpcId=vpc)["GroupId"]
    batch.create_compute_environment(
        computeEnvironmentName=f"osf-ce-{tag}",
        type="MANAGED",
        state="DISABLED",  # nothing is dispatched: jobs stay RUNNABLE
        computeResources={
            "type": "FARGATE",
            "maxvCpus": 4,
            "subnets": [subnet],
            "securityGroupIds": [sg],
        },
    )
    batch.create_job_queue(
        jobQueueName=f"osf-q-{tag}",
        priority=1,
        computeEnvironmentOrder=[{"order": 1, "computeEnvironment": f"osf-ce-{tag}"}],
    )
    batch.register_job_definition(
        jobDefinitionName=f"osf-jd-{tag}",
        type="container",
        platformCapabilities=["FARGATE"],
        containerProperties={
            "image": "osimflow-worker:test",
            "command": ["true"],
            "resourceRequirements": [
                {"type": "VCPU", "value": "1"},
                {"type": "MEMORY", "value": "2048"},
            ],
            "executionRoleArn": f"arn:aws:iam::{EMULATOR_ACCOUNT}:role/osf-exec",
        },
    )
    return {"queue": f"osf-q-{tag}", "jobdef": f"osf-jd-{tag}", "batch": batch}


@pytest.fixture
def payload_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", "emulator-test-secret")


def _executor(emulator: dict[str, Any], **kwargs: Any) -> Any:
    from osimflow.executors import AWSBatchExecutor

    return AWSBatchExecutor(
        job_queue=emulator["queue"],
        job_definition=emulator["jobdef"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        poll_interval_s=0.5,
        max_poll_interval_s=1.0,
        **kwargs,
    )


def _work() -> int:
    return 42


def test_default_executor_rejects_emulator_static_keys(
    emulator: dict[str, Any], payload_secret: None
) -> None:
    """The IAM-role-only default must refuse static keys, emulator ones included."""
    with pytest.raises(RuntimeError, match="long-lived"):
        _executor(emulator).submit(_work, name="osf-default", memory_mb=2048)


def test_submit_job_read_back_matches_request(
    emulator: dict[str, Any], payload_secret: None
) -> None:
    handle = _executor(emulator, allow_long_lived_credentials=True).submit(
        _work, name="osf-readback", memory_mb=2048
    )
    job = emulator["batch"].describe_jobs(jobs=[handle.job_id])["jobs"][0]

    assert job["jobName"].startswith("osf-readback")
    assert job["jobQueue"].endswith(f"job-queue/{emulator['queue']}")
    assert re.search(rf"job-definition/{emulator['jobdef']}:\d+$", job["jobDefinition"])
    assert job["status"] in {"SUBMITTED", "PENDING", "RUNNABLE"}
    env = {e["name"]: e["value"] for e in job["container"].get("environment", [])}
    assert {"OSIMFLOW_TASK_PAYLOAD", "OSIMFLOW_TASK_PAYLOAD_SIG"} <= env.keys()
    resources = {r["type"]: r["value"] for r in job["container"].get("resourceRequirements", [])}
    assert resources.get("MEMORY") == "2048"


def test_cancel_moves_runnable_job_to_failed(
    emulator: dict[str, Any], payload_secret: None
) -> None:
    handle = _executor(emulator, allow_long_lived_credentials=True).submit(
        _work, name="osf-cancel", memory_mb=2048
    )
    assert handle.cancel() is True

    deadline = time.monotonic() + 30
    while True:
        job = emulator["batch"].describe_jobs(jobs=[handle.job_id])["jobs"][0]
        if job["status"] == "FAILED" or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    assert job["status"] == "FAILED"
    assert "cancel" in (job.get("statusReason") or "").lower()


def test_unknown_job_queue_surfaces_batch_error(
    emulator: dict[str, Any], payload_secret: None
) -> None:
    from osimflow.executors import AWSBatchExecutor

    ex = AWSBatchExecutor(
        job_queue=f"osf-missing-{uuid.uuid4().hex[:6]}",
        job_definition=emulator["jobdef"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        allow_long_lived_credentials=True,
        max_retries=0,
    )
    with pytest.raises(Exception, match="(?i)queue"):
        ex.submit(_work, name="osf-noqueue", memory_mb=2048)


def test_s3_detach_store_cycle_honours_endpoint_env(
    emulator: dict[str, Any], tmp_path: Path
) -> None:
    """S3CampaignStore over S3Storage with no explicit endpoint: the SDK must
    pick up AWS_ENDPOINT_URL (a missed endpoint would go to real AWS and fail
    on the emulator's keys)."""
    import boto3

    from osimflow.s3_campaign import (
        COMPLETE_MARKER,
        FAILED_MARKER,
        S3CampaignError,
        S3CampaignStore,
        new_handoff,
    )
    from osimflow.storage import S3Storage

    bucket = f"osf-detach-{uuid.uuid4().hex[:8]}"
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket=bucket)
    store = S3CampaignStore(S3Storage(bucket, "campaigns"), tmp_path / "scratch")
    cid, sids = "camp", ["0000", "0001", "0002"]

    outcome, claim = store.claim_submission(cid)
    assert outcome == "acquired" and claim is not None
    assert store.claim_submission(cid)[0] == "in_progress"
    for sid in sids:
        store.record_job(cid, sid, f"job-{sid}", f"osf-{cid}-{sid}")
    samples = tmp_path / "samples.json"
    samples.write_text(json.dumps([{"sample_id": s} for s in sids]))
    store.write_handoff(
        new_handoff(
            cid, sids, openstudio_version="3.10.0", kpis=None, job_ids={s: f"job-{s}" for s in sids}
        ),
        samples,
    )
    store.finish_claim(claim)
    assert store.claim_submission(cid)[0] == "already_submitted"
    assert store.status(cid)["state"] == "running"

    with pytest.raises(S3CampaignError):
        store.download(cid, tmp_path / "early")  # strict download refuses a running campaign

    # What workers upload, written straight to S3.
    base = f"campaigns/{cid}/work/sim"
    s3.put_object(Bucket=bucket, Key=f"{base}/0000/out.osw", Body=b'{"completed_status":"Success"}')
    s3.put_object(Bucket=bucket, Key=f"{base}/0000/{COMPLETE_MARKER}", Body=b"")
    s3.put_object(Bucket=bucket, Key=f"{base}/0001/{FAILED_MARKER}", Body=b"boom")
    st = store.status(cid)
    assert (st["state"], st["failed"], st["pending"]) == ("running", ["0001"], ["0002"])

    overlay = store.status(cid, substrate_failures={"0002": "spot interruption"})
    assert overlay["state"] == "completed_with_failures" and overlay["failed"] == ["0001", "0002"]

    s3.put_object(Bucket=bucket, Key=f"{base}/0002/{COMPLETE_MARKER}", Body=b"")
    final = store.status(cid)
    assert (final["state"], final["succeeded"], final["failed"]) == (
        "completed_with_failures",
        2,
        ["0001"],
    )
    assert [h.campaign_id for h in store.list_campaigns()] == [cid]

    out = tmp_path / "out"
    store.download(cid, out, include_artifacts=True)
    assert (out / "aggregated_results.csv").is_file()
    assert (out / "failed_simulations.csv").is_file()
    assert (out / "work" / "sim" / "0000" / "out.osw").is_file()
