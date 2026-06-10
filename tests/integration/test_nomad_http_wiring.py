"""Tests for issue #27 — wire NomadExecutor to the HashiCorp Nomad HTTP API.

Mirrors the structure of ``test_awsbatch_boto3_wiring.py`` (issue #5) so the
two remote-substrate executors can be reviewed side-by-side.

Acceptance criteria from the issue:

  * ``NomadExecutor`` extends ``BaseExecutor`` and conforms to the same
    ``submit()`` / ``Handle`` / ``shutdown()`` contract as
    ``LocalExecutor`` / ``SlurmExecutor`` / ``AWSBatchExecutor``.
  * Lazy-imports its HTTP transport (the stdlib ``urllib.request``) so
    local-only / slurm-only / aws-only users do not pay any import cost
    beyond what is already loaded.
  * Submits parameterized ``batch`` jobs with the NREL OpenStudio
    container image. The job spec carries the ``OSIMFLOW_OS_VERSION`` /
    ``OSIMFLOW_CONTAINER`` env vars so the work layer is
    substrate-agnostic.
  * Maps OSimFlow resource directives (``cpus`` / ``memory_mb`` /
    ``time_min``) to the Nomad ``resources`` block.
  * Polls ``GET /v1/allocation/<id>`` with exponential backoff until the
    allocation reaches a terminal state; failed allocations re-raise a
    ``RuntimeError`` whose message includes the Nomad status
    description.
  * Sends the same per-submit kwargs (env, container, cpus, memory,
    time_min) as the other remote executors; consumes the
    ``openstudio_version`` kwarg the Campaign passes.
  * No long-lived credentials — the Nomad ACL token is sourced from
    the ``NOMAD_TOKEN`` env var (the documented Nomad pattern for
    CI/automation). The constructor signature does NOT accept a token
    kwarg; users set the env var themselves.
  * ``pyproject.toml`` has the ``[nomad]`` extras group. The standard
    library is used for HTTP, so the extras group is intentionally
    empty (a marker) so users can document their intent in
    requirements.txt.

The tests mock ``urllib.request.urlopen`` so no real Nomad cluster is
needed in CI; the test is portable to any Python 3.12+ environment.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import NomadExecutor


# ---------------------------------------------------------------------------
# Lazy import: the Nomad executor must not import anything heavy at module
# load. The stdlib is unavoidable; we only assert the executor does not
# sneak in third-party packages (e.g. python-nomad) that would penalize
# local / slurm / aws users.
# ---------------------------------------------------------------------------
def test_nomad_executor_does_not_third_party_import_at_module_load() -> None:
    """Importing ``osimflow.executors`` must not import python-nomad or
    any other Nomad SDK. The transport is stdlib-only.

    The local-executor / slurm-executor / aws-batch users should not
    pay the import cost of a Nomad SDK they will never use.
    """
    import osimflow.executors as exec_mod

    forbidden = ("nomad", "python_nomad")
    for name in forbidden:
        assert not hasattr(exec_mod, name), (
            f"{name!r} was imported at module load — must be lazy-imported"
        )


# ---------------------------------------------------------------------------
# SubmitJob payload: verify the executor builds a valid Nomad job spec
# and POSTs it to the right endpoint. This is the core acceptance
# criterion for the executor.
# ---------------------------------------------------------------------------
def test_nomad_submit_builds_batch_job_spec() -> None:
    """``submit()`` must call ``urllib.request.urlopen`` with a POST to
    ``/v1/jobs`` whose body is a valid ``batch`` job spec carrying the
    OpenStudio container, env vars, and resource directives.
    """
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps(
        {"EvalID": "abc-eval", "JobID": "osimflow/sample-0", "Index": 0}
    ).encode("utf-8")
    fake_response.status = 200
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = lambda s, *a: None

    with patch("urllib.request.urlopen", return_value=fake_response) as fake_urlopen:
        ex = NomadExecutor(address="http://nomad.local:4646", datacentre="dc1")
        handle = ex.submit(
            lambda: "ok",
            name="sample-0",
            cpus=4,
            memory_mb=2048,
            time_min=30,
            container="openstudio_cli_image:3.4.0",
            openstudio_version="3.4.0",
        )

    # First call must be POST /v1/jobs.
    fake_urlopen.assert_called_once()
    request_obj = fake_urlopen.call_args.args[0]
    assert request_obj.get_method() == "POST", (
        f"expected POST to /v1/jobs, got {request_obj.get_method()}"
    )
    assert request_obj.full_url == "http://nomad.local:4646/v1/jobs", (
        f"unexpected URL: {request_obj.full_url!r}"
    )

    # The job spec must be a parameterized batch job with the right
    # shape.
    payload = json.loads(request_obj.data.decode("utf-8"))
    job = payload["Job"]
    assert job["Type"] == "batch", f"expected batch job, got {job['Type']!r}"
    assert job["Name"].startswith("osimflow-"), job["Name"]
    assert job["Datacenters"] == ["dc1"]

    # Dispatched parameterized job: the per-sample metadata must
    # travel in the Meta payload so the work layer can recover it
    # without a shared filesystem in the trivial case.
    assert "Meta" in job
    assert job["Meta"]["OSIMFLOW_SAMPLE_NAME"] == "sample-0"
    assert job["Meta"]["OSIMFLOW_OS_VERSION"] == "3.4.0"

    # Container task: NREL OpenStudio image, env vars, resource block.
    task = job["TaskGroups"][0]["Tasks"][0]
    assert task["Name"] == "osimflow"
    assert task["Config"]["image"] == "openstudio_cli_image:3.4.0"
    env = task["Config"]["env"]
    env_dict = {e["name"]: e["value"] for e in env}
    assert env_dict["OSIMFLOW_OS_VERSION"] == "3.4.0"
    assert env_dict["OSIMFLOW_CONTAINER"] == "openstudio_cli_image:3.4.0"

    # Resources: cpus -> CPU compute, memory_mb -> MemoryMB.
    resources = task["Resources"]
    assert resources["CPU"] == 4000  # Nomad CPU is in MHz; 4 cpus * 1000
    # Nomad's memory is in MB by default.
    assert resources["MemoryMB"] == 2048

    # Handle carries the Nomad job id.
    assert handle.job_id == "osimflow/sample-0"
    ex.shutdown()


def test_nomad_submit_converts_cpus_to_mhz() -> None:
    """Nomad's ``CPU`` resource is in MHz. A 1-cpu job must be 1000 MHz
    so it lands on a client that advertises 1+ GHz CPUs.
    """
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({"JobID": "x"}).encode("utf-8")
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = lambda s, *a: None

    with patch("urllib.request.urlopen", return_value=fake_response):
        ex = NomadExecutor()
        ex.submit(lambda: None, name="t", cpus=1, memory_mb=512)

    request_obj = ex._client.urlopen.call_args.args[0]  # type: ignore[attr-defined]
    payload = json.loads(request_obj.data.decode("utf-8"))
    task = payload["Job"]["TaskGroups"][0]["Tasks"][0]
    assert task["Resources"]["CPU"] == 1000
    assert task["Resources"]["MemoryMB"] == 512
    ex.shutdown()


def test_nomad_submit_uses_minimum_resource_defaults() -> None:
    """When the caller does not pass cpus/memory_mb/time_min, the
    executor must still build a valid job spec from the BaseExecutor
    defaults (1 / 1024 / 60).
    """
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({"JobID": "job-defaults"}).encode("utf-8")
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = lambda s, *a: None

    with patch("urllib.request.urlopen", return_value=fake_response):
        ex = NomadExecutor()
        ex.submit(lambda: None, name="t")

    request_obj = ex._client.urlopen.call_args.args[0]  # type: ignore[attr-defined]
    payload = json.loads(request_obj.data.decode("utf-8"))
    task = payload["Job"]["TaskGroups"][0]["Tasks"][0]
    assert task["Resources"]["CPU"] == 1000
    assert task["Resources"]["MemoryMB"] == 1024
    ex.shutdown()


def test_nomad_submit_omits_env_when_not_provided() -> None:
    """If the caller does not pass ``container`` or
    ``openstudio_version``, the env list must still be a well-formed
    list (Nomad rejects malformed env blocks), but the per-job env
    keys may be absent.
    """
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({"JobID": "job-no-env"}).encode("utf-8")
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = lambda s, *a: None

    with patch("urllib.request.urlopen", return_value=fake_response):
        ex = NomadExecutor()
        ex.submit(lambda: None, name="t")

    request_obj = ex._client.urlopen.call_args.args[0]  # type: ignore[attr-defined]
    payload = json.loads(request_obj.data.decode("utf-8"))
    env = payload["Job"]["TaskGroups"][0]["Tasks"][0]["Config"]["env"]
    assert isinstance(env, list)
    for entry in env:
        assert set(entry.keys()) == {"name", "value"}
    ex.shutdown()


# ---------------------------------------------------------------------------
# Polling and failure handling: GET /v1/allocation/<id> must be polled
# until terminal, and FAILED must re-raise with the status description
# so the Campaign's `except Exception` path logs the failure correctly.
# ---------------------------------------------------------------------------
def _fake_response(body: dict[str, Any] | list[Any]) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: None
    return resp


@contextmanager
def mocked_nomad_client(
    *,
    submit_response: dict[str, Any],
    alloc_responses: list[dict[str, Any]],
) -> Iterator[MagicMock]:
    """Context manager: patch urllib.request.urlopen so the first call
    is the job submit (returning ``submit_response``) and subsequent
    calls return the next entry of ``alloc_responses``.

    The mock records every urlopen call so the test can inspect the
    sequence of URLs the executor polled.
    """
    queue: list[Any] = [submit_response, *alloc_responses]
    calls: list[Any] = []

    def fake_urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
        if not queue:
            raise AssertionError("executor issued more HTTP calls than expected")
        body = queue.pop(0)
        calls.append((request.get_method(), request.full_url))
        return _fake_response(body)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen) as mock:
        mock.calls = calls  # type: ignore[attr-defined]
        yield mock


def test_nomad_result_returns_none_on_success() -> None:
    """``Handle.result()`` must block until the Nomad allocation
    reaches the ``complete`` terminal state and return ``None`` to
    the Campaign. The actual KPI extraction happens in a downstream
    step that reads on-disk artifacts from shared storage.
    """
    with mocked_nomad_client(
        submit_response={"JobID": "osimflow/ok", "Index": 0, "EvalID": "e1"},
        alloc_responses=[
            {"ID": "alloc-1", "ClientStatus": "complete", "JobID": "osimflow/ok"},
        ],
    ) as mock:
        ex = NomadExecutor(poll_interval_s=0.01, max_poll_interval_s=0.02)
        handle = ex.submit(lambda: 42, name="ok")
        result = handle.result(timeout=5)

    assert result is None
    assert handle.done() is True
    # The mock recorded the submit and the allocation lookup.
    methods = [c[0] for c in mock.calls]  # type: ignore[attr-defined]
    assert methods[0] == "POST"
    assert all(m == "GET" for m in methods[1:])
    ex.shutdown()


def test_nomad_failed_raises_with_status_description() -> None:
    """When the Nomad allocation reaches a failed state, ``Handle.result()``
    must re-raise an exception whose message includes the status
    description. The Campaign's ``except Exception`` path needs a string
    it can log.
    """
    with mocked_nomad_client(
        submit_response={"JobID": "osimflow/fail", "Index": 0, "EvalID": "e2"},
        alloc_responses=[
            {
                "ID": "alloc-2",
                "ClientStatus": "failed",
                "JobID": "osimflow/fail",
                "TaskStates": {
                    "osimflow": {
                        "State": "dead",
                        "Failed": True,
                        "Events": [
                            {
                                "Type": "Terminated",
                                "Description": "Exit Code: 137 (OOM killed)",
                            }
                        ],
                    }
                },
            }
        ],
    ):
        ex = NomadExecutor(poll_interval_s=0.01, max_poll_interval_s=0.02)
        handle = ex.submit(lambda: None, name="fail")
        with pytest.raises(RuntimeError, match="Exit Code: 137"):
            handle.result(timeout=5)
    ex.shutdown()


# ---------------------------------------------------------------------------
# Polling cadence: the executor must back off exponentially starting
# from ``poll_interval_s`` and cap at ``max_poll_interval_s``.
# ---------------------------------------------------------------------------
def test_nomad_polling_uses_exponential_backoff() -> None:
    """The allocation lookup cadence should grow exponentially until it
    caps at ``max_poll_interval_s``.
    """
    with mocked_nomad_client(
        submit_response={"JobID": "osimflow/poll", "Index": 0, "EvalID": "e3"},
        alloc_responses=[
            {"ID": "alloc-poll", "ClientStatus": "pending", "JobID": "osimflow/poll"},
            {"ID": "alloc-poll", "ClientStatus": "pending", "JobID": "osimflow/poll"},
            {"ID": "alloc-poll", "ClientStatus": "pending", "JobID": "osimflow/poll"},
            {"ID": "alloc-poll", "ClientStatus": "complete", "JobID": "osimflow/poll"},
        ],
    ):
        sleep_durations: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleep_durations.append(seconds)

        with patch("time.sleep", side_effect=fake_sleep):
            ex = NomadExecutor(poll_interval_s=1.0, max_poll_interval_s=4.0)
            handle = ex.submit(lambda: None, name="poll")
            handle.result(timeout=5)

    assert sleep_durations[0] == 1.0
    # Subsequent sleeps must be non-decreasing (and capped). Note: the
    # final running call does not sleep, so the trailing slice may be
    # shorter — `strict=False` is intentional.
    assert all(
        b >= a
        for a, b in zip(sleep_durations, sleep_durations[1:])  # noqa: B905
    ), f"sleep durations not non-decreasing: {sleep_durations}"
    assert sleep_durations[-1] <= 4.0, f"final sleep {sleep_durations[-1]} exceeds cap 4.0"
    ex.shutdown()


# ---------------------------------------------------------------------------
# Security: the executor must source the Nomad ACL token from the
# NOMAD_TOKEN env var, not from a constructor kwarg. Mirrors the AWS
# Batch rule of "no long-lived credentials in code".
# ---------------------------------------------------------------------------
def test_nomad_executor_does_not_accept_token_kwarg() -> None:
    """The constructor must not expose a ``token`` parameter. Users
    set ``NOMAD_TOKEN`` in their environment; that is the documented
    Nomad pattern for CI/automation.
    """
    import inspect

    sig = inspect.signature(NomadExecutor.__init__)
    params = list(sig.parameters)
    assert "token" not in params
    assert "nomad_token" not in params
    assert "acl_token" not in params


def test_nomad_executor_sources_address_from_env() -> None:
    """``NomadExecutor()`` with no args must use the ``NOMAD_ADDR`` env
    var (defaulting to ``http://127.0.0.1:4646`` if unset). Pinning the
    address in code would hard-code the deployment.
    """
    with patch.dict(os.environ, {"NOMAD_ADDR": "http://nomad.prod:4646"}):
        ex = NomadExecutor()
        assert ex.address == "http://nomad.prod:4646"
        ex.shutdown()

    # And the default when NOMAD_ADDR is unset.
    env_without_addr = {k: v for k, v in os.environ.items() if k != "NOMAD_ADDR"}
    with patch.dict(os.environ, env_without_addr, clear=True):
        ex = NomadExecutor()
        assert ex.address == "http://127.0.0.1:4646"
        ex.shutdown()


# ---------------------------------------------------------------------------
# Token propagation: when NOMAD_TOKEN is set, the Authorization header
# must carry it on every request (Nomad ACL auth). The test sets a
# sentinel env var, drives a submit, and asserts the header value.
# ---------------------------------------------------------------------------
def test_nomad_executor_propagates_nomad_token_header() -> None:
    """``NOMAD_TOKEN`` must be forwarded as the ``X-Nomad-Token`` header
    on every request. This is Nomad's ACL auth contract for external
    clients.
    """
    with patch.dict(os.environ, {"NOMAD_TOKEN": "sentinel-token-abc"}):
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"JobID": "x"}).encode("utf-8")
        fake_response.__enter__ = lambda s: s
        fake_response.__exit__ = lambda s, *a: None

        with patch("urllib.request.urlopen", return_value=fake_response):
            ex = NomadExecutor()
            ex.submit(lambda: None, name="t")

    request_obj = ex._client.urlopen.call_args.args[0]  # type: ignore[attr-defined]
    # urllib.request.Request normalizes header names to Title-Case
    # (``X-Nomad-Token`` → ``X-nomad-token``). Assert the header is
    # present regardless of casing — Nomad's HTTP API is case-insensitive.
    all_headers = {k.lower(): v for k, v in request_obj.headers.items()}
    assert all_headers.get("x-nomad-token") == "sentinel-token-abc", (
        f"NOMAD_TOKEN not forwarded: {dict(request_obj.headers)!r}"
    )
    ex.shutdown()


# ---------------------------------------------------------------------------
# Job name sanitization: Nomad job names must be DNS-1123 labels. The
# executor must slugify user-supplied names so a sample id like
# "sample-0" lands as a valid job name without Nomad rejecting it.
# ---------------------------------------------------------------------------
def test_nomad_submit_sanitizes_job_name() -> None:
    """The job name must be a DNS-1123 label (lowercase, alphanumerics
    + dashes, max 63 chars). The executor should slugify user input.
    """
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({"JobID": "x"}).encode("utf-8")
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = lambda s, *a: None

    with patch("urllib.request.urlopen", return_value=fake_response):
        ex = NomadExecutor()
        ex.submit(lambda: None, name="SAMPLE_0.weird!")

    request_obj = ex._client.urlopen.call_args.args[0]  # type: ignore[attr-defined]
    payload = json.loads(request_obj.data.decode("utf-8"))
    job_name = payload["Job"]["Name"]
    assert job_name == job_name.lower(), f"job name not lowercase: {job_name!r}"
    # DNS-1123 label: lowercase alphanumeric + dashes, max 63 chars.
    import re

    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", job_name), (
        f"job name not DNS-1123: {job_name!r}"
    )
    ex.shutdown()


# ---------------------------------------------------------------------------
# pyproject.toml: verify the [nomad] extras group exists. The stdlib is
# used for HTTP so the group is intentionally empty; the marker still
# serves as a documentation signal for users that the executor
# supports Nomad.
# ---------------------------------------------------------------------------
def test_pyproject_has_nomad_extras_group() -> None:
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    optional = data["project"]["optional-dependencies"]
    assert "nomad" in optional, "pyproject.toml is missing the [nomad] extras group"
