"""Redis deployment-mode health check split out of ``health.py`` (issue #1890)."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import cast
from urllib.parse import urlparse

from osimflow._health_types import CheckCategory, CheckResult, CheckStatus

log = logging.getLogger("osimflow.health")


# ---------------------------------------------------------------------------
# Redis deployment-mode check (issue #1562)
# ---------------------------------------------------------------------------
# One of the four planes — DistributedCache, RedisDocumentStore,
# DistributedJobQueue, and the API RateLimitMiddleware — reads from a
# single ``--redis-url`` (see ADR-0004
# ``.agents/results/architecture/0004-redis-ha-scope.md``). The check
# here surfaces the deployment topology the rest of the system assumes
# so operators can confirm the topology matches the scoped decision and
# learn the cache-replay resume path when the instance is unreachable.
#
# Returns SKIP when no URL is provided (single-node campaigns do not
# touch Redis). When a URL is provided, performs a short-timeout PING +
# INFO probe and reports the deployment-mode heuristic alongside the
# Redis version and replication role. The check is INFORMATIONAL —
# unlike the per-executor checks, it is NOT promoted to CRITICAL when
# the user names the configured executor (the Redis plane is shared
# across executors; promoting only the matching executor's check would
# miss every other plane).
# ---------------------------------------------------------------------------


_REDIS_HEALTH_CHECK_NAME = "Redis Deployment Mode"
_REDIS_HEALTH_PROBE_TIMEOUT_S = 5.0


def _redact_redis_url(redis_url: str) -> str:
    """Return *redis_url* with any embedded password replaced by ``***``.

    Surfaces the URL in ``CheckResult.detail`` without leaking
    credentials to logs / CI artifacts. Mirrors the safety stance of
    ``osimflow.distributed_cache.validate_redis_url`` (#1277).
    """
    parsed = urlparse(redis_url)
    if parsed.password is None:
        return redis_url
    netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
    return parsed._replace(netloc=netloc).geturl()


def _classify_redis_mode(redis_url: str) -> str:
    """Heuristically classify the Redis deployment mode from the URL.

    Returns one of ``"single"``, ``"sentinel"``, ``"cluster"``,
    ``"unknown"``. The single-instance case is the only one the
    current client path supports (issue #1562 / ADR-0004); the other
    values produce a WARN so operators notice before the campaign
    mid-run aborts when Sentinel / Cluster triggers a failover the
    redis-py ``Redis`` client cannot route through.
    """
    parsed = urlparse(redis_url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if "sentinel" in scheme or host.startswith("sentinel-"):
        return "sentinel"
    if "cluster" in scheme or host.startswith("redis-cluster"):
        return "cluster"
    if scheme in {"redis", "rediss", "unix"}:
        return "single"
    return "unknown"


def _check_redis_deployment_mode(redis_url: str | None) -> CheckResult:
    """Report the deployment mode of the Redis instance behind ``--redis-url``.

    Issue #1562 acceptance criterion (b): make the single-instance-Redis
    scoped decision visible to operators and document the cache-replay
    resume path as the recovery story.

    Parameters
    ----------
    redis_url
        The ``--redis-url`` value passed to ``osimflow health`` (or the
        campaign config). When ``None``, returns SKIP with a message
        pointing the operator at the four planes the URL would
        coordinate.

    Returns
    -------
    CheckResult
        Status PASS for the supported single-instance case, WARN for
        Sentinel / Cluster URLs (which the current client path cannot
        route through), FAIL when the URL is unreachable. All three
        ``detail`` strings cite ADR-0004 and the user-guide recovery
        subsection.
    """
    name = _REDIS_HEALTH_CHECK_NAME

    if not redis_url:
        return CheckResult(
            name=name,
            status=CheckStatus.SKIP,
            category=CheckCategory.INFORMATIONAL,
            message="Not configured (no --redis-url)",
            detail=(
                "OSimFlow defaults to a single-node SQLite cache and the "
                "filesystem-based job queue. Set --redis-url to coordinate "
                "DistributedCache, RedisDocumentStore, DistributedJobQueue, "
                "and the API rate limiter across nodes (issue #1562 / "
                "ADR-0004)."
            ),
        )

    mode = _classify_redis_mode(redis_url)
    redacted = _redact_redis_url(redis_url)

    try:
        import redis as redis_sync  # noqa: PLC0415 — lazy; the [api] extra
    except ImportError:
        return CheckResult(
            name=name,
            status=CheckStatus.SKIP,
            category=CheckCategory.INFORMATIONAL,
            message="redis client not installed",
            detail=(
                "Install with: pip install 'osimflow[api]' (or the "
                "[redis] extra) to enable the Redis deployment-mode "
                "check."
            ),
        )

    try:
        client = redis_sync.from_url(
            redis_url,
            socket_timeout=_REDIS_HEALTH_PROBE_TIMEOUT_S,
            socket_connect_timeout=_REDIS_HEALTH_PROBE_TIMEOUT_S,
            decode_responses=True,
        )
        t0 = time.monotonic()
        client.ping()
        latency_ms = int((time.monotonic() - t0) * 1000)

        info: dict[str, object] = {}
        try:
            # redis-py's sync ``Redis.info()`` returns ``dict[str, Any]`` at
            # runtime, but the stub makes sync ``Redis`` inherit from
            # ``AsyncCoreCommands`` so mypy resolves ``client.info()`` to
            # ``Awaitable[Any] | Any``. Narrow with ``cast`` so downstream
            # ``isinstance(info, dict)`` checks stay type-correct.
            info = cast(dict[str, object], client.info())
        except Exception as exc:  # noqa: BLE001, PERF203 — INFO is best-effort
            log.debug("redis INFO failed for %s: %s", redacted, exc)

        role_section = info.get("replication") if isinstance(info, dict) else None
        role = role_section.get("role", "unknown") if isinstance(role_section, dict) else "unknown"
        redis_version = info.get("redis_version") if isinstance(info, dict) else None
        if isinstance(redis_version, bytes):
            redis_version = redis_version.decode()
        if redis_version is None:
            redis_version = "unknown"

        with contextlib.suppress(Exception):  # closing is best-effort
            client.close()

    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            category=CheckCategory.INFORMATIONAL,
            message=f"Unreachable: {type(exc).__name__}",
            detail=(
                f"url={redacted} error={exc}. "
                "A Redis outage aborts the in-flight campaign by design "
                "(issue #1014 / #1562). Recovery: re-run the campaign "
                "with the same --outdir; completed steps replay from "
                "cache (see docs/user-guide.md §7.7 and ADR-0004)."
            ),
        )

    if mode == "single":
        return CheckResult(
            name=name,
            status=CheckStatus.PASS,
            category=CheckCategory.INFORMATIONAL,
            message=(
                f"Connected: single-instance Redis {redis_version} ({latency_ms}ms, role={role})"
            ),
            detail=(
                f"url={redacted} mode=single latency={latency_ms}ms "
                f"role={role} version={redis_version}. "
                "ADR-0004 records single-instance Redis as the scoped "
                "decision; recovery on outage is cache-replay resume "
                "(see docs/user-guide.md §7.7)."
            ),
        )

    if mode == "unknown":
        return CheckResult(
            name=name,
            status=CheckStatus.WARN,
            category=CheckCategory.INFORMATIONAL,
            message=(
                f"Connected but URL scheme did not classify cleanly (mode=unknown, {latency_ms}ms)"
            ),
            detail=(
                f"url={redacted} mode=unknown latency={latency_ms}ms "
                f"role={role} version={redis_version}. "
                "ADR-0004 supports single-instance Redis only. "
                "Verify the --redis-url is a `redis://` or `rediss://` "
                "URL pointing at one instance; the current client path "
                "cannot route through Sentinel or Cluster."
            ),
        )

    return CheckResult(
        name=name,
        status=CheckStatus.WARN,
        category=CheckCategory.INFORMATIONAL,
        message=(
            f"Connected but topology={mode} is not currently supported "
            f"by the OSimFlow client path ({latency_ms}ms)"
        ),
        detail=(
            f"url={redacted} mode={mode} latency={latency_ms}ms "
            f"role={role} version={redis_version}. "
            "ADR-0004 documents single-instance Redis as the scoped "
            "decision; Sentinel / Cluster client wiring is not in this "
            "release. If the Redis instance fails over mid-campaign "
            "the document store will fail loud (issue #1014) and the "
            "campaign must be restarted via cache replay "
            "(docs/user-guide.md §7.7)."
        ),
    )


# Built-in executor ↔ health-check dispatch table. The orchestrator iterates
# ``ExecutorRegistry.iter_health_checks()`` (registered below at module
# import; the registry state is anchored in ``executors/base.py`` so it
# survives reloads — issue #1463) — keep these names in sync with the
