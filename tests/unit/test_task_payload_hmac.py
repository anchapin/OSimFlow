"""HMAC-SHA256 task-payload signature tests (issue #1177).

Covers the shared signing/verification helpers in
``osimflow.task_payload_hmac``, the fail-closed verification gate in
``osimflow.remote_runner``, and the legacy unsigned path.
"""

from __future__ import annotations

import json
import logging

import pytest

from osimflow import remote_runner
from osimflow.task_payload_hmac import (
    TASK_PAYLOAD_SECRET_ENV,
    TASK_PAYLOAD_SIG_ENV,
    build_signature_env,
    resolve_payload_secret,
    sign_task_payload,
    verify_task_payload,
)

SECRET = "super-secret-shared-key"

VALID_PAYLOAD = json.dumps({"step": "sim", "args": [], "kwargs": {}})


@pytest.fixture(autouse=True)
def _clean_signature_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the host's signature configuration."""
    monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
    monkeypatch.delenv(TASK_PAYLOAD_SIG_ENV, raising=False)
    monkeypatch.delenv("NOMAD_META_task_payload", raising=False)
    monkeypatch.delenv("NOMAD_META_task_payload_sig", raising=False)
    monkeypatch.delenv("NOMAD_META_task_payload_secret", raising=False)


class TestSignVerifyHelpers:
    def test_sign_verify_roundtrip(self) -> None:
        signature = sign_task_payload(VALID_PAYLOAD, SECRET)
        assert verify_task_payload(VALID_PAYLOAD, signature, SECRET)

    def test_tampered_payload_rejected(self) -> None:
        signature = sign_task_payload(VALID_PAYLOAD, SECRET)
        tampered = json.dumps({"step": "sim", "args": ["injected"], "kwargs": {}})
        assert not verify_task_payload(tampered, signature, SECRET)

    def test_missing_signature_with_secret_configured_rejected(self) -> None:
        assert not verify_task_payload(VALID_PAYLOAD, None, SECRET)
        assert not verify_task_payload(VALID_PAYLOAD, "", SECRET)

    def test_wrong_secret_rejected(self) -> None:
        signature = sign_task_payload(VALID_PAYLOAD, SECRET)
        assert not verify_task_payload(VALID_PAYLOAD, signature, "other-secret")


class TestResolvePayloadSecret:
    def test_reads_secret_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
        assert resolve_payload_secret() == SECRET

    def test_reads_secret_from_nomad_meta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOMAD_META_task_payload_secret", SECRET)
        assert resolve_payload_secret() == SECRET

    def test_env_takes_precedence_over_nomad_meta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
        monkeypatch.setenv("NOMAD_META_task_payload_secret", "meta-secret")
        assert resolve_payload_secret() == SECRET

    def test_none_when_unconfigured(self) -> None:
        assert resolve_payload_secret() is None


class TestBuildSignatureEnv:
    def test_includes_signature_and_secret_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
        sig_env = build_signature_env(VALID_PAYLOAD)
        assert sig_env[TASK_PAYLOAD_SECRET_ENV] == SECRET
        assert sig_env[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(VALID_PAYLOAD, SECRET)

    def test_explicit_secret_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "env-secret")
        sig_env = build_signature_env(VALID_PAYLOAD, secret=SECRET)
        assert sig_env[TASK_PAYLOAD_SECRET_ENV] == SECRET
        assert sig_env[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(VALID_PAYLOAD, SECRET)

    def test_empty_in_legacy_unsigned_mode(self) -> None:
        assert build_signature_env(VALID_PAYLOAD) == {}


class TestRemoteRunnerVerification:
    def test_valid_signature_decodes_and_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD", VALID_PAYLOAD)
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
        monkeypatch.setenv(TASK_PAYLOAD_SIG_ENV, sign_task_payload(VALID_PAYLOAD, SECRET))
        payload = remote_runner._load_payload()  # noqa: SLF001
        assert payload["step"] == "sim"

    def test_tampered_signature_fails_closed_before_decode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tampered = json.dumps({"step": "sim", "args": ["injected"], "kwargs": {}})
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD", tampered)
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
        # Signature over the ORIGINAL payload — the env var was rewritten.
        monkeypatch.setenv(TASK_PAYLOAD_SIG_ENV, sign_task_payload(VALID_PAYLOAD, SECRET))
        with pytest.raises(RuntimeError, match="verification failed"):
            remote_runner._load_payload()  # noqa: SLF001

    def test_missing_signature_with_secret_configured_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD", VALID_PAYLOAD)
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
        with pytest.raises(RuntimeError, match="verification failed"):
            remote_runner._load_payload()  # noqa: SLF001

    def test_nomad_meta_signature_verifies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOMAD_META_task_payload", VALID_PAYLOAD)  # noqa: SIM112
        monkeypatch.setenv("NOMAD_META_task_payload_secret", SECRET)  # noqa: SIM112
        monkeypatch.setenv(  # noqa: SIM112
            "NOMAD_META_task_payload_sig", sign_task_payload(VALID_PAYLOAD, SECRET)
        )
        payload = remote_runner._load_payload()  # noqa: SLF001
        assert payload["step"] == "sim"

    def test_legacy_no_secret_warns_but_executes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD", VALID_PAYLOAD)
        with caplog.at_level(logging.WARNING, logger="osimflow.remote_runner"):
            payload = remote_runner._load_payload()  # noqa: SLF001
        assert payload["step"] == "sim"
        assert any(
            "legacy mode" in record.message and "unsigned" in record.message
            for record in caplog.records
        )

    def test_main_fails_closed_without_executing_step(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        executed: list[dict[str, object]] = []
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD", VALID_PAYLOAD)
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
        monkeypatch.setenv(TASK_PAYLOAD_SIG_ENV, "deadbeef")
        monkeypatch.setattr(
            remote_runner,
            "_run_payload",
            lambda payload: executed.append(payload),
        )
        rc = remote_runner.main()
        assert rc == 1
        assert executed == []
        stderr = capsys.readouterr().err
        assert "verification failed" in stderr
