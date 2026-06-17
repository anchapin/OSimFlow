"""Unit tests for osimflow.executors.KubernetesExecutor (issue #254).

Covers:
  - KubernetesExecutor: submit, _submit_job, _wait_for_terminal, _get_pod_status
  - _KubernetesHandle: result, done
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import KubernetesExecutor
from osimflow.executors.kubernetes_executor import _KubernetesHandle

try:
    from kubernetes import client  # noqa: F401

    _HAS_KUBERNETES = True
except ImportError:
    _HAS_KUBERNETES = False


class TestKubernetesExecutor:
    """KubernetesExecutor wraps the K8s BatchV1Api."""

    pytestmark = pytest.mark.skipif(not _HAS_KUBERNETES, reason="kubernetes not installed")

    def _make_mock_client(self) -> MagicMock:
        mock_client = MagicMock()
        mock_client.create_namespaced_job.return_value = None
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
            return_value={"status": {"phase": "Succeeded"}},
        ):
            handle = ex.submit(lambda: None, name="test")
        assert hasattr(handle, "result")
        assert hasattr(handle, "job_id")
        assert handle.job_id == "osimflow-test"

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
            return_value={"status": {"phase": "Succeeded"}},
        ):
            handle = ex.submit(lambda: None, name="heavy", cpus=4, memory_mb=8192)
        assert handle.job_id == "osimflow-heavy"

    def test_submit_with_container(self) -> None:
        mock_client = self._make_mock_client()
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        with patch.object(
            ex,
            "_wait_for_terminal",
            return_value={"status": {"phase": "Succeeded"}},
        ):
            handle = ex.submit(
                lambda: None,
                name="test",
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
            )
        assert handle.job_id == "osimflow-test"
        mock_client.create_namespaced_job.assert_called_once()
        call_kwargs = mock_client.create_namespaced_job.call_args
        job = call_kwargs.kwargs["body"]
        container = job.spec.template.spec.containers[0]
        assert container.image == "nrel/openstudio:3.11.0"
        env = {e.name: e.value for e in container.env}
        assert env["OSIMFLOW_OS_VERSION"] == "3.11.0"
        assert env["OSIMFLOW_CONTAINER"] == "nrel/openstudio:3.11.0"

    def test_name_attribute(self) -> None:
        assert KubernetesExecutor.__new__(KubernetesExecutor).name == "kubernetes"  # noqa: SLF001

    def test_namespace_default(self) -> None:
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex.namespace = "default"
        assert ex.namespace == "default"

    def test_wait_for_terminal_succeeds(self) -> None:
        mock_client = self._make_mock_client()
        mock_pod = MagicMock()
        mock_pod.to_dict.return_value = {"status": {"phase": "Succeeded"}}
        mock_client.list_namespaced_pod.return_value.items = [mock_pod]
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 0.1
        ex.max_poll_interval_s = 1.0
        with patch("osimflow.executors.time.sleep"):
            result = ex._wait_for_terminal("osimflow-test")
        assert result["status"]["phase"] == "Succeeded"

    def test_wait_for_terminal_fails(self) -> None:
        mock_client = self._make_mock_client()
        mock_pod = MagicMock()
        mock_pod.to_dict.return_value = {"status": {"phase": "Failed"}}
        mock_client.list_namespaced_pod.return_value.items = [mock_pod]
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        ex.poll_interval_s = 0.1
        ex.max_poll_interval_s = 1.0
        with patch("osimflow.executors.time.sleep"):
            result = ex._wait_for_terminal("osimflow-test")
        assert result["status"]["phase"] == "Failed"

    def test_get_pod_status_no_pods(self) -> None:
        mock_client = self._make_mock_client()
        mock_client.list_namespaced_pod.return_value.items = []
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        result = ex._get_pod_status("osimflow-test")
        assert result == {"status": {"phase": "Pending"}}

    def test_get_pod_status_with_pods(self) -> None:
        mock_client = self._make_mock_client()
        mock_pod = MagicMock()
        mock_pod.to_dict.return_value = {"status": {"phase": "Running"}}
        mock_client.list_namespaced_pod.return_value.items = [mock_pod]
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex._client = mock_client
        ex.namespace = "default"
        result = ex._get_pod_status("osimflow-test")
        assert result["status"]["phase"] == "Running"

    def test_build_job_name(self) -> None:
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex.namespace = "default"
        assert ex._build_job_name("test") == "osimflow-test"
        assert ex._build_job_name("sample_0") == "osimflow-sample-0"
        assert ex._build_job_name("Heavy_Simulation") == "osimflow-heavy-simulation"

    def test_build_environment(self) -> None:
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex.namespace = "default"
        env = ex._build_environment(
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
        )
        env_dict = {e["name"]: e["value"] for e in env}
        assert env_dict["OSIMFLOW_OS_VERSION"] == "3.11.0"
        assert env_dict["OSIMFLOW_CONTAINER"] == "nrel/openstudio:3.11.0"

    def test_build_environment_defaults(self) -> None:
        ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
        ex.namespace = "default"
        env = ex._build_environment(container=None, openstudio_version=None)
        env_dict = {e["name"]: e["value"] for e in env}
        assert env_dict["OSIMFLOW_CONTAINER"] == "nrel/openstudio:latest"


class TestKubernetesHandle:
    """_KubernetesHandle polls K8s Job on .result() and .done()."""

    def test_result_returns_none_on_succeeded(self) -> None:
        mock_ex = MagicMock()
        mock_ex._wait_for_terminal.return_value = {"status": {"phase": "Succeeded"}}
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        result = handle.result()
        assert result is None

    def test_result_returns_result_hint_on_succeeded(self) -> None:
        mock_ex = MagicMock()
        mock_ex._wait_for_terminal.return_value = {"status": {"phase": "Succeeded"}}
        hint = Path("/tmp/osimflow/plots")
        handle = _KubernetesHandle(
            job_name="test",
            executor=mock_ex,
            submit_params={},
            result_hint=hint,
        )
        assert handle.result() == hint

    def test_result_raises_on_failed(self) -> None:
        mock_ex = MagicMock()
        mock_ex._wait_for_terminal.return_value = {
            "status": {"phase": "Failed"},
        }
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        with pytest.raises(RuntimeError, match="Failed"):
            handle.result()

    def test_done_true_when_succeeded(self) -> None:
        mock_ex = MagicMock()
        mock_ex._get_pod_status.return_value = {"status": {"phase": "Succeeded"}}
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        assert handle.done() is True

    def test_done_false_when_running(self) -> None:
        mock_ex = MagicMock()
        mock_ex._get_pod_status.return_value = {"status": {"phase": "Running"}}
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        assert handle.done() is False

    def test_worker_fields(self) -> None:
        mock_ex = MagicMock()
        mock_ex.namespace = "default"
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        assert handle.worker_id == "test"
        assert handle.worker_region is None

    def test_extract_failure_reason_terminated(self) -> None:
        mock_ex = MagicMock()
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        pod_status = {
            "status": {
                "containerStatuses": [
                    {
                        "state": {
                            "terminated": {
                                "exitCode": 1,
                                "reason": "NonZeroExit",
                            }
                        }
                    }
                ]
            }
        }
        reason = handle._extract_failure_reason(pod_status)
        assert "exit code 1" in reason

    def test_extract_failure_reason_waiting(self) -> None:
        mock_ex = MagicMock()
        handle = _KubernetesHandle(job_name="test", executor=mock_ex, submit_params={})
        pod_status = {
            "status": {
                "containerStatuses": [
                    {
                        "state": {
                            "waiting": {
                                "reason": "ImagePullBackOff",
                                "message": "rpc error",
                            }
                        }
                    }
                ]
            }
        }
        reason = handle._extract_failure_reason(pod_status)
        assert "ImagePullBackOff" in reason
