"""Unit tests for ``osimflow.taskqueue`` (issue #1452).

The module was previously blanket-omitted from coverage; removing the
omission requires the in-process surface to actually be exercised.
``DaskTaskQueue`` is tested with a fake client (monkeypatched
``_ensure_client``) so the suite stays hermetic — ``dask`` is an
optional extra and is NOT imported at test time.
"""

import concurrent.futures
import dataclasses

import pytest

from osimflow.taskqueue import (
    ConsumerQueue,
    DaskTaskQueue,
    NoOpTaskQueue,
    ProducerQueue,
    TaskHandle,
    TaskQueueStatus,
    build_task_queue,
)


def _future_with_result(result: object = None) -> concurrent.futures.Future:
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.set_result(result)
    return fut


# ---------------------------------------------------------------------------
# TaskQueueStatus / TaskHandle
# ---------------------------------------------------------------------------


def test_task_queue_status_members() -> None:
    assert {s.name for s in TaskQueueStatus} == {
        "PENDING",
        "RUNNING",
        "SUCCESS",
        "FAILED",
        "RETRYING",
    }


def test_task_handle_done_only_on_terminal_states() -> None:
    for status, expected in [
        (TaskQueueStatus.PENDING, False),
        (TaskQueueStatus.RUNNING, False),
        (TaskQueueStatus.RETRYING, False),
        (TaskQueueStatus.SUCCESS, True),
        (TaskQueueStatus.FAILED, True),
    ]:
        handle = TaskHandle(task_id="t1", status=status)
        assert handle.done() is expected


def test_task_handle_result_without_future_raises() -> None:
    handle = TaskHandle(task_id="t2")
    with pytest.raises(RuntimeError, match="no backing future"):
        handle.result()


def test_task_handle_result_returns_future_value() -> None:
    handle = TaskHandle(task_id="t3", _future=_future_with_result(42))
    assert handle.result(timeout=1) == 42


def test_task_handle_result_raises_task_exception() -> None:
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.set_exception(ValueError("boom"))
    handle = TaskHandle(task_id="t4", _future=fut)
    with pytest.raises(ValueError, match="boom"):
        handle.result()


def test_task_handle_retry_requires_failed_state() -> None:
    handle = TaskHandle(task_id="t5", status=TaskQueueStatus.RUNNING)
    with pytest.raises(RuntimeError, match="cannot be retried"):
        handle.retry()


def test_task_handle_retry_on_failed_is_accepted() -> None:
    handle = TaskHandle(task_id="t6", status=TaskQueueStatus.FAILED)
    handle.retry()


def test_task_handle_defaults() -> None:
    handle = TaskHandle(task_id="t7")
    assert handle.status is TaskQueueStatus.PENDING
    assert handle._future is None
    assert handle.worker_id is None
    assert dataclasses.is_dataclass(handle)


# ---------------------------------------------------------------------------
# NoOpTaskQueue
# ---------------------------------------------------------------------------


def test_noop_queue_implements_both_interfaces() -> None:
    queue = NoOpTaskQueue()
    assert isinstance(queue, ProducerQueue)
    assert isinstance(queue, ConsumerQueue)
    assert queue.name == "none"


def test_noop_enqueue_runs_synchronously() -> None:
    queue = NoOpTaskQueue()
    handle = queue.enqueue(lambda a, b: a + b, 2, b=3)
    assert handle.status is TaskQueueStatus.SUCCESS
    assert handle.done()
    assert handle.worker_id == "main"
    assert handle.result() == 5


def test_noop_enqueue_captures_exception_in_future() -> None:
    queue = NoOpTaskQueue()

    def boom() -> None:
        raise RuntimeError("kaput")

    handle = queue.enqueue(boom)
    with pytest.raises(RuntimeError, match="kaput"):
        handle.result()


def test_noop_get_result_raises_for_failing_task() -> None:
    queue = NoOpTaskQueue()

    def boom() -> None:
        raise RuntimeError("kaput")

    handle = queue.enqueue(boom)
    with pytest.raises(RuntimeError, match="kaput"):
        queue.get_result(handle)


def test_noop_submit_is_alias_for_enqueue() -> None:
    queue = NoOpTaskQueue()
    handle = queue.submit(str.upper, "hi")
    assert handle.result() == "HI"


def test_noop_get_result_without_future_raises() -> None:
    queue = NoOpTaskQueue()
    with pytest.raises(RuntimeError, match="no backing future"):
        queue.get_result(TaskHandle(task_id="t8"))


def test_noop_retry_unsupported() -> None:
    queue = NoOpTaskQueue()
    with pytest.raises(RuntimeError, match="retry is not supported"):
        queue.retry(TaskHandle(task_id="t9", status=TaskQueueStatus.FAILED))


def test_noop_shutdown_is_noop() -> None:
    NoOpTaskQueue().shutdown()


# ---------------------------------------------------------------------------
# DaskTaskQueue (fake client — dask itself is an optional extra)
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False
        self.submitted: list = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return _future_with_result(fn(*args, **kwargs))

    def close(self) -> None:
        self.closed = True


class _FakeCluster:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_dask_queue_init_state() -> None:
    queue = DaskTaskQueue(scheduler_address="tcp://sched:8786", max_retries=5)
    assert queue.scheduler_address == "tcp://sched:8786"
    assert queue.max_retries == 5
    assert queue._client is None
    assert queue._embedded_cluster is None
    assert queue.name == "dask"


def test_dask_ensure_client_caches_existing_client() -> None:
    queue = DaskTaskQueue()
    fake = _FakeClient()
    queue._client = fake
    assert queue._ensure_client() is fake


def test_dask_ensure_client_imports_dask_lazily() -> None:
    queue = DaskTaskQueue()
    with pytest.raises(ImportError):
        queue._ensure_client()


def test_dask_submit_via_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = DaskTaskQueue()
    fake = _FakeClient()
    monkeypatch.setattr(queue, "_ensure_client", lambda: fake)
    handle = queue.submit(lambda x: x * 2, 21)
    assert handle.task_id.startswith("dask-task-")
    assert handle.status is TaskQueueStatus.PENDING
    assert handle.result() == 42
    assert fake.submitted


def test_dask_enqueue_is_alias_for_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = DaskTaskQueue()
    fake = _FakeClient()
    monkeypatch.setattr(queue, "_ensure_client", lambda: fake)
    handle = queue.enqueue(lambda: "done")
    assert handle.result() == "done"


def test_dask_get_result_without_future_raises() -> None:
    queue = DaskTaskQueue()
    with pytest.raises(RuntimeError, match="no backing future"):
        queue.get_result(TaskHandle(task_id="t10"))


def test_dask_get_result_returns_future_value() -> None:
    queue = DaskTaskQueue()
    handle = TaskHandle(task_id="t13", _future=_future_with_result("ok"))
    assert queue.get_result(handle) == "ok"


def test_dask_retry_requires_failed_state() -> None:
    queue = DaskTaskQueue()
    with pytest.raises(RuntimeError, match="cannot be retried"):
        queue.retry(TaskHandle(task_id="t11", status=TaskQueueStatus.PENDING))


def test_dask_retry_on_failed_raises_not_implemented() -> None:
    queue = DaskTaskQueue()
    with pytest.raises(NotImplementedError, match="re-submitting"):
        queue.retry(TaskHandle(task_id="t12", status=TaskQueueStatus.FAILED))


def test_dask_shutdown_closes_client_and_cluster() -> None:
    queue = DaskTaskQueue()
    fake_client = _FakeClient()
    fake_cluster = _FakeCluster()
    queue._client = fake_client
    queue._embedded_cluster = fake_cluster
    queue.shutdown()
    assert fake_client.closed
    assert fake_cluster.closed
    assert queue._client is None
    assert queue._embedded_cluster is None


def test_dask_shutdown_without_resources_is_noop() -> None:
    DaskTaskQueue().shutdown()


# ---------------------------------------------------------------------------
# build_task_queue factory
# ---------------------------------------------------------------------------


def test_build_task_queue_none_returns_noop() -> None:
    assert isinstance(build_task_queue("none"), NoOpTaskQueue)


def test_build_task_queue_dask_returns_dask_queue() -> None:
    queue = build_task_queue("dask", "tcp://sched:8786", max_retries=7)
    assert isinstance(queue, DaskTaskQueue)
    assert queue.scheduler_address == "tcp://sched:8786"
    assert queue.max_retries == 7


def test_build_task_queue_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="unknown task queue backend"):
        build_task_queue("celery")
