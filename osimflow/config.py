"""Campaign configuration loader.

A thin wrapper around the `variables.yml` schema, plus the CLI flags
the PRD §1.4 calls out as required (`--input_variables`,
`--template_sim_package`, `--n_samples`, `--outdir`,
`--openstudio_version`, `--archive_intermediates`).
"""

import dataclasses
import logging
from pathlib import Path
from typing import Any

import yaml

from .byos import ByosTrustLevel
from .validation import (
    ValidationError,
    validate_path_within,
    validate_template_package,
    validate_variables_yml,
)

log = logging.getLogger("osimflow.config")


@dataclasses.dataclass
class CampaignConfig:
    input_variables: Path
    template_sim_package: Path
    n_samples: int
    outdir: Path
    openstudio_version: str
    archive_intermediates: bool = False
    custom_apply_script: Path | None = None
    custom_kpi_extractor: Path | None = None
    # Optional MLflow tracking server (issue #7). When None, the campaign
    # runs without any MLflow integration (no mlflow import, no logging).
    # When set, the Campaign begins an MLflow run at start and ends it at
    # completion, logging params / metrics / artifacts.
    mlflow_tracking_uri: str | None = None
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
    # Azure Batch configuration (issue #254).
    azure_batch_account_name: str | None = None
    azure_batch_account_url: str | None = None
    azure_batch_pool_id: str = "osimflow-pool"
    azure_batch_location: str = "eastus"
    # Google Cloud Batch configuration (issue #254).
    google_batch_project_id: str | None = None
    google_batch_region: str = "us-central1"
    google_batch_service_account: str | None = None
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
    # Per-sample retry configuration (issue #252).
    # max_sample_retries: maximum retry attempts for transient per-sample
    # failures (network timeout, resource contention, etc.). A value of 0
    # disables retries. Each retry uses exponential backoff starting at
    # base_delay seconds (default 1.0), doubling each attempt up to 60s cap.
    max_sample_retries: int = 3
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
            }
            log.info(
                "objective config: name=%s, direction=%s, weight=%.2f",
                objective["name"],
                objective["direction"],
                objective["weight"],
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
        google_batch_project_id=(
            str(args["google_batch_project_id"]) if args.get("google_batch_project_id") else None
        ),
        google_batch_region=str(args.get("google_batch_region", "us-central1")),
        google_batch_service_account=(
            str(args["google_batch_service_account"])
            if args.get("google_batch_service_account")
            else None
        ),
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
        prometheus_port=int(str(args.get("prometheus_port", 9090))),
        otel_endpoint=str(args["otel_endpoint"]) if args.get("otel_endpoint") else None,
        registry_path=(Path(str(args["registry"])).resolve() if args.get("registry") else None),
        objective=objective,
        constraints=constraints,
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
    )
