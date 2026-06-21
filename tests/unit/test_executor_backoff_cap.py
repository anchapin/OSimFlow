"""Regression tests for issue #647 — backoff cap applied BEFORE sleep.

In 5 executors (kubernetes, docker_swarm, pbs, google_batch, azure_batch),
the exponential backoff delay was calculated and capped AFTER sleeping,
meaning a single sleep could theoretically exceed max_poll_interval_s.

The fix applies the cap BEFORE time.sleep(), ensuring no single sleep
duration ever exceeds max_poll_interval_s.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors.azure_batch_executor import AzureBatchExecutor
from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor
from osimflow.executors.google_batch_executor import GoogleBatchExecutor
from osimflow.executors.kubernetes_executor import KubernetesExecutor
from osimflow.executors.pbs_executor import PBSExecutor


class TestBackoffCapAppliedBeforeSleep:
    """Verify backoff cap is applied before sleep in 5 executors.

    Regression test for issue #647.
    """

    def test_azure_batch_wait_for_terminal_never_exceeds_max_interval(
        self,
    ) -> None:
        """Azure Batch _wait_for_terminal must never sleep longer than max_poll_interval_s."""
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex._azure_identity = MagicMock()
        ex._azure_batch = MagicMock()
        ex._client = MagicMock()
        ex.account_name = "testaccount"
        ex.account_url = "https://testaccount.eastus.batch.azure.com"
        ex.pool_id = "test-pool"
        ex.location = "eastus"
        ex.poll_interval_s = 1.0
        ex.max_poll_interval_s = 8.0
        ex.use_spot = False
        ex.fallback_to_on_demand = False
        ex.max_retries = 3

        mock_job_running = MagicMock()
        mock_job_running.properties.execution_info.end_time = None
        mock_job_done = MagicMock()
        mock_job_done.properties.execution_info.end_time = "2024-01-01T00:01:00Z"
        ex._client.job.get.side_effect = [
            mock_job_running,
            mock_job_running,
            mock_job_running,
            mock_job_running,
            mock_job_done,
        ]

        sleep_durations: list[float] = []

        def capture_sleep(duration: float) -> None:
            sleep_durations.append(duration)

        with patch("osimflow.executors.azure_batch_executor.time.sleep", side_effect=capture_sleep):
            job = ex._wait_for_terminal("test-job")

        assert job.properties.execution_info.end_time is not None

        for sleep_duration in sleep_durations:
            assert sleep_duration <= ex.max_poll_interval_s, (
                f"sleep({sleep_duration:.2f}s) exceeded max_poll_interval_s="
                f"{ex.max_poll_interval_s}s — backoff cap was applied AFTER sleep"
            )

        # With FIXED pattern (cap BEFORE sleep):
        # delay=1.0 -> double+cap -> 2.0 -> sleep(2.0)
        # delay=2.0 -> double+cap -> 4.0 -> sleep(4.0)
        # delay=4.0 -> double+cap -> 8.0 -> sleep(8.0)
        # delay=8.0 -> double+cap -> 8.0 (capped) -> sleep(8.0)
        assert sleep_durations == [2.0, 4.0, 8.0, 8.0]

    def test_google_batch_wait_for_terminal_never_exceeds_max_interval(
        self,
    ) -> None:
        """Google Batch _wait_for_terminal must never sleep longer than max_poll_interval_s."""
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._batch_v1 = MagicMock()
        ex._client = MagicMock()
        ex.project_id = "test-project"
        ex.region = "us-central1"
        ex.batch_service_account = None
        ex.poll_interval_s = 1.0
        ex.max_poll_interval_s = 8.0
        ex.use_spot = False
        ex.fallback_to_on_demand = False
        ex.max_retries = 3
        ex._submit_job = MagicMock(return_value="osimflow-test")

        mock_job_running = MagicMock()
        mock_job_running.status.state = ex._batch_v1.JobStatus.State.RUNNING
        mock_job_done = MagicMock()
        mock_job_done.status.state = ex._batch_v1.JobStatus.State.SUCCEEDED
        ex._client.get_job.side_effect = [
            mock_job_running,
            mock_job_running,
            mock_job_running,
            mock_job_running,
            mock_job_done,
        ]

        sleep_durations: list[float] = []

        def capture_sleep(duration: float) -> None:
            sleep_durations.append(duration)

        with patch("osimflow.executors.google_batch_executor.time.sleep", side_effect=capture_sleep):
            job = ex._wait_for_terminal("test-job")

        assert job.status.state == ex._batch_v1.JobStatus.State.SUCCEEDED

        for sleep_duration in sleep_durations:
            assert sleep_duration <= ex.max_poll_interval_s, (
                f"sleep({sleep_duration:.2f}s) exceeded max_poll_interval_s="
                f"{ex.max_poll_interval_s}s"
            )

        assert sleep_durations == [2.0, 4.0, 8.0, 8.0]

    def test_pbs_wait_for_terminal_never_exceeds_max_interval(
        self,
    ) -> None:
        """PBS _wait_for_terminal must never sleep longer than max_poll_interval_s."""
        ex = PBSExecutor.__new__(PBSExecutor)
        ex.poll_interval_s = 1.0
        ex.max_poll_interval_s = 8.0

        call_count = [0]

        def mock_query_state(job_id: str) -> str:
            call_count[0] += 1
            return "F" if call_count[0] >= 5 else "R"

        ex._query_job_state = mock_query_state
        ex._parse_exit_status = MagicMock(return_value=0)

        sleep_durations: list[float] = []

        def capture_sleep(duration: float) -> None:
            sleep_durations.append(duration)

        with patch("osimflow.executors.pbs_executor.time.sleep", side_effect=capture_sleep):
            state, exit_code = ex._wait_for_terminal("12345")

        assert state == "F"

        for sleep_duration in sleep_durations:
            assert sleep_duration <= ex.max_poll_interval_s, (
                f"sleep({sleep_duration:.2f}s) exceeded max_poll_interval_s="
                f"{ex.max_poll_interval_s}s"
            )

        assert sleep_durations == [2.0, 4.0, 8.0, 8.0]

    def test_kubernetes_wait_for_terminal_never_exceeds_max_interval(
        self,
    ) -> None:
        """Kubernetes _wait_for_terminal must never sleep longer than max_poll_interval_s."""
        ex = KubernetesExecutor.__new__(KubernetesExecutor)
        ex._client = MagicMock()
        ex.namespace = "default"
        ex.poll_interval_s = 1.0
        ex.max_poll_interval_s = 8.0

        pending_pod = MagicMock()
        pending_pod.to_dict.return_value = {"status": {"phase": "Pending"}}
        succeeded_pod = MagicMock()
        succeeded_pod.to_dict.return_value = {"status": {"phase": "Succeeded"}}

        result1 = MagicMock(items=[pending_pod])
        result2 = MagicMock(items=[pending_pod])
        result3 = MagicMock(items=[pending_pod])
        result4 = MagicMock(items=[pending_pod])
        result5 = MagicMock(items=[succeeded_pod])

        ex._client.list_namespaced_pod.side_effect = [
            result1, result2, result3, result4, result5
        ]

        sleep_durations: list[float] = []

        def capture_sleep(duration: float) -> None:
            sleep_durations.append(duration)

        with patch("osimflow.executors.kubernetes_executor.time.sleep", side_effect=capture_sleep):
            pod = ex._wait_for_terminal("test-job")

        assert pod["status"]["phase"] == "Succeeded"

        for sleep_duration in sleep_durations:
            assert sleep_duration <= ex.max_poll_interval_s, (
                f"sleep({sleep_duration:.2f}s) exceeded max_poll_interval_s="
                f"{ex.max_poll_interval_s}s"
            )

        assert sleep_durations == [2.0, 4.0, 8.0, 8.0]

    def test_docker_swarm_wait_for_terminal_never_exceeds_max_interval(
        self,
    ) -> None:
        """Docker Swarm _wait_for_terminal must never sleep longer than max_poll_interval_s."""
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
        ex._client = MagicMock()
        ex.poll_interval_s = 1.0
        ex.max_poll_interval_s = 8.0

        # Patch _get_service_status to return controlled dicts directly
        # so we bypass the complex service.tasks() mocking
        def make_status(state: str) -> dict:
            return {"tasks": [{"status": {"State": state}}]}

        call_count = [0]

        def mock_get_status(name: str) -> dict:
            call_count[0] += 1
            if call_count[0] >= 5:
                return make_status("complete")
            return make_status("running")

        ex._get_service_status = mock_get_status

        sleep_durations: list[float] = []

        def capture_sleep(duration: float) -> None:
            sleep_durations.append(duration)

        with patch("osimflow.executors.docker_swarm_executor.time.sleep", side_effect=capture_sleep):
            task = ex._wait_for_terminal("test-service")

        assert task["status"]["State"] == "complete"

        for sleep_duration in sleep_durations:
            assert sleep_duration <= ex.max_poll_interval_s, (
                f"sleep({sleep_duration:.2f}s) exceeded max_poll_interval_s="
                f"{ex.max_poll_interval_s}s"
            )

        assert sleep_durations == [2.0, 4.0, 8.0, 8.0]
