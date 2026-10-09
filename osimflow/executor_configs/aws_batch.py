"""AWS Batch executor configuration + CLI flags (issue #1575).

Owns the ``AWSBatchConfig`` dataclass and the ``--aws-batch-*`` /
``--ecr-repository`` flags that ``osimflow run``/``osimflow warm-cache``
register for the ``aws_batch`` executor. Registered as the
``aws_batch`` argument hook by ``osimflow/executor_configs/__init__.py``.
"""

import argparse
import dataclasses
from typing import Any


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
    on_demand_job_queue
        On-demand-capable Batch job queue the fallback resubmits to
        (required with ``fallback_to_on_demand``; issue #1816).
    on_demand_job_definition
        Optional job definition used for the on-demand fallback.
    max_retries
        Maximum number of times a spot-interrupted job is retried before
        falling back or failing.
    submit_rps
        Submit rate-limit in requests per second applied via a shared
        token-bucket limiter (default 800, below AWS Batch's 1000 TPS
        account limit — issue #1010).
    payload_secret_arn
        ARN of an AWS Secrets Manager secret or SSM Parameter Store
        parameter holding the task-payload HMAC secret. When set, the
        secret is injected by the job definition's
        ``containerProperties.secrets`` (SubmitJob has no
        ``containerOverrides.secrets``; issue #1811). The executor
        validates before submission that the job definition maps
        ``OSIMFLOW_TASK_PAYLOAD_SECRET`` to this ARN. The job's
        *execution* role needs ``secretsmanager:GetSecretValue`` or
        ``ssm:GetParameter`` + ``kms:Decrypt`` on it.
    allow_long_lived_credentials
        Opt in to env / shared-file / SSO credentials instead of only
        IAM-role credentials (issue #1833). Default off; not recommended
        for production.
    """

    max_spot_price_usd: float | None = None
    fallback_to_on_demand: bool = False
    on_demand_job_queue: str | None = None
    on_demand_job_definition: str | None = None
    max_retries: int = 3
    submit_rps: float | None = None
    payload_secret_arn: str | None = None
    allow_long_lived_credentials: bool = False


def add_arguments(parser_group: argparse.ArgumentParser) -> None:
    """Register the ``aws_batch`` executor's ``run`` flags (issue #1575)."""
    parser_group.add_argument("--aws-batch-queue", default="osimflow-batch-queue")
    parser_group.add_argument("--aws-batch-job-definition", default=None)
    parser_group.add_argument(
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
    parser_group.add_argument(
        "--aws-batch-fallback-to-on-demand",
        action="store_true",
        help=(
            "When the Spot price exceeds the ceiling or max retries are "
            "exhausted, fall back to the on-demand job queue instead of "
            "failing. Requires --aws-batch-max-spot-price-usd or spot "
            "interruption retries."
        ),
    )
    parser_group.add_argument(
        "--aws-batch-on-demand-queue",
        default=None,
        help=(
            "On-demand-capable Batch job queue used by "
            "--aws-batch-fallback-to-on-demand. Required with the fallback "
            "flag and must differ from --aws-batch-queue (issue #1816)."
        ),
    )
    parser_group.add_argument(
        "--aws-batch-on-demand-job-definition",
        default=None,
        help="Optional job definition used for the on-demand fallback.",
    )
    parser_group.add_argument(
        "--aws-batch-max-retries",
        type=int,
        default=3,
        help=(
            "Maximum number of retries on Spot interruption (default: 3). "
            "Each retry uses exponential backoff. After exhausting retries, "
            "the job fails unless --aws-batch-fallback-to-on-demand is set."
        ),
    )
    parser_group.add_argument(
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
    parser_group.add_argument(
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
    parser_group.add_argument(
        "--ecr-repository",
        default=None,
        help=(
            "ECR repository URI for OpenStudio container images "
            "(e.g. 123456.dkr.ecr.us-east-1.amazonaws.com/osimflow/openstudio). "
            "When set, the Batch executor pulls from ECR instead of Docker Hub."
        ),
    )
    parser_group.add_argument(
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
    parser_group.add_argument(
        "--aws-batch-on-demand-price",
        type=float,
        default=None,
        help=(
            "AWS Batch on-demand price in USD per vCPU-hour for cost tracking "
            "(issue #447). When set alongside --track-costs, this rate is used "
            "instead of the default $0.0132/vCPU·hr to estimate job costs."
        ),
    )
    parser_group.add_argument(
        "--aws-batch-payload-secret-arn",
        default=None,
        help=(
            "ARN of an AWS Secrets Manager secret (or SSM Parameter Store "
            "parameter) holding the task-payload HMAC secret "
            "(key/parameter value = OSIMFLOW_TASK_PAYLOAD_SECRET). When set, "
            "the job definition must inject it via containerProperties.secrets "
            "(validated via describe_job_definitions before submission; "
            "SubmitJob has no containerOverrides.secrets) so the raw "
            "secret never appears in the job spec where it is readable via "
            "DescribeJobs (issues #1633/#1811). "
            "The job's EXECUTION role needs secretsmanager:GetSecretValue (or "
            "ssm:GetParameter + kms:Decrypt) on the ARN. Requires "
            "OSIMFLOW_TASK_PAYLOAD_SECRET on the orchestrator with the "
            "same value for signing. See docs/secret-management.md."
        ),
    )
    parser_group.add_argument(
        "--aws-batch-allow-long-lived-credentials",
        action="store_true",
        default=False,
        help=(
            "Allow the AWS Batch executor to use long-lived / workstation "
            "credentials (AWS_ACCESS_KEY_ID, shared credentials file, SSO "
            "or OIDC env credentials) instead of only IAM-role credentials "
            "from the EC2/ECS metadata service (issue #1833). Default off; "
            "a warning is logged when enabled. Not recommended for production."
        ),
    )


def kwargs_for_executor(**kwargs: Any) -> dict[str, Any]:
    """Translate the flat CLI / API kwargs into ``AWSBatchExecutor`` kwargs (issue #1681).

    Mirrors the pre-#1681 CLI precedence: the substrate-agnostic
    ``--submit-rps`` (or the API's ``submit_rps`` field) overrides the
    legacy ``--aws-batch-submit-rps`` when both are set (issue #1563).
    """
    submit_rps = kwargs.get("submit_rps")
    legacy_submit_rps = kwargs.get("aws_batch_submit_rps")
    if submit_rps is None:
        submit_rps = legacy_submit_rps
    max_spot_price_usd = kwargs.get("aws_batch_max_spot_price_usd")
    return {
        "job_queue": kwargs.get("aws_batch_queue") or "osimflow-batch-queue",
        "job_definition": kwargs.get("aws_batch_job_definition"),
        "max_spot_price_usd": (
            float(max_spot_price_usd) if max_spot_price_usd is not None else None
        ),
        "fallback_to_on_demand": bool(kwargs.get("aws_batch_fallback_to_on_demand", False)),
        "on_demand_job_queue": kwargs.get("aws_batch_on_demand_queue"),
        "on_demand_job_definition": kwargs.get("aws_batch_on_demand_job_definition"),
        "max_retries": int(kwargs.get("aws_batch_max_retries", 3)),
        "instance_type": kwargs.get("aws_batch_instance_type"),
        "submit_rps": submit_rps,
        "payload_secret_arn": kwargs.get("aws_batch_payload_secret_arn"),
        "allow_long_lived_credentials": bool(
            kwargs.get("aws_batch_allow_long_lived_credentials", False)
        ),
    }
