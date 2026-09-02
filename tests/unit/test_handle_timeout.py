"""Polling handles honour ``result(timeout=...)`` (issue #1465).

``Handle.result(timeout)`` is the contract every campaign step relies
on.  These tests pin the deadline semantics for the five polling
handles that previously accepted (and silently ignored) the argument:
Azure Batch, Docker Swarm, Google Batch, Kubernetes, and PBS.

Each test drives the real ``_wait_for_terminal`` polling loop against
a substrate client whose status never becomes terminal, then asserts
that ``result(timeout=0.1)`` raises ``TimeoutError`` promptly instead
of blocking indefinitely.  A wall-clock guard (< 2 s) proves the call
actually returns.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow.executors.azure_batch_executor import AzureBatchExecutor, _AzureBatchHandle
from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor, _DockerSwarmHandle
from osimflow.executors.google_batch_executor import GoogleBatchExecutor, _GoogleBatchHandle
from osimflow.executors.kubernetes_executor import KubernetesExecutor, _KubernetesHandle
from osimflow.executors.pbs_executor import PBSExecutor, _PBSHandle

_TIMEOUT_S = 0.1
_MAX_WALL_S = 2.0


def _assert_times_out(handle_result: Callable[[], Any]) -> None:
    """Assert ``handle_result()`` raises TimeoutError within the wall budget."""
    start = time.monotonic()
    with pytest.raises(TimeoutError, match="Timed out"):
        handle_result()
    elapsed = time.monotonic() - start
    assert elapsed < _MAX_WALL_S, (
        f"result() took {elapsed:.2f}s to time out (budget {_MAX_WALL_S}s)"
    )


class TestHandleResultTimeout:
    """Issue #1465: polling handles enforce the ``timeout`` deadline."""

    def test_azure_batch_handle_result_timeout_raises(self) -> None:
        """_AzureBatchHandle: never-terminal job -> TimeoutError, promptly."""
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex.poll_interval_s = 0.05
        ex.max_poll_interval_s = 0.1
        ex.max_retries = 0
        ex.location = "eastus"
        job = MagicMock()
        job.properties.execution_info.end_time = None  # never terminal
        ex._get_job = MagicMock(return_value=job)

        handle = _AzureBatchHandle(job_id="job-1", executor=ex, submit_params={})
        _assert_times_out(lambda: handle.result(timeout=_TIMEOUT_S))

    def test_docker_swarm_handle_result_timeout_raises(self) -> None:
        """_DockerSwarmHandle: never-terminal service task -> TimeoutError."""
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
        ex.poll_interval_s = 0.05
        ex.max_poll_interval_s = 0.1
        service_status = {"tasks": [{"status": {"State": "running"}}]}
        ex._get_service_status = MagicMock(return_value=service_status)

        handle = _DockerSwarmHandle(service_name="svc-1", executor=ex, submit_params={})
        _assert_times_out(lambda: handle.result(timeout=_TIMEOUT_S))

    def test_google_batch_handle_result_timeout_raises(self) -> None:
        """_GoogleBatchHandle: never-terminal job -> TimeoutError, promptly."""
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex.poll_interval_s = 0.05
        ex.max_poll_interval_s = 0.1
        ex.max_retries = 0
        ex.region = "us-central1"
        # state.name must not contain SUCCEEDED/FAILED (SimpleNamespace:
        # MagicMock's ``.name`` is special-cased by the mock constructor).
        job = SimpleNamespace(status=SimpleNamespace(state=SimpleNamespace(name="RUNNING")))
        ex._get_job = MagicMock(return_value=job)

        handle = _GoogleBatchHandle(job_name="job-1", executor=ex, submit_params={})
        _assert_times_out(lambda: handle.result(timeout=_TIMEOUT_S))

    def test_kubernetes_handle_result_timeout_raises(self) -> None:
        """_KubernetesHandle: pod stuck in Running -> TimeoutError, promptly."""
        ex = KubernetesExecutor.__new__(KubernetesExecutor)
        ex.poll_interval_s = 0.05
        ex.max_poll_interval_s = 0.1
        ex._get_pod_status = MagicMock(return_value={"status": {"phase": "Running"}})

        handle = _KubernetesHandle(job_name="job-1", executor=ex, submit_params={})
        _assert_times_out(lambda: handle.result(timeout=_TIMEOUT_S))

    def test_pbs_handle_result_timeout_raises(self) -> None:
        """_PBSHandle: qstat stuck in R (running) -> TimeoutError, promptly."""
        ex = PBSExecutor.__new__(PBSExecutor)
        ex.poll_interval_s = 0.05
        ex.max_poll_interval_s = 0.1
        ex._query_job_state = MagicMock(return_value="R")

        handle = _PBSHandle(job_id="123.server", executor=ex)
        _assert_times_out(lambda: handle.result(timeout=_TIMEOUT_S))
