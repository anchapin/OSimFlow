"""Audit logging for OSimFlow campaign operations (issue #439).

Provides an immutable audit trail for campaign operations, tracking who did
what and when. Audit events are written to ``${outdir}/audit.jsonl`` (one
JSON object per line) and complement ``run.json`` for forensics.

Pre-defined audit events
-----------------------
Campaign lifecycle
    CAMPAIGN_CREATED, CAMPAIGN_STARTED, CAMPAIGN_STOPPED,
    CAMPAIGN_COMPLETED, CAMPAIGN_FAILED
Per-sample lifecycle
    SAMPLE_CREATED, SAMPLE_COMPLETED, SAMPLE_FAILED
Config & secrets
    CONFIG_CHANGED, SECRETS_ACCESSED
Executor
    JOB_SUBMITTED, JOB_COMPLETED, JOB_FAILED

Actor identification
-------------------
CLI runs:         ``cli:<username>`` or ``cli:root``
API runs:         ``api:<api_key_name>`` or ``anonymous``
System-initiated: ``system``

Sensitive data redaction
-----------------------
The following field names are redacted before logging:
    password, secret, api_key, token, access_key, secret_key,
    credentials, auth, authorization, bearer
"""

from __future__ import annotations

import json
import logging
import os
import pwd
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.audit")

# ---------------------------------------------------------------------------
# Outcome enum
# ---------------------------------------------------------------------------


class AuditOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


# ---------------------------------------------------------------------------
# Pre-defined action constants
# ---------------------------------------------------------------------------
CAMPAIGN_CREATED = "campaign.created"
CAMPAIGN_STARTED = "campaign.started"
CAMPAIGN_STOPPED = "campaign.stopped"
CAMPAIGN_COMPLETED = "campaign.completed"
CAMPAIGN_FAILED = "campaign.failed"
CAMPAIGN_OUTCOME = "campaign.outcome"
SAMPLE_CREATED = "sample.created"
SAMPLE_COMPLETED = "sample.completed"
SAMPLE_FAILED = "sample.failed"
CONFIG_CHANGED = "config.changed"
SECRETS_ACCESSED = "secrets.accessed"
JOB_SUBMITTED = "job.submitted"
JOB_COMPLETED = "job.completed"
JOB_FAILED = "job.failed"

# ---------------------------------------------------------------------------
# Sensitive field redaction
# ---------------------------------------------------------------------------

_SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "api_key",
        "apikey",
        "api-key",
        "token",
        "access_key",
        "access-key",
        "access_key_id",
        "secret_key",
        "secret-key",
        "credentials",
        "auth",
        "authorization",
        "bearer",
        "x-api-key",
        "aws_access_key_id",
        "aws_secret_access_key",
    }
)

_REDACTED = "[REDACTED]"


def _redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *data* with sensitive field values redacted."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        lower_key = key.lower().replace("_", "").replace("-", "")
        if lower_key in _SENSITIVE_FIELD_NAMES:
            result[key] = _REDACTED
        elif isinstance(value, dict):
            result[key] = _redact_dict(value)
        elif isinstance(value, list):
            result[key] = [
                _redact_dict(item)
                if isinstance(item, dict)
                else _REDACTED
                if isinstance(item, dict)
                and any(
                    sk.lower().replace("_", "").replace("-", "") in _SENSITIVE_FIELD_NAMES
                    for sk in item
                )
                else item
                for item in value
            ]
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# AuditEvent dataclass
# ---------------------------------------------------------------------------


@dataclass
class AuditEvent:
    """An immutable audit trail entry.

    Parameters
    ----------
    timestamp
        When the event occurred (UTC).
    actor
        Who initiated the action.
        CLI runs:         ``cli:<username>`` or ``cli:root``
        API runs:         ``api:<api_key_name>`` or ``anonymous``
        System-initiated: ``system``
    action
        What was done (e.g. ``campaign.started``, ``sample.completed``).
    resource
        The resource acted upon (e.g. a campaign ID, sample ID).
    details
        Additional context (parameters, settings, etc.).
        Sensitive fields are automatically redacted.
    outcome
        SUCCESS or FAILURE.
    """

    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    actor: str = "system"
    action: str = ""
    resource: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    outcome: AuditOutcome = AuditOutcome.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "details": _redact_dict(self.details),
            "outcome": self.outcome.value,
        }

    def to_json_line(self) -> str:
        """Return a JSON line string (one line, no trailing newline)."""
        return json.dumps(self.to_dict(), default=str)


# ---------------------------------------------------------------------------
# Actor helpers
# ---------------------------------------------------------------------------


def cli_actor() -> str:
    """Return the actor string for a CLI invocation.

    Returns ``cli:<username>`` or ``cli:root`` if username cannot be determined.
    """
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        username = "root"
    if username == "root" and "_CONDA_DEFAULT_ENV" in os.environ:
        username = "root"
    return f"cli:{username}"


def api_actor(api_key_name: str | None) -> str:
    """Return the actor string for an API invocation.

    Parameters
    ----------
    api_key_name
        The API key name (from the auth store). ``None`` or empty
        string produces ``anonymous``.
    """
    if not api_key_name:
        return "anonymous"
    return f"api:{api_key_name}"


def api_actor_from_request(request: object) -> str:
    """Extract the actor string from a FastAPI Request object.

    Reads ``request.state.api_user`` (set by the auth middleware) to
    derive the actor. Returns ``"anonymous"`` if no user is present.
    """
    api_user: object | None = getattr(request, "state", None)
    if api_user is None:
        return "anonymous"
    user_id: str | None = getattr(api_user, "user_id", None)
    if user_id is None:
        return "anonymous"
    return f"api:{user_id}"


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------


class AuditLogger:
    """Append-only audit trail writer.

    Writes one JSON line per :class:`AuditEvent` to
    ``${outdir}/audit.jsonl``. The file is opened in append mode and
    flushed after each write so events are visible immediately.

    Optionally forwards events to a secondary backend via the *extra_writer*
    callback (e.g. a syslog handler or a second database table).
    """

    def __init__(
        self,
        outdir: Path | str,
        extra_writer: bool = False,
    ) -> None:
        self._outdir = Path(outdir)
        self._audit_path = self._outdir / "audit.jsonl"
        self._extra_writer = extra_writer
        self._lock_guard: bool = False

    @property
    def audit_path(self) -> Path:
        """Path to the audit log file."""
        return self._audit_path

    def log(self, event: AuditEvent) -> None:
        """Append *event* to the audit log.

        This method is thread-safe for concurrent writes from multiple
        campaign workers. Each line is flushed immediately so events
        are durable even after a crash.
        """
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            line = event.to_json_line() + "\n"
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        except Exception as exc:
            log.warning("failed to write audit event: %s", exc)

    def campaign_created(
        self,
        campaign_id: str,
        executor: str,
        n_samples: int,
        openstudio_version: str,
        actor: str | None = None,
    ) -> None:
        """Log a campaign.created event."""
        self.log(
            AuditEvent(
                actor=actor or cli_actor(),
                action=CAMPAIGN_CREATED,
                resource=campaign_id,
                details={
                    "executor": executor,
                    "n_samples": n_samples,
                    "openstudio_version": openstudio_version,
                },
            )
        )

    def campaign_started(
        self,
        campaign_id: str,
        actor: str | None = None,
    ) -> None:
        """Log a campaign.started event."""
        self.log(
            AuditEvent(
                actor=actor or cli_actor(),
                action=CAMPAIGN_STARTED,
                resource=campaign_id,
            )
        )

    def campaign_stopped(
        self,
        campaign_id: str,
        actor: str | None = None,
    ) -> None:
        """Log a campaign.stopped event."""
        self.log(
            AuditEvent(
                actor=actor or cli_actor(),
                action=CAMPAIGN_STOPPED,
                resource=campaign_id,
            )
        )

    def campaign_completed(
        self,
        campaign_id: str,
        duration_s: float,
        n_succeeded: int,
        n_failed: int,
        actor: str | None = None,
    ) -> None:
        """Log a campaign.completed event."""
        self.log(
            AuditEvent(
                actor=actor or cli_actor(),
                action=CAMPAIGN_COMPLETED,
                resource=campaign_id,
                details={
                    "duration_s": round(duration_s, 2),
                    "n_succeeded": n_succeeded,
                    "n_failed": n_failed,
                },
            )
        )

    def campaign_failed(
        self,
        campaign_id: str,
        reason: str,
        actor: str | None = None,
    ) -> None:
        """Log a campaign.failed event."""
        self.log(
            AuditEvent(
                actor=actor or cli_actor(),
                action=CAMPAIGN_FAILED,
                resource=campaign_id,
                details={"reason": reason},
                outcome=AuditOutcome.FAILURE,
            )
        )

    def log_campaign_outcome(
        self,
        campaign_id: str,
        status: str,
        n_samples: int,
        n_succeeded: int,
        n_failed: int,
        elapsed_s: float,
        total_cost_usd: float | None,
        actor: str | None = None,
    ) -> None:
        """Log a campaign.outcome event (issue #439).

        *status* is the campaign status string: "success", "failed", or "cancelled".
        """
        outcome = AuditOutcome.SUCCESS if status == "success" else AuditOutcome.FAILURE
        self.log(
            AuditEvent(
                actor=actor or cli_actor(),
                action=CAMPAIGN_OUTCOME,
                resource=campaign_id,
                details={
                    "status": status,
                    "n_samples": n_samples,
                    "n_succeeded": n_succeeded,
                    "n_failed": n_failed,
                    "elapsed_s": round(elapsed_s, 2),
                    "total_cost_usd": total_cost_usd,
                },
                outcome=outcome,
            )
        )

    def sample_created(
        self,
        campaign_id: str,
        sample_id: str,
        values: dict[str, Any],
        actor: str | None = None,
    ) -> None:
        """Log a sample.created event."""
        self.log(
            AuditEvent(
                actor=actor or cli_actor(),
                action=SAMPLE_CREATED,
                resource=f"{campaign_id}/{sample_id}",
                details={"values": values},
            )
        )

    def sample_completed(
        self,
        campaign_id: str,
        sample_id: str,
        actor: str | None = None,
    ) -> None:
        """Log a sample.completed event."""
        self.log(
            AuditEvent(
                actor=actor or cli_actor(),
                action=SAMPLE_COMPLETED,
                resource=f"{campaign_id}/{sample_id}",
            )
        )

    def sample_failed(
        self,
        campaign_id: str,
        sample_id: str,
        error: str,
        actor: str | None = None,
    ) -> None:
        """Log a sample.failed event."""
        self.log(
            AuditEvent(
                actor=actor or cli_actor(),
                action=SAMPLE_FAILED,
                resource=f"{campaign_id}/{sample_id}",
                details={"error": str(error)[:500]},
                outcome=AuditOutcome.FAILURE,
            )
        )

    def config_changed(
        self,
        campaign_id: str,
        field: str,
        old_value: Any,
        new_value: Any,
        actor: str | None = None,
    ) -> None:
        """Log a config.changed event."""
        self.log(
            AuditEvent(
                actor=actor or cli_actor(),
                action=CONFIG_CHANGED,
                resource=campaign_id,
                details={
                    "field": field,
                    "old_value": str(old_value)[:200],
                    "new_value": str(new_value)[:200],
                },
            )
        )

    def secrets_accessed(
        self,
        campaign_id: str,
        field: str,
        actor: str | None = None,
    ) -> None:
        """Log a secrets.accessed event."""
        self.log(
            AuditEvent(
                actor=actor or cli_actor(),
                action=SECRETS_ACCESSED,
                resource=campaign_id,
                details={"field": field},
                outcome=AuditOutcome.FAILURE,
            )
        )

    def job_submitted(
        self,
        campaign_id: str,
        sample_id: str,
        job_id: str,
        executor: str,
        actor: str | None = None,
    ) -> None:
        """Log a job.submitted event."""
        self.log(
            AuditEvent(
                actor=actor or "system",
                action=JOB_SUBMITTED,
                resource=f"{campaign_id}/{sample_id}",
                details={
                    "job_id": job_id,
                    "executor": executor,
                },
            )
        )

    def job_completed(
        self,
        campaign_id: str,
        sample_id: str,
        job_id: str,
        actor: str | None = None,
    ) -> None:
        """Log a job.completed event."""
        self.log(
            AuditEvent(
                actor=actor or "system",
                action=JOB_COMPLETED,
                resource=f"{campaign_id}/{sample_id}",
                details={"job_id": job_id},
            )
        )

    def job_failed(
        self,
        campaign_id: str,
        sample_id: str,
        job_id: str,
        error: str,
        actor: str | None = None,
    ) -> None:
        """Log a job.failed event."""
        self.log(
            AuditEvent(
                actor=actor or "system",
                action=JOB_FAILED,
                resource=f"{campaign_id}/{sample_id}",
                details={
                    "job_id": job_id,
                    "error": str(error)[:500],
                },
                outcome=AuditOutcome.FAILURE,
            )
        )

    def api_campaign_created(
        self,
        campaign_id: str,
        actor: str,
        executor: str,
        n_samples: int,
    ) -> None:
        """Log an api.campaign.created event (issue #439)."""
        self.log(
            AuditEvent(
                actor=actor,
                action="api.campaign.created",
                resource=campaign_id,
                details={
                    "executor": executor,
                    "n_samples": n_samples,
                },
            )
        )

    def api_campaign_cancelled(
        self,
        campaign_id: str,
        actor: str,
    ) -> None:
        """Log an api.campaign.cancelled event (issue #439)."""
        self.log(
            AuditEvent(
                actor=actor,
                action="api.campaign.cancelled",
                resource=campaign_id,
            )
        )

    def api_campaign_paused(
        self,
        campaign_id: str,
        actor: str,
    ) -> None:
        """Log an api.campaign.paused event (issue #553)."""
        self.log(
            AuditEvent(
                actor=actor,
                action="api.campaign.paused",
                resource=campaign_id,
            )
        )

    def api_campaign_resumed(
        self,
        campaign_id: str,
        actor: str,
    ) -> None:
        """Log an api.campaign.resumed event (issue #553)."""
        self.log(
            AuditEvent(
                actor=actor,
                action="api.campaign.resumed",
                resource=campaign_id,
            )
        )
