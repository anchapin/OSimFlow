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

Each step is cached via the cache built by `build_cache`: a plain
`SQLiteCache` in single-node local mode (the default), or a
`DistributedCache` when `--redis-url` is configured (issue #993). Each
per-sample step submits to
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

__all__ = ["Campaign", "CampaignError", "QuotaExceededError", "SimResult"]

import concurrent.futures
import contextlib
import dataclasses
import fcntl
import hashlib
import inspect
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict

import yaml

from ._campaign_cost_tracker import CampaignCostTracker
from ._campaign_observability import ObservabilityManager
from .alerting import AlertManager, build_alert_manager
from .algorithms import AlgorithmRegistry, BaseAlgorithm
from .apply_params import (
    EPW_FILE_KEY,
    _build_mappings,
    preflight_check,
)
from .cache import CacheKey, _container_digest_for, sha256_of_dict, sha256_of_files
from .chaos import (
    ChaosEngine,
    CPUSpikeInjector,
    KillSwitchSimulator,
    MemoryPressureInjector,
    NetworkDelayInjector,
)
from .config import CampaignConfig
from .cost_tracking import (
    DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR,
    DEFAULT_SPOT_PRICE_PER_VCPU_HOUR,
)
from .data_point_manager import DataPointManager
from .distributed_cache import build_cache, campaign_state_namespace
from .distributed_jobqueue import build_job_queue
from .executors import BaseExecutor, Handle
from .json_utils import safe_json_dumps, safe_json_loads
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
from .observability import new_trace_id
from .pareto import ParetoFront, ParetoSolution
from .registry import CampaignRegistry
from .storage import ResultStorageUploader, build_result_storage
from .taskqueue import ConsumerQueue
from .taskqueue import TaskHandle as TQHandle
from .weather import EPWValidationError, validate_all_epw_files, validate_epw
from .webhook import WebhookClient
from .work import (
    SevereEnergyPlusError,
    aggregate_results,
    default_apply_parameters,
    extract_kpis,
    generate_plots,
    preflight_run_model,
    publish_kpi_results,
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


class CampaignError(RuntimeError):
    """Raised when a critical campaign-level error should abort execution.

    Unlike step-level errors that are caught and recorded in run.json,
    a CampaignError signals an unrecoverable condition that should halt
    the campaign immediately.
    """


# Type aliases — these are the schemas of intermediate DAG outputs.
class SampleSpec(TypedDict, total=False):
    sample_id: str
    values: dict[str, object]
    # Per-sample override paths (GAP-009). When set on a sample, these
    # replace the campaign-level template_sim_package (seed_model) or
    # weather file (weather_file) for that sample only.
    seed_model: str
    weather_file: str


class VariableSpec(TypedDict, total=False):
    name: str
    distribution: str
    min: float
    max: float
    mean: float
    sigma: float


SampleDict = dict[str, Path]  # sample_id -> path (per-sample work dir)


@dataclasses.dataclass(frozen=True)
class SimResult:
    """Structured result from ``step_run_openstudio_sim``.

    The ``success`` field is ``False`` when one or more samples recorded a
    non‑zero ``sim_exit_code`` (i.e. partial or complete fan‑out failure).
    Callers can inspect this field instead of needing to parse ``run.json``.
    """

    samples: SampleDict
    success: bool


@dataclasses.dataclass(frozen=True)
class StepOutputs:
    """Describes the files produced by a DAG step."""

    produced: tuple[str, ...] = ()
    """Glob patterns relative to work_dir (e.g. 'apply/*/modified.osw')."""

    kpi_pattern: str | None = None
    """For fan-out steps: glob pattern for per-sample KPI JSON files."""


@dataclasses.dataclass(frozen=True)
class StepInputs:
    """Describes the files required by a DAG step before it can run."""

    required: tuple[str, ...] = ()
    """Exact file paths required (checked via is_file())."""

    required_patterns: tuple[str, ...] = ()
    """Glob patterns relative to work_dir; ALL files matching must exist."""

    count: int | None = None
    """Expected number of files from a fan-out step (None = no check)."""


@dataclasses.dataclass(frozen=True)
class DAGStep:
    """Describes a DAG step for data-driven execution (issues #1276, #1392).

    Extends ``StepInputs``/``StepOutputs`` with step execution metadata so
    that ``_run_one_generation`` can iterate over steps from configuration
    instead of calling step methods by name.

    Issue #1392: each step now declares its own input/output contract via
    :attr:`inputs_signature` and :attr:`outputs_signature`.  The prior
    if/elif chain in ``_run_one_generation`` was the only contract for
    step-method signatures — any new step declared in ``_STEP_DEPENDENCIES``
    had no documented path to register its own args.  With the new
    signatures, ``step_method(*inputs_signature(state))`` is generic;
    new steps just plug in their own callable.
    """

    inputs: StepInputs
    outputs: StepOutputs
    method: str
    condition: Callable[..., bool] | None = None
    fan_out: bool = False
    inputs_signature: Callable[..., tuple[Any, ...]] | None = None
    """Callable returning the positional arg tuple for ``method``.

    Receives the per-generation state namespace (a ``SimpleNamespace``
    exposing ``samples``, ``parameterized``, ``simulated``, ``kpi_files``,
    ``aggregated``, ``campaign``, ``algo``, ``generation``).  Each step's
    signature callable picks the slots it needs and ignores the rest.
    When ``None``, ``method`` is called with no args.
    """
    outputs_signature: Callable[[Any], tuple[str, Any] | None] | None = None
    """Callable taking the method's return value and returning
    ``(slot_name, slot_value)`` to merge into the per-generation state.

    Built-in slot names: ``"parameterized"``, ``"simulated"``,
    ``"kpi_files"``, ``"aggregated"``.  When ``None``, the return value
    is ignored (e.g. for ``step_generate_samples`` whose result is
    captured before the dispatcher loop runs)."""


def _always_run(campaign: "Campaign", algo: Any, **kwargs: Any) -> bool:
    return True


# ---------------------------------------------------------------------------
# Per-step inputs/outputs signatures (issue #1392).
#
# These helpers close over the per-generation state namespace exposed by
# ``_run_one_generation``.  Each step reads only the slots it needs and
# returns the positional arg tuple for its method.
#
# Output-signature helpers take the method's return value and return a
# ``(slot_name, slot_value)`` pair that the dispatcher writes back into
# the per-generation state.  Steps whose return value is not consumed by
# any downstream step leave ``outputs_signature=None``.
# ---------------------------------------------------------------------------


def _sig_lhs_samples(
    state: Any, campaign: "Campaign", algo: Any, generation: int
) -> tuple[Any, ...]:
    return (algo, generation)


def _sig_preflight(state: Any, campaign: "Campaign", algo: Any, generation: int) -> tuple[Any, ...]:
    return ()


def _sig_apply(state: Any, campaign: "Campaign", algo: Any, generation: int) -> tuple[Any, ...]:
    return (state.samples, generation)


def _sig_validate_measure(
    state: Any, campaign: "Campaign", algo: Any, generation: int
) -> tuple[Any, ...]:
    return ()


def _sig_run_sim(state: Any, campaign: "Campaign", algo: Any, generation: int) -> tuple[Any, ...]:
    return (state.parameterized, generation)


def _sig_extract_kpis(
    state: Any, campaign: "Campaign", algo: Any, generation: int
) -> tuple[Any, ...]:
    return (state.simulated, generation)


def _out_parameterized(result: Any) -> tuple[str, Any]:
    return ("parameterized", result)


def _out_simulated(result: Any) -> tuple[str, Any]:
    """``step_run_openstudio_sim`` returns a ``SimResult`` whose ``.samples``
    field is the per-sample ``{sample_id: sim_dir}`` mapping the
    dispatcher feeds into ``step_extract_kpis``."""

    return ("simulated", result.samples)


def _out_kpi_files(result: Any) -> tuple[str, Any]:
    return ("kpi_files", result)


# Cross-step data dependency map (issue #850).
# Maps each step name to the outputs it PRODUCES and the inputs it CONSUMES.
# Before running a step, _verify_step_inputs() confirms all required inputs
# are present so e.g. AGGREGATE_RESULTS cannot run until all EXTRACT_KPIS
# outputs are confirmed on disk.
#
# For issue #1276, this is extended with execution metadata (method name,
# condition, fan_out) so new steps can be added via configuration alone.
_STEP_DEPENDENCIES: dict[str, DAGStep] = {
    "GENERATE_LHS_SAMPLES": DAGStep(
        inputs=StepInputs(),
        outputs=StepOutputs(produced=("samples.json",)),
        method="step_generate_samples",
        condition=_always_run,
        inputs_signature=_sig_lhs_samples,
    ),
    "PREFLIGHT_RUN_MODEL": DAGStep(
        inputs=StepInputs(),
        outputs=StepOutputs(produced=("preflight_OK",)),
        method="step_preflight_run_model",
        condition=lambda campaign, algo, generation, **_: generation == 0,
        inputs_signature=_sig_preflight,
    ),
    "APPLY_PARAMETERS": DAGStep(
        inputs=StepInputs(required=("samples.json",)),
        outputs=StepOutputs(produced=("apply/*/",)),
        method="step_apply_parameters",
        fan_out=False,
        inputs_signature=_sig_apply,
        outputs_signature=_out_parameterized,
    ),
    "VALIDATE_MEASURE_VARIABLES": DAGStep(
        inputs=StepInputs(),
        outputs=StepOutputs(produced=()),
        method="step_validate_measure_variables",
        inputs_signature=_sig_validate_measure,
    ),
    "RUN_OPENSTUDIO_SIM": DAGStep(
        inputs=StepInputs(required_patterns=("apply/*/",)),
        outputs=StepOutputs(produced=("sim/*/",)),
        method="step_run_openstudio_sim",
        fan_out=True,
        inputs_signature=_sig_run_sim,
        outputs_signature=_out_simulated,
    ),
    "EXTRACT_KPIS": DAGStep(
        inputs=StepInputs(required_patterns=("sim/*/",)),
        outputs=StepOutputs(kpi_pattern="kpis/kpi_*.json"),
        method="step_extract_kpis",
        fan_out=True,
        inputs_signature=_sig_extract_kpis,
        outputs_signature=_out_kpi_files,
    ),
    "COMPUTE_SENSITIVITY_INDICES": DAGStep(
        inputs=StepInputs(),
        outputs=StepOutputs(produced=("sensitivity_indices.json",)),
        method="step_compute_sensitivity_indices",
        condition=lambda campaign, algo, **_: campaign.cfg.algorithm == "sobol",
    ),
    "COMPUTE_UQ_INDICES": DAGStep(
        inputs=StepInputs(),
        outputs=StepOutputs(produced=("uq_results.json",)),
        method="step_compute_uq_indices",
        condition=lambda campaign, algo, **_: campaign.cfg.algorithm == "uq",
    ),
    # Note (issue #1419): ``AGGREGATE_RESULTS`` and ``GENERATE_BASIC_PLOTS``
    # are intentionally NOT declared in this dict.  Both are single-shot
    # steps that run exactly once in ``_finalize_full_campaign`` after the
    # per-generation loop completes.  Including them here would cause two
    # problems: (a) ``_verify_step_inputs("GENERATE_BASIC_PLOTS")`` would
    # fire inside the loop and demand ``../aggregated_results.csv`` before
    # AGGREGATE_RESULTS has had a chance to write it (the bug from the
    # v0 stub-mode failure), and (b) per-generation calls would either
    # over-cache or overwrite the canonical single-shot result with
    # partial data.  Keeping them out of the loop preserves both the
    # "single-shot" semantics and the original cache-key behaviour.
}


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


@contextlib.contextmanager
def _scoped_dry_run_env() -> Iterator[None]:
    """Set ``OSIMFLOW_DOCKER_SWARM_DRY_RUN=1`` for the duration of a block.

    The ``DockerSwarmExecutor`` reads this env var in
    ``_is_dev_fallback_enabled()`` to decide whether to fall back to
    ``LocalExecutor`` during ``--dry-run`` campaigns (issue #944).

    Captures and restores the prior value so the var never leaks across
    campaigns in a long-lived process (e.g. ``osimflow serve``) or across
    tests sharing a worker process under ``pytest-xdist`` (issue #976).
    Restore runs even when the wrapped block raises.
    """
    prev = os.environ.get("OSIMFLOW_DOCKER_SWARM_DRY_RUN")
    os.environ["OSIMFLOW_DOCKER_SWARM_DRY_RUN"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("OSIMFLOW_DOCKER_SWARM_DRY_RUN", None)
        else:
            os.environ["OSIMFLOW_DOCKER_SWARM_DRY_RUN"] = prev


def _byos_file_hash(path: Path | None) -> str:
    """SHA-256 of a BYOS user script, or a sentinel when unset/missing.

    Issue #1011. The returned string is mixed into the cache key for
    ``APPLY_PARAMETERS`` and ``EXTRACT_KPIS`` so that editing the
    user-supplied script invalidates the cached results. Three outcomes:

    * ``path is None`` → ``"byos-unset"``. No BYOS script configured;
      the cache key falls back to ``code_hashes["bin"]`` unchanged.
    * ``path.resolve().is_file() is False`` → ``"byos-missing"``. The
      configured script is unreadable; do not raise — return a stable
      sentinel so the cache key remains deterministic (issue #1011
      stop condition).
    * otherwise → ``sha256_of_files([path.resolve()])`` of the file
      bytes, using the same hashing primitive as ``_compute_code_hashes``
      so the rest of the cache key is consistent.
    """
    if path is None:
        return "byos-unset"
    try:
        resolved = path.resolve()
    except OSError:
        return "byos-missing"
    if not resolved.is_file():
        return "byos-missing"
    return sha256_of_files([resolved])


def _combine_code_hash(*hashes: str) -> str:
    """SHA-256 of the concatenation of multiple code-hash strings.

    Used by ``Campaign._code_hash_with_byos`` (issue #1011) to fold
    the BYOS user-script hash into the existing ``code_hashes["bin"]``
    without changing the schema of :class:`CacheKey.code_sha256`. Any
    change in any input produces a different output, so editing the
    user script invalidates the cache key.
    """
    h = hashlib.sha256()
    for part in hashes:
        h.update(part.encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


def _build_default_chaos_engine(cfg: Any) -> ChaosEngine:
    """Build a :class:`ChaosEngine` from :class:`ChaosConfig` settings.

    Issue #1013 wires the chaos module into :class:`Campaign`; this
    helper turns the user-facing config knobs into the matching
    fault injectors and registers them on a fresh ``ChaosEngine``.
    The schedule is *not* enforced here — it lives in
    :meth:`Campaign._maybe_inject_chaos` so the engine itself stays
    neutral.

    All scenario names are validated at config parse time in
    :func:`osimflow.config._parse_chaos_scenarios` (issue #1209), so
    no further unknown-name handling is needed here.
    """
    engine = ChaosEngine(enabled=True)
    scenarios = list(cfg.scenarios)
    for name in scenarios:
        if name in ("kill_switch", "kill_switch_simulator"):
            engine.register(KillSwitchSimulator(fail_after=cfg.fail_after))
        elif name == "network_delay":
            engine.register(
                NetworkDelayInjector(
                    delay_s=cfg.delay_s,
                    jitter_s=cfg.jitter_s,
                    probability=cfg.probability,
                )
            )
        elif name == "cpu_spike":
            engine.register(
                CPUSpikeInjector(
                    duration_s=cfg.duration_s,
                    intensity=cfg.intensity,
                    probability=cfg.probability,
                )
            )
        elif name == "memory_pressure":
            engine.register(
                MemoryPressureInjector(
                    size_mb=cfg.size_mb,
                    duration_s=cfg.duration_s,
                    probability=cfg.probability,
                )
            )
    return engine


class Campaign:
    def __init__(  # noqa: PLR0912
        self,
        cfg: CampaignConfig,
        executor: BaseExecutor,
        apply_fn: Callable[..., Path | None] | None = None,
        extract_fn: Callable[..., Path] | None = None,
        max_workers: int = 1,
        task_queue: ConsumerQueue | None = None,
        data_point_manager: DataPointManager | None = None,
        chaos_engine: ChaosEngine | None = None,
    ):
        self.cfg = cfg
        # NOTE: the OSIMFLOW_DOCKER_SWARM_DRY_RUN env var is scoped to
        # ``run()`` (see ``_scoped_dry_run_env``) so it cannot leak
        # across campaigns/tests in a long-lived process (issue #976).
        self.executor = executor
        self.max_workers = max_workers
        self.task_queue = task_queue
        # Per-sample override manager (GAP-009). When set, per-sample
        # seed_model and weather_file overrides from the DataPointManager
        # are injected into SampleSpec before processing.
        self._dp_manager = data_point_manager
        # Resolve apply_fn: explicit param > cfg.custom_apply_script > default.
        if apply_fn is not None:
            self.apply_fn = apply_fn
        elif cfg.custom_apply_script is not None:
            from .byos import load_user_function  # noqa: PLC0415

            log.info("loading BYOS apply_fn from %s", cfg.custom_apply_script)
            self.apply_fn = load_user_function(
                cfg.custom_apply_script,
                trust_level=cfg.byos_trust_level,
                timeout_s=cfg.byos_timeout_s,
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
                cfg.custom_kpi_extractor,
                trust_level=cfg.byos_trust_level,
                timeout_s=cfg.byos_timeout_s,
            )
        else:
            self.extract_fn = extract_kpis
        # Campaign state backend (issue #993 / T8.2). When --redis-url is
        # configured, build_cache returns a DistributedCache: shared cache
        # entries live in a Redis hash under a namespace derived from the
        # (shared) outdir, and each process uses a pid-private local
        # SQLite file — so concurrent campaign processes coordinating on
        # the same state never contend on a single SQLite database (the
        # T8.1 lock reproducer). When redis_url is None (the default),
        # this is a plain SQLiteCache at cfg.cache_db — single-node
        # behaviour is unchanged.
        self._cache_namespace = campaign_state_namespace(cfg.outdir)
        self.cache = build_cache(
            db_path=cfg.cache_db,
            redis_url=cfg.redis_url,
            campaign_id=self._cache_namespace,
        )
        self._python_container_image = os.environ.get(
            "OSIMFLOW_PYTHON_CONTAINER_IMAGE",
            CONTAINER_PY,
        )
        if self._python_container_image != CONTAINER_PY:
            log.info(
                "using override for Python container image: %s",
                self._python_container_image,
            )
        # Resolve the content-addressable digests ONCE per campaign so
        # cache keys are stable across the entire run and `docker inspect`
        # is not called per-sample. ``_container_digest_for`` falls back to
        # a SHA-256 of the label when docker is absent, hung, or the image
        # is not pulled locally. See issue #1023.
        self._python_container_digest = _container_digest_for(self._python_container_image)
        self._os_container_digest = _container_digest_for(
            CONTAINER_OS.format(version=cfg.openstudio_version)
        )
        # Issue #1081: when the caller pins images by SHA256 digest via
        # ``--container-digest``, honor that digest for BOTH images instead
        # of the resolved mutable-tag digest. The digest is content-addressable
        # across machines, so cache keys stay stable and no executor ever sees
        # a bare ``:latest`` / ``:<version>`` tag as the image reference.
        if cfg.container_digest:
            self._python_container_digest = cfg.container_digest
            self._os_container_digest = cfg.container_digest
            log.info("container images pinned by digest: %s", cfg.container_digest)
        # Hash the code that affects per-step behavior so a `bin/*.py` edit
        # invalidates cached results. This is the fix for the
        # "Python glue invisible to cache hash" gotcha in
        # `.agents/results/result-architecture.md` issue #2. Passing the
        # cfg makes ``_compute_code_hashes`` also include the BYOS
        # user-script content hashes (issue #1011) so editing the
        # user-supplied apply / KPI scripts invalidates the per-sample
        # cache.
        self.code_hashes = self._compute_code_hashes(cfg)
        chaos_summary: dict[str, object] = {}
        chaos_cfg_obj = getattr(cfg, "chaos", None)
        if chaos_cfg_obj is not None:
            chaos_summary = {
                "enabled": bool(getattr(chaos_cfg_obj, "enabled", False)),
                "scenarios": list(getattr(chaos_cfg_obj, "scenarios", [])),
                "schedule": str(getattr(chaos_cfg_obj, "schedule", "none")),
            }
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
                "shard_count": cfg.shard_count,
                "shard_index": cfg.shard_index,
                "shard_start": cfg.shard_start,
                "shard_end": cfg.shard_end,
                "nomad_fanout_submit_rate_per_sec": cfg.nomad_fanout_submit_rate_per_sec,
                "nomad_fanout_submit_chunk_size": cfg.nomad_fanout_submit_chunk_size,
                "chaos": chaos_summary,
            },
        )
        # Per-sample accumulator. The three per-sample steps write here;
        # we emit SampleTrace rows in _finalize_samples().
        self._sample_state: dict[str, dict[str, object]] = {}
        self._latest_samples_file: Path = self.cfg.samples_file
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
        # Observability backend (issue #132). Built from cfg so the
        # correct backend is always used — NullBackend when "none" (zero
        # overhead) or a real backend when configured.
        self._obs = ObservabilityManager(cfg)

        # Wire CircuitBreaker state transitions to observability (issue #1310).
        # _breaker callbacks are set here after _obs is available; the breakers
        # themselves were created earlier in self.cache and self._document_store.
        self._wire_circuit_breaker_callbacks()

        # Alerting (issue #1180). Built from cfg so alerts fire for
        # step failures, sample failures, worker death, and quota-exceeded
        # events during campaign execution.
        self._alert_manager: AlertManager | None = None
        obs_cfg = getattr(cfg, "_observability", None)
        if obs_cfg is not None and obs_cfg.alert_rules is not None:
            try:
                self._alert_manager = build_alert_manager(
                    rules_path=obs_cfg.alert_rules,
                    destinations_path=obs_cfg.alert_destinations,
                    on_alert=self.trace.record_alert,
                )
                log.info(
                    "alerting enabled: rules=%s destinations=%s",
                    obs_cfg.alert_rules,
                    obs_cfg.alert_destinations,
                )
            except Exception as exc:
                log.warning("could not initialize AlertManager: %s — continuing without", exc)

        # Campaign registry (issue #266). Auto-register on run start
        # and update status on completion.
        self._registry: CampaignRegistry | None = None
        reg_path = getattr(cfg, "registry_path", None)
        try:
            self._registry = CampaignRegistry(db_path=reg_path)
        except Exception as exc:
            log.warning(
                "could not open campaign registry: %s (continuing without)", exc, exc_info=True
            )

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
                    allow_insecure_endpoint=cfg.allow_insecure_storage_endpoint,
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
        self._executor_submit_transport_kwargs: dict[str, Any] = {
            "result_transport_mode": "shared_fs",
        }
        # Ephemeral-runner executors (Nomad, Kubernetes — issue #996) push
        # results job-side to object storage when a backend is configured;
        # their handles materialize the artifacts back to local paths.
        if self.executor.requires_remote_runner_payload and self._result_storage is not None:
            self._executor_submit_transport_kwargs = {
                "result_transport_mode": "object_storage",
                "result_storage_backend": cfg.result_storage_backend,
                "result_storage_bucket": cfg.result_storage_bucket,
                "result_storage_prefix": str(cfg.outdir.name),
                "result_storage_endpoint": cfg.result_storage_endpoint,
            }

        # Cost tracking (issue #447). Built here so the correct backend is
        # always used.  None when enable_cost_tracking is False (zero overhead).
        self._cost_tracker = CampaignCostTracker(
            campaign_id=self.trace.campaign_id,
            cfg=cfg,
            executor=executor,
        )

        # Chaos engine (issue #1013). When the user passes an explicit
        # ``chaos_engine`` we use it directly; otherwise we build one
        # from ``cfg.chaos`` if ``enabled=True`` and at least one
        # scenario is listed. The engine stays None for every campaign
        # that does not opt in, so ``_maybe_inject_chaos`` is a no-op
        # unless explicitly requested — the wiring is invisible to
        # non-chaos campaigns.
        chaos_cfg = getattr(cfg, "chaos", None)
        if chaos_engine is not None:
            self._chaos_engine: ChaosEngine | None = chaos_engine
        elif chaos_cfg is not None and chaos_cfg.enabled and chaos_cfg.scenarios:
            self._chaos_engine = _build_default_chaos_engine(chaos_cfg)
        else:
            self._chaos_engine = None
        if self._chaos_engine is not None:
            scenarios_repr: object
            schedule_repr: object
            if chaos_cfg is not None and chaos_cfg.scenarios:
                scenarios_repr = list(chaos_cfg.scenarios)
                schedule_repr = chaos_cfg.schedule
            else:
                # User supplied a custom engine; describe what is
                # actually registered so the log line is not a lie.
                scenarios_repr = [
                    getattr(inj, "fault_type", None)
                    and getattr(inj.fault_type, "value", None)
                    or type(inj).__name__
                    for inj in self._chaos_engine._injectors
                ]
                schedule_repr = "custom"
            log.info(
                "chaos engine enabled: scenarios=%s schedule=%s",
                scenarios_repr,
                schedule_repr,
            )

        # Graceful shutdown (issue #255): cancellation flag and lock.
        self._cancel_requested = False
        self._cancel_lock = threading.Lock()
        # Soft pause (issue #553): pause flag and lock.
        self._pause_requested = False
        self._pause_lock = threading.Lock()
        # Original signal handlers — restored on exit.
        self._prev_sigint: Any = None
        self._prev_sigterm: Any = None

        # Consecutive checkpoint failure counter (issue #739). After N
        # consecutive failures in _checkpoint_sample we abort instead of
        # silently continuing.
        self._consecutive_checkpoint_failures = 0

    def _maybe_alert(self, event_type: str, context: dict[str, Any]) -> None:
        """Send an alert if AlertManager is configured (issue #1180).

        Best-effort: failures are silently logged and never propagate.
        """
        if self._alert_manager is not None:
            try:
                self._alert_manager.notify(event_type, context)
            except Exception as exc:
                log.warning("alert %s failed: %s", event_type, exc)

    def _wire_circuit_breaker_callbacks(self) -> None:
        """Wire CircuitBreaker state transitions to the observability backend (issue #1310).

        Called after ``self._obs`` is initialised so the backend is available.
        The circuit breakers themselves were created earlier during
        ``self.cache`` and ``self._document_store`` construction.
        """
        backend = self._obs.backend

        def _record(circuit_name: str, from_state: str, to_state: str) -> None:
            backend.record_circuit_breaker_event(circuit_name, from_state, to_state)

        # Wire DistributedCache breaker.
        cache = getattr(self, "cache", None)
        if cache is not None and hasattr(cache, "_breaker"):
            cache._breaker.set_on_transition_callback(_record)

        # Wire RedisDocumentStore breaker.
        doc_store = getattr(self, "_document_store", None)
        if doc_store is not None and hasattr(doc_store, "_breaker"):
            doc_store._breaker.set_on_transition_callback(_record)

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
            self._maybe_alert(
                "quota.exceeded",
                {
                    "campaign_id": self.trace.campaign_id,
                    "quota_type": "max_samples",
                    "limit": quota.max_samples,
                    "current": self.cfg.n_samples,
                    "message": f"n_samples={self.cfg.n_samples} exceeds max_samples={quota.max_samples}",
                },
            )
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

    def _verify_step_inputs(self, step_name: str) -> None:
        """Verify all required inputs for *step_name* are present before running.

        This is the cross-step data dependency check for issue #850.
        Checks that every file described in ``_STEP_DEPENDENCIES[step_name]``
        actually exists on disk.  Raises ``FileNotFoundError`` if any
        required input is missing, preventing a step from running against
        an incomplete or stale set of upstream outputs.

        Also validates that the expected number of fan-out files are present
        when a ``count`` expectation is declared (e.g. all N KPI files exist
        before AGGREGATE_RESULTS runs).  When the upstream step exposes a
        ``StepOutputs.kpi_pattern``, that pattern drives the canonical
        sample-count check (issue #1391): an explicit ``inputs.count``
        that disagrees with the upstream-derived count is rejected, and
        ``count`` may be left explicitly ``None`` to skip the consistency
        check (i.e. the contract is "set ``count`` to match upstream
        ``kpi_pattern`` or leave it ``None``").
        """
        if step_name not in _STEP_DEPENDENCIES:
            return

        step_info = _STEP_DEPENDENCIES[step_name]
        inputs = step_info.inputs

        # Check exact-file requirements.
        for rel_path in inputs.required:
            abs_path = self.cfg.work_dir / rel_path
            if not abs_path.is_file():
                raise FileNotFoundError(
                    f"Step {step_name!r} requires input {rel_path!r} "
                    f"(resolved to {abs_path}) which was not found. "
                    f"Ensure all upstream steps completed successfully."
                )

        # Check glob-pattern requirements — ALL patterns must match at least one
        # file, and the aggregated match count must satisfy ``inputs.count``
        # when declared (issue #1391).
        all_matches: list[Path] = []
        for pattern in inputs.required_patterns:
            matches = sorted(self.cfg.work_dir.glob(pattern))
            if not matches:
                raise FileNotFoundError(
                    f"Step {step_name!r} requires at least one file matching "
                    f"pattern {pattern!r} in {self.cfg.work_dir}, but none were found. "
                    f"Ensure all upstream steps completed successfully."
                )
            all_matches.extend(matches)

        # Enforce ``inputs.count`` against the actual on-disk match count when
        # set.  An explicit count is the canonical contract for fan-out
        # steps — it must match the number of files the upstream step
        # actually produced.  ``count=None`` opts out of the check.
        if inputs.count is not None and len(all_matches) != inputs.count:
            raise FileNotFoundError(
                f"Step {step_name!r} declared count={inputs.count} but found "
                f"{len(all_matches)} files matching its required patterns in "
                f"{self.cfg.work_dir}. Ensure all upstream samples completed "
                f"successfully."
            )

        # Validate ``inputs.count`` against the count derived from the
        # upstream ``StepOutputs.kpi_pattern`` (issue #1391).  When a fan-out
        # step that produces this input declared a ``kpi_pattern``, that
        # pattern is the authoritative source of the per-sample file count.
        # An explicit ``inputs.count`` that disagrees with the upstream-
        # derived count is a configuration error (or, equivalently, a sign
        # that the upstream step is missing expected KPI files).  ``count``
        # may be left explicitly ``None`` to skip this consistency check.
        upstream_expected = self._upstream_kpi_match_count(step_name)
        if (
            upstream_expected is not None
            and inputs.count is not None
            and inputs.count != upstream_expected
        ):
            raise FileNotFoundError(
                f"Step {step_name!r} declared count={inputs.count} but the "
                f"upstream kpi_pattern produced {upstream_expected} files "
                f"in {self.cfg.work_dir}. The count must match the "
                f"upstream fan-out's ``kpi_pattern`` match count (or be "
                f"left explicitly ``None``)."
            )

    def _upstream_kpi_match_count(self, step_name: str) -> int | None:
        """Return the count of files matching any upstream step's ``kpi_pattern``.

        Used by ``_verify_step_inputs`` (issue #1391) to derive the canonical
        expected sample count from the upstream fan-out step.  Returns the
        count of files matching the upstream ``kpi_pattern`` that exactly
        appears in this step's ``required_patterns``, or ``None`` when no
        upstream step exposes a ``kpi_pattern`` that the current step
        consumes.
        """
        step_info = _STEP_DEPENDENCIES.get(step_name)
        if step_info is None:
            return None
        required = set(step_info.inputs.required_patterns)
        for other_name, other_info in _STEP_DEPENDENCIES.items():
            if other_name == step_name:
                continue
            kpi_pattern = other_info.outputs.kpi_pattern
            if kpi_pattern is None or kpi_pattern not in required:
                continue
            return len(sorted(self.cfg.work_dir.glob(kpi_pattern)))
        return None

    def _compute_code_hashes(self, cfg: CampaignConfig | None = None) -> dict[str, str]:
        """SHA-256 of every work script, plus the work.py module.

        The work scripts live in ``osimflow._work_scripts`` (shipped with
        the wheel). A development checkout (``pip install -e .``) also has
        copies in ``bin/`` (backward-compatible shims). We hash the UNION
        of both directories whenever either exists — sorted, deduped —
        so dev checkouts and wheel installs agree on the cache key.
        Fixes issue #1021.

        The work.py module is included because it is the work layer that
        the Campaign itself depends on; if a contributor edits it, we
        must re-run downstream steps.

        When ``cfg`` is provided, also include the resolved file-content
        hashes of ``cfg.custom_apply_script`` and ``cfg.custom_kpi_extractor``
        under the ``byos_apply`` and ``byos_kpi`` keys (issue #1011).
        These are mixed into the per-sample cache key for ``APPLY_PARAMETERS``
        and ``EXTRACT_KPIS`` respectively, so editing a BYOS user script
        invalidates the cached results. When ``cfg`` is omitted (the
        legacy ``Campaign._compute_code_hashes(_Stub())`` test path),
        the byos entries fall back to the ``"byos-unset"`` sentinel and
        ``self.code_hashes["bin"]`` continues to be the cache-key hash.
        """
        from . import work  # noqa: PLC0415

        # Resolve both work-script directories and take the union
        # (sorted, deduped) whenever either exists.
        package_root = Path(__file__).resolve().parent
        repo_root = package_root.parent
        candidates: list[Path] = []
        for d in (package_root / "_work_scripts", repo_root / "bin"):
            if d.is_dir():
                candidates.extend(d.glob("*.py"))
        work_file = Path(inspect.getfile(work))
        # Also fold in the work-layer modules so editing them invalidates
        # per-sample cache entries (issue #1022). Without this addition,
        # the per-sample steps used ``bin = _work_scripts/*.py + bin/*.py``
        # only, and editing ``osimflow.work`` or ``osimflow.apply_params``
        # silently kept cached results warm — wrong. The ``work`` hash
        # below still covers ``work.py`` separately for ``AGGREGATE_RESULTS``
        # because aggregate re-runs don't depend on the per-sample work
        # scripts (the docstring after #1036 spells out the two-hash scheme).
        try:
            apply_params_file = Path(inspect.getfile(sys.modules["osimflow"].apply_params))
        except (AttributeError, KeyError):
            apply_params_file = None
        for f in (work_file, apply_params_file):
            if f is not None and f.is_file():
                candidates.append(f)
        files = sorted(set(candidates), key=str)
        # Pick the effective cfg. When ``__init__`` calls this with its
        # cfg we get the BYOS entries; when tests call it with no cfg
        # (e.g. ``Campaign._compute_code_hashes(_Stub())``) we fall back
        # to ``self.cfg`` if a stub happens to carry one, then to None.
        effective_cfg = cfg if cfg is not None else getattr(self, "cfg", None)
        byos_apply_path = effective_cfg.custom_apply_script if effective_cfg is not None else None
        byos_kpi_path = effective_cfg.custom_kpi_extractor if effective_cfg is not None else None
        return {
            "bin": sha256_of_files(files),
            "work": sha256_of_files([work_file]),
            "byos_apply": _byos_file_hash(byos_apply_path),
            "byos_kpi": _byos_file_hash(byos_kpi_path),
        }

    def _code_hash_with_byos(self, byos_key: str) -> str:
        """Cache-key ``code_sha256`` optionally mixed with a BYOS hash.

        When ``code_hashes[byos_key]`` is the ``"byos-unset"`` sentinel
        (no user script configured), returns ``code_hashes["bin"]``
        unchanged so existing cached entries continue to hit after
        upgrading — no impact when BYOS is not configured (issue #1011
        acceptance criterion).

        When the user script is configured, returns the SHA-256 of the
        concatenation ``bin|byos`` so any edit to the user script
        produces a distinct cache key and forces the affected per-sample
        step to re-run.
        """
        base = self.code_hashes["bin"]
        byos_hash = self.code_hashes.get(byos_key, "byos-unset")
        if byos_hash == "byos-unset":
            return base
        return _combine_code_hash(base, byos_hash)

    def _inject_dp_overrides(self, samples: list[SampleSpec]) -> list[SampleSpec]:
        """Inject per-sample overrides from DataPointManager into SampleSpec list.

        For each sample, if the DataPointManager has a DataPoint with
        seed_model or weather_file set, those values are injected into
        the SampleSpec so subsequent steps use the per-sample overrides
        instead of the campaign-level defaults (GAP-009).
        """
        if self._dp_manager is None:
            return samples
        result: list[SampleSpec] = []
        for s in samples:
            sid = str(s["sample_id"])
            dp = self._dp_manager.get(sid)
            if dp is not None:
                overridden: SampleSpec = dict(s)  # type: ignore[assignment]
                if dp.seed_model:
                    overridden["seed_model"] = dp.seed_model
                if dp.weather_file:
                    overridden["weather_file"] = dp.weather_file
                result.append(overridden)
            else:
                result.append(s)
        return result

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
            self._obs.record_sample_cost(sid, cost_usd, trace_id=trace_id)
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
                DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR - DEFAULT_SPOT_PRICE_PER_VCPU_HOUR
            ) / DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR
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
        try:
            self.trace.update_sample(trace)
        except Exception as exc:
            self._consecutive_checkpoint_failures += 1
            log.warning(
                "checkpoint failed for sample %s (consecutive failures: %d): %s",
                sid,
                self._consecutive_checkpoint_failures,
                exc,
                exc_info=True,
            )
            if self._consecutive_checkpoint_failures >= 3:
                log.error(
                    "too many consecutive checkpoint failures (%d) — aborting campaign",
                    self._consecutive_checkpoint_failures,
                )
                raise
            return
        self._consecutive_checkpoint_failures = 0

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
        ``self._effective_max_workers()``, so up to that many results
        are collected in parallel (bounded by
        ``resource_quota.max_concurrent_samples`` when set — issue #1009).
        Each per-sample error is caught, logged with ``exc_info=True``,
        and recorded — it is never swallowed.

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
            trace_id = self._trace_id_for(sid)
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
                # Record sample status to observability backend immediately
                # so crashed samples are not missed (issue #847).
                self._obs.record_sample_status(sid, "failed", trace_id=trace_id)
                # Send sample failure alert (issue #1180).
                self._maybe_alert(
                    "sample.failed",
                    {
                        "campaign_id": self.trace.campaign_id,
                        "sample_id": sid,
                        "step": step_name,
                        "status": "failed",
                        "error": str(e)[:500],
                    },
                )
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
                # Soft pause (issue #553): running samples complete, new ones are skipped.
                if self._check_pause_requested():
                    self._write_paused_trace()
                    raise KeyboardInterrupt("pause requested during fan-out")
                _await_one(item)
            if self._cancel_requested:
                raise KeyboardInterrupt("cancellation requested during fan-out")
            return

        # max_workers > 1: use a ThreadPoolExecutor to await results
        # concurrently.  Each _await_one call blocks on handle.result(),
        # so the pool parallelism effectively controls how many samples
        # we wait for at the same time.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._effective_max_workers(),
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
                # Soft pause (issue #553): running samples complete, new ones are skipped.
                # Do NOT cancel futures — let in-flight work finish naturally.
                if self._check_pause_requested():
                    self._write_paused_trace()
                    log.warning("pause requested during %s — breaking fan-out", step_name)
                    break
                # CancelledError is a BaseException (not Exception), so we must
                # suppress it explicitly here — it is raised when a future was
                # cancelled via f.cancel() during a cancellation sweep.
                with contextlib.suppress(Exception, concurrent.futures.CancelledError):
                    future.result()

    def _record_costs(self, step_name: str, cost_usd: float, spot_savings_usd: float) -> None:
        """Record per-step aggregated costs from the completed fan-out.

        This is called after each ``_submit_and_await_all`` for the three
        fan-out steps (APPLY_PARAMETERS, RUN_OPENSTUDIO_SIM, EXTRACT_KPIS).

        When ``self._cost_tracker`` is None (cost tracking disabled), this
        is a no-op.
        """
        if self._cost_tracker is None:
            return
        self._cost_tracker.record_step_costs(step_name, cost_usd, spot_savings_usd)

    def _finalize_costs(self) -> None:
        """Build and persist the campaign cost summary.

        Called once at the end of ``run()`` when cost tracking is enabled.
        """
        if self._cost_tracker is None:
            return
        summary_dict = self._cost_tracker.finalize()
        if summary_dict is not None:
            self.trace.cost_summary = summary_dict
            log.info("campaign cost summary written to run.json")

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
                log.warning(
                    "could not read KPI file %s for baseline comparison: %s",
                    kpi_path,
                    exc,
                    exc_info=True,
                )
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
        weather_file_override: str | None = None,
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
        # GAP-009: per-sample weather_file override takes precedence over
        # campaign-level epw_file target resolution.
        if weather_file_override:
            resolved = dict(params)
            resolved[EPW_FILE_KEY] = str(weather_file_override)
            log.debug(
                "resolved epw_file (GAP-009 override): %s",
                weather_file_override,
            )
            return resolved

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
            raise FileNotFoundError(f"Init script not found: {script!r}")
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
            log.warning("finalize script error: %s (best-effort — continuing)", exc, exc_info=True)

    def _hook_env(self) -> dict[str, str]:
        """Build the environment dict for hook scripts."""
        base = dict(os.environ)
        base["OSIMFLOW_OUTDIR"] = str(self.cfg.outdir)
        base["OSIMFLOW_N_SAMPLES"] = str(self.cfg.n_samples)
        base["OSIMFLOW_EXECUTOR"] = self.executor.name
        base["OSIMFLOW_ALGORITHM"] = self.cfg.algorithm
        if self.cfg.shard_count is not None and self.cfg.shard_index is not None:
            base["OSIMFLOW_SHARD_COUNT"] = str(self.cfg.shard_count)
            base["OSIMFLOW_SHARD_INDEX"] = str(self.cfg.shard_index)
        if self.cfg.shard_start is not None and self.cfg.shard_end is not None:
            base["OSIMFLOW_SHARD_START"] = str(self.cfg.shard_start)
            base["OSIMFLOW_SHARD_END"] = str(self.cfg.shard_end)
        return base

    def _shard_label(self) -> str | None:
        if self.cfg.shard_count is not None and self.cfg.shard_index is not None:
            return f"part-{self.cfg.shard_index}-of-{self.cfg.shard_count}"
        if self.cfg.shard_start is not None and self.cfg.shard_end is not None:
            return f"range-{self.cfg.shard_start}-{self.cfg.shard_end}"
        return None

    def _samples_manifest_path(self) -> Path:
        label = self._shard_label()
        if label is None:
            return self.cfg.samples_file
        return self.cfg.work_dir / f"samples.{label}.json"

    def _apply_sharding(
        self,
        samples: list[SampleSpec],
        *,
        generation: int,
    ) -> list[SampleSpec]:
        """Return only samples assigned to this shard (if sharding configured)."""
        if self.cfg.shard_count is not None and self.cfg.shard_index is not None:
            shard_count = self.cfg.shard_count
            shard_index = self.cfg.shard_index
            selected = [s for idx, s in enumerate(samples) if idx % shard_count == shard_index]
            log.info(
                "sharding(partition): generation=%d selected %d/%d samples (index=%d count=%d)",
                generation,
                len(selected),
                len(samples),
                shard_index,
                shard_count,
            )
            return selected
        if self.cfg.shard_start is not None and self.cfg.shard_end is not None:
            start = self.cfg.shard_start
            end = self.cfg.shard_end
            selected = samples[start:end]
            log.info(
                "sharding(range): generation=%d selected %d/%d samples (start=%d end=%d)",
                generation,
                len(selected),
                len(samples),
                start,
                end,
            )
            return selected
        return samples

    def _fanout_submit_chunk_size(self, total: int) -> int:
        """Compute bounded chunk size for fan-out submission.

        Delegates to the executor's get_bounded_fanout_chunk_size method
        so the Campaign class remains executor-agnostic.
        """
        return self.executor.get_bounded_fanout_chunk_size(total)

    def _fanout_submit_interval_s(self) -> float:
        """Compute per-submit pacing interval for fan-out submission.

        Delegates to the executor's fanout_submit_interval_s method
        so the Campaign class remains executor-agnostic.
        """
        return self.executor.fanout_submit_interval_s()

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
            log.warning("could not parse variables.yml for campaign_meta: %s", exc, exc_info=True)

        meta: dict[str, object] = {
            "campaign_id": self.trace.campaign_id,
            "algorithm": self.cfg.algorithm,
            "n_samples": self.cfg.n_samples,
            "shard": {
                "count": self.cfg.shard_count,
                "index": self.cfg.shard_index,
                "start": self.cfg.shard_start,
                "end": self.cfg.shard_end,
                "label": self._shard_label(),
            },
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
        samples_file = self._latest_samples_file
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
                log.warning("could not read samples.json for provenance: %s", exc, exc_info=True)

        provenance: dict[str, object] = {
            "campaign_id": self.trace.campaign_id,
            "sampling": sampling_details,
            "shard": {
                "count": self.cfg.shard_count,
                "index": self.cfg.shard_index,
                "start": self.cfg.shard_start,
                "end": self.cfg.shard_end,
                "label": self._shard_label(),
                "samples_file": str(samples_file),
            },
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
        safe_json_dumps(provenance, out_path, default=str, indent=2)
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
        safe_json_dumps(manifest, out_path, default=str, indent=2)
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
            log.warning("failed to register campaign: %s", exc, exc_info=True)

    def _update_registry_status(self, status: str) -> None:
        """Update the campaign status in the registry on completion."""
        if self._registry is None:
            return
        try:
            self._registry.update_status(self.trace.campaign_id, status)
        except Exception as exc:
            log.warning("failed to update campaign status in registry: %s", exc, exc_info=True)

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
        # Fast path: check the in-memory flag first (no file I/O).
        with self._cancel_lock:
            if self._cancel_requested:
                return True

        # Check the .stop file with cross-process file locking to close the
        # TOCTOU race window (issue #649). Using fcntl.flock() ensures that
        # between checking "does .stop file exist" and acting on that check,
        # no other process can interfere (on POSIX systems).
        stop_file = self.cfg.outdir / ".stop"
        try:
            # Open existing file (fail if it doesn't exist; we don't create it).
            # O_NOFOLLOW prevents symlink attacks.
            fd = os.open(str(stop_file), os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            # File does not exist or is not accessible — no cancel requested.
            return False

        try:
            if sys.platform == "win32":
                # On Windows, msvcrt.locking does not support exclusive locks.
                # Fall back to a simple existence check inside the open fd.
                # The advisory locking on Windows is less robust than POSIX
                # flock, so we rely on the atomic rename from the API server
                # for safety.
                file_exists = True
            else:
                try:
                    # Acquire exclusive lock (non-blocking). If we get it, we're
                    # the sole accessor and can safely check the file state.
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    file_exists = stop_file.is_file()
                except BlockingIOError:
                    # Another process holds a conflicting lock — we cannot
                    # safely read the file state. Treat as cancel requested
                    # (conservative: better to cancel when we shouldn't than
                    # to miss a cancel request).
                    file_exists = True
            try:
                if file_exists:
                    log.warning(".stop file detected — requesting cancellation")
                    with self._cancel_lock:
                        self._cancel_requested = True
                    return True
            finally:
                if sys.platform != "win32":
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return False

    def _check_pause_requested(self) -> bool:
        """Check if pause has been requested via the ``.pause`` file.

        The ``.pause`` file is written by the REST API's
        ``POST /api/v1/campaign/pause`` endpoint (issue #553) or by the
        CLI ``osimflow pause`` command.

        Unlike the cancelled flag, the pause flag is NOT latched — we
        check the file existence on every call so that deleting the
        ``.pause`` file immediately unblocks new submissions (issue #798).

        Returns:
            ``True`` if pause is requested, ``False`` otherwise.
        """
        pause_file = self.cfg.outdir / ".pause"
        if pause_file.is_file():
            log.warning(".pause file detected — pausing new submissions")
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
            log.warning("could not write cancellation trace: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Soft pause / resume (issue #553)
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Request campaign pause (soft-stop).

        Writes a ``.pause`` flag file to the campaign directory and
        records the ``paused_at`` timestamp in the run trace.  Running
        samples complete normally; the executor checks for the ``.pause``
        file between sample dispatches and skips queuing new ones.

        Thread-safe and idempotent.
        """
        pause_file = self.cfg.outdir / ".pause"
        safe_json_dumps({"requested_at": time.time()}, pause_file)
        self.trace.status = "paused"
        self.trace.paused_at = time.time()
        self.trace.write(self.cfg.outdir / "run.json")
        log.warning("campaign pause requested (paused_at=%.0f)", self.trace.paused_at)

    def resume(self) -> None:
        """Resume a paused campaign.

        Removes the ``.pause`` flag file and clears ``paused_at`` from
        the run trace.  The executor's fan-out loop checks for the
        ``.pause`` file between sample dispatches and will resume
        queuing pending samples.

        Thread-safe and idempotent.
        """
        pause_file = self.cfg.outdir / ".pause"
        if pause_file.is_file():
            pause_file.unlink()
        with self._pause_lock:
            self._pause_requested = False
        self.trace.status = "running"
        self.trace.paused_at = None
        self.trace.write(self.cfg.outdir / "run.json")
        log.warning("campaign resume requested")

    def _write_paused_trace(self) -> None:
        """Write run.json with paused status when a soft-pause is triggered.

        Sets ``trace.status = "paused"`` and records the ``paused_at``
        timestamp so a subsequent resume can continue from where the
        campaign left off.
        """
        try:
            self.trace.status = "paused"
            self.trace.paused_at = time.time()
            self.cfg.outdir.mkdir(parents=True, exist_ok=True)
            self.trace.write(self.cfg.outdir / "run.json")
            log.info("wrote paused trace to run.json (paused_at=%.0f)", self.trace.paused_at)
        except Exception as exc:
            log.warning("could not write paused trace: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def _collect_circuit_breaker_states(self) -> dict[str, str]:
        """Collect final state of all CircuitBreaker instances (issue #1307).

        Returns a dict mapping breaker name to state (closed/open/half_open).
        Only DistributedCache has a circuit breaker; SQLiteCache does not.
        """
        states: dict[str, str] = {}
        cache = getattr(self, "cache", None)
        if cache is not None:
            breaker = getattr(cache, "_breaker", None)
            if breaker is not None:
                with contextlib.suppress(Exception):
                    states[breaker.name] = breaker.state
        return states

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

        # Reset pause flag so a paused campaign can be re-run without
        # restarting (issue #798).
        with self._pause_lock:
            self._pause_requested = False

        # Wire chaos_schedule to RunTrace (issue #1309).
        chaos_cfg_obj = getattr(self.cfg, "chaos", None)
        if chaos_cfg_obj is not None:
            self.trace.chaos_schedule = str(getattr(chaos_cfg_obj, "schedule", "none"))

        run_name = maybe_start_mlflow_run(self.cfg.mlflow_tracking_uri, self.trace.campaign_id)
        if run_name is not None:
            log_mlflow_params(_make_mlflow_param_view(self.cfg, self.executor.name))

        t0 = time.time()
        campaign_status = "failure"

        # Auto-register campaign in registry (issue #266).
        self._register_campaign()

        # Write campaign metadata manifest at start (issue #277).
        self._write_campaign_meta()

        # Start periodic observability flush (issue #1186).
        self._obs.start_periodic_flush()

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
                # Scope the dry-run env var to this execution so it
                # cannot leak across campaigns/tests (issue #976).
                with _scoped_dry_run_env():
                    result = self._run_dry_run(t0)
            elif self.cfg.sample is not None:
                result = self._run_single_sample(t0)
            else:
                result = self._run_full_campaign(t0)
            # If cancellation was detected in _finalize_full_campaign,
            # self.trace.status will be "cancelled" — propagate that to
            # campaign_status so the caller sees the correct final state.
            campaign_status = "cancelled" if self.trace.status == "cancelled" else "success"
            self.trace.status = campaign_status
            return result
        except KeyboardInterrupt:
            campaign_status = "cancelled"
            log.warning("campaign cancelled by user or signal")
            self.trace.status = "cancelled"
            self._cancel_active_jobs()
            # Collect circuit breaker states for run.json (issue #1307)
            self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
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
        except Exception as exc:
            campaign_status = "failure"
            self.trace.status = "failure"
            self.trace.error_summary = f"{type(exc).__name__}: {exc}"
            # Collect circuit breaker states for run.json (issue #1307)
            self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
            self.trace.finalize()
            self.cfg.outdir.mkdir(parents=True, exist_ok=True)
            log.exception("campaign failed")
            # Send campaign failure alert (issue #1180).
            self._maybe_alert(
                "campaign.failed",
                {
                    "campaign_id": self.trace.campaign_id,
                    "status": "failure",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
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
            self._obs.stop_periodic_flush()
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

            # Close the SQLite cache. ``close()`` runs a PASSIVE WAL
            # checkpoint and the connection; it never raises and never
            # removes the auxiliary ``.sqlite-wal`` / ``.sqlite-shm``
            # files, so peer worker processes sharing the same cache
            # during campaign cancellation do not crash with
            # ``FileNotFoundError`` (issue #620). Safe to call multiple
            # times (close() is idempotent and thread-safe).
            self.cache.close()

            # Close the result storage uploader (issue #339).
            if self._result_storage is not None:
                self._result_storage.close()

    def _abort_run_path_cancel(
        self,
        t0: float,
        samples: list[SampleSpec],
        kpi_files: list[Path],
    ) -> dict[str, object]:
        """Write a partial cancelled trace and return a cancelled result dict.

        Called by the dry-run and single-sample run paths (issue #621)
        when an inter-step cancellation check fires.  Both paths bypass
        the generation loop in ``_run_full_campaign``, so they need their
        own mid-flight polling — without it a ``.stop`` file written
        between two steps is ignored until the path returns, by which
        point a ``status="ok"`` trace has already been written.

        The partial-trace write here mirrors the cancellation handling
        in ``_finalize_full_campaign`` (lines ~2355-2368) so the cleanup
        from #620 (PASSIVE WAL checkpoint, thread-safe cache close) runs
        cleanly in ``run()``'s ``finally`` block.  ``run()`` propagates
        ``trace.status == "cancelled"`` into ``campaign_status`` so the
        caller observes the correct final state.
        """
        log.warning("cancellation requested mid-run-path — writing partial trace")
        self._finalize_samples()
        self.trace.status = "cancelled"
        # Collect circuit breaker states for run.json (issue #1307)
        self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
        self.trace.finalize()
        self.cfg.outdir.mkdir(parents=True, exist_ok=True)
        self.trace.write(self.cfg.outdir / "run.json")
        return {
            "samples": samples,
            "kpis": kpi_files,
            "aggregated": {"csv": None, "failed": None},
            "plots": [],
            "elapsed_s": time.time() - t0,
            "run_json": self.cfg.outdir / "run.json",
        }

    def _run_dry_run(self, t0: float) -> dict[str, object]:
        """Dry-run mode: 1 sample, local executor, steps 1-4 only.

        Cancellation is polled between every step (issue #621).  The
        dry-run path bypasses the generation loop in
        ``_run_full_campaign`` (which already polls ``_check_cancel_requested()``
        at the top of each iteration), so these inter-step checks are the
        only place a ``.stop`` file or in-memory cancel flag is honoured
        mid-flight.  On cancel a partial trace is written with
        ``status="cancelled"`` and the method returns cleanly — the
        ``run()`` caller observes the cancelled state without unwinding
        the stack via ``KeyboardInterrupt``.
        """
        original_n = self.cfg.n_samples
        self.cfg = dataclasses.replace(self.cfg, n_samples=1)
        log.info("DRY RUN: overriding n_samples from %d to 1", original_n)

        # Build algorithm kwargs (issue #529: R-NSGA-II support)
        algo_kwargs: dict[str, Any] = {}
        if self.cfg.algorithm == "nsga2":
            if self.cfg.nsga2_reference_points is not None:
                algo_kwargs["ref_points"] = self.cfg.nsga2_reference_points
            if self.cfg.nsga2_reference_directions is not None:
                algo_kwargs["ref_dirs"] = self.cfg.nsga2_reference_directions

        samples: list[SampleSpec] = []
        kpi_files: list[Path] = []

        # Pre-step check: cancel requested before the dry-run starts.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, samples, kpi_files)

        algo = AlgorithmRegistry.get(self.cfg.algorithm, **algo_kwargs)
        samples = self.step_generate_samples(algo)
        # GAP-009: inject per-sample seed_model / weather_file overrides
        # from the DataPointManager before processing.
        samples = self._inject_dp_overrides(samples)
        samples = self._apply_sharding(samples, generation=0)
        samples_path = self._samples_manifest_path()
        samples_path.parent.mkdir(parents=True, exist_ok=True)
        safe_json_dumps({"samples": samples}, samples_path, indent=2, raise_on_error=True)
        self._latest_samples_file = samples_path

        # Inter-step check: cancel requested during sample generation /
        # manifest write, before APPLY_PARAMETERS.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, samples, kpi_files)

        parameterized: SampleDict = self.step_apply_parameters(samples)

        # Inter-step check: cancel requested during APPLY_PARAMETERS,
        # before the long-running RUN_OPENSTUDIO_SIM step.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, samples, kpi_files)

        simulated: SampleDict = self.step_run_openstudio_sim(parameterized).samples

        # Inter-step check: cancel requested during RUN_OPENSTUDIO_SIM,
        # before EXTRACT_KPIS.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, samples, kpi_files)

        kpi_files = self.step_extract_kpis(simulated)

        # Post-step check: cancel requested during EXTRACT_KPIS (after
        # that step's own entry check has passed).  Without this guard
        # the trace.write below would record status="ok" and the cancel
        # signal would be silently lost (issue #621).
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, samples, kpi_files)

        t1 = time.time()

        self._finalize_samples()
        # Collect circuit breaker states for run.json (issue #1307)
        self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
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
        """Single-sample mode: run only sample N through steps 2-4.

        Cancellation is polled between every step (issue #621).  Like the
        dry-run path, this path bypasses the generation loop in
        ``_run_full_campaign``, so the inter-step checks here are the
        only place a ``.stop`` file or in-memory cancel flag is honoured
        mid-flight.
        """
        sample_idx = self.cfg.sample
        assert sample_idx is not None
        samples_file = self._samples_manifest_path()
        if not samples_file.exists():
            raise FileNotFoundError(
                f"Samples.json not found at {samples_file!r}. "
                "Run a full campaign (or --dry-run) first to generate samples."
            )
        all_samples_raw = safe_json_loads(samples_file, default=None, log_warnings=False)
        if all_samples_raw is None:
            raise ValueError(
                f"Samples.json at {samples_file!r} is corrupted or unreadable. "
                "Run a full campaign (or --dry-run) first to generate valid samples."
            )
        all_samples = cast_samples(all_samples_raw["samples"])
        if sample_idx < 0 or sample_idx >= len(all_samples):
            raise IndexError(
                f"Sample index {sample_idx} out of range [0, {len(all_samples) - 1}]. "
                f"Total samples available: {len(all_samples)}"
            )
        target = all_samples[sample_idx]
        log.info("SINGLE SAMPLE: running sample %d (id=%s)", sample_idx, target["sample_id"])

        kpi_files: list[Path] = []

        # Pre-step check: cancel requested before the sample starts.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, [target], kpi_files)

        parameterized: SampleDict = self.step_apply_parameters([target])

        # Inter-step check: cancel requested during APPLY_PARAMETERS,
        # before the long-running RUN_OPENSTUDIO_SIM step.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, [target], kpi_files)

        simulated: SampleDict = self.step_run_openstudio_sim(parameterized).samples

        # Inter-step check: cancel requested during RUN_OPENSTUDIO_SIM,
        # before EXTRACT_KPIS.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, [target], kpi_files)

        kpi_files = self.step_extract_kpis(simulated)

        # Post-step check: cancel requested during EXTRACT_KPIS (after
        # that step's own entry check has passed).  Without this guard
        # the trace.write below would record status="ok" and the cancel
        # signal would be silently lost (issue #621).
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, [target], kpi_files)

        t1 = time.time()

        self._finalize_samples()
        # Collect circuit breaker states for run.json (issue #1307)
        self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
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

        # Build algorithm kwargs (issue #529: R-NSGA-II support)
        algo_kwargs: dict[str, Any] = {}
        if self.cfg.algorithm == "nsga2":
            if self.cfg.nsga2_reference_points is not None:
                algo_kwargs["ref_points"] = self.cfg.nsga2_reference_points
            if self.cfg.nsga2_reference_directions is not None:
                algo_kwargs["ref_dirs"] = self.cfg.nsga2_reference_directions

        algo = AlgorithmRegistry.get(self.cfg.algorithm, **algo_kwargs)

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

    def _run_one_generation(  # noqa: PLR0912
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
                # verify there is actually something to reuse before continuing
                pending = getattr(algo, "_pending_proposed_samples", None)
                if not pending:
                    raise RuntimeError(
                        f"observe() returned empty samples at generation {generation} "
                        f"for algorithm {algo.name()!r} and no previous samples are "
                        "available; cannot continue iterative optimisation"
                    )
                log.warning(
                    "observe() returned empty samples at generation %d; reusing %d previous samples",
                    generation,
                    len(pending),
                )

        samples = self.step_generate_samples(algo, generation=generation)
        samples = self._inject_dp_overrides(samples)
        samples = self._apply_sharding(samples, generation=generation)
        samples_link = self._samples_manifest_path()
        samples_link.parent.mkdir(parents=True, exist_ok=True)
        safe_json_dumps({"samples": samples}, samples_link, indent=2, raise_on_error=True)
        self._latest_samples_file = samples_link

        # Per-generation state namespace (issue #1392).  Each step's
        # ``inputs_signature`` callable reads from this and each step's
        # ``outputs_signature`` callable writes back into it.  ``samples``
        # is seeded here from the pre-loop ``step_generate_samples`` call;
        # ``parameterized``/``simulated``/``kpi_files``/``aggregated`` are
        # populated by their respective ``outputs_signature`` as the
        # dispatcher iterates.
        gen_state = SimpleNamespace(
            samples=samples,
            parameterized=None,
            simulated={},
            kpi_files=[],
            aggregated={},
        )

        for step_name, step_info in _STEP_DEPENDENCIES.items():
            if step_info.condition is not None and not step_info.condition(
                self, algo, generation=generation
            ):
                log.debug("step %s skipped (condition returned False)", step_name)
                continue

            self._verify_step_inputs(step_name)
            step_method = getattr(self, step_info.method, None)
            if step_method is None:
                log.warning(
                    "step method %r for %r not found; skipping", step_info.method, step_name
                )
                continue

            self._maybe_inject_chaos(step_name, "before_step")

            # Dispatcher consults ``inputs_signature``/``outputs_signature``
            # instead of a hardcoded if/elif chain (issue #1392).  Each step
            # declares its own arg tuple via ``inputs_signature``; each
            # step captures its return value into ``gen_state`` via
            # ``outputs_signature``.  New steps just register their own
            # callables in ``_STEP_DEPENDENCIES`` — no dispatcher edit.
            #
            # A ``None`` ``inputs_signature`` means the step is configured
            # in the table for monitoring / configuration purposes but is
            # *not* dispatched by this loop (the legacy ``COMPUTE_*``
            # steps are invoked explicitly in the post-loop code below;
            # this preserves their pre-#1392 behaviour where the if/elif
            # chain did not invoke them but still ran the
            # before/after chaos hooks).
            if step_info.inputs_signature is not None:
                args: tuple[Any, ...] = step_info.inputs_signature(
                    gen_state, self, algo, generation
                )
                result = step_method(*args)
                if step_info.outputs_signature is not None:
                    slot = step_info.outputs_signature(result)
                    if slot is not None:
                        slot_name, slot_value = slot
                        setattr(gen_state, slot_name, slot_value)
            else:
                log.debug(
                    "step %s has no inputs_signature; not invoked by dispatcher",
                    step_name,
                )

            self._maybe_inject_chaos(step_name, "after_step")
            log.debug("step %s completed", step_name)

        # Mirror the per-generation state back to local variables for the
        # post-loop code below (Sobol / UQ / Pareto / monitoring).
        samples = gen_state.samples
        simulated = gen_state.simulated
        kpi_files = gen_state.kpi_files

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

        # UQ analysis (issue #530): compute POF, CIs, and distribution summaries.
        if self.cfg.algorithm == "uq":
            uq_variables: dict[str, Any] = {}
            if self.cfg.input_variables.exists():
                with self.cfg.input_variables.open() as fh:
                    raw = yaml.safe_load(fh)
                    if isinstance(raw, dict):
                        uq_variables = raw
            self.step_compute_uq_indices(samples, kpi_files, uq_variables, generation=generation)

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
                    log.warning("could not load previous Pareto front: %s", exc, exc_info=True)

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
                    "could not build ParetoSolution for sample %s: %s",
                    sample.get("sample_id"),
                    exc,
                    exc_info=True,
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
            # Collect circuit breaker states for run.json (issue #1307)
            self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
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
        # Collect circuit breaker states for run.json (issue #1307)
        self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
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

    def _maybe_inject_chaos(
        self,
        step_name: str,
        when: str,
        target_id: str | None = None,
    ) -> None:
        """Opt-in chaos fault injection (issue #1013).

        Called from ``_run_one_generation`` before/after each DAG step
        and from the per-sample fan-out loops. No-op unless the
        campaign has an active ``ChaosEngine`` and the configured
        ``cfg.chaos.schedule`` matches *when*. The schedule string
        is intentionally single-valued so a campaign either fires
        on step boundaries, on per-sample boundaries, or never —
        combining schedules is not supported in this iteration.

        Failures inside the engine never propagate: every injector
        is wrapped in its own try/except in
        :meth:`ChaosEngine.inject`, and we wrap the call here for
        defence in depth so a buggy user-supplied ``chaos_engine``
        cannot break the campaign.

        Parameters
        ----------
        step_name
            The DAG step the fault is being attached to. Used both
            for logging and for the ``step`` field of the recorded
            invocation.
        when
            One of ``"before_step"``, ``"after_step"``, or
            ``"per_sample"``. Other values are silently ignored.
        target_id
            Identifier of the injection target — typically the
            sample ID for ``per_sample`` injections and the step
            name for step-boundary injections. Defaults to
            ``step_name`` so callers don't have to invent an ID.
        """
        engine = self._chaos_engine
        if engine is None or not engine.enabled:
            return
        chaos_cfg = getattr(self.cfg, "chaos", None)
        schedule = getattr(chaos_cfg, "schedule", "none")
        if schedule == "none" or schedule != when:
            return
        tid = target_id if target_id is not None else step_name
        try:
            results = engine.inject(tid)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "chaos inject failed for %s/%s: %s (continuing)",
                step_name,
                tid,
                exc,
                exc_info=True,
            )
            return
        if results:
            self.trace.record_chaos_invocation(
                step=step_name,
                when=when,
                target_id=tid,
                results=results,
            )
            log.info(
                "chaos: %s @ %s target=%s injected=%d",
                step_name,
                when,
                tid,
                sum(1 for r in results if r.injected),
            )

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
            container_digest=self._python_container_digest,
            generation=generation,
            n_samples=self.cfg.n_samples,
        )
        cached = self.cache.lookup(key)
        if cached:
            samples_data = safe_json_loads(cached, default=None)
            if samples_data is not None:
                samples_obj: object = samples_data["samples"]
                samples_from_cache = cast_samples(samples_obj)
                self.trace.step_finished(
                    step_label,
                    cache="HIT",
                    elapsed_s=time.time() - t0,
                    exit_code=0,
                )
                self._obs.record_step_duration(step_label, time.time() - t0, generation=generation)
                return samples_from_cache
            log.warning("Cache entry %s is corrupted; treating as cache-miss", key)

        out_dir = self.cfg.work_dir / algo.name()
        result_path = algo.generate_samples(
            variables=variables,
            n_samples=self.cfg.n_samples,
            seed=None,
            outdir=out_dir,
        )
        try:
            self.cache.store(key, Path(result_path), exit_code=0)
            run_samples_data = safe_json_loads(Path(result_path), default=None, log_warnings=False)
            if run_samples_data is None:
                raise ValueError(f"Generated samples file {result_path!r} is corrupted")
            run_samples_obj: object = run_samples_data["samples"]
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
            code_sha256=self.code_hashes["bin"],
            container_digest=self._os_container_digest,
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

    def step_validate_measure_variables(self) -> None:
        """Validate variables.yml against discovered measure arguments.

        Runs before ``RUN_OPENSTUDIO_SIM`` as a pre-flight check (GAP-003).
        Raises ``UnmappedVariableError`` if any variable name in
        ``variables.yml`` does not correspond to a discovered measure
        argument.

        Note: Variable validation always runs regardless of ``skip_preflight``.
        Only the actual preflight OpenStudio simulation run is skipped when
        ``skip_preflight=True``.
        """
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
        except Exception as exc:
            log.error(f"Variable validation failed: {exc}")
            raise CampaignError(f"Variable validation failed: {exc}") from exc

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

    def step_apply_parameters(  # noqa: PLR0912, PLR0915
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
        that checks every parameter name across all samples
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

        # Soft pause (issue #553): skip this step entirely if pause was requested.
        if self._check_pause_requested():
            self._write_paused_trace()
            raise KeyboardInterrupt("pause requested before APPLY_PARAMETERS")

        # Load variable definitions from variables.yml for epw_file
        # target resolution and pre-flight validation (issue #55).
        variable_defs: list[dict[str, Any]] = self._load_variable_defs()

        # Pre-flight EPW validation: verify all mapped .epw files exist
        # in the template_sim_package directory.
        self._preflight_validate_epw_files(variable_defs)

        # Pre-flight validation: validate ALL parameter names
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
            # GAP-009: per-sample seed_model override. When set, this
            # replaces the campaign-level template_sim_package for this
            # sample's apply_parameters step.
            seed_model_override: str | None = s.get("seed_model")
            # Resolve epw_file targets: inject __epw_file__ with the
            # mapped .epw path (issue #55). The apply function extracts
            # this reserved key and uses it to mutate the .osw
            # weather_file field.
            # GAP-009: if a per-sample weather_file override is set, use it
            # for epw resolution instead of the campaign-level weather file.
            weather_file_override: str | None = s.get("weather_file")
            resolved_params = self._resolve_epw_targets(
                params, variable_defs, weather_file_override=weather_file_override
            )
            # Include seed_model_override in the cache key so a different
            # seed model for the same sample params is a distinct cache entry.
            inputs_hash = sha256_of_dict(
                {
                    "params": resolved_params,
                    "sid": sid,
                    "seed_model": seed_model_override,
                }
            )
            key = CacheKey(
                step="APPLY_PARAMETERS",
                sample_id=sid,
                openstudio_version=self.cfg.openstudio_version,
                inputs_sha256=inputs_hash,
                code_sha256=self._code_hash_with_byos("byos_apply"),
                container_digest=self._python_container_digest,
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
                "seed_model_override": seed_model_override,
            }

        # --- Phase 2/3: bounded submit and await in chunks ---
        pending_items = list(pending.items())
        if pending_items:
            chunk_size = self._fanout_submit_chunk_size(len(pending_items))
        else:
            chunk_size = 1  # unused when pending_items is empty, but avoids range(0,0,0)
        submit_interval_s = self._fanout_submit_interval_s()
        next_submit_at = 0.0
        for chunk_start in range(0, len(pending_items), chunk_size):
            if self._check_pause_requested():
                break
            submissions: dict[str, tuple[Handle | TQHandle, Callable[[Any], None]]] = {}
            chunk = pending_items[chunk_start : chunk_start + chunk_size]
            for sid, ctx in chunk:
                if self._check_pause_requested():
                    break
                if submit_interval_s > 0.0:
                    now = time.monotonic()
                    if now < next_submit_at:
                        time.sleep(next_submit_at - now)
                    next_submit_at = max(next_submit_at, now) + submit_interval_s
                handle: Handle | TQHandle
                # GAP-009: use per-sample seed_model override if set,
                # otherwise fall back to the campaign-level template_sim_package.
                template_pkg: Path = (
                    Path(ctx["seed_model_override"])
                    if ctx["seed_model_override"]
                    else self.cfg.template_sim_package
                )
                apply_out_dir: Path = ctx["out_dir"]
                shutil.copytree(template_pkg, apply_out_dir, dirs_exist_ok=True)
                # The BYOS contract (osimflow.byos_contract._BYOS_CONTRACT
                # — issue #1061) specifies ``apply_parameters(template,
                # parameters, sample_id, out)``. The orchestrator
                # previously forwarded only the per-sample output dir
                # and the resolved parameter dict, which silently
                # satisfied the legacy 2-arg default but diverged from
                # the documented 4-arg contract — and caused the
                # subprocess validation (PR #1058) to reject any
                # user-supplied BYOS apply script. Forward all four
                # positional args so the contract and the default
                # implement the same shape.
                # APPLY_PARAMETERS must propagate --max-sample-retries for
                # uniform retry semantics: RUN_OPENSTUDIO_SIM and EXTRACT_KPIS
                # both forward ``max_retries=self.cfg.max_sample_retries`` so
                # transient ``_apply_osm_mutations`` failures (file locks,
                # partial .osm writes) are retried instead of failing the
                # sample outright.  ``max_retries<=0`` disables retry.  See
                # issue #1394.
                if self.task_queue is not None:
                    handle = self.task_queue.submit(
                        self.apply_fn,
                        template_pkg,
                        ctx["resolved_params"],
                        sid,
                        apply_out_dir,
                        max_retries=self.cfg.max_sample_retries,
                    )
                else:
                    handle = self.executor.submit(
                        self.apply_fn,
                        template_pkg,
                        ctx["resolved_params"],
                        sid,
                        apply_out_dir,
                        name=f"apply_{sid}",
                        cpus=1,
                        memory_mb=512,
                        time_min=5,
                        container=self._python_container_image,
                        container_digest=self._python_container_digest,
                        result_hint=apply_out_dir,
                        max_retries=self.cfg.max_sample_retries,
                        **self._executor_submit_transport_kwargs,
                    )

                # Build the on-success callback (captures per-sample context).
                key = ctx["key"]
                state = ctx["state"]
                archive = self.cfg.archive_intermediates

                def _on_success(
                    result_path: Any,
                    _sid: str = sid,
                    _key: CacheKey = key,
                    _state: dict[str, object] = state,
                    _apply_out_dir: Path = apply_out_dir,
                    _archive: bool = archive,
                ) -> None:
                    self.cache.store(_key, _apply_out_dir, exit_code=0)
                    out[_sid] = _apply_out_dir
                    _state["apply_exit_code"] = 0
                    _state["apply_status"] = "ok"
                    self.trace.step_item_done("APPLY_PARAMETERS", status="ok")
                    # Record sample status to observability backend immediately
                    # so completed samples are not missed if campaign crashes
                    # before _finalize_samples (issue #847).
                    self._obs.record_sample_status(_sid, "ok", trace_id=self._trace_id_for(_sid))
                    if _archive:
                        archive_dst = self.cfg.outdir / "archive" / "apply" / _sid
                        self._archive_sample_artifacts(
                            _apply_out_dir, archive_dst, ["*.osw", "*.osm"]
                        )

                submissions[sid] = (handle, _on_success)
            self._submit_and_await_all(submissions, "APPLY_PARAMETERS")
        total_cost, total_savings = self._cost_tracker.sum_sample_costs(self._sample_state)
        self._record_costs("APPLY_PARAMETERS", total_cost, total_savings)

        # Record failures for samples that didn't succeed.
        for _sid, ctx in pending.items():
            state = ctx["state"]
            if state.get("apply_exit_code") != 0 and "apply_status" not in state:
                state["apply_exit_code"] = 1
                state["apply_status"] = "failed"
                state["error_summary"] = "APPLY: unknown error during concurrent execution"
                self.cache.store(ctx["key"], ctx["out_dir"], exit_code=1)
                self.trace.step_item_done("APPLY_PARAMETERS", status="failed")
                # Record sample status to observability backend (issue #847).
                self._obs.record_sample_status(_sid, "failed", trace_id=self._trace_id_for(_sid))
                # Send sample failure alert (issue #1180).
                self._maybe_alert(
                    "sample.failed",
                    {
                        "campaign_id": self.trace.campaign_id,
                        "sample_id": _sid,
                        "step": "APPLY_PARAMETERS",
                        "status": "failed",
                        "error": "apply exited with non-zero code",
                    },
                )

        self.trace.step_finished(
            "APPLY_PARAMETERS",
            cache=cache_label,
            elapsed_s=time.time() - t0,
            exit_code=0,
        )
        self._obs.record_step_duration("APPLY_PARAMETERS", time.time() - t0, generation=generation)
        return out

    def step_run_openstudio_sim(  # noqa: PLR0912, PLR0915
        self,
        parameterized: SampleDict,
        generation: int = 0,
    ) -> SimResult:
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

        # Soft pause (issue #553): skip this step entirely if pause was requested.
        if self._check_pause_requested():
            self._write_paused_trace()
            raise KeyboardInterrupt("pause requested before RUN_OPENSTUDIO_SIM")

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
                    # Per-sample modified_sim_package (issue #783). When
                    # GAP-009 seed_model_override assigns different seed
                    # models per sample, each sample must get its own
                    # cache entry.
                    "modified_sim_package": str(mod_pkg),
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
                container_digest=self._os_container_digest,
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

        # --- Phase 2/3: bounded submit and await in chunks ---
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
                        stdout_path=ctx["stdout_log"],
                        stderr_path=ctx["stderr_log"],
                        max_retries=self.cfg.max_sample_retries,
                        timeout_s=self.cfg.byos_timeout_s,
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
                        container_digest=self._os_container_digest,
                        openstudio_version=os_version,
                        stdout_path=ctx["stdout_log"],
                        stderr_path=ctx["stderr_log"],
                        max_retries=self.cfg.max_sample_retries,
                        worker_id="local",
                        result_hint=Path(ctx["out_dir"]) / sid,
                        **self._executor_submit_transport_kwargs,
                    )

        pending_items = list(pending.items())
        if pending_items:
            chunk_size = self._fanout_submit_chunk_size(len(pending_items))
        else:
            chunk_size = 1  # unused when pending_items is empty, but avoids range(0,0,0)
        submit_interval_s = self._fanout_submit_interval_s()
        next_submit_at = 0.0
        for chunk_start in range(0, len(pending_items), chunk_size):
            if self._check_pause_requested():
                break
            submissions: dict[str, tuple[Handle | TQHandle, Callable[[Any], None]]] = {}
            chunk = pending_items[chunk_start : chunk_start + chunk_size]
            for sid, ctx in chunk:
                if self._check_pause_requested():
                    break
                if submit_interval_s > 0.0:
                    now = time.monotonic()
                    if now < next_submit_at:
                        time.sleep(next_submit_at - now)
                    next_submit_at = max(next_submit_at, now) + submit_interval_s
                # Chaos wiring (issue #1013): per-sample injection. No-op
                # unless ``cfg.chaos.schedule == "per_sample"``, in which
                # case every sample submits under the configured fault
                # schedule (network_delay / cpu_spike / kill_switch).
                self._maybe_inject_chaos("RUN_OPENSTUDIO_SIM", "per_sample", target_id=sid)
                handle: Handle | TQHandle
                if self.task_queue is not None:
                    handle = self.task_queue.submit(
                        run_openstudio_sim,
                        ctx["mod_pkg"],
                        sid,
                        os_version,
                        ctx["out_dir"],
                        stdout_path=ctx["stdout_log"],
                        stderr_path=ctx["stderr_log"],
                        max_retries=self.cfg.max_sample_retries,
                        timeout_s=self.cfg.byos_timeout_s,
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
                        container_digest=self._os_container_digest,
                        openstudio_version=os_version,
                        stdout_path=ctx["stdout_log"],
                        stderr_path=ctx["stderr_log"],
                        max_retries=self.cfg.max_sample_retries,
                        timeout_s=self.cfg.byos_timeout_s,
                        worker_id="local",
                        result_hint=Path(ctx["out_dir"]) / sid,
                        **self._executor_submit_transport_kwargs,
                    )

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
                    err = result_path / "eplusout.err"
                    if err.exists():
                        err.unlink()
                    self.cache.store(_key, Path(result_path), exit_code=0)
                    out[_sid] = Path(result_path)
                    _state["sim_exit_code"] = 0
                    _state["sim_status"] = "ok"
                    _state["eplusout_sql"] = str(result_path / "eplusout.sql")
                    _state["worker_id"] = _handle.worker_id
                    _state["worker_ip"] = getattr(_handle, "worker_ip", None)
                    _state["worker_region"] = getattr(_handle, "worker_region", None)
                    _state["cost_usd"] = getattr(_handle, "cost_usd", None)
                    _state["billed_duration_seconds"] = getattr(
                        _handle, "billed_duration_seconds", None
                    )
                    self.trace.step_item_done("RUN_OPENSTUDIO_SIM", status="ok")
                    # Record sample status to observability backend immediately
                    # so completed samples are not missed if campaign crashes
                    # before _finalize_samples (issue #847).
                    self._obs.record_sample_status(_sid, "ok", trace_id=self._trace_id_for(_sid))
                    if _archive:
                        archive_dst = self.cfg.outdir / "archive" / "sim" / _sid
                        self._archive_sample_artifacts(
                            Path(result_path), archive_dst, ["*.osw", "*.osm", "eplusout.sql"]
                        )
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
                                    exc_info=True,
                                )

                submissions[sid] = (handle, _on_success)

            self._submit_and_await_all(
                submissions,
                "RUN_OPENSTUDIO_SIM",
                recovery_manager=recovery_manager,
                resubmit_callback=resubmit_callback,
            )
        total_cost, total_savings = self._cost_tracker.sum_sample_costs(self._sample_state)
        self._record_costs("RUN_OPENSTUDIO_SIM", total_cost, total_savings)

        # Record failures for samples that didn't succeed.
        for _sid, ctx in pending.items():
            state = ctx["state"]
            if state.get("sim_exit_code") != 0 and "sim_status" not in state:
                state["sim_exit_code"] = 1
                state["sim_status"] = "failed"
                state["error_summary"] = "SIM: unknown error during concurrent execution"
                self.cache.store(ctx["key"], ctx["out_dir"], exit_code=1)
                self.trace.step_item_done("RUN_OPENSTUDIO_SIM", status="failed")
                # Record sample status to observability backend (issue #847).
                self._obs.record_sample_status(_sid, "failed", trace_id=self._trace_id_for(_sid))
                # Send sample failure alert (issue #1180).
                self._maybe_alert(
                    "sample.failed",
                    {
                        "campaign_id": self.trace.campaign_id,
                        "sample_id": _sid,
                        "step": "RUN_OPENSTUDIO_SIM",
                        "status": "failed",
                        "error": "sim exited with non-zero code",
                    },
                )

        any_failed = any(ctx["state"].get("sim_exit_code", 0) != 0 for ctx in pending.values())
        self.trace.step_finished(
            "RUN_OPENSTUDIO_SIM",
            cache="MISS×N" if n else "SKIPPED",
            elapsed_s=time.time() - t0,
            exit_code=1 if any_failed else 0,
        )
        self._obs.record_step_duration(
            "RUN_OPENSTUDIO_SIM", time.time() - t0, generation=generation
        )
        return SimResult(samples=out, success=not any_failed)

    # ------------------------------------------------------------------
    # Worker direct-to-storage push (issue #625, Epic #624)
    # ------------------------------------------------------------------
    def _coordinator_url(self) -> str | None:
        """Resolve the Coordinator base URL for worker status reporting.

        Reads ``cfg.coordinator_url`` when present (forward-compatible with a
        future CampaignConfig field) and otherwise falls back to the
        ``OSIMFLOW_COORDINATOR_URL`` environment variable, which is the
        established pattern for forwarding per-job config to distributed
        workers (AGENTS.md §10).
        """
        cfg_url = getattr(self.cfg, "coordinator_url", None)
        if cfg_url:
            return str(cfg_url)
        return os.environ.get("OSIMFLOW_COORDINATOR_URL")

    def _coordinator_api_key(self) -> str | None:
        """Resolve an optional bearer token for Coordinator PATCH calls."""
        return os.environ.get("OSIMFLOW_API_KEY")

    def _publish_sample_results(
        self,
        *,
        sample_id: str,
        index: int,
        simulation_dir: Path,
        kpi_path: Path | None,
        exit_code: int,
        status: str,
    ) -> None:
        """Push one sample's results directly to storage (issue #625).

        Uploads ``kpis.json`` + an atomic ``_manifest.json`` to the configured
        :class:`ResultStorage` backend and best-effort reports completion to
        the Coordinator.  This is a no-op when no result storage is configured
        or when the backend is :class:`LocalStorage` (local path unchanged).

        Uses the **raw** synchronous backend (``ResultStorageUploader._storage``)
        rather than the async upload queue, because the manifest must become
        visible strictly after ``kpis.json`` — the async wrapper cannot
        guarantee that ordering.
        """
        if self._result_storage is None:
            return
        # Access the raw sync backend that the async uploader wraps.
        backend = getattr(self._result_storage, "_storage", None)
        if backend is None:
            return
        try:
            publish_kpi_results(
                storage=backend,
                campaign_id=self.trace.campaign_id,
                sample_id=sample_id,
                index=index,
                simulation_dir=simulation_dir,
                kpi_path=kpi_path,
                exit_code=exit_code,
                status=status,
                archive_intermediates=self.cfg.archive_intermediates,
                coordinator_url=self._coordinator_url(),
                api_key=self._coordinator_api_key(),
            )
        except OSError as exc:
            # Storage failures must not abort the extract step; the manifest
            # is telemetry/coordination, not the primary result.
            log.warning(
                "EXTRACT_KPIS: direct-to-storage publish failed for %s: %s",
                sample_id,
                exc,
                exc_info=True,
            )

    def step_extract_kpis(  # noqa: PLR0912, PLR0915
        self,
        simulated: SampleDict,
        generation: int = 0,
    ) -> list[Path]:
        """Fan-out: for each simulated sample, extract KPIs."""
        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before EXTRACT_KPIS")

        # Soft pause (issue #553): skip this step entirely if pause was requested.
        if self._check_pause_requested():
            self._write_paused_trace()
            raise KeyboardInterrupt("pause requested before EXTRACT_KPIS")

        out: list[Path] = []
        n = len(simulated)
        self.trace.step_started("EXTRACT_KPIS", total=n)

        # --- Phase 1: cache check for all samples ---
        pending: dict[str, dict[str, Any]] = {}
        os_version = self.cfg.openstudio_version
        for sid, sim_dir in simulated.items():
            inputs_hash = sha256_of_dict(
                {
                    "sim_dir": str(sim_dir),
                    "sid": sid,
                    "os_version": os_version,
                    # Issue #1082: include the KPI filter in the cache key so
                    # that changing --kpis invalidates stale KPI JSON.
                    "kpis": list(self.cfg.kpis) if self.cfg.kpis else [],
                }
            )
            key = CacheKey(
                step="EXTRACT_KPIS",
                sample_id=sid,
                openstudio_version=os_version,
                inputs_sha256=inputs_hash,
                code_sha256=self._code_hash_with_byos("byos_kpi"),
                container_digest=self._python_container_digest,
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
                "os_version": os_version,
            }

        # --- Phase 2/3: bounded submit and await in chunks ---
        pending_items = list(pending.items())
        # Zero-based sample index within the campaign (issue #625 manifest field).
        index_map: dict[str, int] = {sid: i for i, (sid, _c) in enumerate(pending_items)}
        if pending_items:
            chunk_size = self._fanout_submit_chunk_size(len(pending_items))
        else:
            chunk_size = 1  # unused when pending_items is empty, but avoids range(0,0,0)
        submit_interval_s = self._fanout_submit_interval_s()
        next_submit_at = 0.0
        for chunk_start in range(0, len(pending_items), chunk_size):
            if self._check_pause_requested():
                break
            submissions: dict[str, tuple[Handle | TQHandle, Callable[[Any], None]]] = {}
            chunk = pending_items[chunk_start : chunk_start + chunk_size]
            for sid, ctx in chunk:
                if self._check_pause_requested():
                    break
                if submit_interval_s > 0.0:
                    now = time.monotonic()
                    if now < next_submit_at:
                        time.sleep(next_submit_at - now)
                    next_submit_at = max(next_submit_at, now) + submit_interval_s
                # Chaos wiring (issue #1013): per-sample injection for the
                # KPI extraction fan-out. No-op unless the schedule is
                # ``per_sample``.
                self._maybe_inject_chaos("EXTRACT_KPIS", "per_sample", target_id=sid)
                handle: Handle | TQHandle
                if self.task_queue is not None:
                    handle = self.task_queue.submit(
                        self.extract_fn,
                        ctx["sim_dir"],
                        sid,
                        ctx["kpi_dir"],
                        openstudio_version=ctx["os_version"],
                        kpis=self.cfg.kpis,
                        max_retries=self.cfg.max_sample_retries,
                    )
                else:
                    handle = self.executor.submit(
                        self.extract_fn,
                        ctx["sim_dir"],
                        sid,
                        ctx["kpi_dir"],
                        openstudio_version=ctx["os_version"],
                        kpis=self.cfg.kpis,
                        name=f"kpi_{sid}",
                        cpus=1,
                        memory_mb=1024,
                        time_min=10,
                        container=self._python_container_image,
                        container_digest=self._python_container_digest,
                        result_hint=Path(ctx["kpi_dir"]) / f"kpi_{sid}.json",
                        max_retries=self.cfg.max_sample_retries,
                        **self._executor_submit_transport_kwargs,
                    )

                key = ctx["key"]
                state = ctx["state"]
                # Bind loop variables so each closure captures its own sample.
                _sim_dir = ctx["sim_dir"]
                _sample_index = index_map[sid]

                def _on_success(
                    result_path: Any,
                    _sid: str = sid,
                    _key: CacheKey = key,
                    _state: dict[str, object] = state,
                    _sim_dir: Path = _sim_dir,
                    _index: int = _sample_index,
                ) -> None:
                    self.cache.store(_key, Path(result_path), exit_code=0)
                    out.append(Path(result_path))
                    _state["extract_exit_code"] = 0
                    _state["extract_status"] = "ok"
                    self.trace.step_item_done("EXTRACT_KPIS", status="ok")
                    # Record sample status to observability backend immediately
                    # so completed samples are not missed if campaign crashes
                    # before _finalize_samples (issue #847).
                    self._obs.record_sample_status(_sid, "ok", trace_id=self._trace_id_for(_sid))
                    # Worker direct-to-storage push (issue #625): upload
                    # kpis.json + atomic _manifest.json, then report to the
                    # Coordinator. No-op for the LocalStorage backend.
                    self._publish_sample_results(
                        sample_id=_sid,
                        index=_index,
                        simulation_dir=_sim_dir,
                        kpi_path=Path(result_path),
                        exit_code=0,
                        status="completed",
                    )

                submissions[sid] = (handle, _on_success)
            self._submit_and_await_all(submissions, "EXTRACT_KPIS")
        total_cost, total_savings = self._cost_tracker.sum_sample_costs(self._sample_state)
        self._record_costs("EXTRACT_KPIS", total_cost, total_savings)

        # Record failures for samples that didn't succeed.
        for _sid, ctx in pending.items():
            state = ctx["state"]
            if state.get("extract_exit_code") != 0 and "extract_status" not in state:
                state["extract_exit_code"] = 1
                state["extract_status"] = "failed"
                state["error_summary"] = "EXTRACT: unknown error during concurrent execution"
                self.trace.step_item_done("EXTRACT_KPIS", status="failed")
            # Worker direct-to-storage (issue #625): publish a 'failed'
            # manifest for any sample that did not complete cleanly. Successful
            # samples were already published in _on_success above and are
            # skipped here (extract_status == "ok" / "cached").
            # Also record sample status to observability backend (issue #847).
            if state.get("extract_status") == "failed":
                self._publish_sample_results(
                    sample_id=_sid,
                    index=index_map.get(_sid, -1),
                    simulation_dir=Path(ctx["sim_dir"]),
                    kpi_path=None,
                    exit_code=int(state.get("extract_exit_code", 1) or 1),
                    status="failed",
                )
                self._obs.record_sample_status(_sid, "failed", trace_id=self._trace_id_for(_sid))
                # Send sample failure alert (issue #1180).
                self._maybe_alert(
                    "sample.failed",
                    {
                        "campaign_id": self.trace.campaign_id,
                        "sample_id": _sid,
                        "step": "EXTRACT_KPIS",
                        "status": "failed",
                        "error": "extract exited with non-zero code",
                    },
                )

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
                log.warning("could not read KPI file %s: %s", kpi_path, exc, exc_info=True)

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

    def step_compute_uq_indices(
        self,
        samples: list[SampleSpec],
        kpi_files: list[Path],
        variables: dict[str, Any],
        generation: int = 0,
    ) -> Path | None:
        """Compute UQ indices (POF, CIs, distribution summaries) after KPI extraction.

        This step runs only when ``cfg.algorithm == "uq"``. It reads the
        per-sample KPI values, passes them to
        :meth:`UncertaintyQuantification.compute_uq_indices`, and stores
        the resulting ``uq_results.json`` in the campaign output directory.

        The step is not cached — UQ computation is cheap relative to
        simulation and the user may want to re-run with different thresholds.

        Parameters
        ----------
        samples
            Sample specs from ``step_generate_samples``.
        kpi_files
            Per-sample KPI JSON files from ``step_extract_kpis``.
        variables
            Parsed ``variables.yml`` dict.
        generation
            Generation number (included for consistency; UQ is
            single-shot so this is always 0).

        Returns
        -------
        Path | None
            Path to ``uq_results.json``, or ``None`` if the algorithm
            is not ``"uq"`` or no KPI files are available.
        """
        if self.cfg.algorithm != "uq":
            return None

        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before COMPUTE_UQ_INDICES")

        if not kpi_files:
            log.warning("COMPUTE_UQ_INDICES: no KPI files — skipping")
            self.trace.step_finished(
                "COMPUTE_UQ_INDICES",
                cache="SKIPPED",
                elapsed_s=0.0,
                exit_code=0,
            )
            return None

        kpi_values: dict[str, dict[str, float]] = {}
        for kpi_path in kpi_files:
            try:
                data = json.loads(kpi_path.read_text())
                sid = str(data.get("sample_id", kpi_path.stem.replace("kpi_", "")))
                kpis = data.get("kpis", {})
                numeric_kpis = {k: float(v) for k, v in kpis.items() if isinstance(v, (int, float))}
                kpi_values[sid] = numeric_kpis
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                log.warning("could not read KPI file %s: %s", kpi_path, exc, exc_info=True)

        failure_thresholds: dict[str, tuple[float, str]] | None = None
        if self.cfg.uq_failure_thresholds:
            failure_thresholds = {}
            from osimflow.algorithms.uq import _parse_failure_threshold  # noqa: PLC0415

            for raw in self.cfg.uq_failure_thresholds:
                try:
                    kpi_name, threshold = _parse_failure_threshold(raw)
                    failure_thresholds[kpi_name] = (threshold, "greater")
                except ValueError as exc:
                    log.warning("invalid failure threshold %r: %s", raw, exc, exc_info=True)

        algo = AlgorithmRegistry.get("uq")
        uq_dir = self.cfg.outdir / "uq"
        uq_dir.mkdir(parents=True, exist_ok=True)

        try:
            uq_path = algo.compute_uq_indices(
                variables=variables,
                samples=samples,  # type: ignore[arg-type]
                kpi_values=kpi_values,
                outdir=uq_dir,
                failure_thresholds=failure_thresholds,
            )
        except Exception as exc:
            log.error(
                "COMPUTE_UQ_INDICES failed: %s",
                exc,
                exc_info=True,
            )
            self.trace.step_finished(
                "COMPUTE_UQ_INDICES",
                cache="MISS",
                elapsed_s=time.time() - t0,
                exit_code=1,
            )
            raise RuntimeError("compute_uq_indices failed") from exc

        elapsed = time.time() - t0
        self.trace.step_finished(
            "COMPUTE_UQ_INDICES",
            cache="MISS",
            elapsed_s=elapsed,
            exit_code=0,
        )
        self._obs.record_step_duration("COMPUTE_UQ_INDICES", elapsed, generation=generation)
        log.info("COMPUTE_UQ_INDICES: wrote %s", uq_path)
        return uq_path

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

        # Cross-step data dependency check (issue #850): verify all KPI files
        # are present before aggregating. This prevents AGGREGATE_RESULTS from
        # running against a stale or incomplete set of upstream outputs.
        self._verify_step_inputs("AGGREGATE_RESULTS")
        for kpi_file in kpi_files:
            if not kpi_file.is_file():
                raise FileNotFoundError(
                    f"AGGREGATE_RESULTS requires KPI file {kpi_file} which was not found. "
                    f"Ensure all EXTRACT_KPIS samples completed successfully."
                )

        sim_dirs = list(simulated.values())
        inputs_hash = sha256_of_dict(
            {
                "kpis": [str(p) for p in kpi_files],
                "sims": [str(p) for p in sim_dirs],
                "baseline_sample_id": baseline_sample_id or "None",
            }
        )
        key = CacheKey(
            step="AGGREGATE_RESULTS",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256=inputs_hash,
            code_sha256=self.code_hashes["work"],
            container_digest=self._python_container_digest,
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
            container=self._python_container_image,
            container_digest=self._python_container_digest,
            result_hint={
                "csv": self.cfg.outdir / "aggregated_results.csv",
                "parquet": self.cfg.outdir / "aggregated_results.parquet",
                "failed": self.cfg.outdir / "failed_simulations.csv",
            },
            **self._executor_submit_transport_kwargs,
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
            container=self._python_container_image,
            container_digest=self._python_container_digest,
            result_hint=[],
            **self._executor_submit_transport_kwargs,
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
