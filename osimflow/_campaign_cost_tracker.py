"""CampaignCostTracker for Campaign — wraps CostTracker lifecycle and accumulation.

This module extracts cost tracking operations from the Campaign class,
including:
- CostTracker construction from CampaignConfig
- Per-step cost recording
- Per-sample cost accumulation
- Campaign-level cost summary finalization

The CampaignCostTracker is constructed with a CampaignConfig and a campaign_id,
and exposes methods for the Campaign to record and accumulate costs
throughout the campaign lifecycle.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import CampaignConfig
from .cost_tracking import CostTracker
from .executors import AWSBatchExecutor

log = logging.getLogger("osimflow.campaign")

_EXECUTORS_WITH_SPOT = frozenset({"aws_batch"})
_EXECUTORS_WITH_FLAT_RATE = frozenset(
    {
        "slurm",
        "pbs",
        "nomad",
        "kubernetes",
        "dask_jobqueue",
        "local",
        "docker_swarm",
    }
)


class CampaignCostTracker:
    """Manages cost tracker lifecycle and per-sample cost accumulation.

    This class encapsulates cost tracking operations for a Campaign,
    constructing the underlying CostTracker at initialization and
    providing methods to record per-step costs and accumulate
    campaign-level totals.

    Parameters
    ----------
    campaign_id
        Unique campaign identifier (used for CostTracker initialization).
    cfg
        Campaign configuration used to determine pricing and backend.
    executor_name
        Name of the executor (e.g., "aws_batch", "slurm", "local").

    Attributes
    ----------
    tracker : CostTracker | None
        The underlying cost tracker, or None if cost tracking is disabled.
    """

    def __init__(
        self,
        campaign_id: str,
        cfg: CampaignConfig,
        executor_name: str,
    ) -> None:
        self._executor_name = executor_name
        self._tracker: CostTracker | None = self._build_tracker(
            campaign_id=campaign_id,
            cfg=cfg,
            executor_name=executor_name,
        )

    # ------------------------------------------------------------------
    # Tracker construction
    # ------------------------------------------------------------------
    @staticmethod
    def _build_tracker(
        campaign_id: str,
        cfg: CampaignConfig,
        executor_name: str,
    ) -> CostTracker | None:
        """Build a CostTracker from CampaignConfig, or None if disabled.

        Returns None when ``cfg.enable_cost_tracking`` is False (zero overhead).
        """
        if not cfg.enable_cost_tracking:
            return None
        try:
            from .cost_tracking import build_cost_tracker  # noqa: PLC0415

            return build_cost_tracker(
                campaign_id=campaign_id,
                executor_type=executor_name,
                result_storage_backend=cfg.result_storage_backend,
                result_storage_bucket=cfg.result_storage_bucket,
                result_storage_prefix=str(cfg.outdir.name),
                result_storage_endpoint=cfg.result_storage_endpoint,
                track_costs=cfg.enable_cost_tracking,
                aws_on_demand_per_vcpu_hour=cfg.cost_on_demand_price,
                aws_spot_per_vcpu_hour=cfg.cost_spot_price,
                slurm_cost_per_node_hour=getattr(cfg, "slurm_cost_per_node_hour", 0.0),
            )
        except Exception as exc:
            log.warning("could not initialize cost tracker: %s — continuing without", exc)
            return None

    # ------------------------------------------------------------------
    # Per-step cost recording
    # ------------------------------------------------------------------
    def record_step_costs(
        self,
        step_name: str,
        cost_usd: float,
        spot_savings_usd: float = 0.0,
    ) -> None:
        """Record aggregated costs from a completed fan-out step.

        Parameters
        ----------
        step_name
            The step name (e.g., "APPLY_PARAMETERS", "RUN_OPENSTUDIO_SIM").
        cost_usd
            Total cost in USD for the step.
        spot_savings_usd
            Estimated spot savings for the step.
        """
        if self._tracker is None:
            return
        self._tracker.record_actual(step_name, cost_usd, spot_savings_usd)

    # ------------------------------------------------------------------
    # Cost accumulation helpers
    # ------------------------------------------------------------------
    def sum_sample_costs(self, sample_state: dict[str, dict[str, object]]) -> tuple[float, float]:
        """Sum per-sample costs from sample state accumulator.

        Parameters
        ----------
        sample_state
            The Campaign's ``_sample_state`` dict mapping sample_id to
            per-sample state dicts containing ``cost_usd`` entries.

        Returns
        -------
        tuple[float, float]
            Tuple of (total_cost, total_savings) where savings is
            computed using the executor-specific pricing model.
        """
        total_cost = 0.0
        for state in sample_state.values():
            cost_usd = state.get("cost_usd")
            if cost_usd is not None:
                total_cost += float(str(cost_usd))
        return total_cost, self.compute_spot_savings(total_cost)

    def compute_spot_savings(self, total_cost: float) -> float:
        """Compute estimated spot savings from on-demand total cost.

        Uses an executor-specific pricing model:

        - **AWS Batch**: Uses the configured on-demand vs. spot price ratio
          (default: ~40% savings — $0.05 on-demand vs. $0.03 spot).
        - **Flat-rate executors** (Slurm, PBS, Nomad, Kubernetes,
          Dask-JobQueue, Local, Docker Swarm): These bill at flat
          node-hour rates with no spot market — returns 0.0.
        - **Other cloud executors** (Azure, Google Batch): Falls back
          to the AWS Batch ratio until executor-specific pricing is
          implemented (issue #1190).

        Parameters
        ----------
        total_cost
            Total on-demand cost in USD.

        Returns
        -------
        float
            Estimated savings if jobs ran on spot instances (0.0 for
            flat-rate executors).
        """
        if total_cost <= 0:
            return 0.0
        if self._executor_name in _EXECUTORS_WITH_FLAT_RATE:
            return 0.0
        savings_ratio = (
            AWSBatchExecutor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR
            - AWSBatchExecutor.DEFAULT_SPOT_PRICE_PER_VCPU_HOUR
        ) / AWSBatchExecutor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR
        return round(total_cost * savings_ratio, 6)

    # ------------------------------------------------------------------
    # Campaign-level summary
    # ------------------------------------------------------------------
    def finalize(self) -> dict[str, Any] | None:
        """Build and return the campaign cost summary.

        Returns None if cost tracking is disabled.

        Returns
        -------
        dict[str, Any] | None
            Cost summary dict suitable for assigning to ``RunTrace.cost_summary``,
            or None if cost tracking is not enabled.
        """
        if self._tracker is None:
            return None
        try:
            summary = self._tracker.finalize()
            return summary.to_dict()
        except Exception as exc:
            log.warning("could not finalize cost summary: %s", exc, exc_info=True)
            return None

    @property
    def is_enabled(self) -> bool:
        """Return True if cost tracking is enabled."""
        return self._tracker is not None
