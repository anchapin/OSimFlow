"""Local handoff record for Coordinator-backed (``--detach``) campaigns.

When a user runs ``osimflow run --detach --coordinator-url ...`` the CLI hands
the campaign to a remote Coordinator service and exits immediately. So that
``osimflow status`` / ``osimflow download`` can later reconnect to that remote
campaign — even from a fresh shell or a rebooted machine — the CLI writes a
small JSON "handoff record" under the campaign outdir.

The record is intentionally tiny and self-describing: it carries the
``campaign_id`` returned by the Coordinator, the Coordinator base URL, the
status-poll URL, and a timestamp. It is the single source of truth for "this
outdir is associated with remote campaign X".

This module is the only place that reads/writes the record, so the on-disk
shape is centralised here (issue #630, Epic #624).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("osimflow.handoff_record")

#: Filename of the handoff record, written inside the campaign outdir.
HANDOFF_RECORD_NAME = ".coordinator_handoff.json"

#: Version tag for forward compatibility. Bump if the schema changes.
HANDOFF_RECORD_VERSION = 1

#: The HTTP header name used to make ``POST /coordinator/handoff`` idempotent
#: (issue #630). Defined here — the single dependency-light place — so the
#: server (``osimflow.api.coordinator``) and the CLI (``osimflow.__main__``)
#: agree on the contract without the CLI having to import the FastAPI stack.
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


class NoHandoffRecordError(RuntimeError):
    """Raised when an outdir has no Coordinator handoff record.

    Carries the actionable, user-facing message required by issue #630:
    "no Coordinator campaign associated with this outdir; did you run with
    ``--detach``?".
    """


@dataclass(frozen=True)
class HandoffRecord:
    """A persisted Coordinator handoff record.

    Attributes
    ----------
    campaign_id
        The campaign identifier the Coordinator assigned at handoff.
    coordinator_url
        Base URL of the Coordinator service (no trailing slash required).
        Used to rebuild status/results URLs from a fresh shell.
    submitted_at
        Unix epoch seconds when the handoff was accepted by the Coordinator.
    status_url
        Absolute URL to poll for live campaign status
        (``GET /api/v1/coordinator/campaigns/{campaign_id}``).
    idempotency_key
        The ``Idempotency-Key`` the CLI sent with the handoff (if any). Kept
        for debugging duplicate-handoff investigations; not required for
        reconnect.
    """

    campaign_id: str
    coordinator_url: str
    submitted_at: float
    status_url: str
    idempotency_key: str | None = None


def _record_path(outdir: Path) -> Path:
    """Return the absolute path to the handoff record inside ``outdir``."""
    return outdir / HANDOFF_RECORD_NAME


def write_handoff_record(outdir: Path, record: HandoffRecord) -> Path:
    """Persist ``record`` to ``outdir/.coordinator_handoff.json``.

    Creates ``outdir`` if it does not exist. Writes atomically-ish (write then
    flush) — the file is small and written once per campaign, so the simple
    "write to final path" approach is sufficient and keeps the helper tiny.

    Returns the path written.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    path = _record_path(outdir)
    payload = {
        "version": HANDOFF_RECORD_VERSION,
        **asdict(record),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    log.info(
        "Wrote Coordinator handoff record for campaign %s to %s",
        record.campaign_id,
        path,
    )
    return path


def read_handoff_record(outdir: Path) -> HandoffRecord:
    """Read the handoff record for ``outdir``.

    Raises :class:`NoHandoffRecordError` with the actionable, user-facing
    message if the record is absent or unreadable. Callers should surface that
    message directly to the user.
    """
    path = _record_path(outdir)
    if not path.exists():
        raise NoHandoffRecordError(
            "no Coordinator campaign associated with this outdir; did you run with `--detach`?"
        )
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        # A corrupt record is treated as "no usable record" so the caller can
        # present one consistent recovery path (re-hand-off).
        log.warning("Handoff record at %s is unreadable: %s", path, exc)
        raise NoHandoffRecordError(
            f"the Coordinator handoff record at {path} is unreadable "
            f"({exc}); remove it and re-run with `--detach` to re-hand-off."
        ) from exc

    required = ("campaign_id", "coordinator_url", "submitted_at", "status_url")
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise NoHandoffRecordError(
            f"the Coordinator handoff record at {path} is missing required "
            f"field(s): {', '.join(missing)}. Remove it and re-run with "
            f"`--detach` to re-hand-off."
        )

    return HandoffRecord(
        campaign_id=str(data["campaign_id"]),
        coordinator_url=str(data["coordinator_url"]),
        submitted_at=float(data["submitted_at"]),
        status_url=str(data["status_url"]),
        idempotency_key=data.get("idempotency_key"),
    )


def handoff_record_exists(outdir: Path) -> bool:
    """Return True if a handoff record exists under ``outdir``."""
    return _record_path(outdir).exists()


__all__ = [
    "HANDOFF_RECORD_NAME",
    "HANDOFF_RECORD_VERSION",
    "IDEMPOTENCY_KEY_HEADER",
    "HandoffRecord",
    "NoHandoffRecordError",
    "handoff_record_exists",
    "read_handoff_record",
    "write_handoff_record",
]
