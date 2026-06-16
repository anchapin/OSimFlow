"""Campaign orchestrator.

This is the ~300-line class that drives the campaign DAG.
The shape is:

    1. GENERATE_LHS_SAMPLES — one shot, no fan-out.
    2. PREFLIGHT_RUN_MODEL  — one shot, validates seed model (issue #107).
    3. APPLY_PARAMETERS     — fan out over N samples.
    4. RUN_OPENSTUDIO_SIM   — fan out over N samples (heavy).
    5. EXTRACT_KPIS         — fan out over N samples.
    6. AGGREGATE_RESULTS    — one shot after all KPIs.
    7. GENERATE_BASIC_PLOTS — one shot after aggregation.

Each step is cached via `SQLiteCache`. Each per-sample step submits to
the configured `BaseExecutor`. Fan-out is bounded by the executor's
max_workers so we do not overwhelm the underlying scheduler.

BYOS extension is exposed at the `apply_fn=` / `extract_fn=` constructor
parameters — the user supplies a Python file with a function of the right
signature and we discover + call it via `inspect.signature`.

When `apply_fn` / `extract_fn` are not passed explicitly, the Campaign
falls back to loading them from `cfg.custom_apply_script` /
`cfg.custom_kpi_extractor` via the canonical ``osimflow.byos`` loader.
This ensures `CampaignConfig.custom_apply_script` is always consumed,
even when the Campaign is constructed programmatically (without the CLI
doing the pre-load).

Per `.agents/results/monitoring-decision.md`, the campaign writes a
single `run.json` trace to `${outdir}/run.json` at completion. The trace
includes per-step timing, per-sample status, and cache hit/miss counts.
"""

import concurrent.futures
import contextlib
import dataclasses
import hashlib
import inspect
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import threading
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict

import yaml

from .algorithms import AlgorithmRegistry, BaseAlgorithm
from .apply_params import (
    EPW_FILE_KEY,
    _build_mappings,
    preflight_check,
)
from .cache import CacheKey, SQLiteCache, sha256_of_dict, sha256_of_files
from .config import CampaignConfig
from .distributed_jobqueue import build_job_queue
from .executors import AWSBatchExecutor, BaseExecutor, Handle
from .measures import MeasureRegistry, UnmappedVariableError
from .mlflow_hook import (
    log_mlflow_artifacts,
    log_mlflow_metrics,
    log_mlflow_params,
    maybe_end_mlflow_run,
    maybe_start_mlflow_run,
)
from .monitoring import (
    GenerationTrace,
    RunTrace,
    SampleTrace,
    WorkerRecoveryManager,
    sample_log_paths,
)
from .observability import (
    CloudWatchBackend,
    NullBackend,
    ObservabilityBackend,
    OpenTelemetryBackend,
    PrometheusBackend,
    new_trace_id,
)
from .pareto import ParetoFront, ParetoSolution
from .registry import CampaignRegistry
from .storage import ResultStorageUploader, build_result_storage
from .taskqueue import TaskHandle as TQHandle
from .taskqueue import TaskQueue
from .weather import EPWValidationError, validate_all_epw_files, validate_epw
from .webhook import WebhookClient
from .work import (
    SevereEnergyPlusError,
    aggregate_results,
    default_apply_parameters,
    extract_kpis,
    generate_plots,
    preflight_run_model,
    run_openstudio_sim,
)


def _osimflow_version() -> str:
    """Return the installed OSimFlow version, or 'unknown'."""
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version("osimflow")
    except Exception:
        return "unknown"


log = logging.getLogger("osimflow.campaign")

# Image registries. The OpenStudio CLI image is consumed directly from
# NREL's upstream `nrel/openstudio` on Docker Hub — see
# `docs/openstudio-image-distribution.md` and ADR-0002 for the rationale.
# The scientific Python image remains a project-owned ghcr.io artifact.
CONTAINER_OS = "docker.io/nrel/openstudio:{version}"
CONTAINER_PY = "ghcr.io/anchapin/scientific_python_image:latest"


class QuotaExceededError(RuntimeError):
    """Raised when a campaign resource quota is exceeded (issue #446)."""

    def __init__(
        self,
        message: str,
        quota_type: str,
        limit: int | float,
        current: int | float,
    ) -> None:
        super().__init__(message)
        self.quota_type = quota_type
        self.limit = limit
        self.current = current


# Type aliases — these are the schemas of intermediate DAG outputs.
class SampleSpec(TypedDict):
    sample_id: str
    values: dict[str, object]


class VariableSpec(TypedDict, total=False):
    name: str
    distribution: str
    min: float
    max: float
    mean: float
    sigma: float


SampleDict = dict[str, Path]  # sample_id -> path (per-sample work dir)


class _CancelRegistry:
    """Global registry holding the currently-running Campaign for signal handling.

    When a SIGINT/SIGTERM is received, the signal handler calls
    ``request_cancel()`` on whatever Campaign is registered here.
    Only one Campaign can run at a time per process — the registry
    is updated at ``run()`` entry and cleared on exit.
    """

    def __init__(self) -> None:
        self._campaign: Campaign | None = None
        self._lock = threading.Lock()

    def register(self, campaign: "Campaign") -> None:
        with self._lock:
            self._campaign = campaign

    def request_cancel(self) -> None:
        with self._lock:
            if self._campaign is not None:
                self._campaign.request_cancel()

    def clear(self) -> None:
        with self._lock:
            self._campaign = None


_cancel_registry = _CancelRegistry()


class Campaign:
    def __init__(
        self,
        cfg: CampaignConfig,
        executor: BaseExecutor,
        apply_fn: Callable[..., Path] | None = None,
        extract_fn: Callable[..., Path] | None = None,
        max_workers: int = 1,
        task_queue: TaskQueue | None = None,
    ):
        self.cfg = cfg
        self.executor = executor
        self.max_workers = max_workers
        self.task_queue = task_queue
        # Resolve apply_fn: explicit param > cfg.custom_apply_script > default.
        if apply_fn is not None:
            self.apply_fn = apply_fn
        elif cfg.custom_apply_script is not None:
            from .byos import load_user_function  # noqa: PLC0415

            log.info("loading BYOS apply_fn from %s", cfg.custom_apply_script)
            self.apply_fn = load_user_function(
                cfg.custom_apply_script, trust_level=cfg.byos_trust_level
            )
        else:
            self.apply_fn = default_apply_parameters
        # Resolve extract_fn: explicit param > cfg.custom_kpi_extractor > default.
        if extract_fn is not None:
            self.extract_fn = extract_fn
        elif cfg.custom_kpi_extractor is not None:
            from .byos import load_user_function  # noqa: PLC0415

            log.info("loading BYOS extract_fn from %s", cfg.custom_kpi_extractor)
            self.extract_fn = load_user_function(
                cfg.custom_kpi_extractor, trust_level=cfg.byos_trust_level
            )
        else:
            self.extract_fn = extract_kpis
        self.cache = SQLiteCache(cfg.cache_db)
        # Hash the code that affects per-step behavior so a `bin/*.py` edit
        # invalidates cached results. This is the fix for the
        # "Python glue invisible to cache hash" gotcha in
        # `.agents/results/result-architecture.md` issue #2.
        self.code_hashes = self._compute_code_hashes()
        self.trace = RunTrace(
            campaign_id=time.strftime("%Y-%m-%dT%H-%M-%S"),
            config_summary={
                "executor": executor.name,
                "openstudio_version": cfg.openstudio_version,
                "n_samples": cfg.n_samples,
                "archive_intermediates": cfg.archive_intermediates,
                "algorithm": cfg.algorithm,
                "custom_apply_script": (
                    str(cfg.custom_apply_script) if cfg.custom_apply_script else None
                ),
                "custom_kpi_extractor": (
                    str(cfg.custom_kpi_extractor) if cfg.custom_kpi_extractor else None
                ),
                "baseline_sample_id": (str(cfg.baseline["sample_id"]) if cfg.baseline else None),
            },
        )
        # Per-sample accumulator. The three per-sample steps write here;
        # we emit SampleTrace rows in _finalize_samples().
        self._sample_state: dict[str, dict[str, object]] = {}
        log.info("max_workers=%d (fan-out parallelism)", self.max_workers)
        # Job queue for crash recovery (issue #263) + distributed coordination
        # (issue #393). When redis_url is set, build_job_queue returns a
        # DistributedJobQueue that broadcasts state changes via Redis pub/sub,
        # enabling coherent job state across multi-node Slurm/AWS Batch
        # campaigns.  When redis_url is None, a plain JobQueue is used (single-
        # process crash recovery only).
        self._job_queue = build_job_queue(
            queue_dir=cfg.work_dir / "queue",
            redis_url=cfg.redis_url,
            campaign_id=self.trace.campaign_id,
        )
        # Observability backend (issue #132). Built from cfg so the
        # correct backend is always used — NullBackend when "none" (zero
        # overhead) or a real backend when configured.
        self._obs: ObservabilityBackend = self._build_observability_backend(cfg)
        # Campaign registry (issue #266). Auto-register on run start
        # and update status on completion.
        self._registry: CampaignRegistry | None = None
        reg_path = getattr(cfg, "registry_path", None)
        try:
            self._registry = CampaignRegistry(db_path=reg_path)
        except Exception as exc:
            log.warning("could not open campaign registry: %s (continuing without)", exc)

        # Result storage uploader for distributed campaigns (issue #339).
        # Built here so the correct backend is always used.  LocalStorage
        # is a no-op so there is zero overhead when result_storage_backend
        # is "local" (the default).
        self._result_storage: ResultStorageUploader | None = None
        if cfg.result_storage_backend != "local" and cfg.result_storage_bucket:
            try:
                store = build_result_storage(
                    backend=cfg.result_storage_backend,
                    bucket=cfg.result_storage_bucket,
                    prefix=str(cfg.outdir.name),
                    endpoint_url=cfg.result_storage_endpoint,
                )
                self._result_storage = ResultStorageUploader(store)
                log.info(
                    "result storage enabled: backend=%s bucket=%s prefix=%s",
                    cfg.result_storage_backend,
                    cfg.result_storage_bucket,
                    cfg.outdir.name,
                )
            except Exception as exc:
                log.warning(
                    "could not initialize result storage (%s:%s): %s — continuing without",
                    cfg.result_storage_backend,
                    cfg.result_storage_bucket,
                    exc,
                )

        # Graceful shutdown (issue #255): cancellation flag and lock.
        self._cancel_requested = False
        self._cancel_lock = threading.Lock()
        # Original signal handlers — restored on exit.
        self._prev_sigint: Any = None
        self._prev_sigterm: Any = None

    @staticmethod
    def _build_observability_backend(cfg: CampaignConfig) -> ObservabilityBackend:
        """Instantiate the correct observability backend from config.

        Returns NullBackend when ``cfg.observability == "none"`` (zero
        overhead — all methods are empty ``pass`` bodies).
        """
        backend_type = cfg.observability
        if backend_type == "none":
            return NullBackend()
        if backend_type == "cloudwatch":
            return CloudWatchBackend(
                namespace=cfg.cloudwatch_namespace,
            )
        if backend_type == "prometheus":
            return PrometheusBackend(
                pushgateway_url=f"localhost:{cfg.prometheus_port}",
            )
        if backend_type == "opentelemetry":
            endpoint = cfg.otel_endpoint or "http://localhost:4317"
            return OpenTelemetryBackend(endpoint=endpoint)
        raise ValueError(f"unknown observability backend: {backend_type}")

    def _enforce_start_quota(self) -> None:
        """Fail fast if the campaign's resource quota is already exceeded at start.

        Called at the beginning of ``run()`` before any work is dispatched.
        Raises ``QuotaExceededError`` with a descriptive message if any quota
        is already violated.
        """
        quota = self.cfg.resource_quota
        if quota is None:
            return

        if quota.max_samples is not None and self.cfg.n_samples > quota.max_samples:
            raise QuotaExceededError(
                f"n_samples={self.cfg.n_samples} exceeds resource_quota.max_samples="
                f"{quota.max_samples}",
                quota_type="max_samples",
                limit=quota.max_samples,
                current=self.cfg.n_samples,
            )

        log.info(
            "resource quota active: max_samples=%s, max_cost_usd=%s, "
            "max_wall_time_min=%s, max_concurrent_samples=%s",
            quota.max_samples,
            quota.max_cost_usd,
            quota.max_wall_time_min,
            quota.max_concurrent_samples,
        )

    def _check_quota_exceeded(self) -> bool:
        """Return True if any hard quota limit has been reached.

        Checks:
        - ``max_samples``: total samples submitted so far vs. the limit.
        - ``max_cost_usd``: accumulated campaign cost vs. the limit.
        - ``max_wall_time_min``: elapsed campaign time vs. the limit.

        Does NOT check ``max_concurrent_samples`` — that is enforced
        by bounding ``max_workers`` at construction time.
        """
        quota = self.cfg.resource_quota
        if quota is None:
            return False

        if quota.max_samples is not None:
            submitted = sum(
                1
                for state in self._sample_state.values()
                if any(k.endswith("_exit_code") or k.endswith("_status") for k in state)
            )
            if submitted >= quota.max_samples:
                log.warning(
                    "max_samples quota reached (%d >= %d) — skipping further submissions",
                    submitted,
                    quota.max_samples,
                )
                return True

        if quota.max_cost_usd is not None and self.trace.total_cost_usd >= quota.max_cost_usd:
            log.warning(
                "max_cost_usd quota reached (%.2f >= %.2f) — skipping further submissions",
                self.trace.total_cost_usd,
                quota.max_cost_usd,
            )
            return True

        elapsed_min = (time.time() - self.trace.started_at) / 60.0
        if quota.max_wall_time_min is not None and elapsed_min >= quota.max_wall_time_min:
            log.warning(
                "max_wall_time_min quota reached (%.1f >= %.1f min) — skipping further submissions",
                elapsed_min,
                quota.max_wall_time_min,
            )
            return True

        return False

    def _effective_max_workers(self) -> int:
        """Return the effective max_workers bounded by max_concurrent_samples quota.

        If a ``max_concurrent_samples`` quota is set, the fan-out parallelism
        is capped to that value. Otherwise, ``self.max_workers`` is returned
        unchanged.
        """
        quota = self.cfg.resource_quota
        if quota is not None and quota.max_concurrent_samples is not None:
            return min(self.max_workers, quota.max_concurrent_samples)
        return self.max_workers

    def _compute_code_hashes(self) -> dict[str, str]:
        """SHA-256 of every work script, plus the work.py module.

        The work scripts live in ``osimflow._work_scripts`` (shipped
        with the wheel).  A development checkout also has copies in
        ``bin/``; the hash covers whichever directory is found.
        The work.py module is included because it is the work layer that
        the Campaign itself depends on; if a contributor edits it, we
        must re-run downstream steps.
        """
        from . import work  # noqa: PLC0415

        # Resolve the work-scripts directory.
        scripts_dir = Path(__file__).resolve().parent / "_work_scripts"
        if not scripts_dir.is_dir():  # noqa: SIM108
            # Development fallback: repo root bin/ directory.
            scripts_dir = Path(__file__).resolve().parent.parent / "bin"
        files = sorted(scripts_dir.glob("*.py"))
        work_file = Path(inspect.getfile(work))
        return {
            "bin": sha256_of_files(files),
            "work": sha256_of_files([work_file]),
        }

    def _trace_id_for(self, sample_id: str) -> str:
        """Return the per-sample trace ID, minting one on first access.

        The trace ID is stored in ``_sample_state[sample_id]["trace_id"]``
        so every observability call for this sample (cost, status,
        per-step fan-out events) shares the same correlation key.  Minted
        lazily via :func:`osimflow.observability.new_trace_id` — short
        (8 hex chars) and stable across cache hits, retries, and
        incremental checkpoints (issue #436).
        """
        state = self._sample_state.setdefault(sample_id, {})
        tid_obj = state.get("trace_id")
        if isinstance(tid_obj, str):
            return tid_obj
        tid = new_trace_id()
        state["trace_id"] = tid
        return tid

    def _finalize_samples(self) -> None:
        """Emit one SampleTrace per sample based on accumulated per-step state.

        Also records per-sample observability metrics (duration, status)
        via the configured backend (issue #132).

        Deduplicates against incremental checkpoints: if a sample was
        already written to run.json by _checkpoint_sample (via SSE live
        updates), the existing entry is replaced rather than appended,
        so the per_sample list never grows faster than the sample count.
        """
        existing_ids: set[str] = {s.sample_id for s in self.trace.per_sample}
        for sid, state in self._sample_state.items():
            apply_ok = state.get("apply_exit_code") == 0
            sim_ok = state.get("sim_exit_code") == 0
            extract_ok = state.get("extract_exit_code") == 0
            # A sample is "ok" if every step that ran succeeded.
            status = "ok" if apply_ok and sim_ok and extract_ok else "failed"
            # Coerce optional stringy fields via str() rather than dropping
            # non-None values: previous code accepted any truthy value, and
            # JSON-serializing Path/str objects in run.json requires str().
            eplusout_sql_obj = state.get("eplusout_sql")
            eplusout_sql = None if eplusout_sql_obj is None else str(eplusout_sql_obj)
            error_summary_obj = state.get("error_summary")
            error_summary = None if error_summary_obj is None else str(error_summary_obj)
            # Per-sample log paths (issue #6). Optional because the
            # fields are only populated by RUN_OPENSTUDIO_SIM; samples
            # that errored out in APPLY_PARAMETERS never reach that
            # step and have no associated log files.
            stdout_log_obj = state.get("stdout_log")
            stdout_log = None if stdout_log_obj is None else str(stdout_log_obj)
            stderr_log_obj = state.get("stderr_log")
            stderr_log = None if stderr_log_obj is None else str(stderr_log_obj)
            # Worker tracking (issue #105): extract from per-sample state.
            worker_id_obj = state.get("worker_id")
            worker_id = None if worker_id_obj is None else str(worker_id_obj)
            worker_ip_obj = state.get("worker_ip")
            worker_ip = None if worker_ip_obj is None else str(worker_ip_obj)
            worker_region_obj = state.get("worker_region")
            worker_region = None if worker_region_obj is None else str(worker_region_obj)
            # Cost tracking (issue #126): extract from per-sample state.
            cost_usd_obj = state.get("cost_usd")
            cost_usd: float | None = None if cost_usd_obj is None else float(str(cost_usd_obj))
            billed_duration_obj = state.get("billed_duration_seconds")
            billed_duration_seconds: float | None = (
                None if billed_duration_obj is None else float(str(billed_duration_obj))
            )
            # Observability: record per-sample cost metric (issue #132).
            # Forward the per-sample trace_id so the metric can be joined
            # to a distributed trace (issue #436).
            trace_id = self._trace_id_for(sid)
            if cost_usd is not None:
                self._obs.record_sample_metric(sid, "cost_usd", cost_usd, trace_id=trace_id)
            trace = SampleTrace(
                sample_id=sid,
                status=status,
                elapsed_s=0.0,  # per-sample total wall-clock — not yet tracked
                apply_exit_code=int(str(state.get("apply_exit_code", 0))),
                sim_exit_code=int(str(state.get("sim_exit_code", 0))),
                extract_exit_code=int(str(state.get("extract_exit_code", 0))),
                eplusout_sql=eplusout_sql,
                error_summary=error_summary,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                worker_id=worker_id,
                worker_ip=worker_ip,
                worker_region=worker_region,
                cost_usd=cost_usd,
                billed_duration_seconds=billed_duration_seconds,
                trace_id=trace_id,
            )
            # Deduplicate: replace existing entry from incremental checkpoint.
            if sid in existing_ids:
                for i, existing in enumerate(self.trace.per_sample):
                    if existing.sample_id == sid:
                        self.trace.per_sample[i] = trace
                        break
            else:
                self.trace.per_sample.append(trace)
                existing_ids.add(sid)
            # Observability: record per-sample status metric (issue #132).
            # status="ok" → 1.0, status="failed" → 0.0.  Forward the
            # trace_id so the status metric joins the cost metric under
            # the same per-sample trace (issue #436).
            self._obs.record_sample_metric(
                sid,
                "status",
                1.0 if status == "ok" else 0.0,
                trace_id=trace_id,
            )

        # Accumulate campaign-level cost totals (issue #126).
        self._accumulate_cost_summary()

    def _accumulate_cost_summary(self) -> None:
        """Sum per-sample costs into campaign-level totals (issue #126).

        Populates ``self.trace.total_cost_usd`` and
        ``self.trace.spot_savings_usd`` from the individual
        ``SampleTrace.cost_usd`` values.  Non-cloud executors produce
        ``None`` costs, so both totals remain at 0.0 for local runs.
        """
        total = 0.0
        for sample in self.trace.per_sample:
            if sample.cost_usd is not None:
                total += sample.cost_usd
        self.trace.total_cost_usd = round(total, 6)
        # Spot savings is the difference between on-demand and spot.
        # The executor already uses on-demand pricing in cost_usd;
        # spot_savings is the theoretical savings if the job ran on Spot
        # instead. For simplicity, we estimate this as a fixed fraction
        # (~40%) of total on-demand cost, matching the default pricing
        # ratio ($0.05 on-demand vs $0.03 spot).
        if total > 0:
            savings_ratio = (
                AWSBatchExecutor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR
                - AWSBatchExecutor.DEFAULT_SPOT_PRICE_PER_VCPU_HOUR
            ) / AWSBatchExecutor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR
            self.trace.spot_savings_usd = round(total * savings_ratio, 6)
        else:
            self.trace.spot_savings_usd = 0.0

    def _checkpoint_sample(self, sid: str) -> None:
        """Write an incremental run.json checkpoint for a single sample.

        Called after each sample completes (success or failure) inside
        the fan-out loop so SSE clients see live progress without waiting
        for campaign end (issue #275).

        The checkpoint updates only the per-sample entry for *sid* using
        atomic write (temp file + rename).  If run.json does not exist yet
        (campaign not started), this is a no-op.
        """
        state = self._sample_state.get(sid)
        if state is None:
            return

        apply_ok = state.get("apply_exit_code") == 0
        sim_ok = state.get("sim_exit_code") == 0
        extract_ok = state.get("extract_exit_code") == 0
        status = "ok" if apply_ok and sim_ok and extract_ok else "failed"

        eplusout_sql_obj = state.get("eplusout_sql")
        eplusout_sql = None if eplusout_sql_obj is None else str(eplusout_sql_obj)
        error_summary_obj = state.get("error_summary")
        error_summary = None if error_summary_obj is None else str(error_summary_obj)
        stdout_log_obj = state.get("stdout_log")
        stdout_log = None if stdout_log_obj is None else str(stdout_log_obj)
        stderr_log_obj = state.get("stderr_log")
        stderr_log = None if stderr_log_obj is None else str(stderr_log_obj)
        worker_id_obj = state.get("worker_id")
        worker_id = None if worker_id_obj is None else str(worker_id_obj)
        worker_ip_obj = state.get("worker_ip")
        worker_ip = None if worker_ip_obj is None else str(worker_ip_obj)
        worker_region_obj = state.get("worker_region")
        worker_region = None if worker_region_obj is None else str(worker_region_obj)
        cost_usd_obj = state.get("cost_usd")
        cost_usd: float | None = None if cost_usd_obj is None else float(str(cost_usd_obj))
        billed_duration_obj = state.get("billed_duration_seconds")
        billed_duration_seconds: float | None = (
            None if billed_duration_obj is None else float(str(billed_duration_obj))
        )

        trace = SampleTrace(
            sample_id=sid,
            status=status,
            elapsed_s=0.0,
            apply_exit_code=int(str(state.get("apply_exit_code", 0))),
            sim_exit_code=int(str(state.get("sim_exit_code", 0))),
            extract_exit_code=int(str(state.get("extract_exit_code", 0))),
            eplusout_sql=eplusout_sql,
            error_summary=error_summary,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            worker_id=worker_id,
            worker_ip=worker_ip,
            worker_region=worker_region,
            cost_usd=cost_usd,
            billed_duration_seconds=billed_duration_seconds,
            trace_id=self._trace_id_for(sid),
        )
        self.trace.update_sample(trace)

    def _submit_and_await_all(
        self,
        submissions: dict[str, tuple[Handle | TQHandle, Callable[[Any], None]]],
        step_name: str,
        recovery_manager: WorkerRecoveryManager | None = None,
        resubmit_callback: Callable[[str], Handle | TQHandle | None] | None = None,
    ) -> None:
        """Submit all samples to the executor, then await all results concurrently.

        This is the core of the concurrent fan-out fix (issue #286).

        Parameters
        ----------
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
        ``self.max_workers``, so up to ``max_workers`` results are
        collected in parallel.  Each per-sample error is caught, logged
        with ``exc_info=True``, and recorded — it is never swallowed.

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
            self._job_queue.enqueue(
                f"{sid}_{step_name}",
                {"sample_id": sid, "step": step_name},
            )

        def _await_one(
            item: tuple[str, tuple[Handle | TQHandle, Callable[[Any], None]]],
        ) -> str:
            """Await one handle. Returns the sample_id."""
            sid, (handle, on_success) = item
            try:
                result = handle.result()
                on_success(result)
                # Mark completed in the job queue (issue #263).
                self._job_queue.mark_completed(f"{sid}_{step_name}")
                # Reset recovery attempts on successful completion (issue #443).
                if recovery_manager is not None:
                    recovery_manager.reset(sid)
            except Exception as e:
                log.error("%s %s failed: %s", step_name, sid, e, exc_info=True)

                # Worker auto-recovery (issue #443): check if heartbeat is stale.
                # If stale and auto-recovery is enabled, attempt resubmission.
                if recovery_manager is not None and resubmit_callback is not None:
                    can_recover, attempt = recovery_manager.check_and_recover(
                        sid, self.cfg.max_sample_retries
                    )
                    if can_recover:
                        log.info(
                            "%s %s: stale heartbeat detected (attempt %d/%d), auto-recovering",
                            step_name,
                            sid,
                            attempt,
                            self.cfg.max_sample_retries,
                        )
                        # Clear the failed state so recovery doesn't show as failed.
                        state = self._sample_state.setdefault(sid, {})
                        state.pop(f"{step_name.lower}_exit_code", None)
                        state.pop(f"{step_name.lower}_status", None)
                        state.pop("error_summary", None)
                        # Resubmit and await the new handle.
                        new_handle = resubmit_callback(sid)
                        if new_handle is not None:
                            # Capture sid for use in the resubmit logic.
                            recovery_sid = sid

                            # Create a new on_success callback wrapper for the resubmit.
                            def _on_success_resubmit(
                                result_path: Any,
                                _recovery_sid: str = recovery_sid,
                                _on_success: Callable[[Any], None] = on_success,
                            ) -> None:
                                _on_success(result_path)
                                if recovery_manager is not None:
                                    recovery_manager.reset(_recovery_sid)

                            # Await the resubmitted handle.
                            try:
                                result = new_handle.result()
                                _on_success_resubmit(result)
                                self._job_queue.mark_completed(f"{recovery_sid}_{step_name}")
                                self._checkpoint_sample(recovery_sid)
                                return recovery_sid
                            except Exception as resubmit_error:
                                log.error(
                                    "%s %s auto-recovery failed: %s",
                                    step_name,
                                    sid,
                                    resubmit_error,
                                    exc_info=True,
                                )
                                # Fall through to mark as failed.

                # Mark failed in the job queue (issue #263).
                self._job_queue.mark_failed(
                    f"{sid}_{step_name}",
                    str(e)[:500],
                )
                # Record failure in _sample_state so _finalize_samples
                # and _checkpoint_sample see a consistent failed sample.
                state = self._sample_state.setdefault(sid, {})
                state[f"{step_name.lower}_exit_code"] = 1
                state[f"{step_name.lower}_status"] = "failed"
                state["error_summary"] = str(e)[:500]
                self._checkpoint_sample(sid)
                return sid
            # Incremental checkpoint: update run.json after each sample
            # completes so SSE clients see live progress (issue #275).
            self._checkpoint_sample(sid)
            return sid

        # When max_workers == 1, use a sequential loop to avoid the
        # overhead of spinning up a ThreadPoolExecutor.  This preserves
        # the exact backward-compatible behaviour.
        if self.max_workers <= 1:
            for item in submissions.items():
                if self._check_cancel_requested():
                    log.warning("cancellation requested during %s — stopping fan-out", step_name)
                    break
                _await_one(item)
            if self._cancel_requested:
                raise KeyboardInterrupt("cancellation requested during fan-out")
            return

        # max_workers > 1: use a ThreadPoolExecutor to await results
        # concurrently.  Each _await_one call blocks on handle.result(),
        # so the pool parallelism effectively controls how many samples
        # we wait for at the same time.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="osimflow-fanout",
        ) as pool:
            futures = {
                pool.submit(_await_one, (sid, item)): sid for sid, item in submissions.items()
            }
            for future in concurrent.futures.as_completed(futures):
                if self._check_cancel_requested():
                    log.warning("cancellation requested during %s — stopping fan-out", step_name)
                    # Cancel remaining futures.
                    for f in futures:
                        f.cancel()
                    break
                # CancelledError is a BaseException (not Exception), so we must
                # suppress it explicitly here — it is raised when a future was
                # cancelled via f.cancel() during a cancellation sweep.
                with contextlib.suppress(Exception, concurrent.futures.CancelledError):
                    future.result()

    def _compute_baseline_comparison(self, kpi_files: list[Path]) -> None:
        """Compute baseline comparison metrics and store on the run trace.

        Reads the baseline sample's KPIs and computes improvement statistics
        across all parametric samples. Populates ``self.trace.baseline_comparison``
        (issue #64).

        When no baseline is configured, this is a no-op.
        """
        baseline_sid = self._baseline_sample_id()
        if baseline_sid is None:
            return

        all_kpis = self._read_all_kpis(kpi_files)
        if baseline_sid not in all_kpis:
            log.warning(
                "baseline sample_id=%s not found in KPI files; skipping baseline comparison",
                baseline_sid,
            )
            return

        baseline_kpis = all_kpis[baseline_sid]
        comparison = self._compute_improvement_range(baseline_sid, baseline_kpis, all_kpis)
        if comparison:
            self.trace.baseline_comparison = comparison
            log.info("baseline comparison: %s", comparison)

    @staticmethod
    def _read_all_kpis(kpi_files: list[Path]) -> dict[str, dict[str, float]]:
        """Read KPI files into a {sample_id: {kpi_name: value}} mapping."""
        all_kpis: dict[str, dict[str, float]] = {}
        for kpi_path in kpi_files:
            try:
                data = json.loads(kpi_path.read_text())
                sid = str(data.get("sample_id", kpi_path.stem.replace("kpi_", "")))
                kpis = data.get("kpis", {})
                numeric_kpis = {k: float(v) for k, v in kpis.items() if isinstance(v, (int, float))}
                all_kpis[sid] = numeric_kpis
            except Exception as exc:
                log.warning("could not read KPI file %s for baseline comparison: %s", kpi_path, exc)
        return all_kpis

    @staticmethod
    def _compute_improvement_range(
        baseline_sid: str,
        baseline_kpis: dict[str, float],
        all_kpis: dict[str, dict[str, float]],
    ) -> dict[str, object]:
        """Compute pct improvement range for each KPI relative to baseline."""
        comparison: dict[str, object] = {}
        for kpi_name, baseline_val in baseline_kpis.items():
            if baseline_val == 0:
                continue
            parametric_values = [
                kpis[kpi_name]
                for sid, kpis in all_kpis.items()
                if sid != baseline_sid and kpi_name in kpis
            ]
            if not parametric_values:
                continue
            improvements = [(baseline_val - v) / baseline_val * 100.0 for v in parametric_values]
            comparison[f"baseline_{kpi_name}"] = round(baseline_val, 2)
            comparison[f"min_{kpi_name}_improvement_pct"] = round(min(improvements), 2)
            comparison[f"max_{kpi_name}_improvement_pct"] = round(max(improvements), 2)
        return comparison

    def _archive_sample_artifacts(self, src: Path, dst: Path, patterns: list[str]) -> None:
        """Copy files matching *patterns* from *src* into *dst*.

        Creates *dst* (with parents) and copies each file whose name
        matches one of the glob *patterns*.  Uses ``shutil.copy2`` so
        timestamps are preserved (cross-substrate robustness: works on
        local, NFS, and any substrate that exposes a POSIX filesystem).

        This is a private DRY helper called from the archive-aware step
        methods when ``cfg.archive_intermediates`` is ``True``.
        """
        dst.mkdir(parents=True, exist_ok=True)
        for pattern in patterns:
            for f in src.glob(pattern):
                if f.is_file():
                    shutil.copy2(f, dst / f.name)
                    log.debug("archived %s -> %s", f, dst / f.name)

    def _baseline_sample_id(self) -> str | None:
        """Return the baseline sample_id from config, or None."""
        if self.cfg.baseline is None:
            return None
        return str(self.cfg.baseline.get("sample_id", "baseline"))

    # ------------------------------------------------------------------
    # EPW file helpers (issue #55)
    # ------------------------------------------------------------------
    def _load_variable_defs(self) -> list[dict[str, Any]]:
        """Load variable definitions from ``cfg.input_variables`` (variables.yml).

        Returns the raw ``variables`` list so the Campaign can inspect
        ``target`` and ``mapping`` metadata that the LHS generator does
        not propagate to the sample dicts.
        """
        try:
            raw: Any = yaml.safe_load(self.cfg.input_variables.read_text())
        except Exception as exc:
            log.error("Failed to load variables.yml: %s", exc)
            raise
        if not isinstance(raw, dict):
            return []
        variables: Any = raw.get("variables", [])
        if not isinstance(variables, list):
            return []
        return variables

    @staticmethod
    def _collect_epw_mappings(
        variable_defs: list[dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Collect (variable_name, mapping_dict) for all epw_file targets."""
        result: list[tuple[str, dict[str, Any]]] = []
        for var in variable_defs:
            if var.get("target") != "epw_file":
                continue
            mapping = var.get("mapping")
            if mapping and isinstance(mapping, dict):
                result.append((var["name"], mapping))
        return result

    def _preflight_validate_epw_files(self, variable_defs: list[dict[str, Any]]) -> None:
        """Pre-flight check: verify all mapped .epw files exist and are valid.

        For every variable with ``target: epw_file`` and a ``mapping``
        dict, verify each mapped value is a file that exists inside
        the ``template_sim_package`` directory.  Fail fast with a
        clear error message listing every missing file.

        Additionally, validates the EPW format of each referenced file
        (header line starts with ``LOCATION``) so that malformed weather
        files are caught before any simulations start (issue #63).

        After validating the explicitly-referenced files, also validates
        all ``.epw`` files found in the ``weather/`` subdirectory of the
        template package (configurable via ``cfg.weather_dir``).

        Raises:
            FileNotFoundError: one or more mapped .epw files are missing.
            EPWValidationError: one or more .epw files fail format validation.
        """
        template_dir = self.cfg.template_sim_package
        epw_mappings = self._collect_epw_mappings(variable_defs)

        # Phase 1: check existence of all mapped .epw files.
        self._check_epw_existence(epw_mappings, template_dir)

        # Phase 2: validate EPW format for all referenced + discovered files.
        self._check_epw_format(epw_mappings, template_dir)

    @staticmethod
    def _check_epw_existence(
        epw_mappings: list[tuple[str, dict[str, Any]]],
        template_dir: Path,
    ) -> None:
        """Raise FileNotFoundError if any mapped .epw file is missing."""
        missing: list[str] = []
        for var_name, mapping in epw_mappings:
            for cat_value, epw_rel_path in mapping.items():
                epw_abs = template_dir / str(epw_rel_path)
                if not epw_abs.is_file():
                    missing.append(
                        f"  variable={var_name!r} value={cat_value!r} -> {epw_abs} (missing)"
                    )
        if missing:
            raise FileNotFoundError(
                "PRE-FLIGHT EPW VALIDATION FAILED: the following mapped "
                ".epw files were not found in template_sim_package="
                f"{template_dir}:\n" + "\n".join(missing)
            )

    def _check_epw_format(
        self,
        epw_mappings: list[tuple[str, dict[str, Any]]],
        template_dir: Path,
    ) -> None:
        """Raise EPWValidationError if any .epw file has invalid format."""
        format_errors: list[str] = []
        for _var_name, mapping in epw_mappings:
            for _cat_value, epw_rel_path in mapping.items():
                epw_abs = template_dir / str(epw_rel_path)
                try:
                    validate_epw(epw_abs)
                except EPWValidationError as exc:
                    format_errors.append(f"  {exc}")

        # Also validate all EPW files in the weather subdirectory (issue #63).
        try:
            validate_all_epw_files(template_dir, self.cfg.weather_dir)
        except EPWValidationError as exc:
            format_errors.append(f"  {exc}")

        if format_errors:
            raise EPWValidationError(
                "PRE-FLIGHT EPW FORMAT VALIDATION FAILED: the following "
                ".epw files have invalid format:\n" + "\n".join(format_errors)
            )

    def _resolve_epw_targets(
        self,
        params: dict[str, object],
        variable_defs: list[dict[str, Any]],
    ) -> dict[str, object]:
        """Resolve ``epw_file`` targets in a sample's parameter dict.

        For each variable with ``target: epw_file``, look up the
        parameter value in the variable's ``mapping`` dict and inject
        the resolved .epw path under :data:`EPW_FILE_KEY`
        (``"__epw_file__"``) into a copy of *params*.

        Categorical variables produce structured dicts (``{"label": ...,
        "index": ...}``); this method extracts the ``label`` for
        mapping lookups so the downstream resolution works transparently.

        If no ``epw_file`` targets exist, returns *params* unchanged.

        Raises:
            ValueError: a parameter value is not in the variable's mapping.
        """
        epw_vars = [v for v in variable_defs if v.get("target") == "epw_file"]
        if not epw_vars:
            return params
        resolved = dict(params)
        for var in epw_vars:
            name = var["name"]
            mapping = var.get("mapping", {})
            raw_value = params.get(name)
            if raw_value is None:
                continue
            # Categorical variables produce structured dicts; extract the label.
            if isinstance(raw_value, dict) and "label" in raw_value:
                value = raw_value["label"]
            else:
                value = raw_value
            epw_path = mapping.get(value)
            if epw_path is None:
                raise ValueError(
                    f"Parameter {name!r} has value {value!r} which is "
                    f"not in the epw_file mapping. Available keys: "
                    f"{sorted(mapping.keys())}"
                )
            resolved[EPW_FILE_KEY] = str(epw_path)
            log.debug(
                "resolved epw_file: %s=%s -> %s",
                name,
                value,
                epw_path,
            )
        return resolved

    # ------------------------------------------------------------------
    # Shell hooks (issue #108)
    # ------------------------------------------------------------------
    def _run_init_script(self) -> None:
        """Run the init script before the first campaign step.

        Raises ``subprocess.CalledProcessError`` if the script exits
        non-zero, which aborts the campaign.
        """
        script = self.cfg.init_script
        if script is None:
            return
        if not script.is_file():
            raise FileNotFoundError(f"init script not found: {script}")
        env = self._hook_env()
        log.info("running init script: %s", script)
        t0 = time.time()
        result = subprocess.run(  # noqa: S603
            [str(script)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        elapsed = time.time() - t0
        self.trace.init_script_duration_s = elapsed
        if result.stdout:
            for line in result.stdout.splitlines():
                log.info("init-script stdout: %s", line)
        if result.stderr:
            for line in result.stderr.splitlines():
                log.info("init-script stderr: %s", line)
        log.info("init script completed in %.2fs", elapsed)

    def _run_finalize_script(self, status: str, duration_s: float) -> None:
        """Run the finalize script after the last campaign step.

        Best-effort: a non-zero exit code is logged but does NOT raise.
        """
        script = self.cfg.finalize_script
        if script is None:
            return
        if not script.is_file():
            log.warning("finalize script not found: %s — skipping", script)
            return
        env = self._hook_env()
        env["OSIMFLOW_STATUS"] = status
        env["OSIMFLOW_DURATION_S"] = f"{duration_s:.2f}"
        log.info("running finalize script: %s", script)
        t0 = time.time()
        try:
            result = subprocess.run(  # noqa: S603
                [str(script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            elapsed = time.time() - t0
            self.trace.finalize_script_duration_s = elapsed
            if result.stdout:
                for line in result.stdout.splitlines():
                    log.info("finalize-script stdout: %s", line)
            if result.stderr:
                for line in result.stderr.splitlines():
                    log.info("finalize-script stderr: %s", line)
            if result.returncode != 0:
                log.warning(
                    "finalize script exited %d (best-effort — continuing)",
                    result.returncode,
                )
            else:
                log.info("finalize script completed in %.2fs", elapsed)
        except Exception as exc:
            elapsed = time.time() - t0
            self.trace.finalize_script_duration_s = elapsed
            log.warning("finalize script error: %s (best-effort — continuing)", exc)

    def _hook_env(self) -> dict[str, str]:
        """Build the environment dict for hook scripts."""
        base = dict(os.environ)
        base["OSIMFLOW_OUTDIR"] = str(self.cfg.outdir)
        base["OSIMFLOW_N_SAMPLES"] = str(self.cfg.n_samples)
        base["OSIMFLOW_EXECUTOR"] = self.executor.name
        base["OSIMFLOW_ALGORITHM"] = self.cfg.algorithm
        return base

    # ------------------------------------------------------------------
    # Manifest writers (issue #277)
    # ------------------------------------------------------------------
    def _write_campaign_meta(self) -> None:
        """Write ``campaign_meta.json`` to outdir at campaign start.

        Captures the campaign configuration in a queryable JSON form so
        downstream tools (dashboards, comparators, auditors) can inspect
        a campaign without parsing CLI args or run.json.

        The file is overwritten on each run so re-runs produce the
        latest configuration snapshot.
        """
        # Build input_variables summary from variables.yml.
        variable_summary: list[dict[str, object]] = []
        try:
            raw: Any = yaml.safe_load(self.cfg.input_variables.read_text())
            if isinstance(raw, dict):
                for var in raw.get("variables", []):
                    if isinstance(var, dict) and "name" in var:
                        entry: dict[str, object] = {
                            "name": var["name"],
                            "distribution": var.get("distribution", "unknown"),
                        }
                        for key in ("min", "max", "mean", "sigma", "mode", "steps"):
                            if key in var:
                                entry[key] = var[key]
                        variable_summary.append(entry)
        except Exception as exc:
            log.warning("could not parse variables.yml for campaign_meta: %s", exc)

        meta: dict[str, object] = {
            "campaign_id": self.trace.campaign_id,
            "algorithm": self.cfg.algorithm,
            "n_samples": self.cfg.n_samples,
            "openstudio_version": self.cfg.openstudio_version,
            "executor_type": self.executor.name,
            "input_variables": {
                "path": str(self.cfg.input_variables),
                "variables": variable_summary,
            },
            "template_sim_package": str(self.cfg.template_sim_package),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "osimflow_version": _osimflow_version(),
        }
        out_path = self.cfg.outdir / "campaign_meta.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(meta, indent=2, default=str))
        log.info("wrote campaign metadata to %s", out_path)

    def _write_provenance(self) -> None:
        """Write ``provenance.json`` to outdir at campaign completion.

        Captures the full sampling details, code hashes used for cache
        invalidation, and runtime environment information for
        reproducibility auditing.
        """
        # Read samples.json if it exists for seed/algorithm details.
        sampling_details: dict[str, object] = {
            "algorithm": self.cfg.algorithm,
            "n_samples": self.cfg.n_samples,
            "max_generations": self.cfg.max_generations,
        }
        samples_file = self.cfg.samples_file
        if samples_file.exists():
            try:
                samples_data = json.loads(samples_file.read_text())
                # Capture the sample IDs so provenance is self-describing.
                sampling_details["sample_ids"] = [
                    s.get("sample_id", f"unknown_{i}")
                    for i, s in enumerate(samples_data.get("samples", []))
                ]
                sampling_details["n_actual_samples"] = len(samples_data.get("samples", []))
            except Exception as exc:
                log.warning("could not read samples.json for provenance: %s", exc)

        provenance: dict[str, object] = {
            "campaign_id": self.trace.campaign_id,
            "sampling": sampling_details,
            "code_hashes": self.code_hashes,
            "environment": {
                "osimflow_version": _osimflow_version(),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "python_implementation": platform.python_implementation(),
            },
            "cache_stats": self.cache.stats(),
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        out_path = self.cfg.outdir / "provenance.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(provenance, indent=2, default=str))
        log.info("wrote provenance to %s", out_path)

    def _write_artifact_manifest(self) -> None:
        """Write ``artifact_manifest.json`` to outdir after aggregation.

        Scans the outdir for all output files and records their paths,
        sizes, and SHA-256 checksums grouped by category (results, plots,
        logs, intermediates).
        """
        artifacts: list[dict[str, object]] = []

        # Maps (path_prefix, is_prefix_match) -> category for common cases.
        prefix_map = {
            "plots": "plots",
            "work/sim": "intermediates",
            "work/apply": "intermediates",
        }
        ext_map = {
            ".png": "plots",
            ".pdf": "plots",
            ".svg": "plots",
            ".csv": "results",
            ".parquet": "results",
            ".log": "logs",
            ".sqlite": "cache",
        }

        def _categorise(path: Path) -> str:
            """Assign a category based on file location/extension."""
            rel = str(path.relative_to(self.cfg.outdir))
            # Check prefix-based categories first.
            for prefix, cat in prefix_map.items():
                if rel.startswith(prefix):
                    return cat
            # Check extension-based categories.
            suffix = path.suffix
            if suffix in ext_map:
                return ext_map[suffix]
            # JSON files: distinguish by name.
            if suffix == ".json":
                if "run.json" in rel:
                    return "logs"
                if any(x in rel for x in ("campaign_meta", "provenance", "artifact_manifest")):
                    return "metadata"
                return "results"
            return "other"

        for f in sorted(self.cfg.outdir.rglob("*")):
            if not f.is_file():
                continue
            try:
                rel_path = str(f.relative_to(self.cfg.outdir))
            except ValueError:
                continue  # skip files outside outdir
            category = _categorise(f)
            # Compute checksum for files that are not the manifest itself.
            sha256 = (
                hashlib.sha256(f.read_bytes()).hexdigest()
                if "artifact_manifest" not in rel_path
                else ""
            )
            artifacts.append(
                {
                    "path": rel_path,
                    "size_bytes": f.stat().st_size,
                    "checksum_sha256": sha256,
                    "category": category,
                }
            )

        manifest: dict[str, object] = {
            "campaign_id": self.trace.campaign_id,
            "artifacts": artifacts,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        out_path = self.cfg.outdir / "artifact_manifest.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(manifest, indent=2, default=str))
        log.info("wrote artifact manifest to %s (%d files)", out_path, len(artifacts))

    # ------------------------------------------------------------------
    # Registry helpers (issue #266)
    # ------------------------------------------------------------------
    def _register_campaign(self) -> None:
        """Register this campaign in the campaign registry at start."""
        if self._registry is None:
            return
        try:
            self._registry.register(
                self.trace.campaign_id,
                name=self.trace.campaign_id,
                project=self.cfg.project,
                outdir=str(self.cfg.outdir),
                status="running",
                algorithm=self.cfg.algorithm,
                n_samples=self.cfg.n_samples,
                executor=self.executor.name,
                openstudio_version=self.cfg.openstudio_version,
                metadata={
                    "archive_intermediates": self.cfg.archive_intermediates,
                    "dry_run": self.cfg.dry_run,
                    "max_generations": self.cfg.max_generations,
                },
            )
        except Exception as exc:
            log.warning("failed to register campaign: %s", exc)

    def _update_registry_status(self, status: str) -> None:
        """Update the campaign status in the registry on completion."""
        if self._registry is None:
            return
        try:
            self._registry.update_status(self.trace.campaign_id, status)
        except Exception as exc:
            log.warning("failed to update campaign status in registry: %s", exc)

    # ------------------------------------------------------------------
    # Graceful shutdown (issue #255)
    # ------------------------------------------------------------------
    def request_cancel(self) -> None:
        """Request campaign cancellation.

        Called by the signal handler or by external code that wants to
        stop a running campaign. Thread-safe. Idempotent.
        """
        with self._cancel_lock:
            self._cancel_requested = True
        log.warning("campaign cancellation requested")

    def _check_cancel_requested(self) -> bool:
        """Check if cancellation has been requested.

        Checks both the in-memory flag and the ``.stop`` file in the
        outdir. The ``.stop`` file is written by the REST API's
        ``POST /api/v1/campaign/stop`` endpoint (issue #143) and by
        external tooling that wants to interrupt a running campaign.

        Returns:
            ``True`` if cancellation is requested, ``False`` otherwise.
        """
        with self._cancel_lock:
            if self._cancel_requested:
                return True
        # Also check the .stop file (written by the API server).
        stop_file = self.cfg.outdir / ".stop"
        if stop_file.is_file():
            log.warning(".stop file detected — requesting cancellation")
            with self._cancel_lock:
                self._cancel_requested = True
            return True
        return False

    def _setup_signal_handlers(self) -> None:
        """Register SIGINT/SIGTERM handlers to request graceful shutdown.

        Saves the previous handlers so they can be restored on exit.
        When a signal is received, ``request_cancel()`` is called.
        """
        self._prev_sigint = signal.signal(signal.SIGINT, self._handle_signal)
        self._prev_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)
        log.debug("signal handlers registered (SIGINT/SIGTERM)")

    @staticmethod
    def _handle_signal(signum: int, _frame: object) -> None:
        """Signal handler that requests cancellation on the Campaign instance.

        Uses a global registry so the signal can reach the running Campaign
        even though the signal callback only receives (signum, frame).
        """
        sig_name = signal.Signals(signum).name
        log.warning("received %s — requesting cancellation", sig_name)
        _cancel_registry.request_cancel()

    def _restore_signal_handlers(self) -> None:
        """Restore the previous signal handlers."""
        if self._prev_sigint is not None:
            signal.signal(signal.SIGINT, self._prev_sigint)
        if self._prev_sigterm is not None:
            signal.signal(signal.SIGTERM, self._prev_sigterm)
        log.debug("signal handlers restored")

    def _cancel_active_jobs(self) -> None:
        """Cancel all active futures submitted to the executor.

        Called during graceful shutdown to stop in-flight work as quickly
        as possible. The executor's ``cancel()`` method is called on
        each active handle; handles that were already completing are
        given a short grace period to finish.
        """
        log.info("canceling active executor jobs")
        self.executor.cancel()
        log.info("executor cancel requested")

    def _write_shutdown_trace(self, status: str = "cancelled") -> None:
        """Write run.json with cancellation status before exit.

        Marks the campaign as cancelled so a re-run can resume correctly.
        """
        try:
            self.trace.status = status
            self.trace.write(self.cfg.outdir / "run.json")
            log.info("wrote cancellation trace to run.json")
        except Exception as exc:
            log.warning("could not write cancellation trace: %s", exc)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self) -> dict[str, object]:  # noqa: PLR0912, PLR0915
        log.info("=" * 60)
        log.info("OSimFlow campaign start")
        log.info("  executor:      %s", self.executor.name)
        log.info("  n_samples:     %d", self.cfg.n_samples)
        log.info("  os_version:    %s", self.cfg.openstudio_version)
        log.info("  outdir:        %s", self.cfg.outdir)
        log.info("  work_dir:      %s", self.cfg.work_dir)
        if self.cfg.dry_run:
            log.info("  ** DRY RUN MODE **")
        if self.cfg.sample is not None:
            log.info("  ** SINGLE SAMPLE MODE: sample %d **", self.cfg.sample)
        log.info("=" * 60)

        # Register this campaign for signal handling (issue #255).
        _cancel_registry.register(self)

        # Setup signal handlers for graceful shutdown.
        self._setup_signal_handlers()

        run_name = maybe_start_mlflow_run(self.cfg.mlflow_tracking_uri, self.trace.campaign_id)
        if run_name is not None:
            log_mlflow_params(_make_mlflow_param_view(self.cfg, self.executor.name))

        t0 = time.time()
        campaign_status = "failure"

        # Auto-register campaign in registry (issue #266).
        self._register_campaign()

        # Write campaign metadata manifest at start (issue #277).
        self._write_campaign_meta()

        try:
            # Pre-campaign cancellation check: if cancellation is requested
            # BEFORE the campaign starts, the except/finally blocks below
            # will write run.json, restore signal handlers, and close the
            # cache.  Keeping this inside the try block is essential so that
            # the finally cleanup (WAL checkpoint, registry update, etc.)
            # always runs even for pre-campaign cancellations.
            if self._check_cancel_requested():
                raise KeyboardInterrupt("cancellation requested before campaign start")

            # Init hook (issue #108): runs before the first campaign step.
            # Must succeed (exit 0) or the campaign aborts.
            self._run_init_script()

            # Initialize run.json after init script succeeds so the init hook
            # sees a clean outdir (issue #108).  _checkpoint_sample() handles a
            # missing run.json gracefully (no-op), so this write is only needed
            # for SSE clients to poll status from the start of the fan-out.
            self.trace.write(self.cfg.outdir / "run.json")

            # Crash recovery (issue #263): reset any in-flight jobs from
            # a previous interrupted run back to pending so they are
            # reprocessed during this run.  The cache layer ensures
            # completed work is not re-executed — only truly lost work
            # (where the executor completed but the orchestrator crashed
            # before recording the result) gets retried.
            recovered = self._job_queue.recover()
            if recovered:
                log.info(
                    "crash recovery: %d in-flight job(s) reset to pending",
                    len(recovered),
                )

            if self.cfg.dry_run:
                result = self._run_dry_run(t0)
            elif self.cfg.sample is not None:
                result = self._run_single_sample(t0)
            else:
                result = self._run_full_campaign(t0)
            # If cancellation was detected in _finalize_full_campaign,
            # self.trace.status will be "cancelled" — propagate that to
            # campaign_status so the caller sees the correct final state.
            campaign_status = "cancelled" if self.trace.status == "cancelled" else "success"
            return result
        except KeyboardInterrupt:
            campaign_status = "cancelled"
            log.warning("campaign cancelled by user or signal")
            self.trace.status = "cancelled"
            self._cancel_active_jobs()
            self.trace.finalize()
            # Always write the trace on cancellation so callers can inspect
            # the partial state.  The file may not exist yet when
            # cancellation fires before the first step (e.g. pre-campaign
            # cancel or .stop file present before run()).
            self.cfg.outdir.mkdir(parents=True, exist_ok=True)
            self.trace.write(self.cfg.outdir / "run.json")
            # Do NOT re-raise — cancellation has been handled; the finally
            # block will run and the campaign will exit with the correct
            # status. Re-raising would crash the worker in concurrent mode
            # and cause test_cancel_during_generation_loop_stops to fail.
            return {"status": "cancelled", "trace": self.trace}
        finally:
            # Restore signal handlers FIRST, before any other cleanup.
            self._restore_signal_handlers()
            _cancel_registry.clear()

            # Finalize hook (issue #108): best-effort after all steps.
            # Runs even if init script failed, so the user gets a
            # notification that the campaign aborted.
            duration = time.time() - t0
            # Observability: record campaign duration and flush backend.
            self._obs.record_campaign_duration(duration)
            self._obs.flush()
            self._run_finalize_script(campaign_status, duration)
            # Re-write run.json to include finalize hook timing
            if (self.cfg.outdir / "run.json").exists():
                self.trace.write(self.cfg.outdir / "run.json")

            # Write provenance manifest at completion (issue #277).
            self._write_provenance()

            # Write artifact manifest after all files are produced (issue #277).
            # Placed after provenance so the manifest captures all output files.
            self._write_artifact_manifest()

            maybe_end_mlflow_run()

            # Update registry status (issue #266).
            self._update_registry_status(campaign_status)

            # Fire webhook callback (issue #283). Best-effort: delivery
            # failures are logged but do not affect campaign status.
            self._maybe_fire_webhook(campaign_status, duration)

            # Close the SQLite cache: checkpoints the WAL and removes the
            # auxiliary .sqlite-wal / .sqlite-shm files so pytest-xdist
            # parallel teardown does not see "FileNotFoundError" on those
            # files. Safe to call multiple times (close() is idempotent).
            self.cache.close()

            # Close the result storage uploader (issue #339).
            if self._result_storage is not None:
                self._result_storage.close()

    def _run_dry_run(self, t0: float) -> dict[str, object]:
        """Dry-run mode: 1 sample, local executor, steps 1-4 only."""
        original_n = self.cfg.n_samples
        self.cfg = dataclasses.replace(self.cfg, n_samples=1)
        log.info("DRY RUN: overriding n_samples from %d to 1", original_n)

        algo = AlgorithmRegistry.get(self.cfg.algorithm)
        samples: list[SampleSpec] = self.step_generate_samples(algo)
        self.cfg.samples_file.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.samples_file.write_text(json.dumps({"samples": samples}, indent=2))
        parameterized: SampleDict = self.step_apply_parameters(samples)
        simulated: SampleDict = self.step_run_openstudio_sim(parameterized)
        kpi_files: list[Path] = self.step_extract_kpis(simulated)
        t1 = time.time()

        self._finalize_samples()
        self.trace.finalize()
        self.trace.write(self.cfg.outdir / "run.json")

        n_ok = sum(1 for s in self.trace.per_sample if s.status == "ok")
        n_failed = sum(1 for s in self.trace.per_sample if s.status == "failed")
        log_mlflow_metrics(t1 - t0, n_ok, n_failed)

        sample_elapsed = t1 - t0
        est_total_s = sample_elapsed * original_n

        print("\n" + "=" * 60)
        print("DRY RUN COMPLETE")
        print("=" * 60)
        print(f"  1/{original_n} samples processed in {sample_elapsed:.1f}s")
        print(f"  Status: {n_ok} succeeded, {n_failed} failed")
        print(f"  Output: {self.cfg.work_dir}")
        if kpi_files:
            print(f"  KPI file: {kpi_files[0]}")
        print()
        print("Full campaign estimate:")
        print(
            f"  {original_n} samples x {sample_elapsed:.1f}s = ~{est_total_s:.0f}s (~{est_total_s / 60:.1f} min)"
        )
        print()
        print("Next steps:")
        print(f"  1. Review output in {self.cfg.work_dir}")
        print("  2. If correct, re-run without --dry-run for full campaign")
        print("=" * 60)

        return {
            "samples": samples,
            "kpis": kpi_files,
            "aggregated": {"csv": None, "failed": None},
            "plots": [],
            "elapsed_s": t1 - t0,
            "run_json": self.cfg.outdir / "run.json",
        }

    def _run_single_sample(self, t0: float) -> dict[str, object]:
        """Single-sample mode: run only sample N through steps 2-4."""
        sample_idx = self.cfg.sample
        assert sample_idx is not None
        samples_file = self.cfg.samples_file
        if not samples_file.exists():
            raise FileNotFoundError(
                f"samples.json not found at {samples_file}. "
                "Run a full campaign (or --dry-run) first to generate samples."
            )
        all_samples = cast_samples(json.loads(samples_file.read_text())["samples"])
        if sample_idx < 0 or sample_idx >= len(all_samples):
            raise IndexError(
                f"Sample index {sample_idx} out of range [0, {len(all_samples) - 1}]. "
                f"Total samples available: {len(all_samples)}"
            )
        target = all_samples[sample_idx]
        log.info("SINGLE SAMPLE: running sample %d (id=%s)", sample_idx, target["sample_id"])

        parameterized: SampleDict = self.step_apply_parameters([target])
        simulated: SampleDict = self.step_run_openstudio_sim(parameterized)
        kpi_files: list[Path] = self.step_extract_kpis(simulated)
        t1 = time.time()

        self._finalize_samples()
        self.trace.finalize()
        self.trace.write(self.cfg.outdir / "run.json")

        log.info("=" * 60)
        log.info("SINGLE SAMPLE COMPLETE: sample %d in %.1fs", sample_idx, t1 - t0)
        log.info("=" * 60)

        return {
            "samples": [target],
            "kpis": kpi_files,
            "aggregated": {"csv": None, "failed": None},
            "plots": [],
            "elapsed_s": t1 - t0,
            "run_json": self.cfg.outdir / "run.json",
        }

    def _run_full_campaign(self, t0: float) -> dict[str, object]:
        """Standard full campaign: all steps, all samples.

        For iterative algorithms (``max_generations > 1``), the fan-out
        steps (APPLY_PARAMETERS → RUN_OPENSTUDIO_SIM → EXTRACT_KPIS)
        loop for up to ``cfg.max_generations`` iterations.  Each
        iteration is a "generation" tracked in the cache key.  LHS with
        ``max_generations=1`` is backward-compatible.
        """
        if self.cfg.max_generations < 1:
            raise ValueError(f"max_generations must be >= 1, got {self.cfg.max_generations}")

        algo = AlgorithmRegistry.get(self.cfg.algorithm)

        # History accumulator: one dict per generation.
        history: list[dict[str, Any]] = []

        all_kpi_files: list[Path] = []
        last_simulated: SampleDict = {}
        last_samples: list[SampleSpec] = []

        for generation in range(self.cfg.max_generations):
            if self._check_cancel_requested():
                log.warning("cancellation requested — stopping generation loop")
                break
            log.info(
                "--- generation %d/%d ---",
                generation + 1,
                self.cfg.max_generations,
            )
            result = self._run_one_generation(algo, history, generation)
            if result is None:
                # Algorithm converged; mark last generation as converged.
                if self.trace.generations:
                    self.trace.generations[-1].converged = True
                break
            samples, kpi_files, simulated = result
            last_samples = samples
            last_simulated = simulated
            all_kpi_files.extend(kpi_files)
            history.append(
                {
                    "generation": generation,
                    "samples": samples,
                    "kpi_files": [str(p) for p in kpi_files],
                }
            )
            # Single-shot algorithms never iterate.
            if not algo.is_iterative():
                break

        return self._finalize_full_campaign(t0, all_kpi_files, last_simulated, last_samples)

    def _run_one_generation(
        self,
        algo: BaseAlgorithm,
        history: list[dict[str, Any]],
        generation: int,
    ) -> tuple[list[SampleSpec], list[Path], SampleDict] | None:
        """Run one generation of the fan-out DAG.

        Returns (samples, kpi_files, simulated_dirs), or ``None`` if
        the algorithm has converged and the loop should stop.

        The feedback loop (issue #270):

        1. For generation > 0, check convergence. If converged, stop.
        2. Call ``algo.observe(history)`` — this reads KPI results from
           previous generations and updates the optimizer's internal
           state (best params, proposed samples, etc.).
        3. Call ``step_generate_samples(algo)`` — for iterative algorithms,
           ``algo.generate_samples()`` reads the internal state set by
           ``observe()`` and returns the proposed samples. For single-shot
           algorithms, it always returns LHS samples.
        4. Run the fan-out DAG: apply → simulate → extract KPIs.
        5. Record per-generation monitoring (issue #270).
        """
        gen_t0 = time.time()

        # Convergence check: after the first generation, ask the
        # algorithm whether we should continue.
        if generation > 0:
            if algo.is_converged(history):
                log.info(
                    "algorithm %s converged at generation %d; stopping loop",
                    algo.name(),
                    generation,
                )
                return None
            # observe() reads KPI history and updates optimizer state.
            # The returned samples are also stored in the explicit
            # _pending_proposed_samples slot for verifiable contract
            # (issue #332).
            new_samples = algo.observe(history)
            if new_samples:
                cast_samples(new_samples)
                # Verify observe() return matches the explicit slot
                # (issue #332). This catches bugs where an algorithm
                # sets internal state but fails to return.
                pending = getattr(algo, "_pending_proposed_samples", None)
                if pending is not None and pending != new_samples:
                    log.error(
                        "observe() return value does not match "
                        "_pending_proposed_samples for algorithm %s",
                        algo.name(),
                    )
            else:
                log.warning(
                    "observe() returned empty samples at generation %d; reusing previous",
                    generation,
                )

        samples = self.step_generate_samples(algo, generation=generation)
        samples_link = self.cfg.samples_file
        samples_link.parent.mkdir(parents=True, exist_ok=True)
        samples_link.write_text(json.dumps({"samples": samples}, indent=2))

        if generation == 0:
            self.step_preflight_run_model()

        parameterized: SampleDict = self.step_apply_parameters(samples, generation=generation)
        self.step_validate_measure_variables(generation=generation)
        simulated: SampleDict = self.step_run_openstudio_sim(parameterized, generation=generation)
        kpi_files: list[Path] = self.step_extract_kpis(simulated, generation=generation)

        # Sobol sensitivity indices (issue #346): compute after KPI extraction.
        if self.cfg.algorithm == "sobol":
            variables: dict[str, Any] = {}
            if self.cfg.input_variables.exists():
                with self.cfg.input_variables.open() as fh:
                    raw = yaml.safe_load(fh)
                    if isinstance(raw, dict):
                        variables = raw
            self.step_compute_sensitivity_indices(
                samples, kpi_files, variables, generation=generation
            )

        # Per-generation Pareto front persistence for multi-objective
        # algorithms (issue #141).  When the algorithm reports
        # is_multi_objective(), build ParetoSolution objects from the
        # extracted KPIs and persist the front to outdir/pareto/gen_N.json.
        if algo.is_multi_objective() and kpi_files:
            self._persist_pareto_front(algo, samples, kpi_files, generation)

        # Per-generation monitoring (issue #270).
        gen_elapsed = time.time() - gen_t0
        gen_samples = [s for s in self.trace.per_sample if s.generation == generation]
        n_succeeded = sum(1 for s in gen_samples if s.status == "ok")
        n_failed = sum(1 for s in gen_samples if s.status == "failed")
        best_objective = self._extract_best_objective(algo, kpi_files)
        self.trace.generation_done(
            GenerationTrace(
                generation=generation,
                n_samples=len(samples),
                n_succeeded=n_succeeded,
                n_failed=n_failed,
                converged=False,  # updated later if needed
                best_objective=best_objective,
                elapsed_s=round(gen_elapsed, 3),
            )
        )
        log.info(
            "generation %d complete: %d samples (%d ok, %d failed) in %.1fs",
            generation,
            len(samples),
            n_succeeded,
            n_failed,
            gen_elapsed,
        )

        return samples, kpi_files, simulated

    @staticmethod
    def _extract_best_objective(
        algo: BaseAlgorithm,
        kpi_files: list[Path],
    ) -> float | None:
        """Extract the best objective value from KPI files.

        For single-objective algorithms (DE, DA, PSO), reads the primary
        KPI. For multi-objective (NSGA-II), returns None (use Pareto front
        instead). The objective name is inferred from the algorithm's
        default (``eui`` for DE/DA/PSO).
        """
        if algo.is_multi_objective():
            return None
        if not kpi_files:
            return None
        best: float | None = None
        for kpi_path in kpi_files:
            try:
                data = json.loads(kpi_path.read_text())
                kpis = data.get("kpis", {})
                # Default objective is "eui" — matches DE/DA/PSO defaults.
                val = kpis.get("eui")
                if (
                    val is not None
                    and isinstance(val, (int, float))
                    and (best is None or float(val) < best)
                ):
                    best = float(val)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
        return best

    def _persist_pareto_front(
        self,
        algo: BaseAlgorithm,
        samples: list[SampleSpec],
        kpi_files: list[Path],
        generation: int,
    ) -> None:
        """Build/update the Pareto front and persist per-generation JSON.

        Parameters
        ----------
        algo
            The algorithm instance (must report ``is_multi_objective()``).
        samples
            The sample specs for this generation.
        kpi_files
            Extracted KPI JSON files (one per sample).
        generation
            0-based generation index — used in the output filename.
        """
        # Load existing front (if any) from the previous generation.
        pareto_dir = self.cfg.outdir / "pareto"
        pareto_path = pareto_dir / f"gen_{generation}.json"
        front: ParetoFront | None = None

        # Try to load from previous generation's file to carry forward
        # non-dominated solutions.
        if generation > 0:
            prev_path = pareto_dir / f"gen_{generation - 1}.json"
            if prev_path.exists():
                try:
                    front = ParetoFront.load(prev_path)
                except Exception as exc:
                    log.warning("could not load previous Pareto front: %s", exc)

        # Determine objective names from the first KPI file that has data.
        objective_names: list[str] = []
        for kpi_path in kpi_files:
            try:
                kpi_data = json.loads(kpi_path.read_text())
                kpis = kpi_data.get("kpis", {})
                objective_names = sorted(k for k, v in kpis.items() if isinstance(v, (int, float)))
                if objective_names:
                    break
            except Exception:
                continue

        if not objective_names:
            log.warning("no objective KPIs found; skipping Pareto front")
            return

        if front is None:
            front = ParetoFront(objective_names=objective_names)

        # Build ParetoSolution objects from samples + KPIs.
        # Match by index (samples[i] -> kpi_files[i]) — this is the
        # same correspondence the Campaign uses throughout.
        new_solutions: list[ParetoSolution] = []
        for i, sample in enumerate(samples):
            if i >= len(kpi_files):
                break
            try:
                kpi_data = json.loads(kpi_files[i].read_text())
                kpis = kpi_data.get("kpis", {})
                objectives = {k: float(v) for k, v in kpis.items() if isinstance(v, (int, float))}
                parameters = {
                    k: float(v) for k, v in sample["values"].items() if isinstance(v, (int, float))
                }
                new_solutions.append(
                    ParetoSolution(
                        sample_id=str(sample["sample_id"]),
                        objectives=objectives,
                        parameters=parameters,
                        generation=generation,
                    )
                )
            except Exception as exc:
                log.warning(
                    "could not build ParetoSolution for sample %s: %s", sample.get("sample_id"), exc
                )

        if new_solutions:
            front.add_generation(new_solutions)
            front.save(pareto_path)

    def _finalize_full_campaign(
        self,
        t0: float,
        all_kpi_files: list[Path],
        last_simulated: SampleDict,
        last_samples: list[SampleSpec],
    ) -> dict[str, object]:
        """Aggregate, plot, archive, and write the run trace."""
        if self._check_cancel_requested():
            log.warning("cancellation requested before aggregation — writing partial trace")
            self._finalize_samples()
            self.trace.status = "cancelled"
            self.trace.finalize()
            self.trace.write(self.cfg.outdir / "run.json")
            return {
                "samples": last_samples,
                "kpis": all_kpi_files,
                "aggregated": {},
                "plots": [],
                "elapsed_s": time.time() - t0,
                "run_json": self.cfg.outdir / "run.json",
            }
        aggregated: dict[str, Path] = self.step_aggregate_results(
            all_kpi_files, last_simulated, baseline_sample_id=self._baseline_sample_id()
        )
        plots: list[Path] = self.step_generate_plots(
            aggregated, baseline_sample_id=self._baseline_sample_id()
        )
        t1 = time.time()

        self._compute_baseline_comparison(all_kpi_files)
        self._maybe_archive_inputs()

        self._finalize_samples()
        self.trace.finalize()
        self.trace.write(self.cfg.outdir / "run.json")

        n_succeeded = sum(1 for s in self.trace.per_sample if s.status == "ok")
        n_failed = sum(1 for s in self.trace.per_sample if s.status == "failed")
        log_mlflow_metrics(t1 - t0, n_succeeded, n_failed)
        log_mlflow_artifacts(
            aggregated["csv"],
            aggregated["failed"],
            self.cfg.outdir / "run.json",
        )

        log.info("=" * 60)
        log.info("OSimFlow campaign complete in %.1fs", t1 - t0)
        log.info("  cache stats:   %s", self.cache.stats())
        log.info("  aggregated:    %s", aggregated["csv"])
        log.info("  failed:        %s", aggregated["failed"])
        log.info("  plots:         %s", plots)
        log.info("  run trace:     %s", self.cfg.outdir / "run.json")
        if self.trace.total_cost_usd > 0:
            log.info("  total cost:    $%.4f", self.trace.total_cost_usd)
            log.info("  spot savings:  $%.4f", self.trace.spot_savings_usd)
        log.info("=" * 60)
        return {
            "samples": last_samples,
            "kpis": all_kpi_files,
            "aggregated": aggregated,
            "plots": plots,
            "elapsed_s": t1 - t0,
            "run_json": self.cfg.outdir / "run.json",
        }

    def _maybe_archive_inputs(self) -> None:
        """Archive campaign inputs when ``cfg.archive_intermediates`` is set."""
        if not self.cfg.archive_intermediates:
            return
        inputs_archive = self.cfg.outdir / "archive" / "inputs"
        inputs_archive.mkdir(parents=True, exist_ok=True)
        pkg_dst = inputs_archive / self.cfg.template_sim_package.name
        if pkg_dst.exists():
            shutil.rmtree(pkg_dst)
        shutil.copytree(self.cfg.template_sim_package, pkg_dst)
        log.info("archived template_sim_package -> %s", pkg_dst)
        shutil.copy2(self.cfg.input_variables, inputs_archive / self.cfg.input_variables.name)
        log.info("archived input_variables -> %s", inputs_archive / self.cfg.input_variables.name)

    def _maybe_fire_webhook(self, campaign_status: str, elapsed_s: float) -> None:
        """Fire a webhook callback if ``cfg.webhook_url`` is configured (issue #283).

        Best-effort: delivery failures are logged but do not propagate.
        The webhook is sent after the GENERATE_BASIC_PLOTS step, in the
        ``finally`` block of ``run()``, so it fires regardless of success
        or failure — ``campaign_status`` will be ``"success"``,
        ``"failure"``, or ``"cancelled"``.
        """
        if not self.cfg.webhook_url:
            return

        n_succeeded = sum(1 for s in self.trace.per_sample if s.status == "ok")
        n_failed = sum(1 for s in self.trace.per_sample if s.status == "failed")

        client = WebhookClient(url=self.cfg.webhook_url)
        payload = client.build_payload(
            campaign_id=self.trace.campaign_id,
            status=campaign_status,
            elapsed_s=elapsed_s,
            n_samples=self.cfg.n_samples,
            n_succeeded=n_succeeded,
            n_failed=n_failed,
            total_cost_usd=self.trace.total_cost_usd if self.trace.total_cost_usd > 0 else None,
            outdir=str(self.cfg.outdir),
        )

        log.info("firing webhook to %s (status=%s)", self.cfg.webhook_url, campaign_status)
        ok = client.deliver(payload)
        if not ok:
            log.warning(
                "webhook delivery to %s failed (campaign_status=%s)",
                self.cfg.webhook_url,
                campaign_status,
            )

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    def step_generate_samples(
        self,
        algo: BaseAlgorithm,
        generation: int = 0,
    ) -> list[SampleSpec]:
        """Generate samples via the pluggable algorithm framework.

        Dispatches to ``algo.generate_samples()`` with the campaign's
        variables and sample count.  The result is cached under a key
        that includes the algorithm name *and* the generation number so
        each generation's samples are independently cacheable.

        This is the preferred entry point.  ``step_generate_lhs()`` is
        kept as a deprecated convenience wrapper.
        """
        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before GENERATE_SAMPLES")

        algo_name = algo.name().upper()
        step_label = f"GENERATE_{algo_name}_SAMPLES"

        # Read variables.yml once for the algorithm.
        variables: dict[str, Any] = {}
        if self.cfg.input_variables.exists():
            with self.cfg.input_variables.open() as fh:
                raw = yaml.safe_load(fh)
                if isinstance(raw, dict):
                    variables = raw

        inputs_hash = sha256_of_files([self.cfg.input_variables])
        key = CacheKey(
            step=step_label,
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256=inputs_hash,
            code_sha256=self.code_hashes["bin"],
            container_digest=CONTAINER_PY,
            generation=generation,
        )
        cached = self.cache.lookup(key)
        if cached:
            samples_obj: object = json.loads(cached.read_text())["samples"]
            samples_from_cache = cast_samples(samples_obj)
            self.trace.step_finished(
                step_label,
                cache="HIT",
                elapsed_s=time.time() - t0,
                exit_code=0,
            )
            self._obs.record_step_duration(step_label, time.time() - t0, generation=generation)
            return samples_from_cache

        out_dir = self.cfg.work_dir / algo.name()
        result_path = algo.generate_samples(
            variables=variables,
            n_samples=self.cfg.n_samples,
            seed=None,
            outdir=out_dir,
        )
        try:
            self.cache.store(key, Path(result_path), exit_code=0)
            run_samples_obj: object = json.loads(Path(result_path).read_text())["samples"]
            samples = cast_samples(run_samples_obj)

            # Inject baseline sample (issue #64): when cfg.baseline is
            # defined, prepend a fixed-parameter sample to the sample set.
            baseline_sid = self._baseline_sample_id()
            if baseline_sid is not None and self.cfg.baseline is not None:
                baseline_params = self.cfg.baseline.get("parameters", {})
                # Only inject if not already present in the sample set.
                existing_ids = {s["sample_id"] for s in samples}
                if baseline_sid not in existing_ids:
                    params_dict: dict[str, object] = (
                        dict(baseline_params) if isinstance(baseline_params, dict) else {}
                    )
                    baseline_sample: SampleSpec = SampleSpec(
                        sample_id=baseline_sid,
                        values=params_dict,
                    )
                    samples.insert(0, baseline_sample)
                    log.info(
                        "injected baseline sample_id=%s with %d parameters",
                        baseline_sid,
                        len(params_dict),
                    )
                else:
                    log.info(
                        "baseline sample_id=%s already in sample set; skipping injection",
                        baseline_sid,
                    )

            self.trace.step_finished(
                step_label,
                cache="MISS",
                elapsed_s=time.time() - t0,
                exit_code=0,
            )
            self._obs.record_step_duration(step_label, time.time() - t0, generation=generation)
            return samples
        except Exception as e:
            log.error("%s failed: %s", step_label, e)
            self.trace.step_finished(
                step_label,
                cache="MISS",
                elapsed_s=time.time() - t0,
                exit_code=1,
            )
            raise

    def step_generate_lhs(self) -> list[SampleSpec]:
        """Single-shot: read variables.yml, produce N parameter sets.

        .. deprecated::
            Use ``step_generate_samples(algo)`` instead.  This method
            is retained for backward compatibility and calls
            ``step_generate_samples`` with ``LHSAlgorithm``.
        """
        warnings.warn(
            "step_generate_lhs() is deprecated; use step_generate_samples(algo) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        algo = AlgorithmRegistry.get("lhs")
        return self.step_generate_samples(algo)

    def step_preflight_run_model(self) -> None:
        """Single-shot: run a throwaway simulation of the seed model.

        Makes a temporary copy of the ``template_sim_package`` and runs
        ``openstudio.cli run -w workflow.osw`` (or the stub when the CLI
        is not available).  If the run encounters severe errors, raises
        :class:`SevereEnergyPlusError` to abort the campaign before
        spending cloud budget.

        This step is cached: a successful preflight result is stored in
        the cache so a re-run with the same template and version does not
        re-execute the simulation.  The cache key includes the template
        package hash and the OpenStudio version.

        Skipped when ``cfg.skip_preflight`` is ``True`` (the
        ``--skip-preflight`` CLI flag).

        Raises:
            SevereEnergyPlusError: the seed model has errors that would
                cause every sample to fail.
        """
        if self.cfg.skip_preflight:
            log.info("PREFLIGHT_RUN_MODEL: skipped (--skip-preflight)")
            self.trace.step_finished(
                "PREFLIGHT_RUN_MODEL",
                cache="SKIPPED",
                elapsed_s=0.0,
                exit_code=0,
            )
            return

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before PREFLIGHT_RUN_MODEL")

        t0 = time.time()
        os_version = self.cfg.openstudio_version
        inputs_hash = sha256_of_files(sorted(self.cfg.template_sim_package.rglob("*")))
        key = CacheKey(
            step="PREFLIGHT_RUN_MODEL",
            sample_id="ALL",
            openstudio_version=os_version,
            inputs_sha256=inputs_hash,
            code_sha256=self.code_hashes["work"],
            container_digest=CONTAINER_OS.format(version=os_version),
        )
        cached = self.cache.lookup(key)
        if cached:
            log.info("PREFLIGHT_RUN_MODEL: cache HIT")
            elapsed = time.time() - t0
            self.trace.step_finished(
                "PREFLIGHT_RUN_MODEL",
                cache="HIT",
                elapsed_s=elapsed,
                exit_code=0,
            )
            self._obs.record_step_duration("PREFLIGHT_RUN_MODEL", elapsed)
            return

        try:
            preflight_run_model(
                self.cfg.template_sim_package,
                os_version,
            )
        except SevereEnergyPlusError:
            log.error("PREFLIGHT_RUN_MODEL: seed model has severe errors")
            self.trace.step_finished(
                "PREFLIGHT_RUN_MODEL",
                cache="MISS",
                elapsed_s=time.time() - t0,
                exit_code=1,
            )
            raise

        # Store a marker file so the cache has a path to record.
        preflight_marker = self.cfg.work_dir / "preflight_OK"
        preflight_marker.parent.mkdir(parents=True, exist_ok=True)
        preflight_marker.write_text(f"preflight passed at {time.strftime('%Y-%m-%dT%H:%M:%S')}")
        self.cache.store(key, preflight_marker, exit_code=0)
        elapsed = time.time() - t0
        self.trace.step_finished(
            "PREFLIGHT_RUN_MODEL",
            cache="MISS",
            elapsed_s=elapsed,
            exit_code=0,
        )
        self._obs.record_step_duration("PREFLIGHT_RUN_MODEL", elapsed)

    def step_validate_measure_variables(self, generation: int = 0) -> None:
        """Validate variables.yml against discovered measure arguments.

        Runs before ``RUN_OPENSTUDIO_SIM`` as a pre-flight check (GAP-003).
        Raises ``UnmappedVariableError`` if any variable name in
        ``variables.yml`` does not correspond to a discovered measure
        argument.
        """
        if self.cfg.skip_preflight:
            return

        if not self.cfg.input_variables.exists():
            return

        t0 = time.time()
        try:
            with self.cfg.input_variables.open() as fh:
                raw = yaml.safe_load(fh)
            variables: list[dict[str, Any]]
            if isinstance(raw, dict):
                variables = raw.get("variables", [])
            else:
                variables = []
        except Exception:
            return

        if not variables:
            return

        registry = MeasureRegistry()
        registry.index_measures(self.cfg.template_sim_package)

        if not registry._measures:
            return

        try:
            registry.validate_variables_mapping(variables, registry)
        except UnmappedVariableError:
            self.trace.step_finished(
                "VALIDATE_MEASURE_VARIABLES",
                cache="MISS",
                elapsed_s=time.time() - t0,
                exit_code=1,
            )
            self._obs.record_step_duration("VALIDATE_MEASURE_VARIABLES", time.time() - t0)
            raise

        self.trace.step_finished(
            "VALIDATE_MEASURE_VARIABLES",
            cache="MISS",
            elapsed_s=time.time() - t0,
            exit_code=0,
        )
        self._obs.record_step_duration("VALIDATE_MEASURE_VARIABLES", time.time() - t0)

    def step_apply_parameters(  # noqa: PLR0915
        self,
        samples: list[SampleSpec],
        generation: int = 0,
    ) -> SampleDict:
        """Fan-out: for each sample, produce a modified sim package.

        Parameters
        ----------
        samples
            The sample specifications to parameterise.
        generation
            The generation number (0-based).  Included in the cache key
            so that the same sample in different generations gets
            independently cached entries.

        Before submitting any work, runs a pre-flight validation pass
        (PRD §1.4) that checks every parameter name across all samples
        against the template's available measure arguments and .osm
        attributes. This ensures a typo in ``variables.yml`` fails fast
        *before* any simulations start.

        Additionally, for variables with ``target: epw_file`` (issue
        #55), verifies that all mapped ``.epw`` files exist in the
        ``template_sim_package`` directory and resolves each sample's
        categorical value to the corresponding weather file path.

        Raises:
            UnmappedParameterError: a parameter name does not map to any
                template attribute or measure argument. The error message
                includes fuzzy-match suggestions for likely typos.
            AmbiguousParameterError: a plain argument name appears in
                multiple measure steps and must be disambiguated via the
                dotted ``MeasureName.argument_name`` form.
            FileNotFoundError: an ``epw_file`` target references a
                ``.epw`` file that does not exist in the template
                simulation package.
        """
        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before APPLY_PARAMETERS")

        # Load variable definitions from variables.yml for epw_file
        # target resolution and pre-flight validation (issue #55).
        variable_defs: list[dict[str, Any]] = self._load_variable_defs()

        # Pre-flight EPW validation: verify all mapped .epw files exist
        # in the template_sim_package directory.
        self._preflight_validate_epw_files(variable_defs)

        # Pre-flight validation (PRD §1.4): validate ALL parameter names
        # against the template before submitting any work. Collect the
        # union of parameter names across all samples so a single bad
        # variable in any sample blocks the entire step.
        all_param_keys: dict[str, None] = {}
        for s in samples:
            all_param_keys.update(dict.fromkeys(s["values"].keys()))
        if all_param_keys:
            mappings = _build_mappings(self.cfg.template_sim_package)
            preflight_check(all_param_keys, mappings)
            log.info(
                "pre-flight check passed: %d parameter(s) validated against %s",
                len(all_param_keys),
                self.cfg.template_sim_package,
            )

        out: SampleDict = {}
        cache_label = "MISS×N" if samples else "SKIPPED"
        self.trace.step_started("APPLY_PARAMETERS", total=len(samples))

        # --- Phase 1: cache check for all samples ---
        # Collect non-cached samples and their per-sample context.
        pending: dict[str, dict[str, Any]] = {}
        for s in samples:
            sid = str(s["sample_id"])
            params = s["values"]
            # Resolve epw_file targets: inject __epw_file__ with the
            # mapped .epw path (issue #55). The apply function extracts
            # this reserved key and uses it to mutate the .osw
            # weather_file field.
            resolved_params = self._resolve_epw_targets(params, variable_defs)
            inputs_hash = sha256_of_dict({"params": resolved_params, "sid": sid})
            key = CacheKey(
                step="APPLY_PARAMETERS",
                sample_id=sid,
                openstudio_version="N/A",
                inputs_sha256=inputs_hash,
                code_sha256=self.code_hashes["bin"],
                container_digest=CONTAINER_PY,
                generation=generation,
            )
            state = self._sample_state.setdefault(sid, {})
            cached = self.cache.lookup(key)
            if cached:
                out[sid] = cached
                state["apply_exit_code"] = 0
                state["apply_status"] = "cached"
                self.trace.step_item_done("APPLY_PARAMETERS", status="cached")
                continue
            out_dir = self.cfg.work_dir / "apply" / sid
            out_dir.mkdir(parents=True, exist_ok=True)
            pending[sid] = {
                "resolved_params": resolved_params,
                "out_dir": out_dir,
                "key": key,
                "state": state,
            }

        # --- Phase 2: submit all non-cached samples at once ---
        submissions: dict[str, tuple[Handle | TQHandle, Callable[[Any], None]]] = {}
        for sid, ctx in pending.items():
            handle: Handle | TQHandle
            if self.task_queue is not None:
                handle = self.task_queue.submit(
                    self.apply_fn,
                    self.cfg.template_sim_package,
                    ctx["resolved_params"],
                    sid,
                    ctx["out_dir"],
                )
            else:
                handle = self.executor.submit(
                    self.apply_fn,
                    self.cfg.template_sim_package,
                    ctx["resolved_params"],
                    sid,
                    ctx["out_dir"],
                    name=f"apply_{sid}",
                    cpus=1,
                    memory_mb=512,
                    time_min=5,
                    container=CONTAINER_PY,
                )

            # Build the on-success callback (captures per-sample context).
            key = ctx["key"]
            state = ctx["state"]
            out_dir = ctx["out_dir"]
            archive = self.cfg.archive_intermediates

            def _on_success(
                result_path: Any,
                _sid: str = sid,
                _key: CacheKey = key,
                _state: dict[str, object] = state,
                _out_dir: Path = out_dir,
                _archive: bool = archive,
            ) -> None:
                self.cache.store(_key, Path(result_path), exit_code=0)
                out[_sid] = Path(result_path)
                _state["apply_exit_code"] = 0
                _state["apply_status"] = "ok"
                self.trace.step_item_done("APPLY_PARAMETERS", status="ok")
                # Archive modified .osw/.osm when flag is set
                if _archive:
                    archive_dst = self.cfg.outdir / "archive" / "apply" / _sid
                    self._archive_sample_artifacts(
                        Path(result_path), archive_dst, ["*.osw", "*.osm"]
                    )

            submissions[sid] = (handle, _on_success)

        # --- Phase 3: await all results concurrently ---
        self._submit_and_await_all(submissions, "APPLY_PARAMETERS")

        # Record failures for samples that didn't succeed.
        for _sid, ctx in pending.items():
            state = ctx["state"]
            if state.get("apply_exit_code") != 0 and "apply_status" not in state:
                state["apply_exit_code"] = 1
                state["apply_status"] = "failed"
                state["error_summary"] = "APPLY: unknown error during concurrent execution"
                self.cache.store(ctx["key"], ctx["out_dir"], exit_code=1)
                self.trace.step_item_done("APPLY_PARAMETERS", status="failed")

        self.trace.step_finished(
            "APPLY_PARAMETERS",
            cache=cache_label,
            elapsed_s=time.time() - t0,
            exit_code=0,
        )
        self._obs.record_step_duration("APPLY_PARAMETERS", time.time() - t0, generation=generation)
        return out

    def step_run_openstudio_sim(  # noqa: PLR0915
        self,
        parameterized: SampleDict,
        generation: int = 0,
    ) -> SampleDict:
        """Fan-out (heavy): for each sample, run the OpenStudio simulation.

        For each sample, this step computes the per-sample stdout/stderr
        log paths via `osimflow.monitoring.sample_log_paths()` (issue #6)
        and passes them as keyword arguments to `executor.submit()`. The
        LocalExecutor's work function uses these to redirect the
        underlying `openstudio.cli` subprocess output to disk, so the
        user can `cat` the per-sample log files to debug a failed run.

        The paths are also recorded on the per-sample state so the
        SampleTrace row in `run.json` references them.
        """
        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before RUN_OPENSTUDIO_SIM")

        out: SampleDict = {}
        os_version = self.cfg.openstudio_version
        n = len(parameterized)
        self.trace.step_started("RUN_OPENSTUDIO_SIM", total=n)

        # --- Phase 1: cache check for all samples ---
        pending: dict[str, dict[str, Any]] = {}
        for sid, mod_pkg in parameterized.items():
            # Per-sample log file paths (issue #6). Computed here so the
            # work function receives them as kwargs. The
            # `sample_log_paths` helper creates the directory and is
            # idempotent, so it is safe to call before submit(). The
            # helper takes the user-facing `outdir` (not `work_dir`)
            # and appends `work/sim/<sid>` per `.agents/results/monitoring-decision.md`.
            stdout_log, stderr_log = sample_log_paths(self.cfg.outdir, sid)
            inputs_hash = sha256_of_dict(
                {
                    "template": str(self.cfg.template_sim_package),
                    "sid": sid,
                    "os_version": os_version,
                    # Hash the log paths into the cache key so a user
                    # who moves the outdir gets a fresh run (paths
                    # change → cache miss → re-run).
                    "stdout_log": str(stdout_log),
                    "stderr_log": str(stderr_log),
                }
            )
            key = CacheKey(
                step="RUN_OPENSTUDIO_SIM",
                sample_id=sid,
                openstudio_version=os_version,
                inputs_sha256=inputs_hash,
                code_sha256=self.code_hashes["bin"],
                container_digest=CONTAINER_OS.format(version=os_version),
                generation=generation,
            )
            state = self._sample_state.setdefault(sid, {})
            # Always populate the log path fields so consumers of the
            # run.json trace know where to look, even on a cache hit
            # (the previous run's log files are still on disk).
            state["stdout_log"] = str(stdout_log)
            state["stderr_log"] = str(stderr_log)
            cached = self.cache.lookup(key)
            if cached:
                out[sid] = cached
                state["sim_exit_code"] = 0
                state["sim_status"] = "cached"
                state["eplusout_sql"] = str(cached / "eplusout.sql")
                self.trace.step_item_done("RUN_OPENSTUDIO_SIM", status="cached")
                continue
            out_dir = self.cfg.work_dir / "sim"
            out_dir.mkdir(parents=True, exist_ok=True)
            pending[sid] = {
                "mod_pkg": mod_pkg,
                "out_dir": out_dir,
                "key": key,
                "state": state,
                "stdout_log": stdout_log,
                "stderr_log": stderr_log,
            }

        # --- Phase 2: submit all non-cached samples at once ---
        submissions: dict[str, tuple[Handle | TQHandle, Callable[[Any], None]]] = {}
        for sid, ctx in pending.items():
            handle: Handle | TQHandle
            if self.task_queue is not None:
                handle = self.task_queue.submit(
                    run_openstudio_sim,
                    ctx["mod_pkg"],
                    sid,
                    os_version,
                    ctx["out_dir"],
                    openstudio_version=os_version,
                    stdout_path=ctx["stdout_log"],
                    stderr_path=ctx["stderr_log"],
                    max_retries=self.cfg.max_sample_retries,
                    worker_id="local",
                )
            else:
                handle = self.executor.submit(
                    run_openstudio_sim,
                    ctx["mod_pkg"],
                    sid,
                    os_version,
                    ctx["out_dir"],
                    name=f"sim_{sid}",
                    cpus=4,
                    memory_mb=8 * 1024,
                    time_min=240,
                    container=CONTAINER_OS.format(version=os_version),
                    openstudio_version=os_version,
                    stdout_path=ctx["stdout_log"],
                    stderr_path=ctx["stderr_log"],
                    max_retries=self.cfg.max_sample_retries,
                    worker_id="local",
                )

            # Build the on-success callback (captures per-sample context).
            key = ctx["key"]
            state = ctx["state"]
            archive = self.cfg.archive_intermediates
            h = handle

            def _on_success(
                result_path: Any,
                _sid: str = sid,
                _key: CacheKey = key,
                _state: dict[str, object] = state,
                _archive: bool = archive,
                _handle: Handle | TQHandle = h,
            ) -> None:
                # Intermediate-file optimization (PRD §1.4): drop empty
                # `.err` (the eplusout.err from the OpenStudio run). The
                # per-sample `stderr.log` is the *replacement* and is
                # preserved regardless of size.
                err = result_path / "eplusout.err"
                if err.exists() and err.stat().st_size == 0:
                    err.unlink()
                # NOTE(issue #274): eplusout.sql is preserved unconditionally.
                # It is NEVER deleted after a successful simulation because it
                # contains the full 8760-hour raw time-series needed by the
                # time-series API. The file lives at
                #   {outdir}/work/sim/{sample_id}/eplusout.sql
                # and is available for querying even when --archive_intermediates
                # is not set. Storage implications: 5-200+ MB per sample.
                self.cache.store(_key, Path(result_path), exit_code=0)
                out[_sid] = Path(result_path)
                _state["sim_exit_code"] = 0
                _state["sim_status"] = "ok"
                _state["eplusout_sql"] = str(result_path / "eplusout.sql")
                # Worker tracking (issue #105): capture from the sim handle.
                # TaskHandle only has worker_id; Handle has full worker tracking.
                _state["worker_id"] = _handle.worker_id
                _state["worker_ip"] = getattr(_handle, "worker_ip", None)
                _state["worker_region"] = getattr(_handle, "worker_region", None)
                # Cost tracking (issue #126): capture from the sim handle.
                _state["cost_usd"] = getattr(_handle, "cost_usd", None)
                _state["billed_duration_seconds"] = getattr(
                    _handle, "billed_duration_seconds", None
                )
                self.trace.step_item_done("RUN_OPENSTUDIO_SIM", status="ok")
                # Archive eplusout.sql when flag is set
                if _archive:
                    archive_dst = self.cfg.outdir / "archive" / "sim" / _sid
                    self._archive_sample_artifacts(Path(result_path), archive_dst, ["eplusout.sql"])
                # Result storage upload (issue #339): upload eplusout.sql after
                # successful simulation when a remote backend is configured.
                if self._result_storage is not None:
                    sql_path = result_path / "eplusout.sql"
                    if sql_path.is_file():
                        try:
                            self._result_storage.upload_file(
                                sql_path,
                                f"sim/{_sid}/eplusout.sql",
                            )
                            log.debug(
                                "result storage: uploaded eplusout.sql for sample %s",
                                _sid,
                            )
                        except OSError as exc:
                            log.warning(
                                "result storage: upload failed for sample %s: %s",
                                _sid,
                                exc,
                            )

            submissions[sid] = (handle, _on_success)

        # --- Phase 3: await all results concurrently ---
        # Worker auto-recovery (issue #443): set up recovery manager and resubmit
        # callback when auto-recovery is enabled.
        recovery_manager: WorkerRecoveryManager | None = None
        resubmit_callback: Callable[[str], Handle | TQHandle | None] | None = None

        if self.cfg.worker_auto_recovery:
            recovery_manager = WorkerRecoveryManager(self.cfg.outdir)

            def resubmit_callback(sid: str) -> Handle | TQHandle | None:
                """Resubmit a failed job for auto-recovery."""
                ctx = pending.get(sid)
                if ctx is None:
                    log.error("auto-recovery: no pending context for sample %s", sid)
                    return None
                log.info(
                    "auto-recovery: resubmitting sample %s (outdir=%s)",
                    sid,
                    ctx["out_dir"],
                )
                if self.task_queue is not None:
                    return self.task_queue.submit(
                        run_openstudio_sim,
                        ctx["mod_pkg"],
                        sid,
                        os_version,
                        ctx["out_dir"],
                        openstudio_version=os_version,
                        stdout_path=ctx["stdout_log"],
                        stderr_path=ctx["stderr_log"],
                        max_retries=self.cfg.max_sample_retries,
                        worker_id="local",
                    )
                else:
                    return self.executor.submit(
                        run_openstudio_sim,
                        ctx["mod_pkg"],
                        sid,
                        os_version,
                        ctx["out_dir"],
                        name=f"sim_{sid}",
                        cpus=4,
                        memory_mb=8 * 1024,
                        time_min=240,
                        container=CONTAINER_OS.format(version=os_version),
                        openstudio_version=os_version,
                        stdout_path=ctx["stdout_log"],
                        stderr_path=ctx["stderr_log"],
                        max_retries=self.cfg.max_sample_retries,
                        worker_id="local",
                    )

        self._submit_and_await_all(
            submissions,
            "RUN_OPENSTUDIO_SIM",
            recovery_manager=recovery_manager,
            resubmit_callback=resubmit_callback,
        )

        # Record failures for samples that didn't succeed.
        for _sid, ctx in pending.items():
            state = ctx["state"]
            if state.get("sim_exit_code") != 0 and "sim_status" not in state:
                state["sim_exit_code"] = 1
                state["sim_status"] = "failed"
                state["error_summary"] = "SIM: unknown error during concurrent execution"
                self.cache.store(ctx["key"], ctx["out_dir"], exit_code=1)
                self.trace.step_item_done("RUN_OPENSTUDIO_SIM", status="failed")

        self.trace.step_finished(
            "RUN_OPENSTUDIO_SIM",
            cache="MISS×N" if n else "SKIPPED",
            elapsed_s=time.time() - t0,
            exit_code=0,
        )
        self._obs.record_step_duration(
            "RUN_OPENSTUDIO_SIM", time.time() - t0, generation=generation
        )
        return out

    def step_extract_kpis(  # noqa: PLR0915
        self,
        simulated: SampleDict,
        generation: int = 0,
    ) -> list[Path]:
        """Fan-out: for each simulated sample, extract KPIs."""
        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before EXTRACT_KPIS")

        out: list[Path] = []
        n = len(simulated)
        self.trace.step_started("EXTRACT_KPIS", total=n)

        # --- Phase 1: cache check for all samples ---
        pending: dict[str, dict[str, Any]] = {}
        for sid, sim_dir in simulated.items():
            inputs_hash = sha256_of_dict({"sim_dir": str(sim_dir), "sid": sid})
            key = CacheKey(
                step="EXTRACT_KPIS",
                sample_id=sid,
                openstudio_version="N/A",
                inputs_sha256=inputs_hash,
                code_sha256=self.code_hashes["bin"],
                container_digest=CONTAINER_PY,
                generation=generation,
            )
            state = self._sample_state.setdefault(sid, {})
            cached = self.cache.lookup(key)
            if cached:
                out.append(cached)
                state["extract_exit_code"] = 0
                state["extract_status"] = "cached"
                self.trace.step_item_done("EXTRACT_KPIS", status="cached")
                continue
            kpi_dir = self.cfg.work_dir / "kpis"
            kpi_dir.mkdir(parents=True, exist_ok=True)
            pending[sid] = {
                "sim_dir": sim_dir,
                "kpi_dir": kpi_dir,
                "key": key,
                "state": state,
            }

        # --- Phase 2: submit all non-cached samples at once ---
        submissions: dict[str, tuple[Handle | TQHandle, Callable[[Any], None]]] = {}
        for sid, ctx in pending.items():
            handle: Handle | TQHandle
            if self.task_queue is not None:
                handle = self.task_queue.submit(
                    self.extract_fn,
                    ctx["sim_dir"],
                    sid,
                    ctx["kpi_dir"],
                )
            else:
                handle = self.executor.submit(
                    self.extract_fn,
                    ctx["sim_dir"],
                    sid,
                    ctx["kpi_dir"],
                    name=f"kpi_{sid}",
                    cpus=1,
                    memory_mb=1024,
                    time_min=10,
                    container=CONTAINER_PY,
                )

            # Build the on-success callback (captures per-sample context).
            key = ctx["key"]
            state = ctx["state"]

            def _on_success(
                result_path: Any,
                _sid: str = sid,
                _key: CacheKey = key,
                _state: dict[str, object] = state,
            ) -> None:
                self.cache.store(_key, Path(result_path), exit_code=0)
                out.append(Path(result_path))
                _state["extract_exit_code"] = 0
                _state["extract_status"] = "ok"
                self.trace.step_item_done("EXTRACT_KPIS", status="ok")
                # Result storage upload (issue #339): upload KPI JSON after
                # successful extraction when a remote backend is configured.
                if self._result_storage is not None:
                    kpi_path = Path(result_path)
                    if kpi_path.is_file():
                        try:
                            self._result_storage.upload_file(
                                kpi_path,
                                f"kpis/{kpi_path.name}",
                            )
                            log.debug(
                                "result storage: uploaded KPI for sample %s",
                                _sid,
                            )
                        except OSError as exc:
                            log.warning(
                                "result storage: upload failed for KPI sample %s: %s",
                                _sid,
                                exc,
                            )

            submissions[sid] = (handle, _on_success)

        # --- Phase 3: await all results concurrently ---
        self._submit_and_await_all(submissions, "EXTRACT_KPIS")

        # Record failures for samples that didn't succeed.
        for _sid, ctx in pending.items():
            state = ctx["state"]
            if state.get("extract_exit_code") != 0 and "extract_status" not in state:
                state["extract_exit_code"] = 1
                state["extract_status"] = "failed"
                state["error_summary"] = "EXTRACT: unknown error during concurrent execution"
                self.trace.step_item_done("EXTRACT_KPIS", status="failed")

        self.trace.step_finished(
            "EXTRACT_KPIS",
            cache="MISS×N" if n else "SKIPPED",
            elapsed_s=time.time() - t0,
            exit_code=0,
        )
        self._obs.record_step_duration("EXTRACT_KPIS", time.time() - t0, generation=generation)
        return sorted(out)

    def step_compute_sensitivity_indices(
        self,
        samples: list[SampleSpec],
        kpi_files: list[Path],
        variables: dict[str, Any],
        generation: int = 0,
    ) -> Path | None:
        """Compute Sobol sensitivity indices after KPI extraction.

        This step runs only when ``cfg.algorithm == "sobol"``. It reads
        the per-sample KPI values, passes them to
        :meth:`SobolAlgorithm.compute_sensitivity_indices`, and stores
        the resulting ``sensitivity_indices.json`` in the campaign
        output directory.

        The step is not cached — sensitivity index computation is cheap
        relative to simulation and the user may want to re-run with
        different KPI selections.

        Parameters
        ----------
        samples
            Sample specs from ``step_generate_samples``.
        kpi_files
            Per-sample KPI JSON files from ``step_extract_kpis``.
        variables
            Parsed ``variables.yml`` dict.
        generation
            Generation number (included for consistency; Sobol is
            single-shot so this is always 0).

        Returns
        -------
        Path | None
            Path to ``sensitivity_indices.json``, or ``None`` if the
            algorithm is not ``"sobol"`` or no KPI files are available.
        """
        if self.cfg.algorithm != "sobol":
            return None

        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before COMPUTE_SENSITIVITY_INDICES")

        if not kpi_files:
            log.warning("COMPUTE_SENSITIVITY_INDICES: no KPI files — skipping")
            self.trace.step_finished(
                "COMPUTE_SENSITIVITY_INDICES",
                cache="SKIPPED",
                elapsed_s=0.0,
                exit_code=0,
            )
            return None

        # Build {sample_id: {kpi_name: value}} mapping from KPI files.
        kpi_values: dict[str, dict[str, float]] = {}
        for kpi_path in kpi_files:
            try:
                data = json.loads(kpi_path.read_text())
                sid = str(data.get("sample_id", kpi_path.stem.replace("kpi_", "")))
                kpis = data.get("kpis", {})
                numeric_kpis = {k: float(v) for k, v in kpis.items() if isinstance(v, (int, float))}
                kpi_values[sid] = numeric_kpis
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                log.warning("could not read KPI file %s: %s", kpi_path, exc)

        algo = AlgorithmRegistry.get("sobol")
        indices_dir = self.cfg.outdir / "sensitivity"
        indices_dir.mkdir(parents=True, exist_ok=True)

        try:
            indices_path = algo.compute_sensitivity_indices(
                variables=variables,
                samples=samples,  # type: ignore[arg-type]
                kpi_values=kpi_values,
                outdir=indices_dir,
            )
        except Exception as exc:
            log.error(
                "COMPUTE_SENSITIVITY_INDICES failed: %s",
                exc,
                exc_info=True,
            )
            self.trace.step_finished(
                "COMPUTE_SENSITIVITY_INDICES",
                cache="MISS",
                elapsed_s=time.time() - t0,
                exit_code=1,
            )
            raise RuntimeError("compute_sensitivity_indices failed") from exc

        elapsed = time.time() - t0
        self.trace.step_finished(
            "COMPUTE_SENSITIVITY_INDICES",
            cache="MISS",
            elapsed_s=elapsed,
            exit_code=0,
        )
        self._obs.record_step_duration(
            "COMPUTE_SENSITIVITY_INDICES", elapsed, generation=generation
        )
        log.info("COMPUTE_SENSITIVITY_INDICES: wrote %s", indices_path)
        return indices_path

    def step_aggregate_results(
        self,
        kpi_files: list[Path],
        simulated: SampleDict,
        baseline_sample_id: str | None = None,
    ) -> dict[str, Path]:
        """Single-shot: aggregate all KPIs into a CSV + Parquet + failed CSV.

        Args:
            kpi_files: per-sample KPI JSON files from step_extract_kpis.
            simulated: mapping of sample_id to simulation output directory.
            baseline_sample_id: optional baseline sample ID (issue #64).
                When provided, the aggregator adds pct improvement columns.
        """
        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before AGGREGATE_RESULTS")

        sim_dirs = list(simulated.values())
        inputs_hash = sha256_of_dict(
            {
                "kpis": [str(p) for p in kpi_files],
                "sims": [str(p) for p in sim_dirs],
            }
        )
        key = CacheKey(
            step="AGGREGATE_RESULTS",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256=inputs_hash,
            code_sha256=self.code_hashes["bin"],
            container_digest=CONTAINER_PY,
        )
        cached = self.cache.lookup(key)
        if cached:
            elapsed = time.time() - t0
            self.trace.step_finished(
                "AGGREGATE_RESULTS",
                cache="HIT",
                elapsed_s=elapsed,
                exit_code=0,
            )
            self._obs.record_step_duration("AGGREGATE_RESULTS", elapsed)
            return {
                "csv": cached,
                "parquet": cached.parent / "aggregated_results.parquet",
                "failed": cached.parent / "failed_simulations.csv",
            }
        handle = self.executor.submit(
            aggregate_results,
            kpi_files,
            sim_dirs,
            self.cfg.outdir,
            baseline_sample_id=baseline_sample_id,
            samples_json=self.cfg.samples_file,
            name="aggregate",
            cpus=2,
            memory_mb=4 * 1024,
            time_min=15,
            container=CONTAINER_PY,
        )
        result_obj: object = handle.result(timeout=300)
        result = cast_aggregate_result(result_obj)
        self.cache.store(key, result["csv"], exit_code=0)
        elapsed = time.time() - t0
        self.trace.step_finished(
            "AGGREGATE_RESULTS",
            cache="MISS",
            elapsed_s=elapsed,
            exit_code=0,
        )
        self._obs.record_step_duration("AGGREGATE_RESULTS", elapsed)
        return result

    def step_generate_plots(
        self,
        aggregated: dict[str, Path],
        baseline_sample_id: str | None = None,
    ) -> list[Path]:
        """Single-shot: render 1-3 summary plots. Not cached — plots are
        cheap to regenerate and the user may want to tweak styling.

        Args:
            aggregated: dict with 'csv' and 'failed' paths from aggregation.
            baseline_sample_id: optional baseline sample ID (issue #64).
                When provided, the plot generator adds a vertical reference
                line for the baseline EUI on the EUI histogram.
        """
        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before GENERATE_BASIC_PLOTS")

        plots_dir = self.cfg.outdir / "plots"
        handle = self.executor.submit(
            generate_plots,
            aggregated["csv"],
            aggregated["failed"],
            plots_dir,
            baseline_sample_id=baseline_sample_id,
            name="plots",
            cpus=1,
            memory_mb=1024,
            time_min=10,
            container=CONTAINER_PY,
        )
        result_obj: object = handle.result(timeout=120)
        result = cast_plot_paths(result_obj)
        elapsed = time.time() - t0
        self.trace.step_finished(
            "GENERATE_BASIC_PLOTS",
            cache="SKIPPED",
            elapsed_s=elapsed,
            exit_code=0,
        )
        self._obs.record_step_duration("GENERATE_BASIC_PLOTS", elapsed)
        return result

    def warm_cache(self, n_warm: int = 10) -> dict[str, object]:
        """Run N pilot samples to pre-populate the cache before a campaign.

        Generates N pilot samples using the configured sampling algorithm,
        runs ``APPLY_PARAMETERS`` and ``RUN_OPENSTUDIO_SIM`` for each, and
        stores results in the SQLite cache.  When the real campaign starts
        with the same configuration, those steps will hit the cache instead
        of re-running the simulation.

        This method intentionally skips KPI extraction and result aggregation
        — those steps are not cached and running them for pilot samples would
        add latency for no cache benefit.

        Args:
            n_warm: number of pilot samples to run (default 10).

        Returns:
            dict with ``n_samples`` and ``cache_stats``.
        """
        log.info("Cache warming: generating %d pilot samples", n_warm)
        algo = AlgorithmRegistry.get(self.cfg.algorithm)
        warm_dir = self.cfg.outdir / ".osimflow_warm"
        warm_dir.mkdir(parents=True, exist_ok=True)

        variables: dict[str, Any] = {}
        if self.cfg.input_variables.exists():
            with self.cfg.input_variables.open() as fh:
                raw = yaml.safe_load(fh)
                if isinstance(raw, dict):
                    variables = raw

        result_path = algo.generate_samples(
            variables=variables,
            n_samples=n_warm,
            seed=None,
            outdir=warm_dir,
        )
        samples_obj: object = json.loads(Path(result_path).read_text())["samples"]
        pilot_specs: list[SampleSpec] = cast_samples(samples_obj)

        log.info(
            "Cache warming: running apply + sim for %d pilot samples (populating cache)",
            len(pilot_specs),
        )
        parameterized: SampleDict = self.step_apply_parameters(pilot_specs)
        self.step_run_openstudio_sim(parameterized, generation=0)
        stats = self.cache.get_stats()
        log.info(
            "Cache warming complete: %d/%d steps cached",
            stats.hits,
            stats.hits + stats.misses,
        )
        return {
            "n_samples": len(pilot_specs),
            "cache_stats": {
                "hits": stats.hits,
                "misses": stats.misses,
                "total_keys": stats.total_keys,
            },
        }


# ---------------------------------------------------------------------------
# MLflow param view (issue #7)
def _make_mlflow_param_view(cfg: CampaignConfig, executor_name: str) -> SimpleNamespace:
    """Build a small adapter for `log_mlflow_params`.

    The hook reads attributes via `getattr`, so a `SimpleNamespace` is
    sufficient. The adapter is built from a `CampaignConfig` + the
    executor's `name` so the hook does not import `osimflow.config`
    (avoiding a circular import).
    """
    return SimpleNamespace(
        executor=executor_name,
        openstudio_version=cfg.openstudio_version,
        n_samples=cfg.n_samples,
        archive_intermediates=cfg.archive_intermediates,
    )


# ---------------------------------------------------------------------------
# Cast helpers — narrow YAML/external `object` types to the strict shapes
# the rest of the code relies on. The cast is a single audit point.
# ---------------------------------------------------------------------------
def cast_samples(obj: object) -> list[SampleSpec]:
    """Narrow a `samples` JSON value to the canonical SampleSpec list."""
    if not isinstance(obj, list):
        raise TypeError(f"samples must be a list, got {type(obj).__name__}")
    out: list[SampleSpec] = []
    for item in obj:
        if not isinstance(item, dict):
            raise TypeError("sample entry must be a dict")
        sid = item.get("sample_id")
        values = item.get("values")
        if not isinstance(sid, str) or not isinstance(values, dict):
            raise TypeError("sample entry must have str 'sample_id' and dict 'values'")
        out.append(SampleSpec(sample_id=sid, values=values))
    return out


def cast_variables(obj: object) -> list[VariableSpec]:
    """Narrow the `variables` list from variables.yml.

    Each entry is a dict with at least `name` and `distribution` keys,
    plus distribution-specific numeric fields.
    """
    if not isinstance(obj, list):
        raise TypeError(f"variables must be a list, got {type(obj).__name__}")
    out: list[VariableSpec] = []
    for item in obj:
        if not isinstance(item, dict) or "name" not in item or "distribution" not in item:
            raise TypeError("variable entry must have 'name' and 'distribution'")
        v: VariableSpec = {
            "name": str(item["name"]),
            "distribution": str(item["distribution"]),
            "min": float(item.get("min", 0.0)),
            "max": float(item.get("max", 0.0)),
            "mean": float(item.get("mean", 0.0)),
            "sigma": float(item.get("sigma", 0.0)),
        }
        out.append(v)
    return out


def cast_aggregate_result(obj: object) -> dict[str, Path]:
    """Narrow aggregate_results() return to a Path-keyed dict."""
    if not isinstance(obj, dict):
        raise TypeError(f"aggregate result must be a dict, got {type(obj).__name__}")
    out: dict[str, Path] = {}
    for k, v in obj.items():
        out[str(k)] = Path(str(v))
    return out


def cast_plot_paths(obj: object) -> list[Path]:
    """Narrow generate_plots() return to a list[Path]."""
    if not isinstance(obj, list):
        raise TypeError(f"plot list must be a list, got {type(obj).__name__}")
    out: list[Path] = []
    for item in obj:
        out.append(Path(str(item)))
    return out
