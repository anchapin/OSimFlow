"""SSE live events and campaign stop endpoints (issue #143).

Provides:
  - GET /api/v1/events  — Server-Sent Events stream that watches run.json
  - POST /api/v1/campaign/stop — write a ``.stop`` flag file to halt a campaign
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

log = logging.getLogger("osimflow.api.events")

events_router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

POLL_INTERVAL_S = 1.0
HEARTBEAT_INTERVAL_S = 15.0
MAX_ITERATIONS_DEFAULT = 0  # 0 = unlimited


def diff_events(
    old: dict[str, Any],
    new: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compare two run.json snapshots and emit structured events.

    Event types:
      - ``sample.started``  — new sample appeared in per_sample
      - ``sample.completed`` — sample status changed to ok/failed/cached
      - ``sample.error``     — sample failed with error summary (issue #385)
      - ``step.completed``   — new step in steps list
      - ``campaign.completed`` — finished_at became non-null
    """
    events: list[dict[str, Any]] = []

    # --- campaign completion ---
    old_finished = old.get("finished_at")
    new_finished = new.get("finished_at")
    if old_finished is None and new_finished is not None:
        events.append(
            {
                "event": "campaign.completed",
                "data": {
                    "campaign_id": new.get("campaign_id"),
                    "finished_at": new_finished,
                    "elapsed_s": new.get("elapsed_s"),
                },
            }
        )

    # --- step completion ---
    old_steps = {s.get("step") for s in old.get("steps", [])}
    new_steps: list[dict[str, Any]] = new.get("steps", [])
    for step in new_steps:
        if step.get("step") not in old_steps:
            events.append({"event": "step.completed", "data": step})

    # --- sample changes ---
    old_samples: dict[str, dict[str, Any]] = {
        s.get("sample_id", ""): s for s in old.get("per_sample", [])
    }
    new_samples: list[dict[str, Any]] = new.get("per_sample", [])
    for sample in new_samples:
        sid = sample.get("sample_id", "")
        old_sample = old_samples.get(sid)
        if old_sample is None:
            # New sample appeared — it may already be completed.
            status = sample.get("status")
            if status in ("ok", "failed", "cached"):
                events.append({"event": "sample.completed", "data": sample})
                # Emit error event for failed samples (issue #385)
                if status == "failed":
                    error_summary = sample.get("error_summary")
                    events.append({
                        "event": "sample.error",
                        "data": {
                            "sample_id": sid,
                            "error_summary": error_summary,
                        },
                    })
            else:
                events.append({"event": "sample.started", "data": sample})
        else:
            # Existing sample — check for status change.
            old_status = old_sample.get("status")
            new_status = sample.get("status")
            if old_status != new_status and new_status in (
                "ok",
                "failed",
                "cached",
            ):
                events.append({"event": "sample.completed", "data": sample})
                # Emit error event when sample transitions to failed (issue #385)
                if new_status == "failed":
                    error_summary = sample.get("error_summary")
                    events.append({
                        "event": "sample.error",
                        "data": {
                            "sample_id": sid,
                            "error_summary": error_summary,
                        },
                    })

    return events


# ---------------------------------------------------------------------------
# GET /api/v1/events  — SSE stream
# ---------------------------------------------------------------------------


async def _event_generator(
    request: Request,
    *,
    poll_interval: float = POLL_INTERVAL_S,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
    max_iterations: int = MAX_ITERATIONS_DEFAULT,
) -> Any:
    """Async generator yielding SSE events from run.json polling.

    Parameters
    ----------
    poll_interval
        Seconds between run.json polls.
    heartbeat_interval
        Seconds between heartbeat pings.
    max_iterations
        Maximum polling iterations (0 = unlimited).  Non-zero values
        are used for testing to ensure the generator terminates.
    """
    outdir: Path | None = request.app.state.outdir

    if outdir is None:
        yield {"event": "error", "data": json.dumps({"detail": "No output directory configured"})}
        return

    run_json_path = outdir / "run.json"
    last_snapshot: dict[str, Any] = {}
    last_heartbeat = time.monotonic()
    iteration = 0

    while True:
        if await request.is_disconnected():
            break

        # --- read current run.json ---
        current_snapshot: dict[str, Any] = {}
        if run_json_path.exists():
            try:
                current_snapshot = json.loads(run_json_path.read_text())
            except (json.JSONDecodeError, OSError):
                current_snapshot = {}

        # --- diff and emit ---
        if current_snapshot != last_snapshot:
            new_events = diff_events(last_snapshot, current_snapshot)
            for evt in new_events:
                yield {
                    "event": evt["event"],
                    "data": json.dumps(evt["data"], default=str),
                }
            last_snapshot = current_snapshot

        # --- heartbeat ---
        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_interval:
            yield {"event": "ping", "data": ""}
            last_heartbeat = now

        # --- iteration cap (testing) ---
        iteration += 1
        if max_iterations and iteration >= max_iterations:
            break

        await asyncio.sleep(poll_interval)


@events_router.get("/api/v1/events")  # type: ignore[untyped-decorator]
async def sse_events(request: Request) -> EventSourceResponse:
    """SSE endpoint streaming live campaign events.

    Polls ``run.json`` at ~1 Hz and emits structured events:
    ``sample.started``, ``sample.completed``, ``step.completed``,
    ``campaign.completed``.  Sends ``: ping`` heartbeat comments when
    the campaign has not started yet (no run.json).

    Available in both read-only and read-write modes (issue #275).
    """
    return EventSourceResponse(_event_generator(request))


# ---------------------------------------------------------------------------
# POST /api/v1/campaign/stop
# ---------------------------------------------------------------------------


@events_router.post("/api/v1/campaign/stop")  # type: ignore[untyped-decorator]
async def campaign_stop(request: Request) -> dict[str, str]:
    """Write a ``.stop`` flag file to request campaign cancellation.

    The campaign orchestrator checks for ``${outdir}/.stop`` between steps.
    Returns 403 in read-only mode.
    """
    if getattr(request.app.state, "read_only", True):
        raise HTTPException(
            status_code=403,
            detail="Stop not available in read-only mode",
        )
    outdir: Path | None = request.app.state.outdir
    if outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")

    stop_file = outdir / ".stop"
    stop_file.write_text(json.dumps({"requested_at": time.time()}))
    log.info("stop flag written to %s", stop_file)
    return {"status": "stopping"}
