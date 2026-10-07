"""Job-definition image authority checks (issue #1810)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow.executors import AWSBatchExecutor

DIGEST = "sha256:" + "a" * 64
ECR = "123456789012.dkr.ecr.us-east-1.amazonaws.com/osimflow-worker"


def _executor(image: str, **kw: Any) -> tuple[AWSBatchExecutor, MagicMock]:
    ex = AWSBatchExecutor(**kw)
    client = MagicMock()
    client.describe_job_definitions.return_value = {
        "jobDefinitions": [{"revision": 4, "containerProperties": {"image": image}}]
    }
    client.submit_job.return_value = {"jobId": "j-1"}
    ex._client = client  # noqa: SLF001
    return ex, client


def _submit(ex: AWSBatchExecutor, **kw: Any) -> str:
    return ex._submit_job(  # noqa: SLF001
        name="n", cpus=1, memory_mb=1, time_min=1, environment=[], **kw
    )


def test_matching_tag_submits() -> None:
    ex, client = _executor(f"{ECR}:3.11.0")
    assert _submit(ex, expected_image=f"{ECR}:3.11.0") == "j-1"
    client.submit_job.assert_called_once()


def test_mismatched_tag_fails_before_submit() -> None:
    ex, client = _executor(f"{ECR}:3.10.0")
    with pytest.raises(RuntimeError, match="OSIMFLOW_CONTAINER does not change"):
        _submit(ex, expected_image=f"{ECR}:3.11.0")
    client.submit_job.assert_not_called()


def test_digest_matches_job_definition_digest_ref() -> None:
    ex, _ = _executor(f"{ECR}@{DIGEST}")
    assert _submit(ex, expected_image=DIGEST) == "j-1"
    assert _submit(ex, expected_image=f"other/repo@{DIGEST}") == "j-1"


def test_digest_mismatch_fails() -> None:
    ex, client = _executor(f"{ECR}:3.11.0")
    with pytest.raises(RuntimeError, match="does not match"):
        _submit(ex, expected_image=DIGEST)
    client.submit_job.assert_not_called()


def test_missing_job_definition_fails() -> None:
    ex, client = _executor("x")
    client.describe_job_definitions.return_value = {"jobDefinitions": []}
    with pytest.raises(RuntimeError, match="not found"):
        _submit(ex, expected_image="x:1")


def test_no_pin_means_no_describe_call() -> None:
    ex, client = _executor("anything")
    _submit(ex)
    client.describe_job_definitions.assert_not_called()


def test_do_submit_pins_only_when_requested() -> None:
    ex = AWSBatchExecutor()
    assert ex._container_digest is None  # noqa: SLF001
    pinned = AWSBatchExecutor(ecr_repository=ECR)
    assert pinned._resolve_container_image("3.11.0") == f"{ECR}:3.11.0"  # noqa: SLF001
