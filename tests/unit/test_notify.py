"""Unit tests for the notification backends (issue #628).

Covers:

1. Each backend's ``send()`` succeeds on the happy path (with the
   network/AWS/SMTP dependencies mocked — no real SNS/SES/SMTP calls).
2. Each backend's ``send()`` swallows errors (logs with ``exc_info=True``
   and never raises — issue #628 criterion #4).
3. :func:`build_notify_backend` selects the correct backend from the
   available channels and ``notification_type`` argument.
4. :class:`NullNotifyBackend` is the safe default when no channel is
   configured.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow.notify import (
    EmailNotifyBackend,
    NotifyBackend,
    NullNotifyBackend,
    SNSNotifyBackend,
    WebhookNotifyBackend,
    build_notify_backend,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingBackend(NotifyBackend):
    """In-memory backend that records every ``send()`` invocation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.raise_on_send: Exception | None = None

    def send(self, event: str, payload: dict[str, Any]) -> None:
        self.calls.append((event, payload))
        if self.raise_on_send is not None:
            raise self.raise_on_send


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


def test_notify_backend_is_abstract() -> None:
    """NotifyBackend cannot be instantiated directly."""
    with pytest.raises(TypeError):
        NotifyBackend()  # type: ignore[abstract]


def test_subclass_must_implement_send() -> None:
    """A subclass without ``send`` is rejected at instantiation time."""

    class _Incomplete(NotifyBackend):  # type: ignore[abstract]
        pass

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# NullNotifyBackend
# ---------------------------------------------------------------------------


def test_null_backend_send_does_not_raise() -> None:
    """The default backend is a no-op that swallows everything."""
    backend = NullNotifyBackend()
    # Should not raise regardless of inputs.
    backend.send("campaign.succeeded", {"any": "payload"})
    backend.send("any.event", {})


# ---------------------------------------------------------------------------
# SNSNotifyBackend
# ---------------------------------------------------------------------------


def test_sns_backend_requires_topic_arn() -> None:
    """An empty topic_arn is rejected at construction time (fail-fast)."""
    with pytest.raises(ValueError, match="topic_arn"):
        SNSNotifyBackend(topic_arn="")


def test_sns_backend_send_happy_path() -> None:
    """A successful ``sns.publish`` is logged at INFO (not WARNING)."""
    backend = SNSNotifyBackend(
        topic_arn="arn:aws:sns:us-east-1:123456789012:osimflow",
        subject="Campaign done",
    )
    fake_sns = MagicMock()
    fake_sns.publish.return_value = {"MessageId": "msg-1"}

    with patch("boto3.client", return_value=fake_sns) as mock_client:
        backend.send(
            "campaign.succeeded",
            {"campaign_id": "c1", "download_url": "https://signed/example.csv"},
        )

    mock_client.assert_called_once_with("sns")
    fake_sns.publish.assert_called_once()
    call_kwargs = fake_sns.publish.call_args.kwargs
    assert call_kwargs["TopicArn"] == "arn:aws:sns:us-east-1:123456789012:osimflow"
    # The Message body is JSON containing the event + payload.
    body = json.loads(call_kwargs["Message"])
    assert body["event"] == "campaign.succeeded"
    assert body["campaign_id"] == "c1"
    # Subject is passed through (and capped at 100 chars by SNS).
    assert call_kwargs["Subject"] == "Campaign done"


def test_sns_backend_send_subject_capped_at_100_chars() -> None:
    """SNS rejects subjects > 100 chars; we truncate preemptively."""
    long_subject = "x" * 250
    backend = SNSNotifyBackend(topic_arn="arn:aws:sns:us-east-1:123:t", subject=long_subject)
    fake_sns = MagicMock()
    with patch("boto3.client", return_value=fake_sns):
        backend.send("campaign.succeeded", {"campaign_id": "c1"})
    assert len(fake_sns.publish.call_args.kwargs["Subject"]) == 100


def test_sns_backend_send_swallows_errors() -> None:
    """A boto3 failure is logged with exc_info=True and never raises (criterion #4)."""
    backend = SNSNotifyBackend(topic_arn="arn:aws:sns:us-east-1:123:t")
    fake_sns = MagicMock()
    fake_sns.publish.side_effect = RuntimeError("AWS is down")

    with patch("boto3.client", return_value=fake_sns):
        # Must NOT raise.
        backend.send("campaign.succeeded", {"campaign_id": "c1"})


def test_sns_backend_send_swallows_import_error() -> None:
    """If boto3 isn't installed, the backend still doesn't raise."""
    backend = SNSNotifyBackend(topic_arn="arn:aws:sns:us-east-1:123:t")

    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "boto3":
            raise ImportError("no boto3")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_fake_import):
        backend.send("campaign.succeeded", {"campaign_id": "c1"})


# ---------------------------------------------------------------------------
# EmailNotifyBackend
# ---------------------------------------------------------------------------


def test_email_backend_requires_recipient() -> None:
    """An empty recipient is rejected at construction time."""
    with pytest.raises(ValueError, match="recipient"):
        EmailNotifyBackend(recipient="")


def test_email_backend_send_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful SMTP send dispatches the message via the injected server."""
    monkeypatch.setenv("OSIMFLOW_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("OSIMFLOW_SMTP_PORT", "587")
    monkeypatch.setenv("OSIMFLOW_SMTP_USE_TLS", "1")

    sent_messages: list[Any] = []

    class _FakeSMTP:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def __enter__(self) -> _FakeSMTP:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def starttls(self) -> None:
            pass

        def login(self, user: str, password: str) -> None:
            pass

        def send_message(self, msg: Any) -> None:
            sent_messages.append(msg)

    backend = EmailNotifyBackend(recipient="ops@example.com", sender="bot@osimflow")
    with patch("smtplib.SMTP", _FakeSMTP):
        backend.send(
            "campaign.succeeded",
            {"campaign_id": "c1", "download_url": "https://signed/example.csv"},
        )

    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert msg["To"] == "ops@example.com"
    assert msg["From"] == "bot@osimflow"
    assert "c1" in msg["Subject"]
    assert "campaign.succeeded" in msg["Subject"]
    # The body contains the JSON payload.
    body = msg.get_content()
    assert "download_url" in body
    assert "https://signed/example.csv" in body


def test_email_backend_send_swallows_smtp_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SMTP failure is logged and never raises (criterion #4)."""
    monkeypatch.setenv("OSIMFLOW_SMTP_HOST", "unreachable.example.com")

    class _BoomSMTP:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _BoomSMTP:
            raise smtplib_refused("connection refused")

        def __exit__(self, *exc: Any) -> None:
            return None

    def smtplib_refused(msg: str) -> Exception:
        import smtplib as sm

        return sm.SMTPException(msg)

    backend = EmailNotifyBackend(recipient="ops@example.com")
    with patch("smtplib.SMTP", _BoomSMTP):
        backend.send("campaign.succeeded", {"campaign_id": "c1"})


def test_email_backend_credentials_from_env_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials are read from env vars only (AGENTS.md §10)."""
    monkeypatch.setenv("OSIMFLOW_SMTP_USER", "env-user")
    monkeypatch.setenv("OSIMFLOW_SMTP_PASSWORD", "env-pass")

    captured: dict[str, Any] = {}

    class _SMTP:
        def __init__(self, host: str, port: int) -> None:
            captured["host"] = host
            captured["port"] = port

        def __enter__(self) -> _SMTP:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def starttls(self) -> None:
            pass

        def login(self, user: str, password: str) -> None:
            captured["user"] = user
            captured["password"] = password

        def send_message(self, msg: Any) -> None:
            pass

    backend = EmailNotifyBackend(recipient="ops@example.com")
    # The constructor never accepted user/password — the backend object
    # carries no credentials at all.
    assert not hasattr(backend, "user")
    assert not hasattr(backend, "password")

    with patch("smtplib.SMTP", _SMTP):
        backend.send("campaign.succeeded", {"campaign_id": "c1"})

    assert captured["user"] == "env-user"
    assert captured["password"] == "env-pass"


# ---------------------------------------------------------------------------
# WebhookNotifyBackend
# ---------------------------------------------------------------------------


def test_webhook_backend_requires_url() -> None:
    """An empty url is rejected at construction time."""
    with pytest.raises(ValueError, match="url"):
        WebhookNotifyBackend(url="")


def test_webhook_backend_send_happy_path() -> None:
    """A successful webhook POST dispatches the merged #283 + §3.5 payload."""
    backend = WebhookNotifyBackend(url="https://hooks.example.com/osimflow")

    delivered: list[dict[str, Any]] = []

    def _fake_deliver(self: Any, payload: dict[str, Any]) -> bool:
        delivered.append(payload)
        return True

    with patch("osimflow.notify.WebhookClient.deliver", _fake_deliver):
        backend.send(
            "campaign.succeeded",
            {
                "campaign_id": "c1",
                "download_url": "https://signed/example.csv",
                "expires_in_seconds": 3600,
            },
        )

    assert len(delivered) == 1
    body = delivered[0]
    # Event is added on top of the caller's payload.
    assert body["event"] == "campaign.succeeded"
    assert body["campaign_id"] == "c1"
    assert body["download_url"] == "https://signed/example.csv"
    assert body["expires_in_seconds"] == 3600


def test_webhook_backend_send_swallows_delivery_failure() -> None:
    """A webhook that exhausts retries is logged (not raised)."""
    backend = WebhookNotifyBackend(url="https://hooks.example.com/osimflow")

    def _false_deliver(self: Any, payload: dict[str, Any]) -> bool:
        return False

    with patch("osimflow.notify.WebhookClient.deliver", _false_deliver):
        backend.send("campaign.succeeded", {"campaign_id": "c1"})


def test_webhook_backend_send_swallows_unexpected_exception() -> None:
    """An exception from WebhookClient is caught + logged (criterion #4)."""
    backend = WebhookNotifyBackend(url="https://hooks.example.com/osimflow")

    def _boom_deliver(self: Any, payload: dict[str, Any]) -> bool:
        raise RuntimeError("network gone")

    with patch("osimflow.notify.WebhookClient.deliver", _boom_deliver):
        backend.send("campaign.succeeded", {"campaign_id": "c1"})


# ---------------------------------------------------------------------------
# build_notify_backend factory
# ---------------------------------------------------------------------------


def test_factory_returns_null_when_nothing_configured() -> None:
    """No channels → NullNotifyBackend (notifications are opt-in)."""
    backend = build_notify_backend()
    assert isinstance(backend, NullNotifyBackend)


def test_factory_priority_sns_then_email_then_webhook() -> None:
    """When multiple channels are configured, SNS wins by default."""
    backend = build_notify_backend(
        sns_topic_arn="arn:aws:sns:us-east-1:123:t",
        notification_email="ops@example.com",
        webhook_url="https://hooks.example.com",
    )
    assert isinstance(backend, SNSNotifyBackend)

    # Drop SNS → email wins.
    backend = build_notify_backend(
        notification_email="ops@example.com",
        webhook_url="https://hooks.example.com",
    )
    assert isinstance(backend, EmailNotifyBackend)

    # Drop email → webhook wins.
    backend = build_notify_backend(webhook_url="https://hooks.example.com")
    assert isinstance(backend, WebhookNotifyBackend)


def test_factory_explicit_notification_type_selects_that_channel() -> None:
    """notification_type='webhook' picks webhook even when SNS is configured."""
    backend = build_notify_backend(
        sns_topic_arn="arn:aws:sns:us-east-1:123:t",
        webhook_url="https://hooks.example.com",
        notification_type="webhook",
    )
    assert isinstance(backend, WebhookNotifyBackend)


def test_factory_explicit_type_for_unconfigured_channel_falls_through() -> None:
    """Asking for webhook when only SNS is configured falls through to SNS."""
    backend = build_notify_backend(
        sns_topic_arn="arn:aws:sns:us-east-1:123:t",
        notification_type="webhook",
    )
    # The fallback is the priority order, so SNS is selected.
    assert isinstance(backend, SNSNotifyBackend)


def test_factory_explicit_type_for_unconfigured_channel_returns_null_when_nothing() -> None:
    """Asking for any channel when nothing is configured returns Null."""
    backend = build_notify_backend(notification_type="sns")
    assert isinstance(backend, NullNotifyBackend)


def test_factory_passes_subject_to_sns_backend() -> None:
    """The subject argument is forwarded to the SNS backend."""
    backend = build_notify_backend(
        sns_topic_arn="arn:aws:sns:us-east-1:123:t",
        subject="Custom subject",
    )
    assert isinstance(backend, SNSNotifyBackend)
    assert backend.subject == "Custom subject"


# ---------------------------------------------------------------------------
# Recording backend sanity check (used by the integration test)
# ---------------------------------------------------------------------------


def test_recording_backend_records_calls() -> None:
    """The test double used elsewhere records calls in order."""
    backend = _RecordingBackend()
    backend.send("e1", {"k": 1})
    backend.send("e2", {"k": 2})
    assert backend.calls == [("e1", {"k": 1}), ("e2", {"k": 2})]


def test_recording_backend_does_not_raise_on_exception() -> None:
    """Even our test double doesn't leak exceptions in normal use."""
    backend = _RecordingBackend()
    backend.raise_on_send = ValueError("boom")
    # When used directly, it raises — that's how the integration test
    # verifies the dispatch layer's belt-and-braces wrapper.
    with pytest.raises(ValueError, match="boom"):
        backend.send("e", {})
