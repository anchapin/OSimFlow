"""Argparse parser construction, presets, and per-subcommand argument registration."""

import argparse
import json
import logging
from pathlib import Path

from osimflow import (
    AWSBatchExecutor,  # noqa: F401 — re-exported for the historical ``osimflow.__main__.<X>Executor`` patch seam (pre-#1681).
    AzureBatchExecutor,  # noqa: F401 — see above.
    DaskJobQueueExecutor,  # noqa: F401 — see above.
    DockerSwarmExecutor,  # noqa: F401 — see above.
    GoogleBatchExecutor,  # noqa: F401 — see above.
    KubernetesExecutor,  # noqa: F401 — see above.
    NomadExecutor,  # noqa: F401 — see above.
    PBSExecutor,  # noqa: F401 — see above.
    SlurmExecutor,  # noqa: F401 — see above.
)
from osimflow.cli.serve import _cmd_serve
from osimflow.cosign import DEFAULT_COSIGN_OIDC_ISSUER
from osimflow.executor_configs import add_executor_arguments

log = logging.getLogger("osimflow.__main__")

PRESETS: dict[str, dict[str, object]] = {
    # local-quick: small local campaign for learning/testing
    "local-quick": {
        "executor": "local",
        "max_workers": 2,
        "openstudio_version": "3.10.0",
        "algorithm": "lhs",
        "max_generations": 1,
    },
    # local-large: larger local campaign with more parallelism
    "local-large": {
        "executor": "local",
        "max_workers": 8,
        "openstudio_version": "3.10.0",
        "algorithm": "lhs",
        "max_generations": 1,
    },
    # slurm-hpc: production campaign on a real Slurm cluster
    "slurm-hpc": {
        "executor": "slurm",
        "slurm_real": True,
        "slurm_partition": "short",
        "openstudio_version": "3.10.0",
        "algorithm": "lhs",
        "max_generations": 1,
    },
    # slurm-gpu: GPU-enabled Slurm campaign
    "slurm-gpu": {
        "executor": "slurm",
        "slurm_real": True,
        "slurm_partition": "gpu",
        "slurm_qos": "high",
        "slurm_constraint": "gpu",
        "slurm_gres": "gpu:1",
        "openstudio_version": "3.10.0",
        "algorithm": "lhs",
        "max_generations": 1,
    },
    # aws-batch-cloud: large-scale campaign on AWS Batch
    "aws-batch-cloud": {
        "executor": "aws_batch",
        "aws_batch_queue": "osimflow-batch-queue",
        "aws_batch_max_retries": 3,
        "openstudio_version": "3.10.0",
        "algorithm": "lhs",
        "max_generations": 1,
    },
    # sensitivity-morris: Morris sensitivity analysis (SALib)
    "sensitivity-morris": {
        "executor": "local",
        "max_workers": 4,
        "openstudio_version": "3.10.0",
        "algorithm": "morris",
        "max_generations": 1,
    },
    # sensitivity-fast99: FAST99 sensitivity analysis (SALib)
    "sensitivity-fast99": {
        "executor": "local",
        "max_workers": 4,
        "openstudio_version": "3.10.0",
        "algorithm": "fast99",
        "max_generations": 1,
    },
    # optimization-de: Differential evolution optimization
    "optimization-de": {
        "executor": "slurm",
        "slurm_real": True,
        "slurm_partition": "short",
        "openstudio_version": "3.10.0",
        "algorithm": "de",
        "max_generations": 50,
    },
    # optimization-nsga2: NSGA-II multi-objective optimization
    "optimization-nsga2": {
        "executor": "slurm",
        "slurm_real": True,
        "slurm_partition": "short",
        "openstudio_version": "3.10.0",
        "algorithm": "nsga2",
        "max_generations": 50,
    },
}


def _apply_preset(args: argparse.Namespace) -> None:
    """Apply preset values to ``args`` in-place.

    Individual flags that were explicitly set on the command line
    always take precedence over preset values.

    A flag is considered "explicitly set" when its current value differs
    from the parser default.  For string/int/enum flags this works cleanly.
    For boolean store_true flags where the default is False, argparse sets
    the value to False both when the user passes ``--no-flag`` and when the
    user passes nothing at all.  Since we cannot distinguish these two
    cases, any False value (the argparse default) is treated as "not
    explicitly overridden" and the preset value is applied.
    """
    preset_name = getattr(args, "preset", None)
    if not preset_name:
        return

    if preset_name not in PRESETS:
        return  # validated by argparse choice

    preset_defaults = _PresetDefaultCache.get()
    current = vars(args)
    for key, preset_value in PRESETS[preset_name].items():
        current_value = current.get(key)
        default_value = preset_defaults.get(key)
        # Apply preset when:
        # - current is None (flag not touched on command line), OR
        # - current equals the parser default (user did not change it from default)
        # For store_true booleans with default False, this means the preset
        # overrides False (the argparse default) since we cannot distinguish
        # "user passed --no-flag" from "user passed nothing".
        if current_value is None or current_value == default_value:
            setattr(args, key, preset_value)


def _extract_max_concurrent_samples(resource_quota: str | None) -> int | None:
    """Extract max_concurrent_samples from a JSON resource_quota string."""
    if resource_quota is None:
        return None
    try:
        quota: dict[str, object] = json.loads(resource_quota)
        value = quota.get("max_concurrent_samples")
        if isinstance(value, int):
            return value
        return None
    except (json.JSONDecodeError, TypeError):
        return None


def _add_run_args(run: argparse.ArgumentParser) -> None:  # noqa: PLR0915
    run.add_argument(
        "--preset",
        choices=list(PRESETS.keys()),
        default=None,
        help=(
            "Apply a named preset of recommended flags for a specific use case. "
            "Individual CLI flags override preset values. "
            "Presets: " + ", ".join(PRESETS.keys()) + ". "
            "Use 'local-quick' for a fast 2-sample local test. "
            "Use 'slurm-hpc' for a real Slurm cluster. "
            "Use 'aws-batch-cloud' for AWS Batch. "
            "Use 'optimization-de' or 'optimization-nsga2' for optimization. "
            "(issue #384)"
        ),
    )
    run.add_argument(
        "--executor",
        choices=[
            "local",
            "slurm",
            "aws_batch",
            "nomad",
            "azure_batch",
            "google_batch",
            "kubernetes",
            "pbs",
            "dask_jobqueue",
            "docker_swarm",
        ],
        default="local",
        help="Executor backend (default: local). See --preset for quick-start bundles.",
    )
    # Per-executor flags (issue #1575): each built-in executor owns its
    # ``--flags`` in ``osimflow/executor_configs/<name>.py`` and registers
    # them via the ``add_arguments`` hook iterated here. Third-party
    # executor plug-ins register through
    # ``ExecutorRegistry.register_arguments`` /
    # ``osimflow.executor_configs.register_executor_arguments``.
    add_executor_arguments(run)
    # Issue #1563: substrate-agnostic submit-rate override. Each
    # executor carries a substrate-appropriate default RPS (e.g. 800 for
    # AWS Batch, 5 for Nomad/Kubernetes); ``--submit-rps <float>``
    # overrides the default for the chosen executor. ``None`` /
    # omitted means "use the executor's default". Setting this to a
    # low value is useful for cluster benchmarks and conformance
    # checks that want to verify throttling without waiting seconds
    # of wall time.
    run.add_argument(
        "--submit-rps",
        type=float,
        default=None,
        help=(
            "Submit rate limit in requests per second, enforced via the "
            "shared token-bucket limiter (issue #1563). Overrides the "
            "chosen executor's substrate-appropriate default (e.g. 800 "
            "for AWS Batch, 5 for Nomad/Kubernetes, 100 for Slurm/PBS). "
            "Set to a low value for throttling conformance checks; "
            "leave unset to use the executor default."
        ),
    )
    run.add_argument(
        "--shard-count",
        type=int,
        default=None,
        help=(
            "Total number of coordinator shards (partition mode). "
            "Requires --shard-index. Samples are assigned by global index modulo shard-count."
        ),
    )
    run.add_argument(
        "--shard-index",
        type=int,
        default=None,
        help=("Zero-based shard index for partition mode. Requires --shard-count."),
    )
    run.add_argument(
        "--shard-start",
        type=int,
        default=None,
        help=("Inclusive sample index start for explicit range shard mode. Requires --shard-end."),
    )
    run.add_argument(
        "--shard-end",
        type=int,
        default=None,
        help=("Exclusive sample index end for explicit range shard mode. Requires --shard-start."),
    )
    run.add_argument(
        "--task-queue",
        choices=["none", "dask"],
        default="none",
        help=(
            "Distributed task queue backend for fan-out steps "
            "(APPLY_PARAMETERS, RUN_OPENSTUDIO_SIM, EXTRACT_KPIS). "
            "When 'none' (default), work is submitted directly to the "
            "configured --executor. When 'dask', work is submitted to "
            "a Dask scheduler (requires --dask-scheduler-address unless "
            "a local embedded cluster is acceptable)."
        ),
    )
    run.add_argument(
        "--dask-scheduler-address",
        default=None,
        help=(
            "Dask scheduler address (e.g. tcp://scheduler:8786). "
            "When omitted and --task-queue is 'dask', an embedded "
            "single-process LocalCluster is started automatically."
        ),
    )
    run.add_argument(
        "--redis-url",
        default=None,
        help=(
            "Redis URL for distributed campaign state (issue #993, T8.2). "
            "When set, cache entries shared across nodes/processes are "
            "coordinated through Redis and each process uses a private "
            "local SQLite file, so concurrent campaigns against the same "
            "outdir no longer contend on one SQLite database. "
            "Example: redis://localhost:6379/0. Default: single-node "
            "SQLite cache (unchanged behaviour)."
        ),
    )
    # Cost tracking flags (issue #447). Substrate-specific rates
    # (--aws-batch-spot-price / --aws-batch-on-demand-price /
    # --slurm-cost-per-node-hour) live with their executors in
    # osimflow/executor_configs/ (issue #1575).
    run.add_argument(
        "--enable-cost-tracking",
        action="store_true",
        help=(
            "Enable campaign cost tracking for cloud/HPC resources (issue #447). "
            "When set, estimated and actual costs are recorded per-sample and "
            "a cost summary is written to run.json at campaign completion."
        ),
    )
    run.add_argument(
        "--cost-on-demand-price",
        type=float,
        default=None,
        help=(
            "On-demand price per vCPU-hour for cost estimation (USD). "
            "Used when cloud provider APIs are unavailable. "
            "Default: 0.05 (i.e., $0.05/vCPU·hour)."
        ),
    )
    run.add_argument(
        "--cost-spot-price",
        type=float,
        default=None,
        help=(
            "Spot price per vCPU-hour for cost estimation (USD). "
            "Used to estimate potential savings vs on-demand. "
            "Default: 0.03 (i.e., $0.03/vCPU·hour, ~40%% savings)."
        ),
    )
    run.add_argument(
        "--input_variables",
        default=None,
        help=(
            "Path to variables.yml. Required unless --prebuilt-workflow is set, "
            "in which case an empty variable set is synthesised."
        ),
    )
    run.add_argument("--template_sim_package", required=True)
    run.add_argument("--n_samples", type=int, required=True)
    run.add_argument("--outdir", required=True)
    run.add_argument("--openstudio_version", default="3.10.0")
    run.add_argument(
        "--container-digest",
        default=None,
        help=(
            "Pin container images by SHA256 digest (e.g. 'sha256:abc...' or "
            "'repo@sha256:abc...'). When set, overrides the mutable tag for ALL "
            "executors (issue #1081)."
        ),
    )
    run.add_argument(
        "--require-cosign-identity",
        default=None,
        help=(
            "Verify the OpenStudio image signature at campaign init via "
            "`cosign verify --certificate-identity <ID>` (keyless, sigstore). "
            "The campaign refuses to run when verification fails or the cosign "
            "binary is unavailable (issue #1385)."
        ),
    )
    run.add_argument(
        "--cosign-oidc-issuer",
        default=None,
        help=(
            "Expected OIDC issuer for --require-cosign-identity keyless "
            f"verification (default: {DEFAULT_COSIGN_OIDC_ISSUER})."
        ),
    )
    run.add_argument(
        "--project",
        default="",
        help=(
            "Project name to group this campaign under (issue #390). "
            "Used for organizing campaigns in the registry. "
            "Example: --project 'Building Energy Analysis Q1 2026'."
        ),
    )
    run.add_argument("--archive_intermediates", action="store_true")
    run.add_argument(
        "--custom_apply_script",
        default=None,
        help="Path to a .py file defining apply_parameters(...) (BYOS)",
    )
    run.add_argument(
        "--custom_kpi_extractor",
        default=None,
        help="Path to a .py file defining extract_kpis(...) (BYOS)",
    )
    run.add_argument(
        "--kpis",
        nargs="*",
        metavar="KPI",
        default=None,
        help="Restrict KPI extraction to these names (all KPIs if omitted).",
    )
    run.add_argument("--log_level", default="INFO")
    run.add_argument(
        "--algorithm",
        default=None,
        help=(
            "Sampling algorithm to use (default: lhs). "
            "Available algorithms are registered in AlgorithmRegistry."
        ),
    )
    run.add_argument(
        "--nsga2-reference-points",
        default=None,
        help=(
            "Reference points for R-NSGA-II (issue #529). "
            "Comma-separated fractions representing aspiration points on the "
            "Pareto front, e.g., '0.25,0.5,0.75' for 2 objectives. "
            "Only used when --algorithm is 'nsga2'."
        ),
    )
    run.add_argument(
        "--nsga2-reference-directions",
        default=None,
        help=(
            "Reference direction strategy for R-NSGA-II (issue #529). "
            "Supported values: 'das-dennis' (Das-Dennis structured points), "
            "'energy' (Riesz s-Energy well-spaced points), "
            "'wedge' (wedge/decomposition-based), "
            "'incremental' (incremental method). "
            "Only used when --algorithm is 'nsga2'."
        ),
    )
    run.add_argument(
        "--mlflow_tracking_uri",
        default=None,
        help=(
            "MLflow tracking server URI (e.g. http://localhost:5000). "
            "When set, the campaign logs params/metrics/artifacts to MLflow. "
            "Requires `pip install osimflow[mlflow]`."
        ),
    )
    run.add_argument(
        "--weather_dir",
        default="weather",
        help=(
            "Name of the weather subdirectory inside template_sim_package "
            "that holds .epw files. Default: weather."
        ),
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run a single sample in local mode to validate setup before "
            "full campaign. Forces n_samples=1, local executor, steps 1-4 only."
        ),
    )
    run.add_argument(
        "--sample",
        type=int,
        default=None,
        help=(
            "Run only sample N (0-indexed) through steps 2-4. "
            "Skips GENERATE_LHS_SAMPLES; reuses existing samples.json. "
            "Useful for debugging a specific failed sample."
        ),
    )
    run.add_argument(
        "--init-script",
        default=None,
        type=Path,
        help=(
            "Path to a shell script to run before the campaign starts. "
            "Must exit 0 or the campaign aborts. Environment variables "
            "OSIMFLOW_OUTDIR, OSIMFLOW_N_SAMPLES, OSIMFLOW_EXECUTOR, "
            "and OSIMFLOW_ALGORITHM are set."
        ),
    )
    run.add_argument(
        "--finalize-script",
        default=None,
        type=Path,
        help=(
            "Path to a shell script to run after the campaign completes. "
            "Best-effort: a non-zero exit is logged but does NOT fail "
            "the campaign. Receives the same env vars as --init-script "
            "plus OSIMFLOW_STATUS (success/failure) and "
            "OSIMFLOW_DURATION_S."
        ),
    )
    run.add_argument(
        "--init-script-timeout",
        type=float,
        default=600.0,
        help=(
            "Wall-clock timeout in seconds for the --init-script "
            "subprocess (issue #1685). On expiry the child process group "
            "is SIGKILLed and the campaign aborts with a CampaignError "
            "before any step runs. Default: 600 s — a hung init script "
            "(e.g. stuck on an NFS mount or a lock) used to block "
            "Campaign.run() forever."
        ),
    )
    run.add_argument(
        "--finalize-script-timeout",
        type=float,
        default=600.0,
        help=(
            "Wall-clock timeout in seconds for the --finalize-script "
            "subprocess (issue #1685). On expiry the child process group "
            "is SIGKILLed and a warning is logged, but the finalize hook "
            "returns normally so the Campaign.run() finally block can "
            "still rewrite run.json, fire the webhook, and cache.close(). "
            "Default: 600 s."
        ),
    )
    run.add_argument(
        "--prebuilt-workflow",
        action="store_true",
        help=(
            "Execute the existing OSW package in --template_sim_package as-is "
            "(issue #1812): no parameter mutation, no OpenStudio Python "
            "bindings, no variables required. Rejects non-empty variables "
            "and, on remote executors, custom apply/extract hooks."
        ),
    )
    run.add_argument(
        "--skip-preflight",
        action="store_true",
        help=(
            "Skip the PREFLIGHT_RUN_MODEL step that validates the seed "
            "model before the full campaign. Useful when the model is "
            "known-good or when iterating on downstream steps."
        ),
    )
    run.add_argument(
        "--max-generations",
        type=int,
        default=1,
        help=(
            "Maximum number of DAG generations to run (default: 1). "
            "Iterative algorithms (NSGA-II, DE) loop the fan-out steps "
            "for multiple generations. LHS is single-generation."
        ),
    )
    run.add_argument(
        "--max-sample-retries",
        type=int,
        default=3,
        help=(
            "Maximum retry attempts for transient per-sample failures "
            "(network timeout, resource contention, etc.). Default: 3. "
            "Set to 0 to disable retries. Each retry uses exponential "
            "backoff starting at 1s, doubling each attempt up to 60s cap."
        ),
    )
    run.add_argument(
        "--byos-trust-level",
        choices=["subprocess", "inprocess"],
        default="subprocess",
        help=(
            "BYOS script execution mode (default: subprocess). "
            "'subprocess' runs user scripts in an isolated child process "
            "(recommended). 'inprocess' loads scripts directly into the "
            "orchestrator process (legacy, less secure)."
        ),
    )
    run.add_argument(
        "--require-trusted-scripts",
        action="store_true",
        default=None,
        help=(
            "Reject --byos-trust-level inprocess for production hardening "
            "(issues #908, #1207). When set, the CLI exits with an error if "
            "--byos-trust-level inprocess is also passed. When not set "
            "(default), inprocess is blocked by default and this flag must be "
            "passed to explicitly allow it in trusted development environments."
        ),
    )
    run.add_argument(
        "--byos-resource-limits",
        default=None,
        help=(
            "JSON dict of rlimit names to values applied to the BYOS "
            "subprocess (issue #343). Example: "
            '\'{"RLIMIT_CPU": 300, "RLIMIT_AS": 4294967296}\'. '
            "resource.error from impossible limits is caught and logged "
            "as a warning (non-fatal)."
        ),
    )
    run.add_argument(
        "--byos-timeout-s",
        type=float,
        default=None,
        help=(
            "Wall-clock timeout in seconds for the BYOS subprocess and the "
            "real OpenStudio CLI simulation subprocess (issue #1109). When "
            "set, a wedged subprocess is killed and the sample fails "
            "instead of hanging the campaign forever; a timeout kill is "
            "non-transient and is not retried (issue #1534). Default: no "
            "timeout (effectively unbounded) because annual EnergyPlus "
            "simulations routinely exceed 10 minutes — bound long runs via "
            "the executor's walltime instead, or set this explicitly."
        ),
    )
    run.add_argument(
        "--sample-await-timeout-s",
        type=float,
        default=None,
        help=(
            "Orchestrator-side per-step await deadline floor in seconds "
            "(issue #1566). When set, the campaign derives the deadline "
            "passed to ``handle.result(timeout=...)`` as "
            "``max(time_min_for_step * 60, byos_timeout_s, await_timeout_s)`` "
            "and a wedged substrate (Nomad allocation that never reaches "
            "terminal, Docker Swarm service in a non-terminal state, K8s "
            "``time_min=0`` yielding no ``activeDeadlineSeconds``) becomes "
            "a ``TimeoutError`` that flows through the existing per-sample "
            "failure recording (``failed_simulations.csv`` row, alert, "
            "checkpoint) instead of parking an ``_await_one`` thread "
            "indefinitely. Default: no orchestrator-side deadline "
            "(preserves the pre-#1566 bare-``handle.result()`` semantics)."
        ),
    )
    run.add_argument(
        "--observability",
        choices=["none", "cloudwatch", "prometheus", "opentelemetry"],
        default="none",
        help=(
            "Observability backend for campaign metrics (default: none). "
            "none = no metrics emitted (zero overhead). "
            "cloudwatch = AWS CloudWatch (requires osimflow[aws]). "
            "prometheus = Prometheus pushgateway. "
            "opentelemetry = OTLP gRPC exporter."
        ),
    )
    run.add_argument(
        "--cloudwatch-namespace",
        default="OSimFlow",
        help="CloudWatch namespace for metrics (default: OSimFlow).",
    )
    run.add_argument(
        "--cloudwatch-log-group",
        default=None,
        help="CloudWatch log group name (optional).",
    )
    run.add_argument(
        "--prometheus-port",
        type=int,
        default=9090,
        help="Prometheus pushgateway port (default: 9090).",
    )
    run.add_argument(
        "--otel-endpoint",
        default=None,
        help='OpenTelemetry OTLP gRPC endpoint (e.g. "http://localhost:4317").',
    )
    run.add_argument(
        "--observability-flush-interval",
        type=float,
        default=30.0,
        help=(
            "Periodic flush interval in seconds for observability backends (issue #1186). "
            "Metrics are flushed at this interval to prevent loss on early crash. "
            "Set to 0 to disable periodic flush (flush only at campaign end). "
            "Default: 30.0 seconds."
        ),
    )
    run.add_argument(
        "--no-tui",
        action="store_true",
        help=(
            "The rich TUI activates automatically when all three conditions are met: "
            "(a) rich >= 13.0 is installed, (b) stdout is a TTY (not piped/CI), "
            "and (c) --no-tui is not passed. "
            "Pass this flag to disable the TUI when piping output or in environments "
            "where the TUI causes display issues (issue #1221)."
        ),
    )
    run.add_argument(
        "--registry",
        default=None,
        type=Path,
        help=(
            "Path to the campaign registry database (default: ~/.osimflow/registry.db). "
            "Override the default location for multi-campaign management."
        ),
    )
    run.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Enable air-gapped / offline mode (issue #261). "
            "Skips Docker Hub pulls, PyPI version checks, and online weather downloads. "
            "Requires --offline-bundle to be set pointing to a pre-bundled offline asset directory."
        ),
    )
    run.add_argument(
        "--offline-bundle",
        default=None,
        type=Path,
        help=(
            "Path to the offline bundle directory created by "
            "scripts/bundle_offline.py. Contains pip/, docker/, and weather/ subdirectories. "
            "Required when --offline is set."
        ),
    )
    run.add_argument(
        "--webhook-url",
        default=None,
        help=(
            "URL to POST a campaign completion summary to. "
            "The POST contains a JSON payload with campaign_id, status, "
            "elapsed_s, n_samples, n_succeeded, n_failed, total_cost_usd, "
            "and outdir. Best-effort: delivery failures are logged but do "
            "not affect campaign status. (issue #283)"
        ),
    )
    run.add_argument(
        "--result-storage-backend",
        choices=["local", "s3", "gs", "azure"],
        default="local",
        help=(
            "Result storage backend for multi-node campaigns (issue #339). "
            "local = no remote upload (default). "
            "s3 = Amazon S3 (or S3-compatible like MinIO/R2). "
            "gs = Google Cloud Storage. "
            "azure = Azure Blob Storage."
        ),
    )
    run.add_argument(
        "--result-storage-bucket",
        default=None,
        help=(
            "Bucket/container name for result storage (issue #339). "
            "For S3/gs this is the bucket name; for Azure it is the container name. "
            "Required when --result-storage-backend is not 'local'."
        ),
    )
    run.add_argument(
        "--result-storage-endpoint",
        default=None,
        help=(
            "Custom S3-compatible endpoint URL for result storage (issue #339). "
            "Use for MinIO, Cloudflare R2, or other S3-compatible stores. "
            "Only valid when --result-storage-backend is 's3'."
        ),
    )
    run.add_argument(
        "--resource-quota",
        default=None,
        help=(
            "JSON dict of resource quota limits for the campaign (issue #446). "
            'Example: \'{"max_samples": 100, "max_cost_usd": 5000.0, '
            '"max_wall_time_min": 240, "max_concurrent_samples": 10}\'. '
            "All fields are optional. The campaign fails fast at start if "
            "a quota is already exceeded, and skips further sample submissions "
            "when the quota is exhausted during fan-out steps."
        ),
    )
    run.add_argument(
        "--alert-rules",
        default=None,
        type=Path,
        help=(
            "Path to a YAML file defining alert rules (issue #438). "
            "Each rule specifies an event_type, severity, message_template, "
            "and condition. Built-in rules are always included; custom rules "
            "from this file are added alongside them."
        ),
    )
    run.add_argument(
        "--alert-destinations",
        default=None,
        type=Path,
        help=(
            "Path to a YAML file defining alert destinations (issue #438). "
            "Supported destination types: webhook (url), email (smtp_host, recipients), "
            "and log (level). When not set, alerts are only logged."
        ),
    )
    run.add_argument(
        "--track-costs",
        action="store_true",
        dest="enable_cost_tracking",
        help=(
            "Enable campaign cost tracking (issue #447). "
            "Estimates cloud/HPC resource costs and writes a cost summary JSON "
            "alongside campaign outputs. Supports AWS Batch (on-demand/Spot), "
            "Slurm (per-node-hour), and Local (no cost) executors. "
            "Alias of --enable-cost-tracking; both flags write to the "
            "``enable_cost_tracking`` dest so ``load_config`` reads the "
            "value correctly (issue #1556)."
        ),
    )
    run.add_argument(
        "--chaos-enabled",
        action="store_true",
        default=False,
        help=(
            "Enable chaos fault injection during the campaign (issue #1013). "
            "Off by default. When set, ``--chaos-scenarios`` lists the "
            "injectors (kill_switch, network_delay, cpu_spike, memory_pressure) "
            "and ``--chaos-schedule`` controls when they fire (before_step, "
            "after_step, per_sample, or none). All chaos faults are "
            "non-destructive in this wiring — the orchestrator is never "
            "killed and a fault never aborts the campaign."
        ),
    )
    run.add_argument(
        "--chaos-scenarios",
        default=None,
        help=(
            "Comma-separated list of chaos scenarios to enable (issue #1013). "
            "Used together with ``--chaos-enabled``. Built-in scenarios: "
            "``kill_switch``, ``network_delay``, ``cpu_spike``, "
            "``memory_pressure``. Default: ``kill_switch`` when "
            "``--chaos-enabled`` is set without this flag."
        ),
    )
    run.add_argument(
        "--chaos-schedule",
        choices=["none", "before_step", "after_step", "per_sample"],
        default="none",
        help=(
            "When to inject chaos faults during the campaign (issue #1013). "
            "``before_step``/``after_step`` fire once per DAG step; "
            "``per_sample`` fires once per sample submission inside the "
            "RUN_OPENSTUDIO_SIM and EXTRACT_KPIS fan-outs. "
            "``none`` (default) disables injection even when "
            "``--chaos-enabled`` is set."
        ),
    )
    run.add_argument(
        "--chaos-probability",
        type=float,
        default=1.0,
        help=(
            "Per-injector base probability forwarded to "
            "``network_delay``, ``cpu_spike``, and ``memory_pressure`` "
            "(issue #1013). 1.0 = always inject. Default: 1.0."
        ),
    )
    run.add_argument(
        "--chaos-delay-s",
        type=float,
        default=0.1,
        help=("Delay in seconds applied by ``network_delay`` (issue #1013). Default: 0.1s."),
    )
    run.add_argument(
        "--chaos-jitter-s",
        type=float,
        default=0.05,
        help=(
            "Random jitter in seconds applied by ``network_delay`` (issue #1013). Default: 0.05s."
        ),
    )
    run.add_argument(
        "--chaos-duration-s",
        type=float,
        default=0.5,
        help=(
            "Duration in seconds for ``cpu_spike`` and ``memory_pressure`` "
            "scenarios (issue #1013). Default: 0.5s."
        ),
    )
    run.add_argument(
        "--chaos-intensity",
        type=float,
        default=0.5,
        help=("CPU intensity fraction (0.0-1.0) for ``cpu_spike`` (issue #1013). Default: 0.5."),
    )
    run.add_argument(
        "--chaos-size-mb",
        type=int,
        default=64,
        help=("Size in MB for the ``memory_pressure`` injector (issue #1013). Default: 64 MB."),
    )
    run.add_argument(
        "--chaos-fail-after",
        type=int,
        default=2,
        help=("Number of inject calls before ``kill_switch`` activates (issue #1013). Default: 2."),
    )
    run.add_argument(
        "--uq-method",
        default="latin_hypercube",
        help=(
            "Uncertainty Quantification (UQ) sampling method (issue #530). "
            "Options: 'latin_hypercube' (default) or 'monte_carlo'. "
            "Used when --algorithm uq is set."
        ),
    )
    run.add_argument(
        "--uq-n-samples",
        type=int,
        default=None,
        help=(
            "Number of Monte Carlo samples for UQ analysis (issue #530). "
            "When omitted, defaults to --n_samples. "
            "Used when --algorithm uq is set."
        ),
    )
    run.add_argument(
        "--uq-failure-threshold",
        action="append",
        default=None,
        dest="uq_failure_thresholds",
        help=(
            "Failure threshold for probability of failure (POF) analysis (issue #530). "
            "Format: 'kpi_name=threshold_value' (e.g., 'eui=150'). "
            "Can be specified multiple times for multiple KPIs. "
            "Used when --algorithm uq is set."
        ),
    )
    run.add_argument(
        "--detach-s3",
        dest="detach_s3",
        action="store_true",
        default=False,
        help=(
            "AWS Batch only: submit the jobs, write a handoff record + samples.json "
            "to the --result-storage-bucket S3 bucket and exit without waiting "
            "(issue #1873). Check progress with `osimflow status --from-s3` and "
            "fetch results with `osimflow download --from-s3`. Re-running with the "
            "same --outdir name is a no-op once the handoff exists."
        ),
    )
    run.add_argument(
        "--detach",
        action="store_true",
        default=False,
        help=(
            "Hand off the campaign to a remote Coordinator service and exit "
            "immediately (Phase 2 fire-and-forget mode). Requires --coordinator-url. "
            "The campaign runs on the Coordinator; poll its status via "
            "GET <coordinator-url>/api/v1/coordinator/campaigns/<id>."
        ),
    )
    run.add_argument(
        "--coordinator-url",
        default=None,
        help=(
            "Base URL of the Coordinator service (e.g., https://coordinator.example.com). "
            "Required when --detach is set. The CLI will POST to "
            "<coordinator-url>/api/v1/coordinator/handoff."
        ),
    )
    run.add_argument(
        "--s3-artifact-bucket",
        default=None,
        help=(
            "S3 bucket name for centralized artifact storage (issue #601). "
            "When set, base simulation assets (.osm, .epw) are uploaded to S3 "
            "once at campaign creation. Remote executor nodes download them "
            "directly via pre-signed URLs, eliminating the local-machine bottleneck "
            "for large fan-out campaigns."
        ),
    )
    run.add_argument(
        "--s3-artifact-prefix",
        default=None,
        help=(
            "S3 prefix within the artifact bucket for this campaign (issue #601). "
            "Example: 'campaign-123' or 'project Q1/run-456'. "
            "Required when --s3-artifact-bucket is set."
        ),
    )
    run.add_argument(
        "--s3-artifact-region",
        default=None,
        help=(
            "AWS region for the S3 artifact bucket (issue #601). "
            "When omitted, uses the region from the IAM role or default "
            "credential chain. Required for pre-signed URL generation "
            "with some bucket configurations."
        ),
    )
    run.add_argument(
        "--s3-artifact-endpoint",
        default=None,
        help=(
            "Custom S3-compatible endpoint URL for artifact storage (issue #601). "
            "Use for MinIO, Cloudflare R2, or other S3-compatible stores. "
            "Only valid when --s3-artifact-bucket is set."
        ),
    )
    run.add_argument(
        "--allow-insecure-storage-endpoint",
        action="store_true",
        default=False,
        help=(
            "Opt-in flag to allow plaintext 'http://' --result-storage-endpoint, "
            "--s3-artifact-endpoint, and --coordinator-url / OSIMFLOW_COORDINATOR_URL "
            "URLs (issues #1386, #1550). Defaults to False "
            "(fail-closed) — non-HTTPS endpoints are rejected by the storage "
            "backend / Coordinator client to prevent AWS SigV4 signing material and "
            "the bearer API key from leaking in cleartext. Use only for dev/test "
            "against a local MinIO or loopback Coordinator; loopback "
            "hosts are exempt without this flag. Mirrors the Redis 'rediss://' "
            "enforcement from issue #1321."
        ),
    )
    run.add_argument(
        "--s3-artifact-presigned-url-expiration",
        type=int,
        default=3600,
        help=(
            "Expiration time in seconds for pre-signed URLs (issue #601). "
            "Remote executor nodes must download artifacts within this window. "
            "Default: 3600 (1 hour). Min: 60, Max: 43200 (12 hours)."
        ),
    )
    run.add_argument(
        "--bcl-api-key",
        default=None,
        help=(
            "API key for the NREL Building Component Library (BCL). "
            "Required for accessing BCL measures when --validate-measures is set. "
            "Can also be set via the BCL_API_KEY environment variable (issue #580)."
        ),
    )
    run.add_argument(
        "--validate-measures",
        action="store_true",
        help=(
            "Validate measure arguments against the BCL taxonomy when discovering "
            "BCL measures (issue #580). Logs warnings for argument name/type "
            "deviations from the expected taxonomy."
        ),
    )


def _add_import_osa_args(imp: argparse.ArgumentParser) -> None:
    imp.add_argument(
        "input",
        type=Path,
        help="Path to the .osa or analysis.json file to import",
    )
    imp.add_argument(
        "--output",
        type=Path,
        default=Path("variables.yml"),
        help="Output path for the converted variables.yml (default: variables.yml)",
    )
    imp.add_argument(
        "--template-package",
        type=Path,
        default=None,
        help="Optional source template simulation package directory to stage",
    )
    imp.add_argument(
        "--package-output",
        type=Path,
        default=None,
        help="Destination directory for the staged template simulation package",
    )
    imp.add_argument("--log_level", default="INFO")


def _add_import_gem_osa_args(imp: argparse.ArgumentParser) -> None:
    imp.add_argument("project_dir", type=Path, help="Gem project directory")
    imp.add_argument("--output-dir", type=Path, required=True, help="Campaign directories root")
    imp.add_argument("--batch", action="append", default=None, help="Batch id (repeatable)")
    imp.add_argument(
        "--template-package", type=Path, default=None, help="Package with workflow.osw"
    )
    imp.add_argument("--configs", type=Path, default=None, help="configs.yml (default: project)")
    imp.add_argument("--log_level", default="INFO")


def _add_export_args(exp: argparse.ArgumentParser) -> None:
    exp.add_argument(
        "--target",
        choices=["pat"],
        required=True,
        help="Export format (currently only 'pat' for PAT-compatible analysis.json)",
    )
    exp.add_argument(
        "--variables",
        type=Path,
        required=True,
        help="Path to variables.yml to export",
    )
    exp.add_argument(
        "--outdir",
        type=Path,
        default=Path("."),
        help="Output directory for exported files (default: current directory)",
    )
    exp.add_argument(
        "--n_samples",
        type=int,
        default=10,
        help="Number of samples to record in the analysis (default: 10)",
    )
    exp.add_argument(
        "--algorithm",
        default="lhs",
        help="Sampling algorithm name to export (default: lhs)",
    )
    exp.add_argument(
        "--openstudio_version",
        default="3.10.0",
        help="OpenStudio CLI version (default: 3.10.0)",
    )
    exp.add_argument("--log_level", default="INFO")


def _add_serve_args(serve: argparse.ArgumentParser) -> None:
    serve.add_argument("--outdir", type=Path, required=True, help="Campaign output directory")
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1 — localhost only). Pass 0.0.0.0 for network access.",
    )
    serve.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    serve.add_argument(
        "--enable-writes",
        action="store_true",
        default=False,
        help="Enable write endpoints (POST/PUT/DELETE). Default: read-only.",
    )
    serve.add_argument(
        "--api-key",
        default=None,
        help=(
            "API key for authentication (single-key mode). "
            "SEC-001: REQUIRED when binding to a non-local interface — "
            "without it every endpoint is reachable without credentials "
            "(issue #1095). When no --api-key is supplied and no "
            "--api-keys-file is set, an ephemeral key is auto-generated and "
            "printed to stderr at startup (issue #1553 / #1117) so the "
            "serve is never left unauthenticated — including on a read-only "
            "localhost bind, where other local accounts can otherwise read "
            "run.json, KPI results, and registry listings over the loopback "
            "interface on a shared host. Pass --api-key explicitly to pin a "
            "stable key across serves. Use --api-keys-file for multi-user "
            "authentication (issue #395)."
        ),
    )
    serve.add_argument(
        "--api-keys-file",
        default=None,
        type=Path,
        help=(
            "Path to a JSON file containing multiple API keys with per-user "
            "roles (issue #395). When set, --api-key is ignored. "
            'File format: {"users": [{"key": "...", "user_id": "...", "role": "..."}]}. '
            "Roles: readonly, readwrite, admin. "
            "File mode must be 0600 (owner read/write only); group/world "
            "readable files are refused at load time to avoid leaking "
            "admin keys on shared hosts (issue #1480). Override with "
            "--allow-insecure-api-keys-file (dev/test only)."
        ),
    )
    serve.add_argument(
        "--allow-insecure-api-keys-file",
        action="store_true",
        default=False,
        help=(
            "Opt-in flag to allow --api-keys-file with group/world readable "
            "permissions (issue #1480). Defaults to False (fail-closed) — "
            "a permissive file is refused at load time because any local "
            "account on the host can read every API key, including "
            "admin-role keys. Mirrors --allow-insecure-storage-endpoint "
            "(issue #1386). Dev/test only — fix with 'chmod 0600 <file>' "
            "instead."
        ),
    )
    serve.add_argument(
        "--cors-origins",
        default=None,
        help=(
            "Comma-separated list of allowed CORS origins (default: empty = "
            "same-origin only). Example: 'http://localhost:3000,https://app.example.com'"
        ),
    )
    serve.add_argument(
        "--rate-limit",
        default="60/minute",
        help="Rate limit string, e.g. '60/minute' (default: 60/minute).",
    )
    serve.add_argument(
        "--rate-limit-key",
        default="ip",
        choices=["ip", "user", "campaign"],
        help=(
            "Rate limit key type: 'ip' (default, per-IP limiting), "
            "'user' (per-API-key limiting, issue #445), or "
            "'campaign' (per-campaign-ID limiting, issue #445)."
        ),
    )
    serve.add_argument(
        "--rate-limit-trust-x-forwarded-for",
        action="store_true",
        default=False,
        help=(
            "Honor the X-Forwarded-For header for rate-limit identity "
            "ONLY when the immediate upstream is in --trusted-proxies "
            "(issue #1683). Defaults to off: XFF is ignored and the "
            "socket peer is the only rate-limit identity, so a client "
            "cannot rotate a spoofed XFF value to obtain a fresh "
            "bucket. Can also be enabled via OSIMFLOW_TRUST_X_FORWARDED_FOR."
        ),
    )
    serve.add_argument(
        "--trusted-proxies",
        default=None,
        help=(
            "Comma-separated list of trusted-proxy CIDR blocks / IPs "
            "used by the X-Forwarded-For trust gate (issue #1683). "
            "Each entry accepts a single IP ('10.0.0.1', "
            "'2001:db8::1') or a CIDR block ('10.0.0.0/8', "
            "'2001:db8::/32'). Required whenever "
            "--rate-limit-trust-x-forwarded-for is set; otherwise "
            "the API refuses to start (fail-closed). Can also be "
            "set via OSIMFLOW_TRUSTED_PROXIES."
        ),
    )
    serve.add_argument(
        "--tls-cert",
        default=None,
        type=Path,
        help=(
            "Path to a PEM-encoded TLS certificate file. "
            "When provided, --tls-key is also required and HTTPS is enabled. "
            "SEC-004: TLS is required for production deployments."
        ),
    )
    serve.add_argument(
        "--tls-key",
        default=None,
        type=Path,
        help=(
            "Path to a PEM-encoded TLS private key file. "
            "Required when --tls-cert is provided. "
            "SEC-004: TLS is required for production deployments."
        ),
    )
    serve.add_argument(
        "--read-write",
        dest="read_only",
        action="store_false",
        help="Allow campaign control (stop, live events). Disables --read-only.",
    )
    serve.add_argument(
        "--ui",
        action="store_true",
        default=False,
        help="Enable the campaign setup web UI at /ui/ (issue #337).",
    )
    serve.add_argument(
        "--editor",
        action="store_true",
        default=False,
        help=(
            "Enable the Variable Designer web UI at /ui/designer/ "
            "for editing variable YAML files. (issue #587)"
        ),
    )
    serve.add_argument(
        "--dashboard",
        action="store_true",
        default=False,
        help=(
            "Also launch the Streamlit results dashboard on port 8501. "
            "Requires osimflow[viz] extra. (issue #383)"
        ),
    )
    serve.add_argument("--log_level", default="INFO")
    serve.add_argument(
        "--registry",
        default=None,
        type=Path,
        help=(
            "Path to the campaign registry database (issue #404). "
            "When set, the POST /api/v1/campaigns/compare endpoint "
            "can resolve campaign IDs via the registry. "
            "Default: ~/.osimflow/registry.db"
        ),
    )
    serve.add_argument(
        "--api-redis-url",
        default=None,
        help=(
            "Redis URL for distributed rate limiting across multiple API instances "
            "behind a load balancer (issue #663).  Example: redis://localhost:6379/0. "
            "When not set, the in-process limiter is used (per-process only)."
        ),
    )
    serve.set_defaults(func=_cmd_serve)


def _add_dashboard_args(dash: argparse.ArgumentParser) -> None:
    dash.add_argument(
        "outdir",
        type=Path,
        help="Campaign output directory to visualise",
    )
    dash.add_argument(
        "--port",
        type=int,
        default=8501,
        help="Port for the local dashboard (default: 8501)",
    )
    dash.add_argument("--log_level", default="INFO")


def _add_from_s3_args(p: argparse.ArgumentParser) -> None:
    """Flags shared by ``status`` / ``list`` / ``download`` for S3-backed state."""
    p.add_argument(
        "--from-s3",
        dest="from_s3",
        action="store_true",
        default=False,
        help="Read detached-campaign state from the S3 result bucket (issue #1873)",
    )
    p.add_argument("--result-storage-bucket", default=None, help="S3 bucket (with --from-s3)")
    p.add_argument(
        "--result-storage-endpoint", default=None, help="Custom S3 endpoint (with --from-s3)"
    )
    p.add_argument(
        "--allow-insecure-storage-endpoint",
        action="store_true",
        default=False,
        help="Allow a plaintext http:// S3 endpoint (with --from-s3)",
    )


def _add_list_args(lst: argparse.ArgumentParser) -> None:
    _add_from_s3_args(lst)
    lst.add_argument("--prefix", default="", help="Campaign id prefix filter (with --from-s3)")
    lst.add_argument(
        "--status",
        default=None,
        help="Filter by campaign status (running, success, failure)",
    )
    lst.add_argument(
        "--project",
        default=None,
        help="Filter by project name (issue #390)",
    )
    lst.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of campaigns to show (default: 50)",
    )
    lst.add_argument(
        "--registry",
        default=None,
        type=Path,
        help="Path to the campaign registry database (default: ~/.osimflow/registry.db)",
    )
    lst.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    lst.add_argument("--log_level", default="INFO")


def _add_show_args(show: argparse.ArgumentParser) -> None:
    show.add_argument(
        "campaign_id",
        help="Campaign ID to show details for",
    )
    show.add_argument(
        "--registry",
        default=None,
        type=Path,
        help="Path to the campaign registry database (default: ~/.osimflow/registry.db)",
    )
    show.add_argument("--log_level", default="INFO")


def _add_compare_args(cmp: argparse.ArgumentParser) -> None:
    cmp.add_argument(
        "id1",
        nargs="?",
        help="First campaign ID (used when --outdirs is not given)",
    )
    cmp.add_argument(
        "id2",
        nargs="?",
        help="Second campaign ID (used when --outdirs is not given)",
    )
    cmp.add_argument(
        "--registry",
        default=None,
        type=Path,
        help="Path to the campaign registry database (default: ~/.osimflow/registry.db)",
    )
    cmp.add_argument(
        "--outdirs",
        nargs="+",
        type=Path,
        metavar="OUTDIR",
        help="Two or more campaign output directories to compare using KPI data "
        "(ignores registry). Use this to compare campaigns that are not in the registry.",
    )
    cmp.add_argument(
        "--labels",
        nargs="+",
        metavar="LABEL",
        help="Optional labels for each --outdirs path (must match the number of paths).",
    )
    cmp.add_argument(
        "--kpis",
        nargs="*",
        metavar="KPI",
        help="Restrict comparison to these KPI names (all KPIs if omitted).",
    )
    cmp.add_argument(
        "--export",
        type=Path,
        metavar="CSV",
        help="Export combined results to the given CSV path.",
    )
    cmp.add_argument("--log_level", default="INFO")


def _add_aggregate_runs_args(agr: argparse.ArgumentParser) -> None:
    agr.add_argument(
        "outdirs",
        nargs="+",
        type=Path,
        metavar="OUTDIR",
        help="Two or more campaign output directories to aggregate",
    )
    agr.add_argument(
        "--labels",
        nargs="*",
        metavar="LABEL",
        help="Optional labels for each outdir "
        "(defaults to campaign_id from run.json or outdir stem)",
    )
    agr.add_argument(
        "--output",
        type=Path,
        metavar="CSV",
        help="Path to write the combined aggregated CSV "
        "(default: <first_outdir>/../aggregated_combined.csv)",
    )
    agr.add_argument(
        "--kpis",
        nargs="*",
        metavar="KPI",
        help="Restrict output to these KPI names (all KPIs if omitted)",
    )
    agr.add_argument("--log_level", default="INFO")


def _add_run_batches_args(rb: argparse.ArgumentParser) -> None:
    rb.add_argument("--manifest", type=Path, required=True, help="YAML/JSON batch manifest")
    rb.add_argument(
        "--root",
        type=Path,
        default=Path("batches"),
        help="Directory holding Batch<id>_<name> campaign dirs, the submit lock and the summary",
    )
    rb.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep running later batches after one fails (default: skip the rest)",
    )
    rb.add_argument(
        "--submit-lock-timeout-s",
        type=float,
        default=3600.0,
        help="Seconds to wait for the submit lock before giving up (default: 3600)",
    )
    rb.add_argument(
        "--dry-run", action="store_true", help="Print each batch's `osimflow run` command only"
    )
    rb.add_argument("--log_level", default="INFO")


def _add_status_args(st: argparse.ArgumentParser) -> None:
    _add_from_s3_args(st)
    st.add_argument(
        "outdir",
        type=Path,
        help="Campaign output directory containing run.json",
    )
    st.add_argument("--log_level", default="INFO")


def _add_download_args(dl: argparse.ArgumentParser) -> None:
    _add_from_s3_args(dl)
    dl.add_argument(
        "--allow-partial",
        action="store_true",
        help="With --from-s3: download even if some samples are not complete",
    )
    dl.add_argument(
        "outdir",
        type=Path,
        help="Campaign output directory to download results from",
    )
    dl.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Destination directory (default: <outdir>-downloads/<campaign_id>)",
    )
    dl.add_argument(
        "--include-intermediates",
        action="store_true",
        help="Also download per-sample .osw/.osm and eplusout.sql files",
    )
    dl.add_argument("--log_level", default="INFO")


def _add_cancel_args(cancel: argparse.ArgumentParser) -> None:
    cancel.add_argument(
        "outdir",
        type=Path,
        help="Campaign output directory containing run.json",
    )
    cancel.add_argument("--log_level", default="INFO")


def _add_mark_for_reanalysis_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments for the mark-for-reanalysis command (issue #420)."""
    parser.add_argument(
        "outdir",
        type=Path,
        help="Campaign output directory containing run.json and .osimflow/data_points.json",
    )
    parser.add_argument(
        "sample_id",
        type=str,
        help="Sample ID to re-analyze (must be COMPLETED or FAILED)",
    )
    parser.add_argument(
        "--priority",
        type=int,
        default=0,
        help="Priority of the new reanalysis sample (default: 0)",
    )
    parser.add_argument("--log_level", default="INFO")


def _add_merge_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments for the merge command (issue #418)."""
    parser.add_argument(
        "outdir",
        type=Path,
        help="Campaign output directory containing .osimflow/data_points.json",
    )
    parser.add_argument(
        "--source-ids",
        type=str,
        nargs="+",
        required=True,
        help="Source sample IDs to merge (at least one required)",
    )
    parser.add_argument(
        "--target-id",
        type=str,
        required=True,
        help="Target sample ID for the merged result",
    )
    parser.add_argument(
        "--target-work-dir",
        type=Path,
        required=True,
        help="Path to the target sample's work directory",
    )
    parser.add_argument("--log_level", default="INFO")


def _add_pause_args(pause: argparse.ArgumentParser) -> None:
    pause.add_argument(
        "outdir",
        type=Path,
        help="Campaign output directory containing run.json",
    )
    pause.add_argument("--log_level", default="INFO")


def _add_resume_args(resume: argparse.ArgumentParser) -> None:
    resume.add_argument(
        "outdir",
        type=Path,
        help="Campaign output directory containing run.json",
    )
    resume.add_argument("--log_level", default="INFO")


def _add_backup_args(bk: argparse.ArgumentParser) -> None:
    bk.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Output path for the backup file. "
            "Default: auto-generated timestamped file in <registry_dir>/backups/. "
            "(issue #440)"
        ),
    )
    bk.add_argument(
        "--registry",
        default=None,
        type=Path,
        help="Path to the campaign registry database (default: ~/.osimflow/registry.db)",
    )
    bk.add_argument("--log_level", default="INFO")


def _add_restore_args(rs: argparse.ArgumentParser) -> None:
    rs.add_argument(
        "backup_file",
        type=Path,
        help="Path to the backup SQLite database to restore from",
    )
    rs.add_argument(
        "--registry",
        default=None,
        type=Path,
        help="Path to the campaign registry database (default: ~/.osimflow/registry.db)",
    )
    rs.add_argument(
        "--merge",
        action="store_true",
        help=(
            "Merge backup records into the existing registry instead of "
            "replacing all records. Existing records with the same id "
            "are overwritten; new records are inserted. (issue #440)"
        ),
    )
    rs.add_argument("--log_level", default="INFO")


def _add_health_args(health: argparse.ArgumentParser) -> None:
    health.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help=(
            "Directory to check write permissions and disk space in (default: current directory)."
        ),
    )
    health.add_argument(
        "--json",
        action="store_true",
        help="Output results as machine-readable JSON instead of a table.",
    )
    health.add_argument(
        "--offline",
        action="store_true",
        help="Skip the network connectivity check.",
    )
    health.add_argument(
        "--executor",
        default=None,
        help=(
            "Name of the executor the campaign will dispatch with (e.g. 'local', "
            "'slurm', 'aws_batch'). Promotes that executor's health check from "
            "INFORMATIONAL to CRITICAL so a missing substrate fails the health "
            "subcommand instead of merely warning. (issue #1024)"
        ),
    )
    health.add_argument(
        "--redis-url",
        default=None,
        help=(
            "Probe the deployment mode of the Redis instance behind this "
            "URL (issue #1562 / ADR-0004). Reports single / sentinel / "
            "cluster / unknown based on a short PING + INFO probe. "
            "Single-instance Redis is the only topology the OSimFlow "
            "client path currently supports; the campaign-restart-by-"
            "replay recovery path is documented in user-guide.md §7.7. "
            "When omitted, the Redis check SKIPs."
        ),
    )
    health.add_argument("--log_level", default="ERROR")


def _add_measure_args(measure: argparse.ArgumentParser) -> None:
    measure.add_argument(
        "action",
        choices=["list"],
        help="Action to perform (only 'list' is currently supported).",
    )
    measure.add_argument(
        "--template",
        type=Path,
        required=True,
        help="Path to the template simulation package (must contain a measures/ directory).",
    )
    measure.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table).",
    )
    measure.add_argument("--log_level", default="INFO")


def _add_query_results_args(qr: argparse.ArgumentParser) -> None:
    """Add arguments for the query-results command (issue #585)."""
    qr.add_argument(
        "--campaign-ids",
        type=str,
        default=None,
        help=(
            "Comma-separated campaign IDs to query. "
            "If not provided, queries all campaigns in the current directory."
        ),
    )
    qr.add_argument(
        "--outdirs",
        type=str,
        default=None,
        help=(
            "Comma-separated output directory paths to query (alternative to --campaign-ids). "
            "Each path must point to a campaign output directory."
        ),
    )
    qr.add_argument(
        "--filter",
        type=str,
        default=None,
        dest="filter_expr",
        help=(
            "Filter expression in 'column op value' format, e.g., 'eui > 100'. "
            "Supports: >, <, >=, <=, ==, !=. "
            "Can be specified multiple times; separate expressions with semicolons. "
            "Example: 'eui > 100; status == ok'"
        ),
    )
    qr.add_argument(
        "--page",
        type=int,
        default=1,
        help="Page number (1-indexed, default: 1)",
    )
    qr.add_argument(
        "--per-page",
        type=int,
        default=100,
        help="Items per page (default: 100, max: 1000)",
    )
    qr.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    qr.add_argument(
        "--log_level",
        default="INFO",
    )


def _add_export_results_args(er: argparse.ArgumentParser) -> None:
    """Add arguments for the export-results command (issue #585)."""
    er.add_argument(
        "--campaign-ids",
        type=str,
        default=None,
        help=(
            "Comma-separated campaign IDs to export. "
            "If not provided, exports all campaigns in the current directory."
        ),
    )
    er.add_argument(
        "--outdirs",
        type=str,
        default=None,
        help=(
            "Comma-separated output directory paths to export (alternative to --campaign-ids). "
            "Each path must point to a campaign output directory."
        ),
    )
    er.add_argument(
        "--filter",
        type=str,
        default=None,
        dest="filter_expr",
        help=(
            "Filter expression in 'column op value' format. "
            "Can be specified multiple times; separate expressions with semicolons."
        ),
    )
    er.add_argument(
        "--format",
        choices=["csv", "json", "openstudio-server-csv"],
        default="csv",
        help=(
            "Export format (default: csv). 'openstudio-server-csv' writes the "
            "openstudio-server download_data.csv layout (issue #1872)."
        ),
    )
    er.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output file path. If not provided, prints to stdout.",
    )
    er.add_argument(
        "--include-failed",
        action="store_true",
        default=True,
        help="Include failed simulations in the export (default: True)",
    )
    er.add_argument(
        "--no-include-failed",
        action="store_false",
        dest="include_failed",
        help="Exclude failed simulations from the export.",
    )
    er.add_argument(
        "--log_level",
        default="INFO",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="osimflow",
        description="OSimFlow — parametric OpenStudio simulation campaigns",
    )
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run a campaign")
    _add_run_args(run)
    warm = sub.add_parser("warm-cache", help="Pre-populate the simulation cache before a campaign")
    _add_run_args(warm)
    warm.add_argument(
        "--n_warm",
        type=int,
        default=10,
        help="Number of pilot samples to run for cache warming (default: 10)",
    )
    imp = sub.add_parser(
        "import-osa",
        help="Import an OpenStudio Analysis (.osa / analysis.json) file",
    )
    _add_import_osa_args(imp)
    imp_gem = sub.add_parser(
        "import-gem-osa",
        help="Import openstudio-bem-to-surrogate-gem parametric_space/osa_workflow files",
    )
    _add_import_gem_osa_args(imp_gem)
    exp = sub.add_parser(
        "export",
        help="Export campaign state to an external format",
    )
    _add_export_args(exp)
    serve = sub.add_parser("serve", help="Start REST API server")
    _add_serve_args(serve)
    dash = sub.add_parser(
        "dashboard",
        help="Launch local ephemeral dashboard for campaign results",
    )
    _add_dashboard_args(dash)
    lst = sub.add_parser(
        "list",
        help="List all registered campaigns",
    )
    _add_list_args(lst)
    show = sub.add_parser(
        "show",
        help="Show detailed info for a campaign",
    )
    _add_show_args(show)
    cmp = sub.add_parser(
        "compare",
        help="Compare two campaigns side by side",
    )
    _add_compare_args(cmp)
    agr = sub.add_parser(
        "aggregate-runs",
        help="Aggregate KPI results from two or more campaign runs into a combined dataset "
        "and print cross-run statistics (issue #588)",
    )
    _add_aggregate_runs_args(agr)
    st = sub.add_parser(
        "status",
        help="Show detailed status of a campaign (reads run.json)",
    )
    _add_status_args(st)
    dl = sub.add_parser(
        "download",
        help="Download results from a completed campaign",
    )
    _add_download_args(dl)
    cncl = sub.add_parser(
        "cancel",
        help="Request graceful cancellation of a running campaign",
    )
    _add_cancel_args(cncl)
    rb = sub.add_parser(
        "run-batches",
        help="Run several named batches sequentially as separate campaigns (issue #1874)",
    )
    _add_run_batches_args(rb)
    reanl = sub.add_parser(
        "mark-for-reanalysis",
        help="Mark a completed/failed sample for re-running (issue #420)",
    )
    _add_mark_for_reanalysis_args(reanl)
    mrg = sub.add_parser(
        "merge",
        help="Merge multiple data points into a single target (issue #418)",
    )
    _add_merge_args(mrg)
    pause_cmd = sub.add_parser(
        "pause",
        help="Request graceful pause of a running campaign (issue #444)",
    )
    _add_pause_args(pause_cmd)
    resume_cmd = sub.add_parser(
        "resume",
        help="Resume a paused campaign: clear .pause and re-launch the recorded run (cache replay; issue #1628)",
    )
    _add_resume_args(resume_cmd)
    bk = sub.add_parser(
        "backup",
        help="Create a backup of the campaign registry (issue #440)",
    )
    _add_backup_args(bk)
    rs = sub.add_parser(
        "restore",
        help="Restore/import the campaign registry from a backup (issue #440)",
    )
    _add_restore_args(rs)
    health = sub.add_parser(
        "health",
        help="Verify system health before starting a campaign (issue #411)",
    )
    _add_health_args(health)
    measure = sub.add_parser(
        "measure",
        help="Discover and inspect measures from a template simulation package (issue #532)",
    )
    _add_measure_args(measure)
    qr = sub.add_parser(
        "query-results",
        help="Query aggregated results across multiple campaigns (issue #585)",
    )
    _add_query_results_args(qr)
    er = sub.add_parser(
        "export-results",
        help="Export aggregated results to CSV or JSON (issue #585)",
    )
    _add_export_results_args(er)
    return p


def _get_arg_defaults() -> dict[str, object]:
    """Return a dict of {dest: default} for the run subcommand.

    Used by _apply_preset to determine which values are parser defaults
    (and thus safe to override) vs user-provided values.
    """
    # Build only the run subparser without triggering the full parser
    # construction that requires all command handlers to be defined.
    p = argparse.ArgumentParser()
    run = p.add_subparsers().add_parser("run")
    _add_run_args(run)
    defaults: dict[str, object] = {}
    for action in run._actions:
        if hasattr(action, "dest") and action.dest:
            defaults[action.dest] = action.default
    return defaults


class _PresetDefaultCache:
    """Lazily built on first use by _apply_preset."""

    _cache: dict[str, object] | None = None

    @classmethod
    def get(cls) -> dict[str, object]:
        if cls._cache is None:
            cls._cache = _get_arg_defaults()
        return cls._cache
