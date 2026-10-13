"""Narrowed ``except`` handlers (issue #1943): expected errors are handled, others propagate."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from redis.exceptions import ConnectionError as RedisConnectionError

from osimflow.distributed_cache import _redis_errors
from osimflow.notifications.alerting import (
    EmailDestination,
    load_alert_destinations_from_yaml,
    load_alert_rules_from_yaml,
)
from osimflow.storage import S3ArtifactStorage


def test_redis_errors_cover_redis_and_os_errors() -> None:
    errs = _redis_errors()
    assert issubclass(RedisConnectionError, errs)
    assert issubclass(ConnectionResetError, errs)
    assert not issubclass(KeyError, errs)


def test_s3_exists_false_on_client_error_and_propagates_others() -> None:
    store = S3ArtifactStorage.__new__(S3ArtifactStorage)
    store.bucket = "b"
    store.prefix = ""
    client = MagicMock()
    store._client = client
    client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
    assert store.artifact_exists("x") is False
    client.head_object.side_effect = KeyError("boom")
    with pytest.raises(KeyError):
        store.artifact_exists("x")


def test_alert_loaders_tolerate_bad_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "bad.yml"
    bad.write_text("a: [unclosed\n", encoding="utf-8")
    assert load_alert_rules_from_yaml(bad) == []
    assert load_alert_destinations_from_yaml(bad) == []
    assert load_alert_rules_from_yaml(tmp_path / "missing.yml") == []
    assert load_alert_destinations_from_yaml(tmp_path / "missing.yml") == []


def test_email_destination_returns_false_on_smtp_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import smtplib

    from osimflow.notifications.alerting import Alert

    def boom(*_a: object, **_k: object) -> None:
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    dest = EmailDestination(smtp_host="localhost", recipients=["a@b.c"])
    alert = Alert(
        rule_name="n",
        event_type="e",
        severity="WARNING",
        message="m",
        context={},
        timestamp=0.0,
    )
    assert dest.send(alert) is False
