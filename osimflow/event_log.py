"""Append-only campaign event log for auditing and state replay (issue #396).

An alternative to the snapshot-based ``run.json``, this log records every
state transition as a JSON Lines entry so operators can:

* Audit exactly what happened during a campaign.
* Replay state after a crash (event sourcing).
* Join with external observability backends via the embedded ``trace_id``.

The log file is at ``{outdir}/events.jsonl`` and is opened in append mode
for the entire campaign duration.  Each line is a JSON object::

    {
        "ts": "<ISO8601>",
        "type": "<EVENT_TYPE>",
        "data": { ... },
        "trace_id": "<opaque hex>"
    }

Event types
-----------
CAMPAIGN_STARTED    — campaign.run() entered, before any step
STEP_STARTED       — a step began (APPLY_PARAMETERS, RUN_OPENSTUDIO_SIM, …)
STEP_COMPLETED     — a step finished (includes cache label + elapsed_s)
SAMPLE_STARTED     — a single sample entered a fan-out step
SAMPLE_COMPLETED   — a single sample completed a fan-out step successfully
SAMPLE_FAILED      — a single sample failed a fan-out step (includes error)
CAMPAIGN_COMPLETED  — campaign.run() exiting with status="success"
CAMPAIGN_FAILED     — campaign.run() exiting with status="failed" or "cancelled"
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.event_log")


class EventType(StrEnum):
    CAMPAIGN_STARTED = "CAMPAIGN_STARTED"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    SAMPLE_STARTED = "SAMPLE_STARTED"
    SAMPLE_COMPLETED = "SAMPLE_COMPLETED"
    SAMPLE_FAILED = "SAMPLE_FAILED"
    CAMPAIGN_COMPLETED = "CAMPAIGN_COMPLETED"
    CAMPAIGN_FAILED = "CAMPAIGN_FAILED"


class CampaignEventLog:
    """Append-only event log for a single campaign.

    Thread-safe: all writes go through a single lock so multiple
    concurrent fan-out threads can emit events without corruption.
    """

    def __init__(self, outdir: Path) -> None:
        self._path = outdir / "events.jsonl"
        self._lock = threading.Lock()
        self._written = False

    # ------------------------------------------------------------------
    # Emit
    # ------------------------------------------------------------------
    def _emit(
        self,
        event_type: EventType,
        data: dict[str, Any],
        trace_id: str | None = None,
    ) -> None:
        """Append one JSON Lines entry to the log file.

        Uses a per-campaign lock for thread-safety.  The file is
        created on first write and kept open (append mode) for the
        remainder of the campaign so every event is durable.
        """
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "type": event_type.value,
            "data": data,
            "trace_id": trace_id,
        }
        line = json.dumps(entry, default=str)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._written = True

    def campaign_started(
        self,
        campaign_id: str,
        executor: str,
        n_samples: int,
        algorithm: str,
        openstudio_version: str,
    ) -> None:
        self._emit(
            EventType.CAMPAIGN_STARTED,
            {
                "campaign_id": campaign_id,
                "executor": executor,
                "n_samples": n_samples,
                "algorithm": algorithm,
                "openstudio_version": openstudio_version,
            },
        )

    def step_started(
        self,
        campaign_id: str,
        step: str,
        generation: int = 0,
        trace_id: str | None = None,
    ) -> None:
        self._emit(
            EventType.STEP_STARTED,
            {"campaign_id": campaign_id, "step": step, "generation": generation},
            trace_id=trace_id,
        )

    def step_completed(
        self,
        campaign_id: str,
        step: str,
        elapsed_s: float,
        cache_hit: bool,
        generation: int = 0,
        trace_id: str | None = None,
    ) -> None:
        self._emit(
            EventType.STEP_COMPLETED,
            {
                "campaign_id": campaign_id,
                "step": step,
                "elapsed_s": round(elapsed_s, 3),
                "cache_hit": cache_hit,
                "generation": generation,
            },
            trace_id=trace_id,
        )

    def sample_started(
        self,
        campaign_id: str,
        step: str,
        sample_id: str,
        generation: int = 0,
        trace_id: str | None = None,
    ) -> None:
        self._emit(
            EventType.SAMPLE_STARTED,
            {"campaign_id": campaign_id, "sample_id": sample_id, "step": step, "generation": generation},
            trace_id=trace_id,
        )

    def sample_completed(
        self,
        campaign_id: str,
        step: str,
        sample_id: str,
        generation: int = 0,
        trace_id: str | None = None,
    ) -> None:
        self._emit(
            EventType.SAMPLE_COMPLETED,
            {
                "campaign_id": campaign_id,
                "sample_id": sample_id,
                "step": step,
                "generation": generation,
            },
            trace_id=trace_id,
        )

    def sample_failed(
        self,
        campaign_id: str,
        step: str,
        sample_id: str,
        reason: str,
        generation: int = 0,
        trace_id: str | None = None,
    ) -> None:
        self._emit(
            EventType.SAMPLE_FAILED,
            {
                "campaign_id": campaign_id,
                "sample_id": sample_id,
                "step": step,
                "reason": reason,
                "generation": generation,
            },
            trace_id=trace_id,
        )

    def campaign_completed(
        self,
        campaign_id: str,
        elapsed_s: float,
        n_succeeded: int,
        n_failed: int,
    ) -> None:
        self._emit(
            EventType.CAMPAIGN_COMPLETED,
            {
                "campaign_id": campaign_id,
                "elapsed_s": round(elapsed_s, 3),
                "n_succeeded": n_succeeded,
                "n_failed": n_failed,
            },
        )

    def campaign_failed(
        self,
        campaign_id: str,
        reason: str,
        elapsed_s: float,
    ) -> None:
        self._emit(
            EventType.CAMPAIGN_FAILED,
            {
                "campaign_id": campaign_id,
                "reason": reason,
                "elapsed_s": round(elapsed_s, 3),
            },
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def flush(self) -> None:
        """Ensure all buffered writes reach disk.

        A no-op in this implementation (each write is synchronous to
        the OS append buffer).  Present for API symmetry with other
        OSimFlow persistency objects.
        """
        with self._lock:
            if self._path.is_file():
                self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path


# ---------------------------------------------------------------------------
# Reader utility
# ---------------------------------------------------------------------------


def read_event_log(path: Path) -> list[dict[str, Any]]:
    """Parse an ``events.jsonl`` file into a list of event dicts.

    Parameters
    ----------
    path
        Path to an ``events.jsonl`` file produced by ``CampaignEventLog``.

    Returns
    -------
    List of event dicts in the order they were written.  Each dict
    contains the keys ``ts`` (ISO8601 string), ``type`` (EventType value),
    ``data`` (event-specific payload), and ``trace_id`` (opaque hex or
    ``None``).
    """
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError:
                log.warning("skipped unparseable line in %s", path)
                continue
    return events

