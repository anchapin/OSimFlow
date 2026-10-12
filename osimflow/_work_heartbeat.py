"""Moved from ``osimflow.work`` (issue #1890); re-exported there."""

import json
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

log = logging.getLogger("osimflow.work")

# ---------------------------------------------------------------------------
# Container / simulation health monitoring (issue #415)
# ---------------------------------------------------------------------------

HEARTBEAT_INTERVAL_S = 30  # seconds between heartbeat writes
HEARTBEAT_FILENAME = ".heartbeat.json"
HEALTH_CHECK_INTERVAL_S = 60  # default health check tolerance (stale if no beat for this long)


class SimulationHealthStatus(StrEnum):
    HEALTHY = "healthy"
    STALE = "stale"  # no heartbeat within the expected interval
    UNKNOWN = "unknown"


@dataclass
class SimulationHealth:
    status: SimulationHealthStatus
    last_heartbeat: float | None = None
    pid: int | None = None
    message: str | None = None


def _write_heartbeat(sim_out: Path, pid: int, sample_id: str, version: str) -> None:
    """Write a heartbeat file for the running simulation.

    The heartbeat is consumed by ``check_container_health`` to detect
    simulations that have gone silent (container freeze, process deadlock).
    """
    heartbeat_path = sim_out / HEARTBEAT_FILENAME
    payload = {
        "pid": pid,
        "sample_id": sample_id,
        "version": version,
        "timestamp": time.time(),
    }
    try:
        sim_out.mkdir(parents=True, exist_ok=True)
        heartbeat_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.warning("failed to write heartbeat for %s: %s", sample_id, exc)


def check_container_health(
    sim_out: Path,
    health_check_interval: float = HEALTH_CHECK_INTERVAL_S,
) -> SimulationHealth:
    """Check whether the simulation container is still responsive.

    Returns a ``SimulationHealth`` describing the current state:
    - HEALTHY: heartbeat is fresh (written within health_check_interval).
    - STALE: heartbeat exists but is older than health_check_interval.
    - UNKNOWN: no heartbeat file exists (simulation may not have started yet
      or the file was deleted).

    The caller can use the STALE status to decide whether to cancel and
    retry a frozen simulation.  This function does **not** terminate
    anything — it only reports health status.
    """
    heartbeat_path = sim_out / HEARTBEAT_FILENAME
    if not heartbeat_path.is_file():
        return SimulationHealth(
            status=SimulationHealthStatus.UNKNOWN,
            message="no heartbeat file found",
        )
    try:
        payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        last_ts = payload.get("timestamp")
        if last_ts is None:
            return SimulationHealth(
                status=SimulationHealthStatus.UNKNOWN, message="heartbeat has no timestamp"
            )
        age = time.time() - last_ts
        if age > health_check_interval:
            return SimulationHealth(
                status=SimulationHealthStatus.STALE,
                last_heartbeat=last_ts,
                pid=payload.get("pid"),
                message=f"heartbeat is {age:.0f}s old (threshold {health_check_interval}s)",
            )
        return SimulationHealth(
            status=SimulationHealthStatus.HEALTHY,
            last_heartbeat=last_ts,
            pid=payload.get("pid"),
        )
    except (OSError, json.JSONDecodeError) as exc:
        return SimulationHealth(
            status=SimulationHealthStatus.UNKNOWN,
            message=f"failed to read heartbeat: {exc}",
        )
