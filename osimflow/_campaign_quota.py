"""Quota enforcement for Campaign (issue #1462 extraction).

This module extracts resource-quota enforcement from the Campaign
class (issue #446 originally introduced the quota guards):

- fail-fast start-time quota checks (``max_samples``),
- mid-campaign hard-limit checks (``max_samples`` / ``max_cost_usd`` /
  ``max_wall_time_min``),
- fan-out parallelism bounding (``max_concurrent_samples``).

Since issue #1533 the guard is wired into ``Campaign.run()``: the
start check runs before the init hook, and every fan-out submission
loop (APPLY_PARAMETERS / RUN_OPENSTUDIO_SIM / EXTRACT_KPIS) calls
``check_quota_exceeded()`` at each chunk boundary, stopping new
submissions (and firing the once-per-campaign ``quota.exceeded``
alert) when a hard limit trips.

The ``CampaignQuotaGuard`` is constructed with the campaign config,
the live :class:`~osimflow.monitoring.RunTrace`, the shared
per-sample state dict, the configured ``max_workers``, and an alert
callback — mirroring the ``_campaign_cost_tracker.py`` collaborator
pattern.  ``QuotaExceededError`` is defined here and re-exported from
``osimflow.campaign`` (it stays in the public ``__all__``).
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from .config import CampaignConfig
from .errors import OSimFlowRuntimeError
from .monitoring import RunTrace

log = logging.getLogger("osimflow.campaign")


class QuotaExceededError(OSimFlowRuntimeError):
    """Raised when a campaign resource quota is exceeded (issue #446)."""

    def __init__(
        self,
        message: str,
        quota_type: str,
        limit: int | float,
        current: int | float,
    ) -> None:
        super().__init__(message)
        self.quota_type = quota_type
        self.limit = limit
        self.current = current


class CampaignQuotaGuard:
    """Owns all ``resource_quota`` enforcement for a Campaign.

    Parameters mirror the Campaign attributes the guard reads live:
    ``trace`` (cost totals / start time), ``sample_state`` (submitted
    sample count), and ``max_workers`` (parallelism bounding).  The
    dict and trace are passed by reference so mid-campaign mutations
    are visible without re-wiring.
    """

    def __init__(
        self,
        cfg: CampaignConfig,
        trace: RunTrace,
        sample_state: dict[str, dict[str, object]],
        max_workers: int,
        maybe_alert: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self._cfg = cfg
        self._trace = trace
        self._sample_state = sample_state
        self._max_workers = max_workers
        self._maybe_alert = maybe_alert
        # Mid-campaign stop alerts fire once per campaign (issue #1533):
        # every fan-out chunk boundary re-checks the quota, but the
        # ``quota.exceeded`` alert must not storm on each check.
        self._quota_stop_alerted = False

    def enforce_start_quota(self) -> None:
        """Fail fast if the campaign's resource quota is already exceeded at start.

        Called at the beginning of ``run()`` before any work is dispatched.
        Raises ``QuotaExceededError`` with a descriptive message if any quota
        is already violated.
        """
        quota = self._cfg.resource_quota
        if quota is None:
            return

        if quota.max_samples is not None and self._cfg.n_samples > quota.max_samples:
            self._maybe_alert(
                "quota.exceeded",
                {
                    "campaign_id": self._trace.campaign_id,
                    "quota_type": "max_samples",
                    "limit": quota.max_samples,
                    "current": self._cfg.n_samples,
                    "message": f"n_samples={self._cfg.n_samples} exceeds max_samples={quota.max_samples}",
                },
            )
            raise QuotaExceededError(
                f"n_samples={self._cfg.n_samples} exceeds resource_quota.max_samples="
                f"{quota.max_samples}",
                quota_type="max_samples",
                limit=quota.max_samples,
                current=self._cfg.n_samples,
            )

        log.info(
            "resource quota active: max_samples=%s, max_cost_usd=%s, "
            "max_wall_time_min=%s, max_concurrent_samples=%s",
            quota.max_samples,
            quota.max_cost_usd,
            quota.max_wall_time_min,
            quota.max_concurrent_samples,
        )

    def check_quota_exceeded(self) -> bool:
        """Return True if any hard quota limit has been reached.

        Checks:
        - ``max_samples``: total samples submitted so far vs. the limit.
        - ``max_cost_usd``: accumulated campaign cost vs. the limit.
        - ``max_wall_time_min``: elapsed campaign time vs. the limit.

        Does NOT check ``max_concurrent_samples`` — that is enforced
        by bounding ``max_workers`` at construction time.

        When a hard limit trips, the ``quota.exceeded`` alert fires
        (once per campaign — issue #1533) so the documented alert
        surface matches runtime behaviour.
        """
        quota = self._cfg.resource_quota
        if quota is None:
            return False

        if quota.max_samples is not None:
            submitted = sum(
                1
                for state in self._sample_state.values()
                if any(k.endswith("_exit_code") or k.endswith("_status") for k in state)
            )
            if submitted >= quota.max_samples:
                log.warning(
                    "max_samples quota reached (%d >= %d) — skipping further submissions",
                    submitted,
                    quota.max_samples,
                )
                self._alert_quota_stop("max_samples", quota.max_samples, submitted)
                return True

        if quota.max_cost_usd is not None:
            accrued = self._accrued_cost_usd()
            if accrued >= quota.max_cost_usd:
                log.warning(
                    "max_cost_usd quota reached (%.2f >= %.2f) — skipping further submissions",
                    accrued,
                    quota.max_cost_usd,
                )
                self._alert_quota_stop("max_cost_usd", quota.max_cost_usd, accrued)
                return True

        elapsed_min = (time.time() - self._trace.started_at) / 60.0
        if quota.max_wall_time_min is not None and elapsed_min >= quota.max_wall_time_min:
            log.warning(
                "max_wall_time_min quota reached (%.1f >= %.1f min) — skipping further submissions",
                elapsed_min,
                quota.max_wall_time_min,
            )
            self._alert_quota_stop("max_wall_time_min", quota.max_wall_time_min, elapsed_min)
            return True

        return False

    def _accrued_cost_usd(self) -> float:
        """Return the campaign cost accrued so far (issue #1533).

        Mid-campaign, per-sample costs live in the shared
        ``_sample_state`` dict (populated by the RUN_OPENSTUDIO_SIM
        fan-out as handles report ``cost_usd``); ``trace.total_cost_usd``
        is only refreshed from those per-sample values at finalize time.
        Sum the per-sample costs and take the max with the trace total
        so the check works at any point without double counting (the
        trace total and the sample-state sum are the same figure once
        both are populated).
        """
        accrued = 0.0
        for state in self._sample_state.values():
            cost_obj = state.get("cost_usd")
            if cost_obj is None:
                continue
            try:
                accrued += float(str(cost_obj))
            except (TypeError, ValueError):
                continue
        return max(accrued, self._trace.total_cost_usd)

    def _alert_quota_stop(
        self,
        quota_type: str,
        limit: int | float,
        current: int | float,
    ) -> None:
        """Fire the ``quota.exceeded`` alert once per campaign (issue #1533).

        The fan-out loops call :meth:`check_quota_exceeded` at every
        chunk boundary; this dedup guard keeps the alert surface at
        one notification per campaign regardless of how many checks
        trip.
        """
        if self._quota_stop_alerted:
            return
        self._quota_stop_alerted = True
        self._maybe_alert(
            "quota.exceeded",
            {
                "campaign_id": self._trace.campaign_id,
                "quota_type": quota_type,
                "limit": limit,
                "current": current,
                "message": (
                    f"{quota_type} quota reached ({current} >= {limit}) — "
                    "stopping further sample submissions"
                ),
            },
        )

    def effective_max_workers(self) -> int:
        """Return the effective max_workers bounded by max_concurrent_samples quota.

        If a ``max_concurrent_samples`` quota is set, the fan-out parallelism
        is capped to that value. Otherwise, the configured ``max_workers``
        is returned unchanged.
        """
        quota = self._cfg.resource_quota
        if quota is not None and quota.max_concurrent_samples is not None:
            return min(self._max_workers, quota.max_concurrent_samples)
        return self._max_workers
