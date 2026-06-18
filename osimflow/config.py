"""Campaign configuration loader.

A thin wrapper around the `variables.yml` schema, plus the CLI flags
the PRD §1.4 calls out as required (`--input_variables`,
`--template_sim_package`, `--n_samples`, `--outdir`,
`--openstudio_version`, `--archive_intermediates`).
"""

import dataclasses
import json
import logging
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
            "max_samples must be >= 1",
            field="resource_quota.max_samples",
        )
    if quota.max_cost_usd is not None and quota.max_cost_usd < 0:
        raise ValidationError(
            "max_cost_usd must be >= 0",
            field="resource_quota.max_cost_usd",
        )
    if quota.max_wall_time_min is not None and quota.max_wall_time_min <= 0:
        raise ValidationError(
            "max_wall_time_min must be > 0",
            field="resource_quota.max_wall_time_min",
        )
    if quota.max_concurrent_samples is not None and quota.max_concurrent_samples < 1:
        raise ValidationError(
            "max_concurrent_samples must be >= 1",
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
        f"{key} must be an integer, got {type(val).__name__}",
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
        f"{key} must be a number, got {type(val).__name__}",
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


@dataclasses.dataclass
class CampaignConfig:
    input_variables: Path
    template_sim_package: Path
    n_samples: int
    outdir: Path
    openstudio_version: str
    # Project hierarchy support (issue #390). Groups campaigns under a named
    # project, enabling multi-campaign organization. When set, the campaign
    # is associated with the given project name in the registry.
    project: str = ""
    archive_intermediates: bool = False
    custom_apply_script: Path | None = None
    custom_kpi_extractor: Path | None = None
    # Optional MLflow tracking server (issue #7). When None, the campaign
    # runs without any MLflow integration (no mlflow import, no logging).
    # When set, the Campaign begins an MLflow run at start and ends it at
    # completion, logging params / metrics / artifacts.
    mlflow_tracking_uri: str | None = None
    # Optional Redis URL for distributed cache invalidation (issue #330).
    # When None, a single-node SQLiteCache is used. When set (e.g.,
    # "redis://localhost:6379/0"), a DistributedCache is used that
    # broadcasts invalidation events across all campaign workers via
    # Redis pub/sub, enabling coherent cache state on multi-node
    # Slurm/AWS Batch campaigns.
    redis_url: str | None = None
    # Optional Slurm advanced directives (issue #4). Forwarded to
    # `SlurmExecutor` when `--executor slurm` is selected. All default
    # to `None`; submitit omits unset directives from the sbatch header.
    slurm_qos: str | None = None
    slurm_constraint: str | None = None
    slurm_gres: str | None = None
    # Optional ASHRAE 90.1 baseline comparison mode (issue #64).
    # When a `baseline` section is defined in variables.yml, the
    # campaign injects a fixed-parameter baseline sample alongside
    # the LHS parametric samples, computes percentage improvement
    # for each KPI, and adds baseline reference data to outputs.
    # The dict has keys: "sample_id" (str) and "parameters" (dict).
    # When None, baseline comparison is disabled and behaviour is
    # unchanged.
    baseline: dict[str, object] | None = None
    # Weather file subdirectory convention (issue #63).
    # Name of the subdirectory inside template_sim_package that holds
    # .epw weather files. Defaults to "weather". The pre-flight
    # validation pass checks that all .epw files referenced in
    # variables.yml exist under this directory and validates their
    # EPW format (header line starts with "LOCATION").
    weather_dir: str = "weather"
    # Dry-run mode (issue #59): run exactly 1 sample locally to validate
    # setup before committing to a full campaign. Forces n_samples=1 and
    # LocalExecutor regardless of CLI flags. Runs steps 1-4 only (no
    # aggregation or plots) and prints a summary.
    dry_run: bool = False
    # Single-sample mode (issue #59): run only the sample at 0-based index
    # N through steps 2-4. Skips GENERATE_LHS_SAMPLES (reuses existing
    # samples.json). Useful for debugging a specific failed sample.
    sample: int | None = None
    # Sampling algorithm name (issue #121). Dispatched through
    # AlgorithmRegistry.get(). Defaults to "lhs" for backward
    # compatibility. Future options: "sobol", "morris", etc.
    algorithm: str = "lhs"
    # Pre/post campaign shell hooks (issue #108).
    # --init-script: runs before the first campaign step. Must exit 0
    # or the campaign aborts. Useful for S3 sync, Slack notifications.
    init_script: Path | None = None
    # --finalize-script: runs after the last campaign step. Best-effort:
    # a non-zero exit code is logged but does NOT fail the campaign.
    # Useful for cleanup, upload, notification, DynamoDB writes.
    finalize_script: Path | None = None
    # Skip preflight model run (issue #107). When True, the
    # PREFLIGHT_RUN_MODEL step is skipped, allowing the campaign to
    # proceed without validating the seed model first. Useful when the
    # model is known-good or when iterating on downstream steps.
    skip_preflight: bool = False
    # Generation loop (issue #122). Iterative/optimization algorithms
    # (NSGA-II, Bayesian optimisation) loop the fan-out steps multiple
    # times.  Default 1 = single-generation (backward compatible with
    # LHS).  Set >1 to run the fan-out DAG for that many generations.
    max_generations: int = 1
    # Spot instance retry + price ceiling for AWS Batch (issue #131).
    # When aws_batch_max_spot_price_usd is set, the executor queries the
    # current Spot price before submitting and rejects jobs that would
    # exceed the ceiling (unless fallback to on-demand is enabled).
    # aws_batch_fallback_to_on_demand switches to the on-demand queue
    # after spot price rejection or max retries exhausted.
    # aws_batch_max_retries controls how many times a spot-interrupted
    # job is retried before falling back or failing.
    aws_batch_max_spot_price_usd: float | None = None
    aws_batch_fallback_to_on_demand: bool = False
    aws_batch_max_retries: int = 3
    ecr_repository: str | None = (
        None  # e.g. "123456.dkr.ecr.us-east-1.amazonaws.com/osimflow/openstudio"
    )
    # Azure Batch configuration (issue #254, issue #352).
    azure_batch_account_name: str | None = None
    azure_batch_account_url: str | None = None
    azure_batch_pool_id: str = "osimflow-pool"
    azure_batch_location: str = "eastus"
    azure_use_spot: bool = False
    azure_fallback_to_on_demand: bool = False
    azure_max_retries: int = 3
    # Google Cloud Batch configuration (issue #254, issue #352).
    google_batch_project_id: str | None = None
    google_batch_region: str = "us-central1"
    google_batch_service_account: str | None = None
    google_use_spot: bool = False
    google_fallback_to_on_demand: bool = False
    google_max_retries: int = 3
    # BYOS trust level (issue #269). Controls how user-supplied scripts
    # are executed. Default is SUBPROCESS (isolated child process) for
    # security. INPROCESS (legacy) loads the script directly into the
    # orchestrator process — use only when the user explicitly trusts
    # the script.
    byos_trust_level: ByosTrustLevel = ByosTrustLevel.SUBPROCESS
    # Observability backend selection (issue #132 / G20c).
    # When "none" (default), NullBackend is used — zero overhead.
    # Supported: "none", "cloudwatch", "prometheus", "opentelemetry".
    observability: str = "none"
    # CloudWatch backend options (issue #132).
    cloudwatch_namespace: str = "OSimFlow"
    cloudwatch_log_group: str | None = None
    log_aggregation_url: str | None = None
    # Prometheus backend options (issue #132).
    prometheus_port: int = 9090
    # OpenTelemetry backend options (issue #132).
    otel_endpoint: str | None = None
    # Campaign registry path (issue #266). When None, the default
    # ~/.osimflow/registry.db is used. Override via --registry flag
    # or OSIMFLOW_REGISTRY env var.
    registry_path: Path | None = None
    # Objective function configuration (issue #282).
    # objective: {name: <kpi_name>, direction: minimize|maximize, weight: <float>}
    # For single-objective optimizers (DE, DA). Multi-objective algorithms
    # (NSGA-II, PSO, SPEA2) read weights from the variables dict directly.
    objective: dict[str, object] | None = None
    # Constraint definitions (issue #282).
    # List of {name: <kpi_name>, max: <float>, min: <float>|None}.
    # During objective evaluation, constraint violations are penalised with
    # a large positive value (1e9) added to the objective.
    constraints: list[dict[str, object]] | None = None
    # R-NSGA-II reference points (issue #529). Comma-separated fractions
    # representing aspiration points on the Pareto front, e.g.,
    # "0.25,0.5,0.75" for 2 objectives. Only used with --algorithm nsga2.
    nsga2_reference_points: str | None = None
    # R-NSGA-II reference direction strategy (issue #529). Supported:
    # das-dennis, energy, wedge, incremental. Only used with --algorithm nsga2.
    nsga2_reference_directions: str | None = None
    # Per-sample retry configuration (issue #252).
    # max_sample_retries: maximum retry attempts for transient per-sample
    # failures (network timeout, resource contention, etc.). A value of 0
    # disables retries. Each retry uses exponential backoff starting at
    # base_delay seconds (default 1.0), doubling each attempt up to 60s cap.
    max_sample_retries: int = 3
    # Worker auto-recovery (issue #443). When True and a job fails, OSimFlow
    # checks if the worker's heartbeat is stale (no update for 60+ seconds).
    # If stale, the job is automatically resubmitted (up to max_sample_retries).
    # This handles worker crashes without manual intervention. When False,
    # failed jobs are marked as failed without auto-recovery.
    worker_auto_recovery: bool = True
    # Air-gapped / offline mode (issue #261). When True, OSimFlow skips
    # Docker Hub pulls, PyPI version checks, and online weather downloads.
    # It reads pip wheels from --offline_bundle/pip/ and uses pre-loaded
    # Docker images from the local registry.
    offline: bool = False
    # Path to the offline bundle directory (issue #261). Contains pip/,
    # docker/, and weather/ subdirectories created by
    # scripts/bundle_offline.py. When set alongside --offline, the campaign
    # uses this path instead of reaching out to the internet.
    offline_bundle: Path | None = None
    # Webhook URL for campaign completion callbacks (issue #283).
    # When set, OSimFlow POSTs a JSON summary to this URL after the
    # GENERATE_BASIC_PLOTS step completes. Best-effort: delivery failures
    # are logged but do not affect campaign status.
    webhook_url: str | None = None
    # Nomad TLS configuration (issue #344). When nomad_tls is True, the
    # NomadExecutor uses HTTPS to connect to the Nomad cluster. The
    # nomad_cert, nomad_key, and nomad_ca_cert fields specify client
    # certificate, key, and CA certificate paths for mTLS authentication.
    nomad_tls: bool = False
    nomad_cert: Path | None = None
    nomad_key: Path | None = None
    nomad_ca_cert: Path | None = None
    # BYOS resource limits (issue #343). A dict mapping rlimit names to
    # integer values (e.g. {"RLIMIT_CPU": 300, "RLIMIT_AS": 4294967296}).
    # Applied via resource.setrlimit before the BYOS subprocess is
    # spawned. resource.error from impossible limits is caught and
    # logged as a warning (non-fatal).
    byos_resource_limits: dict[str, int] | None = None
    # Result storage backend (issue #339). When set to a non-local value
    # (s3/gs/azure), simulation outputs (eplusout.sql) and KPI JSONs are
    # uploaded to the configured bucket after each successful step.
    result_storage_backend: str = "local"
    # Bucket/container name for result storage (issue #339).
    # For S3: the bucket name. For GCS: the bucket name. For Azure: the container.
    # Ignored when result_storage_backend is "local".
    result_storage_bucket: str = ""
    # S3-compatible endpoint URL for result storage (issue #339).
    # Used for MinIO, Cloudflare R2, and other S3-compatible stores.
    result_storage_endpoint: str | None = None
    # Distributed task queue backend (issue #335). When "none" (default),
    # fan-out steps submit directly to the configured executor. When
    # "dask", work is submitted to a Dask scheduler via DaskTaskQueue.
    task_queue: str = "none"
    # Dask scheduler address (issue #335). E.g. "tcp://scheduler:8786".
    # When None and task_queue="dask", an embedded LocalCluster is used.
    dask_scheduler_address: str | None = None
    # Resource quota limits for the campaign (issue #446).
    # When None, no quota enforcement is applied.
    # When set, the campaign fails fast at start and/or skips further
    # sample submissions when the quota is exhausted.
    resource_quota: ResourceQuota | None = None
    # Cost tracking configuration (issue #447). When True, the campaign
    # tracks estimated and actual costs for cloud/HPC resources and writes
    # a cost summary to run.json. When False (default), cost tracking
    # is disabled for backward compatibility.
    enable_cost_tracking: bool = False
    # On-demand price per vCPU-hour for cost estimation. Used when cloud
    # provider APIs are unavailable. Default $0.05/vCPU·h.
    cost_on_demand_price: float = 0.05
    # Spot price per vCPU-hour for cost estimation. Default $0.03/vCPU·h
    # (40% savings vs on-demand).
    cost_spot_price: float = 0.03
    # Slurm cost per node-hour for cost estimation. Default $0.0 (free).
    slurm_cost_per_node_hour: float = 0.0
    # Alert rules YAML file path (issue #438). When set, custom alert rules
    # are loaded from this file in addition to the built-in rules.
    alert_rules: Path | None = None
    # Alert destinations YAML file path (issue #438). When set, alert
    # destinations are loaded from this file.
    alert_destinations: Path | None = None
    # Cross-step retry configuration (issue #416). When a fan-out step
    # (APPLY_PARAMETERS, RUN_OPENSTUDIO_SIM, EXTRACT_KPIS) fails with a
    # transient error, retry that specific step up to max_step_retries
    # times before aborting the campaign. A value of 0 disables retries.
    # Only transient errors trigger retry; permanent errors (invalid input,
    # missing files) abort immediately.
    max_step_retries: int = 2
    # UQ (Uncertainty Quantification) configuration (issue #530).
    # uq_method: sampling method for UQ analysis. Default 'latin_hypercube'.
    uq_method: str = "latin_hypercube"
    # uq_n_samples: number of Monte Carlo samples for UQ analysis.
    # When None, defaults to n_samples.
    uq_n_samples: int | None = None
    # uq_failure_thresholds: list of 'kpi=threshold' strings defining
    # failure thresholds for probability of failure (POF) computation.
    # Example: ['eui=150', 'cooling=5000']
    uq_failure_thresholds: list[str] | None = None
    # BCL API key for the NREL Building Component Library (issue #580).
    # Can also be set via the BCL_API_KEY env var.
    # Some BCL API endpoints require authentication.
    bcl_api_key: str | None = None
    # Validate measure arguments against BCL taxonomy when discovering
    # measures from BCL (issue #580). When True, warnings are logged
    # for measures with incomplete metadata or unexpected argument types.
    validate_measures: bool = False
    # S3 artifact storage configuration (issue #601). When set, base
    # simulation assets (.osm, .epw) are uploaded to S3 once at campaign
    # creation and pre-signed URLs are generated for remote executor
    # nodes to download directly, eliminating the local-machine bottleneck.
    s3_artifact_bucket: str = ""
    # S3 artifact storage prefix within the bucket (issue #601).
    s3_artifact_prefix: str = ""
    # AWS region for S3 artifact storage (issue #601). When None,
    # uses the region from the IAM role or default credential chain.
    s3_artifact_region: str | None = None
    # Custom S3-compatible endpoint URL for artifact storage (issue #601).
    # Used for MinIO, Cloudflare R2, and other S3-compatible stores.
    s3_artifact_endpoint: str | None = None
    # Expiration time in seconds for pre-signed URLs (issue #601).
    # Default 3600 (1 hour). Remote nodes must download artifacts
    # within this window.
    s3_artifact_presigned_url_expiration: int = 3600

    # R-NSGA-II reference points (issue #529). Comma-separated fractions
    # along the Pareto front for 2-objective problems, or explicit reference
    # point coordinates for higher dimensions. When set, the NSGA2Algorithm
    # uses pymoo's RNSGA2 with these reference points instead of standard
    # crowding distance. Example: "0.25,0.5,0.75" for three reference points.
    nsga2_ref_points: str | None = None
    # R-NSGA-II reference direction generation strategy (issue #529).
    # When set alongside nsga2_ref_points, the specified reference direction
    # strategy is used to generate reference points. Supported values:
    # "das_dennis" (Das-Dennis decomposition), "wedge" (wedge pattern),
    # "adaptive" (adaptive update during evolution).
    nsga2_ref_dirs_strategy: str | None = None

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


def load_config(args: dict[str, object]) -> CampaignConfig:
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
            f"n_samples must be >= 1, got {n_samples}",
            field="n_samples",
        )
    max_generations = int(str(args.get("max_generations", 1)))
    if max_generations < 1:
        raise ValidationError(
            f"max_generations must be >= 1, got {max_generations}",
            field="max_generations",
        )

    openstudio_version = str(args["openstudio_version"])
    if not openstudio_version or not openstudio_version[0].isdigit():
        raise ValidationError(
            f"openstudio_version must start with a digit, got {openstudio_version!r}",
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

    return CampaignConfig(
        input_variables=variables_yml,
        template_sim_package=template,
        n_samples=int(str(args["n_samples"])),
        outdir=outdir,
        openstudio_version=str(args["openstudio_version"]),
        project=str(args.get("project", "")),
        archive_intermediates=bool(args.get("archive_intermediates", False)),
        custom_apply_script=Path(str(custom_apply)).resolve() if custom_apply else None,
        custom_kpi_extractor=Path(str(custom_kpi)).resolve() if custom_kpi else None,
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
        nomad_tls=bool(args.get("nomad_tls", False)),
        nomad_cert=(Path(str(args["nomad_cert"])).resolve() if args.get("nomad_cert") else None),
        nomad_key=(Path(str(args["nomad_key"])).resolve() if args.get("nomad_key") else None),
        nomad_ca_cert=(
            Path(str(args["nomad_ca_cert"])).resolve() if args.get("nomad_ca_cert") else None
        ),
        byos_resource_limits=(
            args["byos_resource_limits"] if args.get("byos_resource_limits") else None  # type: ignore[arg-type]
        ),
        result_storage_backend=str(args.get("result_storage_backend", "local")),
        result_storage_bucket=str(args.get("result_storage_bucket", "")),
        result_storage_endpoint=(
            str(args["result_storage_endpoint"]) if args.get("result_storage_endpoint") else None
        ),
        task_queue=str(args.get("task_queue", "none")),
        dask_scheduler_address=(
            str(args["dask_scheduler_address"]) if args.get("dask_scheduler_address") else None
        ),
        resource_quota=_parse_resource_quota(args.get("resource_quota")),
        enable_cost_tracking=bool(args.get("enable_cost_tracking", False)),
        cost_on_demand_price=float(str(args.get("cost_on_demand_price", 0.05))),
        cost_spot_price=float(str(args.get("cost_spot_price", 0.03))),
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
    )
