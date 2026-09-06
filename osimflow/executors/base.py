"""Shared executor primitives: BaseExecutor and Handle.

These are in a separate module to avoid circular import issues when
per-executor modules (e.g. azure_batch_executor.py) need to inherit
from them before the full __init__.py is initialized.
"""

from __future__ import annotations

__all__ = [
    "BaseExecutor",
    "Handle",
    "PollingHandle",
    "PollOutcome",
    "SubmitRequest",
    "poll_until_terminal",
    "retry_with_backoff",
]

import abc
import dataclasses
import enum
import json
import logging
import os
import random
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

from osimflow.executors._rate_limiter import TokenBucketRateLimiter
from osimflow.executors.transport import encode_transport_value, validate_transport_mode

log = logging.getLogger("osimflow.executors.base")


@dataclasses.dataclass
class Handle:
    """A future-like handle. Substrate-specific implementations subclass this.

    The Handle abstracts over both `concurrent.futures.Future` (local)
    and `submitit.Future` (Slurm), whose `.result()` and `.done()`
    signatures differ slightly. We unify them here.

    Worker tracking fields (issue #105) are populated by each executor
    at submit time so the Campaign can attribute every sample to the
    worker that processed it — essential for cost attribution and
    debugging large campaigns.

    Error tracking (issue #721): polling errors are captured here so
    callers can distinguish "still running" from "failed with error".
    """

    job_id: str
    _future: Future[Any]
    worker_id: str | None = None
    worker_ip: str | None = None
    worker_region: str | None = None
    cost_usd: float | None = None
    billed_duration_seconds: float | None = None
    error: Exception | None = None

    def result(self, timeout: float | None = None) -> Any:
        if self.error is not None:
            raise self.error
        try:
            return self._future.result(timeout=timeout)
        except TypeError:
            return self._future.result()

    def done(self) -> bool:
        try:
            return self._future.done()
        except AttributeError:
            return getattr(self._future, "_completed", False)

    def is_failed(self) -> bool:
        """Return True if a polling error has been captured."""
        return self.error is not None


class PollOutcome(enum.Enum):
    """Terminal classification of a polled job (issue #1464).

    ``PollingHandle._classify`` returns this plus the substrate's raw
    spot-classification input, so the shared state machine can dispatch
    without knowing the substrate's job schema.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Terminal but neither succeeded nor failed (e.g. a Google Batch
    #: state outside SUCCEEDED/FAILED): treated as success with a
    #: ``None`` result, matching the pre-refactor Google behaviour.
    INDETERMINATE = "indeterminate"


def poll_until_terminal[T](  # noqa: PLR0912 — one branch per substrate seam (probe error / terminal / transient / deadline / grow order)
    probe: Callable[[], T],
    *,
    is_terminal: Callable[[T], bool],
    timeout: float | None,
    timeout_message: Callable[[float], str],
    poll_interval_s: float,
    max_poll_interval_s: float,
    on_pending: Callable[[T, float, float], None] | None = None,
    tolerate_probe_errors: bool = False,
    on_probe_error: Callable[[Exception, float], None] | None = None,
    is_transient: Callable[[T], bool] | None = None,
    on_transient: Callable[[T, float], None] | None = None,
    sleep_for: Callable[[float, float | None], float] | None = None,
    next_delay: Callable[[float], float] | None = None,
    grow_before_sleep: bool = False,
) -> T:
    """Shared terminal-poll loop for every polling executor (issue #1540).

    This is the single owner of the poll skeleton that used to be
    hand-rolled (with per-substrate drift) inside the AWS Batch, Azure
    Batch, Google Batch, Kubernetes, Docker Swarm, Nomad, and PBS
    executors' ``_wait_for_terminal`` methods:

    * ``time.monotonic`` deadline tracking — remaining time is
      recomputed every iteration and expiry raises ``TimeoutError``
      built from ``timeout_message(elapsed)``;
    * ``min(delay, remaining)`` clamping so a single sleep never
      overshoots the caller-supplied deadline (a custom ``sleep_for``
      takes over the clamping duty entirely);
    * capped exponential backoff — ``min(delay * 2,
      max_poll_interval_s)`` by default, with two extension seams for
      Nomad's adaptive curve (``next_delay``) and phased sleep
      (``sleep_for``);
    * ``grow_before_sleep`` selects the two historic orderings: the
      Azure / Google / Kubernetes / Docker Swarm / PBS loops double
      the delay *before* sleeping (first sleep is
      ``2 * poll_interval_s``), while the AWS and Nomad loops sleep
      the current delay first and grow afterwards.

    Substrate-specific behaviour arrives purely via callables, so this
    function never logs and never knows the job schema:

    * ``probe()`` — one non-blocking substrate status call. May raise;
      when ``tolerate_probe_errors`` is set (Kubernetes / Docker
      Swarm), the exception is passed to ``on_probe_error`` and the
      loop keeps polling; otherwise it propagates immediately.
    * ``is_terminal(status)`` — terminal check; the terminal probe
      result is returned to the caller.
    * ``is_transient(status)`` / ``on_transient(status, delay)`` —
      PBS transient-qstat handling (#1405): a transient probe result
      (e.g. ``PBS_STATE_TRANSIENT``) retries at the *current* delay
      without growing the backoff and without clamping to the
      remaining deadline.
    * ``on_pending(status, delay, sleep_amount)`` — per-iteration
      info-log hook; ``delay`` is the pre-growth backoff state and
      ``sleep_amount`` is what will actually be slept, so each
      substrate can log exactly the value it historically logged.

    The AWS / Azure / Google / Nomad request-retry helpers use the
    bounded-attempt sibling :func:`retry_with_backoff` instead of this
    loop.
    """
    delay = float(poll_interval_s)

    def _grow(current: float) -> float:
        if next_delay is not None:
            return next_delay(current)
        return min(current * 2.0, max_poll_interval_s)

    def _sleep_amount(current: float, remaining: float | None) -> float:
        if sleep_for is not None:
            return sleep_for(current, remaining)
        if remaining is None:
            return current
        return min(current, remaining)

    start = time.monotonic()
    while True:
        probe_error = False
        try:
            status = probe()
        except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
            if not tolerate_probe_errors:
                raise
            probe_error = True
            if on_probe_error is not None:
                on_probe_error(exc, delay)
        else:
            if is_terminal(status):
                return status
            if is_transient is not None and is_transient(status):
                if on_transient is not None:
                    on_transient(status, delay)
                if timeout is not None:
                    elapsed = time.monotonic() - start
                    if elapsed >= timeout:
                        raise TimeoutError(timeout_message(elapsed))
                time.sleep(delay)
                continue

        if timeout is not None:
            elapsed = time.monotonic() - start
            remaining = timeout - elapsed
            if remaining <= 0:
                raise TimeoutError(timeout_message(elapsed))
        else:
            remaining = None

        pre_sleep_delay = delay
        if grow_before_sleep:
            delay = _grow(delay)
        amount = _sleep_amount(delay, remaining)
        if not probe_error and on_pending is not None:
            on_pending(status, pre_sleep_delay, amount)
        time.sleep(amount)
        if not grow_before_sleep:
            delay = _grow(delay)


def retry_with_backoff[T](
    call_fn: Callable[[], T],
    *,
    retry_on: Callable[[BaseException], bool],
    max_attempts: int,
    initial_delay_s: float = 0.5,
    max_delay_s: float = 30.0,
    jitter: bool = True,
    on_retry: Callable[[BaseException, int, float], None] | None = None,
) -> T:
    """Shared bounded-attempt retry-with-backoff for substrate requests (issue #1540).

    This is the single owner of the exponential schedule that used to
    be hand-rolled in ``_submit_job_with_retry`` (AWS Batch),
    ``_retry_azure_submit`` (Azure Batch), ``_retry_nomad_request``
    (Nomad), and ``_retry_pbs_call`` (PBS):

    * ``max_attempts`` bounded loop — an exception classified
      non-retryable by ``retry_on``, or raised on the final attempt,
      propagates immediately;
    * full-jitter exponential backoff — the sleep window is
      ``min(delay, max_delay_s)`` and each retry sleeps
      ``random.uniform(0, window)`` (``jitter=False`` for the PBS
      variant, which sleeps the window deterministically);
    * the window doubles per attempt, capped at ``max_delay_s``.

    The helper itself never logs: each substrate passes an
    ``on_retry(exc, attempt, sleep_window)`` callback that emits its
    historic warning line through its own module logger.
    """
    delay = float(initial_delay_s)
    for attempt in range(1, max_attempts + 1):
        try:
            return call_fn()
        except BaseException as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
            if not retry_on(exc) or attempt >= max_attempts:
                raise
            window = min(delay, max_delay_s)
            if on_retry is not None:
                on_retry(exc, attempt, window)
            time.sleep(random.uniform(0, window) if jitter else window)
            delay = min(delay * 2.0, max_delay_s)
    raise RuntimeError(  # pragma: no cover — loop always returns or raises
        f"retry_with_backoff loop exited unexpectedly after {max_attempts} attempts"
    )


class PollingHandle(Handle):
    """Shared poll-retry-fallback state machine for polling handles (issue #1464).

    ``result()`` used to be duplicated nearly verbatim across
    ``_AzureBatchHandle`` and ``_GoogleBatchHandle`` (with a simpler
    poll-until-terminal-plus-``set_exception`` skeleton repeated in the
    Kubernetes / Nomad / Docker Swarm / PBS handles), so a fix to the
    backoff curve, retry accounting, deadline, or exception-raising
    path had to be applied and verified five-plus times.

    This template-method base owns the cross-substrate state machine:

    * the caller-supplied ``timeout`` deadline (issue #1465) — the
      start time is shared across spot-retry iterations, remaining
      time is recomputed every attempt, and expiry raises
      ``TimeoutError``;
    * retry accounting — ``max(0, executor.max_retries) + 1`` attempts;
    * jittered exponential backoff between spot retries
      (``min(5s * 2**attempt, 60s)`` with full jitter);
    * spot-interruption classification dispatch;
    * the fallback-to-on-demand transition when retries are exhausted
      and ``executor.fallback_to_on_demand`` is set.

    Substrate handles shrink to hooks:

    * ``_wait_for_terminal(timeout)`` — poll until terminal (raises
      ``TimeoutError`` on its own poll deadline); executors implement
      this on top of :func:`poll_until_terminal` so the poll skeleton
      lives here too (issue #1540);
    * ``_classify(job)`` — map a terminal job to a ``PollOutcome``
      plus the raw spot-classification reason;
    * ``_resolve_success_result(timeout)`` — resolve + materialize the
      callback-facing success value (kept substrate-local so tests can
      monkeypatch ``materialize_object_storage_result`` per module);
      ``timeout`` is the caller-supplied deadline, forwarded so
      handles backed by a local mirror future (Nomad's
      non-remote-results mode) can honour it;
    * ``_is_spot_interruption(reason)`` — spot classifier (default
      ``False`` — no spot support);
    * ``_resubmit()`` — spot retry: submit a replacement job and
      update the handle's job id / ``worker_id``;
    * ``_submit_on_demand()`` — the fallback submission (same updates);
    * ``_failure_error(job)`` / ``_fallback_failure_error(job)`` —
      substrate-worded ``RuntimeError`` factories;
    * ``_poll_job_id()`` — identifier used in timeout messages
      (default ``self.job_id``);
    * ``_retry_limit()`` / ``_has_on_demand_fallback()`` — read from
      ``executor.max_retries`` / ``executor.fallback_to_on_demand``
      with getattr defaults, so executors without spot support get a
      single-attempt, no-fallback machine.
    """

    _executor: Any
    _result_hint: Any = None
    _result_transport_mode: str = "auto"
    _result_storage_backend: str | None = None
    _result_storage_bucket: str | None = None
    _result_storage_prefix: str | None = None
    _result_storage_endpoint: str | None = None

    # ------------------------------------------------------------------
    # Substrate hooks
    # ------------------------------------------------------------------

    def _wait_for_terminal(self, timeout: float | None) -> Any:
        """Poll the substrate until the job reaches a terminal state."""
        raise NotImplementedError

    def _classify(self, job: Any) -> tuple[PollOutcome, str | None]:
        """Classify a terminal job as (outcome, raw spot-classification reason)."""
        raise NotImplementedError

    def _resolve_success_result(self, timeout: float | None = None) -> Any:
        """Resolve and materialize the callback-facing success result.

        ``timeout`` is the caller-supplied ``result()`` deadline;
        handles backed by a local mirror future (Nomad) forward it.
        """
        raise NotImplementedError

    def _poll_job_id(self) -> str:
        """Job identifier used in deadline-exceeded messages."""
        return self.job_id

    def _is_spot_interruption(self, reason: str | None) -> bool:
        """Whether a failure reason indicates a spot/preemptible interruption."""
        return False

    def _resubmit(self) -> None:
        """Spot retry: resubmit and update the handle's job identity."""
        raise NotImplementedError

    def _submit_on_demand(self) -> None:
        """Fallback: submit the on-demand replacement job."""
        raise NotImplementedError

    def _retry_limit(self) -> int:
        # int-coerced defensively: tests drive handles against bare
        # MagicMock executors whose auto-generated ``max_retries``
        # attribute would otherwise blow up ``max()`` (treated as 0).
        try:
            return max(0, int(getattr(self._executor, "max_retries", 0)))
        except (TypeError, ValueError):
            return 0

    def _has_on_demand_fallback(self) -> bool:
        # ``is True`` (not ``bool()``) so mock executors with an
        # auto-generated attribute don't accidentally enable the
        # fallback transition.
        return getattr(self._executor, "fallback_to_on_demand", False) is True

    def _failure_error(self, job: Any) -> RuntimeError:
        """Substrate-worded error for a non-spot terminal failure."""
        raise NotImplementedError

    def _fallback_failure_error(self, job: Any) -> RuntimeError:
        """Substrate-worded error for a failure after the on-demand fallback."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared state machine
    # ------------------------------------------------------------------

    def result(self, timeout: float | None = None) -> Any:
        # Timeout tracking (issue #1465): elapsed time is shared across
        # spot-retry iterations so the caller-supplied deadline is honoured
        # regardless of how many times the job is resubmitted. The
        # substrate-side kill (pool task timeout / allocation timeout /
        # activeDeadlineSeconds / walltime) remains defense in depth.
        start = time.monotonic()
        remaining: float | None = None  # None means "no timeout"
        effective_max_retries = self._retry_limit()
        for attempt in range(effective_max_retries + 1):
            # Compute remaining time for this poll iteration.
            if timeout is not None:
                elapsed = time.monotonic() - start
                remaining = timeout - elapsed
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out after {elapsed:.1f}s waiting for job {self._poll_job_id()!r}"
                    )

            try:
                job = self._wait_for_terminal(remaining)
            except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
                self._future.set_exception(exc)
                raise

            outcome, spot_reason = self._classify(job)
            if outcome is PollOutcome.SUCCEEDED:
                resolved = self._resolve_success_result(timeout)
                self._future.set_result(resolved)
                return resolved
            if outcome is PollOutcome.INDETERMINATE:
                self._future.set_result(None)
                return None

            if self._is_spot_interruption(spot_reason):
                if attempt < effective_max_retries:
                    backoff = min(5.0 * (2**attempt), 60.0)
                    jittered_backoff = random.uniform(0, backoff)
                    log.warning(
                        "Spot/preemptible interrupted (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1,
                        effective_max_retries,
                        jittered_backoff,
                        spot_reason,
                    )
                    time.sleep(jittered_backoff)
                    self._resubmit()
                    continue

                if self._has_on_demand_fallback():
                    log.warning(
                        "Spot retries exhausted (%d), falling back to on-demand",
                        effective_max_retries,
                    )
                    self._submit_on_demand()
                    try:
                        job = self._wait_for_terminal(remaining)
                    except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
                        self._future.set_exception(exc)
                        raise
                    outcome, _ = self._classify(job)
                    if outcome is PollOutcome.SUCCEEDED:
                        resolved = self._resolve_success_result(timeout)
                        self._future.set_result(resolved)
                        return resolved
                    error = self._fallback_failure_error(job)
                    self._future.set_exception(error)
                    raise error
                raise RuntimeError(
                    f"Spot retries exhausted ({effective_max_retries}): {spot_reason}"
                )

            error = self._failure_error(job)
            self._future.set_exception(error)
            raise error

        raise RuntimeError("result loop exited unexpectedly")  # pragma: no cover


@dataclasses.dataclass
class SubmitRequest:
    """Structured submit request replacing implicit kwargs (issue #725, #1273).

    Construct and pass a ``SubmitRequest`` to ``BaseExecutor.submit()`` instead
    of passing keyword arguments.  This enforces field completeness at the
    type-checker level: missing required fields become mypy errors rather
    than silent runtime failures when executors receive unexpected ``**kwargs``.

    Example::

        request = SubmitRequest(
            fn=run_openstudio_sim,
            args=(mod_pkg, sid, os_version, out_dir),
            name=f"sim_{sid}",
            cpus=4,
            memory_mb=8 * 1024,
            time_min=240,
            container="nrel/openstudio:3.11.0",
        )
        handle = executor.submit_request(request)
    """

    fn: Callable[..., Any]
    """The callable to execute."""

    args: tuple[Any, ...] = ()
    """Positional arguments passed to *fn*."""

    name: str = "task"
    cpus: int = 1
    memory_mb: int = 1024
    time_min: int = 60
    container: str | None = None
    container_digest: str | None = None
    openstudio_version: str | None = None
    result_hint: Any = None
    remote_command: str | None = None
    result_transport_mode: str | None = None
    result_storage_backend: str | None = None
    result_storage_bucket: str | None = None
    result_storage_prefix: str | None = None
    result_storage_endpoint: str | None = None
    variables_json: str | None = None
    env: dict[str, str] | None = None
    stdout_path: Any = None
    stderr_path: Any = None
    max_retries: int | None = None
    worker_id: str | None = None


class BaseExecutor(abc.ABC):
    """All executors conform to this interface."""

    name: str = "base"

    #: Substrate-appropriate default submit rate (issue #1563).
    #:
    #: ``None`` (the base default) means *no throttling* — the
    #: executor's ``__init__`` constructs a disabled
    #: :class:`~osimflow.executors._rate_limiter.TokenBucketRateLimiter`
    #: and :meth:`submit` short-circuits without acquiring. Each
    #: concrete executor overrides this with a substrate-appropriate
    #: value (e.g. 10 RPS for AWS/Azure/Google Batch, 5 RPS for
    #: Kubernetes/Nomad, infinity for :class:`LocalExecutor`); the
    #: user can override via ``--submit-rps <float>`` on the CLI or
    #: the per-executor config dataclass.
    #:
    #: The base class deliberately leaves this ``None`` so the
    #: abstract contract stays neutral. Concrete executors that
    #: genuinely have no quota to bump against (only ``LocalExecutor``
    #: today) should explicitly set ``default_submit_rps = float("inf")``
    #: to record the policy decision rather than inheriting ``None``.
    default_submit_rps: float | None = None

    @property
    def requires_remote_runner_payload(self) -> bool:
        """Whether this executor dispatches work via ``python -m osimflow.remote_runner``.

        Executors that use a remote-runner payload (e.g. Nomad, Kubernetes)
        marshal step calls into ``OSIMFLOW_TASK_PAYLOAD`` and execute them
        inside a job container.  Other executors (Local, Slurm, AWS Batch, etc.)
        invoke work scripts directly and never need the payload path.

        Override in subclasses that differ from the default.
        """
        return False

    #: Whether this executor signs ``OSIMFLOW_TASK_PAYLOAD`` with the
    #: orchestrator's HMAC secret via ``build_signature_env``. The health
    #: check (issue #1404) warns when an executor requires the payload
    #: contract but does not sign it while the orchestrator has a secret
    #: configured — the per-sample jobs would fail signature verification
    #: at runtime (remote_runner fails closed, issue #1205). Set to
    #: ``True`` in every executor that calls ``build_signature_env``.
    signs_task_payload: bool = False

    #: Per-instance rate limiter (issue #1563). Initialised in
    #: :meth:`_init_rate_limiter` from :attr:`default_submit_rps` plus
    #: the constructor's ``submit_rps`` override. ``None`` until
    #: :meth:`_init_rate_limiter` runs (i.e. before ``__init__``
    #: completes); subclasses MUST call :meth:`_init_rate_limiter`
    #: from their own ``__init__`` so :meth:`submit` always has a
    #: limiter to acquire from.
    _rate_limiter: TokenBucketRateLimiter

    def _init_rate_limiter(
        self,
        submit_rps: float | None,
        *,
        name: str | None = None,
    ) -> TokenBucketRateLimiter:
        """Resolve and memoise the shared rate limiter for this executor.

        Subclasses call this at the end of their ``__init__`` with the
        effective RPS (constructor ``submit_rps`` override if non-None,
        else :attr:`default_submit_rps`). The shared singleton
        semantics (one bucket per ``(name, effective_rps)`` tuple) are
        critical so multiple executor instances stay within the same
        quota — see :meth:`TokenBucketRateLimiter.get_shared` (issue
        #1010's original AWS rationale; #1563's generalised version).

        ``None``, ``<= 0``, or ``+inf`` selects the shared *disabled*
        limiter — a no-op so the local-executor / test paths pay no
        lock cost. ``+inf`` is treated as "no quota to bump against"
        (``LocalExecutor`` uses ``float('inf')`` to record the policy
        decision explicitly, per issue #1563).
        """
        import math

        effective = float(submit_rps) if submit_rps is not None else self.default_submit_rps
        resolved_name = name or self.name
        if (
            effective is None
            or effective <= 0.0
            or effective == float("inf")
            or math.isnan(effective)
        ):
            self._rate_limiter = TokenBucketRateLimiter.get_shared(0.0, name=resolved_name)
            return self._rate_limiter
        self._rate_limiter = TokenBucketRateLimiter.get_shared(effective, name=resolved_name)
        return self._rate_limiter

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        container_digest: str | None = None,
        openstudio_version: str | None = None,
        result_hint: Any = None,
        remote_command: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
        variables_json: str | None = None,
        env: dict[str, str] | None = None,
        stdout_path: Any = None,
        stderr_path: Any = None,
        max_retries: int | None = None,
        worker_id: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        """Submit *fn* for execution on this executor's substrate.

        Template method (issue #1563): acquires a token from the
        executor's :attr:`_rate_limiter` (constructed by
        :meth:`_init_rate_limiter` from :attr:`default_submit_rps` plus
        any per-instance override) and delegates the substrate-specific
        call to :meth:`_do_submit`. The throttle lives here, not in each
        executor, so a substrate cannot accidentally skip it.
        """
        # Lazy init: tests that bypass the real constructor (e.g.
        # ``ExecutorClass.__new__(ExecutorClass)`` followed by attribute
        # injection) skip ``__init__`` and therefore ``_init_rate_limiter``.
        # Fall back to a disabled singleton here so the template-method
        # contract stays uniform across in-process and stub executors.
        limiter = getattr(self, "_rate_limiter", None)
        if limiter is None:
            limiter = self._init_rate_limiter(None)
        # Acquire BEFORE the substrate call so an over-eager fan-out
        # caller can't bypass the throttle by calling
        # ``executor._do_submit(...)`` directly. Disabled limiters
        # short-circuit (TokenBucketRateLimiter.acquire returns
        # immediately when rate <= 0).
        limiter.acquire()
        return self._do_submit(
            fn,
            *args,
            name=name,
            cpus=cpus,
            memory_mb=memory_mb,
            time_min=time_min,
            container=container,
            container_digest=container_digest,
            openstudio_version=openstudio_version,
            result_hint=result_hint,
            remote_command=remote_command,
            result_transport_mode=result_transport_mode,
            result_storage_backend=result_storage_backend,
            result_storage_bucket=result_storage_bucket,
            result_storage_prefix=result_storage_prefix,
            result_storage_endpoint=result_storage_endpoint,
            variables_json=variables_json,
            env=env,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            max_retries=max_retries,
            worker_id=worker_id,
            **kwargs,
        )

    def _do_submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str,
        cpus: int,
        memory_mb: int,
        time_min: int,
        container: str | None,
        container_digest: str | None,
        openstudio_version: str | None,
        result_hint: Any,
        remote_command: str | None,
        result_transport_mode: str | None,
        result_storage_backend: str | None,
        result_storage_bucket: str | None,
        result_storage_prefix: str | None,
        result_storage_endpoint: str | None,
        variables_json: str | None,
        env: dict[str, str] | None,
        stdout_path: Any,
        stderr_path: Any,
        max_retries: int | None,
        worker_id: str | None,
        **kwargs: Any,
    ) -> Handle:
        """Substrate-specific submit. Called by :meth:`submit` after token acquisition.

        Concrete executors MUST override exactly one of the following two
        hooks (issue #1563, post-#1602 CI regression fix):

        * :meth:`_do_submit` (recommended) — the template-method seam that
          receives the per-call submit request *after* the rate limiter
          has admitted it. Every production executor
          (``AWSBatchExecutor``, ``AzureBatchExecutor``, ``GoogleBatchExecutor``,
          ``NomadExecutor``, ``KubernetesExecutor``, ``DockerSwarmExecutor``,
          ``PBSExecutor``, ``DaskJobQueueExecutor``, ``SlurmExecutor``,
          ``LocalExecutor``) implements this hook.
        * :meth:`submit` — the legacy full override. Kept working for
          backward compatibility with in-tree test stubs (e.g.
          ``tests/unit/test_resource_quota.py::_NoOpExecutor``,
          ``tests/unit/test_pause_lifecycle.py``, ``TestParetoTracking``
          in ``tests/unit/test_campaign.py``) that pre-date the
          template-method split. A stub that overrides ``submit()``
          bypasses the rate limiter by design — production executors
          must NOT do this.

        This method is intentionally *not* decorated with
        ``@abc.abstractmethod`` even though it raises
        ``NotImplementedError`` by default. The decorator would block
        instantiation of legacy test stubs that override ``submit()``
        directly (the parent's ``__abstractmethods__`` set would flag
        them as still abstract, raising ``TypeError`` at construction
        time — see the PR #1602 CI regression in
        ``tests/unit/test_resource_quota.py`` and friends).
        """
        raise NotImplementedError(
            f"{type(self).__name__} must override either _do_submit() (recommended, "
            f"rate-limited template-method seam) or submit() (legacy full override)."
        )

    def submit_request(self, request: SubmitRequest) -> Handle:
        """Submit a structured request (preferred over raw kwargs, issue #725).

        This is the type-safe path for ``executor.submit()`` calls.
        All fan-out submit calls in campaign.py should use this method
        rather than passing implicit ``**kwargs`` to ``submit()``.

        Args:
            request: A ``SubmitRequest`` dataclass describing the callable,
                positional args, and resource requirements.

        Returns:
            A ``Handle`` that can be used to retrieve the result.

        Raises:
            TypeError: If called with any kwargs (enforced by the
                ``submit_request`` overload signature).
            ValueError: If ``request.result_transport_mode`` is not a
                mode this executor supports per the transport capability
                matrix (issue #1473).
        """
        # Issue #1473: validate the declared per-executor transport
        # capability matrix instead of letting executors silently
        # discard an unsupported ``result_transport_mode``.
        validate_transport_mode(self.name, request.result_transport_mode)
        return self.submit(
            request.fn,
            *request.args,
            name=request.name,
            cpus=request.cpus,
            memory_mb=request.memory_mb,
            time_min=request.time_min,
            container=request.container,
            container_digest=request.container_digest,
            openstudio_version=request.openstudio_version,
            result_hint=request.result_hint,
            remote_command=request.remote_command,
            result_transport_mode=request.result_transport_mode,
            result_storage_backend=request.result_storage_backend,
            result_storage_bucket=request.result_storage_bucket,
            result_storage_prefix=request.result_storage_prefix,
            result_storage_endpoint=request.result_storage_endpoint,
            variables_json=request.variables_json,
            env=request.env,
            stdout_path=request.stdout_path,
            stderr_path=request.stderr_path,
            max_retries=request.max_retries,
            worker_id=request.worker_id,
        )

    def negotiate_contract_version(self) -> list[str]:
        """Return the remote runner's supported BYOS contract versions for version negotiation.

        This is the "hand-off" hook (issue #1331) that allows the Campaign to
        verify version compatibility *before* submitting work, rather than
        discovering a mismatch at runtime inside the remote container.

        The default implementation returns ``[]`` (no version info available),
        which means the runtime check inside the container will verify the version.
        Executors that use a remote-runner payload (Kubernetes, Nomad, etc.)
        should override this to actually query the container image.

        Raises
        ------
        RuntimeError
            If the remote runner is incompatible with the local BYOS contract
            version.
        """
        return []

    @property
    def supports_spot_market(self) -> bool:
        """Whether this executor supports spot/preemptible VMs with variable pricing.

        When True, the CampaignCostTracker estimates spot savings using
        the executor's pricing model. Subclasses that support spot markets
        (AWS Batch, Azure Batch, Google Batch) override this to return True.
        """
        return False

    @abc.abstractmethod
    def shutdown(self) -> None: ...

    def cancel(self) -> None:
        """Cancel all active futures (issue #255).

        Override in subclasses that manage their own job queues
        (Slurm, AWS Batch) to send cancellation signals to the
        underlying substrate. The base implementation is a no-op
        for executors that do not need explicit cancellation.
        """
        return None

    def fanout_submit_chunk_size(self, total: int) -> int:
        """Return the bounded chunk size for fan-out submission.

        Override in executors that need to limit the number of
        concurrent submissions (e.g., Nomad's rate-limiting).
        The default returns a bounded chunk size so that unbounded
        submission bursts are avoided even when the caller passes a
        very large ``total`` (issue #1342).
        """
        if total <= 0:
            return 1
        return min(total, max(1, (os.cpu_count() or 1) * 4, 50))

    def get_bounded_fanout_chunk_size(self, total: int) -> int:
        """Return the bounded chunk size; delegates to fanout_submit_chunk_size.

        This method exists so NomadExecutor can expose a
        fanout_submit_chunk_size property without losing the bounded
        computation. Subclasses that override fanout_submit_chunk_size
        should not need to override this.
        """
        return self.fanout_submit_chunk_size(total)

    def fanout_submit_interval_s(self) -> float:
        """Return the per-submit pacing interval for fan-out submission.

        Override in executors that need to pace submissions
        (e.g., Nomad's submit rate limiting). The default
        returns 0.0 (no pacing).
        """
        return 0.0

    # --- Shared remote-runner payload helpers (issue #1168) ---
    # Used by executors that dispatch via ``python -m osimflow.remote_runner``
    # (Nomad, Kubernetes, AWS Batch, Azure Batch, Google Batch, Docker Swarm).

    @staticmethod
    def _infer_step_name(submit_name: str) -> str:
        """Map a submit name to the remote_runner step identifier.

        The Campaign names fan-out tasks ``apply_<sid>`` / ``sim_<sid>``
        / ``kpi_<sid>`` and the single-shot steps ``aggregate`` / ``plots``;
        the remote runner resolves the work function from the step identifier.
        """
        lower = submit_name.lower()
        if lower.startswith("apply_"):
            return "apply"
        if lower.startswith("sim_"):
            return "sim"
        if lower.startswith("kpi_"):
            return "extract"
        if lower.startswith("aggregate"):
            return "aggregate"
        if lower.startswith("plots"):
            return "plots"
        return "unknown"

    @staticmethod
    def _encode_payload_value(value: Any) -> Any:  # noqa: ANN401
        """Encode Python values for transport-safe JSON payloads."""
        return encode_transport_value(value)

    @staticmethod
    def _build_task_payload(
        *,
        step_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        result_hint: Any,  # noqa: ANN401
        name: str,
    ) -> str:
        """Serialize the step call for the ephemeral runner.

        Uses the shared serialization so ``osimflow.remote_runner`` can
        decode any executor's Jobs identically (issue #996).
        """
        payload = {
            "schema_version": 1,
            "name": name,
            "step": step_name,
            "args": [BaseExecutor._encode_payload_value(a) for a in args],
            "kwargs": {k: BaseExecutor._encode_payload_value(v) for k, v in kwargs.items()},
            "result_hint": BaseExecutor._encode_payload_value(result_hint),
        }
        return json.dumps(payload)


# ---------------------------------------------------------------------------
# Executor registry state (issue #1463)
# ---------------------------------------------------------------------------
# Module-level dicts that back ``ExecutorRegistry``'s class attributes in
# ``osimflow/executors/__init__.py``. They are anchored here — not declared
# as class-level dict literals in the package init — because
# ``importlib.reload(osimflow.executors)`` re-executes the package init and
# would recreate class-level literals, silently dropping every executor
# registration and every health check registered from ``osimflow.health``.
# This module is imported once and cached by the import system, so aliasing
# these dicts keeps registry state stable across reloads of the package.
if TYPE_CHECKING:
    from osimflow.health import CheckResult

_EXECUTOR_REGISTRY: dict[str, type[BaseExecutor]] = {}
_EXECUTOR_HEALTH_CHECKS: dict[str, Callable[[], CheckResult]] = {}
