"""Tests for issue #5 — wire AWSBatchExecutor to boto3.

Acceptance criteria from the issue:
  - `osimflow run --executor aws_batch --aws-batch-queue <X>` submits N tasks
    to Batch and the tasks run in the `openstudio_cli_image:<version>`
    container.
  - Failed Batch tasks re-raise with a clear `statusReason` so
    `osimflow.campaign` logs the failure correctly.
  - A pytest smoke test that mocks `boto3.client('batch')` to verify the
    right `submit_job` call is made (no real AWS account needed in CI).
  - `pyproject.toml` has the `[aws]` extras group with `boto3`
    (verify the version range).
  - `AGENTS.md` §4 is updated with a real AWS Batch invocation example.

The production wiring is the configuration plumbing; the mocked boto3
client is the observable side-effect we assert against (the same
pattern as `test_slurm_production_wiring.py` mocks submitit).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import AWSBatchExecutor


# ---------------------------------------------------------------------------
# Lazy import: boto3 is heavy and not installed in the minimal local
# environment. The executor must NOT import boto3 at import time; it
# must lazy-import when the executor is instantiated.
# ---------------------------------------------------------------------------
def test_aws_batch_executor_does_not_import_boto3_at_module_load() -> None:
    """Importing the `osimflow.executors` module must not pull in boto3.
    The local-executor / slurm-executor users should not pay the cost."""
    # The module is already loaded by the conftest / earlier tests; check
    # that boto3 is NOT in osimflow.executors' module namespace.
    import osimflow.executors as exec_mod

    assert not hasattr(exec_mod, "boto3"), (
        "boto3 was imported at module load — must be lazy-imported in __init__"
    )


# ---------------------------------------------------------------------------
# SubmitJob payload: verify the executor builds the right containerOverrides
# and environment-variables list. This is the core acceptance criterion.
# ---------------------------------------------------------------------------
def test_aws_batch_submit_builds_container_overrides() -> None:
    """`submit()` must call `batch.submit_job` with containerOverrides that
    carry vcpus / memory / command / environment."""
    fake_client = MagicMock()
    fake_client.submit_job.return_value = {"jobId": "abc-123"}

    with patch("boto3.client", return_value=fake_client) as boto3_client:
        ex = AWSBatchExecutor(job_queue="my-queue", job_definition="my-job-def")
        handle = ex.submit(
            lambda: "ok",
            name="sample-0",
            cpus=4,
            memory_mb=2048,
            time_min=30,
            container="openstudio_cli_image:3.4.0",
            openstudio_version="3.4.0",
        )

    # boto3.client must be called with 'batch' and a `region_name`
    # that is either absent or None — the IAM role / AWS_REGION env
    # var / ~/.aws/config decides the region. We must NOT pin a region
    # in the source code.
    boto3_client.assert_called_once()
    call_args, call_kwargs = boto3_client.call_args
    assert call_args == ("batch",), f"expected ('batch',), got {call_args}"
    # Either no region kwarg, or region_name=None.
    assert call_kwargs.get("region_name", None) is None, (
        f"region_name must not be pinned, got {call_kwargs.get('region_name')!r}"
    )

    # Inspect the submit_job call.
    fake_client.submit_job.assert_called_once()
    kwargs = fake_client.submit_job.call_args.kwargs
    assert kwargs["jobQueue"] == "my-queue"
    assert kwargs["jobDefinition"] == "my-job-def"
    assert kwargs["jobName"] == "sample-0"
    overrides = kwargs["containerOverrides"]
    assert overrides["vcpus"] == 4
    # 2048 MB -> 2048 MiB (Batch API uses MiB)
    assert overrides["memory"] == 2048
    # The task must be killed after time_min minutes.
    assert kwargs["timeout"] == {"attemptDurationSeconds": 30 * 60}
    # Environment must carry the OS version and the container tag.
    env = overrides["environment"]
    env_dict = {e["name"]: e["value"] for e in env}
    assert env_dict["OSIMFLOW_OS_VERSION"] == "3.4.0"
    assert env_dict["OSIMFLOW_CONTAINER"] == "openstudio_cli_image:3.4.0"
    # Handle exposes the Batch jobId.
    assert handle.job_id == "abc-123"
    ex.shutdown()


def test_aws_batch_submit_uses_minimum_resource_defaults() -> None:
    """When the caller does not pass cpus/memory_mb/time_min, the executor
    must still build a valid submit_job payload from its own defaults."""
    fake_client = MagicMock()
    fake_client.submit_job.return_value = {"jobId": "job-defaults"}

    with patch("boto3.client", return_value=fake_client):
        ex = AWSBatchExecutor()
        ex.submit(lambda: None, name="t")

    overrides = fake_client.submit_job.call_args.kwargs["containerOverrides"]
    # cpus=1, memory_mb=1024, time_min=60 are the BaseExecutor.submit defaults.
    assert overrides["vcpus"] == 1
    assert overrides["memory"] == 1024
    ex.shutdown()


def test_aws_batch_submit_omits_env_when_not_provided() -> None:
    """If the caller does not pass `container` or `openstudio_version`,
    the environment list should still be present (so the task gets a
    sane baseline) but `OSIMFLOW_CONTAINER` / `OSIMFLOW_OS_VERSION` may
    be absent. We assert the list itself is well-formed."""
    fake_client = MagicMock()
    fake_client.submit_job.return_value = {"jobId": "job-no-env"}

    with patch("boto3.client", return_value=fake_client):
        ex = AWSBatchExecutor()
        ex.submit(lambda: None, name="t")

    overrides = fake_client.submit_job.call_args.kwargs["containerOverrides"]
    env = overrides["environment"]
    assert isinstance(env, list)
    for entry in env:
        assert set(entry.keys()) == {"name", "value"}
    ex.shutdown()


# ---------------------------------------------------------------------------
# Polling and failure handling: describe_jobs must be polled until the
# task is SUCCEEDED, and FAILED must re-raise with the statusReason so
# the Campaign's `except Exception` path logs the failure correctly.
# ---------------------------------------------------------------------------
def test_aws_batch_result_returns_none_on_success() -> None:
    """`Handle.result()` must block until the Batch task SUCCEEDS and
    then return. The function's return value is not available locally
    (it ran in the Batch container), so `None` is the correct sentinel
    for "the task finished cleanly". The Campaign uses this branch to
    know the sample completed; KPI extraction reads the container's
    on-disk artifacts (eplusout.sql, etc.) in a downstream step.
    """
    fake_client = MagicMock()
    fake_client.submit_job.return_value = {"jobId": "job-success"}
    # describe_jobs returns SUCCEEDED on the first poll.
    fake_client.describe_jobs.return_value = {
        "jobs": [
            {
                "jobId": "job-success",
                "status": "SUCCEEDED",
                "statusReason": "All tasks completed",
                "container": {"exitCode": 0, "taskArn": "arn:aws:ecs:..."},
            }
        ]
    }

    with patch("boto3.client", return_value=fake_client):
        ex = AWSBatchExecutor(poll_interval_s=0.01, max_poll_interval_s=0.02)
        handle = ex.submit(lambda: 42, name="ok-job")
        result = handle.result(timeout=5)

    assert result is None
    assert handle.done() is True
    ex.shutdown()


def test_aws_batch_failed_raises_with_status_reason() -> None:
    """When the Batch task FAILED, Handle.result() must re-raise an
    exception whose message includes the statusReason. The Campaign's
    `except Exception` path needs a string it can log."""
    fake_client = MagicMock()
    fake_client.submit_job.return_value = {"jobId": "job-fail"}
    fake_client.describe_jobs.return_value = {
        "jobs": [
            {
                "jobId": "job-fail",
                "status": "FAILED",
                "statusReason": "Essential container exited with code 1",
                "container": {"exitCode": 1, "taskArn": "arn:aws:ecs:..."},
            }
        ]
    }

    with patch("boto3.client", return_value=fake_client):
        ex = AWSBatchExecutor(poll_interval_s=0.01, max_poll_interval_s=0.02)
        handle = ex.submit(lambda: None, name="fail-job")
        with pytest.raises(RuntimeError, match="Essential container exited"):
            handle.result(timeout=5)
    ex.shutdown()


# ---------------------------------------------------------------------------
# Polling cadence: the executor must back off exponentially starting
# from `poll_interval_s` and cap at `max_poll_interval_s`.
# ---------------------------------------------------------------------------
def test_aws_batch_polling_uses_exponential_backoff() -> None:
    """The describe_jobs call cadence should grow exponentially until it
    caps at `max_poll_interval_s`. We assert that the sleep call sequence
    is monotonically non-decreasing and eventually bounded."""
    fake_client = MagicMock()
    fake_client.submit_job.return_value = {"jobId": "job-poll"}

    # Three RUNNING responses, then SUCCEEDED on the fourth call.
    describe_calls = [
        {"jobs": [{"jobId": "job-poll", "status": "RUNNING", "statusReason": "..."}]},
        {"jobs": [{"jobId": "job-poll", "status": "RUNNING", "statusReason": "..."}]},
        {"jobs": [{"jobId": "job-poll", "status": "RUNNING", "statusReason": "..."}]},
        {"jobs": [{"jobId": "job-poll", "status": "SUCCEEDED", "statusReason": "OK"}]},
    ]
    fake_client.describe_jobs.side_effect = describe_calls

    sleep_durations: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_durations.append(seconds)

    with (
        patch("boto3.client", return_value=fake_client),
        patch("time.sleep", side_effect=fake_sleep),
    ):
        ex = AWSBatchExecutor(poll_interval_s=1.0, max_poll_interval_s=4.0)
        handle = ex.submit(lambda: None, name="poll-job")
        handle.result(timeout=5)

    # The first sleep is poll_interval_s; subsequent sleeps double
    # (1, 2, 4) and the cap is 4.0 — the last sleep is 4.0, not 8.0.
    assert sleep_durations[0] == 1.0
    # Subsequent sleeps must be non-decreasing (and capped). Note:
    # the final SUCCEEDED call does not sleep, so the trailing
    # `zip` slice may be shorter — `strict=False` is intentional.
    assert all(
        b >= a
        for a, b in zip(sleep_durations, sleep_durations[1:])  # noqa: B905
    ), f"sleep durations not non-decreasing: {sleep_durations}"
    assert sleep_durations[-1] <= 4.0, f"final sleep {sleep_durations[-1]} exceeds cap 4.0"
    ex.shutdown()


# ---------------------------------------------------------------------------
# Security: the executor must not accept long-lived AWS access keys.
# PRD §6 mandates IAM roles on the Batch compute environment.
# ---------------------------------------------------------------------------
def test_aws_batch_executor_rejects_aws_access_key_pair() -> None:
    """Accepting `aws_access_key_id` / `aws_secret_access_key` would
    violate the security policy. The constructor signature must not
    expose them."""
    import inspect

    sig = inspect.signature(AWSBatchExecutor.__init__)
    params = list(sig.parameters)
    assert "aws_access_key_id" not in params
    assert "aws_secret_access_key" not in params


# ---------------------------------------------------------------------------
# boto3 client creation must NOT pin a region — the IAM role's region
# (or AWS_REGION env var) decides. Pinning a region in code would
# hard-code the deployment, which is a portability trap.
# ---------------------------------------------------------------------------
def test_aws_batch_does_not_pin_aws_region_in_code() -> None:
    fake_client = MagicMock()
    fake_client.submit_job.return_value = {"jobId": "x"}

    with patch("boto3.client", return_value=fake_client) as boto3_client:
        # Clear AWS region env vars so we know nothing leaks from the
        # test environment into the executor's boto3.client call.
        env_before = os.environ.pop("AWS_DEFAULT_REGION", None)
        env_before2 = os.environ.pop("AWS_REGION", None)
        try:
            ex = AWSBatchExecutor()
            ex.submit(lambda: None, name="t")
        finally:
            if env_before is not None:
                os.environ["AWS_DEFAULT_REGION"] = env_before
            if env_before2 is not None:
                os.environ["AWS_REGION"] = env_before2

    args, kwargs = boto3_client.call_args
    # We allow `region_name=None` (the default) but not an explicit region.
    if "region_name" in kwargs:
        assert kwargs["region_name"] is None
    # Positional region arg is not acceptable.
    if len(args) >= 2:
        assert args[1] is None
    ex.shutdown()


# ---------------------------------------------------------------------------
# Pyproject.toml: verify the [aws] extras group has a boto3 entry with
# a reasonable version range (not so low that it misses the modern API,
# not pinned to a specific version).
# ---------------------------------------------------------------------------
def test_pyproject_has_aws_extras_with_boto3() -> None:
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    optional = data["project"]["optional-dependencies"]
    assert "aws" in optional, "pyproject.toml is missing the [aws] extras group"
    aws_deps = optional["aws"]
    boto3_spec = next((d for d in aws_deps if d.lower().startswith("boto3")), None)
    assert boto3_spec is not None, "boto3 is not declared in the [aws] extras group"
    # The boto3 API for batch has been stable since 1.20; 1.28 is a
    # reasonable floor (covers ~2023+). Allow >= without an upper bound.
    assert ">=" in boto3_spec, f"boto3 spec {boto3_spec!r} should use a >= floor"
