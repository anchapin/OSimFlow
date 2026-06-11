"""Tests for issue #135 — Nomad HCL job spec and parameterized dispatch.

Acceptance criteria (G19a):

  * ``infra/nomad/osimflow_worker.hcl`` exists and defines a parameterized
    batch job with security constraints (no privileged, memory limited).
  * ``NomadExecutor._build_parameterized_spec()`` produces a valid Nomad
    JSON job spec that matches the HCL template's structure.
  * ``NomadExecutor.register_parameterized_job()`` registers the spec via
    ``POST /v1/jobs`` and returns the job ID.
  * ``NomadExecutor.dispatch_sample()`` dispatches a child job via
    ``POST /v1/job/<id>/dispatch`` with per-sample metadata.
  * ``dispatch_sample()`` raises ``RuntimeError`` when called before
    ``register_parameterized_job()``.
  * The HCL template exists on disk and is syntactically valid.

All tests mock ``urllib.request.urlopen`` so no real Nomad cluster is
needed in CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import NomadExecutor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
HCL_TEMPLATE = REPO_ROOT / "infra" / "nomad" / "osimflow_worker.hcl"


# ---------------------------------------------------------------------------
# Test: HCL template file exists and has the right structure
# ---------------------------------------------------------------------------
def test_hcl_template_file_exists() -> None:
    """The HCL template must exist at ``infra/nomad/osimflow_worker.hcl``."""
    assert HCL_TEMPLATE.is_file(), f"missing HCL template: {HCL_TEMPLATE}"


def test_hcl_template_contains_parameterized_block() -> None:
    """The HCL template must define a ``parameterized`` block with the
    required and optional meta keys.
    """
    content = HCL_TEMPLATE.read_text(encoding="utf-8")
    assert "parameterized" in content, "HCL template missing 'parameterized' block"
    assert "meta_required" in content, "HCL template missing 'meta_required'"
    assert "SAMPLE_ID" in content, "HCL template missing 'SAMPLE_ID' in meta_required"
    assert "OUTDIR" in content, "HCL template missing 'OUTDIR' in meta_required"
    assert "meta_optional" in content, "HCL template missing 'meta_optional'"
    assert "OPENSTUDIO_VERSION" in content, (
        "HCL template missing 'OPENSTUDIO_VERSION' in meta_optional"
    )


def test_hcl_template_security_no_privileged() -> None:
    """The HCL template must explicitly set ``privileged = false``."""
    content = HCL_TEMPLATE.read_text(encoding="utf-8")
    assert "privileged = false" in content, (
        "HCL template missing 'privileged = false' security constraint"
    )


def test_hcl_template_security_memory_limited() -> None:
    """The HCL template must set memory limits (4096 MB)."""
    content = HCL_TEMPLATE.read_text(encoding="utf-8")
    assert "4096" in content, "HCL template missing memory limit (4096 MB)"


def test_hcl_template_security_restart_fail_fast() -> None:
    """The HCL template must set ``mode = "fail"`` in the restart policy."""
    content = HCL_TEMPLATE.read_text(encoding="utf-8")
    assert 'mode     = "fail"' in content, "HCL template missing restart policy mode = fail"


# ---------------------------------------------------------------------------
# Test: _build_parameterized_spec produces valid JSON spec
# ---------------------------------------------------------------------------
def test_build_parameterized_spec_structure() -> None:
    """``_build_parameterized_spec()`` must produce a Nomad JSON job spec
    with the parameterized block, security constraints, and resource limits.
    """
    ex = NomadExecutor(address="http://nomad.test:4646", datacentre="dc1")
    spec = ex._build_parameterized_spec(  # noqa: SLF001
        datacentre="dc1",
        cpus=2000,
        memory_mb=4096,
    )
    ex.shutdown()

    job = spec["Job"]
    assert job["ID"] == "osimflow-worker"
    assert job["Name"] == "osimflow-worker"
    assert job["Type"] == "batch"
    assert job["Datacenters"] == ["dc1"]

    # Parameterized block
    param = job["ParameterizedJob"]
    assert param["MetaRequired"] == ["SAMPLE_ID", "OUTDIR"]
    assert "OPENSTUDIO_VERSION" in param["MetaOptional"]
    assert "GENERATION" in param["MetaOptional"]


def test_build_parameterized_spec_security_no_privileged() -> None:
    """The parameterized spec must set ``privileged: false`` on the task."""
    ex = NomadExecutor()
    spec = ex._build_parameterized_spec(datacentre="dc1")  # noqa: SLF001
    ex.shutdown()

    task = spec["Job"]["TaskGroups"][0]["Tasks"][0]
    assert task["Config"]["privileged"] is False, (
        "privileged must be False in the parameterized spec"
    )


def test_build_parameterized_spec_memory_limited() -> None:
    """The parameterized spec must set memory limits."""
    ex = NomadExecutor()
    spec = ex._build_parameterized_spec(datacentre="dc1", memory_mb=4096)  # noqa: SLF001
    ex.shutdown()

    task = spec["Job"]["TaskGroups"][0]["Tasks"][0]
    assert task["Resources"]["MemoryMB"] == 4096
    assert task["Config"]["memory_mb"] == 4096


def test_build_parameterized_spec_restart_fail_fast() -> None:
    """The parameterized spec must set restart mode to ``fail``."""
    ex = NomadExecutor()
    spec = ex._build_parameterized_spec(datacentre="dc1")  # noqa: SLF001
    ex.shutdown()

    restart = spec["Job"]["TaskGroups"][0]["RestartPolicy"]
    assert restart["Mode"] == "fail"
    assert restart["Attempts"] == 1


def test_build_parameterized_spec_meta_interpolation() -> None:
    """The task's meta block must use Nomad interpolation syntax."""
    ex = NomadExecutor()
    spec = ex._build_parameterized_spec(datacentre="dc1")  # noqa: SLF001
    ex.shutdown()

    task = spec["Job"]["TaskGroups"][0]["Tasks"][0]
    assert task["Meta"]["SAMPLE_ID"] == "${NOMAD_META_SAMPLE_ID}"
    assert task["Meta"]["OUTDIR"] == "${NOMAD_META_OUTDIR}"


# ---------------------------------------------------------------------------
# Test: register_parameterized_job submits via HTTP API
# ---------------------------------------------------------------------------
def test_register_parameterized_job_posts_spec() -> None:
    """``register_parameterized_job()`` must POST the parameterized spec
    to ``/v1/jobs`` and return the job ID.
    """
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps(
        {"JobID": "osimflow-worker", "EvalID": "eval-register", "Index": 1}
    ).encode("utf-8")
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = lambda s, *a: None

    with patch("urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
        ex = NomadExecutor(address="http://nomad.test:4646", datacentre="dc1")
        job_id = ex.register_parameterized_job(cpus=2000, memory_mb=4096)

    assert job_id == "osimflow-worker"
    assert ex._parameterized_job_registered is True  # noqa: SLF001

    # Verify the POST payload
    mock_urlopen.assert_called_once()
    request_obj = mock_urlopen.call_args.args[0]
    assert request_obj.get_method() == "POST"
    assert request_obj.full_url == "http://nomad.test:4646/v1/jobs"
    payload = json.loads(request_obj.data.decode("utf-8"))
    assert payload["Job"]["ID"] == "osimflow-worker"
    assert "ParameterizedJob" in payload["Job"]
    ex.shutdown()


# ---------------------------------------------------------------------------
# Test: dispatch_sample posts to /v1/job/<id>/dispatch
# ---------------------------------------------------------------------------
def test_dispatch_sample_posts_dispatch_request() -> None:
    """``dispatch_sample()`` must POST to ``/v1/job/osimflow-worker/dispatch``
    with the per-sample metadata.
    """
    # Two HTTP calls: register + dispatch
    register_response = MagicMock()
    register_response.read.return_value = json.dumps(
        {"JobID": "osimflow-worker", "EvalID": "eval-reg", "Index": 1}
    ).encode("utf-8")
    register_response.__enter__ = lambda s: s
    register_response.__exit__ = lambda s, *a: None

    dispatch_response = MagicMock()
    dispatch_response.read.return_value = json.dumps(
        {
            "JobID": "osimflow-worker/dispatch-1234567890",
            "EvalID": "eval-dispatch-1",
            "Index": 2,
        }
    ).encode("utf-8")
    dispatch_response.__enter__ = lambda s: s
    dispatch_response.__exit__ = lambda s, *a: None

    requests_seen: list[Any] = []

    call_count = 0

    def fake_urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        requests_seen.append(request)
        if call_count == 1:
            return register_response
        return dispatch_response

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ex = NomadExecutor(address="http://nomad.test:4646", datacentre="dc1")
        ex.register_parameterized_job()
        handle = ex.dispatch_sample(
            sample_id="sample-0",
            outdir="/data/runs/001",
            openstudio_version="3.5.0",
            generation=2,
        )

    assert handle.job_id == "osimflow-worker/dispatch-1234567890"
    assert call_count == 2

    # Verify the dispatch payload (second request)
    request_obj = requests_seen[-1]
    assert request_obj.get_method() == "POST"
    assert "/v1/job/osimflow-worker/dispatch" in request_obj.full_url
    payload = json.loads(request_obj.data.decode("utf-8"))
    assert payload["Meta"]["SAMPLE_ID"] == "sample-0"
    assert payload["Meta"]["OUTDIR"] == "/data/runs/001"
    assert payload["Meta"]["OPENSTUDIO_VERSION"] == "3.5.0"
    assert payload["Meta"]["GENERATION"] == "2"
    ex.shutdown()


def test_dispatch_sample_raises_before_register() -> None:
    """``dispatch_sample()`` must raise ``RuntimeError`` when called
    before ``register_parameterized_job()``.
    """
    ex = NomadExecutor(address="http://nomad.test:4646", datacentre="dc1")
    with pytest.raises(RuntimeError, match="not registered"):
        ex.dispatch_sample(
            sample_id="sample-0",
            outdir="/data/runs/001",
        )
    ex.shutdown()


def test_dispatch_sample_optional_meta_omitted_when_none() -> None:
    """When ``openstudio_version`` and ``generation`` are not provided,
    they must NOT appear in the dispatch metadata.
    """
    register_response = MagicMock()
    register_response.read.return_value = json.dumps(
        {"JobID": "osimflow-worker", "EvalID": "eval-reg", "Index": 1}
    ).encode("utf-8")
    register_response.__enter__ = lambda s: s
    register_response.__exit__ = lambda s, *a: None

    dispatch_response = MagicMock()
    dispatch_response.read.return_value = json.dumps(
        {
            "JobID": "osimflow-worker/dispatch-abc",
            "EvalID": "eval-dispatch-2",
            "Index": 3,
        }
    ).encode("utf-8")
    dispatch_response.__enter__ = lambda s: s
    dispatch_response.__exit__ = lambda s, *a: None

    requests_seen: list[Any] = []

    call_count = 0

    def fake_urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        requests_seen.append(request)
        if call_count == 1:
            return register_response
        return dispatch_response

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ex = NomadExecutor(address="http://nomad.test:4646", datacentre="dc1")
        ex.register_parameterized_job()
        ex.dispatch_sample(
            sample_id="sample-1",
            outdir="/data/runs/002",
        )

    request_obj = requests_seen[-1]
    payload = json.loads(request_obj.data.decode("utf-8"))
    meta = payload["Meta"]
    assert "OPENSTUDIO_VERSION" not in meta, f"OPENSTUDIO_VERSION should be omitted: {meta}"
    assert "GENERATION" not in meta, f"GENERATION should be omitted: {meta}"
    assert meta["SAMPLE_ID"] == "sample-1"
    assert meta["OUTDIR"] == "/data/runs/002"
    ex.shutdown()


# ---------------------------------------------------------------------------
# Test: dispatch returns a Handle with correct job_id
# ---------------------------------------------------------------------------
def test_dispatch_handle_carries_job_id() -> None:
    """The handle returned by ``dispatch_sample()`` must carry the
    dispatched job ID and evaluation ID for downstream polling.
    """
    register_response = MagicMock()
    register_response.read.return_value = json.dumps(
        {"JobID": "osimflow-worker", "EvalID": "eval-reg", "Index": 1}
    ).encode("utf-8")
    register_response.__enter__ = lambda s: s
    register_response.__exit__ = lambda s, *a: None

    dispatch_response = MagicMock()
    dispatch_response.read.return_value = json.dumps(
        {
            "JobID": "osimflow-worker/dispatch-xyz",
            "EvalID": "eval-dispatch-3",
            "Index": 4,
        }
    ).encode("utf-8")
    dispatch_response.__enter__ = lambda s: s
    dispatch_response.__exit__ = lambda s, *a: None

    call_count = 0

    def fake_urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return register_response
        return dispatch_response

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ex = NomadExecutor(address="http://nomad.test:4646", datacentre="dc1")
        ex.register_parameterized_job()
        handle = ex.dispatch_sample(
            sample_id="sample-2",
            outdir="/data/runs/003",
        )

    assert handle.job_id == "osimflow-worker/dispatch-xyz"
    assert handle.worker_id == "osimflow-worker/dispatch-xyz"
    assert handle.worker_region == "dc1"
    ex.shutdown()
