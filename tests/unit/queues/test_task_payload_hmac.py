"""HMAC-SHA256 task-payload signature tests (issue #1177, #1205).

Covers the shared signing/verification helpers in
``osimflow.task_payload_hmac`` and the fail-closed verification gate in
``osimflow.remote_runner``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from osimflow import remote_runner
from osimflow.task_payload_hmac import (
    TASK_PAYLOAD_ENV,
    TASK_PAYLOAD_HMAC_ENV,
    TASK_PAYLOAD_SECRET_ENV,
    TASK_PAYLOAD_SECRET_FILE_ENV,
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
    monkeypatch.delenv(TASK_PAYLOAD_SECRET_FILE_ENV, raising=False)
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

    def test_reads_secret_from_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        """Issue #1633: the Swarm file fallback resolves the mounted Docker secret."""
        secret_file = tmp_path / "osimflow-hmac"
        secret_file.write_text(SECRET, encoding="utf-8")
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_FILE_ENV, str(secret_file))
        assert resolve_payload_secret() == SECRET

    def test_env_takes_precedence_over_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        secret_file = tmp_path / "osimflow-hmac"
        secret_file.write_text("file-secret", encoding="utf-8")
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_FILE_ENV, str(secret_file))
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
        assert resolve_payload_secret() == SECRET

    def test_file_strips_surrounding_whitespace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """`docker secret create` pipelines commonly append a trailing newline."""
        secret_file = tmp_path / "osimflow-hmac"
        secret_file.write_text(f"\n{SECRET}\n", encoding="utf-8")
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_FILE_ENV, str(secret_file))
        assert resolve_payload_secret() == SECRET

    def test_unreadable_file_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_FILE_ENV, "/nonexistent/osimflow-hmac")
        with pytest.raises(RuntimeError, match=TASK_PAYLOAD_SECRET_FILE_ENV):
            resolve_payload_secret()

    def test_empty_file_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        secret_file = tmp_path / "osimflow-hmac"
        secret_file.write_text("   \n", encoding="utf-8")
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_FILE_ENV, str(secret_file))
        with pytest.raises(RuntimeError, match="empty"):
            resolve_payload_secret()


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

    def test_include_secret_false_ships_signature_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #1633: signature-only mode for out-of-band secret delivery."""
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
        sig_env = build_signature_env(VALID_PAYLOAD, include_secret=False)
        assert TASK_PAYLOAD_SIG_ENV in sig_env
        assert TASK_PAYLOAD_SECRET_ENV not in sig_env
        assert sig_env[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(VALID_PAYLOAD, SECRET)

    def test_include_secret_false_empty_in_legacy_unsigned_mode(self) -> None:
        assert build_signature_env(VALID_PAYLOAD, include_secret=False) == {}

    def test_require_secret_raises_when_missing(self) -> None:
        """Issue #2025: executors invoking remote_runner must not fall back to {}."""
        with pytest.raises(RuntimeError, match=TASK_PAYLOAD_SECRET_ENV):
            build_signature_env(VALID_PAYLOAD, require_secret=True)

    def test_require_secret_raises_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "")
        with pytest.raises(RuntimeError, match=TASK_PAYLOAD_SECRET_ENV):
            build_signature_env(VALID_PAYLOAD, require_secret=True)

    def test_resolves_secret_from_secret_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret_file = tmp_path / "payload_secret"
        secret_file.write_text(f"{SECRET}\n", encoding="utf-8")
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_FILE_ENV, str(secret_file))
        sig_env = build_signature_env(VALID_PAYLOAD, include_secret=False, require_secret=True)
        assert sig_env == {TASK_PAYLOAD_SIG_ENV: sign_task_payload(VALID_PAYLOAD, SECRET)}


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

    def test_no_secret_rejected_fail_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD", VALID_PAYLOAD)
        with pytest.raises(RuntimeError, match="OSIMFLOW_TASK_PAYLOAD_SECRET is not configured"):
            remote_runner._load_payload()  # noqa: SLF001

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


class TestAwsBatchToRemoteRunnerAlignment:
    """Issue #2025: end-to-end alignment between AWSBatchExecutor and remote_runner."""

    AWS_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:osimflow-hmac-AbCdEf"

    def test_submit_fails_fast_before_submit_job_when_secret_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing secret fails locally before staging inputs or calling submit_job."""
        from unittest.mock import MagicMock

        from osimflow.executors import AWSBatchExecutor

        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        ex = AWSBatchExecutor()
        client = MagicMock()
        ex._client = client  # noqa: SLF001

        with pytest.raises(RuntimeError, match=TASK_PAYLOAD_SECRET_ENV):
            ex.submit(lambda: "ok", name="sim_0001")
        client.submit_job.assert_not_called()

        ex_with_arn = AWSBatchExecutor(payload_secret_arn=self.AWS_SECRET_ARN)
        ex_with_arn._client = client  # noqa: SLF001
        with pytest.raises(RuntimeError, match="aws-batch-payload-secret-arn"):
            ex_with_arn.submit(lambda: "ok", name="sim_0001")
        client.submit_job.assert_not_called()

    def test_end_to_end_build_environment_to_remote_runner_main(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Convert AWSBatchExecutor._build_environment(...) output into an env map,
        register a harmless step, invoke remote_runner.main(), and verify all 4 cases:
        1. valid signature executes once;
        2. altered payload or altered signature fails;
        3. missing secret fails before remote job submission;
        4. payload_secret_arn mode emits OSIMFLOW_TASK_PAYLOAD_SIG (TASK_PAYLOAD_HMAC_ENV)
           and no literal OSIMFLOW_TASK_PAYLOAD_SECRET override.
        """
        import sys
        from unittest.mock import MagicMock

        from osimflow.executors import AWSBatchExecutor
        from osimflow.remote_runner import StepFunctionRegistry

        monkeypatch.setattr(sys, "argv", ["osimflow.remote_runner"])
        executed: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def _harmless_step(*args: object, **kwargs: object) -> dict[str, object]:
            executed.append((args, kwargs))
            return {"status": "ran", "args": list(args)}

        monkeypatch.setitem(StepFunctionRegistry._registry, "harmless_2025", _harmless_step)  # noqa: SLF001
        payload_str = json.dumps(
            {
                "schema_version": 1,
                "name": "harmless_0001",
                "step": "harmless_2025",
                "args": ["sample-0001"],
                "kwargs": {"flag": True},
            }
        )

        # (3) Missing secret fails before remote job submission.
        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        ex_unsigned = AWSBatchExecutor()
        mock_batch = MagicMock()
        ex_unsigned._client = mock_batch  # noqa: SLF001
        with pytest.raises(RuntimeError, match=TASK_PAYLOAD_SECRET_ENV):
            ex_unsigned._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload=payload_str,
            )
        with pytest.raises(RuntimeError, match=TASK_PAYLOAD_SECRET_ENV):
            ex_unsigned.submit(_harmless_step, "sample-0001", name="sim_0001")
        mock_batch.submit_job.assert_not_called()
        assert executed == []

        # (1) Valid signature executes once in default (literal-secret) mode.
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
        ex_default = AWSBatchExecutor()
        env_entries = ex_default._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=payload_str,
        )
        env_map = {entry["name"]: entry["value"] for entry in env_entries}
        assert env_map[TASK_PAYLOAD_ENV] == payload_str
        assert env_map[TASK_PAYLOAD_HMAC_ENV] == sign_task_payload(payload_str, SECRET)
        assert env_map[TASK_PAYLOAD_SECRET_ENV] == SECRET

        for key in (TASK_PAYLOAD_ENV, TASK_PAYLOAD_SIG_ENV, TASK_PAYLOAD_SECRET_ENV):
            monkeypatch.delenv(key, raising=False)
        for key, value in env_map.items():
            monkeypatch.setenv(key, value)

        rc = remote_runner.main()
        assert rc == 0
        assert len(executed) == 1
        assert executed[0] == (("sample-0001",), {"flag": True})
        capsys.readouterr()

        # (2a) Altered payload fails without executing the step.
        executed.clear()
        tampered_payload = json.dumps(
            {
                "schema_version": 1,
                "name": "harmless_0001",
                "step": "harmless_2025",
                "args": ["tampered-sample"],
                "kwargs": {"flag": True},
            }
        )
        monkeypatch.setenv(TASK_PAYLOAD_ENV, tampered_payload)
        rc_tampered_payload = remote_runner.main()
        assert rc_tampered_payload == 1
        assert executed == []
        assert "verification failed" in capsys.readouterr().err

        # (2b) Altered signature fails without executing the step.
        monkeypatch.setenv(TASK_PAYLOAD_ENV, payload_str)
        monkeypatch.setenv(TASK_PAYLOAD_SIG_ENV, "0" * 64)
        rc_tampered_sig = remote_runner.main()
        assert rc_tampered_sig == 1
        assert executed == []
        assert "verification failed" in capsys.readouterr().err

        # (4) payload_secret_arn mode emits OSIMFLOW_TASK_PAYLOAD_SIG
        # (TASK_PAYLOAD_HMAC_ENV) and no literal OSIMFLOW_TASK_PAYLOAD_SECRET
        # override, and executes once when the container receives the secret
        # from containerProperties.secrets.
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
        ex_arn = AWSBatchExecutor(payload_secret_arn=self.AWS_SECRET_ARN)
        arn_env_entries = ex_arn._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=payload_str,
        )
        arn_env_map = {entry["name"]: entry["value"] for entry in arn_env_entries}
        assert TASK_PAYLOAD_HMAC_ENV in arn_env_map
        assert arn_env_map[TASK_PAYLOAD_HMAC_ENV] == sign_task_payload(payload_str, SECRET)
        assert TASK_PAYLOAD_SECRET_ENV not in arn_env_map

        for key in (TASK_PAYLOAD_ENV, TASK_PAYLOAD_SIG_ENV, TASK_PAYLOAD_SECRET_ENV):
            monkeypatch.delenv(key, raising=False)
        for key, value in arn_env_map.items():
            monkeypatch.setenv(key, value)
        # Simulate AWS Batch injecting the secret from containerProperties.secrets:
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)

        rc_arn = remote_runner.main()
        assert rc_arn == 0
        assert len(executed) == 1
        assert executed[0] == (("sample-0001",), {"flag": True})
