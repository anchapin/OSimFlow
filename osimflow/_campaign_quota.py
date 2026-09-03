"""Quota enforcement for Campaign (issue #1462 extraction).

This module extracts resource-quota enforcement from the Campaign
class (issue #446 originally introduced the quota guards):

- fail-fast start-time quota checks (``max_samples``),
- mid-campaign hard-limit checks (``max_samples`` / ``max_cost_usd`` /
  ``max_wall_time_min``),
- fan-out parallelism bounding (``max_concurrent_samples``).

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
                return True

        if quota.max_cost_usd is not None and self._trace.total_cost_usd >= quota.max_cost_usd:
            log.warning(
                "max_cost_usd quota reached (%.2f >= %.2f) — skipping further submissions",
                self._trace.total_cost_usd,
                quota.max_cost_usd,
            )
            return True

        elapsed_min = (time.time() - self._trace.started_at) / 60.0
        if quota.max_wall_time_min is not None and elapsed_min >= quota.max_wall_time_min:
            log.warning(
                "max_wall_time_min quota reached (%.1f >= %.1f min) — skipping further submissions",
                elapsed_min,
                quota.max_wall_time_min,
            )
            return True

        return False

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
