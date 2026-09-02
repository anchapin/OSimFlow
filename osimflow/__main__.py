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
import json
import logging
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Any

import httpx
import yaml

from osimflow import (
    AWSBatchExecutor,
    AzureBatchExecutor,
    BaseExecutor,
    Campaign,
    CampaignConfig,
    CampaignRecord,
    CampaignRegistry,
    DaskJobQueueExecutor,
    DockerSwarmExecutor,
    GoogleBatchExecutor,
    KubernetesExecutor,
    LocalExecutor,
    NomadExecutor,
    PBSExecutor,
    SlurmExecutor,
    build_task_queue,
    load_config,
)
from osimflow.audit import AuditLogger
from osimflow.byos import ByosTrustLevel, load_user_function, validate_trust_level
from osimflow.cosign import DEFAULT_COSIGN_OIDC_ISSUER
from osimflow.cross_run_aggregator import CrossRunAggregator
from osimflow.exporters.osa import OSAExporter
from osimflow.handoff_record import (
    IDEMPOTENCY_KEY_HEADER,
    HandoffRecord,
    NoHandoffRecordError,
    handoff_record_exists,
    read_handoff_record,
    write_handoff_record,
)
from osimflow.importers.osa import OSAImportError, osa_to_variables_yml, parse_osa
from osimflow.validation import ValidationError

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


def _byos_audit_logger(args: argparse.Namespace) -> AuditLogger | None:
    """Audit logger bound to the campaign outdir for BYOS events (#1399).

    Returns ``None`` when no outdir is resolvable (e.g. ``osimflow serve``
    code paths that construct their own logger); callers treat ``None``
    as "no audit sink configured".
    """
    outdir_raw = getattr(args, "outdir", None)
    if not outdir_raw:
        return None
    return AuditLogger(Path(outdir_raw))


#: Executors whose jobs pull the OpenStudio image from a managed cloud
#: registry at campaign scale. Running one of these without
#: ``--container-digest`` triggers the mutable-tag supply-chain warning
#: (issue #1320).
_CLOUD_EXECUTORS = frozenset({"aws_batch", "azure_batch", "google_batch"})


def _warn_if_mutable_tag(executor_name: str, container_digest: str | None) -> None:
    """Warn when a cloud executor would pull by mutable tag (issue #1320).

    A compromised or backdoored registry tag update would affect every
    active campaign. Digest pinning is the documented production default
    (docs/container-image-strategy.md); this warning makes skipping it a
    conscious decision rather than an oversight.
    """
    if container_digest is not None:
        return
    if executor_name not in _CLOUD_EXECUTORS:
        return
    warnings.warn(
        f"--container-digest is NOT set; executor '{executor_name}' will pull "
        "nrel/openstudio:<version> by MUTABLE tag from the registry. A compromised "
        "or backdoored tag update would affect this campaign (supply-chain risk). "
        "Pin a content-addressed digest for production: "
        "`docker inspect --format '{{index .RepoDigests 0}}' nrel/openstudio:<version>` "
        "and pass it via --container-digest. See docs/container-image-strategy.md.",
        UserWarning,
        stacklevel=2,
    )


def _build_executor(args: argparse.Namespace) -> BaseExecutor:  # noqa: PLR0911
    """Dispatch to the correct executor based on ``args.executor``."""
    max_concurrent_samples = _extract_max_concurrent_samples(args.resource_quota)
    if args.executor == "local":
        return LocalExecutor(
            max_workers=args.max_workers,
            max_concurrent_samples=max_concurrent_samples,
        )
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
            instance_type=args.aws_batch_instance_type,
            submit_rps=args.aws_batch_submit_rps,
        )
    # Nomad executor — address and datacentre.
    if args.executor == "nomad":
        # Issue #1316: derive a unique dispatch job ID per campaign to prevent
        # concurrent campaigns on the same Nomad cluster from overwriting each other's
        # parameterized job spec. The user can override with --nomad-dispatch-job-id.
        dispatch_job_id = args.nomad_dispatch_job_id
        if dispatch_job_id is None and getattr(args, "outdir", None):
            dispatch_job_id = f"osimflow-worker-{abs(hash(str(args.outdir)))}"
        return NomadExecutor(
            address=args.nomad_address,
            datacentre=args.nomad_datacentre,
            dispatch_policy=args.nomad_dispatch_policy,
            estimated_run_size=int(args.n_samples),
            fanout_submit_rate_per_sec=args.nomad_fanout_submit_rate_per_sec,
            fanout_submit_chunk_size=args.nomad_fanout_submit_chunk_size,
            allocation_resolution_timeout_s=args.nomad_allocation_resolution_timeout_s,
            poll_interval_s=args.nomad_poll_interval_s,
            max_poll_interval_s=args.nomad_max_poll_interval_s,
            remote_results_only=args.nomad_remote_results_only,
            verify_tls=args.nomad_tls_verify,
            tls=args.nomad_tls,
            cert=args.nomad_cert,
            key=args.nomad_key,
            ca_cert=args.nomad_ca_cert,
            dispatch_job_id=dispatch_job_id,
            allow_insecure_token=args.nomad_allow_insecure_token,
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
    # Kubernetes executor — namespace, polling config, and native Job
    # controls (issue #997). Defaults preserve the pre-#997 manifest
    # byte-for-byte; passing flags here is opt-in.
    if args.executor == "kubernetes":
        return KubernetesExecutor(
            namespace=args.kubernetes_namespace,
            poll_interval_s=args.kubernetes_poll_interval_s,
            max_poll_interval_s=args.kubernetes_max_poll_interval_s,
            backoff_limit=args.kubernetes_backoff_limit,
            ttl_seconds_after_finished=args.kubernetes_ttl_seconds_after_finished,
            queue_name=args.kubernetes_queue_name,
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
    if args.executor == "docker_swarm":
        return DockerSwarmExecutor(
            poll_interval_s=args.docker_swarm_poll_interval_s,
            max_poll_interval_s=args.docker_swarm_max_poll_interval_s,
            image=args.docker_swarm_image,
            network=args.docker_swarm_network,
        )

    # Fall back to the ExecutorRegistry for plugin-discovered executors
    # (issue #432, #1275).  Third-party executors registered via entry_points
    # are available here.  CLI args are forwarded as kwargs so plugin
    # executors can receive configuration (issue #1275).
    from osimflow.executors import ExecutorRegistry  # noqa: PLC0415

    if args.executor in ExecutorRegistry.list_available():
        executor_cls = ExecutorRegistry.get(args.executor)
        log.info(
            "instantiating executor '%s' from registry (class=%s)",
            args.executor,
            executor_cls.__qualname__,
        )
        return executor_cls(**vars(args))

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
            "docker_swarm",
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
        "--aws-batch-instance-type",
        default=None,
        help=(
            "AWS EC2 instance type used for the Spot price ceiling check "
            "(e.g. 'm5.large'). When set, ``describe_spot_price_history`` "
            "is scoped to this instance type so the ceiling check is "
            "reliable. When omitted, the check uses the minimum price "
            "across all instance types and a warning is logged (issue #792)."
        ),
    )
    run.add_argument(
        "--aws-batch-submit-rps",
        type=float,
        default=None,
        help=(
            "Submit rate limit in submissions per second, enforced via a "
            "shared token-bucket limiter (issue #1010). Default 800 RPS, "
            "below AWS Batch's 1000 TPS account limit. Set to a lower "
            "value to avoid ThrottlingException on smaller accounts."
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
        "--nomad-dispatch-policy",
        choices=[
            "keep_manual",
            "force_dispatch",
            "auto_prefer_dispatch",
            "direct",
            "dispatch",
            "auto",
        ],
        default="keep_manual",
        help=(
            "Nomad submission policy. Preferred values: "
            "'keep_manual' (default, no auto-switching), "
            "'force_dispatch' (always dispatch), "
            "'auto_prefer_dispatch' (auto-switch to dispatch for large runs). "
            "Legacy aliases are accepted for compatibility: "
            "'direct'->'keep_manual', 'dispatch'->'force_dispatch', 'auto'->'auto_prefer_dispatch'."
        ),
    )
    run.add_argument(
        "--nomad-allocation-resolution-timeout-s",
        type=float,
        default=30.0,
        help=("Timeout in seconds to resolve Nomad EvalID to Allocation ID (default: 30.0)."),
    )
    run.add_argument(
        "--nomad-poll-interval-s",
        type=float,
        default=5.0,
        help="Initial Nomad allocation polling interval in seconds (default: 5.0).",
    )
    run.add_argument(
        "--nomad-max-poll-interval-s",
        type=float,
        default=60.0,
        help="Maximum Nomad allocation polling interval in seconds (default: 60.0).",
    )
    run.add_argument(
        "--nomad-fanout-submit-rate-per-sec",
        type=float,
        default=None,
        help=(
            "Optional fan-out submit rate limit for Nomad (submissions/sec). "
            "Used by fan-out steps to pace submission and reduce coordinator pressure."
        ),
    )
    run.add_argument(
        "--nomad-fanout-submit-chunk-size",
        type=int,
        default=0,
        help=(
            "Optional fan-out submit chunk size for Nomad (0 disables chunking). "
            "When >0, fan-out submits at most this many tasks per chunk."
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
        "--nomad-allow-insecure-token",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Allow NOMAD_TOKEN to be transmitted without TLS to a non-local "
            "Nomad address. Fails closed by default (SEC-009, issue #1450): "
            "without this flag, osimflow run --executor nomad raises when a "
            "token is configured for a non-local address without TLS. "
            "Dev/test only — mirrors --allow-insecure-storage-endpoint "
            "(issue #1386)."
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
        "--nomad-remote-results-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "DEPRECATED compatibility toggle. True (default) keeps Nomad in remote-results mode. "
            "Set --no-nomad-remote-results-only only for temporary migration compatibility; "
            "the legacy local-callable mode is scheduled for removal after one minor release."
        ),
    )
    run.add_argument(
        "--nomad-dispatch-job-id",
        default=None,
        help=(
            "Override the Nomad dispatch job ID in dispatch mode (issue #1316). "
            "When not set, the executor derives a unique ID from the campaign outdir hash. "
            "Setting this is only needed when multiple campaigns must share the same job ID "
            "(e.g., to leverage a pre-registered job spec)."
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
    # Native Job controls (issue #997). Defaults preserve the pre-#997
    # manifest byte-for-byte: backoff_limit=0, no TTL, no extra labels.
    run.add_argument(
        "--kubernetes-backoff-limit",
        type=int,
        default=0,
        help=(
            "Native Job ``backoffLimit`` (default: 0 — preserves the "
            "orchestrator-side retry semantics from ``--max-sample-retries``). "
            "Set to >0 to enable K8s-native pod retry as an alternative: "
            "the kubelet restarts the failed pod up to this many times "
            "without a resubmit round-trip through the orchestrator. "
            "Pick one mechanism, not both — running K8s-native retry "
            "and ``--max-sample-retries`` together will double-count "
            "failures. (issue #997)"
        ),
    )
    run.add_argument(
        "--kubernetes-ttl-seconds-after-finished",
        type=int,
        default=None,
        help=(
            "Native Job ``ttlSecondsAfterFinished`` (default: unset — "
            "Jobs are retained in the API server until manually deleted). "
            "Set to a positive integer (seconds) to have the API server "
            "garbage-collect completed/failed Jobs after this delay, "
            "releasing the etcd footprint and pod resources across a "
            "large sweep. (issue #997)"
        ),
    )
    run.add_argument(
        "--kubernetes-queue-name",
        default=None,
        help=(
            "Kueue ClusterQueue name applied as the "
            "``kueue.x-k8s.io/queue-name`` label on Job metadata (issue #997). "
            "When set, Kueue manages the Job through suspend/resume and "
            "honors fair-sharing, priority, and preemption across the "
            "cluster. Inert on clusters without Kueue installed. "
            "Example: --kubernetes-queue-name team-a-cpu."
        ),
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
        default=1,
        help="Minimum number of Dask workers to keep alive (default: 1).",
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
    # Docker Swarm executor flags (issue #582)
    run.add_argument(
        "--docker-swarm-poll-interval-s",
        type=float,
        default=5.0,
        help="Docker Swarm polling interval in seconds (default: 5.0).",
    )
    run.add_argument(
        "--docker-swarm-max-poll-interval-s",
        type=float,
        default=60.0,
        help="Docker Swarm max polling interval in seconds (default: 60.0).",
    )
    run.add_argument(
        "--docker-swarm-image",
        default="nrel/openstudio:3.11.0",
        help="Docker image for Swarm services (default: nrel/openstudio:3.11.0). "
        "WARNING: Using 'latest' is not recommended for production due to "
        "supply-chain risk — the image digest can change over time.",
    )
    run.add_argument(
        "--docker-swarm-network",
        default=None,
        help="Docker network to attach Swarm services to.",
    )
    # Cost tracking flags (issue #447)
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
    run.add_argument("--input_variables", required=True)
    run.add_argument("--template_sim_package", required=True)
    run.add_argument("--n_samples", type=int, required=True)
    run.add_argument("--outdir", required=True)
    run.add_argument("--openstudio_version", default="3.11.0")
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
        default="lhs",
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
        default=600.0,
        help=(
            "Wall-clock timeout in seconds for the BYOS subprocess and the "
            "real OpenStudio CLI simulation subprocess (issue #1109). A wedged "
            "subprocess is killed and the sample fails instead of hanging the "
            "campaign forever. Raise this for large models on slow hardware."
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
        help=(
            "Enable campaign cost tracking (issue #447). "
            "Estimates cloud/HPC resource costs and writes a cost summary JSON "
            "alongside campaign outputs. Supports AWS Batch (on-demand/Spot), "
            "Slurm (per-node-hour), and Local (no cost) executors."
        ),
    )
    run.add_argument(
        "--aws-batch-spot-price",
        type=float,
        default=None,
        help=(
            "AWS Batch Spot price in USD per vCPU-hour for cost tracking "
            "(issue #447). When set alongside --track-costs, this rate is used "
            "instead of the default $0.0036/vCPU·hr to estimate Spot savings. "
            "The on-demand rate is set via --aws-batch-on-demand-price."
        ),
    )
    run.add_argument(
        "--aws-batch-on-demand-price",
        type=float,
        default=None,
        help=(
            "AWS Batch on-demand price in USD per vCPU-hour for cost tracking "
            "(issue #447). When set alongside --track-costs, this rate is used "
            "instead of the default $0.0132/vCPU·hr to estimate job costs."
        ),
    )
    run.add_argument(
        "--slurm-cost-per-node-hour",
        type=float,
        default=None,
        help=(
            "Slurm cost in USD per node-hour for cost tracking (issue #447). "
            "When set alongside --track-costs, this rate is used instead of the "
            "default $0.10/node·hr to estimate job costs."
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
            "Opt-in flag to allow plaintext 'http://' --result-storage-endpoint "
            "and --s3-artifact-endpoint URLs (issue #1386). Defaults to False "
            "(fail-closed) — non-HTTPS endpoints are rejected by the storage "
            "backend to prevent AWS SigV4 signing material from leaking in "
            "cleartext. Use only for dev/test against a local MinIO; loopback "
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
            "SEC-001: REQUIRED when binding to a non-local interface — "
            "without it every endpoint is reachable without credentials "
            "(issue #1095). Required when --enable-writes is set "
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
        choices=["csv", "json"],
        default="csv",
        help="Export format (default: csv)",
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
        help="Request resume of a paused campaign (issue #444)",
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


def _is_local_host(host: str) -> bool:
    """True when *host* binds only to the local machine (issue #1113).

    Loopback binds are exempt from the cleartext-traffic warning because
    that traffic never leaves the host. ``*`` / ``empty`` (all
    interfaces) are treated as non-local.
    """
    normalized = (host or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"localhost", "::1", "[::1]", "0:0:0:0:0:0:0:1"}:
        return True
    return normalized.startswith("127.")


def _warn_if_auth_disabled_nonlocal(
    api_key: str | None,
    api_keys_file: Path | None,
    host: str,
    port: int,
) -> None:
    """Warn loudly when the serve bind is network-accessible with no auth (issue #1095).

    ``APIKeyMiddleware`` passes every request through when no key store is
    configured (``api_key is None and api_keys_file is None``), so all
    endpoints — campaign data, file downloads, time-series queries — are
    reachable without credentials. Loopback binds are exempt because that
    traffic never leaves the host; ``0.0.0.0`` / ``*`` / empty are not.
    """
    auth_enabled = api_key is not None or api_keys_file is not None
    if auth_enabled or _is_local_host(host):
        return
    warnings.warn(
        f"Authentication is DISABLED on non-local bind {host}:{port}: "
        "every endpoint (campaign data, file downloads, time-series "
        "queries) is reachable without credentials. Pass --api-key or "
        "--api-keys-file to require authentication (issue #1095).",
        UserWarning,
        stacklevel=2,
    )
    log.warning(
        "SEC-001: auth disabled for non-local bind %s:%d — all endpoints "
        "are publicly reachable without credentials (issue #1095). "
        "Pass --api-key or --api-keys-file.",
        host,
        port,
    )


def _warn_if_cleartext_nonlocal(tls_cert: Path | None, host: str, port: int) -> None:
    """Warn loudly when a network-accessible serve bind lacks TLS (SEC-004).

    Plain HTTP on a non-local interface transmits everything — including
    API keys — in cleartext. TLS stays opt-in for backwards compatibility
    with local tooling, but non-local binds without it are almost
    certainly a misconfiguration (issue #1113).
    """
    if tls_cert is not None or _is_local_host(host):
        return
    warnings.warn(
        f"TLS is DISABLED for non-local bind {host}:{port}: "
        "traffic (including API keys) is transmitted in cleartext and can "
        "be intercepted (SEC-004). Enable TLS with --tls-cert/--tls-key.",
        UserWarning,
        stacklevel=2,
    )
    log.warning("SEC-004: TLS disabled for non-local bind %s:%d — traffic is cleartext", host, port)


def _cmd_serve(args: argparse.Namespace) -> int:  # noqa: PLR0912
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
        # Print the key to stdout once so the user can capture it (issue
        # #1117): without this the auto-generated key is unusable because
        # it is never surfaced anywhere. Never log the key itself — only a
        # fingerprint for audit purposes.
        print(
            "Auto-generated API key (won't be shown again):\n"
            f"  {api_key}\n"
            "Pass it as the X-API-Key header on write requests.",
            file=sys.stdout,
        )
        log.debug("API key generated")

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
        allow_insecure_api_keys_file=args.allow_insecure_api_keys_file,
        cors_origins=cors_origins,
        rate_limit=args.rate_limit,
        rate_limit_key=args.rate_limit_key,
        ui_enabled=args.ui,
        variable_editor=args.editor,
        results_viewer=args.dashboard,
        dashboard=args.dashboard,
        registry_path=args.registry,
        redis_url=args.api_redis_url,
    )
    if args.host not in ("127.0.0.1", "localhost"):
        log.warning("Binding to %s — the API is now network-accessible.", args.host)
        # SEC-001: require auth on non-local binds (issue #1319)  # noqa: PLR0912
        if not api_key and not api_keys_file:
            print(
                "ERROR: SEC-001 — authentication required on non-localhost bind.\n"
                "Pass --api-key (or --api-keys-file) to enable authentication.\n"
                "See issue #1319.",
                file=sys.stderr,
            )
            return 1

    tls_cert: Path | None = args.tls_cert if args.tls_cert else None
    tls_key: Path | None = args.tls_key if args.tls_key else None

    if tls_cert is not None and tls_key is None:
        print("Error: --tls-key is required when --tls-cert is provided.", file=sys.stderr)
        return 1
    if tls_key is not None and tls_cert is None:
        print("Error: --tls-cert is required when --tls-key is provided.", file=sys.stderr)
        return 1

    # SEC-004: warn loudly on cleartext non-local binds (issue #1113).
    _warn_if_cleartext_nonlocal(tls_cert, args.host, args.port)

    # SEC-001: warn loudly when a non-local bind has no authentication at
    # all — every endpoint is reachable without credentials (issue #1095).
    _warn_if_auth_disabled_nonlocal(api_key, api_keys_file, args.host, args.port)

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
        f"{'ID':<25} {'PROJECT':<20} {'STATUS':<10} "
        f"{'ALGO':<8} {'N':<6} {'EXECUTOR':<10} {'CREATED'}"
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
    """Compare two or more campaigns side by side.

    When ``--outdirs`` is given, uses CrossRunAggregator to produce
    KPI-level statistics across all campaigns.
    Otherwise falls back to registry-based metadata comparison for
    two campaigns identified by registry IDs.
    """
    if args.outdirs:
        return _cmd_compare_outdirs(args)

    if args.id1 is None or args.id2 is None:
        print(
            "error: compare requires either --outdirs or two campaign IDs",
            file=sys.stderr,
        )
        return 1

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


def _cmd_compare_outdirs(args: argparse.Namespace) -> int:  # noqa: PLR0912
    """Compare N campaigns using CrossRunAggregator (KPI-level comparison)."""
    import json as json_mod  # noqa: PLC0415

    if len(args.outdirs) < 2:
        print("error: --outdirs requires at least two campaign directories", file=sys.stderr)
        return 1

    # Build label list
    labels: list[str | None]
    if args.labels:
        if len(args.labels) != len(args.outdirs):
            print(
                f"error: --labels ({len(args.labels)}) must match --outdirs ({len(args.outdirs)})",
                file=sys.stderr,
            )
            return 1
        labels = list(args.labels)
    else:
        labels = [None] * len(args.outdirs)

    aggregator = CrossRunAggregator(campaigns=list(zip(args.outdirs, labels, strict=True)))
    aggregator.load()
    if not aggregator._runs:
        print("error: No valid campaigns found (missing aggregated_results.csv)", file=sys.stderr)
        return 1

    stats = aggregator.compute_cross_run_stats()
    combined_df = aggregator.get_combined_dataframe()

    # Optional CSV export
    if args.export:
        aggregator.export_combined_csv(args.export)
        print(f"Exported combined CSV to {args.export}")

    # Filter KPIs if requested
    kpis_to_show = set(args.kpis) if args.kpis else set(stats.keys())

    # Header
    campaign_labels = list(aggregator._runs.keys())
    col_w = 14
    kpi_col_w = 16

    print(f"\nCross-run KPI comparison ({len(campaign_labels)} campaigns)")
    print("=" * (kpi_col_w + col_w * len(campaign_labels) + 4))

    # KPI comparison table
    print(f"{'KPI':<{kpi_col_w}}", end="")
    for lbl in campaign_labels:
        print(f" {lbl[:col_w]:<{col_w}}", end="")
    print()
    print("-" * (kpi_col_w + col_w * len(campaign_labels) + 4))

    for kpi, s in stats.items():
        if kpi not in kpis_to_show:
            continue
        print(f"{kpi[:kpi_col_w]:<{kpi_col_w}}", end="")
        for label in campaign_labels:
            val = s.values.get(label)
            cell = "N/A" if val is None else f"{val:.4f}"
            print(f" {cell[:col_w]:<{col_w}}", end="")
        print()

    # Best/worst per KPI
    print("\nBest/worst per KPI:")
    print("-" * 60)
    for kpi, s in stats.items():
        if kpi not in kpis_to_show:
            continue
        best_val = s.values.get(s.best_campaign) if s.best_campaign else None
        if best_val is not None:
            print(
                f"  {kpi:<20} best={s.best_campaign} ({best_val:.4f}), "
                f"worst={s.worst_campaign} ({s.values.get(s.worst_campaign or '') or 0:.4f})"
            )

    # Cross-run summary statistics
    print("\nCross-run statistics:")
    print("-" * 60)
    for kpi, s in stats.items():
        if kpi not in kpis_to_show:
            continue
        print(
            f"  {kpi:<20} mean={s.overall_mean:.4f} std={s.overall_std:.4f} "
            f"min={s.overall_min:.4f} max={s.overall_max:.4f}"
        )

    # Combined DataFrame info
    print(
        f"\nCombined dataset: {len(combined_df)} total rows "
        f"({', '.join(f'{lbl}={r.n_samples}' for lbl, r in aggregator._runs.items())})"
    )

    # JSON summary
    summary = aggregator.summary()
    print(f"\nJSON summary: {json_mod.dumps(summary, indent=2, default=str)}")

    return 0


def _cmd_aggregate_runs(args: argparse.Namespace) -> int:
    """Aggregate KPI results from two or more campaign runs into a combined dataset.

    Loads ``aggregated_results.csv`` from each campaign directory, merges them
    into a single DataFrame with campaign labels, and prints cross-run statistics.
    Optionally exports the combined CSV to a user-specified path.
    """
    if len(args.outdirs) < 2:
        print(
            "error: aggregate-runs requires at least two campaign directories",
            file=sys.stderr,
        )
        return 1

    # Build label list
    labels: list[str | None]
    if args.labels is not None and len(args.labels) > 0:
        if len(args.labels) != len(args.outdirs):
            print(
                f"error: --labels ({len(args.labels)}) must match "
                f"positional outdirs ({len(args.outdirs)})",
                file=sys.stderr,
            )
            return 1
        labels = list(args.labels)
    else:
        labels = [None] * len(args.outdirs)

    aggregator = CrossRunAggregator(campaigns=list(zip(args.outdirs, labels, strict=True)))
    aggregator.load()

    loaded = aggregator._runs
    if not loaded:
        print(
            "error: No valid campaigns found. Check that each outdir contains "
            "an aggregated_results.csv file.",
            file=sys.stderr,
        )
        return 1

    # Determine output path
    output_path = args.output
    if output_path is None:
        first = args.outdirs[0].resolve()
        output_path = first.parent / "aggregated_combined.csv"

    aggregator.export_combined_csv(output_path)

    stats = aggregator.compute_cross_run_stats()
    combined_df = aggregator.get_combined_dataframe()

    print(f"\nAggregated {len(loaded)} campaigns:")
    for label, run in loaded.items():
        print(f"  - {label}: {run.n_samples} samples from {run.outdir}")

    print(f"\nCombined dataset: {len(combined_df)} rows -> {output_path}")

    # Filter KPIs if requested
    kpis_to_show = set(args.kpis) if args.kpis else set(stats.keys())

    if stats:
        print(f"\nCross-run statistics ({len(kpis_to_show)} KPIs):")
        print("-" * 80)
        for kpi, s in stats.items():
            if kpi not in kpis_to_show:
                continue
            vals = {k: v for k, v in s.values.items() if v is not None}
            if vals:
                print(
                    f"  {kpi:<24} overall_mean={s.overall_mean:>10.4f}  "
                    f"std={s.overall_std:>8.4f}  "
                    f"range=[{s.overall_min:>10.4f}, {s.overall_max:>10.4f}]"
                )
                for lbl, val in sorted(vals.items(), key=lambda x: x[1]):
                    print(f"    {lbl:<22} mean={val:.4f}")

    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Show detailed status of a campaign.

    For a Coordinator-backed (``--detach``) campaign, resolves the
    ``campaign_id`` from the local handoff record under ``outdir/`` and polls
    the Coordinator for live status — works from a fresh shell / rebooted
    machine (issue #630). Otherwise falls back to the local ``run.json`` view.
    """
    import json as json_mod  # noqa: PLC0415

    outdir: Path = args.outdir.resolve()

    # Coordinator-backed campaign: prefer the live Coordinator status. This
    # path needs no run.json (a detached campaign never ran locally).
    if handoff_record_exists(outdir):
        try:
            record = read_handoff_record(outdir)
        except NoHandoffRecordError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return _print_coordinator_status(record)

    run_json_path = outdir / "run.json"

    if not run_json_path.exists():
        print(f"error: run.json not found in {outdir}", file=sys.stderr)
        print(
            "       (no Coordinator handoff record either; did you run with `--detach`?)",
            file=sys.stderr,
        )
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
            f"\nSamples:    {len(per_sample)} total  |  {n_ok} ok  "
            f"|  {n_failed} failed  |  {n_cached} cached"
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
    """Download results from a completed campaign to a destination directory.

    For a Coordinator-backed (``--detach``) campaign, fetches ONLY the final
    aggregated artifacts via the Coordinator's presigned URL — per-sample
    bytes are not downloaded (issue #630). Otherwise copies the local
    artifacts from ``outdir/``.
    """
    outdir: Path = args.outdir.resolve()

    # Coordinator-backed campaign: fetch aggregated artifacts via presigned
    # URL from the Coordinator (no local run.json needed).
    if handoff_record_exists(outdir):
        try:
            record = read_handoff_record(outdir)
        except NoHandoffRecordError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        output_dir: Path = (
            args.output_dir.resolve()
            if args.output_dir
            else outdir.parent / f"{outdir.name}-downloads" / record.campaign_id
        )
        return _download_coordinator_results(record, output_dir)

    return _download_local_results(args, outdir)


def _download_local_results(args: argparse.Namespace, outdir: Path) -> int:
    """Copy local artifacts (run.json, CSVs, plots/, kpis/) from ``outdir/``.

    This is the original ``osimflow download`` behaviour for campaigns that ran
    locally (no Coordinator). Extracted from ``_cmd_download`` so the
    Coordinator-backed path can short-circuit above.
    """
    import json as json_mod  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    run_json_path = outdir / "run.json"

    if not run_json_path.exists():
        print(f"error: run.json not found in {outdir}", file=sys.stderr)
        print(
            "       (no Coordinator handoff record either; did you run with `--detach`?)",
            file=sys.stderr,
        )
        return 1

    try:
        with open(run_json_path) as f:
            run_data = json_mod.load(f)
    except (OSError, json_mod.JSONDecodeError) as exc:
        print(f"error: could not read run.json: {exc}", file=sys.stderr)
        return 1

    campaign_id = run_data.get("campaign_id", outdir.name)
    output_dir: Path = (
        args.output_dir.resolve()
        if args.output_dir
        else outdir.parent / f"{outdir.name}-downloads" / campaign_id
    )

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


# ---------------------------------------------------------------------------
# Coordinator (``--detach``) reconnect helpers — issue #630, Epic #624
# ---------------------------------------------------------------------------
#
# These functions implement the detach handoff + CLI reconnect UX:
#
#   * ``_build_coordinator_payload`` / ``_coordinator_idempotency_key`` build
#     a deterministic handoff payload + ``Idempotency-Key`` so a retried
#     handoff (e.g. after a lost response) returns the *same* campaign.
#   * ``_perform_detach_handoff`` is the ``osimflow run --detach`` flow: it
#     POSTs to the Coordinator, writes a local handoff record, and exits.
#   * ``_print_coordinator_status`` / ``_download_coordinator_results`` power
#     ``osimflow status`` / ``osimflow download`` for Coordinator-backed
#     campaigns, reading the campaign_id from the local handoff record so
#     they work from a fresh shell / rebooted machine.
#
# httpx is imported at module top; boto3 / json / hashlib are lazy-imported
# inside the few helpers that need them to keep CLI cold-start cheap.


def _coordinator_status_url(coordinator_url: str, campaign_id: str) -> str:
    """Build the absolute campaign-status URL for a Coordinator campaign."""
    return f"{coordinator_url.rstrip('/')}/api/v1/coordinator/campaigns/{campaign_id}"


def _build_coordinator_payload(args: argparse.Namespace, cfg: CampaignConfig) -> dict[str, Any]:
    """Build the JSON payload posted to ``POST /coordinator/handoff``.

    Factored out so the payload shape and the idempotency-key derivation share
    one source of truth.
    """
    return {
        "name": str(cfg.outdir.name) if cfg.outdir else f"campaign-{int(time.time())}",
        "n_samples": cfg.n_samples,
        "executor": args.executor or "local",
        "openstudio_version": cfg.openstudio_version or "3.11.0",
        "algorithm": cfg.algorithm,
        "max_generations": cfg.max_generations,
        "input_variables": str(cfg.input_variables) if cfg.input_variables else None,
        "template_sim_package": str(cfg.template_sim_package) if cfg.template_sim_package else None,
        "custom_apply_script": args.custom_apply_script,
        "custom_kpi_extractor": args.custom_kpi_extractor,
        "archive_intermediates": cfg.archive_intermediates,
        "result_storage_backend": cfg.result_storage_backend,
        "result_storage_bucket": cfg.result_storage_bucket,
        "extra": {
            "slurm_partition": getattr(args, "slurm_partition", None),
            "aws_batch_queue": getattr(args, "aws_batch_queue", None),
            "nomad_datacentre": getattr(args, "nomad_datacentre", None),
        },
    }


def _coordinator_idempotency_key(payload: dict[str, Any], outdir: Path | None) -> str:
    """Derive a deterministic ``Idempotency-Key`` for a handoff.

    Hashing the resolved payload + the outdir means re-running the *same*
    ``osimflow run --detach ...`` command produces the *same* key, so a retry
    after a lost HTTP response reuses the campaign the Coordinator already
    created instead of minting a second one (issue #630). Changing any
    campaign parameter (or the outdir) yields a different key -> a new
    campaign, which is the desired behaviour.
    """
    import hashlib  # noqa: PLC0415
    import json as json_mod  # noqa: PLC0415

    digest = hashlib.sha256()
    digest.update(json_mod.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
    if outdir is not None:
        digest.update(str(outdir.resolve()).encode("utf-8"))
    return f"osimflow-{digest.hexdigest()[:32]}"


def _perform_detach_handoff(args: argparse.Namespace) -> int:
    """Execute the ``osimflow run --detach`` fire-and-forget handoff.

    Posts the campaign config to the Coordinator with a deterministic
    ``Idempotency-Key``, writes a local handoff record under ``outdir``, and
    exits cleanly. Failure modes (Coordinator unreachable, 4xx config error,
    5xx server error) surface a clear, actionable message; 4xx leaves no
    remote state, while 5xx/idempotency lets the user safely re-run the same
    command to recover.
    """
    coordinator_url = (args.coordinator_url or "").rstrip("/")
    if not coordinator_url:
        print("error: --detach requires --coordinator-url", file=sys.stderr)
        return 1

    # Idempotent fast path: this outdir is already associated with a remote
    # campaign. Return the existing campaign_id without loading the config or
    # hitting the network. (Checked before load_config so a stale/invalid
    # config doesn't block reconnecting to an already-handed-off campaign.)
    outdir_raw = getattr(args, "outdir", None)
    outdir: Path | None = Path(outdir_raw).resolve() if outdir_raw else None
    if outdir is not None and handoff_record_exists(outdir):
        rec = read_handoff_record(outdir)
        print(f"Campaign already handed off to Coordinator: {rec.campaign_id}")
        print(f"  Status URL: {rec.status_url}")
        print(
            "  (outdir already associated with a Coordinator campaign; "
            "use `osimflow status <outdir>` to monitor it.)"
        )
        return 0

    cfg = load_config(vars(args))
    outdir = cfg.outdir

    payload = _build_coordinator_payload(args, cfg)
    idem_key = _coordinator_idempotency_key(payload, outdir)
    handoff_url = f"{coordinator_url}/api/v1/coordinator/handoff"

    try:
        response = httpx.post(
            handoff_url,
            json=payload,
            headers={IDEMPOTENCY_KEY_HEADER: idem_key},
            timeout=30.0,
        )
    except httpx.RequestError as exc:
        # ConnectError is a subclass of RequestError; surface the more
        # actionable "is it running?" hint for connection failures.
        if isinstance(exc, httpx.ConnectError):
            detail = f"could not reach Coordinator at {coordinator_url}: {exc}"
            hint = "Is the Coordinator running and reachable?"
        else:
            detail = f"handoff request to {coordinator_url} failed: {exc}"
            hint = "Re-run the same command to retry."
        print(
            f"error: {detail}\n"
            f"       {hint} No campaign was created; the handoff is "
            "idempotent, so re-running the same command is safe.",
            file=sys.stderr,
        )
        return 1

    # 4xx: the Coordinator rejected the config before creating any state.
    if 400 <= response.status_code < 500:
        print(
            f"error: Coordinator rejected the handoff "
            f"(HTTP {response.status_code}).\n"
            f"       Response: {response.text}\n"
            "       Fix the campaign configuration and re-run. "
            "No campaign was created.",
            file=sys.stderr,
        )
        return 1

    # 5xx: server error — the campaign may or may not exist on the server.
    if response.status_code >= 500:
        print(
            f"error: Coordinator returned HTTP {response.status_code}: "
            f"{response.text}\n"
            "       The campaign may or may not have been created on the "
            "server.\n"
            "       Re-run the SAME command to recover — the Idempotency-Key "
            "reuses any\n"
            "       campaign already created for this configuration.",
            file=sys.stderr,
        )
        return 1

    # 2xx (202 Accepted expected) — success. Persist the handoff record so
    # ``osimflow status`` / ``osimflow download`` can reconnect later.
    result_data = response.json()
    campaign_id = str(result_data.get("campaign_id", ""))
    status_url = str(
        result_data.get("status_url") or _coordinator_status_url(coordinator_url, campaign_id)
    )
    if outdir is not None:
        write_handoff_record(
            outdir,
            HandoffRecord(
                campaign_id=campaign_id,
                coordinator_url=coordinator_url,
                submitted_at=time.time(),
                status_url=status_url,
                idempotency_key=idem_key,
            ),
        )

    print(f"Campaign handed off to Coordinator: {campaign_id}")
    print(f"  Status URL: {status_url}")
    print("  The CLI has exited; the campaign runs on the Coordinator.")
    if outdir is not None:
        print(f"  Monitor with: osimflow status {outdir}")
        print(f"  Download with: osimflow download {outdir}")
    else:
        print("  Monitor with: osimflow status <outdir>")
    return 0


def _print_coordinator_status(record: HandoffRecord) -> int:
    """Poll the Coordinator for live status of a detached campaign.

    Reads the ``campaign_id`` + Coordinator URL from the local handoff record
    (so it works from a fresh shell / rebooted machine) and prints the live
    status returned by ``GET /api/v1/coordinator/campaigns/{id}``.
    """
    status_url = record.status_url or _coordinator_status_url(
        record.coordinator_url, record.campaign_id
    )
    try:
        response = httpx.get(status_url, timeout=15.0)
    except httpx.RequestError as exc:
        print(
            f"error: could not reach Coordinator at {record.coordinator_url} to "
            f"fetch status: {exc}",
            file=sys.stderr,
        )
        return 1

    if response.status_code == 404:
        print(
            f"error: Coordinator has no campaign {record.campaign_id} (HTTP 404). "
            "It may have been deleted, or the Coordinator was reset.",
            file=sys.stderr,
        )
        return 1
    if response.status_code >= 400:
        print(
            f"error: Coordinator returned HTTP {response.status_code} for "
            f"campaign {record.campaign_id}: {response.text}",
            file=sys.stderr,
        )
        return 1

    data = response.json()
    print("Coordinator-backed campaign (detached)")
    print(f"  Campaign ID:  {data.get('campaign_id', record.campaign_id)}")
    print(f"  Name:         {data.get('name', 'unknown')}")
    print(f"  Status:       {data.get('status', 'unknown')}")
    print(f"  Executor:     {data.get('executor', 'unknown')}")
    print(f"  OpenStudio:   {data.get('openstudio_version', 'unknown')}")
    print(f"  Samples:      {data.get('n_samples', 'unknown')}")
    created = data.get("created_at")
    if isinstance(created, (int, float)) and created:
        print(
            f"  Created:      {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(created)))}"
        )
    updated = data.get("updated_at")
    if isinstance(updated, (int, float)) and updated:
        print(
            f"  Updated:      {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(updated)))}"
        )
    print(f"  Status URL:   {status_url}")
    print("  Download:     osimflow download <outdir>")
    return 0


def _download_coordinator_results(
    record: HandoffRecord, output_dir: Path, *, http_get: Any = None
) -> int:
    """Download ONLY the final aggregated artifacts for a detached campaign.

    Fetches ``GET /api/v1/coordinator/campaigns/{id}/results`` and downloads
    the aggregated-results CSV via the Coordinator-provided presigned URL —
    per-sample bytes are deliberately NOT downloaded (issue #630).

    ``http_get`` is an optional injectable callable ``(url, *, timeout,
    follow_redirects) -> response`` used only by tests; production callers
    leave it as ``None`` to use ``httpx.get``.
    """
    results_url = (
        f"{record.coordinator_url.rstrip('/')}"
        f"/api/v1/coordinator/campaigns/{record.campaign_id}/results"
    )
    fetch = http_get if http_get is not None else httpx.get

    try:
        results_response = fetch(results_url, timeout=15.0, follow_redirects=True)
    except httpx.RequestError as exc:
        print(
            f"error: could not reach Coordinator at {record.coordinator_url} to "
            f"list results: {exc}",
            file=sys.stderr,
        )
        return 1
    if results_response.status_code >= 400:
        print(
            f"error: Coordinator returned HTTP {results_response.status_code} "
            f"listing results: {results_response.text}",
            file=sys.stderr,
        )
        return 1

    body = results_response.json()
    result_status = str(body.get("status", "unknown"))
    presigned = body.get("aggregated_results_url")
    agg_key = body.get("aggregated_results_key")

    if not presigned:
        # Nothing to download yet (still running, or aggregation produced no
        # aggregated object). Give the user an actionable, status-aware hint.
        print(
            f"Campaign {record.campaign_id} has no aggregated results to "
            f"download yet (status: {result_status})."
        )
        if result_status in ("pending", "running", "aggregating"):
            print("The campaign is not finished. Re-run this command once it reaches `complete`.")
        elif agg_key:
            print(
                f"An aggregated key is referenced ({agg_key}) but the "
                "Coordinator did not return a presigned URL; check the "
                "Coordinator's S3 permissions."
            )
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / "aggregated_results.csv"
    try:
        file_response = fetch(presigned, timeout=120.0, follow_redirects=True)
    except httpx.RequestError as exc:
        print(f"error: failed to download aggregated results: {exc}", file=sys.stderr)
        return 1
    if file_response.status_code >= 400:
        print(
            f"error: aggregated-results download failed "
            f"(HTTP {file_response.status_code}): {file_response.text}",
            file=sys.stderr,
        )
        return 1

    with open(dest, "wb") as fh:
        fh.write(file_response.content)
    log.info("Downloaded aggregated results for campaign %s -> %s", record.campaign_id, dest)

    print(f"\nDownloaded aggregated results for campaign '{record.campaign_id}' to:")
    print(f"  {dest}")
    print(
        "  (Per-sample bytes were intentionally not downloaded. Run the "
        "campaign locally or query the Coordinator's per-sample API to get them.)"
    )
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


def _cmd_mark_for_reanalysis(args: argparse.Namespace) -> int:
    """Mark a completed/failed sample for re-running (issue #420)."""
    from osimflow.data_point_manager import DataPointManager  # noqa: PLC0415

    outdir: Path = args.outdir.resolve()
    dp_file = outdir / ".osimflow" / "data_points.json"

    if not dp_file.exists():
        print(f"error: data_points.json not found in {outdir}/.osimflow/", file=sys.stderr)
        return 1

    mgr = DataPointManager(outdir=outdir)
    try:
        new_dp = mgr.mark_for_reanalysis(args.sample_id)
    except KeyError as exc:
        print(
            f"error: sample_id {args.sample_id!r} not found in data_points.json: {exc}",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Reanalysis sample created: {new_dp.sample_id}")
    print(f"  Status: {new_dp.status.value}")
    print(f"  Original: {args.sample_id}")
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    """Merge multiple data points into a single target (issue #418)."""
    from osimflow.data_point_manager import DataPointManager  # noqa: PLC0415

    outdir: Path = args.outdir.resolve()
    dp_file = outdir / ".osimflow" / "data_points.json"

    if not dp_file.exists():
        print(f"error: data_points.json not found in {outdir}/.osimflow/", file=sys.stderr)
        return 1

    mgr = DataPointManager(outdir=outdir)
    try:
        target = mgr.merge(
            source_ids=args.source_ids,
            target_id=args.target_id,
            target_work_dir=args.target_work_dir,
        )
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Merged {len(args.source_ids)} source(s) into: {target.sample_id}")
    print(f"  Status: {target.status.value}")
    print(f"  Merged from: {', '.join(args.source_ids)}")
    return 0


def _cmd_warm_cache(args: argparse.Namespace) -> int:
    """Pre-populate the simulation cache before a campaign (issue #427)."""
    _apply_preset(args)
    cfg: CampaignConfig = load_config(vars(args))
    executor = _build_executor(args)
    task_queue = build_task_queue(cfg.task_queue, cfg.dask_scheduler_address)
    campaign = Campaign(
        cfg,
        executor,
        apply_fn=None,
        extract_fn=None,
        max_workers=args.max_workers,
        task_queue=task_queue,
    )
    result: Any = campaign.warm_cache(n_warm=args.n_warm)
    cache_stats: Any = result["cache_stats"]
    print(
        f"Cache warming complete: {result['n_samples']} samples warm, "
        f"{cache_stats['hits']} cache hits, {cache_stats['misses']} misses, "
        f"{cache_stats['total_keys']} total keys"
    )
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    """Run system health checks (issue #411, #1024)."""
    from osimflow.health import (  # noqa: PLC0415
        format_results,
        get_exit_code,
        run_health_checks,
        to_json,
    )

    outdir = args.outdir if args.outdir else Path.cwd()
    report = run_health_checks(
        outdir=outdir,
        skip_network=args.offline,
        configured_executor=args.executor,
    )

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


def _cmd_pause(args: argparse.Namespace) -> int:
    """Request graceful pause of a running campaign.

    Writes a ``.pause`` flag file to the campaign's output directory.
    The campaign orchestrator checks for this file during fan-out and
    waits for in-flight samples to complete before writing the paused
    trace to run.json. Unlike cancellation, pausing allows subsequent
    resume to continue from where the campaign left off.
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

    if run_data.get("finished_at") is not None:
        print(
            f"error: campaign '{run_data.get('campaign_id', 'unknown')}' "
            f"has already completed (finished_at is set)",
            file=sys.stderr,
        )
        return 1

    if run_data.get("status") == "paused":
        print(f"campaign '{run_data.get('campaign_id', outdir.name)}' is already paused")
        return 0

    pause_file = outdir / ".pause"
    if pause_file.exists():
        print(f"pause already requested (--outdir={outdir})")
        return 0

    pause_file.write_text(json_mod.dumps({"requested_at": time.time()}))
    campaign_id = run_data.get("campaign_id", outdir.name)
    print(f"pause requested for campaign '{campaign_id}'")
    print(f"  outdir:  {outdir}")
    print(f"  pause file: {pause_file}")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    """Request resume of a paused campaign.

    Removes the ``.pause`` flag file from the campaign's output directory.
    The campaign orchestrator detects the cleared pause condition and
    continues processing pending samples.
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

    if run_data.get("status") != "paused":
        print(
            f"error: campaign '{run_data.get('campaign_id', outdir.name)}' "
            f"is not paused (status={run_data.get('status')})",
            file=sys.stderr,
        )
        return 1

    pause_file = outdir / ".pause"
    if not pause_file.exists():
        print(
            f"warning: .pause file not found in {outdir}; "
            "campaign may not recognize resume request",
            file=sys.stderr,
        )

    if pause_file.exists():
        pause_file.unlink()
        print(f"pause flag removed from {pause_file}")
    campaign_id = run_data.get("campaign_id", outdir.name)
    print(f"resume requested for campaign '{campaign_id}'")
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


def _cmd_measure_list(args: argparse.Namespace) -> int:
    """List available measures in a template simulation package (issue #532)."""
    import json as json_mod  # noqa: PLC0415

    from osimflow import MeasureRegistry  # noqa: PLC0415

    template_path: Path = args.template
    if not template_path.exists():
        print(f"error: template path not found: {template_path}", file=sys.stderr)
        return 1
    if not template_path.is_dir():
        print(f"error: template path is not a directory: {template_path}", file=sys.stderr)
        return 1

    registry = MeasureRegistry()
    registry.index_measures(template_path)
    measures = registry.list_available_measures()

    if not measures:
        measures_dir = template_path / "measures"
        if measures_dir.is_dir():
            print(f"No measures found in {measures_dir}")
        else:
            print(f"No measures/ directory found in {template_path}")
        return 0

    if args.format == "json":
        print(json_mod.dumps(measures, indent=2, default=str))
        return 0

    # table format (default)
    print(f"Available Measures in {template_path / 'measures/'}:")
    print()
    for m in measures:
        print(f"  - {m['name']} ({m['language']})")
        for arg in m.get("arguments", []):
            default_str = ""
            if arg.get("default") is not None:
                default_str = f", default: {arg['default']}"
            required_str = " [required]" if arg.get("required") else ""
            print(f"    Args: {arg['name']} [{arg['type']}]{default_str}{required_str}")
        if not m.get("arguments"):
            print("    (no arguments discovered)")
        print()

    print(f"Total: {len(measures)} measure(s)")
    return 0


def _cmd_query_results(args: argparse.Namespace) -> int:
    """Query aggregated results across multiple campaigns (issue #585)."""
    import json as json_mod  # noqa: PLC0415

    from osimflow.api.results_query import query_results_cli  # noqa: PLC0415

    campaign_ids = None
    if args.campaign_ids:
        campaign_ids = [c.strip() for c in args.campaign_ids.split(",") if c.strip()]

    outdirs = None
    if args.outdirs:
        outdirs = [o.strip() for o in args.outdirs.split(",") if o.strip()]

    result = query_results_cli(
        campaign_ids=campaign_ids,
        outdirs=outdirs,
        filter_expr=args.filter_expr,
        page=args.page,
        per_page=args.per_page,
        format=args.format,
    )

    if not result["rows"]:
        print("No results found.")
        return 0

    if args.format == "json":
        print(
            json_mod.dumps(
                {
                    "rows": result["rows"],
                    "total": result["total"],
                    "columns": result["columns"],
                    "campaigns_queried": result["campaigns_queried"],
                },
                indent=2,
                default=str,
            )
        )
        return 0

    # table format
    columns = result["columns"]
    if not columns:
        print("No columns available.")
        return 0

    # Print header
    header = "  ".join(f"{c[:20]:<20}" for c in columns)
    print(f"{'COLUMN':<20}  " + header)
    print("-" * 100)

    for i, row in enumerate(result["rows"]):
        print(f"Row {i + 1}: " + "  ".join(f"{str(row.get(c, ''))[:20]:<20}" for c in columns))

    print(
        f"\nTotal: {result['total']} result(s), page "
        f"{args.page}/{max(1, (result['total'] + args.per_page - 1) // args.per_page)}"
    )
    print(f"Campaigns queried: {result['campaigns_queried']}")
    return 0


def _cmd_export_results(args: argparse.Namespace) -> int:
    """Export aggregated results to CSV or JSON (issue #585)."""
    from osimflow.api.results_query import export_results_cli  # noqa: PLC0415

    campaign_ids = None
    if args.campaign_ids:
        campaign_ids = [c.strip() for c in args.campaign_ids.split(",") if c.strip()]

    outdirs = None
    if args.outdirs:
        outdirs = [o.strip() for o in args.outdirs.split(",") if o.strip()]

    return export_results_cli(
        campaign_ids=campaign_ids,
        outdirs=outdirs,
        filter_expr=args.filter_expr,
        format=args.format,
        output_path=args.output,
        include_failed=args.include_failed,
    )


def _friendly_run_input_error(exc: Exception) -> str:
    """Translate a run-input loading failure into a one-line, flag-referencing
    error message (issue #1461).

    ``osimflow.config.load_config`` raises exceptions whose messages name the
    internal snake_case parameters (``variables_yml``, ``template_sim_package``).
    This maps them onto the user-facing CLI flags so the most common first-run
    mistakes surface as one actionable line instead of a raw traceback.
    Multi-line exception text (e.g. YAML parse errors carrying line/column
    context) is flattened to a single line.
    """
    message = " ".join(str(exc).split())

    if isinstance(exc, FileNotFoundError):
        for prefix, prefix_flag in (
            ("variables_yml not found: ", "--input_variables"),
            ("template_sim_package not found: ", "--template_sim_package"),
        ):
            if message.startswith(prefix):
                return f"{prefix_flag}: file not found: {message.removeprefix(prefix)}"
        return f"file not found: {message}"

    flag: str | None = None
    if isinstance(exc, ValidationError):
        field = exc.field
        if field == "template_sim_package":
            flag = "--template_sim_package"
        elif field in ("variables", None) or "variables.yml" in message:
            flag = "--input_variables"
    elif isinstance(exc, yaml.YAMLError):
        flag = "--input_variables"
    if flag is not None:
        return f"{flag}: {message}"
    return f"invalid configuration: {message}"


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912, PLR0915
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
        "aggregate-runs": _cmd_aggregate_runs,
        "status": _cmd_status,
        "download": _cmd_download,
        "cancel": _cmd_cancel,
        "mark-for-reanalysis": _cmd_mark_for_reanalysis,
        "merge": _cmd_merge,
        "pause": _cmd_pause,
        "resume": _cmd_resume,
        "backup": _cmd_backup,
        "restore": _cmd_restore,
        "health": _cmd_health,
        "warm-cache": _cmd_warm_cache,
        "measure": _cmd_measure_list,
        "query-results": _cmd_query_results,
        "export-results": _cmd_export_results,
    }
    handler = dispatch.get(args.command)
    if handler is not None:
        return handler(args)
    if args.command != "run":
        return 1

    # Apply preset before load_config so preset values are in place.
    _apply_preset(args)

    # --- BYOS trust-level hardening (issues #908, #1207) -----------------
    # Reject --byos-trust-level inprocess when --require-trusted-scripts is
    # not explicitly set.  This is the production-hardening guard: it fails
    # fast before any config loading or executor construction so operators
    # get a clear error.
    try:
        validate_trust_level(
            ByosTrustLevel(args.byos_trust_level),
            getattr(args, "require_trusted_scripts", None),
            audit_logger=_byos_audit_logger(args),
            script_path=Path(args.custom_apply_script)
            if getattr(args, "custom_apply_script", None)
            else (
                Path(args.custom_kpi_extractor)
                if getattr(args, "custom_kpi_extractor", None)
                else None
            ),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Fire-and-forget handoff (Phase 2 — issue #602; hardened #630) ---
    # Idempotent POST with a deterministic Idempotency-Key, local handoff
    # record under outdir/, clean exit. See _perform_detach_handoff.
    if args.detach:
        return _perform_detach_handoff(args)

    # Issue #1461: config/input-loading failures (missing --input_variables
    # file, missing --template_sim_package dir, malformed YAML) must surface
    # as a one-line, flag-referencing error — never a raw traceback. The
    # exception types raised by load_config stay untouched; they are
    # translated here at the CLI boundary only.
    try:
        cfg: CampaignConfig = load_config(vars(args))
    except (FileNotFoundError, ValidationError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {_friendly_run_input_error(exc)}", file=sys.stderr)
        print("See 'osimflow run --help' for usage.", file=sys.stderr)
        sys.exit(1)
    executor: BaseExecutor
    if cfg.dry_run:
        executor = LocalExecutor(max_workers=1)
        log.info("DRY RUN: forcing LocalExecutor with 1 worker")
    else:
        executor = _build_executor(args)
    # Issue #1320: cloud executors pull `nrel/openstudio:<version>` from a
    # public registry by MUTABLE tag when --container-digest is not set.
    _warn_if_mutable_tag(args.executor, cfg.container_digest)
    trust_level = ByosTrustLevel(args.byos_trust_level)
    if trust_level == ByosTrustLevel.INPROCESS:
        warnings.warn(
            "BYOS trust level 'inprocess' is a production security concern. "
            "User scripts run in the orchestrator process with full access to "
            "memory, credentials, and the filesystem. Only use 'inprocess' in "
            "trusted development environments. The default 'subprocess' mode "
            "(issue #908) is recommended for production. "
            "Set --byos-trust-level=subprocess to silence this warning.",
            UserWarning,
            stacklevel=1,
        )
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
    _byos_audit = _byos_audit_logger(args)
    apply_fn = (
        load_user_function(
            Path(args.custom_apply_script),
            trust_level=trust_level,
            resource_limits=byos_resource_limits,
            timeout_s=args.byos_timeout_s,
            audit_logger=_byos_audit,
        )
        if args.custom_apply_script
        else None
    )
    extract_fn = (
        load_user_function(
            Path(args.custom_kpi_extractor),
            trust_level=trust_level,
            resource_limits=byos_resource_limits,
            timeout_s=args.byos_timeout_s,
            audit_logger=_byos_audit,
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
