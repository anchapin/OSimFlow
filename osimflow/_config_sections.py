"""Focused config dataclasses split out of ``config.py`` (issue #1890)."""

import dataclasses
import logging
from pathlib import Path

from osimflow._config_quota import ResourceQuota
from osimflow.byos import ByosTrustLevel

log = logging.getLogger("osimflow.config")


# ======================================================================
# Focused configuration dataclasses (issue #767)
#
# These dataclasses split CampaignConfig into focused domains:
# - DAGConfig: DAG execution, retries, sharding, BYOS, and hooks
# - StorageConfig: result storage and S3 artifact settings
# - ObservabilityConfig: monitoring, alerting, and MLflow tracking
#
# CampaignConfig composes these and maintains backward compatibility
# via __getattr__ delegation for flat attribute access.
# ======================================================================


@dataclasses.dataclass
class DAGConfig:
    """DAG execution, parallelism, retry, sharding, and hook settings.

    Attributes
    ----------
    project
        Project name for campaign organization (issue #390).
    algorithm
        Sampling algorithm name (issue #121).
    max_generations
        Maximum number of DAG generations (issue #122).
    max_sample_retries
        Maximum per-sample retry attempts (issue #252).
    max_step_retries
        Maximum cross-step retry attempts (issue #416).
    worker_auto_recovery
        Enable worker auto-recovery (issue #443).
    dry_run
        Dry-run mode flag (issue #59).
    sample
        Single-sample mode index (issue #59).
    skip_preflight
        Whether to skip the preflight model run (issue #107).
    prebuilt_workflow
        Execute the supplied OSW package as-is: no model mutation and no
        variables (issue #1812).
    init_script
        Path to pre-campaign initialization script.
    finalize_script
        Path to post-campaign finalization script.
    init_script_timeout
        Wall-clock timeout in seconds for the ``--init-script``
        subprocess (issue #1685).  On expiry the child process group
        is killed and the campaign aborts with ``CampaignError``
        before any step runs.  Defaults to 600 s — a hung init script
        (e.g. stuck on an NFS mount or a lock) used to block
        ``Campaign.run()`` forever.
    finalize_script_timeout
        Wall-clock timeout in seconds for the ``--finalize-script``
        subprocess (issue #1685).  On expiry the child process group
        is killed and the timeout is logged as a warning, but the
        campaign returns normally so the ``finally`` block in
        ``Campaign.run()`` can still rewrite ``run.json``, fire the
        webhook, and call ``cache.close()``.  Defaults to 600 s.
    baseline
        Baseline comparison configuration (issue #64).
    weather_dir
        Subdirectory name for weather files.
    archive_intermediates
        Whether to archive intermediate simulation files.
    custom_apply_script
        Path to custom parameter application script.
    custom_kpi_extractor
        Path to custom KPI extraction script.
    shard_count
        Number of shards for campaign partitioning.
    shard_index
        Shard index for this worker.
    shard_start
        Sample index range start.
    shard_end
        Sample index range end.
    offline
        Air-gapped/offline mode flag (issue #261).
    offline_bundle
        Path to offline bundle directory.
    byos_trust_level
        BYOS script trust level (issue #269).
    byos_resource_limits
        BYOS subprocess resource limits (issue #343).
    byos_timeout_s
        BYOS / OpenStudio subprocess timeout in seconds (issue #1109).
        ``None`` (default) means effectively unbounded (issue #1534) —
        annual EnergyPlus simulations routinely exceed any stock bound.
    await_timeout_s
        Orchestrator-side per-step await deadline floor in seconds
        (issue #1566).  When set, the campaign derives the deadline
        passed to ``handle.result(timeout=...)`` as
        ``max(time_min_for_step * 60, byos_timeout_s, await_timeout_s)``
        and a wedged substrate (Nomad allocation that never reaches
        terminal, Docker Swarm service in a non-terminal state, K8s
        ``time_min=0`` yielding no ``activeDeadlineSeconds``) becomes
        a ``TimeoutError`` that flows through the existing
        per-sample failure recording.  ``None`` (default) preserves
        the pre-#1566 bare-``handle.result()`` behaviour for users
        who do not want an orchestrator-side bound.
    ecr_repository
        ECR repository URI for OpenStudio images (issue #144).
    resource_quota
        Resource quota limits (issue #446).
    redis_url
        Redis URL for distributed cache invalidation (issue #330).
    task_queue
        Distributed task queue backend (issue #335).
    dask_scheduler_address
        Dask scheduler address.
    """

    project: str = ""
    algorithm: str = "lhs"
    max_generations: int = 1
    max_sample_retries: int = 3
    max_step_retries: int = 2
    worker_auto_recovery: bool = True
    dry_run: bool = False
    sample: int | None = None
    skip_preflight: bool = False
    prebuilt_workflow: bool = False
    init_script: Path | None = None
    finalize_script: Path | None = None
    init_script_timeout: float = 600.0
    finalize_script_timeout: float = 600.0
    baseline: dict[str, object] | None = None
    weather_dir: str = "weather"
    archive_intermediates: bool = False
    custom_apply_script: Path | None = None
    custom_kpi_extractor: Path | None = None
    shard_count: int | None = None
    shard_index: int | None = None
    shard_start: int | None = None
    shard_end: int | None = None
    offline: bool = False
    offline_bundle: Path | None = None
    byos_trust_level: ByosTrustLevel = ByosTrustLevel.SUBPROCESS
    byos_resource_limits: dict[str, int] | None = None
    byos_timeout_s: float | None = None
    await_timeout_s: float | None = None
    ecr_repository: str | None = None
    resource_quota: ResourceQuota | None = None
    redis_url: str | None = None
    task_queue: str = "none"
    dask_scheduler_address: str | None = None


@dataclasses.dataclass
class StorageConfig:
    """Result storage and S3 artifact settings.

    Attributes
    ----------
    result_storage_backend
        Result storage backend name (issue #339).
    result_storage_bucket
        Storage bucket/container name.
    result_storage_endpoint
        S3-compatible endpoint URL.
    s3_artifact_bucket
        S3 artifact storage bucket (issue #601).
    s3_artifact_prefix
        S3 artifact key prefix.
    s3_artifact_region
        AWS region for S3 artifact storage.
    s3_artifact_endpoint
        S3-compatible endpoint for artifact storage.
    s3_artifact_presigned_url_expiration
        Pre-signed URL expiration in seconds.
    allow_insecure_storage_endpoint
        Opt-in flag for plaintext ``http://`` S3 storage endpoints
        (issue #1386).  Defaults to ``False`` (fail-closed — mirrors the
        Redis ``rediss://`` enforcement from issue #1321).  When ``True``
        the storage backend emits a loud ``WARNING`` and uses the
        plaintext endpoint anyway.
    """

    result_storage_backend: str = "local"
    result_storage_bucket: str = ""
    result_storage_endpoint: str | None = None
    s3_artifact_bucket: str = ""
    s3_artifact_prefix: str = ""
    s3_artifact_region: str | None = None
    s3_artifact_endpoint: str | None = None
    s3_artifact_presigned_url_expiration: int = 3600
    allow_insecure_storage_endpoint: bool = False


@dataclasses.dataclass
class ObservabilityConfig:
    """Observability, monitoring, alerting, and MLflow settings.

    Attributes
    ----------
    observability
        Observability backend name (issue #132).
    cloudwatch_namespace
        CloudWatch metric namespace.
    cloudwatch_log_group
        CloudWatch log group name.
    log_aggregation_url
        Log aggregation URL for distributed logging.
    prometheus_port
        Prometheus metrics HTTP port.
    otel_endpoint
        OpenTelemetry OTLP endpoint URL.
    alert_rules
        Alert rules YAML file path.
    alert_destinations
        Alert destinations YAML file path.
    mlflow_tracking_uri
        MLflow tracking server URI.
    registry_path
        Campaign registry database path.
    webhook_url
        Campaign completion webhook URL.
    enable_cost_tracking
        Enable cost tracking (issue #447).
    cost_on_demand_price
        On-demand price per vCPU-hour.
    cost_spot_price
        Spot price per vCPU-hour.
    """

    observability: str = "none"
    cloudwatch_namespace: str = "OSimFlow"
    cloudwatch_log_group: str | None = None
    log_aggregation_url: str | None = None
    prometheus_port: int = 9090
    otel_endpoint: str | None = None
    alert_rules: Path | None = None
    alert_destinations: Path | None = None
    mlflow_tracking_uri: str | None = None
    registry_path: Path | None = None
    webhook_url: str | None = None
    enable_cost_tracking: bool = False
    cost_on_demand_price: float = 0.05
    cost_spot_price: float = 0.03
    flush_interval_seconds: float = 30.0


# ======================================================================
# Chaos testing config (issue #1013)
# ======================================================================


@dataclasses.dataclass
class ChaosConfig:
    """Opt-in chaos testing settings (issue #1013).

    The ``chaos`` machinery in :mod:`osimflow.chaos` provides a
    ``ChaosEngine`` plus four built-in ``FaultInjector`` types
    (kill switch, network delay, CPU spike, memory pressure). The
    settings here drive the wiring from :class:`Campaign` into the
    engine so the fault scenarios actually fire during a campaign
    run.

    Everything is **off by default**. The ``kill_switch`` scenario
    logs a warning but does not actually terminate the orchestrator
    process (it does not map sample IDs to worker PIDs in this
    wiring), so even the most aggressive setting is non-destructive
    unless the operator deliberately pairs it with a real
    executor-side SIGTERM handling hook.

    Attributes
    ----------
    enabled
        Whether chaos injection is active during the campaign
        (default: False).
    scenarios
        Comma-separated list of scenario names to enable. Built-in
        scenarios:

        - ``kill_switch_simulator`` — ``KillSwitchSimulator(fail_after=fail_after)``
          (logs a warning; does not actually terminate a process — issue #1179)
        - ``kill_switch``  — alias for ``kill_switch_simulator`` (deprecated;
          emits a ``DeprecationWarning``)
        - ``network_delay`` — ``NetworkDelayInjector(delay_s=delay_s,
          jitter_s=jitter_s, probability=probability)``
        - ``cpu_spike``     — ``CPUSpikeInjector(duration_s=duration_s,
          intensity=intensity, probability=probability)``
        - ``memory_pressure`` — ``MemoryPressureInjector(size_mb=size_mb,
          duration_s=duration_s, probability=probability)``

        Empty list is a no-op even when ``enabled`` is True.
    schedule
        When to inject faults (default: ``"none"``). One of:

        - ``"before_step"`` — inject once before each DAG step starts
        - ``"after_step"``  — inject once after each DAG step finishes
        - ``"per_sample"``  — inject once per sample during the fan-out
          (run-sim + extract-kpi)
        - ``"none"``        — disabled (matches ``enabled=False``)

    probability
        Probability forwarded to the probability-aware injectors
        (network delay, CPU spike, memory pressure). 1.0 = always
        inject (default).
    delay_s
        ``delay_s`` for ``NetworkDelayInjector`` (seconds).
    jitter_s
        ``jitter_s`` for ``NetworkDelayInjector`` (seconds).
    duration_s
        Duration for CPU spike / memory pressure injectors (seconds).
    intensity
        CPU intensity fraction (0.0-1.0) for ``CPUSpikeInjector``.
    size_mb
        Size in MB for ``MemoryPressureInjector``.
    fail_after
        Number of inject calls before ``KillSwitchSimulator`` fires.
    """

    enabled: bool = False
    scenarios: list[str] = dataclasses.field(default_factory=list)
    schedule: str = "none"
    probability: float = 1.0
    delay_s: float = 0.1
    jitter_s: float = 0.05
    duration_s: float = 0.5
    intensity: float = 0.5
    size_mb: int = 64
    fail_after: int = 2
