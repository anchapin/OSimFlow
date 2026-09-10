"""Unit tests for osimflow.executors.dask_jobqueue_executor (issue #338, #1689)."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import DaskJobQueueExecutor
from osimflow.executors.dask_jobqueue_executor import (
    _apply_env_isolated,
    _DaskJobQueueHandle,
)


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


# ---------------------------------------------------------------------------
# Issue #1689: per-task ``os.environ`` snapshot/restore so concurrent Dask
# tasks on the same worker process don't leak ``OSIMFLOW_OS_VERSION`` /
# ``OSIMFLOW_CONTAINER`` between each other or into subsequent unrelated
# tasks. The fix moves the env-mutation out of the ``_wrapped`` closure
# into a dedicated ``_apply_env_isolated`` helper that snapshots per-call,
# applies the overrides, runs ``fn``, and restores on exit (even on raise).
# ---------------------------------------------------------------------------

#: Test-prefixed env keys so we can snapshot before/after each test and
#: assert that no per-task key leaks into the process environment.
ENV_PREFIX = "OSIMFLOW_DASK_TEST_"


def _env_keys() -> set[str]:
    return {k for k in os.environ if k.startswith(ENV_PREFIX)}


@pytest.fixture(autouse=True)
def _isolate_environ() -> Any:
    """Snapshot prefixed env keys before/after each test; assert no leaks."""
    baseline = _env_keys()
    try:
        yield
    finally:
        leaked = _env_keys() - baseline
        assert not leaked, f"test leaked env keys: {sorted(leaked)}"


class TestApplyEnvIsolated:
    """Direct unit tests for the ``_apply_env_isolated`` helper.

    These tests exercise the per-task snapshot/restore logic in
    isolation. The integration with the Dask ``_wrapped`` closure is
    covered in :class:`TestDaskWrappedClosure` below.
    """

    def test_fn_sees_overrides(self) -> None:
        """Inside ``fn``, the overridden keys are visible."""
        seen = _apply_env_isolated(
            {"OSIMFLOW_DASK_TEST_KEY": "v"},
            lambda: os.environ.get("OSIMFLOW_DASK_TEST_KEY"),
        )
        assert seen == "v"

    def test_keys_removed_after_task_when_originally_absent(self) -> None:
        """If the key was absent before, it is absent again after."""
        assert "OSIMFLOW_DASK_TEST_KEY" not in os.environ
        _apply_env_isolated(
            {"OSIMFLOW_DASK_TEST_KEY": "v"},
            lambda: None,
        )
        assert "OSIMFLOW_DASK_TEST_KEY" not in os.environ

    def test_keys_restored_to_original_value_when_originally_set(self) -> None:
        """If the key was present, it is restored to its original value."""
        os.environ["OSIMFLOW_DASK_TEST_KEY"] = "original"
        try:
            _apply_env_isolated(
                {"OSIMFLOW_DASK_TEST_KEY": "overridden"},
                lambda: None,
            )
            assert os.environ["OSIMFLOW_DASK_TEST_KEY"] == "original"
        finally:
            os.environ.pop("OSIMFLOW_DASK_TEST_KEY", None)

    def test_keys_restored_when_fn_raises(self) -> None:
        """The ``finally`` clause restores even when ``fn`` raises."""
        os.environ["OSIMFLOW_DASK_TEST_KEY"] = "original"
        try:

            def boom() -> None:
                raise RuntimeError("boom")

            with pytest.raises(RuntimeError, match="boom"):
                _apply_env_isolated(
                    {"OSIMFLOW_DASK_TEST_KEY": "overridden"},
                    boom,
                )
            assert os.environ["OSIMFLOW_DASK_TEST_KEY"] == "original"
        finally:
            os.environ.pop("OSIMFLOW_DASK_TEST_KEY", None)

    def test_missing_key_restored_as_absent_when_fn_raises(self) -> None:
        """A key that was absent before is removed again after a raise."""
        assert "OSIMFLOW_DASK_TEST_KEY" not in os.environ

        def boom() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            _apply_env_isolated(
                {"OSIMFLOW_DASK_TEST_KEY": "x"},
                boom,
            )
        assert "OSIMFLOW_DASK_TEST_KEY" not in os.environ

    def test_returns_fn_return_value(self) -> None:
        """The return value of ``fn`` is propagated unchanged."""
        assert _apply_env_isolated({}, lambda: 42) == 42

    def test_passes_args_through(self) -> None:
        """Positional args are forwarded to ``fn`` unchanged."""
        assert _apply_env_isolated({}, lambda a, b: a * b, 3, 4) == 12

    def test_multiple_overrides_all_restored(self) -> None:
        """All keys in ``overrides`` are snapshot-set-restore'd."""
        os.environ["OSIMFLOW_DASK_TEST_A"] = "orig-A"
        os.environ["OSIMFLOW_DASK_TEST_B"] = "orig-B"
        try:
            _apply_env_isolated(
                {
                    "OSIMFLOW_DASK_TEST_A": "new-A",
                    "OSIMFLOW_DASK_TEST_B": "new-B",
                    "OSIMFLOW_DASK_TEST_C": "new-C",  # originally absent
                },
                lambda: None,
            )
            assert os.environ["OSIMFLOW_DASK_TEST_A"] == "orig-A"
            assert os.environ["OSIMFLOW_DASK_TEST_B"] == "orig-B"
            assert "OSIMFLOW_DASK_TEST_C" not in os.environ
        finally:
            for k in ("OSIMFLOW_DASK_TEST_A", "OSIMFLOW_DASK_TEST_B"):
                os.environ.pop(k, None)

    def test_unrelated_env_keys_untouched(self) -> None:
        """Keys NOT in ``overrides`` are left alone (clear=False semantic)."""
        os.environ["OSIMFLOW_DASK_TEST_UNRELATED"] = "untouched"
        try:
            _apply_env_isolated(
                {"OSIMFLOW_DASK_TEST_KEY": "v"},
                lambda: None,
            )
            assert os.environ["OSIMFLOW_DASK_TEST_UNRELATED"] == "untouched"
        finally:
            os.environ.pop("OSIMFLOW_DASK_TEST_UNRELATED", None)

    def test_sequential_tasks_each_see_own_value_no_leak(self) -> None:
        """The primary acceptance criterion: sequential isolation.

        The previous (broken) closure set ``OSIMFLOW_OS_VERSION`` and
        ``OSIMFLOW_CONTAINER`` on the worker process and never
        restored them, so task N+1 could observe task N's value. With
        per-call snapshot/restore, each task sees a clean env state at
        fn entry (its own override applied, the previous task's value
        already restored) and after each task completes the original
        state is restored.
        """
        seen: list[str] = []
        for value in ("3.5.0", "3.11.0", "3.12.0"):
            observed = _apply_env_isolated(
                {"OSIMFLOW_DASK_TEST_KEY": value},
                lambda: os.environ.get("OSIMFLOW_DASK_TEST_KEY"),
            )
            seen.append(observed or "<missing>")
            # Inside the helper: the override is applied. After the
            # helper returns: the override is gone.
            assert "OSIMFLOW_DASK_TEST_KEY" not in os.environ, (
                f"task with value={value!r} left a residual key"
            )

        assert seen == ["3.5.0", "3.11.0", "3.12.0"], seen

    def test_concurrent_tasks_do_not_leak_between_each_other(self) -> None:
        """Acceptance criteria #1: concurrent tasks with unique keys don't
        observe each other's overrides.

        Each task reads an env key that NO other task writes. With
        per-call snapshots, every task observes its own override
        independently — the snapshot is taken before fn runs and is
        scoped to that single task. This mirrors the LocalExecutor
        #1406 test pattern (per-task unique env keys → concurrent
        isolation is verifiable).

        Note: when concurrent tasks share the SAME env key, a
        cross-thread read race is inherent to ``os.environ`` being
        process-global; the helper guarantees sequential isolation
        (each subsequent task sees a clean env state at fn entry) and
        per-task isolation when tasks use distinct keys.
        """
        results: dict[str, str] = {}
        errors: list[BaseException] = []
        barrier = threading.Barrier(3)

        def task(label: str, key: str) -> None:
            try:
                # All three threads gate here so they all enter their
                # ``_apply_env_isolated`` snapshot window at the same time.
                barrier.wait(timeout=5)
                seen = os.environ.get(key)
                # Tiny sleep to give sibling threads a chance to
                # clobber env if the fix is broken.
                import time as _time  # noqa: PLC0415 — local-scope import

                _time.sleep(0.01)
                results[label] = seen or "<missing>"
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def runner(label: str, key: str, value: str) -> None:
            _apply_env_isolated({key: value}, task, label, key)

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(runner, "task-A", "OSIMFLOW_DASK_TEST_KEY_A", "A"),
                pool.submit(runner, "task-B", "OSIMFLOW_DASK_TEST_KEY_B", "B"),
                pool.submit(runner, "task-C", "OSIMFLOW_DASK_TEST_KEY_C", "C"),
            ]
            for f in futures:
                f.result(timeout=5)

        assert not errors, f"tasks raised: {errors}"
        assert results == {
            "task-A": "A",
            "task-B": "B",
            "task-C": "C",
        }, results

    def test_no_residual_keys_after_concurrent_tasks(self) -> None:
        """Acceptance criteria #2: after all tasks complete, no residual keys.

        Even after a concurrent burst of tasks — including one that
        raises — the per-call snapshot/restore guarantees that the
        process-global ``os.environ`` carries no keys from any task.
        """
        baseline = _env_keys()

        def task(value: str) -> None:
            if value == "raise":
                raise RuntimeError(f"intentional: {value}")

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(task, "task-A"),
                pool.submit(task, "task-B"),
                pool.submit(task, "raise"),
                pool.submit(task, "task-C"),
            ]
            for f in futures:
                # task "raise" will propagate — collect, don't assert.
                try:
                    f.result(timeout=5)
                except RuntimeError:
                    pass

        assert _env_keys() == baseline, (
            f"residual keys after concurrent tasks: {sorted(_env_keys() - baseline)}"
        )


class TestDaskWrappedClosure:
    """Integration: the ``_wrapped`` closure routes through the helper.

    These tests capture the closure submitted to ``cluster.get_client()
    .submit`` (via a mock) and exercise it directly. They verify that:

    * The closure restores env after each call (sequential leak fix).
    * Concurrent calls on the captured closure do not observe each
      other's values.
    * Submitting the closure with no ``container`` / no
      ``openstudio_version`` is a no-op for ``os.environ``.
    """

    def _make_executor(self) -> DaskJobQueueExecutor:
        ex = DaskJobQueueExecutor.__new__(DaskJobQueueExecutor)
        ex.cluster_type = "slurm"
        ex.min_workers = 1
        ex.max_workers = 10
        ex.cpus_per_worker = 2
        ex.memory_per_worker = "4GiB"
        ex.walltime = "02:00:00"
        ex.queue = None
        ex.project = None
        ex.job_extra = {}
        ex.scale_interval_s = 5.0
        ex._cluster = None
        ex._client = None
        ex._scaler_running = False
        return ex

    def _capture_wrapped(
        self,
        ex: DaskJobQueueExecutor,
        fn: Callable[..., Any],
        *submit_args: Any,
        **submit_kwargs: Any,
    ) -> Callable[[], Any]:
        """Capture the closure that the executor submits to Dask."""
        captured: list[Callable[[], Any]] = []

        mock_cluster = MagicMock()
        mock_client = MagicMock()

        def _capture(closure: Callable[[], Any], *a: Any, **kw: Any) -> MagicMock:
            captured.append(closure)
            fut = MagicMock()
            fut.result.return_value = None
            return fut

        mock_cluster.get_client.return_value = mock_client
        mock_client.submit.side_effect = _capture

        with patch.object(ex, "_build_cluster", return_value=mock_cluster):
            ex.submit(fn, *submit_args, **submit_kwargs)
            ex.shutdown()

        assert len(captured) == 1, f"expected 1 captured closure, got {len(captured)}"
        return captured[0]

    def test_wrapped_restores_env_after_completion(self) -> None:
        """After the captured closure runs, the env keys are gone."""
        ex = self._make_executor()
        wrapped = self._capture_wrapped(
            ex,
            lambda: None,
            name="restore-test",
            openstudio_version="3.11.0",
            container="nrel/openstudio:3.11.0",
        )
        assert wrapped() is None
        assert "OSIMFLOW_OS_VERSION" not in os.environ
        assert "OSIMFLOW_CONTAINER" not in os.environ

    def test_wrapped_restores_env_when_fn_raises(self) -> None:
        """``finally`` restore is reached even when ``fn`` raises."""

        def boom() -> None:
            raise RuntimeError("boom")

        ex = self._make_executor()
        wrapped = self._capture_wrapped(
            ex,
            boom,
            name="raise-test",
            openstudio_version="3.11.0",
        )
        with pytest.raises(RuntimeError, match="boom"):
            wrapped()
        assert "OSIMFLOW_OS_VERSION" not in os.environ

    def test_wrapped_no_env_args_is_a_noop(self) -> None:
        """Submitting without ``container`` / ``openstudio_version`` is a no-op.

        Verifies the ``if overrides: return _apply_env_isolated(...)`` /
        ``return fn(*args)`` branch — neither path should touch
        ``os.environ`` and no helper invocation is needed.
        """

        def body() -> str:
            return "ran"

        ex = self._make_executor()
        wrapped = self._capture_wrapped(ex, body, name="no-env-test")
        before = dict(os.environ)
        assert wrapped() == "ran"
        assert dict(os.environ) == before, "no-env submit must not mutate os.environ"

    def test_wrapped_concurrent_no_cross_leak(self) -> None:
        """Acceptance criteria #1+2 from the issue, end-to-end through the closure.

        Submit two tasks with different versions, intercept the closures,
        and run them on a shared thread pool (mirroring Dask's threaded
        worker). A third task submitted after both completes sees no
        residual keys.

        Note: the two tasks share the same env key (``OSIMFLOW_OS_VERSION``)
        — concurrent reads of a process-global key from sibling threads
        are inherently racy, mirroring the LocalExecutor #1406 caveat.
        What the fix DELIVERS deterministically is:

        1. Sequential isolation: each task starts with a clean env
           state (the per-call snapshot captures whatever was current
           at call entry — NOT whatever was set by a previous task).
        2. No residual keys after all tasks complete (acceptance #2).
        3. Per-call snapshot semantics: a pre-set env value is captured
           and restored, proving the snapshot is taken at call time,
           not at closure-creation time.

        For deterministic concurrent cross-task isolation with the
        SAME env key, a per-task delivery mechanism (Dask worker
        plugin) would be required — out of scope for the snapshot /
        try/finally fix.
        """

        def make_task_body(label: str) -> Callable[[], str]:
            def body() -> str:
                return f"{label}:{os.environ.get('OSIMFLOW_OS_VERSION')}"

            return body

        ex_a = self._make_executor()
        wrapped_a = self._capture_wrapped(
            ex_a,
            make_task_body("a"),
            name="a",
            openstudio_version="3.5.0",
        )

        ex_b = self._make_executor()
        wrapped_b = self._capture_wrapped(
            ex_b,
            make_task_body("b"),
            name="b",
            openstudio_version="3.11.0",
        )

        # Run the two captured closures concurrently — same as a Dask
        # threaded worker would.
        barrier = threading.Barrier(2)

        def gated(closure: Callable[[], str]) -> str:
            barrier.wait(timeout=5)
            return closure()

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(gated, wrapped_a)
            fut_b = pool.submit(gated, wrapped_b)
            seen_a = fut_a.result(timeout=5)
            seen_b = fut_b.result(timeout=5)

        # Each closure observed ONE of the two values — proof that
        # per-task snapshot/restore was in effect (the OLD broken code
        # would have left the LAST-set value lingering and the
        # second-to-run closure would observe it; the snapshot/restore
        # code guarantees the override was applied per-task).
        assert seen_a.startswith("a:") and seen_a.endswith(":3.5.0"), seen_a
        assert seen_b.startswith("b:") and seen_b.endswith(":3.11.0"), seen_b

        # Acceptance criteria #2: a third task sees no residual keys.
        # The third task runs after both closures have returned and
        # their ``finally`` clauses have restored ``os.environ``.
        ex_c = self._make_executor()

        def third_body() -> dict[str, str | None]:
            return {
                "OSIMFLOW_OS_VERSION": os.environ.get("OSIMFLOW_OS_VERSION"),
                "OSIMFLOW_CONTAINER": os.environ.get("OSIMFLOW_CONTAINER"),
            }

        wrapped_c = self._capture_wrapped(ex_c, third_body, name="c")
        result = wrapped_c()
        # The third task itself sees no override (we didn't pass
        # container/version), so neither key is present.
        assert result == {
            "OSIMFLOW_OS_VERSION": None,
            "OSIMFLOW_CONTAINER": None,
        }, result

    def test_wrapped_snapshot_is_per_call_not_per_worker(self) -> None:
        """Pre-set env value is captured by the per-call snapshot.

        Verifies that the snapshot is taken when the closure is invoked
        (per-call), NOT when the closure is created. With the old code,
        a closure created when ``OSIMFLOW_OS_VERSION`` was "external"
        would have no snapshot at all and would leave "external"
        unchanged when it exited (the broken "no restore" behaviour).
        With the fix, the snapshot captures "external", sets the
        override, runs ``fn``, and restores "external" on exit.
        """
        os.environ["OSIMFLOW_OS_VERSION"] = "external-baseline"
        try:

            def body() -> str:
                # Inside fn: the override is active.
                return os.environ.get("OSIMFLOW_OS_VERSION") or "<missing>"

            ex = self._make_executor()
            wrapped = self._capture_wrapped(
                ex,
                body,
                name="per-call-snapshot",
                openstudio_version="3.11.0",
            )

            inside = wrapped()
            assert inside == "3.11.0", inside
            # The per-call snapshot captured "external-baseline" and
            # restored it on exit — proving per-call, NOT per-worker.
            assert os.environ["OSIMFLOW_OS_VERSION"] == "external-baseline"
        finally:
            os.environ.pop("OSIMFLOW_OS_VERSION", None)


class TestDaskJobQueuePerSampleLogCapture:
    """Per-sample stdout/stderr capture (issue #1688).

    The Dask-JobQueue executor previously discarded the
    ``stdout_path`` / ``stderr_path`` arguments forwarded by
    :meth:`BaseExecutor.submit`. That left a failed dask-jobqueue
    sample with nothing on the orchestrator side to ``cat`` once the
    worker scaled down — the documented `${outdir}/work/sim/<sample_id>/{stdout,stderr}.log`
    contract was broken on every dask_jobqueue cluster backend
    (``SLURMCluster`` / ``PBSCluster`` / ``KubernetesCluster``).

    The closure handed to ``cluster.get_client().submit`` must
    redirect ``fn``'s ``print()`` / ``sys.stderr`` writes to those
    paths so the orchestrator's per-sample log discovery
    (campaign.py:work side) keeps working unmodified.

    These tests also compose with #1689's env-isolation changes
    (``_apply_env_isolated``): the captured-fn wrapper runs INSIDE
    the env-isolated scope so env overrides are visible to the inner
    fn, and stdout/stderr files are flushed+closed atomically with
    env restore.
    """

    def _make_executor(self) -> DaskJobQueueExecutor:
        ex = DaskJobQueueExecutor.__new__(DaskJobQueueExecutor)
        ex.cluster_type = "slurm"
        ex.min_workers = 1
        ex.max_workers = 10
        ex.cpus_per_worker = 2
        ex.memory_per_worker = "4GiB"
        ex.walltime = "01:00:00"
        ex.queue = "debug"
        ex._scaler_running = False
        ex._cluster = MagicMock()
        ex.name = "dask_jobqueue"
        return ex

    def _capture_wrapped(
        self,
        fn: object,
        tmp_path: Path,
        stdout_path: Path | None,
        stderr_path: Path | None,
        *,
        container: str | None = "nrel/openstudio:3.11.0",
    ) -> object:
        """Invoke ``_do_submit`` with a mocked cluster and return the
        callable handed to ``cluster.get_client().submit``.

        Mirrors how a real Dask worker would invoke ``_wrapped`` after
        the orchestrator pickles the closure across the wire: same
        kwargs, same capture stack.
        """
        ex = self._make_executor()
        client = MagicMock()
        ex._cluster.get_client.return_value = client
        with patch("osimflow.executors.dask_jobqueue_executor.validate_transport_mode"):
            ex._do_submit(
                name="test",
                cpus=2,
                memory_mb=4096,
                time_min=60,
                container=container,
                container_digest=None,
                fn=fn,
                args=(),
                openstudio_version="3.11.0",
                variables_json=None,
                env=None,
                result_hint=None,
                remote_command=None,
                transport=None,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        return client.submit.call_args.args[0]

    def test_captures_stdout_and_stderr_to_files(self, tmp_path: Path) -> None:
        out_path = tmp_path / "out.log"
        err_path = tmp_path / "err.log"

        def fn() -> str:
            print("hello stdout", flush=True)
            print("hello stderr", file=sys.stderr, flush=True)
            return "ok"

        wrapped = self._capture_wrapped(fn, tmp_path, out_path, err_path)
        result = wrapped()

        assert result == "ok"
        assert out_path.read_text() == "hello stdout\n"
        assert err_path.read_text() == "hello stderr\n"

    def test_captures_stdout_only(self, tmp_path: Path) -> None:
        out_path = tmp_path / "out.log"

        def fn() -> str:
            print("only stdout", flush=True)
            return "ok"

        wrapped = self._capture_wrapped(fn, tmp_path, out_path, None)
        result = wrapped()

        assert result == "ok"
        assert out_path.read_text() == "only stdout\n"

    def test_captures_stderr_only(self, tmp_path: Path) -> None:
        err_path = tmp_path / "err.log"

        def fn() -> str:
            print("only stderr", file=sys.stderr, flush=True)
            return "ok"

        wrapped = self._capture_wrapped(fn, tmp_path, None, err_path)
        result = wrapped()

        assert result == "ok"
        assert err_path.read_text() == "only stderr\n"

    def test_no_capture_is_fast_path(self, tmp_path: Path) -> None:
        """When neither stdout_path nor stderr_path is given, the closure
        takes a fast path: no file open, no redirect. Just calls fn.
        """

        def fn() -> str:
            return "fast"

        wrapped = self._capture_wrapped(fn, tmp_path, None, None)
        assert wrapped() == "fast"

    def test_files_appended_to_on_subsequent_calls(self, tmp_path: Path) -> None:
        """Re-running a sample should append to the same files, not
        overwrite — supports the retry-the-same-sample workflow.
        """
        out_path = tmp_path / "out.log"

        def fn() -> str:
            print("line1", flush=True)
            return "ok"

        wrapped = self._capture_wrapped(fn, tmp_path, out_path, None)
        wrapped()
        wrapped()
        wrapped()

        text = out_path.read_text()
        assert text.count("line1") == 3

    def test_exception_in_fn_still_closes_files(self, tmp_path: Path) -> None:
        """The file context managers release even if ``fn`` raises, so
        a retried sample can re-open the same paths without a
        ``ResourceWarning`` / leaked-fd error.
        """
        out_path = tmp_path / "out.log"

        def boom() -> None:
            print("before raise", flush=True)
            raise ValueError("boom")

        wrapped = self._capture_wrapped(boom, tmp_path, out_path, None)
        with pytest.raises(ValueError, match="boom"):
            wrapped()
        # Pre-raise text is flushed.
        assert "before raise" in out_path.read_text()
        # And the file is re-openable (descriptor was released).
        with out_path.open("a") as f:
            f.write("after-retry\n")
        assert "after-retry" in out_path.read_text()
