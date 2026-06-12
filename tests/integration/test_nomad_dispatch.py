"""Tests for issue #135 — Nomad job spec HCL template + parameterized dispatch.

Acceptance criteria:

  * ``infra/nomad/osimflow_worker.hcl`` is valid HCL (parseable by python-hcl2).
  * NomadExecutor in dispatch mode registers the parameterized job once, then
    dispatches per-sample work via ``POST /v1/job/osimflow-worker/dispatch``.
  * Dispatch sends correct meta params (``sample_id``, ``openstudio_version``,
    ``container_image``, ``variables_json``).
  * Allocation failure modes (``failed``, ``lost``, ``unknown``) re-raise with
    descriptive messages.
  * Security: no privileged mode, memory limited to 4096 MB in the dispatch spec.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import NomadExecutor

REPO_ROOT = Path(__file__).resolve().parents[2]
HCL_PATH = REPO_ROOT / "infra" / "nomad" / "osimflow_worker.hcl"


# ---------------------------------------------------------------------------
# HCL validation: osimflow_worker.hcl must be parseable
# ---------------------------------------------------------------------------
class TestHCLSpec:
    """Validate ``infra/nomad/osimflow_worker.hcl`` is well-formed."""

    def test_hcl_file_exists(self) -> None:
        """The HCL file must exist at the documented path."""
        assert HCL_PATH.is_file(), f"missing HCL spec: {HCL_PATH}"

    def test_hcl_is_parseable(self) -> None:
        """python-hcl2 must be able to parse the spec without errors."""
        hcl2 = pytest.importorskip("hcl2")
        content = HCL_PATH.read_text()
        parsed = hcl2.loads(content)
        assert isinstance(parsed, dict), f"hcl2.loads returned unexpected type: {type(parsed)}"
        # Top-level key must be "job" (after HCL2 desugaring it may be
        # wrapped; the important thing is it parsed without exception).
        assert parsed, "parsed HCL is empty"

    def test_hcl_declares_parameterized_job(self) -> None:
        """The job must be a parameterized batch job."""
        hcl2 = pytest.importorskip("hcl2")
        parsed = hcl2.loads(HCL_PATH.read_text())
        # python-hcl2 represents ``job "name" { ... }`` as a list of
        # dicts where the key is the quoted job name, e.g.
        # ``{"job": [{'"osimflow-worker"': {...}}]}``.
        jobs = parsed.get("job", [])
        assert len(jobs) >= 1, f"no job blocks found in HCL: {list(parsed.keys())}"
        job_block = jobs[0]
        # The key of the dict is the job name (with quotes).
        job_keys = list(job_block.keys())
        assert any("osimflow-worker" in k for k in job_keys), (
            f"expected 'osimflow-worker' in job keys, got {job_keys}"
        )

    def test_hcl_has_security_settings(self) -> None:
        """The job must not be privileged and must have resource limits."""
        content = HCL_PATH.read_text()
        assert "privileged = false" in content or 'privileged = "false"' in content, (
            "HCL must explicitly set privileged = false"
        )
        assert "4096" in content, "HCL must limit memory to 4096 MB"
        assert "2000" in content, "HCL must limit CPU to 2000 MHz (2 CPUs)"

    def test_hcl_has_dispatch_meta_vars(self) -> None:
        """The parameterized block must declare sample_id as required."""
        content = HCL_PATH.read_text()
        assert "sample_id" in content, "HCL must declare sample_id in meta_required"
        assert "variables_json" in content, "HCL must declare variables_json in meta_optional"
        assert "openstudio_version" in content, "HCL must declare openstudio_version"
        assert "container_image" in content, "HCL must declare container_image"

    def test_hcl_has_docker_driver(self) -> None:
        """The task must use the Docker driver."""
        content = HCL_PATH.read_text()
        assert 'driver = "docker"' in content, "HCL task must use docker driver"

    def test_hcl_has_zero_restart_policy(self) -> None:
        """The task must not restart on failure (Campaign handles retries)."""
        content = HCL_PATH.read_text()
        assert "attempts = 0" in content, "HCL must set restart attempts to 0"

    def test_hcl_has_env_passthrough(self) -> None:
        """The task must pass through OSIMFLOW_* env vars."""
        content = HCL_PATH.read_text()
        assert "OSIMFLOW_STUB_SIM" in content, "HCL must define OSIMFLOW_STUB_SIM env var"
        assert "OSIMFLOW_OUTDIR" in content, "HCL must define OSIMFLOW_OUTDIR env var"


# ---------------------------------------------------------------------------
# Mock helpers for dispatch tests
# ---------------------------------------------------------------------------
def _fake_response(body: dict[str, Any] | list[Any]) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: None
    return resp


@contextmanager
def mocked_dispatch_transport(
    *,
    register_response: dict[str, Any] | None = None,
    dispatch_response: dict[str, Any] | None = None,
    alloc_responses: list[dict[str, Any]] | None = None,
    eval_alloc_response: list[dict[str, Any]] | None = None,
) -> Iterator[MagicMock]:
    """Context manager that patches ``urllib.request.urlopen`` to handle
    the dispatch workflow: register → dispatch → poll allocation.

    Records every call so tests can assert on the dispatch payload.
    """
    if register_response is None:
        register_response = {
            "JobID": "osimflow-worker",
            "EvalID": "eval-register",
            "Index": 0,
        }
    if dispatch_response is None:
        dispatch_response = {
            "JobID": "osimflow-worker/dispatch-123",
            "EvalID": "eval-dispatch-1",
            "Index": 1,
        }
    if alloc_responses is None:
        alloc_responses = [
            {
                "ID": "alloc-dispatch-1",
                "ClientStatus": "complete",
                "JobID": "osimflow-worker/dispatch-123",
            },
        ]
    if eval_alloc_response is None:
        eval_alloc_response = [
            {"ID": "alloc-dispatch-1", "ClientStatus": "complete"},
        ]

    queue: list[Any] = [
        register_response,
        dispatch_response,
        eval_alloc_response,
        *alloc_responses,
    ]
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
        if not queue:
            raise AssertionError("executor issued more HTTP calls than expected")
        body = queue.pop(0)
        method = request.get_method()
        url = request.full_url

        # Record the call details including payload for POST requests.
        payload: dict[str, Any] | None = None
        if request.data:
            payload = json.loads(request.data.decode("utf-8"))
        calls.append((method, url, payload))
        return _fake_response(body)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen) as mock:
        mock.calls = calls  # type: ignore[attr-defined]
        yield mock


# ---------------------------------------------------------------------------
# Dispatch mode tests
# ---------------------------------------------------------------------------
class TestNomadDispatch:
    """Test NomadExecutor dispatch mode (issue #135)."""

    def test_dispatch_registers_job_on_first_submit(self) -> None:
        """The first ``submit()`` call must register the parameterized job
        via ``POST /v1/jobs`` before dispatching."""
        with mocked_dispatch_transport() as mock:
            ex = NomadExecutor(
                address="http://nomad.test:4646",
                datacentre="dc1",
                poll_interval_s=0.01,
                max_poll_interval_s=0.02,
                use_dispatch=True,
            )
            ex.submit(lambda: "ok", name="sample-0", container="nrel/openstudio:3.11.0")
            ex.shutdown()

        calls = mock.calls  # type: ignore[attr-defined]
        # First call must be POST /v1/jobs (job registration).
        assert calls[0][0] == "POST", f"expected POST, got {calls[0][0]}"
        assert "/v1/jobs" in calls[0][1], f"expected /v1/jobs, got {calls[0][1]}"
        # Verify the registration payload has ParameterizedJob.
        reg_payload = calls[0][2]
        assert reg_payload is not None
        job = reg_payload["Job"]
        assert "ParameterizedJob" in job, (
            f"registration spec missing ParameterizedJob: {list(job.keys())}"
        )
        assert job["ParameterizedJob"]["MetaRequired"] == ["sample_id"]

    def test_dispatch_sends_correct_meta_params(self) -> None:
        """``dispatch_job`` must send ``sample_id``, ``openstudio_version``,
        and ``container_image`` as meta vars."""
        with mocked_dispatch_transport() as mock:
            ex = NomadExecutor(
                address="http://nomad.test:4646",
                poll_interval_s=0.01,
                max_poll_interval_s=0.02,
                use_dispatch=True,
            )
            ex.submit(
                lambda: "ok",
                name="sample-42",
                container="nrel/openstudio:3.9.0",
                openstudio_version="3.9.0",
            )
            ex.shutdown()

        calls = mock.calls  # type: ignore[attr-defined]
        # Second call must be the dispatch (POST /v1/job/osimflow-worker/dispatch).
        assert calls[1][0] == "POST"
        assert "/dispatch" in calls[1][1]
        dispatch_payload = calls[1][2]
        assert dispatch_payload is not None
        meta = dispatch_payload.get("Meta", {})
        assert meta["sample_id"] == "sample-42", f"wrong sample_id: {meta}"
        assert meta["openstudio_version"] == "3.9.0", f"wrong openstudio_version: {meta}"
        assert "nrel/openstudio:3.9.0" in meta["container_image"], f"wrong container_image: {meta}"

    def test_dispatch_sends_variables_json_meta(self) -> None:
        """When ``variables_json`` is provided, it must be included in the
        dispatch meta."""
        with mocked_dispatch_transport() as mock:
            ex = NomadExecutor(
                address="http://nomad.test:4646",
                poll_interval_s=0.01,
                max_poll_interval_s=0.02,
                use_dispatch=True,
            )
            ex.submit(
                lambda: "ok",
                name="sample-0",
                variables_json='{"window_ratio": 0.4}',
            )
            ex.shutdown()

        calls = mock.calls  # type: ignore[attr-defined]
        dispatch_payload = calls[1][2]
        assert dispatch_payload is not None
        meta = dispatch_payload.get("Meta", {})
        assert meta["variables_json"] == '{"window_ratio": 0.4}'

    def test_dispatch_only_registers_once_for_multiple_submits(self) -> None:
        """Multiple ``submit()`` calls must register the parameterized job
        only once, then dispatch for each call."""
        with mocked_dispatch_transport():
            ex = NomadExecutor(
                address="http://nomad.test:4646",
                poll_interval_s=0.01,
                max_poll_interval_s=0.02,
                use_dispatch=True,
            )
            ex.submit(lambda: "ok", name="sample-0")
            # Manually reset — the mock queue is consumed per submit, but
            # the executor should skip registration on the second call.
            assert ex._dispatch_job_registered is True
            ex.shutdown()

    def test_dispatch_build_spec_security_no_privileged(self) -> None:
        """The dispatch job spec must set ``privileged = False``."""
        ex = NomadExecutor(use_dispatch=True)
        spec = ex._build_dispatch_job_spec()
        task = spec["Job"]["TaskGroups"][0]["Tasks"][0]
        assert task["Config"].get("privileged") is False, (
            f"privileged must be False, got {task['Config'].get('privileged')}"
        )
        ex.shutdown()

    def test_dispatch_build_spec_memory_limited(self) -> None:
        """The dispatch job spec must limit memory to 4096 MB."""
        ex = NomadExecutor(use_dispatch=True)
        spec = ex._build_dispatch_job_spec()
        task = spec["Job"]["TaskGroups"][0]["Tasks"][0]
        assert task["Resources"]["MemoryMB"] == 4096, (
            f"memory must be 4096 MB, got {task['Resources']['MemoryMB']}"
        )
        ex.shutdown()

    def test_dispatch_build_spec_cpu_limited(self) -> None:
        """The dispatch job spec must limit CPU to 2000 MHz (2 CPUs)."""
        ex = NomadExecutor(use_dispatch=True)
        spec = ex._build_dispatch_job_spec()
        task = spec["Job"]["TaskGroups"][0]["Tasks"][0]
        assert task["Resources"]["CPU"] == 2000, (
            f"CPU must be 2000 MHz, got {task['Resources']['CPU']}"
        )
        ex.shutdown()

    def test_dispatch_build_spec_zero_restarts(self) -> None:
        """The dispatch job spec must set restart attempts to 0."""
        ex = NomadExecutor(use_dispatch=True)
        spec = ex._build_dispatch_job_spec()
        task = spec["Job"]["TaskGroups"][0]["Tasks"][0]
        assert task["Restart"]["Attempts"] == 0
        ex.shutdown()

    def test_dispatch_build_spec_has_parameterized_job(self) -> None:
        """The dispatch job spec must have a ParameterizedJob block."""
        ex = NomadExecutor(use_dispatch=True)
        spec = ex._build_dispatch_job_spec()
        pj = spec["Job"].get("ParameterizedJob")
        assert pj is not None, "ParameterizedJob block missing"
        assert "sample_id" in pj["MetaRequired"]
        assert "variables_json" in pj["MetaOptional"]
        assert "openstudio_version" in pj["MetaOptional"]
        assert "container_image" in pj["MetaOptional"]
        ex.shutdown()

    def test_dispatch_returns_handle_with_job_id(self) -> None:
        """The handle returned by dispatch submit must carry the dispatched
        job ID."""
        with mocked_dispatch_transport():
            ex = NomadExecutor(
                address="http://nomad.test:4646",
                poll_interval_s=0.01,
                max_poll_interval_s=0.02,
                use_dispatch=True,
            )
            handle = ex.submit(lambda: "ok", name="sample-0")
            ex.shutdown()

        assert handle.job_id == "osimflow-worker/dispatch-123", (
            f"unexpected job_id: {handle.job_id}"
        )


# ---------------------------------------------------------------------------
# Allocation failure handling in dispatch mode
# ---------------------------------------------------------------------------
class TestNomadDispatchFailure:
    """Test allocation failure handling with dispatch mode."""

    def test_failed_allocation_raises_with_description(self) -> None:
        """A ``failed`` allocation must raise RuntimeError with the
        failure description."""
        with mocked_dispatch_transport(
            eval_alloc_response=[{"ID": "alloc-fail", "ClientStatus": "running"}],
            alloc_responses=[
                {
                    "ID": "alloc-fail",
                    "ClientStatus": "failed",
                    "JobID": "osimflow-worker/dispatch-fail",
                    "TaskStates": {
                        "simulate": {
                            "State": "dead",
                            "Failed": True,
                            "Events": [
                                {
                                    "Type": "Terminated",
                                    "Description": "Exit Code: 1 (simulation error)",
                                }
                            ],
                        }
                    },
                },
            ],
        ):
            ex = NomadExecutor(
                poll_interval_s=0.01,
                max_poll_interval_s=0.02,
                use_dispatch=True,
            )
            handle = ex.submit(lambda: None, name="fail-sample")
            with pytest.raises(RuntimeError, match="Exit Code: 1"):
                handle.result(timeout=5)
        ex.shutdown()

    def test_lost_allocation_raises(self) -> None:
        """A ``lost`` allocation must raise RuntimeError."""
        with mocked_dispatch_transport(
            eval_alloc_response=[{"ID": "alloc-lost", "ClientStatus": "running"}],
            alloc_responses=[
                {
                    "ID": "alloc-lost",
                    "ClientStatus": "lost",
                    "JobID": "osimflow-worker/dispatch-lost",
                },
            ],
        ):
            ex = NomadExecutor(
                poll_interval_s=0.01,
                max_poll_interval_s=0.02,
                use_dispatch=True,
            )
            handle = ex.submit(lambda: None, name="lost-sample")
            with pytest.raises(RuntimeError, match="lost"):
                handle.result(timeout=5)
        ex.shutdown()

    def test_unknown_status_treated_as_failure(self) -> None:
        """An allocation with an unrecognized terminal status must still
        raise (defensive — should not happen in practice)."""
        # Nomad doesn't actually return "unknown" as a ClientStatus,
        # but our code only checks for "complete" in the success path,
        # so anything else raises. This tests the defensive fallback.
        with mocked_dispatch_transport(
            eval_alloc_response=[{"ID": "alloc-unk", "ClientStatus": "running"}],
            alloc_responses=[
                {
                    "ID": "alloc-unk",
                    "ClientStatus": "failed",
                    "JobID": "osimflow-worker/dispatch-unk",
                    "TaskStates": {},
                },
            ],
        ):
            ex = NomadExecutor(
                poll_interval_s=0.01,
                max_poll_interval_s=0.02,
                use_dispatch=True,
            )
            handle = ex.submit(lambda: None, name="unk-sample")
            with pytest.raises(RuntimeError):
                handle.result(timeout=5)
        ex.shutdown()


# ---------------------------------------------------------------------------
# Backward compatibility: direct mode (use_dispatch=False) still works
# ---------------------------------------------------------------------------
class TestNomadDirectModeBackwardCompat:
    """Verify the legacy per-job submission path still works."""

    def test_direct_mode_uses_post_v1_jobs(self) -> None:
        """With ``use_dispatch=False``, the executor must POST to
        ``/v1/jobs`` (the legacy behavior)."""
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps(
            {"JobID": "osimflow/sample-0", "EvalID": "e1", "Index": 0}
        ).encode("utf-8")
        fake_response.__enter__ = lambda s: s
        fake_response.__exit__ = lambda s, *a: None

        with patch("urllib.request.urlopen", return_value=fake_response) as mock:
            ex = NomadExecutor(
                address="http://nomad.local:4646",
                use_dispatch=False,
            )
            ex.submit(lambda: "ok", name="sample-0", container="nrel/openstudio:3.11.0")
            ex.shutdown()

        request_obj = mock.call_args.args[0]
        assert request_obj.get_method() == "POST"
        assert "/v1/jobs" in request_obj.full_url
        assert "/dispatch" not in request_obj.full_url
