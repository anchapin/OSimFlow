"""Fan-out wait loop for Campaign per-sample steps (issue #1542).

This module extracts the concurrent fan-out machinery from
``osimflow.campaign`` (the ``_submit_and_await_all`` / ``_await_one``
core of issue #286, the per-step await deadline of issue #1566, the
recovery loop of issues #443 / #1567, and the centralized
failure-marking path of issue #1570):

- :class:`FanoutDeps` — the explicit dependency bundle.  The Campaign
  builds it **at call time** from live instance attributes so the
  historical test seams (``patch.object(campaign, "_job_queue")``,
  ``patch.object(campaign, "_checkpoint_sample")``, ...) keep
  working unchanged.
- :func:`mark_sample_failed` — the per-sample failure-recording path
  (issue #1570): job-queue ``mark_failed``, ``_sample_state`` keys,
  incremental checkpoint, observability metric, ``sample.failed``
  alert.
- :func:`compute_await_deadline` — the per-step orchestrator-side
  await deadline (issue #1566).
- :func:`submit_and_await_all` — enqueue, then await all submitted
  handles sequentially (``max_workers <= 1``) or via a
  ``ThreadPoolExecutor``, honouring cancellation, soft pause
  (issue #553 / #1537), worker auto-recovery (issue #443 / #1567),
  and the 3-strike ``CampaignAbortError`` propagation (issue #739 /
  #1539).

Issue #1542 rule: this collaborator must NOT import or
type-reference ``Campaign`` — every dependency is explicit and
duck-typed, so the wait loop has standalone unit tests
(``tests/unit/test_campaign_fanout.py``) that need no Campaign
instance.
"""

import concurrent.futures
import logging
from collections.abc import Callable
from typing import Any, Protocol

from ._campaign_lifecycle import CampaignPauseRequested
from ._campaign_observability import ObservabilityManager
from ._campaign_sample_trace import CampaignAbortError
from .config import CampaignConfig
from .executors import Handle, get_step_resources
from .monitoring import RunTrace, WorkerRecoveryManager

log = logging.getLogger("osimflow.campaign")


class _JobQueueLike(Protocol):
    """Structural slice of ``JobQueue`` / ``DistributedJobQueue`` the loop uses."""

    def enqueue(self, job_id: str, payload: dict[str, Any], priority: int = 0) -> Any: ...

    def mark_completed(self, job_id: str) -> None: ...

    def mark_failed(self, job_id: str, error: str) -> None: ...


class FanoutDeps:
    """Explicit dependencies for the fan-out wait loop (issue #1542).

    The Campaign's thin delegating methods build this bundle **at call
    time** (reading live attributes off ``self``) so instance-level
    patches applied after construction — the documented test seams —
    are honoured exactly as before the extraction.  Every field is a
    concrete collaborator or callback; there is deliberately no
    ``Campaign`` back-reference.
    """

    def __init__(
        self,
        *,
        cfg: CampaignConfig,
        trace: RunTrace,
        obs: ObservabilityManager,
        job_queue: _JobQueueLike,
        sample_state: dict[str, dict[str, object]],
        max_workers: int,
        effective_max_workers: Callable[[], int],
        compute_await_deadline: Callable[[str], float | None],
        trace_id_for: Callable[[str], str],
        checkpoint_sample: Callable[[str], None],
        mark_sample_failed: Callable[[str, str, BaseException, str], None],
        maybe_alert: Callable[[str, dict[str, Any]], None],
        check_cancel_requested: Callable[[], bool],
        cancel_requested: Callable[[], bool],
        check_pause_requested: Callable[[], bool],
        write_paused_trace: Callable[[], None],
    ) -> None:
        self.cfg = cfg
        self.trace = trace
        self.obs = obs
        self.job_queue = job_queue
        self.sample_state = sample_state
        self.max_workers = max_workers
        self.effective_max_workers = effective_max_workers
        self.compute_await_deadline = compute_await_deadline
        self.trace_id_for = trace_id_for
        self.checkpoint_sample = checkpoint_sample
        self.mark_sample_failed = mark_sample_failed
        self.maybe_alert = maybe_alert
        self.check_cancel_requested = check_cancel_requested
        self.cancel_requested = cancel_requested
        self.check_pause_requested = check_pause_requested
        self.write_paused_trace = write_paused_trace


def mark_sample_failed(
    deps: FanoutDeps,
    sid: str,
    step_name: str,
    error: BaseException,
    trace_id: str,
) -> None:
    """Record a per-sample failure for the given step (issue #1570).

    Centralises the failure-marking path so the await loop can route
    ``concurrent.futures.CancelledError`` and submitit cancellation
    errors through the same accounting as ordinary ``Exception``
    failures.  Without this helper, cancellation errors (which are
    ``BaseException`` subclasses, not ``Exception``) escaped the
    ``except Exception`` block and left the job-queue entry stuck
    ``in_progress``, the sample-state dict empty, no checkpoint
    row, no observability metric, and no ``sample.failed`` alert.

    The helper performs four side-effects, in order, each mirroring
    the pre-#1570 inline failure block:

    1. ``job_queue.mark_failed(...)`` (issue #263).
    2. ``sample_state[...]`` populated with ``<step>_exit_code=1``,
       ``<step>_status="failed"``, ``error_summary`` (issue #847).
    3. ``checkpoint_sample(sid)`` — incremental run.json write.
    4. ``obs.record_sample_status(sid, "failed", ...)`` + the
       ``sample.failed`` alert via ``maybe_alert`` (issue #1180).

    Must remain a no-throw on the accounting side: the 3-strike
    ``CampaignAbortError`` from ``checkpoint_sample`` propagates
    to the caller unchanged so the concurrent fan-out remains
    abortable.
    """
    # Mark failed in the job queue (issue #263).
    deps.job_queue.mark_failed(
        f"{sid}_{step_name}",
        str(error)[:500],
    )
    # Record failure in sample_state so _finalize_samples
    # and checkpoint_sample see a consistent failed sample.
    state = deps.sample_state.setdefault(sid, {})
    state[f"{step_name.lower()}_exit_code"] = 1
    state[f"{step_name.lower()}_status"] = "failed"
    state["error_summary"] = str(error)[:500]
    deps.checkpoint_sample(sid)
    # Record sample status to observability backend immediately
    # so crashed samples are not missed (issue #847).
    deps.obs.record_sample_status(sid, "failed", trace_id=trace_id)
    # Send sample failure alert (issue #1180).
    deps.maybe_alert(
        "sample.failed",
        {
            "campaign_id": deps.trace.campaign_id,
            "sample_id": sid,
            "step": step_name,
            "status": "failed",
            "error": str(error)[:500],
        },
    )


def compute_await_deadline(cfg: CampaignConfig, step_name: str) -> float | None:
    """Derive the per-step orchestrator-side await deadline (issue #1566).

    The deadline is the value passed to ``handle.result(timeout=...)``
    in :func:`submit_and_await_all`.  It guards against a wedged
    substrate (Nomad allocation that never reaches terminal, Docker
    Swarm service in a non-terminal state, K8s ``time_min=0`` yielding
    no ``activeDeadlineSeconds``) parking an await thread forever.

    The deadline combines three inputs, each guarding a different
    failure mode:

    1. ``cfg.await_timeout_s`` — the user-supplied orchestrator-side
       deadline (issue #1566, exposed as ``--sample-await-timeout-s``).
       When set, this is the deadline (honoured directly — a wedged
       substrate becomes a ``TimeoutError`` after *await_timeout_s*
       seconds).  This is the opt-in for Nomad / Docker Swarm /
       mis-configured K8s ``time_min=0`` users.
    2. ``DEFAULT_STEP_RESOURCES[step_name]["time_min"] * 60`` — the
       per-step substrate-aligned default (e.g. 240 s for
       ``RUN_OPENSTUDIO_SIM``, 10 s for ``EXTRACT_KPIS``).  Mirrors
       the ``time_min`` value the campaign hands the executor at
       ``submit()`` time, which most substrates translate into a
       substrate-side kill (Slurm ``--time``, K8s
       ``activeDeadlineSeconds``, AWS Batch ``timeout``).  Steps
       that are missing from :data:`DEFAULT_STEP_RESOURCES` fall
       back to a 60 s floor.
    3. ``cfg.byos_timeout_s`` — the BYOS subprocess watchdog
       (issue #1109).  ``None`` is treated as unbounded and is
       excluded from the maximum.

    Resolution order: when ``await_timeout_s`` is set, it is the
    deadline (the other two inputs would only add overhead on
    already-working substrates).  Otherwise the deadline is the
    maximum of the per-step ``time_min`` floor and ``byos_timeout_s``
    so the orchestrator-side bound accommodates the longest
    expected legitimate work.  Returns ``None`` only when the user
    opted out of every orchestrator-side bound (neither
    ``byos_timeout_s`` nor ``await_timeout_s`` is set) AND the
    per-step ``time_min`` resolved to ``0``/missing — i.e. the user
    is on a substrate without an executor-side kill and wants the
    pre-#1566 bare-``handle.result()`` semantics.

    The deadline is a **floor**, not an override: ``Handle.result``
    accepts ``timeout=None`` for unbounded waits, but when a value
    is supplied the substrate honours it strictly.  Callers that
    pass a longer deadline via the per-handle hook would still be
    honoured (this helper only fires when the campaign is the
    caller).
    """
    # User-supplied orchestrator-side deadline wins (issue #1566).
    # ``await_timeout_s=0`` is treated as unset (mirrors the
    # ``byos_timeout_s=0`` semantics — issue #1109 history).
    if cfg.await_timeout_s is not None and cfg.await_timeout_s > 0:
        return cfg.await_timeout_s
    candidates: list[float] = []
    # Substrate-aligned per-step default (DEFAULT_STEP_RESOURCES falls
    # back to {"time_min": 60} for unknown steps).  Use it as a floor
    # so a wedged Nomad allocation / Docker Swarm service / K8s
    # ``time_min=0`` misconfiguration becomes a TimeoutError instead
    # of an indefinite hang.
    step_resources = get_step_resources(step_name)
    time_min = step_resources.get("time_min", 0)
    if time_min and time_min > 0:
        candidates.append(float(time_min) * 60.0)
    # BYOS subprocess watchdog (issue #1109).  Excludes None — issue
    # #1534 explicitly lifted the old 600 s stock bound because annual
    # EnergyPlus runs routinely exceed it.
    if cfg.byos_timeout_s is not None and cfg.byos_timeout_s > 0:
        candidates.append(cfg.byos_timeout_s)
    if not candidates:
        return None
    return max(candidates)


def submit_and_await_all(
    deps: FanoutDeps,
    submissions: dict[str, tuple[Handle, Callable[[Any], None]]],
    step_name: str,
    recovery_manager: WorkerRecoveryManager | None = None,
    resubmit_callback: Callable[[str], Handle | None] | None = None,
) -> None:
    """Submit all samples to the executor, then await all results concurrently.

    This is the core of the concurrent fan-out fix (issue #286).

    Parameters
    ----------
    deps
        Explicit dependency bundle (see :class:`FanoutDeps`).  Built by
        the Campaign at call time so instance-level patches keep
        working.
    submissions
        Mapping of sample_id to (handle, on_success_callback).
        The ``handle`` has already been submitted to the executor.
        The ``on_success_callback`` receives the result of
        ``handle.result()`` and is responsible for updating
        ``_sample_state``, cache, and monitoring.
    step_name
        The step name for logging.
    recovery_manager
        Optional worker recovery manager for auto-recovery (issue #443).
        When provided and a job fails, the manager is consulted to check
        if the heartbeat is stale. If so and auto-recovery is enabled,
        the job is automatically resubmitted via resubmit_callback.
    resubmit_callback
        Optional callback to resubmit a failed job. Called with the
        sample_id when auto-recovery is triggered. Must return a new
        handle. Only used when recovery_manager is also provided.

    The method submits no new work — all submissions are already
    dispatched.  It awaits results using a
    ``concurrent.futures.ThreadPoolExecutor`` sized to
    ``deps.effective_max_workers()``, so up to that many results
    are collected in parallel (bounded by
    ``resource_quota.max_concurrent_samples`` when set — issue #1009).
    Each per-sample error is caught, logged with ``exc_info=True``,
    and recorded — it is never swallowed. The one exception is
    :class:`CampaignAbortError` (3-strike checkpoint-failure abort,
    issue #739/#1539): it is re-raised from the ``as_completed``
    loop so the abort crosses the worker-thread boundary in
    concurrent mode instead of being silently swallowed.

    For ``max_workers=1`` the behaviour is identical to the old
    sequential loop.

    Job queue integration (issue #263): each sample is enqueued
    before awaiting and marked completed/failed after.  The enqueue
    is idempotent — a sample that was already queued from a previous
    interrupted run is silently skipped.

    Worker auto-recovery (issue #443): when a job fails and
    recovery_manager is provided, the heartbeat is checked. If stale,
    the job is resubmitted automatically up to max_sample_retries.
    """
    if not submissions:
        return

    # Enqueue all samples for crash-recovery persistence (issue #263).
    for sid in submissions:
        deps.job_queue.enqueue(
            f"{sid}_{step_name}",
            {"sample_id": sid, "step": step_name},
        )

    # Per-step await deadline (issue #1566). Derived once per fan-out
    # rather than per-sample: every sample in this step shares the same
    # substrate-side ``time_min`` floor, so the deadline is identical.
    # ``None`` (default) means "no orchestrator-side bound" and the
    # pre-#1566 bare-``handle.result()`` semantics are preserved —
    # users on K8s/Slurm with a working ``activeDeadlineSeconds`` /
    # ``walltime`` continue to rely on the substrate-side kill; users
    # on Nomad / Docker Swarm / mis-configured ``time_min=0`` K8s opt
    # in via ``--sample-await-timeout-s`` (or the implicit
    # ``DEFAULT_STEP_RESOURCES`` floor).
    await_deadline = deps.compute_await_deadline(step_name)

    def _await_one(
        item: tuple[str, tuple[Handle, Callable[[Any], None]]],
    ) -> str:
        """Await one handle. Returns the sample_id."""
        sid, (handle, on_success) = item
        trace_id = deps.trace_id_for(sid)
        try:
            result = handle.result(timeout=await_deadline)
            on_success(result)
            # Mark completed in the job queue (issue #263).
            deps.job_queue.mark_completed(f"{sid}_{step_name}")
            # Reset recovery attempts on successful completion (issue #443).
            if recovery_manager is not None:
                recovery_manager.reset(sid)
        except Exception as e:
            log.error("%s %s failed: %s", step_name, sid, e, exc_info=True)

            # Worker auto-recovery (issue #443, #1567): loop the
            # resubmit until ``max_sample_retries`` is exhausted.
            # ``recovery_manager.check_and_recover`` gates each
            # iteration on heartbeat staleness and the recorded
            # attempt count, so the loop naturally terminates when
            # the retry budget is spent (or the worker comes back
            # alive and the heartbeat is no longer stale).
            recovered = False
            if recovery_manager is not None and resubmit_callback is not None:
                for _ in range(deps.cfg.max_sample_retries):
                    can_recover, attempt = recovery_manager.check_and_recover(
                        sid, deps.cfg.max_sample_retries
                    )
                    if not can_recover:
                        break
                    log.info(
                        "%s %s: stale heartbeat detected (attempt %d/%d), auto-recovering",
                        step_name,
                        sid,
                        attempt,
                        deps.cfg.max_sample_retries,
                    )
                    # Clear the failed state so recovery doesn't show as failed.
                    # NOTE: ``step_name.lower()`` must be called (issue #1567):
                    # the unparenthesised ``step_name.lower`` is the bound
                    # method object, not the lowercase string, so the prior
                    # ``state.pop`` calls targeted garbage keys and the
                    # clearing was dead code.
                    state = deps.sample_state.setdefault(sid, {})
                    state.pop(f"{step_name.lower()}_exit_code", None)
                    state.pop(f"{step_name.lower()}_status", None)
                    state.pop("error_summary", None)
                    # Resubmit and await the new handle.
                    new_handle = resubmit_callback(sid)
                    if new_handle is None:
                        log.error(
                            "%s %s auto-recovery: resubmit_callback returned None, "
                            "aborting retries",
                            step_name,
                            sid,
                        )
                        break
                    recovery_sid = sid

                    # Await the resubmitted handle.
                    try:
                        result = new_handle.result(timeout=await_deadline)
                    except Exception as resubmit_error:
                        log.error(
                            "%s %s auto-recovery attempt %d/%d failed: %s",
                            step_name,
                            sid,
                            attempt,
                            deps.cfg.max_sample_retries,
                            resubmit_error,
                            exc_info=True,
                        )
                        # Continue the loop: re-enter check_and_recover
                        # for the next attempt (issue #1567).
                        continue

                    # Create a new on_success callback wrapper for the resubmit.
                    def _on_success_resubmit(
                        result_path: Any,
                        _recovery_sid: str = recovery_sid,
                        _on_success: Callable[[Any], None] = on_success,
                    ) -> None:
                        _on_success(result_path)
                        if recovery_manager is not None:
                            recovery_manager.reset(_recovery_sid)

                    _on_success_resubmit(result)
                    deps.job_queue.mark_completed(f"{recovery_sid}_{step_name}")
                    deps.checkpoint_sample(recovery_sid)
                    recovered = True
                    return recovery_sid

            if not recovered:
                deps.mark_sample_failed(sid, step_name, e, trace_id)
            return sid
        except BaseException as e:  # noqa: BLE001 — intentional (issue #1570)
            # Cancellation / broken futures surface as ``BaseException``
            # subclasses (``concurrent.futures.CancelledError``,
            # submitit's cancellation error, ...) that explicitly
            # bypass ``except Exception`` in modern Python.  This
            # fallback runs only after ``except Exception`` above has
            # missed the exception, so it sees true cancellation
            # errors (not ordinary ``RuntimeError``/etc.).  Without
            # it the sample would skip the entire failure-recording
            # path and silently disappear from run.json /
            # failed_simulations.csv — exactly the crash/cancel
            # scenario the BYO-monitoring contract is supposed to
            # capture.  Genuine ``KeyboardInterrupt`` /
            # ``SystemExit`` are re-raised unchanged so the user
            # signal still propagates.
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            log.error(
                "%s %s cancelled: %s",
                step_name,
                sid,
                e,
                exc_info=True,
            )
            deps.mark_sample_failed(sid, step_name, e, trace_id)
            return sid
        # Incremental checkpoint: update run.json after each sample
        # completes so SSE clients see live progress (issue #275).
        deps.checkpoint_sample(sid)
        return sid

    # When max_workers == 1, use a sequential loop to avoid the
    # overhead of spinning up a ThreadPoolExecutor.  This preserves
    # the exact backward-compatible behaviour.
    if deps.max_workers <= 1:
        for item in submissions.items():
            if deps.check_cancel_requested():
                log.warning("cancellation requested during %s — stopping fan-out", step_name)
                break
            # Soft pause (issue #553): running samples complete, new ones are skipped.
            if deps.check_pause_requested():
                deps.write_paused_trace()
                # Dedicated pause signal (issue #1537): run()'s
                # KeyboardInterrupt handler maps to *cancellation*;
                # pause must keep status "paused" so `osimflow
                # resume` can continue the campaign.
                raise CampaignPauseRequested("pause requested during fan-out")
            _await_one(item)
        if deps.cancel_requested():
            raise KeyboardInterrupt("cancellation requested during fan-out")
        return

    # max_workers > 1: use a ThreadPoolExecutor to await results
    # concurrently.  Each _await_one call blocks on handle.result(),
    # so the pool parallelism effectively controls how many samples
    # we wait for at the same time.
    def _drain_future(future: concurrent.futures.Future[str]) -> None:
        """Collect one completed fan-out future (issue #1539).

        A CampaignAbortError raised inside an _await_one worker
        thread (3-strike checkpoint-failure abort, issue #739 /
        #1539) must cross the thread boundary: re-raise it so the
        campaign aborts instead of silently completing while its
        monitoring plane cannot persist run.json. Cancel
        not-yet-started futures so no new samples are awaited.
        """
        try:
            future.result()
        except CampaignAbortError:
            log.error(
                "%s fan-out aborted after consecutive checkpoint failures",
                step_name,
            )
            for f in futures:
                f.cancel()
            raise
        # CancelledError is a BaseException (not Exception), so we must
        # suppress it explicitly here — it is raised when a future was
        # cancelled via f.cancel() during a cancellation sweep.
        except (Exception, concurrent.futures.CancelledError):
            pass

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=deps.effective_max_workers(),
        thread_name_prefix="osimflow-fanout",
    ) as pool:
        futures = {pool.submit(_await_one, (sid, item)): sid for sid, item in submissions.items()}
        for future in concurrent.futures.as_completed(futures):
            if deps.check_cancel_requested():
                log.warning("cancellation requested during %s — stopping fan-out", step_name)
                # Cancel remaining futures.
                for f in futures:
                    f.cancel()
                break
            # Soft pause (issue #553): running samples complete, new ones are skipped.
            # Do NOT cancel futures — let in-flight work finish naturally.
            if deps.check_pause_requested():
                deps.write_paused_trace()
                log.warning("pause requested during %s — breaking fan-out", step_name)
                break
            _drain_future(future)
