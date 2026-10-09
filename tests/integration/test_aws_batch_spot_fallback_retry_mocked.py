"""Moto-backed evidence for Spot retry and Spot->on-demand fallback (#1848, #1849).

Live Spot reclamation cannot be forced on demand, so these tests drive the real
``AWSBatchExecutor`` + ``_AWSBatchHandle`` state machine against moto's Batch
wire format, with ``describe_jobs`` reporting a Spot interruption for jobs on
the Spot queue and success for jobs on the on-demand queue. They assert which
queue/job definition every ``submit_job`` call actually used.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from osimflow.executors import AWSBatchExecutor
from osimflow.testing.patch_targets import _AWSBatchHandle
from tests.integration.test_aws_batch_mocked import (
    _JOB_DEF_NAME,
    _QUEUE_NAME,
    _REGION,
    _setup_batch_infra,
)

_OD_QUEUE = "osimflow-test-ondemand-queue"
_OD_JOB_DEF = "osimflow-test-ondemand-job-def"
_SPOT_REASON = "Host EC2 (instance i-0abc) terminated. Spot interruption"


def _add_on_demand_route(batch: Any) -> None:
    ce_arn = batch.describe_compute_environments()["computeEnvironments"][0][
        "computeEnvironmentArn"
    ]
    batch.create_job_queue(
        jobQueueName=_OD_QUEUE,
        state="ENABLED",
        priority=1,
        computeEnvironmentOrder=[{"order": 1, "computeEnvironment": ce_arn}],
    )
    batch.register_job_definition(
        jobDefinitionName=_OD_JOB_DEF,
        type="container",
        containerProperties={
            "image": "public.ecr.aws/docker/library/alpine:latest",
            "vcpus": 1,
            "memory": 512,
            "command": ["echo", "hello"],
        },
    )


def _install_outcomes(
    batch: Any, outcome_for: Callable[[str, int], tuple[str, str]]
) -> list[dict[str, str]]:
    """Record every submit_job and make describe_jobs report scripted outcomes.

    ``outcome_for(queue, queue_attempt)`` returns ``(status, reason)`` where
    ``queue_attempt`` counts prior submissions to that queue.
    """
    submissions: list[dict[str, str]] = []
    job_route: dict[str, tuple[str, int]] = {}
    real_submit = batch.submit_job
    real_describe = batch.describe_jobs

    def _submit(**kwargs: Any) -> Any:
        resp = real_submit(**kwargs)
        queue = kwargs["jobQueue"]
        attempt = sum(1 for s in submissions if s["queue"] == queue)
        submissions.append({"queue": queue, "definition": kwargs["jobDefinition"]})
        job_route[resp["jobId"]] = (queue, attempt)
        return resp

    def _describe(**kwargs: Any) -> Any:
        resp = real_describe(**kwargs)
        for job in resp.get("jobs", []):
            queue, attempt = job_route[job["jobId"]]
            status, reason = outcome_for(queue, attempt)
            job["status"] = status
            job["statusReason"] = reason
        return resp

    batch.submit_job = _submit
    batch.describe_jobs = _describe
    return submissions


def _executor(batch: Any, **kwargs: Any) -> AWSBatchExecutor:
    executor = AWSBatchExecutor(
        job_queue=_QUEUE_NAME,
        job_definition=_JOB_DEF_NAME,
        poll_interval_s=0.01,
        max_poll_interval_s=0.02,
        region_name=_REGION,
        **kwargs,
    )
    executor._client = batch  # noqa: SLF001
    return executor


def _submit(executor: AWSBatchExecutor) -> Any:
    return executor.submit(lambda: None, name="spot-test", cpus=1, memory_mb=512)


@mock_aws
def test_spot_interruption_retried_on_same_queue_then_succeeds() -> None:
    batch = boto3.client("batch", region_name=_REGION)
    _setup_batch_infra(batch)
    submissions = _install_outcomes(
        batch,
        lambda queue, attempt: ("FAILED", _SPOT_REASON) if attempt == 0 else ("SUCCEEDED", "done"),
    )
    executor = _executor(batch, max_retries=2)

    with patch("osimflow.executors.base.time.sleep"):
        handle = _submit(executor)
        assert isinstance(handle, _AWSBatchHandle)
        first_job = handle.job_id
        handle.result(timeout=30)

    assert len(submissions) == 2
    assert {s["queue"] for s in submissions} == {_QUEUE_NAME}
    assert handle.job_id != first_job
    executor.shutdown()


@mock_aws
def test_retries_exhausted_without_fallback_raises() -> None:
    batch = boto3.client("batch", region_name=_REGION)
    _setup_batch_infra(batch)
    submissions = _install_outcomes(batch, lambda q, a: ("FAILED", _SPOT_REASON))  # noqa: ARG005
    executor = _executor(batch, max_retries=1)

    with (
        patch("osimflow.executors.base.time.sleep"),
        pytest.raises(RuntimeError, match="Spot retries exhausted"),
    ):
        _submit(executor).result(timeout=30)

    assert len(submissions) == 2
    executor.shutdown()


@mock_aws
def test_spot_failure_falls_back_to_on_demand_queue_and_definition() -> None:
    batch = boto3.client("batch", region_name=_REGION)
    _setup_batch_infra(batch)
    _add_on_demand_route(batch)
    submissions = _install_outcomes(
        batch,
        lambda queue, attempt: (  # noqa: ARG005
            ("SUCCEEDED", "done") if queue.endswith(_OD_QUEUE) else ("FAILED", _SPOT_REASON)
        ),
    )
    executor = _executor(
        batch,
        max_retries=1,
        fallback_to_on_demand=True,
        on_demand_job_queue=_OD_QUEUE,
        on_demand_job_definition=_OD_JOB_DEF,
    )

    with patch("osimflow.executors.base.time.sleep"):
        _submit(executor).result(timeout=30)

    assert [s["queue"].rsplit("/", 1)[-1] for s in submissions] == [
        _QUEUE_NAME,
        _QUEUE_NAME,
        _OD_QUEUE,
    ]
    assert submissions[-1]["definition"].split("/")[-1].split(":")[0] == _OD_JOB_DEF
    assert submissions[0]["definition"].split("/")[-1].split(":")[0] == _JOB_DEF_NAME
    executor.shutdown()


@mock_aws
def test_on_demand_failure_after_fallback_is_reported() -> None:
    batch = boto3.client("batch", region_name=_REGION)
    _setup_batch_infra(batch)
    _add_on_demand_route(batch)
    _install_outcomes(batch, lambda q, a: ("FAILED", _SPOT_REASON))  # noqa: ARG005
    executor = _executor(
        batch,
        max_retries=0,
        fallback_to_on_demand=True,
        on_demand_job_queue=_OD_QUEUE,
        on_demand_job_definition=_OD_JOB_DEF,
    )

    with pytest.raises(RuntimeError, match="FAILED"):
        _submit(executor).result(timeout=30)
    executor.shutdown()
