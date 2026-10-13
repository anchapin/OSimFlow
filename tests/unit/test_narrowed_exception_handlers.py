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


def test_aws_error_code_probe_handles_odd_shapes() -> None:
    from osimflow.executors.aws_batch_executor import _aws_error_code

    class Good(Exception):
        response = {"Error": {"Code": "Throttling"}}

    class BadMapping(Exception):
        response = "not-a-mapping"

    class NoneErr(Exception):
        response = {"Error": None}

    assert _aws_error_code(Good()) == "Throttling"
    assert _aws_error_code(ValueError("x")) == ""
    assert _aws_error_code(BadMapping()) == ""
    assert _aws_error_code(NoneErr()) == ""


def test_check_sqlite_reports_failure_on_sqlite_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    from osimflow import health

    def boom(*_a: object, **_k: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(health.sqlite3, "connect", boom)
    assert health._check_sqlite().status == health.CheckStatus.FAIL


def test_check_network_reports_failure_on_network_errors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import http.client
    import urllib.error

    from osimflow import health

    for exc in (
        urllib.error.URLError("down"),
        TimeoutError("t"),
        http.client.RemoteDisconnected("rd"),
        ValueError("bad url"),
    ):

        def boom(*_a: object, _exc: Exception = exc, **_k: object) -> None:
            raise _exc

        monkeypatch.setattr(health.urllib.request, "urlopen", boom)
        assert health._check_network().status != health.CheckStatus.PASS


def test_run_cli_probe_captures_oserror(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import subprocess

    from osimflow import _health_executors as he

    def boom(*_a: object, **_k: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(subprocess, "run", boom)
    rc, _out, err = he._run_subprocess_quiet(["x"], 1.0)
    assert rc is None and "PermissionError" in err
