"""Campaign cost tracking for cloud and HPC resources (issue #447).

This module provides cost estimation and tracking for simulation campaigns
running on cloud (AWS Batch, Azure Batch, Google Cloud Batch) and HPC
(Slurm, PBS, etc.) executors.

Classes
-------
CostEstimate
    Per-sample cost estimate with breakdown by resource type.
CostTracker
    Tracks costs during campaign execution and accumulates totals.
CampaignCostSummary
    Final campaign-level cost summary written to run.json.
"""

from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import ResultStorage, build_result_storage

import logging

log = logging.getLogger(__name__)


# Default pricing (USD per vCPU-hour) for on-demand instances.
# These are used when cloud provider APIs are unavailable.
DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR = 0.05  # ~$0.05/vCPU·h = $120/vCPU·month
DEFAULT_SPOT_PRICE_PER_VCPU_HOUR = 0.03  # ~40% savings vs on-demand


@dataclasses.dataclass
class CostEstimate:
    """Per-sample cost estimate with breakdown.

    Attributes
    ----------
    sample_id : str
        Sample identifier.
    estimated_cost_usd : float
        Estimated on-demand cost in USD.
    estimated_spot_cost_usd : float
        Estimated spot/preemptible cost in USD (lower than on-demand).
    estimated_duration_seconds : float
        Estimated wall-clock duration in seconds.
    vcpus : int
        Number of vCPUs allocated for this sample.
    memory_mb : int
        Memory in megabytes allocated for this sample.
    executor : str
        Executor name (e.g., "aws_batch", "slurm").
    created_at : float
        Unix timestamp when this estimate was created.
    """

    sample_id: str
    estimated_cost_usd: float
    estimated_spot_cost_usd: float
    estimated_duration_seconds: float
    vcpus: int = 1
    memory_mb: int = 1024
    executor: str = "unknown"
    created_at: float = dataclasses.field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "estimated_cost_usd": self.estimated_cost_usd,
            "estimated_spot_cost_usd": self.estimated_spot_cost_usd,
            "estimated_duration_seconds": self.estimated_duration_seconds,
            "vcpus": self.vcpus,
            "memory_mb": self.memory_mb,
            "executor": self.executor,
            "created_at": self.created_at,
        }


@dataclasses.dataclass
class CampaignCostSummary:
    """Campaign-level cost summary.

    Written to ``run.json`` under the ``cost_summary`` key so users can
    see total estimated cost before running and actual cost after completion.

    Attributes
    ----------
    total_estimated_cost_usd : float
        Sum of all per-sample estimated costs.
    total_actual_cost_usd : float
        Sum of all per-sample actual costs (from executor).
    total_spot_savings_usd : float
        Estimated savings if spot instances were used throughout.
    n_samples : int
        Total number of samples in the campaign.
    executor : str
        Executor name used for the campaign.
    created_at : float
        Unix timestamp when this summary was created.
    finalized_at : float | None
        Unix timestamp when costs were finalized (None if not yet finalized).
    """

    total_estimated_cost_usd: float = 0.0
    total_actual_cost_usd: float = 0.0
    total_spot_savings_usd: float = 0.0
    n_samples: int = 0
    executor: str = "unknown"
    created_at: float = dataclasses.field(default_factory=time.time)
    finalized_at: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "total_estimated_cost_usd": self.total_estimated_cost_usd,
            "total_actual_cost_usd": self.total_actual_cost_usd,
            "total_spot_savings_usd": self.total_spot_savings_usd,
            "n_samples": self.n_samples,
            "executor": self.executor,
            "created_at": self.created_at,
            "finalized_at": self.finalized_at,
        }


class CostTracker:
    """Tracks costs during campaign execution.

    The tracker maintains per-sample cost estimates before execution and
    accumulates actual costs after each sample completes. It provides
    a running estimate of total campaign cost and spot savings.

    Usage
    -----
    1. Create tracker with campaign configuration.
    2. Call ``record_estimate()`` to add per-sample estimates.
    3. Call ``record_actual()`` to record actual costs after sample completion.
    4. Call ``finalize()`` to produce the final ``CampaignCostSummary``.

    Example
    -------
    >>> tracker = CostTracker(n_samples=100, executor="aws_batch")
    >>> tracker.record_estimate(CostEstimate("sample_001", 0.50, 0.30, 3600.0))
    >>> tracker.record_actual("sample_001", 0.52, 0.31)
    >>> summary = tracker.finalize()
    """

    def __init__(
        self,
        n_samples: int,
        executor: str,
        on_demand_price: float = DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR,
        spot_price: float = DEFAULT_SPOT_PRICE_PER_VCPU_HOUR,
    ):
        """Initialize the cost tracker.

        Parameters
        ----------
        n_samples : int
            Total number of samples in the campaign.
        executor : str
            Executor name (e.g., "aws_batch", "slurm", "local").
        on_demand_price : float
            On-demand price per vCPU-hour (default: $0.05).
        spot_price : float
            Spot price per vCPU-hour (default: $0.03).
        """
        self.n_samples = n_samples
        self.executor = executor
        self.on_demand_price = on_demand_price
        self.spot_price = spot_price

        self._estimates: dict[str, CostEstimate] = {}
        self._actuals: dict[str, tuple[float, float]] = {}  # sample_id -> (cost, savings)
        self._created_at = time.time()

    def record_estimate(self, estimate: CostEstimate) -> None:
        """Record a per-sample cost estimate.

        Parameters
        ----------
        estimate : CostEstimate
            The cost estimate for a single sample.
        """
        self._estimates[estimate.sample_id] = estimate

    def record_actual(
        self,
        sample_id: str,
        actual_cost_usd: float,
        spot_savings_usd: float = 0.0,
    ) -> None:
        """Record actual cost for a completed sample.

        Parameters
        ----------
        sample_id : str
            Sample identifier.
        actual_cost_usd : float
            Actual on-demand cost in USD.
        spot_savings_usd : float
            Spot savings (difference between on-demand and spot).
        """
        self._actuals[sample_id] = (actual_cost_usd, spot_savings_usd)

    @property
    def total_estimated_cost_usd(self) -> float:
        """Sum of all per-sample estimated costs."""
        return sum(e.estimated_cost_usd for e in self._estimates.values())

    @property
    def total_actual_cost_usd(self) -> float:
        """Sum of all per-sample actual costs."""
        return sum(cost for cost, _ in self._actuals.values())

    @property
    def total_spot_savings_usd(self) -> float:
        """Total estimated spot savings across all samples."""
        return sum(savings for _, savings in self._actuals.values())

    @property
    def n_recorded(self) -> int:
        """Number of samples with actual costs recorded."""
        return len(self._actuals)

    @property
    def is_complete(self) -> bool:
        """True if all samples have actual costs recorded."""
        return len(self._actuals) >= self.n_samples

    def get_estimate(self, sample_id: str) -> CostEstimate | None:
        """Get the estimate for a specific sample.

        Parameters
        ----------
        sample_id : str
            Sample identifier.

        Returns
        -------
        CostEstimate | None
            The estimate if available, None otherwise.
        """
        return self._estimates.get(sample_id)

    def finalize(self) -> CampaignCostSummary:
        """Produce the final campaign cost summary.

        Returns
        -------
        CampaignCostSummary
            Summary containing estimated and actual costs.
        """
        return CampaignCostSummary(
            total_estimated_cost_usd=round(self.total_estimated_cost_usd, 6),
            total_actual_cost_usd=round(self.total_actual_cost_usd, 6),
            total_spot_savings_usd=round(self.total_spot_savings_usd, 6),
            n_samples=self.n_samples,
            executor=self.executor,
            created_at=self._created_at,
            finalized_at=time.time(),
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize tracker state for run.json."""
        return {
            "n_samples": self.n_samples,
            "n_recorded": self.n_recorded,
            "is_complete": self.is_complete,
            "total_estimated_cost_usd": self.total_estimated_cost_usd,
            "total_actual_cost_usd": self.total_actual_cost_usd,
            "total_spot_savings_usd": self.total_spot_savings_usd,
            "executor": self.executor,
            "estimates": {sid: est.to_dict() for sid, est in self._estimates.items()},
        }


# Factory
# ---------------------------------------------------------------------------


def build_cost_tracker(
    campaign_id: str,
    executor_type: str,
    result_storage_backend: str = "local",
    result_storage_bucket: str = "",
    result_storage_prefix: str = "",
    result_storage_endpoint: str | None = None,
    *,
    track_costs: bool = False,
    aws_on_demand_per_vcpu_hour: float = DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR,
    aws_spot_per_vcpu_hour: float = DEFAULT_SPOT_PRICE_PER_VCPU_HOUR,
    slurm_cost_per_node_hour: float = DEFAULT_SPOT_PRICE_PER_VCPU_HOUR,
) -> "CostTracker | None":
    """Factory: build a CostTracker if cost tracking is enabled.

    Parameters
    ----------
    campaign_id
        Unique campaign identifier.
    executor_type
        Name of the executor (e.g. ``"aws_batch"``, ``"slurm"``).
    result_storage_backend
        One of ``"local"``, ``"s3"``, ``"gs"``, ``"azure"``.
    result_storage_bucket
        Bucket/container name for remote backends.
    result_storage_prefix
        Prefix for remote paths.
    result_storage_endpoint
        S3-compatible endpoint URL (for MinIO, R2, etc.).
    track_costs
        If False, returns None (cost tracking disabled).
    aws_on_demand_per_vcpu_hour
        Custom AWS Batch on-demand price.
    aws_spot_per_vcpu_hour
        Custom AWS Batch Spot price.
    slurm_cost_per_node_hour
        Custom Slurm per-node-hour price.

    Returns
    -------
    CostTracker or None
        None when ``track_costs`` is False.
    """
    if not track_costs:
        return None

    from .storage import ResultStorage, build_result_storage

    storage: ResultStorage | None = None
    if result_storage_backend != "local":
        try:
            storage = build_result_storage(
                backend=result_storage_backend,
                bucket=result_storage_bucket,
                prefix=result_storage_prefix,
                endpoint_url=result_storage_endpoint,
            )
        except Exception as exc:
            log.warning("could not build ResultStorage for cost tracking: %s", exc, exc_info=True)

    return CostTracker(
        campaign_id=campaign_id,
        executor_type=executor_type,
        result_storage=storage,
        aws_on_demand_per_vcpu_hour=aws_on_demand_per_vcpu_hour,
        aws_spot_per_vcpu_hour=aws_spot_per_vcpu_hour,
        slurm_cost_per_node_hour=slurm_cost_per_node_hour,
    )
