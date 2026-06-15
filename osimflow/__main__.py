"""CLI entry point for OSimFlow.

Usage:
    python -m osimflow run \\
        --executor local \\
        --input_variables variables.yml \\
        --template_sim_package ./example_package \\
        --n_samples 10 \\
        --outdir ./results \\
        --openstudio_version 3.11.0

After `pip install -e .`, also available as:
    osimflow run --executor local ...
"""

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

from osimflow import (
    AWSBatchExecutor,
    AzureBatchExecutor,
    BaseExecutor,
    Campaign,
    CampaignConfig,
    CampaignRecord,
    CampaignRegistry,
    DaskJobQueueExecutor,
    GoogleBatchExecutor,
    KubernetesExecutor,
    LocalExecutor,
    NomadExecutor,
    PBSExecutor,
    SlurmExecutor,
    build_task_queue,
    load_config,
)
from osimflow.byos import ByosTrustLevel, load_user_function
from osimflow.exporters.osa import OSAExporter
from osimflow.importers.osa import OSAImportError, osa_to_variables_yml, parse_osa

log = logging.getLogger("osimflow.__main__")

# Preset configurations for common use cases (issue #384).
# Each preset bundles a set of flags that reduce the 50+ CLI surface
# to a few sensible choices for a specific environment/scale.
# Individual flags always override preset values.
PRESETS: dict[str, dict[str, object]] = {
    # local-quick: small local campaign for learning/testing
    "local-quick": {
        "executor": "local",
        "max_workers": 2,
        "openstudio_version": "3.11.0",
        "algorithm": "lhs",
        "max_generations": 1,
    },
    # local-large: larger local campaign with more parallelism
    "local-large": {
        "executor": "local",
        "max_workers": 8,
        "openstudio_version": "3.11.0",
        "algorithm": "lhs",
        "max_generations": 1,
    },
    # slurm-hpc: production campaign on a real Slurm cluster
    "slurm-hpc": {
        "executor": "slurm",
        "slurm_real": True,
        "slurm_partition": "short",
        "openstudio_version": "3.11.0",
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
        "openstudio_version": "3.11.0",
        "algorithm": "lhs",
        "max_generations": 1,
    },
    # aws-batch-cloud: large-scale campaign on AWS Batch
    "aws-batch-cloud": {
        "executor": "aws_batch",
        "aws_batch_queue": "osimflow-batch-queue",
        "aws_batch_fallback_to_on_demand": True,
        "aws_batch_max_retries": 3,
        "openstudio_version": "3.11.0",
        "algorithm": "lhs",
        "max_generations": 1,
    },
    # sensitivity-morris: Morris sensitivity analysis (SALib)
    "sensitivity-morris": {
        "executor": "local",
        "max_workers": 4,
        "openstudio_version": "3.11.0",
        "algorithm": "morris",
        "max_generations": 1,
    },
    # sensitivity-fast99: FAST99 sensitivity analysis (SALib)
    "sensitivity-fast99": {
        "executor": "local",
        "max_workers": 4,
        "openstudio_version": "3.11.0",
        "algorithm": "fast99",
        "max_generations": 1,
    },
    # optimization-de: Differential evolution optimization
    "optimization-de": {
        "executor": "slurm",
        "slurm_real": True,
        "slurm_partition": "short",
        "openstudio_version": "3.11.0",
        "algorithm": "de",
        "max_generations": 50,
    },
    # optimization-nsga2: NSGA-II multi-objective optimization
    "optimization-nsga2": {
        "executor": "slurm",
        "slurm_real": True,
        "slurm_partition": "short",
        "openstudio_version": "3.11.0",
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


def _build_executor(args: argparse.Namespace) -> BaseExecutor:  # noqa: PLR0911
    """Dispatch to the correct executor based on ``args.executor``."""
    # Local executor — no extra config needed.
    if args.executor == "local":
        return LocalExecutor(max_workers=args.max_workers)
    # Slurm executor — partition, account, and submitit debug flag.
    if args.executor == "slurm":
        return SlurmExecutor(
            partition=args.slurm_partition,
            account=args.slurm_account,
            cpus_per_task=2,
            mem_gb=4,
            time_h=2,
            debug=not args.slurm_real,  # debug unless --slurm_real
            qos=args.slurm_qos,
            constraint=args.slurm_constraint,
            gres=args.slurm_gres,
        )
    # AWS Batch executor — job queue, job definition, and Spot handling.
    if args.executor == "aws_batch":
        return AWSBatchExecutor(
            job_queue=args.aws_batch_queue,
            job_definition=args.aws_batch_job_definition,
            max_spot_price_usd=(
                args.aws_batch_max_spot_price_usd
                if args.aws_batch_max_spot_price_usd is not None
                else None
            ),
            fallback_to_on_demand=args.aws_batch_fallback_to_on_demand,
            max_retries=args.aws_batch_max_retries,
        )
    # Nomad executor — address and datacentre.
    if args.executor == "nomad":
        return NomadExecutor(
            address=args.nomad_address,
            datacentre=args.nomad_datacentre,
            verify_tls=args.nomad_tls_verify,
            tls=args.nomad_tls,
            cert=args.nomad_cert,
            key=args.nomad_key,
            ca_cert=args.nomad_ca_cert,
        )
    # Azure Batch executor — account credentials, pool, and Spot handling.
    if args.executor == "azure_batch":
        return AzureBatchExecutor(
            account_name=args.azure_batch_account_name,
            account_url=args.azure_batch_account_url,
            pool_id=args.azure_batch_pool_id,
            location=args.azure_batch_location,
            use_spot=args.azure_use_spot,
            fallback_to_on_demand=args.azure_fallback_to_on_demand,
            max_retries=args.azure_max_retries,
        )
    # Google Cloud Batch executor — project, region, service account, and Spot handling.
    if args.executor == "google_batch":
        return GoogleBatchExecutor(
            project_id=args.google_batch_project_id,
            region=args.google_batch_region,
            batch_service_account=args.google_batch_service_account,
            use_spot=args.google_use_spot,
            fallback_to_on_demand=args.google_fallback_to_on_demand,
            max_retries=args.google_max_retries,
        )
    # Kubernetes executor — namespace and polling config.
    if args.executor == "kubernetes":
        return KubernetesExecutor(
            namespace=args.kubernetes_namespace,
            poll_interval_s=args.kubernetes_poll_interval_s,
            max_poll_interval_s=args.kubernetes_max_poll_interval_s,
        )
    # PBS executor — server, queue, and debug flag.
    if args.executor == "pbs":
        return PBSExecutor(
            server=args.pbs_server,
            queue=args.pbs_queue,
            debug=not args.pbs_real,  # debug unless --pbs-real
        )
    if args.executor == "dask_jobqueue":
        return DaskJobQueueExecutor(
            cluster_type=args.dask_cluster_type,
            min_workers=args.dask_min_workers,
            max_workers=args.dask_max_workers,
            cpus_per_worker=args.dask_cpus_per_worker,
            memory_per_worker=args.dask_memory_per_worker,
            walltime=args.dask_walltime,
            queue=args.dask_queue,
            project=args.dask_project,
        )

    # Fall back to the ExecutorRegistry for plugin-discovered executors
    # (issue #432).  Third-party executors registered via entry_points
    # are available here.  They must accept no required constructor args
    # (or accept the same kwargs the built-ins do).
    from osimflow.executors import ExecutorRegistry  # noqa: PLC0415

    if args.executor in ExecutorRegistry.list_available():
        executor_cls = ExecutorRegistry.get(args.executor)
        log.info(
            "instantiating executor '%s' from registry (class=%s)",
            args.executor,
            executor_cls.__qualname__,
        )
        return executor_cls()

    raise ValueError(f"unknown executor: {args.executor}")


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
        ],
        default="local",
        help="Executor backend (default: local). See --preset for quick-start bundles.",
    )
    run.add_argument("--max-workers", type=int, default=4, help="Local executor parallelism")
    run.add_argument("--slurm-partition", default="short")
    run.add_argument("--slurm-account", default=None)
    run.add_argument(
        "--slurm-real",
        action="store_true",
        help="Submit to real Slurm (default: submitit DebugExecutor)",
    )
    run.add_argument(
        "--slurm-qos",
        default=None,
        help="Slurm QoS (e.g. 'high'). Requires submitit >= 1.5.",
    )
    run.add_argument(
        "--slurm-constraint",
        default=None,
        help="Slurm constraint feature (e.g. 'gpu'). Requires submitit >= 1.5.",
    )
    run.add_argument(
        "--slurm-gres",
        default=None,
        help="Slurm generic resources (e.g. 'gpu:1'). Requires submitit >= 1.5.",
    )
    run.add_argument("--aws-batch-queue", default="osimflow-batch-queue")
    run.add_argument("--aws-batch-job-definition", default=None)
    run.add_argument(
        "--aws-batch-max-spot-price-usd",
        type=float,
        default=None,
        help=(
            "Maximum Spot price ceiling in USD per vCPU-hour. "
            "When set, the executor checks the current Spot price before "
            "submitting and rejects jobs that would exceed the ceiling "
            "(unless --aws-batch-fallback-to-on-demand is also set)."
        ),
    )
    run.add_argument(
        "--aws-batch-fallback-to-on-demand",
        action="store_true",
        help=(
            "When the Spot price exceeds the ceiling or max retries are "
            "exhausted, fall back to the on-demand job queue instead of "
            "failing. Requires --aws-batch-max-spot-price-usd or spot "
            "interruption retries."
        ),
    )
    run.add_argument(
        "--aws-batch-max-retries",
        type=int,
        default=3,
        help=(
            "Maximum number of retries on Spot interruption (default: 3). "
            "Each retry uses exponential backoff. After exhausting retries, "
            "the job fails unless --aws-batch-fallback-to-on-demand is set."
        ),
    )
    run.add_argument(
        "--ecr-repository",
        default=None,
        help=(
            "ECR repository URI for OpenStudio container images "
            "(e.g. 123456.dkr.ecr.us-east-1.amazonaws.com/osimflow/openstudio). "
            "When set, the Batch executor pulls from ECR instead of Docker Hub."
        ),
    )
    run.add_argument(
        "--nomad-address",
        default=None,
        help=(
            "Nomad cluster HTTP address (e.g. http://nomad.local:4646). "
            "Defaults to the NOMAD_ADDR env var or http://127.0.0.1:4646."
        ),
    )
    run.add_argument(
        "--nomad-datacentre",
        default="dc1",
        help="Nomad datacentre to target (default: dc1).",
    )
    run.add_argument(
        "--nomad-tls-verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Enable (default) or disable TLS certificate verification for the "
            "Nomad HTTP API. Disable with --nomad-tls-verify=false for "
            "development with self-signed certificates. "
            "SEC-009: protects NOMAD_TOKEN from interception."
        ),
    )
    run.add_argument(
        "--nomad-tls",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Enable TLS for the Nomad HTTP API connection. "
            "When enabled, use --nomad-cert, --nomad-key, and --nomad-ca-cert "
            "to specify client certificate files for mTLS authentication."
        ),
    )
    run.add_argument(
        "--nomad-cert",
        default=None,
        help=(
            "Path to the client certificate file (PEM) for mTLS authentication "
            "with the Nomad cluster. Required when --nomad-tls is enabled."
        ),
    )
    run.add_argument(
        "--nomad-key",
        default=None,
        help=(
            "Path to the client private key file (PEM) for mTLS authentication "
            "with the Nomad cluster. Required when --nomad-tls is enabled."
        ),
    )
    run.add_argument(
        "--nomad-ca-cert",
        default=None,
        help=(
            "Path to the CA certificate file (PEM) to verify the Nomad server's "
            "certificate when --nomad-tls is enabled. If not specified, the "
            "system default CA certificates are used."
        ),
    )
    run.add_argument(
        "--azure-batch-account-name",
        default=None,
        help="Azure Batch account name (e.g. osimflowbatch).",
    )
    run.add_argument(
        "--azure-batch-account-url",
        default=None,
        help="Azure Batch account URL (e.g. https://osimflowbatch.eastus.batch.azure.com).",
    )
    run.add_argument(
        "--azure-batch-pool-id",
        default="osimflow-pool",
        help="Azure Batch pool ID (default: osimflow-pool).",
    )
    run.add_argument(
        "--azure-batch-location",
        default="eastus",
        help="Azure region/location for the Batch account (default: eastus).",
    )
    run.add_argument(
        "--azure-use-spot",
        action="store_true",
        help=(
            "Use Azure Spot VMs (low-priority) for Batch tasks (issue #352). "
            "When a Spot interruption occurs, the executor retries up to "
            "--azure-max-retries times before falling back to on-demand "
            "(if --azure-fallback-to-on-demand is set) or failing."
        ),
    )
    run.add_argument(
        "--azure-fallback-to-on-demand",
        action="store_true",
        help=(
            "When Azure Spot retries are exhausted, fall back to on-demand "
            "VMs instead of failing (issue #352). Requires --azure-use-spot."
        ),
    )
    run.add_argument(
        "--azure-max-retries",
        type=int,
        default=3,
        help=(
            "Maximum number of retries on Azure Spot VM interruption "
            "(default: 3). Each retry uses exponential backoff. After "
            "exhausting retries, the job fails unless "
            "--azure-fallback-to-on-demand is set."
        ),
    )
    run.add_argument(
        "--google-batch-project-id",
        default=None,
        help="Google Cloud project ID (e.g. my-project).",
    )
    run.add_argument(
        "--google-batch-region",
        default="us-central1",
        help="Google Cloud region for Batch jobs (default: us-central1).",
    )
    run.add_argument(
        "--google-batch-service-account",
        default=None,
        help="Google Cloud service account email for Batch jobs.",
    )
    run.add_argument(
        "--google-use-spot",
        action="store_true",
        help=(
            "Use Google Spot VMs (preemptible) for Batch jobs (issue #352). "
            "When a preemptible VM is interrupted, the executor retries up to "
            "--google-max-retries times before falling back to on-demand "
            "(if --google-fallback-to-on-demand is set) or failing."
        ),
    )
    run.add_argument(
        "--google-fallback-to-on-demand",
        action="store_true",
        help=(
            "When Google Spot/preemptible retries are exhausted, fall back to "
            "on-demand VMs instead of failing (issue #352). "
            "Requires --google-use-spot."
        ),
    )
    run.add_argument(
        "--google-max-retries",
        type=int,
        default=3,
        help=(
            "Maximum number of retries on Google preemptible VM interruption "
            "(default: 3). Each retry uses exponential backoff. After "
            "exhausting retries, the job fails unless "
            "--google-fallback-to-on-demand is set."
        ),
    )
    run.add_argument(
        "--kubernetes-namespace",
        default="default",
        help="Kubernetes namespace for jobs (default: default).",
    )
    run.add_argument(
        "--kubernetes-poll-interval-s",
        type=float,
        default=5.0,
        help="Poll interval for Job status (seconds, default: 5.0).",
    )
    run.add_argument(
        "--kubernetes-max-poll-interval-s",
        type=float,
        default=60.0,
        help="Max poll interval for Job status (seconds, default: 60.0).",
    )
    run.add_argument(
        "--pbs-server",
        default=None,
        help=(
            "PBS server/cluster address (e.g. pbsserver). "
            "Defaults to the PBS_DEFAULT env var or system default."
        ),
    )
    run.add_argument(
        "--pbs-queue",
        default=None,
        help="PBS queue to submit jobs to (e.g. batch).",
    )
    run.add_argument(
        "--pbs-real",
        action="store_true",
        help="Submit to real PBS (default: debug mode runs locally).",
    )
    run.add_argument(
        "--dask-cluster-type",
        choices=["slurm", "pbs", "kubernetes"],
        default="slurm",
        help="Dask-JobQueue cluster backend (default: slurm).",
    )
    run.add_argument(
        "--dask-min-workers",
        type=int,
        default=0,
        help="Minimum number of Dask workers to keep alive (default: 0).",
    )
    run.add_argument(
        "--dask-max-workers",
        type=int,
        default=10,
        help="Maximum number of Dask workers to scale up to (default: 10).",
    )
    run.add_argument(
        "--dask-cpus-per-worker",
        type=int,
        default=2,
        help="CPUs per Dask worker (default: 2).",
    )
    run.add_argument(
        "--dask-memory-per-worker",
        default="4GiB",
        help="Memory per Dask worker (default: 4GiB).",
    )
    run.add_argument(
        "--dask-walltime",
        default="02:00:00",
        help="Walltime for Dask cluster jobs (default: 02:00:00).",
    )
    run.add_argument(
        "--dask-queue",
        default=None,
        help=" HPC queue/partition for Dask workers (e.g. short, gpu).",
    )
    run.add_argument(
        "--dask-project",
        default=None,
        help="HPC project/account for Dask workers.",
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
    run.add_argument("--input_variables", required=True)
    run.add_argument("--template_sim_package", required=True)
    run.add_argument("--n_samples", type=int, required=True)
    run.add_argument("--outdir", required=True)
    run.add_argument("--openstudio_version", default="3.11.0")
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
    run.add_argument("--log_level", default="INFO")
    run.add_argument(
        "--algorithm",
        default="lhs",
        help=(
            "Sampling algorithm to use (default: lhs). "
            "Available algorithms are registered in AlgorithmRegistry."
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
        "--max-step-retries",
        type=int,
        default=2,
        help=(
            "Maximum retry attempts for transient cross-step failures "
            "(issue #416). When a fan-out step (APPLY_PARAMETERS, "
            "RUN_OPENSTUDIO_SIM, EXTRACT_KPIS) fails with a transient "
            "error, retry that specific step before aborting. Default: 2. "
            "Set to 0 to disable cross-step retries. Only transient errors "
            "(network timeout, resource exhaustion, container crashes) trigger "
            "retry; permanent errors (invalid input, missing files) "
            "abort immediately."
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
        "--no-tui",
        action="store_true",
        help=(
            "Disable the rich terminal UI even when rich is installed "
            "and stdout is a TTY. Useful when piping output or running "
            "in environments where the TUI causes display issues."
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
        default="3.11.0",
        help="OpenStudio CLI version (default: 3.11.0)",
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
            "Required when --enable-writes is set "
            "(auto-generated and logged if not provided). When read-only, "
            "authentication is disabled unless this is set. "
            "Use --api-keys-file for multi-user authentication (issue #395)."
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
            "Roles: readonly, readwrite, admin."
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


def _add_list_args(lst: argparse.ArgumentParser) -> None:
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
        help="First campaign ID",
    )
    cmp.add_argument(
        "id2",
        help="Second campaign ID",
    )
    cmp.add_argument(
        "--registry",
        default=None,
        type=Path,
        help="Path to the campaign registry database (default: ~/.osimflow/registry.db)",
    )
    cmp.add_argument("--log_level", default="INFO")


def _add_status_args(st: argparse.ArgumentParser) -> None:
    st.add_argument(
        "outdir",
        type=Path,
        help="Campaign output directory containing run.json",
    )
    st.add_argument("--log_level", default="INFO")


def _add_download_args(dl: argparse.ArgumentParser) -> None:
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
    health.add_argument("--log_level", default="ERROR")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="osimflow",
        description="OSimFlow — parametric OpenStudio simulation campaigns",
    )
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run a campaign")
    _add_run_args(run)
    imp = sub.add_parser(
        "import-osa",
        help="Import an OpenStudio Analysis (.osa / analysis.json) file",
    )
    _add_import_osa_args(imp)
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


def _cmd_serve(args: argparse.Namespace) -> int:
    """Start the REST API server."""
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError:
        print(
            "Error: osimflow[api] extra required. Install with: pip install osimflow[api]",
            file=sys.stderr,
        )
        return 1

    from osimflow.api import create_app, generate_api_key  # noqa: PLC0415

    # Resolve the API key.
    api_key = args.api_key
    api_keys_file = args.api_keys_file

    # When using multi-user mode, don't auto-generate a key
    if api_keys_file is not None:
        if api_key is not None:
            log.warning("--api-key is ignored when --api-keys-file is set")
        api_key = None
    elif args.enable_writes and not api_key:
        # Auto-generate a key when writes are enabled but none was provided.
        api_key = generate_api_key()
        log.warning("No --api-key provided; auto-generated key for write access: %s", api_key)

    # Parse CORS origins.
    cors_origins: list[str] | None = None
    if args.cors_origins:
        cors_origins = [o.strip() for o in args.cors_origins.split(",") if o.strip()]

    read_only = not args.enable_writes and args.read_only
    app = create_app(
        outdir=args.outdir,
        read_only=read_only,
        api_key=api_key,
        api_keys_file=api_keys_file,
        cors_origins=cors_origins,
        rate_limit=args.rate_limit,
        ui_enabled=args.ui,
        registry_path=args.registry,
    )
    if args.host not in ("127.0.0.1", "localhost"):
        log.warning("Binding to %s — the API is now network-accessible.", args.host)

    tls_cert: Path | None = args.tls_cert if args.tls_cert else None
    tls_key: Path | None = args.tls_key if args.tls_key else None

    if tls_cert is not None and tls_key is None:
        print("Error: --tls-key is required when --tls-cert is provided.", file=sys.stderr)
        return 1
    if tls_key is not None and tls_cert is None:
        print("Error: --tls-cert is required when --tls-key is provided.", file=sys.stderr)
        return 1

    # Launch Streamlit dashboard in a background thread if --dashboard is set.
    dashboard_thread: threading.Thread | None = None
    if args.dashboard:
        try:
            from osimflow.viz.dashboard import create_dashboard_app  # noqa: PLC0415
        except ImportError:
            print(
                "Error: osimflow[viz] extra required for --dashboard. "
                "Install with: pip install osimflow[viz]",
                file=sys.stderr,
            )
            return 1

        def run_dashboard() -> None:
            create_dashboard_app(outdir=args.outdir.resolve(), port=8501)

        dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
        dashboard_thread.start()
        log.info("Streamlit dashboard started on port 8501")

    if tls_cert is not None and tls_key is not None:
        log.warning("TLS enabled: serving on https://%s:%d", args.host, args.port)
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            ssl_certfile=tls_cert,
            ssl_keyfile=tls_key,
        )
    else:
        uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Launch the local ephemeral Streamlit dashboard."""
    outdir: Path = args.outdir.resolve()
    if not outdir.is_dir():
        print(f"error: {outdir} is not a directory", file=sys.stderr)
        return 1

    try:
        from osimflow.viz.dashboard import create_dashboard_app  # noqa: PLC0415
    except ImportError:
        print(
            "Error: osimflow[viz] extra required. Install with: pip install osimflow[viz]",
            file=sys.stderr,
        )
        return 1

    create_dashboard_app(outdir=outdir, port=args.port)
    return 0


def _run_import_osa(args: argparse.Namespace) -> int:
    try:
        osa_data = parse_osa(args.input)
        osa_to_variables_yml(osa_data, args.output)
    except (FileNotFoundError, OSAImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Converted {args.input} -> {args.output}")
    return 0


def _run_export(args: argparse.Namespace) -> int:
    variables_path = Path(args.variables).resolve()
    if not variables_path.exists():
        print(f"error: variables file not found: {variables_path}", file=sys.stderr)
        return 1

    outdir = Path(args.outdir).resolve()
    # Build a minimal CampaignConfig for the exporter.
    # The export subcommand only needs a subset of fields.
    cfg = CampaignConfig(
        input_variables=variables_path,
        template_sim_package=outdir,  # placeholder — not used by export
        n_samples=args.n_samples,
        outdir=outdir,
        openstudio_version=args.openstudio_version,
        algorithm=args.algorithm,
    )

    exporter = OSAExporter()
    path = exporter.export(cfg, outdir)
    print(f"Exported {path}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    """List all registered campaigns."""
    import json as json_mod  # noqa: PLC0415

    registry_path = args.registry if args.registry else None
    reg = CampaignRegistry(db_path=registry_path)
    campaigns = reg.list_campaigns(status=args.status, project=args.project, limit=args.limit)

    if not campaigns:
        print("No campaigns registered.")
        return 0

    if args.format == "json":
        print(
            json_mod.dumps(
                [c.to_dict() for c in campaigns],
                indent=2,
                default=str,
            )
        )
        return 0

    # table format (default)
    print(
        f"{'ID':<25} {'PROJECT':<20} {'STATUS':<10} {'ALGO':<8} {'N':<6} {'EXECUTOR':<10} {'CREATED'}"
    )
    print("-" * 100)
    for c in campaigns:
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at))
        project = c.project[:18] + ".." if len(c.project) > 20 else c.project
        print(
            f"{c.id:<25} {project:<20} {c.status:<10} {c.algorithm:<8} {c.n_samples:<6} "
            f"{c.executor:<10} {created}"
        )
    print(f"\nTotal: {len(campaigns)} campaign(s)")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Show detailed info for a single campaign."""
    import json as json_mod  # noqa: PLC0415

    registry_path = args.registry if args.registry else None
    reg = CampaignRegistry(db_path=registry_path)
    record = reg.get_campaign(args.campaign_id)

    if record is None:
        print(f"Campaign '{args.campaign_id}' not found in registry.", file=sys.stderr)
        return 1

    print(f"Campaign: {record.id}")
    print(f"  Name:               {record.name}")
    print(f"  Status:             {record.status}")
    print(f"  Output directory:   {record.outdir}")
    print(f"  Algorithm:          {record.algorithm}")
    print(f"  Samples:            {record.n_samples}")
    print(f"  Executor:           {record.executor}")
    print(f"  OpenStudio version: {record.openstudio_version}")
    created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created_at))
    print(f"  Created at:         {created}")
    if record.completed_at is not None:
        completed = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.completed_at))
        elapsed = record.completed_at - record.created_at
        print(f"  Completed at:       {completed}")
        print(f"  Duration:           {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    if record.metadata:
        print(f"  Metadata:           {json_mod.dumps(record.metadata, indent=4)}")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    """Compare two campaigns side by side."""
    registry_path = args.registry if args.registry else None
    reg = CampaignRegistry(db_path=registry_path)
    result = reg.compare(args.id1, args.id2)

    left = result["left"]
    right = result["right"]

    if left is None:
        print(f"Campaign '{args.id1}' not found in registry.", file=sys.stderr)
        return 1
    if right is None:
        print(f"Campaign '{args.id2}' not found in registry.", file=sys.stderr)
        return 1

    # Side-by-side comparison table
    fields = [
        ("ID", "id"),
        ("Status", "status"),
        ("Algorithm", "algorithm"),
        ("Samples", "n_samples"),
        ("Executor", "executor"),
        ("OS version", "openstudio_version"),
        ("Created", "_created_fmt"),
        ("Duration", "_duration_fmt"),
    ]

    col_w = 18
    print(f"{'Field':<16} {'Left':<{col_w}} {'Right':<{col_w}}")
    print("-" * (16 + col_w * 2 + 4))

    for label, key in fields:
        lv = _format_field(left, key)
        rv = _format_field(right, key)
        print(f"{label:<16} {lv:<{col_w}} {rv:<{col_w}}")

    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Show detailed status of a campaign by reading run.json."""
    import json as json_mod  # noqa: PLC0415

    outdir: Path = args.outdir.resolve()
    run_json_path = outdir / "run.json"

    if not run_json_path.exists():
        print(f"error: run.json not found in {outdir}", file=sys.stderr)
        return 1

    try:
        with open(run_json_path) as f:
            run_data = json_mod.load(f)
    except (OSError, json_mod.JSONDecodeError) as exc:
        print(f"error: could not read run.json: {exc}", file=sys.stderr)
        return 1

    # Top-level summary
    print(f"Campaign:   {run_data.get('campaign_id', 'unknown')}")
    print(f"Outdir:      {outdir}")
    status = run_data.get("status", "unknown")
    print(f"Status:      {status}")
    started = run_data.get("started_at", 0)
    if started:
        print(f"Started:     {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}")
    finished = run_data.get("finished_at")
    if finished:
        elapsed = finished - started if started else 0
        print(f"Finished:    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(finished))}")
        print(f"Elapsed:     {elapsed:.1f}s ({elapsed / 60:.1f} min)")

    # Steps table
    steps = run_data.get("steps", [])
    if steps:
        print("\nSteps:")
        print(f"  {'STEP':<30} {'CACHE':<10} {'ELAPSED':<10} {'EXIT'}")
        print(f"  {'-' * 30} {'-' * 10} {'-' * 10} {'-' * 5}")
        for s in steps:
            print(
                f"  {s.get('step', ''):<30} {s.get('cache', ''):<10} "
                f"{s.get('elapsed_s', 0):<10.2f} {s.get('exit_code', 0)}"
            )

    # Per-sample summary
    per_sample = run_data.get("per_sample", [])
    if per_sample:
        n_ok = sum(1 for s in per_sample if s.get("status") == "ok")
        n_failed = sum(1 for s in per_sample if s.get("status") == "failed")
        n_cached = sum(1 for s in per_sample if s.get("status") == "cached")
        print(
            f"\nSamples:    {len(per_sample)} total  |  {n_ok} ok  |  {n_failed} failed  |  {n_cached} cached"
        )

    # Generation info for iterative algorithms
    generations = run_data.get("generations", [])
    if generations:
        print("\nGenerations:")
        for g in generations:
            print(
                f"  gen {g.get('generation', '?')}: "
                f"{g.get('n_succeeded', 0)}/{g.get('n_samples', 0)} succeeded  "
                f"({g.get('elapsed_s', 0):.1f}s)"
            )

    print(f"\nrun.json:    {run_json_path}")
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    """Download results from a completed campaign to a destination directory."""
    import json as json_mod  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    outdir: Path = args.outdir.resolve()
    run_json_path = outdir / "run.json"

    if not run_json_path.exists():
        print(f"error: run.json not found in {outdir}", file=sys.stderr)
        return 1

    try:
        with open(run_json_path) as f:
            run_data = json_mod.load(f)
    except (OSError, json_mod.JSONDecodeError) as exc:
        print(f"error: could not read run.json: {exc}", file=sys.stderr)
        return 1

    campaign_id = run_data.get("campaign_id", outdir.name)
    output_dir: Path
    if args.output_dir:
        output_dir = args.output_dir.resolve()
    else:
        output_dir = outdir.parent / f"{outdir.name}-downloads" / campaign_id

    output_dir.mkdir(parents=True, exist_ok=True)
    log.info("Downloading campaign %s to %s", campaign_id, output_dir)

    # Always copy: run.json, aggregated_results.csv, failed_simulations.csv, plots/
    artifacts = [
        ("run.json", run_json_path),
    ]

    csv_path = outdir / "aggregated_results.csv"
    if csv_path.exists():
        artifacts.append(("aggregated_results.csv", csv_path))

    failed_csv = outdir / "failed_simulations.csv"
    if failed_csv.exists():
        artifacts.append(("failed_simulations.csv", failed_csv))

    plots_dir = outdir / "plots"
    if plots_dir.is_dir():
        dest_plots = output_dir / "plots"
        shutil.copytree(plots_dir, dest_plots, dirs_exist_ok=True)
        log.info("  plots/ -> %s", dest_plots)

    kpis_dir = outdir / "kpis"
    if kpis_dir.is_dir():
        dest_kpis = output_dir / "kpis"
        shutil.copytree(kpis_dir, dest_kpis, dirs_exist_ok=True)
        log.info("  kpis/ -> %s", dest_kpis)

    for name, src in artifacts:
        dest = output_dir / name
        shutil.copy2(src, dest)
        log.info("  %s -> %s", name, dest)

    # Optionally copy per-sample intermediates
    if args.include_intermediates:
        work_dir = outdir / "work"
        if work_dir.is_dir():
            dest_work = output_dir / "work"
            shutil.copytree(work_dir, dest_work, dirs_exist_ok=True)
            log.info("  work/ -> %s", dest_work)

    print(f"\nDownloaded campaign '{campaign_id}' to:\n  {output_dir}")
    return 0


def _format_field(record: CampaignRecord, key: str) -> str:
    """Format a campaign record field for display."""
    if key == "_created_fmt":
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(record.created_at))
    if key == "_duration_fmt":
        if record.completed_at is not None:
            elapsed = record.completed_at - record.created_at
            return f"{elapsed:.1f}s"
        return "(running)"
    return str(getattr(record, key, ""))


def _cmd_health(args: argparse.Namespace) -> int:
    """Run system health checks (issue #411)."""
    from osimflow.health import (  # noqa: PLC0415
        format_results,
        get_exit_code,
        run_health_checks,
        to_json,
    )

    outdir = args.outdir if args.outdir else Path.cwd()
    report = run_health_checks(outdir=outdir, skip_network=args.offline)

    if args.json:
        print(to_json(report))
    else:
        print(format_results(report))

    return get_exit_code(report)


def _cmd_cancel(args: argparse.Namespace) -> int:
    """Request graceful cancellation of a running campaign.

    Writes a ``.stop`` flag file to the campaign's output directory.
    The campaign orchestrator checks for this file between steps and
    initiates a graceful shutdown that stops accepting new samples and
    waits for in-flight samples to complete before writing the final
    ``run.json``.
    """
    import json as json_mod  # noqa: PLC0415

    outdir: Path = args.outdir.resolve()
    run_json_path = outdir / "run.json"

    if not run_json_path.exists():
        print(f"error: run.json not found in {outdir}", file=sys.stderr)
        return 1

    try:
        with open(run_json_path) as f:
            run_data = json_mod.load(f)
    except (OSError, json_mod.JSONDecodeError) as exc:
        print(f"error: could not read run.json: {exc}", file=sys.stderr)
        return 1

    # Check if campaign has already completed
    if run_data.get("finished_at") is not None:
        print(
            f"error: campaign '{run_data.get('campaign_id', 'unknown')}' "
            f"has already completed (finished_at is set)",
            file=sys.stderr,
        )
        return 1

    # Check if .stop file already exists
    stop_file = outdir / ".stop"
    if stop_file.exists():
        print(f"cancellation already requested (--outdir={outdir})")
        return 0

    # Write the .stop flag
    stop_file.write_text(json_mod.dumps({"requested_at": time.time()}))
    campaign_id = run_data.get("campaign_id", outdir.name)
    print(f"cancellation requested for campaign '{campaign_id}'")
    print(f"  outdir:  {outdir}")
    print(f"  stop file: {stop_file}")
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    """Create a backup of the campaign registry database (issue #440)."""
    registry_path = args.registry if args.registry else None
    reg = CampaignRegistry(db_path=registry_path)
    if args.output:
        backup_path = Path(args.output)
        reg.export_registry(backup_path)
    else:
        backup_path = reg.backup()
    print(f"Backup created: {backup_path}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    """Restore/import the campaign registry from a backup file (issue #440)."""
    backup_file: Path = Path(args.backup_file)
    if not backup_file.exists():
        print(f"error: backup file not found: {backup_file}", file=sys.stderr)
        return 1

    registry_path = args.registry if args.registry else None
    reg = CampaignRegistry(db_path=registry_path)
    try:
        count = reg.import_registry(backup_file, merge=args.merge)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mode = "merged" if args.merge else "replaced"
    print(f"Registry {mode} with {count} campaign(s) from {backup_file}")
    return 0


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0912, PLR0915
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    dispatch = {
        "import-osa": _run_import_osa,
        "export": _run_export,
        "serve": _cmd_serve,
        "dashboard": _cmd_dashboard,
        "list": _cmd_list,
        "show": _cmd_show,
        "compare": _cmd_compare,
        "status": _cmd_status,
        "download": _cmd_download,
        "cancel": _cmd_cancel,
        "backup": _cmd_backup,
        "restore": _cmd_restore,
        "health": _cmd_health,
    }
    handler = dispatch.get(args.command)
    if handler is not None:
        return handler(args)
    if args.command != "run":
        return 1

    # Apply preset before load_config so preset values are in place.
    _apply_preset(args)

    cfg: CampaignConfig = load_config(vars(args))
    executor: BaseExecutor
    if cfg.dry_run:
        executor = LocalExecutor(max_workers=1)
        log.info("DRY RUN: forcing LocalExecutor with 1 worker")
    else:
        executor = _build_executor(args)
    trust_level = ByosTrustLevel(args.byos_trust_level)
    byos_resource_limits: dict[str, int] | None = None
    if args.byos_resource_limits:
        try:
            import json as json_mod  # noqa: PLC0415

            byos_resource_limits = json_mod.loads(args.byos_resource_limits)
        except (json_mod.JSONDecodeError, TypeError) as exc:
            print(
                f"error: --byos-resource-limits must be a valid JSON dict: {exc}", file=sys.stderr
            )
            return 1
    apply_fn = (
        load_user_function(
            Path(args.custom_apply_script),
            trust_level=trust_level,
            resource_limits=byos_resource_limits,
        )
        if args.custom_apply_script
        else None
    )
    extract_fn = (
        load_user_function(
            Path(args.custom_kpi_extractor),
            trust_level=trust_level,
            resource_limits=byos_resource_limits,
        )
        if args.custom_kpi_extractor
        else None
    )
    task_queue = build_task_queue(cfg.task_queue, cfg.dask_scheduler_address)
    if cfg.task_queue != "none":
        log.info("task queue enabled: backend=%s", cfg.task_queue)
    campaign = Campaign(
        cfg,
        executor,
        apply_fn=apply_fn,
        extract_fn=extract_fn,
        max_workers=args.max_workers,
        task_queue=task_queue,
    )
    # TUI: wrap campaign execution with Rich TUI when available.
    # The TUI is a passive observer (reads run.json) and does not
    # modify campaign state.  It auto-degrades when rich is not
    # installed, stdout is not a TTY, or --no-tui is passed.
    use_tui = not args.no_tui
    tui: object = None
    if use_tui:
        try:
            from osimflow.tui import RichTUI, is_tui_available  # noqa: PLC0415

            if is_tui_available():
                tui = RichTUI(cfg.outdir)
                log.info("Rich TUI enabled")
        except Exception:
            pass  # degrade silently
    try:
        if tui is not None:
            from osimflow.tui import RichTUI  # noqa: PLC0415, F811

            assert isinstance(tui, RichTUI)
            tui.start()
        try:
            result = campaign.run()
        finally:
            if tui is not None:
                from osimflow.tui import RichTUI  # noqa: PLC0415, F811

                assert isinstance(tui, RichTUI)
                tui.stop()
            executor.shutdown()
    except Exception:
        executor.shutdown()
        raise
    print("\n=== CAMPAIGN RESULT ===")
    # The result is dict[str, object] at the type-checker level; the values
    # are well-known at runtime (see Campaign.run() return shape). Cast for
    # the formatter.
    elapsed_s = float(str(result["elapsed_s"]))
    kpis = result["kpis"]
    aggregated = result["aggregated"]
    plots = result["plots"]
    run_json = result["run_json"]
    print(f"elapsed_s: {elapsed_s:.2f}")
    if hasattr(kpis, "__len__"):
        print(f"kpis:      {len(kpis)} files")
    else:
        print(f"kpis:      {kpis}")
    if isinstance(aggregated, dict):
        print(f"csv:       {aggregated.get('csv')}")
        print(f"failed:    {aggregated.get('failed')}")
    else:
        print(f"aggregated: {aggregated}")
    print(f"plots:     {plots}")
    print(f"run trace: {run_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
