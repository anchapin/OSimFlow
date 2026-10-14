"""Issues #1833, #2026: ``AWSBatchExecutor`` submitter vs worker credential enforcement."""

import argparse
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import botocore.credentials
import botocore.exceptions
import pytest

from osimflow.config import AWSBatchConfig, CampaignConfig
from osimflow.executor_configs import aws_batch
from osimflow.executors.aws_batch_executor import (
    AWSBatchExecutor,
    _SubmitterCredentialResolver,
    _validate_submitter_credentials,
)

FLAG = "--aws-batch-allow-long-lived-credentials"


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    aws_batch.add_arguments(parser)
    return parser.parse_args(argv)


def test_flag_defaults_off() -> None:
    assert _parse([]).aws_batch_allow_long_lived_credentials is False


def test_flag_enables() -> None:
    assert _parse([FLAG]).aws_batch_allow_long_lived_credentials is True


def test_kwargs_for_executor_default_and_enabled() -> None:
    assert aws_batch.kwargs_for_executor()["allow_long_lived_credentials"] is False
    kwargs = aws_batch.kwargs_for_executor(aws_batch_allow_long_lived_credentials=True)
    assert kwargs["allow_long_lived_credentials"] is True


def test_config_field_default_and_propagation() -> None:
    assert AWSBatchConfig().allow_long_lived_credentials is False
    cfg = CampaignConfig(
        input_variables=Path("v.yml"),
        template_sim_package=Path("pkg"),
        n_samples=1,
        outdir=Path("out"),
        openstudio_version="3.11.0",
        aws_batch_allow_long_lived_credentials=True,
    )
    assert cfg.aws_batch is not None
    assert cfg.aws_batch.allow_long_lived_credentials is True


def test_executor_receives_flag_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    kwargs = aws_batch.kwargs_for_executor(aws_batch_allow_long_lived_credentials=True)
    with patch("boto3.client", MagicMock()), caplog.at_level(logging.WARNING):
        executor = AWSBatchExecutor(**kwargs)
    assert executor._allow_long_lived_credentials is True
    assert executor._botocore_session is None
    assert any("allow_long_lived_credentials=True" in r.getMessage() for r in caplog.records)


def test_executor_default_does_not_enable() -> None:
    kwargs = aws_batch.kwargs_for_executor()
    with patch("boto3.client", MagicMock()):
        executor = AWSBatchExecutor(**kwargs)
    assert executor._allow_long_lived_credentials is False


def test_default_session_preserves_federated_and_role_providers() -> None:
    """Issue #2026: default session must keep SSO, AssumeRole, web-identity,
    and process providers in the resolver chain rather than stripping to IMDS-only."""
    executor = AWSBatchExecutor()
    resolver = executor._botocore_session.get_component("credential_provider")
    assert isinstance(resolver, _SubmitterCredentialResolver)
    provider_methods = {getattr(p, "METHOD", None) for p in resolver.providers}
    for expected in (
        "env",
        "sso",
        "assume-role",
        "assume-role-with-web-identity",
        "custom-process",
        "shared-credentials-file",
        "container-role",
        "iam-role",
    ):
        assert expected in provider_methods, f"missing provider method {expected!r}"
    # Delegated CredentialResolver methods remain accessible via __getattr__.
    assert resolver.get_provider("sso") is not None


@pytest.mark.parametrize(
    ("method", "token"),
    [
        ("sso", "sso-temp-token"),
        ("assume-role", "assume-role-temp-token"),
        ("assume-role-with-web-identity", "web-identity-temp-token"),
        ("custom-process", "process-temp-token"),
        ("iam-role", "imds-temp-token"),
        ("container-role", "ecs-container-temp-token"),
        ("ec2-instance-metadata", "ec2-metadata-temp-token"),
        ("boto-config", "boto-config-temp-token"),
        ("env", "sts-env-session-token"),
        ("shared-credentials-file", "sts-shared-file-session-token"),
    ],
)
def test_default_accepts_federated_role_and_session_token_credentials(
    method: str, token: str
) -> None:
    """Issue #2026: temporary/federated/role credentials must be accepted by default."""
    executor = AWSBatchExecutor()
    creds = botocore.credentials.Credentials(
        access_key="ASIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        token=token,
        method=method,
    )
    resolver = executor._botocore_session.get_component("credential_provider")
    with patch.object(resolver._resolver, "load_credentials", return_value=creds):
        resolved = executor._botocore_session.get_credentials()
    assert resolved is creds
    frozen = resolved.get_frozen_credentials()
    assert frozen.token == token
    assert resolved.method == method


@pytest.mark.parametrize("method", ["env", "shared-credentials-file", "config-file", "explicit"])
def test_default_rejects_static_credentials_without_session_token(method: str) -> None:
    """Issue #2026: direct static IAM user keys (token is None) must be rejected by default."""
    executor = AWSBatchExecutor()
    static_creds = botocore.credentials.Credentials(
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        token=None,
        method=method,
    )
    resolver = executor._botocore_session.get_component("credential_provider")
    with (
        patch.object(resolver._resolver, "load_credentials", return_value=static_creds),
        pytest.raises(
            RuntimeError,
            match=rf"direct static long-lived AWS credentials from {method!r} \(token is None\)",
        ),
    ):
        executor._botocore_session.get_credentials()


def test_sts_env_with_session_token_succeeds_without_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #2026: STS env vars with AWS_SESSION_TOKEN are accepted without static-key warning."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "FwoGZXIvYXdzEBYaDHsessionTokenExample")

    with caplog.at_level(logging.WARNING):
        executor = AWSBatchExecutor()

    assert not any("static AWS credentials detected" in r.getMessage() for r in caplog.records)
    resolved = executor._botocore_session.get_credentials()
    assert resolved is not None
    assert resolved.method == "env"
    assert resolved.get_frozen_credentials().token == "FwoGZXIvYXdzEBYaDHsessionTokenExample"


def test_static_env_without_session_token_warns_and_rejects(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #2026: static env vars without AWS_SESSION_TOKEN warn on init and fail on resolve."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)

    with caplog.at_level(logging.WARNING):
        executor = AWSBatchExecutor()

    assert any(
        "long-lived static AWS credentials detected in environment" in r.getMessage()
        for r in caplog.records
    )
    with pytest.raises(
        RuntimeError,
        match=r"direct static long-lived AWS credentials from 'env' \(token is None\)",
    ):
        executor._botocore_session.get_credentials()


def test_expired_sso_credentials_surface_clear_error() -> None:
    """Issue #2026: expired SSO token during credential refresh raises a clear RuntimeError."""
    executor = AWSBatchExecutor()

    def _failing_refresh() -> dict[str, str]:
        raise botocore.exceptions.UnauthorizedSSOTokenError()

    deferred_sso = botocore.credentials.DeferredRefreshableCredentials(
        refresh_using=_failing_refresh,
        method="sso",
    )
    resolver = executor._botocore_session.get_component("credential_provider")
    with (
        patch.object(resolver._resolver, "load_credentials", return_value=deferred_sso),
        pytest.raises(
            RuntimeError,
            match=r"failed to resolve temporary AWS credentials via 'sso'.*aws sso login",
        ),
    ):
        executor._botocore_session.get_credentials()


def test_provider_load_error_surfaces_clear_error() -> None:
    """Issue #2026: BotoCoreError raised by load_credentials() surfaces a clear RuntimeError."""
    executor = AWSBatchExecutor()
    resolver = executor._botocore_session.get_component("credential_provider")
    with (
        patch.object(
            resolver._resolver,
            "load_credentials",
            side_effect=botocore.exceptions.SSOTokenLoadError(error_msg="Cached token missing"),
        ),
        pytest.raises(
            RuntimeError,
            match=r"failed to resolve AWS submitter credentials.*aws sso login",
        ),
    ):
        executor._botocore_session.get_credentials()


def test_missing_credentials_surface_clear_error() -> None:
    """Issue #2026: when no provider resolves credentials, raise a clear actionable RuntimeError."""
    with pytest.raises(
        RuntimeError,
        match=r"no AWS submitter credentials found.*aws sso login",
    ):
        _validate_submitter_credentials(None)


def test_worker_environment_never_includes_submitter_aws_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #2026: worker containers rely strictly on IAM task/instance roles;
    submitter AWS_* credentials are never injected into containerOverrides.environment."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "FwoGZXIvYXdzEBYaDHsessionTokenExample")
    monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", "test-hmac-secret")

    executor = AWSBatchExecutor(payload_secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:x")
    env_list = executor._build_environment(
        container=None,
        openstudio_version="3.11.0",
        task_payload='{"step": "sim"}',
    )
    env_names = {entry["name"] for entry in env_list}
    assert "AWS_ACCESS_KEY_ID" not in env_names
    assert "AWS_SECRET_ACCESS_KEY" not in env_names
    assert "AWS_SESSION_TOKEN" not in env_names
