"""Mock-based NomadExecutor tests for reliable CI coverage (issue #195).

These tests validate the ``NomadExecutor`` wiring with mocked HTTP
responses — no Docker or Nomad cluster required. They complement the
Docker-based E2E tests in ``test_single_node.py`` and the stub test
in ``tests/integration/test_nomad_executor_stub.py``.

The key difference from ``test_nomad_executor_stub.py`` is that this
module is located inside the ``nomad_e2e/`` directory and is
explicitly run by the ``nomad-single-node`` CI job to guarantee at
least some Nomad coverage even when Docker is unavailable.

Tests cover:

  * ``NomadClient.submit_job`` — verifies POST /v1/jobs payload shape.
  * ``NomadClient.resolve_allocation`` — verifies eval → alloc lookup.
  * ``NomadClient.wait_for_allocation`` — verifies polling to completion.
  * Full ``NomadExecutor.submit()`` cycle — end-to-end handle lifecycle.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

from osimflow.executors import NomadExecutor


# ---------------------------------------------------------------------------
# Helper: mock urllib.request.urlopen with permissive responses
# ---------------------------------------------------------------------------
@contextmanager
def mocked_nomad_transport() -> Iterator[MagicMock]:
    """Context manager: patch ``urllib.request.urlopen`` so that:

    * ``POST /v1/jobs`` returns a unique ``JobID`` + ``EvalID``.
    * ``GET /v1/evaluation/<id>/allocations`` returns a stub allocation.
    * ``GET /v1/allocation/<id>`` returns ``ClientStatus: complete``.
    * ``GET /v1/job/<id>/allocations`` returns a stub allocation.
    """
    fake_urlopen = MagicMock()
    submit_calls: list[dict[str, Any]] = []
    alloc_calls: list[dict[str, Any]] = []
    call_counter = 0

    def fake_urlopen_fn(request: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_counter
        call_counter += 1
        method = request.get_method()
        url = request.full_url

        if method == "POST" and "/v1/jobs" in url:
            payload: dict[str, Any] = json.loads(request.data.decode("utf-8"))
            submit_calls.append(payload)
            idx = len(submit_calls)
            result = {
                "JobID": f"osimflow/mock-job-{idx}",
                "EvalID": f"eval-mock-{idx}",
                "Index": idx,
            }
            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        if method == "GET" and "/v1/evaluation/" in url and "/allocations" in url:
            idx = call_counter
            alloc_calls.append({"method": method, "url": url})
            result = [{"ID": f"alloc-eval-{idx}", "ClientStatus": "complete", "JobID": "osimflow/mock-job-1"}]
            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        if method == "GET" and "/v1/job/" in url and "/allocations" in url:
            alloc_calls.append({"method": method, "url": url})
            result = [{"ID": "alloc-job-mock", "ClientStatus": "complete", "JobID": "osimflow/mock-job-1"}]
            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        if method == "GET" and "/v1/allocation/" in url:
            alloc_calls.append({"method": method, "url": url})
            result = {
                "ID": f"alloc-mock-{len(alloc_calls)}",
                "ClientStatus": "complete",
                "JobID": "osimflow/mock-job-1",
            }
            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        # Fallback: empty JSON response.
        resp = MagicMock()
        resp.read.return_value = b"{}"
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    fake_urlopen.side_effect = fake_urlopen_fn
    fake_urlopen.submit_calls = submit_calls
    fake_urlopen.alloc_calls = alloc_calls

    with patch("urllib.request.urlopen", side_effect=fake_urlopen_fn):
        yield fake_urlopen


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_nomad_client_submit_job_payload_shape() -> None:
    """Verify ``submit_job`` sends a well-formed POST /v1/jobs request."""
    with mocked_nomad_transport() as transport:
        executor = NomadExecutor(
            address="http://mock-nomad:4646",
            datacentre="dc1",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )
        spec: dict[str, Any] = {
            "Job": {
                "ID": "test-shape",
                "Name": "test-shape",
                "Type": "batch",
                "Datacenters": ["dc1"],
                "TaskGroups": [
                    {
                        "Name": "group",
                        "Tasks": [
                            {
                                "Name": "task",
                                "Driver": "docker",
                                "Config": {"image": "alpine:latest"},
                                "Resources": {"CPU": 100, "MemoryMB": 64},
                            }
                        ],
                    }
                ],
            }
        }
        response = executor._client.submit_job(spec)  # noqa: SLF001
        assert "JobID" in response
        assert "EvalID" in response
        executor.shutdown()

    # Verify the submitted payload preserved the job spec shape.
    assert len(transport.submit_calls) == 1
    submitted = transport.submit_calls[0]
    assert submitted["Job"]["ID"] == "test-shape"
    assert submitted["Job"]["Type"] == "batch"
    task = submitted["Job"]["TaskGroups"][0]["Tasks"][0]
    assert task["Driver"] == "docker"
    assert task["Config"]["image"] == "alpine:latest"


def test_nomad_client_resolve_allocation() -> None:
    """Verify ``resolve_allocation`` can look up an alloc from an eval."""
    with mocked_nomad_transport():
        executor = NomadExecutor(
            address="http://mock-nomad:4646",
            datacentre="dc1",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )
        alloc_id = executor._client.resolve_allocation(  # noqa: SLF001
            eval_id="eval-mock-1",
            job_id="osimflow/mock-job-1",
        )
        assert alloc_id, f"expected an allocation ID, got {alloc_id!r}"
        executor.shutdown()


def test_nomad_client_wait_for_allocation_completes() -> None:
    """Verify ``_wait_for_terminal`` returns a complete allocation."""
    with mocked_nomad_transport():
        executor = NomadExecutor(
            address="http://mock-nomad:4646",
            datacentre="dc1",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )
        alloc = executor._wait_for_terminal("alloc-mock-1")  # noqa: SLF001
        assert alloc["ClientStatus"] == "complete"
        executor.shutdown()


def test_nomad_executor_submit_returns_handle() -> None:
    """Verify ``NomadExecutor.submit()`` returns a handle that resolves.

    The real ``NomadExecutor`` returns ``None`` from ``result()`` on
    success (the work runs on a remote Nomad node, not locally), so
    we assert the handle lifecycle works end-to-end without error.
    """
    with mocked_nomad_transport():
        executor = NomadExecutor(
            address="http://mock-nomad:4646",
            datacentre="dc1",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )

        def dummy_work(x: int) -> int:
            return x * 2

        handle = executor.submit(
            dummy_work,
            21,
            name="test-handle",
            cpus=1,
            memory_mb=256,
            time_min=10,
            container="alpine:latest",
        )
        assert handle is not None
        assert hasattr(handle, "job_id")
        assert hasattr(handle, "result")
        assert hasattr(handle, "done")
        # The NomadExecutor handle returns None on success — the real
        # work runs on the remote Nomad node.
        result = handle.result(timeout=10)
        assert result is None
        assert handle.done()
        executor.shutdown()


def test_nomad_executor_submit_includes_container_image() -> None:
    """Verify the submitted job spec carries the container image tag."""
    with mocked_nomad_transport() as transport:
        executor = NomadExecutor(
            address="http://mock-nomad:4646",
            datacentre="dc1",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )

        def noop() -> None:
            pass

        executor.submit(
            noop,
            name="container-check",
            cpus=1,
            memory_mb=128,
            time_min=5,
            container="nrel/openstudio:3.11.0",
        )
        executor.shutdown()

    assert len(transport.submit_calls) >= 1
    spec = transport.submit_calls[0]
    task = spec["Job"]["TaskGroups"][0]["Tasks"][0]
    assert "3.11.0" in task["Config"]["image"], (
        f"Expected OpenStudio version in image: {task['Config']['image']!r}"
    )


def test_nomad_executor_submit_multiple_tasks() -> None:
    """Verify multiple submits produce unique job specs."""
    with mocked_nomad_transport() as transport:
        executor = NomadExecutor(
            address="http://mock-nomad:4646",
            datacentre="dc1",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )

        def noop() -> None:
            pass

        for i in range(5):
            executor.submit(
                noop,
                name=f"multi-{i}",
                cpus=1,
                memory_mb=64,
                time_min=1,
                container="alpine:latest",
            )

        executor.shutdown()

    assert len(transport.submit_calls) == 5
    # The job Name is built by _slugify_job_name(f"osimflow-{name}").
    job_names = [spec["Job"]["Name"] for spec in transport.submit_calls]
    assert len(set(job_names)) == 5, f"expected 5 unique job names, got {job_names}"
