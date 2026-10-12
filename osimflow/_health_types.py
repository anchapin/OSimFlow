"""CheckStatus / CheckCategory / CheckResult / HealthReport split out of ``health.py`` (issue #1890)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

log = logging.getLogger("osimflow.health")


class CheckStatus(StrEnum):
    """Status of an individual health check."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class CheckCategory(StrEnum):
    """Severity category for a health check."""

    CRITICAL = "critical"
    INFORMATIONAL = "informational"


@dataclass(frozen=True)
class CheckResult:
    """Result of a single health check.

    Attributes:
        name: Human-readable name of the check.
        status: Pass/fail/warn/skip.
        category: Critical or informational.
        message: Short one-line summary.
        detail: Extended explanation (optional).
    """

    name: str
    status: CheckStatus
    category: CheckCategory
    message: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        """True if the check failed."""
        return self.status == CheckStatus.FAIL

    @property
    def critical(self) -> bool:
        """True if this check is critical."""
        return self.category == CheckCategory.CRITICAL

    def to_dict(self) -> dict[str, str]:
        """Serialize to a plain dict for JSON output."""
        return {
            "name": self.name,
            "status": self.status.value,
            "category": self.category.value,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class HealthReport:
    """Aggregated report from all health checks."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def all_critical_pass(self) -> bool:
        """True if no critical check failed."""
        return not any(r.failed and r.critical for r in self.results)

    def to_dict(self) -> dict[str, object]:
        """Serialize the full report to a plain dict."""
        n_pass = sum(1 for r in self.results if r.status == CheckStatus.PASS)
        n_fail = sum(1 for r in self.results if r.status == CheckStatus.FAIL)
        n_warn = sum(1 for r in self.results if r.status == CheckStatus.WARN)
        n_skip = sum(1 for r in self.results if r.status == CheckStatus.SKIP)
        n_critical_fail = sum(1 for r in self.results if r.failed and r.critical)
        return {
            "summary": {
                "total": len(self.results),
                "passed": n_pass,
                "failed": n_fail,
                "warnings": n_warn,
                "skipped": n_skip,
                "critical_failures": n_critical_fail,
                "healthy": self.all_critical_pass,
            },
            "checks": [r.to_dict() for r in self.results],
        }
