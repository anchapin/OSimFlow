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

__all__ = ["Campaign", "CampaignAbortError", "CampaignError", "QuotaExceededError", "SimResult"]

import contextlib
import dataclasses
import json
import logging
import os
import shutil
import sys
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ..algorithms import AlgorithmRegistry, BaseAlgorithm
from ..apply_params import (
    _build_mappings,
    preflight_check,
)
from ..cache import (
    CacheKey,
    _container_digest_for,
    digest_pinned_image_ref,
    sha256_of_dict,
    sha256_of_files,
)
from ..chaos import ChaosEngine
from ..config import CampaignConfig
from ..cosign import (
    DEFAULT_COSIGN_OIDC_ISSUER,
    CosignVerificationError,
    build_cosign_image_ref,
    triangulate_image_ref,
    verify_image_signature,
    write_cosign_receipt,
)
from ..data_point_manager import DataPointManager
from ..distributed_cache import build_cache, campaign_state_namespace
from ..document_store import build_document_store
from ..errors import OSimFlowRuntimeError
from ..executors import BaseExecutor, Handle
from ..executors.transport import ResultTransportConfig
from ..json_utils import safe_json_dumps, safe_json_loads
from ..manifest import _validate_coordinator_url
from ..measures import MeasureRegistry, UnmappedVariableError
from ..notifications.alerting import AlertManager, build_alert_manager
from ..observability.mlflow_hook import (
    log_mlflow_artifacts,
    log_mlflow_metrics,
    log_mlflow_params,
    maybe_end_mlflow_run,
    maybe_start_mlflow_run,
)
from ..observability.monitoring import (
    RunTrace,
    WorkerRecoveryManager,
    sample_log_paths,
)
from ..queues.control_plane import build_job_queue
from ..queues.dispatch import ConsumerQueue
from ..registry import CampaignRegistry
from ..s3_campaign import (
    CLAIM_ACQUIRED,
    CLAIM_ALREADY_SUBMITTED,
    CampaignDetached,
    S3CampaignStore,
    SubmitClaim,
    batch_job_name,
    new_handoff,
)
from ..storage import ResultStorageUploader, build_result_storage
from ..work import (
    SevereEnergyPlusError,
    aggregate_results,
    default_apply_parameters,
    extract_kpis,
    generate_plots,
    preflight_run_model,
    run_openstudio_sim,
)
from .analysis import CampaignAnalysisMixin
from .artifacts import CampaignArtifactWriter
from .baseline import (
    baseline_sample_id as _baseline_sid_from_cfg,
)
from .baseline import (
    compute_baseline_comparison,
    compute_improvement_range,
    read_all_kpis,
)
from .chaos import CampaignChaosWiring
from .code_hashes import (
    _algorithm_code_digest,  # noqa: F401  — re-exported test seam
    _byos_file_hash,  # noqa: F401  — re-exported test seam
    _combine_code_hash,  # noqa: F401  — re-exported test seam
    _transitive_import_closure,  # noqa: F401  — re-exported test seam
    code_hash_with_byos,
    compute_code_hashes,
)
from .cost_tracker import CampaignCostTracker
from .dag import (  # noqa: F401 — re-exported
    DAGStep,
    SampleDict,
    SimResult,
    StepInputs,
    StepOutputs,
    _always_run,
    _out_kpi_files,
    _out_parameterized,
    _out_simulated,
    _sig_apply,
    _sig_extract_kpis,
    _sig_lhs_samples,
    _sig_preflight,
    _sig_run_sim,
    _sig_validate_measure,
)
from .epw import CampaignEpwResolver
from .fanout import (
    FanoutDeps,
)
from .fanout import (
    compute_await_deadline as _compute_await_deadline_impl,
)
from .fanout import (
    dispatch_step_work as _dispatch_step_work_impl,
)
from .fanout import (
    mark_sample_failed as _mark_sample_failed_impl,
)
from .fanout import (
    submit_and_await_all as _submit_and_await_all_impl,
)
from .helpers import (  # noqa: F401 — re-exported
    _make_mlflow_param_view,
    _package_content_digest,
    _scoped_dry_run_env,
    cast_aggregate_result,
    cast_plot_paths,
    cast_samples,
    cast_variables,
)
from .hooks import (
    hook_env as _build_hook_env,
)
from .hooks import (
    maybe_fire_webhook,
    run_finalize_script,
    run_init_script,
)
from .kpis import CampaignKpisMixin
from .lifecycle import (
    CampaignLifecycle,
    CampaignPauseRequested,
    handle_signal,
)
from .lifecycle import (
    CancelRegistry as _CancelRegistry,  # noqa: F401  — re-exported test seam
)
from .lifecycle import (
    cancel_registry as _cancel_registry,
)
from .observability import ObservabilityManager
from .optimization import CampaignOptimizationMixin
from .quota import CampaignQuotaGuard, QuotaExceededError
from .runner import CampaignRunMixin
from .sample_trace import CampaignAbortError, CampaignSampleTraceRecorder
from .sharding import CampaignSharding
from .steps import CampaignStepsMixin
from .types import SampleSpec, VariableSpec  # noqa: F401  — re-exported API


def _osimflow_version() -> str:
    """Return the installed OSimFlow version, or 'unknown'."""
    try:
        from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

        return version("osimflow")
    except PackageNotFoundError:
        return "unknown"


log = logging.getLogger("osimflow.campaign")

# Image registries. The OpenStudio CLI image is consumed directly from
# NREL's upstream `nrel/openstudio` on Docker Hub — see
# `docs/openstudio-image-distribution.md` and ADR-0002 for the rationale.
# The scientific Python image remains a project-owned ghcr.io artifact.
CONTAINER_OS = "docker.io/nrel/openstudio:{version}"

#: Executors whose substrate pulls the OpenStudio container image.  For
#: these, an unresolved image digest fails loudly at ``run()`` start —
#: the executed image must be digest-pinned so a re-published tag cannot
#: change what runs while the cache key still describes the old digest
#: (issue #1536).
CONTAINER_SUBSTRATE_EXECUTORS: frozenset[str] = frozenset(
    {
        "aws_batch",
        "azure_batch",
        "docker_swarm",
        "google_batch",
        "kubernetes",
        "nomad",
    }
)
CONTAINER_PY = "ghcr.io/anchapin/scientific_python_image:latest"

#: Deadline (seconds) for the best-effort result-storage drain in
#: ``Campaign.run()``'s finally block (issue #1672).  Past this bound the
#: uploader logs-and-abandons whatever remains — an auxiliary storage
#: outage must never stall (let alone crash) campaign teardown, which
#: would defeat the restart-by-replay recovery story by preventing the
#: final ``run.json`` rewrite and ``cache.close()``.
_RESULT_STORAGE_CLOSE_TIMEOUT_S = 120.0


# NOTE (issue #1462): ``QuotaExceededError`` is defined in
# ``osimflow.campaign.quota`` and re-exported here (it stays in
# ``__all__``) so the public import paths
# ``osimflow.campaign.QuotaExceededError`` /
# ``osimflow.QuotaExceededError`` are unchanged.
#
# NOTE (issue #1539): ``CampaignAbortError`` is defined in
# ``osimflow.campaign.sample_trace`` (raised by the 3-strike
# checkpoint-failure abort, issue #739) and re-exported here the same
# way, so ``osimflow.campaign.CampaignAbortError`` /
# ``osimflow.CampaignAbortError`` are public import paths.


class CampaignError(OSimFlowRuntimeError):
    """Raised when a critical campaign-level error should abort execution.

    Unlike step-level errors that are caught and recorded in run.json,
    a CampaignError signals an unrecoverable condition that should halt
    the campaign immediately.
    """


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


# NOTE (issue #1462): ``_CancelRegistry`` and the ``_cancel_registry``
# singleton now live in ``osimflow.campaign.lifecycle`` and are
# re-exported above — tests import both names from this module.


# NOTE (issue #1462): the code-hash machinery (import-closure walking,
# ``_byos_file_hash``, ``_combine_code_hash``, ``compute_code_hashes``,
# ``code_hash_with_byos``) and the default chaos-engine builder now
# live in ``osimflow.campaign.code_hashes`` and
# ``osimflow.campaign.chaos`` respectively.


def _resolve_work_fn(
    explicit_fn: Callable[..., Any] | None,
    script_path: Path | None,
    default_fn: Callable[..., Any],
    cfg: CampaignConfig,
    label: str,
) -> Callable[..., Any]:
    """Resolve a BYOS-overridable work function.

    Precedence (issue #1462 extracted from ``Campaign.__init__`` to keep
    ``__init__`` under the too-many-branches budget): explicit
    constructor parameter > ``cfg`` BYOS script path (loaded via the
    canonical ``osimflow.byos`` loader) > package default.
    """
    if explicit_fn is not None:
        return explicit_fn
    if script_path is not None:
        from ..byos import load_user_function  # noqa: PLC0415

        log.info("loading BYOS %s from %s", label, script_path)
        return load_user_function(
            script_path,
            trust_level=cfg.byos_trust_level,
            timeout_s=cfg.byos_timeout_s,
        )
    return default_fn


def _verify_cosign_or_raise(cfg: CampaignConfig) -> None:
    """Verify the OpenStudio image signature when opted in (issue #1385).

    Extracted from ``Campaign.__init__`` (issue #1462).  When the
    operator sets ``--require-cosign-identity``, verify the OpenStudio
    image signature (keyless sigstore) BEFORE anything runs.  A cache
    hit must never silently consume a substituted image — on
    verification failure the campaign refuses to start, which is
    strictly stronger than forcing a cache miss (nothing is written to
    the cache from an untrusted image).
    """
    if not cfg.require_cosign_identity:
        return
    os_image_ref = build_cosign_image_ref(
        container="docker.io/nrel/openstudio",
        container_digest=cfg.container_digest,
        openstudio_version=cfg.openstudio_version,
    )
    issuer = cfg.cosign_oidc_issuer or DEFAULT_COSIGN_OIDC_ISSUER
    # Issue #1536: verifying a mutable-tag ref resolves tag→digest once
    # inside cosign, but the executors pull by tag later — a re-push
    # between verify and pull substitutes the executed image (TOCTOU).
    # Triangulate FIRST and verify the digest-pinned ref; failure to
    # triangulate refuses to run.
    source_tag: str | None = None
    if "@sha256:" not in os_image_ref:
        source_tag = os_image_ref
        os_image_ref = triangulate_image_ref(os_image_ref)
    try:
        verify_image_signature(
            os_image_ref,
            cfg.require_cosign_identity,
            issuer,
        )
    except CosignVerificationError:
        log.error(
            "cosign verification failed for %s — refusing to run the campaign "
            "(issue #1385). The image may have been substituted by a registry "
            "compromise; investigate before re-running.",
            os_image_ref,
        )
        raise
    # Issue #1536: persist the audit receipt with the digest that was
    # actually verified (and the source tag when the ref was
    # triangulated) alongside ``run.json``.
    receipt_digest = (
        os_image_ref.split("@sha256:", 1)[1].split("@", 1)[0]
        if "@sha256:" in os_image_ref
        else None
    )
    try:
        cfg.outdir.mkdir(parents=True, exist_ok=True)
        write_cosign_receipt(
            cfg.outdir,
            image_ref=os_image_ref,
            certificate_identity=str(cfg.require_cosign_identity),
            certificate_oidc_issuer=issuer,
            verified_digest=(f"sha256:{receipt_digest}" if receipt_digest else None),
            triangulated_from_tag=source_tag,
        )
    except OSError as exc:  # pragma: no cover - filesystem edge
        log.warning("could not write cosign verification receipt: %s", exc)
    log.info(
        "cosign verification passed for %s (identity=%s)",
        os_image_ref,
        cfg.require_cosign_identity,
    )


class Campaign(
    CampaignRunMixin,
    CampaignStepsMixin,
    CampaignAnalysisMixin,
    CampaignOptimizationMixin,
    CampaignKpisMixin,
):
    def __init__(
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
        self.apply_fn = _resolve_work_fn(
            apply_fn,
            cfg.custom_apply_script,
            default_apply_parameters,
            cfg,
            "apply_fn",
        )
        # Resolve extract_fn: explicit param > cfg.custom_kpi_extractor > default.
        self.extract_fn = _resolve_work_fn(
            extract_fn,
            cfg.custom_kpi_extractor,
            extract_kpis,
            cfg,
            "extract_fn",
        )
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
        # Document store (issue #1769): the RedisDocumentStore breaker
        # was previously wired to a ``self._document_store`` attribute
        # that was never assigned (the original branch was extracted in
        # the refactor without preserving the assignment, so the
        # ``getattr(self, "_document_store", None)`` call below always
        # saw ``None`` and the Redis control-plane breaker events were
        # silently dropped from run.json).  Build the store alongside
        # the cache so the wire-up actually fires for both
        # ``SQLiteDocumentStore`` (single-node) and
        # ``RedisDocumentStore`` (four-plane Redis control plane, issue
        # #1562 / ADR-0004).
        self._document_store = build_document_store(
            db_path=cfg.work_dir / "documents.sqlite",
            redis_url=cfg.redis_url,
            namespace=self._cache_namespace,
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
        # Issue #1536: the pullable digest-pinned ref every container
        # substrate must execute (``<repo>@sha256:<hex>``).  ``None`` when
        # the digest is unresolved — ``run()`` fails loudly for container
        # substrates in that case instead of silently resolving by tag.
        self._os_digest_ref = digest_pinned_image_ref(
            CONTAINER_OS.format(version=cfg.openstudio_version),
            self._os_container_digest,
        )
        # Issue #1385: when the operator opts in via
        # ``--require-cosign-identity``, verify the OpenStudio image
        # signature (keyless sigstore) BEFORE anything runs.
        _verify_cosign_or_raise(cfg)
        # Hash the code that affects per-step behavior so a `bin/*.py` edit
        # invalidates cached results. This is the fix for the
        # "Python glue invisible to cache hash" gotcha in
        # `.agents/results/architecture/0001-workflow-framework.md` (original
        # analysis: the `archive/agents-reports-1886` git tag). Passing the
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
        # correct backend is always used — NullBackend when "none" (zero
        # overhead) or a real backend when configured.
        self._obs = ObservabilityManager(cfg)

        # Per-sample trace assembly / incremental checkpointing
        # (issue #1462): SampleTrace rows, per-sample trace IDs (issue
        # #436), cost totals (issue #126), and the consecutive-
        # checkpoint-failure abort counter (issue #739).
        self._sample_traces = CampaignSampleTraceRecorder(
            trace=self.trace,
            sample_state=self._sample_state,
            obs=self._obs,
        )

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
                    background_dispatch=True,
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
        # Result-transport contract (issues #1333 → #1541): one frozen
        # ``ResultTransportConfig`` value object, constructed once here,
        # threaded through every ``submit()`` call. Ephemeral-runner
        # executors (Nomad, Kubernetes — issue #996) push results job-side
        # to object storage when a backend is configured; their handles
        # materialize the artifacts back to local paths. Everything else
        # returns results in-band over the shared filesystem.
        self._result_transport_config: ResultTransportConfig = (
            ResultTransportConfig.from_campaign_config(
                cfg,
                object_storage=(
                    self.executor.requires_remote_runner_payload
                    and self._result_storage is not None
                ),
            )
        )

        # Quota enforcement (issue #446 → #1462): the guard reads the
        # live trace / sample-state by reference so mid-campaign quota
        # checks see current totals.
        self._quota = CampaignQuotaGuard(
            cfg=cfg,
            trace=self.trace,
            sample_state=self._sample_state,
            max_workers=max_workers,
            maybe_alert=self._maybe_alert,
        )
        # Sharding (issue #1462): owns shard selection + labels.
        self._sharding = CampaignSharding(cfg)

        # EPW resolution / pre-flight validation (issue #55 → #1462).
        self._epw = CampaignEpwResolver(cfg)

        # Artifact / provenance / manifest writing (issue #277 → #1462).
        self._artifacts = CampaignArtifactWriter(cfg, self.trace)

        # Cost tracking (issue #447). Built here so the correct backend is
        # always used.  None when enable_cost_tracking is False (zero overhead).
        self._cost_tracker = CampaignCostTracker(
            campaign_id=self.trace.campaign_id,
            cfg=cfg,
            executor=executor,
        )

        # Chaos engine (issue #1013 → #1462): engine selection (explicit
        # ``chaos_engine`` > ``cfg.chaos`` when enabled with scenarios >
        # disabled) and the schedule-aware injection hook live in the
        # CampaignChaosWiring collaborator.  ``self._chaos_engine`` is
        # kept as a read-only mirror for backwards compatibility.
        self._chaos = CampaignChaosWiring(cfg, chaos_engine)
        self._chaos_engine: ChaosEngine | None = self._chaos.engine

        # Graceful shutdown / soft pause / signal handling (issues #255,
        # #553 → #1462): flags, locks, and handler bookkeeping live in
        # the CampaignLifecycle collaborator; thin delegating methods
        # below keep the instance API unchanged.  Issue #1542: the
        # lifecycle receives this Campaign as an explicit
        # ``CancelSignal`` protocol (structural — Campaign provides
        # request_cancel / request_pause / request_resume), NOT as the
        # Campaign type, so the collaborator carries no circular
        # back-reference.
        self._lifecycle = CampaignLifecycle(signal=self)

        # Consecutive checkpoint failure counter (issue #739) now lives
        # on CampaignSampleTraceRecorder (see self._sample_traces).

    def _maybe_alert(self, event_type: str, context: dict[str, Any]) -> None:
        """Send an alert if AlertManager is configured (issue #1180).

        Best-effort: failures are silently logged and never propagate.
        """
        if self._alert_manager is not None:
            try:
                self._alert_manager.notify(event_type, context)
            except Exception as exc:
                log.warning("alert %s failed: %s", event_type, exc)

    def _teardown_observability(self, duration: float) -> None:
        """Final observability recording + flush, best-effort (issue #1672).

        ``ObservabilityManager.flush()`` deliberately re-raises the first
        backend exception after attempting every backend, and
        ``stop_periodic_flush()`` performs a coordinated ``flush()`` as
        its last step — so a backend outage at teardown would raise out
        of ``run()``'s finally block, turning a successful multi-hour
        campaign into a crashed process or replacing the original
        in-flight exception and corrupting diagnosis. Contained here,
        mirroring the ``_maybe_alert`` pattern: log at ERROR, never
        raise. Belt-and-suspenders with the per-backend containment
        inside ``ObservabilityManager.record_*`` (issue #1668): that
        guards mid-run recording, this guards the teardown call sites.
        """
        try:
            self._obs.record_campaign_duration(duration)
        except Exception:
            log.exception("observability: failed to record campaign duration")
        try:
            self._obs.stop_periodic_flush()
        except Exception:
            log.exception("observability: periodic-flush teardown failed")
        try:
            self._obs.flush()
        except Exception:
            log.exception("observability: final flush failed")

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
        """Fail fast at ``run()`` start if the resource quota is violated.

        Delegates to :class:`~osimflow.campaign.quota.CampaignQuotaGuard`
        (issue #1462); raises ``QuotaExceededError`` on violation.
        """
        self._quota.enforce_start_quota()

    def _check_quota_exceeded(self) -> bool:
        """Return True if any hard quota limit has been reached.

        Delegates to the quota guard; see
        :meth:`CampaignQuotaGuard.check_quota_exceeded`.
        """
        return self._quota.check_quota_exceeded()

    def _effective_max_workers(self) -> int:
        """Return the effective max_workers bounded by max_concurrent_samples quota.

        Delegates to the quota guard; see
        :meth:`CampaignQuotaGuard.effective_max_workers`.
        """
        return self._quota.effective_max_workers()

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

        Delegates to :func:`osimflow.campaign.code_hashes.compute_code_hashes`
        (issue #1462).  The delegation preserves the historical unbound
        call shape ``Campaign._compute_code_hashes(stub)`` — the
        underlying function reads no real instance state and falls back
        to ``getattr(self, "cfg", None)`` when ``cfg`` is omitted.

        Returns a dict with keys ``bin`` (work scripts + bin shims +
        transitive work-layer import closure, issues #1021 / #1446),
        ``work`` (``osimflow.work``), and ``byos_apply`` / ``byos_kpi``
        (BYOS user-script content hashes, issue #1011).
        """
        return compute_code_hashes(self, cfg)

    def _code_hash_with_byos(self, byos_key: str) -> str:
        """Cache-key ``code_sha256`` optionally mixed with a BYOS hash.

        Delegates to
        :func:`osimflow.campaign.code_hashes.code_hash_with_byos`
        (issue #1462).  Returns ``code_hashes["bin"]`` unchanged when the
        BYOS entry is the ``"byos-unset"`` sentinel so existing cached
        entries keep hitting; otherwise the SHA-256 of ``bin|byos``.
        """
        return code_hash_with_byos(self.code_hashes, byos_key)

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
        """Return the per-sample trace ID, minting one on first access (issue #436)."""
        return self._sample_traces.trace_id_for(sample_id)

    def _finalize_samples(self) -> None:
        """Emit one SampleTrace per sample based on accumulated per-step state.

        Delegates to :class:`CampaignSampleTraceRecorder` (issue #1462);
        also records per-sample observability metrics and accumulates
        campaign-level cost totals, and deduplicates against incremental
        checkpoints.
        """
        self._sample_traces.finalize_samples()

    def _accumulate_cost_summary(self) -> None:
        """Sum per-sample costs into campaign-level totals (issue #126)."""
        self._sample_traces.accumulate_cost_summary()

    def _checkpoint_sample(self, sid: str) -> None:
        """Write an incremental run.json checkpoint for a single sample (issue #275).

        Aborts the campaign after 3 consecutive checkpoint failures by
        raising :class:`CampaignAbortError` (issue #739; propagation
        through the concurrent fan-out fixed in issue #1539).
        """
        self._sample_traces.checkpoint_sample(sid)

    def _mark_sample_failed(
        self,
        sid: str,
        step_name: str,
        error: BaseException,
        trace_id: str,
    ) -> None:
        """Record a per-sample failure for the given step (issue #1570).

        Delegates to
        :func:`osimflow.campaign.fanout.mark_sample_failed`
        (issue #1542) with a call-time dependency bundle so
        instance-level patches of ``_job_queue`` /
        ``_checkpoint_sample`` / ``_maybe_alert`` / ``_obs`` keep
        working.  See the fanout module for the four recorded
        side-effects (job-queue ``mark_failed``, ``_sample_state``
        keys, incremental checkpoint, observability metric +
        ``sample.failed`` alert).  The 3-strike
        ``CampaignAbortError`` from ``_checkpoint_sample``
        propagates unchanged so the concurrent fan-out remains
        abortable.
        """
        _mark_sample_failed_impl(self._fanout_deps(), sid, step_name, error, trace_id)

    def _compute_await_deadline(self, step_name: str) -> float | None:
        """Derive the per-step orchestrator-side await deadline (issue #1566).

        Delegates to
        :func:`osimflow.campaign.fanout.compute_await_deadline`
        (issue #1542): ``cfg.await_timeout_s`` (the
        ``--sample-await-timeout-s`` opt-in) honoured directly when
        set, otherwise ``max(DEFAULT_STEP_RESOURCES[step]
        ["time_min"] * 60, cfg.byos_timeout_s)``, ``None`` only when
        every orchestrator-side bound is opted out of.  See the
        fanout module for the full derivation notes.
        """
        return _compute_await_deadline_impl(self.cfg, step_name)

    def _fanout_deps(self) -> FanoutDeps:
        """Build the explicit fan-out dependency bundle (issue #1542).

        Every field is read from ``self`` **at call time** — not at
        construction — so the documented test seams
        (``patch.object(campaign, "_job_queue")``,
        ``patch.object(campaign, "_checkpoint_sample")``,
        ``patch.object(campaign, "_maybe_alert")``, ...) applied
        after construction behave exactly as before the fanout
        extraction.  The bundle is the complete set of dependencies
        :func:`osimflow.campaign.fanout.submit_and_await_all`
        needs; it deliberately carries no Campaign back-reference.
        """
        return FanoutDeps(
            cfg=self.cfg,
            trace=self.trace,
            obs=self._obs,
            job_queue=self._job_queue,
            sample_state=self._sample_state,
            max_workers=self.max_workers,
            effective_max_workers=self._effective_max_workers,
            compute_await_deadline=self._compute_await_deadline,
            trace_id_for=self._trace_id_for,
            checkpoint_sample=self._checkpoint_sample,
            mark_sample_failed=self._mark_sample_failed,
            maybe_alert=self._maybe_alert,
            check_cancel_requested=self._check_cancel_requested,
            cancel_requested=lambda: self._cancel_requested,
            check_pause_requested=self._check_pause_requested,
            write_paused_trace=self._write_paused_trace,
            # Issue #1538: early substrate kill from inside the wait
            # loop's cancel branch, so parked await threads unblock
            # before the bounded pool drain joins them.
            cancel_active_jobs=self._cancel_active_jobs,
        )

    def _submit_and_await_all(
        self,
        submissions: dict[str, tuple[Handle, Callable[[Any], None]]],
        step_name: str,
        recovery_manager: WorkerRecoveryManager | None = None,
        resubmit_callback: Callable[[str], Handle | None] | None = None,
    ) -> None:
        """Submit all samples to the executor, then await all results concurrently.

        Delegates to
        :func:`osimflow.campaign.fanout.submit_and_await_all`
        (issue #1542 — the fan-out wait loop extracted from
        ``campaign.py`` with its own unit tests).  This is the core
        of the concurrent fan-out fix (issue #286); see the fanout
        module for the full behavioural contract:

        - sequential await when ``max_workers <= 1``,
          ``ThreadPoolExecutor`` otherwise (bounded by
          ``resource_quota.max_concurrent_samples`` — issue #1009),
        - per-sample errors caught, logged with ``exc_info=True``,
          and recorded — never swallowed; ``CampaignAbortError``
          (3-strike checkpoint abort, issue #739/#1539) re-raised
          across the worker-thread boundary,
        - job-queue enqueue / mark-completed / mark-failed (issue #263),
        - cancellation and soft-pause honouring (issues #255, #553,
          #1537),
        - per-step await deadline (issue #1566) and the worker
          auto-recovery resubmit loop (issues #443, #1567).

        The dependency bundle is built at call time (see
        ``_fanout_deps``) so instance-level patches keep working.
        """
        _submit_and_await_all_impl(
            self._fanout_deps(),
            submissions,
            step_name,
            recovery_manager=recovery_manager,
            resubmit_callback=resubmit_callback,
        )

    def _dispatch_step_work(
        self,
        *,
        fn: Callable[..., Any],
        task_args: tuple[Any, ...],
        task_kwargs: dict[str, Any],
        exec_args: tuple[Any, ...],
        exec_kwargs: dict[str, Any],
    ) -> Handle:
        """Submit one per-sample step to the configured substrate (issue #1679).

        Thin delegator to
        :func:`osimflow.campaign.fanout.dispatch_step_work`.  See
        that helper for the full behavioural contract.  Picks
        ``task_queue.submit`` when set, otherwise
        ``executor.submit``; the two arg/kwargs surfaces are
        intentionally separate because the task-queue path takes the
        BYOS work function's natural signature while the
        executor path needs the executor-directive kwargs
        (``cpus`` / ``memory_mb`` / ``time_min`` / ``container`` /
        ``container_digest`` / ``result_hint`` / ``transport``).

        Used by ``step_apply_parameters``,
        ``step_run_openstudio_sim`` (primary fan-out and the
        auto-recovery resubmit callback), and the EXTRACT_KPIS step
        in :class:`osimflow.campaign.kpis.CampaignKpisMixin` —
        the four hand-rolled ``if self.task_queue is not None: ...
        else: self.executor.submit(...)`` branches collapse into
        this single helper (issue #1679 acceptance criterion).
        """
        return _dispatch_step_work_impl(
            task_queue=self.task_queue,
            executor=self.executor,
            fn=fn,
            task_args=task_args,
            task_kwargs=task_kwargs,
            exec_args=exec_args,
            exec_kwargs=exec_kwargs,
        )

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

        Delegates to
        :mod:`osimflow.campaign.baseline` (issue #1462).  No-op when
        no baseline is configured.
        """
        compute_baseline_comparison(self.cfg, self.trace, kpi_files)

    @staticmethod
    def _read_all_kpis(kpi_files: list[Path]) -> dict[str, dict[str, float]]:
        """Read KPI files into a {sample_id: {kpi_name: value}} mapping."""
        return read_all_kpis(kpi_files)

    @staticmethod
    def _compute_improvement_range(
        baseline_sid: str,
        baseline_kpis: dict[str, float],
        all_kpis: dict[str, dict[str, float]],
    ) -> dict[str, object]:
        """Compute pct improvement range for each KPI relative to baseline."""
        return compute_improvement_range(baseline_sid, baseline_kpis, all_kpis)

    def _archive_sample_artifacts(self, src: Path, dst: Path, patterns: list[str]) -> None:
        """Copy files matching *patterns* from *src* into *dst* (delegated).

        Uses ``shutil.copy2`` so timestamps are preserved; called from
        the archive-aware step methods when ``cfg.archive_intermediates``
        is ``True``.
        """
        CampaignArtifactWriter.archive_sample_artifacts(src, dst, patterns)

    def _baseline_sample_id(self) -> str | None:
        """Return the baseline sample_id from config, or None."""
        return _baseline_sid_from_cfg(self.cfg)

    # ------------------------------------------------------------------
    # EPW file helpers (issue #55 → #1462: logic lives in
    # CampaignEpwResolver; thin delegators below).
    # ------------------------------------------------------------------
    def _reject_unsupported_remote_hooks(self) -> None:
        """Fail before submission when a remote executor cannot carry a hook (issue #1812)."""
        check = getattr(self.executor, "validate_work_fn", None)
        if check is None:
            return
        check("apply", self.apply_fn)
        check("extract", self.extract_fn)

    def _load_variable_defs(self) -> list[dict[str, Any]]:
        """Load variable definitions from ``cfg.input_variables`` (variables.yml)."""
        return self._epw.load_variable_defs()

    @staticmethod
    def _collect_epw_mappings(
        variable_defs: list[dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Collect (variable_name, mapping_dict) for all epw_file targets."""
        return CampaignEpwResolver.collect_epw_mappings(variable_defs)

    def _preflight_validate_epw_files(self, variable_defs: list[dict[str, Any]]) -> None:
        """Pre-flight check: verify all mapped .epw files exist and are valid.

        Raises:
            FileNotFoundError: one or more mapped .epw files are missing.
            EPWValidationError: one or more .epw files fail format validation.
        """
        self._epw.preflight_validate_epw_files(variable_defs)

    def _check_epw_existence(
        self,
        epw_mappings: list[tuple[str, dict[str, Any]]],
        template_dir: Path,
    ) -> None:
        """Raise FileNotFoundError if any mapped .epw file is missing."""
        CampaignEpwResolver._check_epw_existence(epw_mappings, template_dir)

    def _check_epw_format(
        self,
        epw_mappings: list[tuple[str, dict[str, Any]]],
        template_dir: Path,
    ) -> None:
        """Raise EPWValidationError if any .epw file has invalid format."""
        self._epw._check_epw_format(epw_mappings, template_dir)

    def _resolve_epw_targets(
        self,
        params: dict[str, object],
        variable_defs: list[dict[str, Any]],
        weather_file_override: str | None = None,
    ) -> dict[str, object]:
        """Resolve ``epw_file`` targets in a sample's parameter dict.

        For each variable with ``target: epw_file``, look up the
        parameter value in the variable's ``mapping`` dict and inject
        the resolved .epw path under ``__epw_file__`` into a copy of
        *params*.  A per-sample ``weather_file`` override (GAP-009)
        takes precedence.

        Raises:
            ValueError: a parameter value is not in the variable's mapping.
        """
        return self._epw.resolve_epw_targets(
            params, variable_defs, weather_file_override=weather_file_override
        )

    # ------------------------------------------------------------------
    # Shell hooks (issue #108 → #1462: logic lives in
    # osimflow.campaign.hooks; thin delegators below).
    # ------------------------------------------------------------------
    def _run_init_script(self) -> None:
        """Run the init script before the first campaign step.

        Raises ``subprocess.CalledProcessError`` if the script exits
        non-zero, which aborts the campaign.
        """
        run_init_script(self.cfg, self.trace, self.executor.name)

    def _run_finalize_script(self, status: str, duration_s: float) -> None:
        """Run the finalize script after the last campaign step.

        Best-effort: a non-zero exit code is logged but does NOT raise.
        """
        run_finalize_script(self.cfg, self.trace, self.executor.name, status, duration_s)

    def _hook_env(self) -> dict[str, str]:
        """Build the environment dict for hook scripts."""
        return _build_hook_env(self.cfg, self.executor.name)

    def _shard_label(self) -> str | None:
        """Return the shard label for this campaign (delegates to CampaignSharding)."""
        return self._sharding.label()

    def _samples_manifest_path(self) -> Path:
        """Return the shard-aware samples-manifest path (delegates to CampaignSharding)."""
        return self._sharding.samples_manifest_path()

    def _apply_sharding(
        self,
        samples: list[SampleSpec],
        *,
        generation: int,
    ) -> list[SampleSpec]:
        """Return only samples assigned to this shard (delegates to CampaignSharding)."""
        return self._sharding.apply_sharding(samples, generation=generation)

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
    # Manifest writers (issue #277 → #1462: logic lives in
    # CampaignArtifactWriter; thin delegators below).
    # ------------------------------------------------------------------
    def _write_campaign_meta(self) -> None:
        """Write ``campaign_meta.json`` to outdir at campaign start."""
        self._artifacts.write_campaign_meta(self.executor.name, self._shard_label())

    def _write_provenance(self) -> None:
        """Write ``provenance.json`` to outdir at campaign completion."""
        self._artifacts.write_provenance(
            code_hashes=self.code_hashes,
            cache_stats=self.cache.stats(),
            latest_samples_file=self._latest_samples_file,
            shard_label=self._shard_label(),
        )

    def _write_artifact_manifest(self) -> None:
        """Write ``artifact_manifest.json`` to outdir after aggregation."""
        self._artifacts.write_artifact_manifest()

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
    # Graceful shutdown (issue #255 → #1462: logic lives in
    # CampaignLifecycle; these are thin delegating wrappers so the
    # instance API — including test seams that patch
    # ``_check_cancel_requested`` — is unchanged).
    # ------------------------------------------------------------------
    @property
    def _cancel_requested(self) -> bool:
        """Sticky cancellation flag (owned by CampaignLifecycle)."""
        return self._lifecycle.cancel_requested

    def request_cancel(self) -> None:
        """Request campaign cancellation.

        Called by the signal handler or by external code that wants to
        stop a running campaign. Thread-safe. Idempotent.
        """
        self._lifecycle.request_cancel()

    def request_pause(self) -> None:
        """CancelSignal protocol surface (issue #1542): alias for :meth:`pause`.

        ``Campaign`` satisfies the ``CancelSignal`` protocol
        structurally; this alias (plus ``request_cancel`` /
        ``request_resume``) is the explicit cancel/pause/resume
        callback surface the lifecycle collaborators depend on.
        """
        self.pause()

    def request_resume(self) -> None:
        """CancelSignal protocol surface (issue #1542): alias for :meth:`resume`."""
        self.resume()

    def _check_cancel_requested(self) -> bool:
        """Check if cancellation has been requested.

        Checks both the in-memory flag and the ``.stop`` file in the
        outdir (written by the REST API's stop endpoint or external
        tooling); the ``.stop`` poll is flock-protected (issue #649).
        """
        return self._lifecycle.check_cancel_requested(self.cfg.outdir)

    def _check_pause_requested(self) -> bool:
        """Check if pause has been requested via the ``.pause`` file.

        The pause flag is NOT latched — deleting the ``.pause`` file
        immediately unblocks new submissions (issue #798).
        """
        return self._lifecycle.check_pause_requested(self.cfg.outdir)

    def _setup_signal_handlers(self) -> None:
        """Register SIGINT/SIGTERM handlers to request graceful shutdown."""
        self._lifecycle.setup_signal_handlers()

    @staticmethod
    def _handle_signal(signum: int, _frame: object) -> None:
        """Signal handler that requests cancellation on the registered Campaign."""
        handle_signal(signum, _frame)

    def _restore_signal_handlers(self) -> None:
        """Restore the previous signal handlers."""
        self._lifecycle.restore_signal_handlers()

    def _cancel_active_jobs(self) -> None:
        """Cancel all active futures submitted to the executor."""
        self._lifecycle.cancel_active_jobs(self.executor)

    def _write_shutdown_trace(self, status: str = "cancelled") -> None:
        """Write run.json with cancellation status before exit."""
        self._lifecycle.write_shutdown_trace(self.trace, self.cfg.outdir, status)

    # ------------------------------------------------------------------
    # Soft pause / resume (issue #553 → #1462: delegated)
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Request campaign pause (soft-stop).

        Writes a ``.pause`` flag file to the campaign directory and
        records the ``paused_at`` timestamp in the run trace.  Running
        samples complete normally; the executor checks for the ``.pause``
        file between sample dispatches and skips queuing new ones.

        Thread-safe and idempotent.
        """
        self._lifecycle.pause(self.cfg.outdir, self.trace)

    def resume(self) -> None:
        """Resume a paused campaign.

        Removes the ``.pause`` flag file and clears ``paused_at`` from
        the run trace.  The executor's fan-out loop checks for the
        ``.pause`` file between sample dispatches and will resume
        queuing pending samples.

        Thread-safe and idempotent.
        """
        self._lifecycle.resume(self.cfg.outdir, self.trace)

    def _write_paused_trace(self) -> None:
        """Write run.json with paused status when a soft-pause is triggered."""
        self._lifecycle.write_paused_trace(self.trace, self.cfg.outdir)

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
