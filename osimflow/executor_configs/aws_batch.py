"""AWS Batch executor configuration + CLI flags (issue #1575).

Owns the ``AWSBatchConfig`` dataclass and the ``--aws-batch-*`` /
``--ecr-repository`` flags that ``osimflow run``/``osimflow warm-cache``
register for the ``aws_batch`` executor. Registered as the
``aws_batch`` argument hook by ``osimflow/executor_configs/__init__.py``.
"""

import argparse
import dataclasses


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
