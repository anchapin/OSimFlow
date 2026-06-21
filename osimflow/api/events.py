"""SSE live events and campaign stop endpoints (issue #143, #395).

Provides:
  - GET /api/v1/events  — Server-Sent Events stream that watches run.json
  - POST /api/v1/campaign/stop — write a ``.stop`` flag file to halt a campaign
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from osimflow.api.auth import get_user_permission

log = logging.getLogger("osimflow.api.events")

events_router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

POLL_INTERVAL_S = 1.0
HEARTBEAT_INTERVAL_S = 15.0
MAX_ITERATIONS_DEFAULT = 0  # 0 = unlimited


# ---------------------------------------------------------------------------
# Cross-platform file locking helpers
# ---------------------------------------------------------------------------


def _read_json_with_lock(path: Path) -> dict[str, Any]:
    """Read a JSON file with a shared (non-exclusive) lock.

    Uses fcntl.flock on Unix and msvcrt.locking on Windows to prevent
    race conditions when multiple SSE clients or the campaign writer
    access run.json concurrently (issue #645).
    """
    data: dict[str, Any] = {}
    if not path.exists():
        return data
    try:
        with path.open("r") as fh:
            if sys.platform == "win32":
                import msvcrt  # noqa: PLC0415

                msvcrt.locking(fh.fileno(), msvcrt.LK_RLCK, 0)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                data = json.loads(fh.read())
            finally:
                if sys.platform == "win32":
                    import msvcrt  # noqa: PLC0415

                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 0)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except (json.JSONDecodeError, OSError):
        pass
    return data


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` atomically via temp file + rename.

    Using a named temp file in the same directory and ``os.replace``
    ensures the rename is atomic on POSIX filesystems (rename is
    guaranteed atomic when dst and src are on the same filesystem),
    eliminating the TOCTOU window between check and write that exists
    when using ``Path.write_text()`` directly.
    """
    dir_path = path.parent
    fd, tmp_path_str = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        os.write(fd, json.dumps(data).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path_str, path)


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
                    events.append(
                        {
                            "event": "sample.error",
                            "data": {
                                "sample_id": sid,
                                "error_summary": error_summary,
                            },
                        }
                    )
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
                    events.append(
                        {
                            "event": "sample.error",
                            "data": {
                                "sample_id": sid,
                                "error_summary": error_summary,
                            },
                        }
                    )

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

        # --- read current run.json with shared lock to prevent race condition (issue #645) ---
        current_snapshot = _read_json_with_lock(run_json_path)

        # --- diff and emit ---
        if current_snapshot != last_snapshot:
            new_events = diff_events(last_snapshot, current_snapshot)
            for evt in new_events:
                yield {
                    "event": evt["event"],
                    "data": json.dumps(evt["data"], default=str),
                }
            last_snapshot = current_snapshot

        # --- heartbeat: fires every heartbeat_interval seconds based on elapsed
        # time, NOT on snapshot equality. This ensures heartbeats continue
        # even when the campaign is actively updating (issue #662).
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
    Returns 403 if the user lacks write permission (issue #395).
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="read-only mode: write permission required",
        )
    outdir: Path | None = request.app.state.outdir
    if outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")

    stop_file = outdir / ".stop"
    _atomic_write_json(stop_file, {"requested_at": time.time()})
    log.info("stop flag written to %s", stop_file)
    return {"status": "stopping"}


# ---------------------------------------------------------------------------
# POST /api/v1/campaign/pause
# ---------------------------------------------------------------------------


@events_router.post("/api/v1/campaign/pause")  # type: ignore[untyped-decorator]
async def campaign_pause(request: Request) -> dict[str, str]:
    """Write a ``.pause`` flag file to request campaign pause.

    The campaign orchestrator checks for ``${outdir}/.pause`` during fan-out,
    waits for in-flight work to complete, and writes a "paused" status
    to run.json. Unlike stop, pause allows subsequent resume.
    Returns 403 if the user lacks write permission (issue #395).
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="read-only mode: write permission required",
        )
    outdir: Path | None = request.app.state.outdir
    if outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")

    run_json_path = outdir / "run.json"
    run_data = _read_json_with_lock(run_json_path)
    if run_data.get("finished_at") is not None:
        raise HTTPException(
            status_code=409,
            detail="campaign has already completed",
        )
    if run_data.get("status") == "paused":
        return {"status": "already_paused"}

    pause_file = outdir / ".pause"
    _atomic_write_json(pause_file, {"requested_at": time.time()})
    log.info("pause flag written to %s", pause_file)
    return {"status": "pausing"}


# ---------------------------------------------------------------------------
# DELETE /api/v1/campaign/pause
# ---------------------------------------------------------------------------


@events_router.delete("/api/v1/campaign/pause")  # type: ignore[untyped-decorator]
async def campaign_resume(request: Request) -> dict[str, str]:
    """Remove the ``.pause`` flag file to request campaign resume.

    The campaign orchestrator detects the cleared pause condition and
    continues processing pending samples. Returns 403 if the user lacks
    write permission (issue #395).
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="read-only mode: write permission required",
        )
    outdir: Path | None = request.app.state.outdir
    if outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")

    run_json_path = outdir / "run.json"
    run_data = _read_json_with_lock(run_json_path)
    if run_data and run_data.get("status") != "paused":
        raise HTTPException(
            status_code=409,
            detail=f"campaign is not paused (status={run_data.get('status')})",
        )

    pause_file = outdir / ".pause"
    if pause_file.exists():
        pause_file.unlink()
        log.info("pause flag removed from %s", pause_file)
    return {"status": "resuming"}
