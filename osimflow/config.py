"""Campaign configuration loader.

A thin wrapper around the `variables.yml` schema, plus the CLI flags
the PRD §1.4 calls out as required (`--input_variables`,
`--template_sim_package`, `--n_samples`, `--outdir`,
`--openstudio_version`, `--archive_intermediates`).
"""

import dataclasses
import logging
from pathlib import Path

import yaml

from .byos import ByosTrustLevel

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

    @property
    def work_dir(self) -> Path:
        return self.outdir / "work"

    @property
    def samples_file(self) -> Path:
        return self.work_dir / "samples.json"

    @property
    def cache_db(self) -> Path:
        return self.work_dir / "cache.sqlite"


def load_config(args: dict[str, object]) -> CampaignConfig:
    """Resolve a config from a flat dict (e.g. argparse namespace -> vars).

    Each value is narrowed via `str()` / `bool()` / `int()` because argparse
    already validates types — the cast here is purely a type-checker
    shim, not a runtime guarantee.
    """
    variables_yml = Path(str(args["input_variables"])).resolve()
    template = Path(str(args["template_sim_package"])).resolve()
    if not variables_yml.exists():
        raise FileNotFoundError(f"variables_yml not found: {variables_yml}")
    if not template.exists():
        raise FileNotFoundError(f"template_sim_package not found: {template}")

    outdir = Path(str(args["outdir"])).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    custom_apply = args.get("custom_apply_script")
    custom_kpi = args.get("custom_kpi_extractor")

    # Parse the optional baseline section from variables.yml (issue #64).
    baseline: dict[str, object] | None = None
    try:
        with variables_yml.open() as f:
            yml_data = yaml.safe_load(f)
        if isinstance(yml_data, dict) and "baseline" in yml_data:
            raw_baseline = yml_data["baseline"]
            if isinstance(raw_baseline, dict):
                baseline = {
                    "sample_id": str(raw_baseline.get("sample_id", "baseline")),
                    "parameters": dict(raw_baseline.get("parameters", {})),
                }
                log.info(
                    "baseline comparison enabled: sample_id=%s, %d parameters",
                    baseline["sample_id"],
                    len(baseline["parameters"]),  # type: ignore[arg-type]
                )
    except Exception as exc:
        log.warning("could not parse baseline section from %s: %s", variables_yml, exc)

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
    )
