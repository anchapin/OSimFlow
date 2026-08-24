"""Campaign configuration loader.

A thin wrapper around the `variables.yml` schema, plus the CLI flags
required (`--input_variables`,
`--template_sim_package`, `--n_samples`, `--outdir`,
`--openstudio_version`, `--archive_intermediates`).
"""

__all__ = [
    "AWSBatchConfig",
    "AzureBatchConfig",
    "CampaignConfig",
    "ChaosConfig",
    "DAGConfig",
    "GoogleBatchConfig",
    "LocalConfig",
    "NomadConfig",
    "ObservabilityConfig",
    "ResourceQuota",
    "SlurmConfig",
    "StorageConfig",
    "coerce_variable_type",
    "load_config",
]

import dataclasses
import json
import logging
import os
from pathlib import Path
from typing import Any, cast

import yaml

from .byos import ByosTrustLevel
from .validation import (
    ValidationError,
    validate_path_within,
    validate_template_package,
    validate_variables_yml,
)

log = logging.getLogger("osimflow.config")


# ======================================================================
# Resource quota (issue #446)
# ======================================================================


@dataclasses.dataclass
class ResourceQuota:
    """Campaign resource quota limits (issue #446).

    All fields are optional. A ``None`` value means "no limit" for that
    resource. Quota enforcement is checked at campaign start (fail-fast)
    and during per-step fan-out (skip further submissions when the
    quota is exhausted).

    Attributes
    ----------
    max_samples
        Maximum total number of samples across all generations.
        When ``None``, there is no limit.
    max_cost_usd
        Maximum total campaign cost in USD.
        When ``None``, there is no limit.
    max_wall_time_min
        Maximum wall-clock time for the entire campaign in minutes.
        When ``None``, there is no limit.
    max_concurrent_samples
        Maximum number of samples that may run concurrently.
        When ``None``, there is no limit (bounded only by the executor's
        ``max_workers``).
    """

    max_samples: int | None = None
    max_cost_usd: float | None = None
    max_wall_time_min: float | None = None
    max_concurrent_samples: int | None = None


def _parse_resource_quota(raw: object) -> ResourceQuota | None:
    """Parse a resource quota from a JSON-serializable dict, JSON string, or None."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"resource_quota must be a valid JSON dict: {exc}",
                field="resource_quota",
            ) from exc
    if not isinstance(raw, dict):
        raise ValidationError(
            f"resource_quota must be a dict, got {type(raw).__name__}",
            field="resource_quota",
        )
    quota = ResourceQuota(
        max_samples=_int_field(raw, "max_samples"),
        max_cost_usd=_float_field(raw, "max_cost_usd"),
        max_wall_time_min=_float_field(raw, "max_wall_time_min"),
        max_concurrent_samples=_int_field(raw, "max_concurrent_samples"),
    )
    if quota.max_samples is not None and quota.max_samples < 1:
        raise ValidationError(
            "Max samples must be >= 1",
            field="resource_quota.max_samples",
        )
    if quota.max_cost_usd is not None and quota.max_cost_usd < 0:
        raise ValidationError(
            "Max cost usd must be >= 0",
            field="resource_quota.max_cost_usd",
        )
    if quota.max_wall_time_min is not None and quota.max_wall_time_min <= 0:
        raise ValidationError(
            "Max wall time min must be > 0",
            field="resource_quota.max_wall_time_min",
        )
    if quota.max_concurrent_samples is not None and quota.max_concurrent_samples < 1:
        raise ValidationError(
            "Max concurrent samples must be >= 1",
            field="resource_quota.max_concurrent_samples",
        )
    return quota


def _int_field(raw: dict[str, object], key: str) -> int | None:
    val = raw.get(key)
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, (float, str)):
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    raise ValidationError(
        f"{key!r} must be an integer, got {type(val).__name__}",
        field=f"resource_quota.{key}",
    )


def _float_field(raw: dict[str, object], key: str) -> float | None:
    val = raw.get(key)
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            pass
    raise ValidationError(
        f"{key!r} must be a number, got {type(val).__name__}",
        field=f"resource_quota.{key}",
    )


# ======================================================================
# Type coercion (issue #409)
# ======================================================================

#: Maps type-name strings (as used in a variable's ``type`` field) to
#: their corresponding Python built-in types.
_TYPE_NAME_MAP: dict[str, type] = {
    "float": float,
    "double": float,
    "int": int,
    "integer": int,
    "bool": bool,
    "boolean": bool,
    "str": str,
    "string": str,
    "list": list,
}

#: Numeric distribution-parameter keys whose YAML values should be
#: coerced to ``float`` during loading (e.g. ``min: "1.0"`` → ``1.0``).
_NUMERIC_PARAM_KEYS: frozenset[str] = frozenset(
    {"min", "max", "mean", "sigma", "mode", "alpha", "beta", "loc", "scale", "rate"}
)


def coerce_variable_type(  # noqa: PLR0911, PLR0912, PLR0915
    value: Any, expected_type: str | type
) -> Any:
    """Coerce *value* to *expected_type*.

    Handles the common YAML-to-Python mismatches that arise when users
    write ``variables.yml`` by hand:

    - ``str → float``  (``"1.5"`` → ``1.5``)
    - ``str → int``    (``"3"`` → ``3``, ``"3.0"`` → ``3``)
    - ``str → bool``   (``"true"`` / ``"1"`` → ``True``, ``"false"`` / ``"0"`` → ``False``)
    - ``str → list``   (``"a, b, c"`` → ``["a", "b", "c"]``)
    - ``int → float``  (widening, always safe)
    - ``float → int``  (only when no precision is lost)

    When *value* is already an instance of *expected_type* it is returned
    unchanged.  Each coercion is logged at ``DEBUG`` level.

    Parameters
    ----------
    value
        The value to coerce (often a string from YAML).
    expected_type
        Target type — either a Python ``type`` object (``float``, ``int``,
        ``bool``, ``str``, ``list``) or a type-name string (``"float"``,
        ``"int"``, ``"bool"``, ``"str"``, ``"list"``; aliases ``"double"``,
        ``"integer"``, ``"boolean"``, ``"string"`` are also accepted).

    Returns
    -------
    The coerced value.

    Raises
    ------
    ValueError
        If *expected_type* is an unknown string, or if the value cannot
        be losslessly coerced.
    """
    # --- resolve string type name → Python type ----------------------
    if isinstance(expected_type, str):
        key = expected_type.lower().strip()
        if key not in _TYPE_NAME_MAP:
            raise ValueError(
                f"unknown type '{expected_type}'. Valid names: {', '.join(sorted(_TYPE_NAME_MAP))}"
            )
        expected_type = _TYPE_NAME_MAP[key]

    # --- already correct type → identity ------------------------------
    # Special-case: ``bool`` is a subclass of ``int`` in Python, so
    # ``isinstance(True, int)`` is ``True``.  We must *not* treat a bool
    # as an int (or vice-versa) when the caller explicitly requests one.
    if type(value) is expected_type:
        return value
    if (
        expected_type is not bool
        and isinstance(value, expected_type)
        and not isinstance(value, bool)
    ):
        return value

    # --- numeric widening / narrowing ---------------------------------
    if expected_type is float:
        if isinstance(value, bool):
            raise ValueError(
                f"cannot coerce bool {value!r} to float — use an explicit int or float value"
            )
        if isinstance(value, int):
            result = float(value)
            log.debug("coerced %r → %r (int→float)", value, result)
            return result
        if isinstance(value, str):
            result = float(value)  # may raise ValueError — let it propagate
            log.debug("coerced %r → %r (str→float)", value, result)
            return result

    if expected_type is int:
        if isinstance(value, bool):
            raise ValueError(f"cannot coerce bool {value!r} to int — use an explicit int value")
        if isinstance(value, float):
            if value != int(value):
                raise ValueError(f"cannot coerce {value!r} to int without loss of precision")
            result = int(value)
            log.debug("coerced %r → %r (float→int)", value, result)
            return result
        if isinstance(value, str):
            try:
                result = int(value)
            except ValueError:
                # Try float intermediary: "3.0" → 3
                f = float(value)
                if f != int(f):
                    raise ValueError(
                        f"cannot coerce {value!r} to int without loss of precision"
                    ) from None
                result = int(f)
            log.debug("coerced %r → %r (str→int)", value, result)
            return result

    # --- bool ---------------------------------------------------------
    if expected_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            norm = value.lower().strip()
            if norm in ("true", "1", "yes", "on"):
                log.debug("coerced %r → True (str→bool)", value)
                return True
            if norm in ("false", "0", "no", "off", ""):
                log.debug("coerced %r → False (str→bool)", value)
                return False
            raise ValueError(f"cannot coerce {value!r} to bool")
        if isinstance(value, (int, float)):
            result = bool(value)
            log.debug("coerced %r → %r (num→bool)", value, result)
            return result

    # --- list (comma-separated string → list) -------------------------
    if expected_type is list:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            parts: list[str] = [item.strip() for item in value.split(",") if item.strip()]
            log.debug("coerced %r → %r (str→list)", value, parts)
            return parts
        raise ValueError(f"cannot coerce {value!r} to list")

    # --- str (stringify anything) -------------------------------------
    if expected_type is str:
        return str(value)

    # --- fallback: try the type constructor ---------------------------
    try:
        return expected_type(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"cannot coerce {value!r} to {expected_type.__name__}") from exc


def _coerce_variables_yml_file(path: Path) -> bool:  # noqa: PLR0912
    """Normalise type representations in a ``variables.yml`` file in-place.

    Reads the YAML, coerces string-valued numeric distribution parameters
    (``min``, ``max``, ``mean``, ``sigma``, ``mode``, ``alpha``, ``beta``,
    ``loc``, ``scale``, ``rate``) to ``float``, and coerces comma-separated
    strings to lists for ``discrete`` / ``categorical`` distributions.

    The file is written back **only** when at least one value was changed.
    Individual coercion failures are silently skipped — the downstream
    ``validate_variables_yml`` call will report the original error with a
    clear message.

    Parameters
    ----------
    path
        Path to the ``variables.yml`` file.

    Returns
    -------
    bool
        ``True`` if the file was modified, ``False`` otherwise.
    """
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return False

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return False  # let validate_variables_yml report the syntax error

    if not isinstance(data, dict) or "variables" not in data:
        return False

    variables = data["variables"]
    if not isinstance(variables, list):
        return False

    changed = False
    for var in variables:
        if not isinstance(var, dict):
            continue

        # Coerce numeric distribution params from string to float.
        for key in _NUMERIC_PARAM_KEYS:
            if key in var and isinstance(var[key], str):
                try:
                    var[key] = coerce_variable_type(var[key], float)
                    changed = True
                except ValueError:
                    pass  # validation will report the error

        # Coerce comma-separated string → list for discrete/categorical.
        dist = var.get("distribution")
        if (
            dist in ("discrete", "categorical")
            and "values" in var
            and isinstance(var["values"], str)
        ):
            try:
                var["values"] = coerce_variable_type(var["values"], list)
                changed = True
            except ValueError:
                pass

    if changed:
        try:
            with path.open("w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            log.debug(
                "type coercion normalised %s (%d variable definitions)",
                path,
                len(variables),
            )
        except OSError as exc:
            log.warning("could not write type-coerced %s: %s", path, exc)
            return False

    return changed


# ======================================================================
# Executor-specific configuration dataclasses (issue #724)
#
# These dataclasses group executor-specific fields into dedicated config
# objects, reducing coupling in CampaignConfig. Each executor config is
# a frozen dataclass with sensible defaults. CampaignConfig maintains
# backward compatibility via __getattr__ delegation.
# ======================================================================


@dataclasses.dataclass(frozen=True)
class SlurmConfig:
    """Slurm executor configuration.

    Attributes
    ----------
    qos
        Quality of Service for the Slurm job.
    constraint
        Constraint for the Slurm job (e.g., "gpu").
    gres
        Generic resource specification (e.g., "gpu:1").
    cost_per_node_hour
        Cost per node-hour in USD for cost tracking.
    """

    qos: str | None = None
    constraint: str | None = None
    gres: str | None = None
    cost_per_node_hour: float = 0.0


@dataclasses.dataclass(frozen=True)
class AWSBatchConfig:
    """AWS Batch executor configuration.

    Attributes
    ----------
    max_spot_price_usd
        Maximum Spot price in USD per vCPU-hour. When set, the executor
        queries the current Spot price before submitting and rejects jobs
        that would exceed the ceiling.
    fallback_to_on_demand
        Whether to fall back to on-demand instances when Spot price
        exceeds the ceiling or max retries are exhausted.
    max_retries
        Maximum number of times a spot-interrupted job is retried before
        falling back or failing.
    submit_rps
        Submit rate-limit in requests per second applied via a shared
        token-bucket limiter (default 800, below AWS Batch's 1000 TPS
        account limit — issue #1010).
    """

    max_spot_price_usd: float | None = None
    fallback_to_on_demand: bool = False
    max_retries: int = 3
    submit_rps: float | None = None


@dataclasses.dataclass(frozen=True)
class AzureBatchConfig:
    """Azure Batch executor configuration.

    Attributes
    ----------
    account_name
        Azure Batch account name.
    account_url
        Azure Batch account URL.
    pool_id
        Azure Batch pool ID.
    location
        Azure region location.
    use_spot
        Whether to use Spot/Low-priority instances.
    fallback_to_on_demand
        Whether to fall back to on-demand instances when Spot is unavailable.
    max_retries
        Maximum number of retries for failed jobs.
    """

    account_name: str | None = None
    account_url: str | None = None
    pool_id: str = "osimflow-pool"
    location: str = "eastus"
    use_spot: bool = False
    fallback_to_on_demand: bool = False
    max_retries: int = 3


@dataclasses.dataclass(frozen=True)
class GoogleBatchConfig:
    """Google Cloud Batch executor configuration.

    Attributes
    ----------
    project_id
        Google Cloud project ID.
    region
        Google Cloud region.
    service_account
        Service account email for the job.
    use_spot
        Whether to use Spot/Preemptible instances.
    fallback_to_on_demand
        Whether to fall back to on-demand instances when Spot is unavailable.
    max_retries
        Maximum number of retries for failed jobs.
    """

    project_id: str | None = None
    region: str = "us-central1"
    service_account: str | None = None
    use_spot: bool = False
    fallback_to_on_demand: bool = False
    max_retries: int = 3


@dataclasses.dataclass(frozen=True)
class NomadConfig:
    """Nomad executor configuration.

    Attributes
    ----------
    dispatch_policy
        Dispatch policy for job submission.
    allocation_resolution_timeout_s
        Timeout for allocation ID resolution.
    poll_interval_s
        Polling interval for allocation status.
    max_poll_interval_s
        Maximum polling interval (exponential backoff cap).
    fanout_submit_rate_per_sec
        Rate limit for fan-out submissions (jobs per second).
    fanout_submit_chunk_size
        Chunk size for fan-out submissions.
    tls
        Whether to use TLS for Nomad connection.
    cert
        Path to client certificate file.
    key
        Path to client key file.
    ca_cert
        Path to CA certificate file.
    """

    dispatch_policy: str = "keep_manual"
    allocation_resolution_timeout_s: float = 30.0
    poll_interval_s: float = 5.0
    max_poll_interval_s: float = 60.0
    fanout_submit_rate_per_sec: float | None = None
    fanout_submit_chunk_size: int = 0
    tls: bool = False
    cert: Path | None = None
    key: Path | None = None
    ca_cert: Path | None = None


@dataclasses.dataclass(frozen=True)
class LocalConfig:
    """Local executor configuration.

    Attributes
    ----------
    max_workers
        Maximum number of parallel workers (stored separately, accessed
        via CLI --max-workers, not this config).
    """

    max_workers: int = 1


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
    init_script
        Path to pre-campaign initialization script.
    finalize_script
        Path to post-campaign finalization script.
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
    init_script: Path | None = None
    finalize_script: Path | None = None
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
    byos_timeout_s: float = 600.0
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
    """

    result_storage_backend: str = "local"
    result_storage_bucket: str = ""
    result_storage_endpoint: str | None = None
    s3_artifact_bucket: str = ""
    s3_artifact_prefix: str = ""
    s3_artifact_region: str | None = None
    s3_artifact_endpoint: str | None = None
    s3_artifact_presigned_url_expiration: int = 3600


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

        - ``kill_switch``  — ``KillSwitchInjector(fail_after=fail_after)``
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
        Number of inject calls before ``KillSwitchInjector`` fires.
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


# ======================================================================
# CampaignConfig with backward-compatible attribute delegation
# ======================================================================


@dataclasses.dataclass
class CampaignConfig:
    """Campaign configuration with focused config groups.

    This dataclass bundles all settings for a campaign run. To reduce
    coupling and improve IDE support, fields are grouped into dedicated
    config dataclasses (issue #767):

    - ``dag`` :class:`DAGConfig` for DAG execution, sharding, retries, and hooks
    - ``storage`` :class:`StorageConfig` for result storage and S3 artifact settings
    - ``observability`` :class:`ObservabilityConfig` for monitoring and alerting
    - ``slurm`` :class:`SlurmConfig` for Slurm executor settings
    - ``aws_batch`` :class:`AWSBatchConfig` for AWS Batch settings
    - ``azure_batch`` :class:`AzureBatchConfig` for Azure Batch settings
    - ``google_batch`` :class:`GoogleBatchConfig` for Google Cloud Batch settings
    - ``nomad`` :class:`NomadConfig` for Nomad executor settings

    For backward compatibility, all fields are also accessible directly as
    flat attributes on CampaignConfig (e.g., ``cfg.project`` or
    ``cfg.result_storage_backend``). This is achieved via ``__getattr__``
    delegation. New code should prefer the grouped config objects.

    Attributes
    ----------
    input_variables
        Path to the variables.yml file defining parametric variables.
    template_sim_package
        Path to the template simulation package directory.
    n_samples
        Number of LHS samples to generate.
    outdir
        Output directory for campaign results.
    openstudio_version
        OpenStudio version string (e.g., "3.11.0").
    dag
        Composed DAG execution, sharding, retries, and hook settings.
    storage
        Composed result storage and S3 artifact settings.
    observability
        Composed observability, monitoring, alerting, and MLflow settings.
    objective
        Objective function configuration (issue #282).
    constraints
        Constraint definitions (issue #282).
    nsga2_reference_points
        R-NSGA-II reference points (issue #529).
    nsga2_reference_directions
        R-NSGA-II reference direction strategy (issue #529).
    nsga2_ref_points
        R-NSGA-II reference points (alias, issue #529).
    nsga2_ref_dirs_strategy
        R-NSGA-II reference direction strategy (alias, issue #529).
    uq_method
        UQ sampling method (issue #530).
    uq_n_samples
        Number of UQ samples (issue #530).
    uq_failure_thresholds
        Failure thresholds for UQ analysis (issue #530).
    bcl_api_key
        NREL BCL API key (issue #580).
    validate_measures
        Validate measures against BCL taxonomy (issue #580).
    slurm
        Slurm executor configuration (grouped).
    aws_batch
        AWS Batch executor configuration (grouped).
    azure_batch
        Azure Batch executor configuration (grouped).
    google_batch
        Google Cloud Batch executor configuration (grouped).
    nomad
        Nomad executor configuration (grouped).
    chaos
        Opt-in chaos testing settings (issue #1013).
    kpis
        Optional list of KPI names to extract (issue #1082). When set,
        only the named KPIs are written to the per-sample KPI JSON.
        When ``None`` (default), all available KPIs are extracted.
    """

    # --- Core required fields ---
    input_variables: Path
    template_sim_package: Path
    n_samples: int
    outdir: Path
    openstudio_version: str

    # --- Container image pinning (issue #1081) ---
    container_digest: str | None = None

    # --- Composed focused configs (issue #767, init=False for backward compat) ---
    dag: DAGConfig = dataclasses.field(init=False)
    storage: StorageConfig = dataclasses.field(init=False)
    chaos: ChaosConfig = dataclasses.field(init=False)
    _observability: ObservabilityConfig = dataclasses.field(init=False)

    # --- Objective and constraints (issue #282) ---
    objective: dict[str, object] | None = None
    constraints: list[dict[str, object]] | None = None

    # --- R-NSGA-II options (issue #529) ---
    nsga2_reference_points: str | None = None
    nsga2_reference_directions: str | None = None
    nsga2_ref_points: str | None = None
    nsga2_ref_dirs_strategy: str | None = None

    # --- UQ configuration (issue #530) ---
    uq_method: str = "latin_hypercube"
    uq_n_samples: int | None = None
    uq_failure_thresholds: list[str] | None = None

    # --- BCL API (issue #580) ---
    bcl_api_key: str | None = None
    validate_measures: bool = False

    # --- Executor-specific configs (grouped, issue #724) ---
    slurm: SlurmConfig | None = None
    aws_batch: AWSBatchConfig | None = None
    azure_batch: AzureBatchConfig | None = None
    google_batch: GoogleBatchConfig | None = None
    nomad: NomadConfig | None = None

    # --- Legacy flat DAG fields (for backward compatibility) ---
    # These fields are still accepted in the constructor. They are
    # used to initialize the dag composed config in __post_init__.
    project: str = ""
    algorithm: str = "lhs"
    max_generations: int = 1
    max_sample_retries: int = 3
    max_step_retries: int = 2
    worker_auto_recovery: bool = True
    dry_run: bool = False
    sample: int | None = None
    skip_preflight: bool = False
    init_script: Path | None = None
    finalize_script: Path | None = None
    baseline: dict[str, object] | None = None
    weather_dir: str = "weather"
    archive_intermediates: bool = False
    custom_apply_script: Path | None = None
    custom_kpi_extractor: Path | None = None
    # --- KPI extraction configuration (issue #1082) ---
    kpis: list[str] | None = None
    shard_count: int | None = None
    shard_index: int | None = None
    shard_start: int | None = None
    shard_end: int | None = None
    offline: bool = False
    offline_bundle: Path | None = None
    byos_trust_level: ByosTrustLevel = ByosTrustLevel.SUBPROCESS
    byos_resource_limits: dict[str, int] | None = None
    byos_timeout_s: float = 600.0
    require_trusted_scripts: bool = True
    ecr_repository: str | None = None
    resource_quota: ResourceQuota | None = None
    redis_url: str | None = None
    task_queue: str = "none"
    dask_scheduler_address: str | None = None

    # --- Legacy flat executor fields (for backward compatibility) ---
    slurm_qos: str | None = None
    slurm_constraint: str | None = None
    slurm_gres: str | None = None
    slurm_cost_per_node_hour: float = 0.0

    aws_batch_max_spot_price_usd: float | None = None
    aws_batch_fallback_to_on_demand: bool = False
    aws_batch_max_retries: int = 3
    aws_batch_submit_rps: float | None = None

    azure_batch_account_name: str | None = None
    azure_batch_account_url: str | None = None
    azure_batch_pool_id: str = "osimflow-pool"
    azure_batch_location: str = "eastus"
    azure_use_spot: bool = False
    azure_fallback_to_on_demand: bool = False
    azure_max_retries: int = 3

    google_batch_project_id: str | None = None
    google_batch_region: str = "us-central1"
    google_batch_service_account: str | None = None
    google_use_spot: bool = False
    google_fallback_to_on_demand: bool = False
    google_max_retries: int = 3

    nomad_dispatch_policy: str = "keep_manual"
    nomad_allocation_resolution_timeout_s: float = 30.0
    nomad_poll_interval_s: float = 5.0
    nomad_max_poll_interval_s: float = 60.0
    nomad_fanout_submit_rate_per_sec: float | None = None
    nomad_fanout_submit_chunk_size: int = 0
    nomad_tls: bool = False
    nomad_cert: Path | None = None
    nomad_key: Path | None = None
    nomad_ca_cert: Path | None = None

    # --- Legacy flat Kubernetes executor fields (issue #997) ---
    # Native Job controls: ``backoff_limit`` (default 0 preserves the
    # orchestrator-side retry semantics from ``max_sample_retries``),
    # ``ttl_seconds_after_finished`` (default None), and Kueue
    # ``queue_name`` (default None).
    kubernetes_backoff_limit: int = 0
    kubernetes_ttl_seconds_after_finished: int | None = None
    kubernetes_queue_name: str | None = None

    # --- Legacy flat storage fields (for backward compatibility) ---
    result_storage_backend: str = "local"
    result_storage_bucket: str = ""
    result_storage_endpoint: str | None = None
    s3_artifact_bucket: str = ""
    s3_artifact_prefix: str = ""
    s3_artifact_region: str | None = None
    s3_artifact_endpoint: str | None = None
    s3_artifact_presigned_url_expiration: int = 3600

    # --- Legacy flat observability fields (for backward compatibility) ---
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

    # --- Legacy flat chaos fields (issue #1013) ---
    chaos_enabled: bool = False
    chaos_scenarios: list[str] = dataclasses.field(default_factory=list)
    chaos_schedule: str = "none"
    chaos_probability: float = 1.0
    chaos_delay_s: float = 0.1
    chaos_jitter_s: float = 0.05
    chaos_duration_s: float = 0.5
    chaos_intensity: float = 0.5
    chaos_size_mb: int = 64
    chaos_fail_after: int = 2

    def __post_init__(self) -> None:
        """Initialize composed configs and executor configs from flat fields."""
        # Initialize dag config from flat fields (always, since init=False)
        self.dag = DAGConfig(
            project=self.project,
            algorithm=self.algorithm,
            max_generations=self.max_generations,
            max_sample_retries=self.max_sample_retries,
            max_step_retries=self.max_step_retries,
            worker_auto_recovery=self.worker_auto_recovery,
            dry_run=self.dry_run,
            sample=self.sample,
            skip_preflight=self.skip_preflight,
            init_script=self.init_script,
            finalize_script=self.finalize_script,
            baseline=self.baseline,
            weather_dir=self.weather_dir,
            archive_intermediates=self.archive_intermediates,
            custom_apply_script=self.custom_apply_script,
            custom_kpi_extractor=self.custom_kpi_extractor,
            shard_count=self.shard_count,
            shard_index=self.shard_index,
            shard_start=self.shard_start,
            shard_end=self.shard_end,
            offline=self.offline,
            offline_bundle=self.offline_bundle,
            byos_trust_level=self.byos_trust_level,
            byos_resource_limits=self.byos_resource_limits,
            byos_timeout_s=self.byos_timeout_s,
            ecr_repository=self.ecr_repository,
            resource_quota=self.resource_quota,
            redis_url=self.redis_url,
            task_queue=self.task_queue,
            dask_scheduler_address=self.dask_scheduler_address,
        )

        # Initialize storage config from flat fields (always, since init=False)
        self.storage = StorageConfig(
            result_storage_backend=self.result_storage_backend,
            result_storage_bucket=self.result_storage_bucket,
            result_storage_endpoint=self.result_storage_endpoint,
            s3_artifact_bucket=self.s3_artifact_bucket,
            s3_artifact_prefix=self.s3_artifact_prefix,
            s3_artifact_region=self.s3_artifact_region,
            s3_artifact_endpoint=self.s3_artifact_endpoint,
            s3_artifact_presigned_url_expiration=self.s3_artifact_presigned_url_expiration,
        )

        # Initialize observability config from flat fields (always, since init=False)
        self._observability = ObservabilityConfig(
            observability=self.observability,
            cloudwatch_namespace=self.cloudwatch_namespace,
            cloudwatch_log_group=self.cloudwatch_log_group,
            log_aggregation_url=self.log_aggregation_url,
            prometheus_port=self.prometheus_port,
            otel_endpoint=self.otel_endpoint,
            alert_rules=self.alert_rules,
            alert_destinations=self.alert_destinations,
            mlflow_tracking_uri=self.mlflow_tracking_uri,
            registry_path=self.registry_path,
            webhook_url=self.webhook_url,
            enable_cost_tracking=self.enable_cost_tracking,
            cost_on_demand_price=self.cost_on_demand_price,
            cost_spot_price=self.cost_spot_price,
        )

        # Slurm config
        if self.slurm is None:
            self.slurm = SlurmConfig(
                qos=self.slurm_qos,
                constraint=self.slurm_constraint,
                gres=self.slurm_gres,
                cost_per_node_hour=self.slurm_cost_per_node_hour,
            )

        # AWS Batch config
        if self.aws_batch is None:
            self.aws_batch = AWSBatchConfig(
                max_spot_price_usd=self.aws_batch_max_spot_price_usd,
                fallback_to_on_demand=self.aws_batch_fallback_to_on_demand,
                max_retries=self.aws_batch_max_retries,
                submit_rps=self.aws_batch_submit_rps,
            )

        # Azure Batch config
        if self.azure_batch is None:
            self.azure_batch = AzureBatchConfig(
                account_name=self.azure_batch_account_name,
                account_url=self.azure_batch_account_url,
                pool_id=self.azure_batch_pool_id,
                location=self.azure_batch_location,
                use_spot=self.azure_use_spot,
                fallback_to_on_demand=self.azure_fallback_to_on_demand,
                max_retries=self.azure_max_retries,
            )

        # Google Batch config
        if self.google_batch is None:
            self.google_batch = GoogleBatchConfig(
                project_id=self.google_batch_project_id,
                region=self.google_batch_region,
                service_account=self.google_batch_service_account,
                use_spot=self.google_use_spot,
                fallback_to_on_demand=self.google_fallback_to_on_demand,
                max_retries=self.google_max_retries,
            )

        # Nomad config
        if self.nomad is None:
            self.nomad = NomadConfig(
                dispatch_policy=self.nomad_dispatch_policy,
                allocation_resolution_timeout_s=self.nomad_allocation_resolution_timeout_s,
                poll_interval_s=self.nomad_poll_interval_s,
                max_poll_interval_s=self.nomad_max_poll_interval_s,
                fanout_submit_rate_per_sec=self.nomad_fanout_submit_rate_per_sec,
                fanout_submit_chunk_size=self.nomad_fanout_submit_chunk_size,
                tls=self.nomad_tls,
                cert=self.nomad_cert,
                key=self.nomad_key,
                ca_cert=self.nomad_ca_cert,
            )

        # Chaos config (issue #1013). Built unconditionally so that
        # ``self.chaos`` is always present on the CampaignConfig instance;
        # the field is composed from the legacy flat ``chaos_*`` fields
        # so ``Campaign(chaos_engine=...)`` can pick them up via
        # ``cfg.chaos.*`` without any user code changes. When
        # ``chaos_enabled`` is set without explicit scenarios, fall back
        # to ``["kill_switch"]`` so the engine is enabled-but-empty
        # never happens — kill_switch is the cheapest safe default.
        scenarios_for_chaos = list(self.chaos_scenarios)
        if self.chaos_enabled and not scenarios_for_chaos:
            scenarios_for_chaos = ["kill_switch"]
        self.chaos = ChaosConfig(
            enabled=self.chaos_enabled,
            scenarios=scenarios_for_chaos,
            schedule=self.chaos_schedule,
            probability=self.chaos_probability,
            delay_s=self.chaos_delay_s,
            jitter_s=self.chaos_jitter_s,
            duration_s=self.chaos_duration_s,
            intensity=self.chaos_intensity,
            size_mb=self.chaos_size_mb,
            fail_after=self.chaos_fail_after,
        )

    def _get_legacy_field(self, name: str, default: Any) -> Any:
        """Get a legacy flat field value for composed config initialization."""
        return getattr(self, name, default)

    # Static mapping: legacy flat attribute name -> (config_attr_name, field_name)
    # Used by __getattr__ to delegate to grouped configs.
    _DELEGATED_ATTRS: dict[str, tuple[str, str]] = dataclasses.field(
        init=False, repr=False, compare=False
    )

    def __getattr__(self, name: str) -> Any:
        """Delegate attribute access to grouped configs for backward compatibility.

        This allows both `cfg.project` (legacy flat access) and
        `cfg.dag.project` (grouped access) to work for all composed configs.
        """
        # Build delegation map lazily on first access
        if "_DELEGATED_ATTRS" not in self.__dict__:
            self._DELEGATED_ATTRS = {
                # DAG config delegation
                "project": ("dag", "project"),
                "algorithm": ("dag", "algorithm"),
                "max_generations": ("dag", "max_generations"),
                "max_sample_retries": ("dag", "max_sample_retries"),
                "max_step_retries": ("dag", "max_step_retries"),
                "worker_auto_recovery": ("dag", "worker_auto_recovery"),
                "dry_run": ("dag", "dry_run"),
                "sample": ("dag", "sample"),
                "skip_preflight": ("dag", "skip_preflight"),
                "init_script": ("dag", "init_script"),
                "finalize_script": ("dag", "finalize_script"),
                "baseline": ("dag", "baseline"),
                "weather_dir": ("dag", "weather_dir"),
                "archive_intermediates": ("dag", "archive_intermediates"),
                "custom_apply_script": ("dag", "custom_apply_script"),
                "custom_kpi_extractor": ("dag", "custom_kpi_extractor"),
                "shard_count": ("dag", "shard_count"),
                "shard_index": ("dag", "shard_index"),
                "shard_start": ("dag", "shard_start"),
                "shard_end": ("dag", "shard_end"),
                "offline": ("dag", "offline"),
                "offline_bundle": ("dag", "offline_bundle"),
                "byos_trust_level": ("dag", "byos_trust_level"),
                "byos_resource_limits": ("dag", "byos_resource_limits"),
                "byos_timeout_s": ("dag", "byos_timeout_s"),
                "ecr_repository": ("dag", "ecr_repository"),
                "resource_quota": ("dag", "resource_quota"),
                "redis_url": ("dag", "redis_url"),
                "task_queue": ("dag", "task_queue"),
                "dask_scheduler_address": ("dag", "dask_scheduler_address"),
                # Storage config delegation
                "result_storage_backend": ("storage", "result_storage_backend"),
                "result_storage_bucket": ("storage", "result_storage_bucket"),
                "result_storage_endpoint": ("storage", "result_storage_endpoint"),
                "s3_artifact_bucket": ("storage", "s3_artifact_bucket"),
                "s3_artifact_prefix": ("storage", "s3_artifact_prefix"),
                "s3_artifact_region": ("storage", "s3_artifact_region"),
                "s3_artifact_endpoint": ("storage", "s3_artifact_endpoint"),
                "s3_artifact_presigned_url_expiration": (
                    "storage",
                    "s3_artifact_presigned_url_expiration",
                ),
                # Observability config delegation
                "observability": ("_observability", "observability"),
                "cloudwatch_namespace": ("observability", "cloudwatch_namespace"),
                "cloudwatch_log_group": ("observability", "cloudwatch_log_group"),
                "log_aggregation_url": ("observability", "log_aggregation_url"),
                "prometheus_port": ("observability", "prometheus_port"),
                "otel_endpoint": ("observability", "otel_endpoint"),
                "alert_rules": ("observability", "alert_rules"),
                "alert_destinations": ("observability", "alert_destinations"),
                "mlflow_tracking_uri": ("observability", "mlflow_tracking_uri"),
                "registry_path": ("observability", "registry_path"),
                "webhook_url": ("observability", "webhook_url"),
                "enable_cost_tracking": ("observability", "enable_cost_tracking"),
                "cost_on_demand_price": ("observability", "cost_on_demand_price"),
                "cost_spot_price": ("observability", "cost_spot_price"),
                # Slurm executor delegation
                "slurm_qos": ("slurm", "qos"),
                "slurm_constraint": ("slurm", "constraint"),
                "slurm_gres": ("slurm", "gres"),
                "slurm_cost_per_node_hour": ("slurm", "cost_per_node_hour"),
                # AWS Batch executor delegation
                "aws_batch_max_spot_price_usd": ("aws_batch", "max_spot_price_usd"),
                "aws_batch_fallback_to_on_demand": ("aws_batch", "fallback_to_on_demand"),
                "aws_batch_max_retries": ("aws_batch", "max_retries"),
                "aws_batch_submit_rps": ("aws_batch", "submit_rps"),
                # Azure Batch executor delegation
                "azure_batch_account_name": ("azure_batch", "account_name"),
                "azure_batch_account_url": ("azure_batch", "account_url"),
                "azure_batch_pool_id": ("azure_batch", "pool_id"),
                "azure_batch_location": ("azure_batch", "location"),
                "azure_use_spot": ("azure_batch", "use_spot"),
                "azure_fallback_to_on_demand": ("azure_batch", "fallback_to_on_demand"),
                "azure_max_retries": ("azure_batch", "max_retries"),
                # Google Batch executor delegation
                "google_batch_project_id": ("google_batch", "project_id"),
                "google_batch_region": ("google_batch", "region"),
                "google_batch_service_account": ("google_batch", "service_account"),
                "google_use_spot": ("google_batch", "use_spot"),
                "google_fallback_to_on_demand": ("google_batch", "fallback_to_on_demand"),
                "google_max_retries": ("google_batch", "max_retries"),
                # Nomad executor delegation
                "nomad_dispatch_policy": ("nomad", "dispatch_policy"),
                "nomad_allocation_resolution_timeout_s": (
                    "nomad",
                    "allocation_resolution_timeout_s",
                ),
                "nomad_poll_interval_s": ("nomad", "poll_interval_s"),
                "nomad_max_poll_interval_s": ("nomad", "max_poll_interval_s"),
                "nomad_fanout_submit_rate_per_sec": ("nomad", "fanout_submit_rate_per_sec"),
                "nomad_fanout_submit_chunk_size": ("nomad", "fanout_submit_chunk_size"),
                "nomad_tls": ("nomad", "tls"),
                "nomad_cert": ("nomad", "cert"),
                "nomad_key": ("nomad", "key"),
                "nomad_ca_cert": ("nomad", "ca_cert"),
                # Chaos config delegation (issue #1013)
                "chaos_enabled": ("chaos", "enabled"),
                "chaos_scenarios": ("chaos", "scenarios"),
                "chaos_schedule": ("chaos", "schedule"),
                "chaos_probability": ("chaos", "probability"),
                "chaos_delay_s": ("chaos", "delay_s"),
                "chaos_jitter_s": ("chaos", "jitter_s"),
                "chaos_duration_s": ("chaos", "duration_s"),
                "chaos_intensity": ("chaos", "intensity"),
                "chaos_size_mb": ("chaos", "size_mb"),
                "chaos_fail_after": ("chaos", "fail_after"),
            }
        if name in self._DELEGATED_ATTRS:
            config_attr, field_name = self._DELEGATED_ATTRS[name]
            return getattr(getattr(self, config_attr), field_name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    @property
    def work_dir(self) -> Path:
        return self.outdir / "work"

    @property
    def samples_file(self) -> Path:
        return self.work_dir / "samples.json"

    @property
    def cache_db(self) -> Path:
        return self.work_dir / "cache.sqlite"


def _parse_objective_and_constraints(
    yml_data: dict[str, object],
) -> tuple[dict[str, object] | None, list[dict[str, object]] | None]:
    """Parse ``objective`` and ``constraints`` sections from variables.yml (issue #282)."""
    objective: dict[str, object] | None = None
    constraints: list[dict[str, object]] | None = None

    if "objective" in yml_data:
        raw_obj = yml_data["objective"]
        if isinstance(raw_obj, dict):
            objective = {
                "name": str(raw_obj.get("name", "eui")),
                "direction": str(raw_obj.get("direction", "minimize")),
                "weight": float(raw_obj.get("weight", 1.0)),
                "target": float(raw_obj["target"]) if "target" in raw_obj else None,
                "scaling_factor": float(raw_obj["scaling_factor"])
                if "scaling_factor" in raw_obj
                else None,
            }
            log.info(
                "objective config: name=%s, direction=%s, weight=%.2f, target=%s, scaling_factor=%s",
                objective["name"],
                objective["direction"],
                objective["weight"],
                objective["target"],
                objective["scaling_factor"],
            )
        else:
            log.warning(
                "objective in variables.yml is not a dict (got %s); ignoring and using default (None)",
                type(raw_obj).__name__,
            )

    if "constraints" in yml_data:
        raw_constraints = yml_data["constraints"]
        if isinstance(raw_constraints, list):
            constraints = []
            for c in raw_constraints:
                if isinstance(c, dict):
                    entry: dict[str, object] = {
                        "name": str(c.get("name", "")),
                        "max": float(c["max"]) if "max" in c else float("inf"),
                    }
                    if "min" in c:
                        entry["min"] = float(c["min"])
                    constraints.append(entry)
            log.info("constraints config: %d constraint(s)", len(constraints))
        else:
            log.warning(
                "constraints in variables.yml is not a list (got %s); ignoring and using default (None)",
                type(raw_constraints).__name__,
            )

    return objective, constraints


def _parse_baseline(variables_yml: Path) -> dict[str, object] | None:
    """Parse the optional ``baseline`` section from variables.yml (issue #64)."""
    try:
        with variables_yml.open() as f:
            yml_data = yaml.safe_load(f)
        if isinstance(yml_data, dict) and "baseline" in yml_data:
            raw_baseline = yml_data["baseline"]
            if isinstance(raw_baseline, dict):
                baseline: dict[str, Any] = {
                    "sample_id": str(raw_baseline.get("sample_id", "baseline")),
                    "parameters": dict(raw_baseline.get("parameters", {})),
                }
                log.info(
                    "baseline comparison enabled: sample_id=%s, %d parameters",
                    baseline["sample_id"],
                    len(baseline["parameters"]),
                )
                return baseline
            else:
                log.warning(
                    "baseline section in %s is not a dict (got %s); ignoring and using default (None)",
                    variables_yml,
                    type(raw_baseline).__name__,
                )
    except Exception as exc:
        log.warning("could not parse baseline section from %s: %s", variables_yml, exc)
    return None


def _validate_script_path(raw: object) -> None:
    if not raw:
        return
    validate_path_within(
        Path(str(raw)),
        Path("/"),
        must_exist=True,
        must_be_file=True,
        readable=True,
    )


def _parse_chaos_scenarios(raw: object) -> list[str]:
    """Parse ``--chaos-scenarios`` into a clean list of strings.

    Issue #1013. Accepts three shapes from ``argparse``:

    * a comma-separated string ``"kill_switch,network_delay"``
    * an already-parsed list/tuple (for programmatic config builders)
    * ``None`` / empty (returns ``[]`` — caller decides the default)

    Whitespace is stripped and empty entries are dropped so a trailing
    comma does not produce a phantom scenario name.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()]
    text = str(raw)
    return [part.strip() for part in text.split(",") if part.strip()]


def load_config(args: dict[str, object]) -> CampaignConfig:  # noqa: PLR0912
    """Resolve a config from a flat dict (e.g. argparse namespace -> vars).

    Each value is narrowed via ``str()`` / ``bool()`` / ``int()`` because
    argparse already validates types — the cast here is purely a
    type-checker shim, not a runtime guarantee.

    Raises
    ------
    ValidationError
        When user-supplied inputs fail validation (path traversal,
        missing required files, malformed variables.yml, etc.).
    FileNotFoundError
        When required input files do not exist.
    """
    variables_yml = Path(str(args["input_variables"])).resolve()
    template = Path(str(args["template_sim_package"])).resolve()
    if not variables_yml.exists():
        raise FileNotFoundError(f"variables_yml not found: {variables_yml}")
    if not template.exists():
        raise FileNotFoundError(f"template_sim_package not found: {template}")

    # --- Input validation (issue #278) ---

    # 0. Type coercion (issue #409): normalise string-typed numeric
    #    parameters (e.g. ``min: "1.0"``) before schema validation so
    #    the downstream checks see correctly typed values.
    _coerce_variables_yml_file(variables_yml)

    # 1. variables.yml schema validation.
    try:
        validate_variables_yml(variables_yml)
    except ValidationError:
        raise

    # 2. Template package structure validation.
    try:
        validate_template_package(template)
    except ValidationError:
        raise

    # 3. Path traversal protection: ensure user-supplied paths don't
    #    escape sensible bounds.  We don't restrict to a single base dir
    #    (the user may legitimately point to /data/models etc.), but we
    #    verify that symlink targets resolve to real paths and reject
    #    null bytes / absurdly long paths.
    outdir = Path(str(args["outdir"])).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    # Validate custom scripts don't escape via symlinks.
    custom_apply = args.get("custom_apply_script")
    custom_kpi = args.get("custom_kpi_extractor")
    _validate_script_path(custom_apply)
    _validate_script_path(custom_kpi)

    # Validate init/finalize scripts.
    init_script = args.get("init_script")
    finalize_script = args.get("finalize_script")
    _validate_script_path(init_script)
    _validate_script_path(finalize_script)

    # 4. Numeric range validation.
    n_samples = int(str(args["n_samples"]))
    if n_samples < 1:
        raise ValidationError(
            f"N_samples must be >= 1, got {n_samples}",
            field="n_samples",
        )
    max_generations = int(str(args.get("max_generations", 1)))
    if max_generations < 1:
        raise ValidationError(
            f"Max generations must be >= 1, got {max_generations}",
            field="max_generations",
        )
    nomad_fanout_submit_chunk_size = int(str(args.get("nomad_fanout_submit_chunk_size", 0)))
    if nomad_fanout_submit_chunk_size < 0:
        raise ValidationError(
            "Nomad fanout submit chunk size must be >= 0",
            field="nomad_fanout_submit_chunk_size",
        )
    shard_count_raw = args.get("shard_count")
    shard_index_raw = args.get("shard_index")
    shard_start_raw = args.get("shard_start")
    shard_end_raw = args.get("shard_end")
    shard_count = int(str(shard_count_raw)) if shard_count_raw is not None else None
    shard_index = int(str(shard_index_raw)) if shard_index_raw is not None else None
    shard_start = int(str(shard_start_raw)) if shard_start_raw is not None else None
    shard_end = int(str(shard_end_raw)) if shard_end_raw is not None else None
    partition_mode = shard_count is not None or shard_index is not None
    range_mode = shard_start is not None or shard_end is not None
    if partition_mode and range_mode:
        raise ValidationError(
            "Shard count/shard index cannot be combined with shard start/shard end",
            field="shard",
        )
    if partition_mode:
        if shard_count is None or shard_index is None:
            raise ValidationError(
                "Both shard_count and shard_index are required for partition sharding",
                field="shard",
            )
        if shard_count < 1:
            raise ValidationError("Shard count must be >= 1", field="shard_count")
        if shard_index < 0 or shard_index >= shard_count:
            raise ValidationError(
                f"Shard index must be in [0, {shard_count - 1}]",
                field="shard_index",
            )
    if range_mode:
        if shard_start is None or shard_end is None:
            raise ValidationError(
                "Both shard_start and shard_end are required for range sharding",
                field="shard",
            )
        if shard_start < 0:
            raise ValidationError("Shard start must be >= 0", field="shard_start")
        if shard_end <= shard_start:
            raise ValidationError("Shard end must be greater than shard start", field="shard_end")

    openstudio_version = str(args["openstudio_version"])
    if not openstudio_version or not openstudio_version[0].isdigit():
        raise ValidationError(
            f"OpenStudio version must start with a digit, got {openstudio_version!r}",
            field="openstudio_version",
        )

    # Parse the optional baseline section from variables.yml (issue #64).
    baseline = _parse_baseline(variables_yml)

    # Parse objective and constraints sections (issue #282).
    try:
        with variables_yml.open() as f:
            yml_data = yaml.safe_load(f)
        objective, constraints = _parse_objective_and_constraints(
            yml_data if isinstance(yml_data, dict) else {}
        )
    except Exception as exc:
        log.warning("could not parse objective/constraints from %s: %s", variables_yml, exc)
        objective, constraints = None, None

    _chaos_intensity = float(str(args.get("chaos_intensity", 0.5)))
    if not (0.0 <= _chaos_intensity <= 1.0):
        raise ValidationError(
            f"chaos_intensity must be between 0.0 and 1.0 (got {_chaos_intensity})",
            field="chaos_intensity",
        )

    _chaos_delay_s = float(str(args.get("chaos_delay_s", 0.1)))
    _chaos_jitter_s = float(str(args.get("chaos_jitter_s", 0.05)))
    if _chaos_jitter_s < 0:
        raise ValidationError(
            f"chaos_jitter_s must be >= 0 (got {_chaos_jitter_s})",
            field="chaos_jitter_s",
        )
    if _chaos_jitter_s > _chaos_delay_s:
        raise ValidationError(
            f"chaos_jitter_s ({_chaos_jitter_s}) cannot exceed chaos_delay_s ({_chaos_delay_s})",
            field="chaos_jitter_s",
        )

    return CampaignConfig(
        input_variables=variables_yml,
        template_sim_package=template,
        n_samples=int(str(args["n_samples"])),
        outdir=outdir,
        openstudio_version=str(args["openstudio_version"]),
        container_digest=(str(args["container_digest"]) if args.get("container_digest") else None),
        project=str(args.get("project", "")),
        archive_intermediates=bool(args.get("archive_intermediates", False)),
        custom_apply_script=Path(str(custom_apply)).resolve() if custom_apply else None,
        custom_kpi_extractor=Path(str(custom_kpi)).resolve() if custom_kpi else None,
        kpis=cast(list[str], args["kpis"]) if args.get("kpis") else None,
        mlflow_tracking_uri=(
            str(args["mlflow_tracking_uri"]) if args.get("mlflow_tracking_uri") else None
        ),
        slurm_qos=str(args["slurm_qos"]) if args.get("slurm_qos") else None,
        slurm_constraint=(str(args["slurm_constraint"]) if args.get("slurm_constraint") else None),
        slurm_gres=str(args["slurm_gres"]) if args.get("slurm_gres") else None,
        baseline=baseline,
        weather_dir=str(args["weather_dir"]) if args.get("weather_dir") else "weather",
        dry_run=bool(args.get("dry_run", False)),
        sample=int(str(args["sample"])) if args.get("sample") is not None else None,
        algorithm=str(args["algorithm"]) if args.get("algorithm") else "lhs",
        init_script=(Path(str(args["init_script"])).resolve() if args.get("init_script") else None),
        finalize_script=(
            Path(str(args["finalize_script"])).resolve() if args.get("finalize_script") else None
        ),
        skip_preflight=bool(args.get("skip_preflight", False)),
        max_generations=int(str(args.get("max_generations", 1))),
        aws_batch_max_spot_price_usd=(
            float(str(args["aws_batch_max_spot_price_usd"]))
            if args.get("aws_batch_max_spot_price_usd") is not None
            else None
        ),
        aws_batch_fallback_to_on_demand=bool(args.get("aws_batch_fallback_to_on_demand", False)),
        aws_batch_max_retries=int(str(args.get("aws_batch_max_retries", 3))),
        aws_batch_submit_rps=(
            float(str(args["aws_batch_submit_rps"]))
            if args.get("aws_batch_submit_rps") is not None
            else None
        ),
        ecr_repository=str(args["ecr_repository"]) if args.get("ecr_repository") else None,
        azure_batch_account_name=(
            str(args["azure_batch_account_name"]) if args.get("azure_batch_account_name") else None
        ),
        azure_batch_account_url=(
            str(args["azure_batch_account_url"]) if args.get("azure_batch_account_url") else None
        ),
        azure_batch_pool_id=str(args.get("azure_batch_pool_id", "osimflow-pool")),
        azure_batch_location=str(args.get("azure_batch_location", "eastus")),
        azure_use_spot=bool(args.get("azure_use_spot", False)),
        azure_fallback_to_on_demand=bool(args.get("azure_fallback_to_on_demand", False)),
        azure_max_retries=int(str(args.get("azure_max_retries", 3))),
        google_batch_project_id=(
            str(args["google_batch_project_id"]) if args.get("google_batch_project_id") else None
        ),
        google_batch_region=str(args.get("google_batch_region", "us-central1")),
        google_batch_service_account=(
            str(args["google_batch_service_account"])
            if args.get("google_batch_service_account")
            else None
        ),
        google_use_spot=bool(args.get("google_use_spot", False)),
        google_fallback_to_on_demand=bool(args.get("google_fallback_to_on_demand", False)),
        google_max_retries=int(str(args.get("google_max_retries", 3))),
        byos_trust_level=(
            ByosTrustLevel(str(args["byos_trust_level"]))
            if args.get("byos_trust_level")
            else ByosTrustLevel.SUBPROCESS
        ),
        require_trusted_scripts=bool(args.get("require_trusted_scripts", True)),
        observability=str(args.get("observability", "none")),
        cloudwatch_namespace=str(args.get("cloudwatch_namespace", "OSimFlow")),
        cloudwatch_log_group=(
            str(args["cloudwatch_log_group"]) if args.get("cloudwatch_log_group") else None
        ),
        log_aggregation_url=(
            str(args["log_aggregation_url"]) if args.get("log_aggregation_url") else None
        ),
        prometheus_port=int(str(args.get("prometheus_port", 9090))),
        otel_endpoint=str(args["otel_endpoint"]) if args.get("otel_endpoint") else None,
        registry_path=(Path(str(args["registry"])).resolve() if args.get("registry") else None),
        objective=objective,
        constraints=constraints,
        nsga2_reference_points=(
            str(args["nsga2_reference_points"]) if args.get("nsga2_reference_points") else None
        ),
        nsga2_reference_directions=(
            str(args["nsga2_reference_directions"])
            if args.get("nsga2_reference_directions")
            else None
        ),
        max_sample_retries=int(str(args.get("max_sample_retries", 3))),
        offline=bool(args.get("offline", False)),
        offline_bundle=(
            Path(str(args["offline_bundle"])).resolve() if args.get("offline_bundle") else None
        ),
        webhook_url=str(args["webhook_url"]) if args.get("webhook_url") else None,
        nomad_dispatch_policy=str(args.get("nomad_dispatch_policy", "keep_manual")),
        nomad_allocation_resolution_timeout_s=float(
            str(args.get("nomad_allocation_resolution_timeout_s", 30.0))
        ),
        nomad_poll_interval_s=float(str(args.get("nomad_poll_interval_s", 5.0))),
        nomad_max_poll_interval_s=float(str(args.get("nomad_max_poll_interval_s", 60.0))),
        nomad_fanout_submit_rate_per_sec=(
            float(str(args["nomad_fanout_submit_rate_per_sec"]))
            if args.get("nomad_fanout_submit_rate_per_sec") is not None
            else None
        ),
        nomad_fanout_submit_chunk_size=nomad_fanout_submit_chunk_size,
        shard_count=shard_count,
        shard_index=shard_index,
        shard_start=shard_start,
        shard_end=shard_end,
        nomad_tls=bool(args.get("nomad_tls", False)),
        nomad_cert=(Path(str(args["nomad_cert"])).resolve() if args.get("nomad_cert") else None),
        nomad_key=(Path(str(args["nomad_key"])).resolve() if args.get("nomad_key") else None),
        nomad_ca_cert=(
            Path(str(args["nomad_ca_cert"])).resolve() if args.get("nomad_ca_cert") else None
        ),
        # Kubernetes native Job controls (issue #997). Defaults preserve
        # the pre-#997 manifest byte-for-byte: backoff_limit=0, no TTL,
        # no extra labels. ``kubernetes_backoff_limit`` of 0 is the
        # documented Kubernetes "no retries" value.
        kubernetes_backoff_limit=int(str(args.get("kubernetes_backoff_limit", 0))),
        kubernetes_ttl_seconds_after_finished=(
            int(str(args["kubernetes_ttl_seconds_after_finished"]))
            if args.get("kubernetes_ttl_seconds_after_finished") is not None
            else None
        ),
        kubernetes_queue_name=(
            str(args["kubernetes_queue_name"]) if args.get("kubernetes_queue_name") else None
        ),
        byos_resource_limits=(
            args["byos_resource_limits"] if args.get("byos_resource_limits") else None  # type: ignore[arg-type]
        ),
        byos_timeout_s=float(str(args.get("byos_timeout_s", 600.0))),
        result_storage_backend=str(args.get("result_storage_backend", "local")),
        result_storage_bucket=str(args.get("result_storage_bucket", "")),
        result_storage_endpoint=(
            str(args["result_storage_endpoint"]) if args.get("result_storage_endpoint") else None
        ),
        task_queue=str(args.get("task_queue", "none")),
        dask_scheduler_address=(
            str(args["dask_scheduler_address"]) if args.get("dask_scheduler_address") else None
        ),
        # Redis URL for distributed campaign state (issue #993 / T8.2).
        # When set, the campaign's shared cache entries are coordinated
        # through Redis; when None (default) the single-node SQLiteCache
        # is used unchanged. Precedence (per docs/distributed-cache.md):
        # CLI flag > OSIMFLOW_REDIS_URL env var > None.
        redis_url=(
            str(args["redis_url"])
            if args.get("redis_url")
            else (os.environ.get("OSIMFLOW_REDIS_URL") or None)
        ),
        resource_quota=_parse_resource_quota(args.get("resource_quota")),
        enable_cost_tracking=bool(args.get("enable_cost_tracking", False)),
        cost_on_demand_price=float(str(args["cost_on_demand_price"]))
        if args.get("cost_on_demand_price") is not None
        else 0.05,
        cost_spot_price=float(str(args["cost_spot_price"]))
        if args.get("cost_spot_price") is not None
        else 0.03,
        alert_rules=(Path(str(args["alert_rules"])).resolve() if args.get("alert_rules") else None),
        alert_destinations=(
            Path(str(args["alert_destinations"])).resolve()
            if args.get("alert_destinations")
            else None
        ),
        max_step_retries=int(str(args.get("max_step_retries", 2))),
        uq_method=str(args.get("uq_method", "latin_hypercube")),
        uq_n_samples=int(str(args["uq_n_samples"]))
        if args.get("uq_n_samples") is not None
        else None,
        uq_failure_thresholds=(
            cast(list[str], args["uq_failure_thresholds"])
            if args.get("uq_failure_thresholds")
            else None
        ),
        nsga2_ref_points=str(args["nsga2_ref_points"]) if args.get("nsga2_ref_points") else None,
        nsga2_ref_dirs_strategy=str(args["nsga2_ref_dirs_strategy"])
        if args.get("nsga2_ref_dirs_strategy")
        else None,
        bcl_api_key=str(args["bcl_api_key"]) if args.get("bcl_api_key") else None,
        validate_measures=bool(args.get("validate_measures", False)),
        s3_artifact_bucket=str(args.get("s3_artifact_bucket", "")),
        s3_artifact_prefix=str(args.get("s3_artifact_prefix", "")),
        s3_artifact_region=str(args["s3_artifact_region"])
        if args.get("s3_artifact_region")
        else None,
        s3_artifact_endpoint=str(args["s3_artifact_endpoint"])
        if args.get("s3_artifact_endpoint")
        else None,
        s3_artifact_presigned_url_expiration=int(str(args["s3_artifact_presigned_url_expiration"]))
        if args.get("s3_artifact_presigned_url_expiration") is not None
        else 3600,
        # Chaos testing settings (issue #1013). All off by default; the
        # ``chaos`` composed config is built from these flat fields in
        # ``__post_init__``. ``chaos_scenarios`` is parsed from a
        # comma-separated string so the CLI can stay close to the
        # existing ``--algo-args foo,bar,baz`` convention. When
        # ``--chaos-enabled`` is set but no scenarios are listed,
        # default to ``["kill_switch"]`` so a typo cannot leave the
        # engine enabled-but-empty.
        chaos_enabled=bool(args.get("chaos_enabled", False)),
        chaos_scenarios=_parse_chaos_scenarios(args.get("chaos_scenarios")),
        chaos_schedule=str(args.get("chaos_schedule", "none")),
        chaos_probability=float(str(args.get("chaos_probability", 1.0))),
        chaos_delay_s=_chaos_delay_s,
        chaos_jitter_s=_chaos_jitter_s,
        chaos_duration_s=float(str(args.get("chaos_duration_s", 0.5))),
        chaos_intensity=_chaos_intensity,
        chaos_size_mb=int(str(args.get("chaos_size_mb", 64))),
        chaos_fail_after=int(str(args.get("chaos_fail_after", 2))),
    )
