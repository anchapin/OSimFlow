"""Resource quota parsing split out of ``config.py`` (issue #1890)."""

import dataclasses
import json
import logging

from osimflow.validation import ValidationError

log = logging.getLogger("osimflow.config")


# ======================================================================
# Resource quota (issue #446)
# ======================================================================


@dataclasses.dataclass
class ResourceQuota:
    """Campaign resource quota limits (issue #446).

    All fields are optional. A ``None`` value means "no limit" for that
    resource. Quota enforcement is checked at campaign start (fail-fast)
    and during per-step fan-out (skip further submissions when the
    quota is exhausted).

    Attributes
    ----------
    max_samples
        Maximum total number of samples across all generations.
        When ``None``, there is no limit.
    max_cost_usd
        Maximum total campaign cost in USD.
        When ``None``, there is no limit.
    max_wall_time_min
        Maximum wall-clock time for the entire campaign in minutes.
        When ``None``, there is no limit.
    max_concurrent_samples
        Maximum number of samples that may run concurrently.
        When ``None``, there is no limit (bounded only by the executor's
        ``max_workers``).
    """

    max_samples: int | None = None
    max_cost_usd: float | None = None
    max_wall_time_min: float | None = None
    max_concurrent_samples: int | None = None


def _parse_resource_quota(raw: object) -> ResourceQuota | None:
    """Parse a resource quota from a JSON-serializable dict, JSON string, or None."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"resource_quota must be a valid JSON dict: {exc}",
                field="resource_quota",
            ) from exc
    if not isinstance(raw, dict):
        raise ValidationError(
            f"resource_quota must be a dict, got {type(raw).__name__}",
            field="resource_quota",
        )
    quota = ResourceQuota(
        max_samples=_int_field(raw, "max_samples"),
        max_cost_usd=_float_field(raw, "max_cost_usd"),
        max_wall_time_min=_float_field(raw, "max_wall_time_min"),
        max_concurrent_samples=_int_field(raw, "max_concurrent_samples"),
    )
    if quota.max_samples is not None and quota.max_samples < 1:
        raise ValidationError(
            "Max samples must be >= 1",
            field="resource_quota.max_samples",
        )
    if quota.max_cost_usd is not None and quota.max_cost_usd < 0:
        raise ValidationError(
            "Max cost usd must be >= 0",
            field="resource_quota.max_cost_usd",
        )
    if quota.max_wall_time_min is not None and quota.max_wall_time_min <= 0:
        raise ValidationError(
            "Max wall time min must be > 0",
            field="resource_quota.max_wall_time_min",
        )
    if quota.max_concurrent_samples is not None and quota.max_concurrent_samples < 1:
        raise ValidationError(
            "Max concurrent samples must be >= 1",
            field="resource_quota.max_concurrent_samples",
        )
    return quota


def _int_field(raw: dict[str, object], key: str) -> int | None:
    val = raw.get(key)
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, (float, str)):
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    raise ValidationError(
        f"{key!r} must be an integer, got {type(val).__name__}",
        field=f"resource_quota.{key}",
    )


def _float_field(raw: dict[str, object], key: str) -> float | None:
    val = raw.get(key)
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            pass
    raise ValidationError(
        f"{key!r} must be a number, got {type(val).__name__}",
        field=f"resource_quota.{key}",
    )
