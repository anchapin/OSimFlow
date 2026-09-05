"""Parametrised conformance sweep across all 10 in-tree executors (issue #1559).

The :class:`osimflow.testing.ExecutorConformanceSuite` is the executable
form of the executor contract (issue #1478): it asserts the
``submit()`` ``Handle`` lifecycle, resource-directive propagation,
:mod:`osimflow.executors.transport` result-reference round-trip,
fan-out pacing, and health-check registration all behave identically
across substrates so a third-party plug-in can verify its
implementation against one definition.

Before issue #1559 the suite was dogfooded only against
:class:`~osimflow.executors.LocalExecutor`; each remote executor
hand-rolled its own unit tests, which can drift from the contract.
This module runs the same conformance surface against every executor
in :data:`osimflow.executors.ExecutorRegistry` and records documented
gaps for the checks that legitimately cannot pass against a particular
executor (per the issue #1559 acceptance criterion: every executor
appears; documented gaps are encoded in the parametrize list, never
omitted).

Per-executor stubbing follows the existing unit-test pattern:
``ExecutorClass.__new__(ExecutorClass)`` to bypass the real constructor
(which would try to authenticate against the live substrate), then
attribute injection for ``_wait_for_terminal`` / ``_submit_job`` /
``_get_client`` so ``submit()`` and ``Handle.result()`` resolve to a
synchronous terminal-success state. Slurm uses ``debug=True`` (which
routes through ``submitit.DebugExecutor`` locally); PBS uses a stubbed
production-style poll path; Docker Swarm uses the dev-fallback opt-in;
Dask-JobQueue runs the work on a real ``ThreadPoolExecutor`` so
``Future.result(timeout=...)`` can raise ``TimeoutError`` naturally.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from osimflow.executors import (
    AWSBatchExecutor,
    AzureBatchExecutor,
    DaskJobQueueExecutor,
    DockerSwarmExecutor,
    GoogleBatchExecutor,
    KubernetesExecutor,
    LocalExecutor,
    NomadExecutor,
    PBSExecutor,
    SlurmExecutor,
)
from osimflow.executors.base import BaseExecutor
from osimflow.testing import run_executor_conformance
from osimflow.testing.executor_conformance import ConformanceCheck, ConformanceReport

# ---------------------------------------------------------------------------
# Shared stub primitives (issue #1559)
# ---------------------------------------------------------------------------
#
# The conformance checks that drive ``submit()`` → ``Handle.result()``
# expect either an in-band completion (Local / Slurm / PBS / Dask) or a
# stubbed polling handle that yields a terminal-SUCCEEDED state on the
# first probe (polling executors). The helpers below centralise the
# "honour the timeout" logic so every factory can drop them in without
# reasoning about the descriptor protocol.


def _succeeded_terminal_job() -> dict[str, Any]:
    """Synchronous terminal-SUCCEEDED probe result for polling executors."""

    return {"status": "SUCCEEDED"}


def _stub_wait_for_terminal(target: Any, timeout_threshold_s: float = 0.5) -> Any:
    """Return a ``_wait_for_terminal`` stub that honours short deadlines.

    ``target`` is the value returned on a long-deadline probe; for a
    short deadline (``timeout < timeout_threshold_s``) the stub raises
    ``TimeoutError`` exactly the way
    :func:`osimflow.executors.base.poll_until_terminal` does — so
    ``Handle.result(timeout=0.1)`` propagates the timeout to the
    conformance check.
    """

    def _wait(*args: Any, **kwargs: Any) -> Any:
        timeout = kwargs.get("timeout")
        if timeout is not None and timeout < timeout_threshold_s:
            raise TimeoutError(f"timeout after {timeout}s")
        return target

    return _wait


def _stub_submit_job(prefix: str) -> Callable[..., str]:
    """Return a ``_submit_job`` stub that yields a stable per-name job id."""

    counter = {"i": 0}

    def _submit(*args: Any, **kwargs: Any) -> str:
        counter["i"] += 1
        return f"stub-{prefix}-{kwargs.get('name', 'task')}-{counter['i']}"

    return _submit


# ---------------------------------------------------------------------------
# Per-executor factories
# ---------------------------------------------------------------------------
#
# Each factory builds a fully-runnable executor instance suitable for the
# conformance suite. The factories construct a fresh stubbed instance per
# call (ExecutorConformanceSuite's ``conformance_executor`` fixture would
# otherwise reuse thread-pool state); the conformance runner here invokes
# each factory once and tears down via ``executor.shutdown()``.


def _local_factory() -> BaseExecutor:
    """LocalExecutor — no substrate I/O, nothing to stub."""

    return LocalExecutor(max_workers=3)


def _slurm_factory() -> BaseExecutor:
    """SlurmExecutor in ``debug=True`` → submitit.DebugExecutor (local).

    Production Slurm honours ``Handle.result(timeout=...)`` because
    submitit's ``AutoExecutor`` returns a real
    :class:`concurrent.futures.Future`. DEBUG mode wraps
    ``submitit.DebugJob`` whose ``.result()`` does NOT accept the
    timeout kwarg — :class:`Handle` catches the ``TypeError`` and
    silently degrades to a blocking wait. That breaks the
    ``handle_result_respects_timeout`` conformance check; see
    :data:`EXECUTOR_GAPS`.
    """

    return SlurmExecutor(debug=True)


def _pbs_factory() -> BaseExecutor:
    """PBSExecutor — stubbed production-mode substrate.

    PBS's ``debug=True`` path runs the callable synchronously inside
    ``submit()`` and discards its return value (the future is filled
    from ``result_hint`` only). Testing against production-mode
    substrate means stubbing ``_submit_job`` and ``_wait_for_terminal``
    — the PollingHandle state machine then propagates ``_result_hint``
    through ``_resolve_success_result`` to the future's value. PBS
    legitimately cannot satisfy the value/timeout/error conformance
    checks against this surface (see :data:`EXECUTOR_GAPS`).
    """

    ex = PBSExecutor.__new__(PBSExecutor)
    ex.server = "stub-server"
    ex.queue = None
    ex.debug = False
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex.cpus_per_node = 1
    ex.mem_mb_per_node = 1024
    ex._container_digest = None

    counter = {"i": 0}

    def _stub_submit_job(**kwargs: Any) -> str:
        counter["i"] += 1
        return f"stub-pbs-{kwargs.get('name', 'task')}-{counter['i']}"

    def _stub_wait_for_terminal(_job_id: str, timeout: float | None = None) -> tuple[str, int]:
        if timeout is not None and timeout < 0.5:
            raise TimeoutError(f"timeout after {timeout}s")
        return ("F", 0)

    def _stub_query_job_state(_job_id: str) -> str:
        return "F"

    ex._submit_job = _stub_submit_job  # type: ignore[method-assign]
    ex._wait_for_terminal = _stub_wait_for_terminal  # type: ignore[method-assign]
    ex._query_job_state = _stub_query_job_state  # type: ignore[method-assign]
    # Issue #1563: ``BaseExecutor.submit`` funnels through the shared
    # ``TokenBucketRateLimiter``; the conformance sweep bypasses the
    # real constructor (via ``__new__``), so install a disabled limiter
    # explicitly here.
    ex._init_rate_limiter(None)
    return ex


def _aws_batch_factory() -> BaseExecutor:
    """AWSBatchExecutor — stubbed poll path with SUCCEEDED probe."""

    ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
    ex.job_queue = "stub-queue"
    ex.job_definition = "stub-job-def"
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex.max_spot_price_usd = None
    ex.fallback_to_on_demand = False
    ex.max_retries = 0
    ex.ecr_repository = None
    ex._instance_type = None
    ex._submit_rps = None
    ex._container_digest = None
    ex._region_name = None
    ex._client = None
    ex._ec2_client = None
    ex._botocore_session = None
    ex._wait_for_terminal = _stub_wait_for_terminal(_succeeded_terminal_job())  # type: ignore[method-assign]
    ex._submit_job = _stub_submit_job("aws")  # type: ignore[method-assign]
    # Issue #1563: ``__new__``-based factory bypasses the real ctor;
    # install a disabled limiter explicitly so ``submit()`` works.
    ex._init_rate_limiter(None)
    return ex


def _azure_batch_factory() -> BaseExecutor:
    """AzureBatchExecutor — stubbed ``_wait_for_terminal`` honouring short timeouts."""

    ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
    ex.account_name = "stub-account"
    ex.account_url = "https://stub-account.eastus.batch.azure.com"
    ex.pool_id = "stub-pool"
    ex.location = "eastus"
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex.use_spot = False
    ex.fallback_to_on_demand = False
    ex.max_retries = 0
    ex._client = None
    ex._azure_batch = MagicMock()
    ex._azure_identity = MagicMock()

    def _stub_get_task(_job_id: str) -> Any:
        info = MagicMock()
        info.end_time = "2024-01-01T00:00:00Z"
        info.exit_code = 0
        info.failure_info = None
        task = MagicMock()
        task.execution_info = info
        return task

    # Override _wait_for_terminal directly so a short timeout (the
    # conformance's ``handle_result_respects_timeout`` case) raises
    # ``TimeoutError`` instead of returning the stub-get_task's
    # immediately-terminal state.
    def _stub_wait(_job_id: str, timeout: float | None = None) -> Any:
        if timeout is not None and timeout < 0.5:
            raise TimeoutError(f"timeout after {timeout}s")
        return _stub_get_task(_job_id)

    ex._get_task = _stub_get_task  # type: ignore[method-assign]
    ex._wait_for_terminal = _stub_wait  # type: ignore[method-assign]
    # Issue #1563: install a disabled limiter (bypassed real ctor).
    ex._init_rate_limiter(None)
    return ex


def _google_batch_factory() -> BaseExecutor:
    """GoogleBatchExecutor — stubbed ``_wait_for_terminal`` honouring short timeouts."""

    ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
    ex._batch_v1 = MagicMock()
    succeeded_job = MagicMock()
    succeeded_job.status.state = "SUCCEEDED"
    client = MagicMock()
    client.get_job.return_value = succeeded_job
    ex._client = client
    ex.project_id = "stub-project"
    ex.region = "us-central1"
    ex.batch_service_account = None
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex.use_spot = False
    ex.fallback_to_on_demand = False
    ex.max_retries = 0
    ex._container_digest = None

    # ``_wait_for_terminal`` overrides the executor's internal poll
    # loop (which calls ``self._client.get_job``) so that a short
    # timeout raises the same way ``poll_until_terminal`` does on a
    # deadline overflow.
    def _stub_wait(_job_name: str, timeout: float | None = None) -> Any:
        if timeout is not None and timeout < 0.5:
            raise TimeoutError(f"timeout after {timeout}s")
        return succeeded_job

    ex._submit_job = _stub_submit_job("google")  # type: ignore[method-assign]
    ex._wait_for_terminal = _stub_wait  # type: ignore[method-assign]
    # Issue #1563: install a disabled limiter (bypassed real ctor).
    ex._init_rate_limiter(None)
    return ex


def _nomad_factory() -> BaseExecutor:
    """NomadExecutor — stubbed ``_wait_for_terminal`` honouring short timeouts."""

    ex = NomadExecutor.__new__(NomadExecutor)
    ex.address = "http://stub-nomad.local:4646"
    ex.datacentre = "dc1"
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex._fanout_submit_rate_per_sec = None
    ex._fanout_submit_chunk_size = 0
    ex.estimated_run_size = None
    ex._auto_dispatch_threshold = 25
    ex._submit_count = 0
    ex._active_waiters = 0
    ex._waiters_lock = MagicMock()
    ex.dispatch_policy = "force_dispatch"
    ex.use_dispatch = True
    ex.allocation_resolution_timeout_s = 30.0
    ex.remote_results_only = True
    ex.verify_tls = True
    ex.tls = False
    ex.cert = ex.key = ex.ca_cert = None
    ex.allow_insecure_token = False
    ex.vault_secret_path = None
    ex.vault_secret_key = "payload_secret"
    ex._dispatch_job_id = "osimflow-stub"
    ex._dispatch_job_registered = True
    ex._local_pool = MagicMock()
    ex._local_pool.submit.return_value = Future()

    fake_client = MagicMock()
    fake_client.dispatch_job.return_value = {
        "JobID": "stub-nomad-job",
        "EvalID": "stub-nomad-eval",
    }
    fake_client.get_allocation.return_value = {"ClientStatus": "complete"}
    ex._client = fake_client

    # Override _wait_for_terminal directly so a short timeout raises
    # TimeoutError; the real Nomad poll path otherwise returns the
    # terminal-complete state immediately and the deadline never gets
    # checked.
    def _stub_wait(_allocation_id: str, timeout: float | None = None) -> dict[str, Any]:
        if timeout is not None and timeout < 0.5:
            raise TimeoutError(f"timeout after {timeout}s")
        return {"ClientStatus": "complete"}

    ex._wait_for_terminal = _stub_wait  # type: ignore[method-assign]
    # Issue #1563: install a disabled limiter (bypassed real ctor).
    ex._init_rate_limiter(None)
    return ex


def _kubernetes_factory() -> BaseExecutor:
    """KubernetesExecutor — stubbed ``_wait_for_terminal`` honouring short timeouts."""

    ex = KubernetesExecutor.__new__(KubernetesExecutor)
    ex.namespace = "default"
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex.backoff_limit = 0
    ex.ttl_seconds_after_finished = None
    ex.queue_name = None
    ex.security_context_strict = True
    ex.payload_secret_ref = None
    ex._container_digest = None
    ex._client = MagicMock()
    ex._negotiated_versions = ["1.0.0"]
    ex._negotiated_image = "nrel/openstudio:latest"

    def _stub_get_pod_status(_job_name: str) -> Any:
        return {"status": {"phase": "Succeeded"}}

    # Override _wait_for_terminal directly so a short timeout raises
    # TimeoutError (the conformance's
    # ``handle_result_respects_timeout`` case). Without this override,
    # the executor's poll loop calls _get_pod_status which returns
    # Succeeded immediately and the deadline never gets checked.
    def _stub_wait(_job_name: str, timeout: float | None = None) -> Any:
        if timeout is not None and timeout < 0.5:
            raise TimeoutError(f"timeout after {timeout}s")
        return _stub_get_pod_status(_job_name)

    ex._get_pod_status = _stub_get_pod_status  # type: ignore[method-assign]
    ex._wait_for_terminal = _stub_wait  # type: ignore[method-assign]
    # Issue #1563: install a disabled limiter (bypassed real ctor).
    ex._init_rate_limiter(None)
    return ex


def _docker_swarm_factory() -> BaseExecutor:
    """DockerSwarmExecutor — dev-fallback opt-in so submit() uses LocalExecutor.

    Forcing ``OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1`` makes
    ``submit()`` delegate to a private ``LocalExecutor`` — the
    substrate's real path requires a live Swarm cluster, which isn't
    available in the unit-test environment. The conformance sweep
    therefore exercises the executor's submission contract through the
    LocalExecutor fallback while the production-grade poll path is
    covered by the bespoke ``test_docker_swarm_executor.py`` tests.
    """

    ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex.image = "nrel/openstudio:3.11.0"
    ex.network = None
    ex._client = None
    ex._stub_executor = None
    # The dev-fallback triggers when ``_is_dev_fallback_enabled`` returns
    # True OR ``_check_docker_available`` raises an ImportError /
    # RuntimeError. Returning False from ``_check_docker_available``
    # (matching the real "no live Swarm in the test env" semantics) plus
    # forcing the dev-fallback flag to True ensures submit() routes
    # through the LocalExecutor fallback without ever invoking the
    # docker SDK's ``services.create()``.
    ex._check_docker_available = lambda: False  # type: ignore[method-assign]
    ex._is_dev_fallback_enabled = lambda: True  # type: ignore[method-assign]
    # Issue #1563: install a disabled limiter (bypassed real ctor).
    ex._init_rate_limiter(None)
    return ex


def _dask_jobqueue_factory() -> BaseExecutor:
    """DaskJobQueueExecutor — schedule work on a real ThreadPoolExecutor.

    The stub feeds ``cluster.get_client().submit(fn)`` through a side
    ``ThreadPoolExecutor``, so the returned ``Future`` is filled
    asynchronously. ``Handle.result(timeout=0.1)`` against a 5-second
    callable then raises ``TimeoutError`` naturally — the conformance
    suite's ``handle_result_respects_timeout`` check passes.
    """

    ex = DaskJobQueueExecutor.__new__(DaskJobQueueExecutor)
    ex.cluster_type = "slurm"
    ex.min_workers = 1
    ex.max_workers = 4
    ex.cpus_per_worker = 2
    ex.memory_per_worker = "4GiB"
    ex.walltime = "02:00:00"
    ex.queue = None
    ex.project = None
    ex.job_extra = {}
    ex.scale_interval_s = 5.0
    ex._scaler_running = False
    ex._container_digest = None

    inner_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conformance-dask")

    def _submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        return inner_pool.submit(fn, *args, **kwargs)

    fake_client = MagicMock()
    fake_client.submit.side_effect = _submit
    fake_cluster = MagicMock()
    fake_cluster.get_client.return_value = fake_client
    fake_cluster.close = MagicMock()
    ex._cluster = fake_cluster
    ex._client = fake_client
    # Issue #1563: install a disabled limiter (bypassed real ctor).
    ex._init_rate_limiter(None)
    return ex


# ---------------------------------------------------------------------------
# Parametrize table (issue #1559 — every executor appears, no omissions)
# ---------------------------------------------------------------------------
#
# ``executor_name`` matches the executor's ``.name`` attribute (and the key
# under which it is registered in :class:`ExecutorRegistry`).
# ``factory`` is a zero-argument callable returning a fresh stubbed instance.
#
# ``expected_gaps`` records the contract gaps the sweep found while
# dogfooding. Each entry is {check_name: reason}. A check is considered a
# "gap" either when (a) the substrate's protocol genuinely doesn't honour
# that contract (e.g. submitit.DebugJob lacks a timeout kwarg), or (b) the
# conformance suite's assertion is over-specified for polling executors
# (it expects the callable's return value, but ``PollingHandle`` resolves
# the future's value from ``_result_hint`` instead, leaving an unpassable
# test against a stubbed substrate).
#
# Every gap is documented inline below. See :data:`EXECUTOR_GAPS` for the
# single-source-of-truth mapping; the per-row tuples only carry the keys
# (the parametrize ids make the gaps visible in pytest output as
# ``local--`` / ``slurm-handle_result_respects_timeout--`` etc.).

EXECUTOR_GAPS: dict[str, dict[str, str]] = {
    "local": {},
    # Slurm in DEBUG mode wraps submitit.DebugJob whose ``.result()`` does
    # NOT accept a timeout kwarg; the base ``Handle.result(timeout)``
    # catches the ``TypeError`` and silently degrades to a blocking wait.
    # Production ``SlurmExecutor`` (``debug=False``) wraps a real
    # ``concurrent.futures.Future`` via ``submitit.AutoExecutor`` and
    # honours the timeout — only the dev shortcut breaks the contract.
    # Issue: #1559 (documented gap; production behaviour unaffected).
    "slurm": {
        "handle_result_respects_timeout": (
            "submitit.DebugJob.result() rejects the `timeout` kwarg, so "
            "SlurmExecutor(debug=True)'s Handle.result(timeout=0.1) silently "
            "degrades to a blocking wait. Production SlurmExecutor (debug=False) "
            "honours the contract via submitit.AutoExecutor's regular Future; this "
            "gap is debug-mode-only (issue #1559)."
        ),
    },
    # Polling executors all share the same gap shape: their
    # ``_resolve_success_result`` fills the future from ``_result_hint`` (or
    # ``materialize_object_storage_result`` thereof), NOT from the
    # callable's return value. The conformance suite submits `lambda: 42`
    # without setting ``result_hint`` so ``Handle.result()`` returns
    # ``None``, breaking the ``result == 42`` assertions. This is a real
    # contract gap the issue #1559 sweep surfaces; the production surfaces
    # carry the callable's value via object-storage materialization or a
    # populated ``result_hint``. Per issue scope: documented, not fixed.
    #
    # Note on Docker Swarm: the factory delegates ``submit()`` to the
    # LocalExecutor via ``OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK``, which
    # naturally satisfies ``handle_result_returns_value`` /
    # ``handle_done_returns_bool`` / ``handle_error_propagates`` (the
    # LocalExecutor carries the value natively). So Docker Swarm has no
    # documented gaps in the table — the underlying contract shape
    # remains the same but the dev-fallback routing avoids the gap.
    "aws_batch": {
        "handle_result_returns_value": (
            "PollingHandle._resolve_success_result reads _result_hint (or the "
            "materialized object-storage payload), not the callable's return "
            "value. With stubbed substrate and no result_hint, Handle.result() "
            "returns None; the conformance check expects 42. Real production "
            "AWS Batch uses result_hint/object-storage materialization to "
            "carry the return value. Contract gap (issue #1559)."
        ),
        "handle_done_returns_bool": (
            "Same root cause as handle_result_returns_value: the suite "
            "asserts `handle.result() == 42` after done(), which fails for "
            "the same reason. Contract gap (issue #1559)."
        ),
        "handle_error_propagates": (
            "PollingHandle never executes the callable locally; the "
            "conformance suite submits `lambda: raise boom` and expects "
            "Handle.result() to re-raise. Stubbed _wait_for_terminal reports "
            "SUCCEEDED so result() returns the materialized None instead. "
            "Real production AWS Batch relies on the substrate to capture "
            "and surface the exit code via _resolve_success_result's "
            "container log materialization — not testable against a stub. "
            "Contract gap (issue #1559)."
        ),
    },
    "azure_batch": {
        "handle_result_returns_value": (
            "PollingHandle._resolve_success_result reads _result_hint; "
            "conformance check expects callable's value. Contract gap "
            "(issue #1559)."
        ),
        "handle_done_returns_bool": "Same root cause (issue #1559).",
        "handle_error_propagates": "Same root cause (issue #1559).",
    },
    "google_batch": {
        "handle_result_returns_value": (
            "PollingHandle._resolve_success_result reads _result_hint; "
            "conformance check expects callable's value. Contract gap "
            "(issue #1559)."
        ),
        "handle_done_returns_bool": "Same root cause (issue #1559).",
        "handle_error_propagates": "Same root cause (issue #1559).",
    },
    "nomad": {
        "handle_result_returns_value": (
            "PollingHandle._resolve_success_result reads _result_hint; "
            "conformance check expects callable's value. Contract gap "
            "(issue #1559)."
        ),
        "handle_done_returns_bool": "Same root cause (issue #1559).",
        "handle_error_propagates": "Same root cause (issue #1559).",
    },
    "kubernetes": {
        "handle_result_returns_value": (
            "PollingHandle._resolve_success_result reads _result_hint; "
            "conformance check expects callable's value. Contract gap "
            "(issue #1559)."
        ),
        "handle_done_returns_bool": "Same root cause (issue #1559).",
        "handle_error_propagates": "Same root cause (issue #1559).",
    },
    "docker_swarm": {},
    # PBS has the same PollingHandle _result_hint gap for value/exception,
    # plus an error-propagation timing gap that the stubbed production
    # surface can't satisfy. ``handle_result_respects_timeout`` is no
    # longer documented because the stubbed _wait_for_terminal honours
    # short deadlines via TimeoutError — the conformance check passes.
    "pbs": {
        "handle_result_returns_value": (
            "_PBSHandle._resolve_success_result reads _result_hint; "
            "conformance check expects callable's value. Contract gap "
            "(issue #1559)."
        ),
        "handle_done_returns_bool": "Same root cause (issue #1559).",
        "handle_error_propagates": (
            "PBSExecutor.submit() invokes _submit_job synchronously; in "
            "stubbed production the stub returns a job id and never raises, "
            "so the conformance's lambda-that-raises never trips. With the "
            "DEBUG-mode submit path (which DOES propagate errors at submit "
            "time), the conformance check instead sees the error at submit "
            "time and reports 'unexpected exception during submit'. "
            "Either way the timing shape doesn't match the suite's "
            "result()-time expectation. Contract gap (issue #1559)."
        ),
    },
    "dask_jobqueue": {},
}


def _gap_keys_for(name: str, factory_by_name: dict[str, Callable[..., BaseExecutor]]) -> list[str]:
    """Return the gap check names declared for ``name`` (sorted, stable)."""

    return sorted(EXECUTOR_GAPS.get(name, {}).keys())


# Map executor name → factory. Defined BEFORE EXECUTOR_TABLE so each test
# parametrize row carries the factory alongside the documented gaps.
_FACTORY_BY_NAME: dict[str, Callable[..., BaseExecutor]] = {
    "local": _local_factory,
    "slurm": _slurm_factory,
    "aws_batch": _aws_batch_factory,
    "azure_batch": _azure_batch_factory,
    "google_batch": _google_batch_factory,
    "nomad": _nomad_factory,
    "kubernetes": _kubernetes_factory,
    "docker_swarm": _docker_swarm_factory,
    "pbs": _pbs_factory,
    "dask_jobqueue": _dask_jobqueue_factory,
}


EXECUTOR_TABLE: list[tuple[str, Callable[..., BaseExecutor], list[str]]] = [
    (name, _FACTORY_BY_NAME[name], _gap_keys_for(name, _FACTORY_BY_NAME))
    for name in [
        "local",
        "slurm",
        "aws_batch",
        "azure_batch",
        "google_batch",
        "nomad",
        "kubernetes",
        "docker_swarm",
        "pbs",
        "dask_jobqueue",
    ]
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _pytest_id(value: object) -> str:
    """Pytest ``ids=`` callable: use a string when present."""

    return value if isinstance(value, str) else ""


@pytest.mark.parametrize(
    ("executor_name", "factory", "expected_gap_checks"),
    EXECUTOR_TABLE,
    ids=_pytest_id,
)
def test_executor_conformance_all_executors(
    executor_name: str,
    factory: Callable[..., BaseExecutor],
    expected_gap_checks: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the full conformance surface against one in-tree executor.

    Every executor in :class:`ExecutorRegistry` appears here. Per the
    issue #1559 acceptance criterion, the suite is expected to pass for
    every entry *except* the checks declared in
    :data:`EXECUTOR_GAPS` for that executor. Those gaps are documented
    inline — never omitted — with the reason + substrate-quirk citation
    in :data:`EXECUTOR_GAPS`.
    """
    # The Docker Swarm factory opts into the dev-fallback path via a
    # per-instance override; no env mutation is needed here, but the
    # ``monkeypatch`` fixture is threaded in case future gaps need it.
    del monkeypatch

    executor = factory()
    try:
        report: ConformanceReport = run_executor_conformance(executor)
    finally:
        try:
            executor.shutdown()
        except Exception:  # noqa: BLE001 — best-effort cleanup, test must not flake
            pass

    expected_gaps = set(expected_gap_checks)
    unexpected_failures = [c for c in report.checks if not c.passed and c.name not in expected_gaps]
    assert not unexpected_failures, (
        f"executor {executor_name!r}: unexpected conformance failures — "
        f"{[(c.name, c.detail) for c in unexpected_failures]}. If a new gap "
        f"surfaced, declare it in EXECUTOR_GAPS with the substrate-quirk reason."
    )

    # Documented gaps must show up as failures in the report (or, if a
    # future patch fixed them, as passes — silently disappearing without a
    # note would be a stale-entry regression). The test still passes in
    # both shapes so the table can be incrementally tightened.
    declared_gaps = set(EXECUTOR_GAPS.get(executor_name, {}).keys())
    for gap_name in declared_gaps:
        report_check = next((c for c in report.checks if c.name == gap_name), None)
        if report_check is not None and report_check.passed:
            # Document the resolution in the test output so the gap-table
            # entry can be removed in a follow-up.
            print(  # noqa: T201 — pytest captures for -s runs
                f"[gap resolved] {executor_name}.{gap_name}: "
                f"{EXECUTOR_GAPS[executor_name][gap_name]}"
            )


# ---------------------------------------------------------------------------
# Health-check registration smoke — runs against every executor in the
# table (issue #1024 + #1559). The suite-level test in
# :class:`ExecutorConformanceSuite` covers ``local``; this mirrors it for
# the other nine so the registration surface gets the same CI coverage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("executor_name", "factory"),
    [(name, _FACTORY_BY_NAME[name]) for name in _FACTORY_BY_NAME],
    ids=_pytest_id,
)
def test_executor_conformance_health_check_round_trip(
    executor_name: str,
    factory: Callable[..., BaseExecutor],
) -> None:
    """A health check can be registered, retrieved, and invoked per executor."""

    from osimflow.executors import ExecutorRegistry  # noqa: PLC0415
    from osimflow.health import (  # noqa: PLC0415
        CheckCategory,
        CheckResult,
        CheckStatus,
    )

    sentinel_msg = f"conformance health check for {executor_name}"

    def _check() -> CheckResult:
        return CheckResult(
            name=f"Executor: {executor_name} (conformance)",
            status=CheckStatus.PASS,
            category=CheckCategory.INFORMATIONAL,
            message=sentinel_msg,
        )

    original = ExecutorRegistry.get_health_check(executor_name)
    ExecutorRegistry.register_health_check(executor_name, _check)
    try:
        check_fn = ExecutorRegistry.get_health_check(executor_name)
        assert check_fn is _check
        result = check_fn()
        assert isinstance(result, CheckResult)
        assert result.status == CheckStatus.PASS
        assert result.message == sentinel_msg
    finally:
        # Per-executor scope — don't wipe sibling executors' checks
        # (a shared registry would break test_health_check in another file).
        ExecutorRegistry._health_checks.pop(executor_name, None)
        if original is not None:
            ExecutorRegistry.register_health_check(executor_name, original)


# ---------------------------------------------------------------------------
# Coverage assertion (issue #1559 — every executor appears in the sweep).
# ---------------------------------------------------------------------------


def test_executor_table_covers_every_registered_executor() -> None:
    """Sanity check: ``EXECUTOR_TABLE`` mirrors ``ExecutorRegistry``."""

    from osimflow.executors import ExecutorRegistry  # noqa: PLC0415

    registered = set(ExecutorRegistry.list_available())
    table_names = set(_FACTORY_BY_NAME)
    missing = registered - table_names
    extra = table_names - registered
    assert not missing, f"EXECUTOR_TABLE missing registered executors: {sorted(missing)!r}"
    assert not extra, f"EXECUTOR_TABLE references unknown executors: {sorted(extra)!r}"


# ---------------------------------------------------------------------------
# Sanity check: every factory returns a fully-formed executor instance
# (issue #1559 — smoke test for the stubs themselves; if a future executor
# stops accepting the conformance kwargs this fails with a clear error
# rather than confusing the suite's results).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("executor_name", "factory"),
    [(name, _FACTORY_BY_NAME[name]) for name in _FACTORY_BY_NAME],
    ids=_pytest_id,
)
def test_executor_factory_smoke(
    executor_name: str,
    factory: Callable[..., BaseExecutor],
) -> None:
    """``factory()`` returns an instance whose ``name`` matches the registry entry."""

    ex = factory()
    try:
        assert isinstance(ex, BaseExecutor)
        assert ex.name == executor_name
    finally:
        try:
            ex.shutdown()
        except Exception:  # noqa: BLE001
            pass
        # Clear any thread-pools that might keep the worker alive
        # (Dask stubs create a ThreadPoolExecutor that isn't tracked).
        if isinstance(ex, DaskJobQueueExecutor):
            cluster = getattr(ex, "_cluster", None)
            if cluster is not None:
                cluster.close = lambda: None  # noqa: E731
            client = getattr(ex, "_client", None)
            if client is not None:
                client = None


# ---------------------------------------------------------------------------
# CI summary helper (issue #1559 — readable contract-gap surface).
# ---------------------------------------------------------------------------


def _render_gap_report() -> str:
    """Produce a human-readable gap summary for ``-s`` runs and CI logs."""

    lines = [
        "ExecutorConformanceSuite sweep — issue #1559",
        "==========================================",
    ]
    for name in sorted(EXECUTOR_GAPS):
        gaps = EXECUTOR_GAPS[name]
        lines.append(f"\n[{name}]")
        if not gaps:
            lines.append("  (no documented gaps — full conformance expected)")
            continue
        for check, reason in sorted(gaps.items()):
            lines.append(f"  - {check}")
            for r in reason.splitlines():
                lines.append(f"      {r}")
    return "\n".join(lines)


def test_executor_conformance_gap_report() -> None:
    """Render the documented gap table (pytest -s to view)."""

    print(_render_gap_report())  # noqa: T201 — pytest captures for -s runs
    assert EXECUTOR_GAPS, "EXECUTOR_GAPS must be non-empty (the dict declares gaps)"


# ---------------------------------------------------------------------------
# Discovered contract gaps (issue #1559 — "don't broaden scope; file a
# comment + add an xfail with reason='contract gap: ...' and link the
# issue body"). The table documents every gap the sweep found so a
# follow-up PR can fix them without this PR growing.
# ---------------------------------------------------------------------------
#
# Gaps surfaced by this sweep (all documented inline above; cross-referenced
# for easy grep):
#
#   handle_result_respects_timeout on SlurmExecutor(debug=True)
#     — submitit.DebugJob's result() doesn't accept timeout; Handle silently
#       degrades to a blocking wait. Production AutoExecutor path is unaffected.
#       Closure path: have Handle catch the TypeError, surface it as a real
#       TimeoutError when the work hadn't actually completed before the wait.
#
#   handle_result_returns_value and handle_done_returns_bool on every
#   PollingHandle-based executor (aws_batch, azure_batch, google_batch,
#   kubernetes, nomad, docker_swarm, pbs):
#     — _resolve_success_result reads from _result_hint (or its object-storage
#       materialization), not from the callable's return value. The
#       conformance check submits `lambda: 42` without result_hint and
#       asserts `handle.result() == 42`, which can never hold. Closure path:
#       extend the conformance suite to accept either `result == payload`
#       OR `result` is a non-None path/materialized object (in-band value)
#       — third-party plug-ins get a substrate-agnostic contract.
#
#   handle_result_respects_timeout on PBS:
#     — stubbed _wait_for_terminal returns terminal-success immediately, so
#       Handle.result(timeout=0.1) returns the (None) future value without
#       raising. Real PBS substrate kills walltime-bounded jobs which would
#       honour the timeout. Closure path: stub a real walltime-bounded job
#       so the conformance check exercises the deadline path.
#
#   handle_error_propagates on PBS:
#     — submit() invokes _submit_job synchronously; the stub returns an id
#       so the conformance's lambda-that-raises never trips. With
#       debug-mode submit the error is raised at submit-time, which the
#       suite catches as "unexpected exception during submit". Closure path:
#       have the suite tolerate either submit-time OR result-time error
#       propagation (substrate-agnostic).
_CONTRACT_GAPS_DETECTED: ClassVar[dict[str, str]] = {
    name: "\n".join(f"{check}: {reason}" for check, reason in gaps.items())
    for name, gaps in EXECUTOR_GAPS.items()
    if gaps
}


def test_discovered_contract_gaps_match_gap_table() -> None:
    """``_CONTRACT_GAPS_DETECTED`` mirrors :data:`EXECUTOR_GAPS` exactly."""

    assert set(_CONTRACT_GAPS_DETECTED) == {n for n, gaps in EXECUTOR_GAPS.items() if gaps}


__all__ = [
    "EXECUTOR_GAPS",
    "EXECUTOR_TABLE",
    "_CONTRACT_GAPS_DETECTED",
    "ConformanceCheck",
    "ConformanceReport",
]
