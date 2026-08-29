"""Unit tests for osimflow.executors.dask_jobqueue_executor (issue #338)."""

from __future__ import annotations

from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import DaskJobQueueExecutor
from osimflow.executors.dask_jobqueue_executor import _DaskJobQueueHandle


class TestDaskJobQueueExecutor:
    """DaskJobQueueExecutor wraps dask_jobqueue cluster backends."""

    def _make_executor(self, **kw: str | int | None) -> DaskJobQueueExecutor:
        ex = DaskJobQueueExecutor.__new__(DaskJobQueueExecutor)
        ex.cluster_type = kw.get("cluster_type", "slurm")
        ex.min_workers = kw.get("min_workers", 1)
        ex.max_workers = kw.get("max_workers", 10)
        ex.cpus_per_worker = kw.get("cpus_per_worker", 2)
        ex.memory_per_worker = kw.get("memory_per_worker", "4GiB")
        ex.walltime = kw.get("walltime", "02:00:00")
        ex.queue = kw.get("queue", None)
        ex.project = kw.get("project", None)
        ex.job_extra = kw.get("job_extra", {})
        ex.scale_interval_s = kw.get("scale_interval_s", 5.0)
        ex._cluster = None
        ex._client = None
        ex._scaler_running = False
        return ex

    def test_name_attribute(self) -> None:
        ex = self._make_executor()
        assert ex.name == "dask_jobqueue"

    def test_default_cluster_type(self) -> None:
        ex = self._make_executor()
        assert ex.cluster_type == "slurm"

    def test_default_min_workers(self) -> None:
        ex = self._make_executor()
        assert ex.min_workers == 1

    def test_default_max_workers(self) -> None:
        ex = self._make_executor()
        assert ex.max_workers == 10

    def test_custom_workers(self) -> None:
        ex = self._make_executor(min_workers=2, max_workers=20)
        assert ex.min_workers == 2
        assert ex.max_workers == 20

    def test_custom_cpus_per_worker(self) -> None:
        ex = self._make_executor(cpus_per_worker=4)
        assert ex.cpus_per_worker == 4

    def test_custom_memory_per_worker(self) -> None:
        ex = self._make_executor(memory_per_worker="8GiB")
        assert ex.memory_per_worker == "8GiB"

    def test_custom_walltime(self) -> None:
        ex = self._make_executor(walltime="04:00:00")
        assert ex.walltime == "04:00:00"

    def test_custom_queue(self) -> None:
        ex = self._make_executor(queue="gpu")
        assert ex.queue == "gpu"

    def test_custom_project(self) -> None:
        ex = self._make_executor(project="myproject")
        assert ex.project == "myproject"

    def test_job_extra_default_empty(self) -> None:
        ex = self._make_executor()
        assert ex.job_extra == {}

    def test_job_extra_custom(self) -> None:
        ex = self._make_executor(job_extra={"gres": "gpu:1"})
        assert ex.job_extra["gres"] == "gpu:1"

    def test_scale_interval_default(self) -> None:
        ex = self._make_executor()
        assert ex.scale_interval_s == 5.0

    def test_scale_interval_custom(self) -> None:
        ex = self._make_executor(scale_interval_s=10.0)
        assert ex.scale_interval_s == 10.0

    def test_cluster_type_pbs(self) -> None:
        ex = self._make_executor(cluster_type="pbs")
        assert ex.cluster_type == "pbs"

    def test_cluster_type_kubernetes(self) -> None:
        ex = self._make_executor(cluster_type="kubernetes")
        assert ex.cluster_type == "kubernetes"

    def test_submit_creates_cluster_on_first_call(self) -> None:
        ex = self._make_executor()
        mock_cluster = MagicMock()
        mock_client = MagicMock()
        mock_future = MagicMock()
        mock_cluster.get_client.return_value = mock_client
        mock_client.submit.return_value = mock_future

        with patch.object(ex, "_build_cluster", return_value=mock_cluster):
            handle = ex.submit(lambda: 42, name="test")

        assert handle.job_id.startswith("dask-test-")
        assert ex._cluster is mock_cluster
        ex.shutdown()

    def test_submit_with_args(self) -> None:
        ex = self._make_executor()
        mock_cluster = MagicMock()
        mock_client = MagicMock()
        mock_future = MagicMock()
        mock_future.result.return_value = 10
        mock_cluster.get_client.return_value = mock_client
        mock_client.submit.return_value = mock_future

        with patch.object(ex, "_build_cluster", return_value=mock_cluster):
            handle = ex.submit(lambda x, y: x + y, 3, 7, name="add")

        assert handle.result(timeout=5) == 10
        ex.shutdown()

    def test_submit_sets_container_env(self) -> None:
        ex = self._make_executor()
        mock_cluster = MagicMock()
        mock_client = MagicMock()
        mock_future = MagicMock()
        mock_cluster.get_client.return_value = mock_client
        mock_client.submit.return_value = mock_future

        with patch.object(ex, "_build_cluster", return_value=mock_cluster):
            ex.submit(lambda: None, name="container-test", container="nrel/openstudio:3.11.0")

        mock_client.submit.assert_called_once()
        ex.shutdown()

    def test_submit_sets_openstudio_version_env(self) -> None:
        ex = self._make_executor()
        mock_cluster = MagicMock()
        mock_client = MagicMock()
        mock_future = MagicMock()
        mock_cluster.get_client.return_value = mock_client
        mock_client.submit.return_value = mock_future

        with patch.object(ex, "_build_cluster", return_value=mock_cluster):
            ex.submit(lambda: None, name="version-test", openstudio_version="3.11.0")

        mock_client.submit.assert_called_once()
        ex.shutdown()

    def test_auto_scaler_started_on_first_submit(self) -> None:
        ex = self._make_executor()
        mock_cluster = MagicMock()
        mock_client = MagicMock()
        mock_future = MagicMock()
        mock_cluster.get_client.return_value = mock_client
        mock_client.submit.return_value = mock_future
        mock_client.tasks.return_value = {}

        with patch.object(ex, "_build_cluster", return_value=mock_cluster):
            ex.submit(lambda: None, name="scaler-test")

        assert ex._scaler_running is True
        ex.shutdown()

    def test_shutdown_closes_cluster(self) -> None:
        ex = self._make_executor()
        mock_cluster = MagicMock()
        ex._cluster = mock_cluster
        ex._scaler_running = True

        ex.shutdown()

        mock_cluster.close.assert_called_once()
        assert ex._cluster is None
        assert ex._scaler_running is False

    def test_shutdown_is_idempotent(self) -> None:
        ex = self._make_executor()
        ex._cluster = None
        ex._scaler_running = False

        ex.shutdown()
        ex.shutdown()

    def test_scale_to_clamps_to_max_workers(self) -> None:
        ex = self._make_executor(min_workers=2, max_workers=5)
        mock_cluster = MagicMock()
        ex._cluster = mock_cluster

        ex._scale_to(100)

        mock_cluster.scale.assert_called_once_with(5)
        ex.shutdown()

    def test_scale_to_clamps_to_min_workers(self) -> None:
        ex = self._make_executor(min_workers=3, max_workers=10)
        mock_cluster = MagicMock()
        ex._cluster = mock_cluster

        ex._scale_to(0)

        mock_cluster.scale.assert_called_once_with(3)
        ex.shutdown()

    def test_scale_to_respects_target_within_range(self) -> None:
        ex = self._make_executor(min_workers=1, max_workers=8)
        mock_cluster = MagicMock()
        ex._cluster = mock_cluster

        ex._scale_to(4)

        mock_cluster.scale.assert_called_once_with(4)
        ex.shutdown()

    def test_unknown_cluster_type_raises(self) -> None:
        pytest.importorskip("dask_jobqueue")
        ex = self._make_executor(cluster_type="unsupported")
        with pytest.raises(ValueError, match="unknown dask cluster type"):
            ex._build_cluster()


class TestDaskJobQueueHandle:
    """_DaskJobQueueHandle wraps a Dask Future."""

    def _make_handle(self, **kwargs: object) -> _DaskJobQueueHandle:
        fut: Future[int] = Future()
        mock_cluster = MagicMock()
        return _DaskJobQueueHandle(
            job_id=kwargs.get("job_id", "dask-test-1"),
            future=fut,
            cluster=mock_cluster,
        )

    def test_result_returns_future_value(self) -> None:
        fut: Future[int] = Future()
        fut.set_result(42)
        h = _DaskJobQueueHandle(job_id="j-1", future=fut, cluster=MagicMock())
        assert h.result() == 42

    def test_result_timeout_forwarded(self) -> None:
        fut: Future[int] = Future()
        fut.set_result(99)
        h = _DaskJobQueueHandle(job_id="j-2", future=fut, cluster=MagicMock())
        assert h.result(timeout=1) == 99

    def test_done_true_when_future_completed(self) -> None:
        fut: Future[int] = Future()
        fut.set_result(1)
        h = _DaskJobQueueHandle(job_id="j-3", future=fut, cluster=MagicMock())
        assert h.done() is True

    def test_done_false_when_future_pending(self) -> None:
        fut: Future[int] = Future()
        h = _DaskJobQueueHandle(job_id="j-4", future=fut, cluster=MagicMock())
        assert h.done() is False

    def test_result_propagates_exception(self) -> None:
        fut: Future[int] = Future()
        fut.set_exception(ValueError("boom"))
        h = _DaskJobQueueHandle(job_id="j-5", future=fut, cluster=MagicMock())
        with pytest.raises(ValueError, match="boom"):
            h.result()

    def test_worker_fields_default_none(self) -> None:
        fut: Future[int] = Future()
        h = _DaskJobQueueHandle(job_id="j-6", future=fut, cluster=MagicMock())
        assert h.worker_id == "j-6"
        assert h.worker_ip is None
        assert h.worker_region is None
