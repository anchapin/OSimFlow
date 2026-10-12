"""Unit tests for substrate job cancellation (issue #1538).

Covers the ``Handle.cancel()`` affordance, the ``BaseExecutor``
live-handle registry that backs ``BaseExecutor.cancel()``, and each of
the ten executors' substrate kill API:

* LocalExecutor — terminate in-flight ``run_subprocess`` children +
  cancel queued futures
* SlurmExecutor — submitit ``Job.cancel()`` (``scancel`` / debug SIGINT)
* PBSExecutor — ``qdel``
* AWSBatchExecutor — ``batch.terminate_job(jobId=...)``
* AzureBatchExecutor — ``BatchClient.terminate_task(job_id, task_id)``
* GoogleBatchExecutor — ``BatchServiceClient.delete_job(name=...)``
* KubernetesExecutor — ``delete_namespaced_job(..., propagation_policy=
  "Foreground")``
* NomadExecutor — ``POST /v1/allocation/<id>/stop``
* DockerSwarmExecutor — ``services.get(name).remove()``
* DaskJobQueueExecutor — ``Future.cancel()`` on the dask futures

Contract under test (per the issue): ``cancel()`` is idempotent, safe
to call after job completion (no-op, no raise), and issues exactly one
substrate kill per handle lifetime.
"""

from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from osimflow._subprocess_utils import (
    _ACTIVE_SUBPROCESSES,
    run_subprocess,
    terminate_active_subprocesses,
)
from osimflow.executors import BaseExecutor, Handle
from osimflow.executors.aws_batch_executor import _AWSBatchHandle
from osimflow.executors.azure_batch_executor import _AzureBatchHandle
from osimflow.executors.base import PollingHandle
from osimflow.executors.docker_swarm_executor import _DockerSwarmHandle
from osimflow.executors.google_batch_executor import _GoogleBatchHandle
from osimflow.executors.kubernetes_executor import _KubernetesHandle
from osimflow.executors.local_executor import LocalExecutor
from osimflow.executors.nomad_executor import _NomadClient, _NomadHandle
from osimflow.executors.pbs_executor import PBSExecutor, _PBSHandle


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _FakeFuture:
    """Duck-typed future with a recording cancel()."""

    def __init__(self, *, cancel_result: Any = True, cancel_raises: bool = False) -> None:
        self.cancel_calls = 0
        self._cancel_result = cancel_result
        self._cancel_raises = cancel_raises

    def cancel(self) -> Any:
        self.cancel_calls += 1
        if self._cancel_raises:
            raise RuntimeError("scancel: invalid job id")
        return self._cancel_result


class _NoCancelFuture:
    """A future without any cancel affordance (unsupported)."""


class _FakeFutureExecutor(BaseExecutor):
    """Minimal executor whose handles are backed by controllable futures."""

    name = "fake"

    def __init__(self) -> None:
        self._init_rate_limiter(None)
        self.futures: dict[str, Future[Any]] = {}

    def _do_submit(
        self,
        fn: Any,
        *args: Any,
        name: str = "task",
        **kwargs: Any,
    ) -> Handle:
        fut: Future[Any] = Future()
        self.futures[f"job-{name}"] = fut
        return Handle(job_id=f"job-{name}", _future=fut)

    def shutdown(self) -> None:
        pass


class _KillRecordingHandle(PollingHandle):
    """PollingHandle-style handle whose substrate kill is recorded."""

    def __init__(self, job_id: str, *, kill_raises: bool = False, kill_result: bool = True) -> None:
        self.job_id = job_id
        self._future = Future()
        self._executor = None
        self.kill_calls = 0
        self.kill_raises = kill_raises
        self.kill_result = kill_result

    def _cancel_job(self) -> bool:
        self.kill_calls += 1
        if self.kill_raises:
            raise RuntimeError("TerminateJob failed")
        return self.kill_result


# ---------------------------------------------------------------------------
# Handle.cancel() — the affordance itself
# ---------------------------------------------------------------------------
class TestHandleCancel:
    def test_true_returning_cancel_issued(self) -> None:
        fut = _FakeFuture(cancel_result=True)
        handle = Handle(job_id="j1", _future=fut)  # type: ignore[arg-type]
        assert handle.cancel() is True
        assert fut.cancel_calls == 1

    def test_none_returning_cancel_counts_as_issued(self) -> None:
        # submitit Job.cancel() / dask Future.cancel() return None on
        # success — None means "request accepted", not "not issued".
        fut = _FakeFuture(cancel_result=None)
        handle = Handle(job_id="j1", _future=fut)  # type: ignore[arg-type]
        assert handle.cancel() is True

    def test_false_returning_cancel_not_issued(self) -> None:
        # concurrent.futures.Future.cancel() returns False for a
        # running or already-done future — no kill was issued.
        fut = _FakeFuture(cancel_result=False)
        handle = Handle(job_id="j1", _future=fut)  # type: ignore[arg-type]
        assert handle.cancel() is False

    def test_idempotent(self) -> None:
        fut = _FakeFuture(cancel_result=True)
        handle = Handle(job_id="j1", _future=fut)  # type: ignore[arg-type]
        assert handle.cancel() is True
        assert handle.cancel() is False
        assert fut.cancel_calls == 1

    def test_cancel_exception_contained(self) -> None:
        fut = _FakeFuture(cancel_raises=True)
        handle = Handle(job_id="j1", _future=fut)  # type: ignore[arg-type]
        assert handle.cancel() is False  # never raises
        assert handle.cancel() is False  # and stays idempotent

    def test_future_without_cancel_unsupported(self) -> None:
        handle = Handle(job_id="j1", _future=_NoCancelFuture())  # type: ignore[arg-type]
        assert handle.cancel() is False

    def test_safe_after_completion(self) -> None:
        fut: Future[Any] = Future()
        fut.set_result(Path("/done"))
        handle = Handle(job_id="j1", _future=fut)
        # Done future: Future.cancel() returns False — a no-op, no raise.
        assert handle.cancel() is False


class TestPollingHandleCancel:
    def test_cancel_job_hook_invoked(self) -> None:
        handle = _KillRecordingHandle("j1")
        assert handle.cancel() is True
        assert handle.kill_calls == 1

    def test_idempotent_single_substrate_call(self) -> None:
        handle = _KillRecordingHandle("j1")
        handle.cancel()
        handle.cancel()
        assert handle.kill_calls == 1

    def test_kill_exception_contained(self) -> None:
        handle = _KillRecordingHandle("j1", kill_raises=True)
        assert handle.cancel() is False  # never raises
        assert handle.cancel() is False  # idempotent even after failure

    def test_default_hook_not_supported(self) -> None:
        handle = PollingHandle.__new__(PollingHandle)
        handle.job_id = "bare"
        handle._future = Future()
        assert handle.cancel() is False


# ---------------------------------------------------------------------------
# BaseExecutor registry + cancel sweep
# ---------------------------------------------------------------------------
class TestBaseExecutorCancelSweep:
    def test_submit_registers_and_cancel_kills(self) -> None:
        ex = _FakeFutureExecutor()
        ex.submit(lambda: None, name="a")
        ex.submit(lambda: None, name="b")
        # Both handles pending (not started): Future.cancel() -> True.
        ex.cancel()
        assert all(fut.cancelled() for fut in ex.futures.values())

    def test_registry_cleared_after_cancel(self) -> None:
        ex = _FakeFutureExecutor()
        ex.submit(lambda: None, name="a")
        ex.cancel()
        # Second sweep is a cheap no-op (no handles, no substrate calls).
        ex.cancel()

    def test_per_handle_failure_does_not_abort_sweep(self) -> None:
        ex = _FakeFutureExecutor()
        h1 = ex.submit(lambda: None, name="a")
        h2 = ex.submit(lambda: None, name="b")
        # First handle's cancel explodes; the second must still be killed.
        h1.cancel = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        ex.cancel()
        assert ex.futures["job-b"].cancelled()

    def test_completed_handles_pruned_from_registry(self) -> None:
        ex = _FakeFutureExecutor()
        ex._LIVE_HANDLE_SWEEP_THRESHOLD = 2  # type: ignore[assignment]
        for i in range(6):
            handle = ex.submit(lambda: None, name=f"n{i}")
            handle._future.set_result(f"done-{i}")  # type: ignore[attr-defined]
        live = ex.__dict__.get("_live_handles", {})
        assert len(live) <= 3  # threshold + at most one in-flight insert


# ---------------------------------------------------------------------------
# LocalExecutor — subprocess terminate + queued future cancel
# ---------------------------------------------------------------------------
class TestLocalExecutorCancel:
    def test_terminate_active_subprocesses_kills_child(self) -> None:
        """A run_subprocess child is SIGTERMed and its caller unblocks."""
        result: dict[str, Any] = {}
        started = threading.Event()

        def _run_child(out: Path) -> None:
            started.set()
            try:
                proc = run_subprocess(
                    ["sleep", "30"],
                    stdout_path=out / "stdout.log",
                    stderr_path=out / "stderr.log",
                )
                result["returncode"] = proc.returncode
            except BaseException as exc:  # noqa: BLE001 — recorded, not raised
                result["error"] = exc

        out_dir: Any = None  # filled below via tmp fixture pattern
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            t = threading.Thread(target=_run_child, args=(out_dir,), daemon=True)
            t.start()
            assert started.wait(timeout=5)
            # Wait until the child is registered.
            deadline = time.monotonic() + 5
            while not _ACTIVE_SUBPROCESSES and time.monotonic() < deadline:
                time.sleep(0.02)
            assert _ACTIVE_SUBPROCESSES, "child was never registered"

            killed = terminate_active_subprocesses()

            assert killed == 1
            t.join(timeout=10)
            assert not t.is_alive()
            assert result.get("returncode") != 0
            assert "error" not in result
        assert not _ACTIVE_SUBPROCESSES

    def test_terminate_no_active_subprocesses(self) -> None:
        assert terminate_active_subprocesses() == 0

    def test_executor_cancel_terminates_subprocess_and_futures(self, tmp_path: Path) -> None:
        ex = LocalExecutor(max_workers=1)
        done: dict[str, Any] = {}
        started = threading.Event()

        def _work() -> None:
            started.set()
            proc = run_subprocess(
                ["sleep", "30"],
                stdout_path=tmp_path / "stdout.log",
                stderr_path=tmp_path / "stderr.log",
            )
            done["returncode"] = proc.returncode

        running = ex.submit(_work, name="running")
        queued = ex.submit(_work, name="queued")
        assert started.wait(timeout=5)
        # Wait until the running work's subprocess is registered (the
        # event fires before run_subprocess spawns the child).
        deadline = time.monotonic() + 5
        while not _ACTIVE_SUBPROCESSES and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _ACTIVE_SUBPROCESSES, "child was never registered"

        ex.cancel()

        # The queued future is cancelled outright; the running one's
        # subprocess was terminated (work function returns nonzero rc).
        assert queued._future.cancelled()
        deadline = time.monotonic() + 10
        while not running._future.done() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert running._future.done()
        assert done.get("returncode", 0) != 0
        ex.shutdown()


# ---------------------------------------------------------------------------
# SlurmExecutor — submitit Job.cancel (scancel / debug SIGINT)
# ---------------------------------------------------------------------------
class TestSlurmExecutorCancel:
    def test_cancel_calls_submitit_job_cancel(self) -> None:
        from osimflow.executors import SlurmExecutor

        cancelled: list[str] = []

        class _FakeJob:
            job_id = "12345"

            def result(self, timeout: float | None = None) -> int:
                return 42

            def done(self) -> bool:
                return False

            def cancel(self, check: bool = True) -> None:
                cancelled.append(self.job_id)

        class _FakeAutoExecutor:
            folder = "/tmp/fake-slurm-logs"

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def update_parameters(self, **kwargs: Any) -> None:
                pass

            def submit(self, fn: Any, *args: Any, **kwargs: Any) -> _FakeJob:
                return _FakeJob()

        with patch("submitit.AutoExecutor", _FakeAutoExecutor):
            ex = SlurmExecutor(partition="short")
            ex.submit(lambda: 1, name="sim_s0000")

            ex.cancel()

        assert cancelled == ["12345"]


# ---------------------------------------------------------------------------
# PBSExecutor — qdel
# ---------------------------------------------------------------------------
class TestPBSExecutorCancel:
    def _handle(self) -> _PBSHandle:
        ex = PBSExecutor.__new__(PBSExecutor)
        ex._init_rate_limiter(None)
        return _PBSHandle(job_id="101.server", executor=ex)

    def test_qdel_invoked_with_job_id(self) -> None:
        handle = self._handle()
        with patch("osimflow.executors.pbs_executor.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["qdel", "101.server"], returncode=0
            )
            assert handle.cancel() is True
        mock_run.assert_called_once_with(
            ["qdel", "101.server"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_qdel_failure_reported_not_raised(self) -> None:
        handle = self._handle()
        with patch("osimflow.executors.pbs_executor.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["qdel", "101.server"], returncode=1
            )
            assert handle.cancel() is False

    def test_idempotent(self) -> None:
        handle = self._handle()
        with patch("osimflow.executors.pbs_executor.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            handle.cancel()
            handle.cancel()
        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# AWSBatchExecutor — TerminateJob
# ---------------------------------------------------------------------------
class TestAWSBatchExecutorCancel:
    def _handle(self) -> tuple[_AWSBatchHandle, MagicMock]:
        from osimflow.executors import AWSBatchExecutor

        ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        ex._client = MagicMock()
        ex._init_rate_limiter(None)
        handle = _AWSBatchHandle(job_id="job-abc-123", executor=ex, submit_params={})
        return handle, ex._client

    def test_terminate_job_called_with_job_id(self) -> None:
        handle, client = self._handle()
        assert handle.cancel() is True
        client.terminate_job.assert_called_once_with(
            jobId="job-abc-123",
            reason="OSimFlow campaign cancellation (issue #1538)",
        )

    def test_idempotent_and_safe_after_completion(self) -> None:
        handle, client = self._handle()
        assert handle.cancel() is True
        # A racing completion: the substrate rejects the terminate.
        client.terminate_job.side_effect = RuntimeError("job already terminal")
        assert handle.cancel() is False  # no raise, reported as not issued
        assert client.terminate_job.call_count == 1  # idempotent


# ---------------------------------------------------------------------------
# AzureBatchExecutor — terminate_task
# ---------------------------------------------------------------------------
class TestAzureBatchExecutorCancel:
    def _handle(self) -> tuple[_AzureBatchHandle, MagicMock]:
        from osimflow.executors import AzureBatchExecutor

        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        ex._client = MagicMock()
        ex.location = "eastus"
        ex._init_rate_limiter(None)
        handle = _AzureBatchHandle(job_id="osimflow-sim-s0000", executor=ex, submit_params={})
        return handle, ex._client

    def test_terminate_task_called_with_job_and_task_id(self) -> None:
        handle, client = self._handle()
        assert handle.cancel() is True
        # One job carries exactly one task sharing the job's id.
        client.terminate_task.assert_called_once_with("osimflow-sim-s0000", "osimflow-sim-s0000")

    def test_idempotent(self) -> None:
        handle, client = self._handle()
        handle.cancel()
        handle.cancel()
        assert client.terminate_task.call_count == 1


# ---------------------------------------------------------------------------
# GoogleBatchExecutor — delete_job
# ---------------------------------------------------------------------------
class TestGoogleBatchExecutorCancel:
    def _handle(self) -> tuple[_GoogleBatchHandle, MagicMock]:
        from osimflow.executors import GoogleBatchExecutor

        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        ex._client = MagicMock()
        ex.region = "us-central1"
        ex._init_rate_limiter(None)
        handle = _GoogleBatchHandle(
            job_name="projects/p/locations/l/jobs/osimflow-sim-s0000",
            executor=ex,
            submit_params={},
        )
        return handle, ex._client

    def test_delete_job_called_with_name(self) -> None:
        handle, client = self._handle()
        assert handle.cancel() is True
        client.delete_job.assert_called_once_with(
            name="projects/p/locations/l/jobs/osimflow-sim-s0000"
        )

    def test_idempotent(self) -> None:
        handle, client = self._handle()
        handle.cancel()
        handle.cancel()
        assert client.delete_job.call_count == 1


# ---------------------------------------------------------------------------
# KubernetesExecutor — delete_namespaced_job
# ---------------------------------------------------------------------------
class TestKubernetesExecutorCancel:
    def _handle(self) -> tuple[_KubernetesHandle, MagicMock]:
        from osimflow.executors import KubernetesExecutor

        ex = KubernetesExecutor.__new__(KubernetesExecutor)
        ex.namespace = "osimflow"
        ex._client = MagicMock()
        ex._init_rate_limiter(None)
        handle = _KubernetesHandle(job_name="osimflow-sim-s0000", executor=ex, submit_params={})
        return handle, ex._client

    def test_delete_namespaced_job_foreground(self) -> None:
        handle, client = self._handle()
        assert handle.cancel() is True
        client.delete_namespaced_job.assert_called_once_with(
            name="osimflow-sim-s0000",
            namespace="osimflow",
            propagation_policy="Foreground",
        )

    def test_idempotent(self) -> None:
        handle, client = self._handle()
        handle.cancel()
        handle.cancel()
        assert client.delete_namespaced_job.call_count == 1


# ---------------------------------------------------------------------------
# NomadExecutor — allocation stop
# ---------------------------------------------------------------------------
class TestNomadExecutorCancel:
    def _handle(self, client: MagicMock) -> _NomadHandle:
        from osimflow.executors import NomadExecutor

        ex = NomadExecutor.__new__(NomadExecutor)
        ex._client = client
        ex.datacentre = "dc1"
        ex._init_rate_limiter(None)
        return _NomadHandle(job_id="osimflow-sim-s0000", eval_id="eval-1", executor=ex)

    def test_stop_allocation_called_with_resolved_id(self) -> None:
        client = MagicMock()
        client.resolve_allocation.return_value = "alloc-9f"
        handle = self._handle(client)
        assert handle.cancel() is True
        client.stop_allocation.assert_called_once_with("alloc-9f")

    def test_idempotent(self) -> None:
        client = MagicMock()
        client.resolve_allocation.return_value = "alloc-9f"
        handle = self._handle(client)
        handle.cancel()
        handle.cancel()
        assert client.stop_allocation.call_count == 1

    def test_client_stop_allocation_posts_to_alloc_stop_endpoint(self) -> None:
        client = _NomadClient(address="http://nomad.example:4646", token=None)
        with patch.object(client, "_request") as mock_request:
            client.stop_allocation("alloc-9f")
        mock_request.assert_called_once_with("POST", "/v1/allocation/alloc-9f/stop")


# ---------------------------------------------------------------------------
# DockerSwarmExecutor — service remove
# ---------------------------------------------------------------------------
class TestDockerSwarmExecutorCancel:
    def _handle(self) -> tuple[_DockerSwarmHandle, MagicMock]:
        from osimflow.executors import DockerSwarmExecutor

        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
        ex._client = MagicMock()
        ex._init_rate_limiter(None)
        handle = _DockerSwarmHandle(
            service_name="osimflow-sim-s0000", executor=ex, submit_params={}
        )
        return handle, ex._client

    def test_service_removed(self) -> None:
        handle, client = self._handle()
        assert handle.cancel() is True
        client.services.get.assert_called_once_with("osimflow-sim-s0000")
        client.services.get.return_value.remove.assert_called_once_with()

    def test_idempotent(self) -> None:
        handle, client = self._handle()
        handle.cancel()
        handle.cancel()
        assert client.services.get.call_count == 1


# ---------------------------------------------------------------------------
# DaskJobQueueExecutor — dask future cancel
# ---------------------------------------------------------------------------
class TestDaskJobQueueExecutorCancel:
    def test_dask_future_cancelled(self) -> None:
        from osimflow.executors import DaskJobQueueExecutor

        ex = DaskJobQueueExecutor.__new__(DaskJobQueueExecutor)
        ex._init_rate_limiter(None)
        # Skip the auto-scaler thread: pretend it is already running.
        ex._scaler_running = True
        dask_future = MagicMock()
        cluster = MagicMock()
        cluster.get_client().submit.return_value = dask_future
        ex._cluster = cluster

        handle = ex.submit(lambda: 1, name="sim_s0000")

        # Duck-typed base Handle.cancel delegates to the dask future's
        # cancel() (returns None -> treated as "kill issued").
        assert handle.cancel() is True
        dask_future.cancel.assert_called_once_with()
        # Idempotent.
        assert handle.cancel() is False
        dask_future.cancel.assert_called_once_with()
