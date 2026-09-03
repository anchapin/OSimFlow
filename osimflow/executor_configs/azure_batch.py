"""Azure Batch executor configuration + CLI flags (issue #1575).

Owns the ``AzureBatchConfig`` dataclass and the ``--azure-*`` flags
that ``osimflow run``/``osimflow warm-cache`` register for the
``azure_batch`` executor. Registered as the ``azure_batch`` argument
hook by ``osimflow/executor_configs/__init__.py``.
"""

import argparse
import dataclasses


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


def add_arguments(parser_group: argparse.ArgumentParser) -> None:
    """Register the ``azure_batch`` executor's ``run`` flags (issue #1575)."""
    parser_group.add_argument(
        "--azure-batch-account-name",
        default=None,
        help="Azure Batch account name (e.g. osimflowbatch).",
    )
    parser_group.add_argument(
        "--azure-batch-account-url",
        default=None,
        help="Azure Batch account URL (e.g. https://osimflowbatch.eastus.batch.azure.com).",
    )
    parser_group.add_argument(
        "--azure-batch-pool-id",
        default="osimflow-pool",
        help="Azure Batch pool ID (default: osimflow-pool).",
    )
    parser_group.add_argument(
        "--azure-batch-location",
        default="eastus",
        help="Azure region/location for the Batch account (default: eastus).",
    )
    parser_group.add_argument(
        "--azure-use-spot",
        action="store_true",
        help=(
            "Use Azure Spot VMs (low-priority) for Batch tasks (issue #352). "
            "When a Spot interruption occurs, the executor retries up to "
            "--azure-max-retries times before falling back to on-demand "
            "(if --azure-fallback-to-on-demand is set) or failing."
        ),
    )
    parser_group.add_argument(
        "--azure-fallback-to-on-demand",
        action="store_true",
        help=(
            "When Azure Spot retries are exhausted, fall back to on-demand "
            "VMs instead of failing (issue #352). Requires --azure-use-spot."
        ),
    )
    parser_group.add_argument(
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
