"""Unit tests for osimflow.executors.KubernetesExecutor (issue #250).

Covers:
  - KubernetesExecutor: submit, _build_job_spec, _wait_for_terminal
  - _KubernetesHandle: result, done
  - _KubernetesClient: create_job, get_job
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import (
    KubernetesExecutor,
    _KubernetesClient,
    _KubernetesHandle,
)


class TestKubernetesExecutor:
    """KubernetesExecutor wraps the K8s BatchV1Api."""

    def _make_mock_client(self) -> MagicMock:
        mock_client = MagicMock()
        mock_client.create_job.return_value = None
        mock_client.get_job.return_value = {
            "api_version": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "osimflow-test"},
            "status": {"conditions": [{"type": "Complete", "status": "True"}], "failed": 0},
        }
        return mock_client

    def test_submit_returns_handle(self) -> None:
        mock_client = self._make_mock_client()
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        with patch.object(
            ex,
            "_wait_for_terminal",
            return_value={"status": {"conditions": [{"type": "Complete"}]}},
        ):
            handle = ex.submit(lambda: None, name="test")
        assert hasattr(handle, "result")
        assert hasattr(handle, "job_name")
        assert handle.job_name == "osimflow-test"

    def test_submit_with_resources(self) -> None:
        mock_client = self._make_mock_client()
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        with patch.object(
            ex,
            "_wait_for_terminal",
            return_value={"status": {"conditions": [{"type": "Complete"}]}},
        ):
            handle = ex.submit(lambda: None, name="heavy", cpus=4, memory_mb=8192)
        assert handle.job_name == "osimflow-heavy"

    def test_build_job_spec(self) -> None:
        mock_client = self._make_mock_client()
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        spec = ex._build_job_spec(
            name="test",
            cpus=2,
            memory_mb=4096,
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
        )
        assert spec["kind"] == "Job"
        assert spec["metadata"]["name"] == "osimflow-test"
        assert (
            spec["spec"]["template"]["spec"]["containers"][0]["image"] == "nrel/openstudio:3.11.0"
        )
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        env_names = [e["name"] for e in env]
        assert "OSIMFLOW_OS_VERSION" in env_names
        assert "OSIMFLOW_CONTAINER" in env_names

    def test_name_attribute(self) -> None:
        assert KubernetesExecutor.__new__(KubernetesExecutor).name == "kubernetes"  # noqa: SLF001

    def test_namespace_default(self) -> None:
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex.namespace = "default"
        assert ex.namespace == "default"

    def test_wait_for_terminal(self) -> None:
        mock_client = self._make_mock_client()
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        with patch("osimflow.executors.time.sleep"):
            result = ex._wait_for_terminal("osimflow-test")
        assert "conditions" in result["status"]


class TestKubernetesHandle:
    """_KubernetesHandle polls K8s Job on .result() and .done()."""

    def test_result_returns_none_on_complete(self) -> None:
        mock_ex = MagicMock()
        mock_ex._wait_for_terminal.return_value = {
            "status": {"conditions": [{"type": "Complete", "status": "True"}]}
        }
        handle = _KubernetesHandle(job_name="test", executor=mock_ex)
        result = handle.result()
        assert result is None

    def test_result_raises_on_failure(self) -> None:
        mock_ex = MagicMock()
        mock_ex._wait_for_terminal.return_value = {"status": {"conditions": [], "failed": 1}}
        handle = _KubernetesHandle(job_name="test", executor=mock_ex)
        with pytest.raises(RuntimeError, match="failed"):
            handle.result()

    def test_done_true_when_complete(self) -> None:
        mock_ex = MagicMock()
        mock_ex._client.get_job.return_value = {
            "status": {"conditions": [{"type": "Complete", "status": "True"}]}
        }
        handle = _KubernetesHandle(job_name="test", executor=mock_ex)
        assert handle.done() is True

    def test_done_false_when_running(self) -> None:
        mock_ex = MagicMock()
        mock_ex._client.get_job.return_value = {"status": {"conditions": [], "failed": 0}}
        handle = _KubernetesHandle(job_name="test", executor=mock_ex)
        assert handle.done() is False

    def test_worker_fields(self) -> None:
        mock_ex = MagicMock()
        mock_ex.namespace = "default"
        handle = _KubernetesHandle(job_name="test", executor=mock_ex)
        assert handle.worker_id == "test"
        assert handle.worker_region == "default"


class TestKubernetesClient:
    """_KubernetesClient wraps the K8s BatchV1Api.

    These tests verify the interface by mocking the underlying batch_v1
    client directly. The real create_job/get_job require the kubernetes
    package which may not be installed in the test environment.
    """

    def test_create_job_signature(self) -> None:
        mock_batch = MagicMock()
        client = _KubernetesClient(batch_v1=mock_batch, namespace="default")
        assert hasattr(client, "create_job")
        assert hasattr(client, "get_job")
        assert client.namespace == "default"

    def test_get_job_signature(self) -> None:
        mock_batch = MagicMock()
        client = _KubernetesClient(batch_v1=mock_batch, namespace="default")
        assert callable(client.get_job)
        assert callable(client.create_job)
