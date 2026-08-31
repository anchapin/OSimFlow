"""Unit tests for ``osimflow.executors.LocalExecutor`` (issue #1406).

Regression for issue #1406: the previous ``_with_env`` implementation
mutated the process-global ``os.environ`` directly
(``os.environ.clear()`` / ``os.environ.update(...)``) inside the
``ThreadPoolExecutor`` task. With ``--max-workers > 1`` and per-submit
``env=``, concurrent workers could observe each other's mid-call
state — one task's ``finally: clear()/update()`` could land while
another task was mid-``fn(*args)`` reading ``os.environ``.

The fix replaces the manual save/restore with
``unittest.mock.patch.dict`` (a stdlib context manager that captures
the dict state on entry and restores it on exit, even when the body
raises). These tests verify the SEMANTIC contract:

* A task submitted with ``env=`` sees those exact values inside its
  body.
* After a task completes (success or failure), the previous
  ``os.environ`` state is restored — no per-call key leaks.
* Recursive / nested ``patch.dict`` scenarios work correctly.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from osimflow.executors import LocalExecutor

N_TASKS = 10
ENV_PREFIX = "OSIMFLOW_TEST_THREADED_ENV_"


def _env_key(i: int) -> str:
    return f"{ENV_PREFIX}{i}"


def _env_value(i: int) -> str:
    return f"value-{i}"


def _snapshot_test_keys() -> set[str]:
    """Return the test-prefixed keys currently visible in os.environ."""
    return {k for k in os.environ if k.startswith(ENV_PREFIX)}


@pytest.fixture
def threaded_executor() -> LocalExecutor:
    """``LocalExecutor`` with multiple workers to exercise fan-out."""
    return LocalExecutor(max_workers=N_TASKS)


@pytest.fixture(autouse=True)
def _isolate_environ() -> Any:
    """Snapshot test-prefixed ``os.environ`` keys before/after each test."""
    prefix_keys_before = _snapshot_test_keys()
    try:
        yield
    finally:
        leaked = _snapshot_test_keys() - prefix_keys_before
        assert not leaked, f"test leaked env keys: {sorted(leaked)}"


class TestLocalExecutorWithEnv:
    """LocalExecutor.submit(env=...) correctness."""

    def test_single_task_sees_its_env_value(
        self, threaded_executor: LocalExecutor
    ) -> None:
        """The simplest case: a task submitted with ``env=`` observes it."""
        handle = threaded_executor.submit(
            lambda: os.environ.get(_env_key(0)),
            env={_env_key(0): _env_value(0)},
            name="single-env",
        )
        assert handle.result(timeout=5) == _env_value(0)
        threaded_executor.shutdown()

    def test_single_task_env_restored_after_completion(
        self, threaded_executor: LocalExecutor
    ) -> None:
        """After a task completes, the per-call env keys must be gone."""
        baseline = _snapshot_test_keys()
        handle = threaded_executor.submit(
            lambda: None,
            env={_env_key(0): _env_value(0)},
            name="env-restored",
        )
        handle.result(timeout=5)
        leaked = _snapshot_test_keys() - baseline
        assert not leaked, f"keys leaked: {sorted(leaked)}"
        threaded_executor.shutdown()

    def test_env_restored_when_task_raises(
        self, threaded_executor: LocalExecutor
    ) -> None:
        """Env is restored even when the task body raises."""
        baseline = _snapshot_test_keys()
        sentinel = "OSIMFLOW_TEST_INSIDE_RAISE"

        def _raise() -> None:
            raise RuntimeError("boom")

        handle = threaded_executor.submit(
            _raise,
            env={sentinel: "inside"},
            name="env-on-exception",
        )
        with pytest.raises(RuntimeError, match="boom"):
            handle.result(timeout=5)
        assert sentinel not in os.environ, "sentinel leaked after raising task"
        leaked = _snapshot_test_keys() - baseline
        assert not leaked, f"keys leaked: {sorted(leaked)}"
        threaded_executor.shutdown()

    def test_pre_existing_env_var_survives_through_task(
        self, threaded_executor: LocalExecutor
    ) -> None:
        """An env var set outside the executor is still visible after a task.

        Verifies that ``patch.dict`` correctly restores the entire
        pre-patch state — not just our additions.
        """
        baseline = _snapshot_test_keys()
        sentinel = "OSIMFLOW_TEST_INHERIT"
        previous = os.environ.get(sentinel)
        os.environ[sentinel] = "before-task"
        try:
            handle = threaded_executor.submit(
                lambda: os.environ.get(sentinel),
                env={_env_key(0): _env_value(0)},
                name="inherit-env",
            )
            assert handle.result(timeout=5) == "before-task"
        finally:
            if previous is None:
                os.environ.pop(sentinel, None)
            else:
                os.environ[sentinel] = previous
            threaded_executor.shutdown()

        assert os.environ.get(sentinel) == previous, (
            "pre-existing env var not restored to its original value"
        )
        leaked = _snapshot_test_keys() - baseline
        assert not leaked, f"keys leaked: {sorted(leaked)}"

    def test_concurrent_submissions_each_complete_with_own_env(
        self, threaded_executor: LocalExecutor
    ) -> None:
        """10 tasks submitted concurrently: each task's env value is returned.

        Each task body returns its OWN expected env value. The test
        verifies the SEMANTIC contract (the per-task ``env=`` is active
        during the task). The issue (#1406) was that concurrent tasks
        could observe each other's mid-call state — submitting 10 tasks
        with distinct env values and asserting each one's body runs
        against its own captured env is the user-visible regression
        test.
        """
        try:
            handles = [
                threaded_executor.submit(
                    lambda i=i: os.environ.get(_env_key(i)),
                    env={_env_key(i): _env_value(i)},
                    name=f"threaded-env-{i}",
                )
                for i in range(N_TASKS)
            ]
            results = [h.result(timeout=10) for h in handles]
        finally:
            threaded_executor.shutdown()

        for i, result in enumerate(results):
            assert result == _env_value(i), (
                f"task {i} expected {_env_value(i)!r} but saw {result!r}"
            )

    def test_no_env_leak_after_concurrent_submissions(
        self, threaded_executor: LocalExecutor
    ) -> None:
        """After 10 concurrent task submissions, no per-call env keys remain."""
        baseline = _snapshot_test_keys()
        try:
            handles = [
                threaded_executor.submit(
                    lambda i=i: (_env_key(i), _env_value(i)),
                    env={_env_key(i): _env_value(i)},
                    name=f"threaded-leak-{i}",
                )
                for i in range(N_TASKS)
            ]
            seen = [h.result(timeout=10) for h in handles]
        finally:
            threaded_executor.shutdown()

        assert sorted(seen) == sorted(
            (_env_key(i), _env_value(i)) for i in range(N_TASKS)
        )
        leaked = _snapshot_test_keys() - baseline
        assert not leaked, f"keys leaked into os.environ: {sorted(leaked)}"

    def test_nested_with_env_does_not_deadlock(
        self, threaded_executor: LocalExecutor
    ) -> None:
        """Recursive-safe: a task that opens a nested ``patch.dict`` works.

        ``patch.dict`` composes — nested contexts each capture and
        restore independently. The fix relies on this property to
        guarantee that a task body which itself mutates ``os.environ``
        (e.g. an OpenStudio measure invoking a helper that wraps
        ``patch.dict``) sees consistent behaviour.
        """
        from unittest.mock import patch

        def _body() -> dict[str, str]:
            with patch.dict(os.environ, {"OUTER": "outer"}, clear=False):
                with patch.dict(os.environ, {"INNER": "inner"}, clear=False):
                    inner = os.environ.get("INNER")
                    outer = os.environ.get("OUTER")
                # Inner exited — OUTER should still be visible.
                outer_after_inner = os.environ.get("OUTER")
            # Both exited — neither should be visible.
            assert "OUTER" not in os.environ
            assert "INNER" not in os.environ
            return {
                "inner": inner or "",
                "outer": outer or "",
                "outer_after_inner": outer_after_inner or "",
            }

        handle = threaded_executor.submit(_body, env={}, name="nested-patch")
        result = handle.result(timeout=5)
        assert result == {
            "inner": "inner",
            "outer": "outer",
            "outer_after_inner": "outer",
        }
        threaded_executor.shutdown()
