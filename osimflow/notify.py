"""Notification backends for campaign completion events (issue #628).

Provides a small backend abstraction behind the existing
``POST /api/v1/coordinator/campaigns/{id}/notify`` router so a finished
campaign can be announced via SNS, email, or webhook. All backends are
**best-effort**: failures are logged with ``exc_info=True`` and never
propagate, so a notification mishap can never flip a succeeded
campaign back to failed (issue #628 criterion #4).

The dispatch layer in :mod:`osimflow.api.coordinator` wraps every
``send()`` call in a ``try/except`` as belt-and-braces, but each
backend's ``send()`` is already responsible for swallowing its own
errors.

Credentials (AGENTS.md §10)
---------------------------
Backends source credentials from the execution IAM role or environment
variables only — never from long-lived keys committed to the repo:

* :class:`SNSNotifyBackend` calls ``boto3.client("sns")`` which walks
  the default AWS credential chain (IAM role on the compute
  environment → env → ``~/.aws/credentials``). No access-key arguments
  are accepted by the constructor.
* :class:`EmailNotifyBackend` uses :mod:`smtplib` with credentials
  pulled from ``OSIMFLOW_SMTP_USER`` / ``OSIMFLOW_SMTP_PASSWORD`` env
  vars (no auth when unset, matching an internal relay).
* :class:`WebhookNotifyBackend` POSTs over HTTPS; no credentials.

The ``boto3`` import is deferred to :meth:`SNSNotifyBackend.send` so
this module is importable without the ``[aws]`` extra installed — the
unit tests for the email and webhook backends do not require it.
"""

from __future__ import annotations

import abc
import json
import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Any

from .webhook import WebhookClient

log = logging.getLogger("osimflow.notify")


class NotifyBackend(abc.ABC):
    """Abstract base for a campaign notification delivery mechanism.

    Subclasses implement :meth:`send`. The contract is **best-effort**:
    a backend MUST swallow its own delivery errors (log with
    ``exc_info=True``) and never raise, so a notification failure can
    never affect campaign status (issue #628 criterion #4).
    """

    @abc.abstractmethod
    def send(self, event: str, payload: dict[str, Any]) -> None:
        """Deliver *payload* (tagged with *event*) via this backend.

        Implementations MUST be best-effort: any failure is logged with
        ``exc_info=True`` and swallowed. ``send()`` never raises.
        """
        ...


class NullNotifyBackend(NotifyBackend):
    """No-op default (notifications are opt-in).

    Returned by :func:`build_notify_backend` when no channel is
    configured so callers can invoke ``send()`` unconditionally without
    a ``None`` check.
    """

    def send(self, event: str, payload: dict[str, Any]) -> None:
        log.debug("NullNotifyBackend: ignoring event %s", event)


class SNSNotifyBackend(NotifyBackend):
    """Publishes the notification payload to an Amazon SNS topic ARN.

    Credentials are sourced from the default ``boto3`` chain (IAM role
    on the compute environment → env → ``~/.aws/credentials``). No
    long-lived AWS access keys are accepted by the constructor
    (AGENTS.md §10).
    """

    def __init__(self, topic_arn: str, subject: str | None = None) -> None:
        if not topic_arn:
            raise ValueError("SNSNotifyBackend requires a non-empty topic_arn")
        self.topic_arn = topic_arn
        self.subject = subject

    def send(self, event: str, payload: dict[str, Any]) -> None:
        try:
            import boto3  # noqa: PLC0415 — lazy import; [aws] is an optional extra

            message = json.dumps({"event": event, **payload}, default=str, sort_keys=True)
            publish_kwargs: dict[str, Any] = {
                "TopicArn": self.topic_arn,
                "Message": message,
            }
            if self.subject:
                # SNS caps Subject at 100 characters.
                publish_kwargs["Subject"] = self.subject[:100]
            sns = boto3.client("sns")
            sns.publish(**publish_kwargs)
            log.info(
                "SNS notification published to %s (event=%s)",
                self.topic_arn,
                event,
            )
        except Exception as exc:
            log.warning(
                "SNS notification to %s failed (event=%s): %s",
                self.topic_arn,
                event,
                exc,
                exc_info=True,
            )


class EmailNotifyBackend(NotifyBackend):
    """Sends the notification payload as a plain-text email.

    SMTP connection parameters and credentials are sourced exclusively
    from environment variables (``OSIMFLOW_SMTP_HOST``,
    ``OSIMFLOW_SMTP_PORT``, ``OSIMFLOW_SMTP_USER``,
    ``OSIMFLOW_SMTP_PASSWORD``, ``OSIMFLOW_SMTP_USE_TLS``) — never from
    constructor arguments or committed config (AGENTS.md §10).
    """

    def __init__(self, recipient: str, sender: str = "osimflow@coordinator") -> None:
        if not recipient:
            raise ValueError("EmailNotifyBackend requires a non-empty recipient")
        self.recipient = recipient
        self.sender = sender

    def send(self, event: str, payload: dict[str, Any]) -> None:
        try:
            host = os.environ.get("OSIMFLOW_SMTP_HOST", "localhost")
            port = int(os.environ.get("OSIMFLOW_SMTP_PORT", "25"))
            user = os.environ.get("OSIMFLOW_SMTP_USER")
            password = os.environ.get("OSIMFLOW_SMTP_PASSWORD")
            use_tls = os.environ.get("OSIMFLOW_SMTP_USE_TLS", "0") in (
                "1",
                "true",
                "yes",
                "on",
            )

            msg = EmailMessage()
            campaign_id = str(payload.get("campaign_id", "(unknown)"))
            msg["Subject"] = f"[OSimFlow] Campaign {campaign_id} — {event}"
            msg["From"] = self.sender
            msg["To"] = self.recipient
            msg.set_content(
                "OSimFlow campaign notification\n"
                "==============================\n"
                f"Event:    {event}\n"
                "Payload:\n"
                f"{json.dumps(payload, indent=2, default=str)}\n",
            )

            with smtplib.SMTP(host, port) as server:
                if use_tls:
                    server.starttls()
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
            log.info(
                "Email notification sent to %s (event=%s)",
                self.recipient,
                event,
            )
        except Exception as exc:
            log.warning(
                "Email notification to %s failed (event=%s): %s",
                self.recipient,
                event,
                exc,
                exc_info=True,
            )


class WebhookNotifyBackend(NotifyBackend):
    """POSTs the notification as JSON to a webhook URL.

    Reuses :class:`osimflow.webhook.WebhookClient` (the issue #283
    callback delivery client) so the wire format and retry semantics
    (exponential backoff, 3 retries) match the existing campaign-
    completion webhook exactly.
    """

    def __init__(self, url: str) -> None:
        if not url:
            raise ValueError("WebhookNotifyBackend requires a non-empty url")
        self.url = url

    def send(self, event: str, payload: dict[str, Any]) -> None:
        try:
            client = WebhookClient(url=self.url)
            # The #283 callback shape is the base; we extend it with the
            # event tag and the presigned download_url + lifetime so the
            # recipient can fetch the aggregated results directly.
            body: dict[str, Any] = {"event": event, **payload}
            ok = client.deliver(body)
            if ok:
                log.info(
                    "Webhook notification delivered to %s (event=%s)",
                    self.url,
                    event,
                )
            else:
                log.warning(
                    "Webhook notification to %s exhausted retries (event=%s)",
                    self.url,
                    event,
                )
        except Exception as exc:
            log.warning(
                "Webhook notification to %s failed (event=%s): %s",
                self.url,
                event,
                exc,
                exc_info=True,
            )


def build_notify_backend(
    *,
    sns_topic_arn: str | None = None,
    notification_email: str | None = None,
    webhook_url: str | None = None,
    notification_type: str | None = None,
    subject: str | None = None,
) -> NotifyBackend:
    """Select a single :class:`NotifyBackend` from the available channels.

    Resolution order when ``notification_type`` is ``None`` (or points
    at a channel that is not configured):

    1. ``sns_topic_arn``        -> :class:`SNSNotifyBackend`
    2. ``notification_email``   -> :class:`EmailNotifyBackend`
    3. ``webhook_url``          -> :class:`WebhookNotifyBackend`
    4. (none configured)        -> :class:`NullNotifyBackend`

    When ``notification_type`` is one of ``"sns"`` / ``"email"`` /
    ``"webhook"`` **and** that channel is configured, it is selected
    explicitly. An explicit request for an unconfigured channel falls
    through to the priority order above (so a caller asking for
    ``"webhook"`` on a campaign that only configured SNS still gets
    notified via SNS, rather than silently dropping the notification).

    The returned backend's :meth:`NotifyBackend.send` is always
    best-effort — never raising — so callers can invoke it without a
    ``try/except`` wrapper, although one is recommended as
    belt-and-braces (issue #628 criterion #4).
    """
    sns = SNSNotifyBackend(topic_arn=sns_topic_arn, subject=subject) if sns_topic_arn else None
    email = EmailNotifyBackend(recipient=notification_email) if notification_email else None
    webhook = WebhookNotifyBackend(url=webhook_url) if webhook_url else None

    by_type: dict[str, NotifyBackend | None] = {
        "sns": sns,
        "email": email,
        "webhook": webhook,
    }

    if notification_type and notification_type in by_type:
        chosen = by_type[notification_type]
        if chosen is not None:
            return chosen
        # Explicit type requested but channel not configured — fall
        # through to the priority order so we still deliver something.
        log.warning(
            "build_notify_backend: notification_type=%r requested but the "
            "channel is not configured; falling through to priority order.",
            notification_type,
        )

    for backend in (sns, email, webhook):
        if backend is not None:
            return backend
    return NullNotifyBackend()
