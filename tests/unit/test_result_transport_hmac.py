"""HMAC coverage for the ``OSIMFLOW_RESULT_*`` transport settings (issue #1549).

The payload HMAC (#1177) signs only ``OSIMFLOW_TASK_PAYLOAD``.  The remote
runner reads the entire result-transport configuration — endpoint, bucket,
prefix, backend, and the ``allow_insecure_storage_endpoint`` escape hatch —
from the same unsigned job environment / Nomad dispatch meta.  Issue #1549
closes the gap with a second HMAC over a canonical serialization of those
settings: the executors emit ``OSIMFLOW_RESULT_TRANSPORT_SIG`` next to the
transport env vars, and ``remote_runner`` verifies it *before* constructing
the storage backend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from osimflow.task_payload_hmac import (
    RESULT_TRANSPORT_SIG_ENV,
    build_signature_env,
    build_transport_signature_env,
    canonical_result_transport_settings,
    sign_task_payload,
    verify_task_payload,
)

SECRET = "test-secret-1549"


@dataclass
class _Transport:
    """Minimal ResultTransportConfig stand-in (duck-typed)."""

    mode: str = "object_storage"
    backend: str = "s3"
    bucket: str = "results"
    prefix: str = "camp-1"
    endpoint: str = "https://minio.example.com"


def _signed_env(transport: _Transport, *, secret: str = SECRET) -> dict[str, str]:
    """Build the full signed transport env the executors emit."""
    env: dict[str, str] = {
        "OSIMFLOW_RESULT_TRANSPORT_MODE": transport.mode,
        "OSIMFLOW_RESULT_STORAGE_BACKEND": transport.backend,
        "OSIMFLOW_RESULT_STORAGE_BUCKET": transport.bucket,
        "OSIMFLOW_RESULT_STORAGE_PREFIX": transport.prefix,
        "OSIMFLOW_RESULT_STORAGE_ENDPOINT": transport.endpoint,
    }
    env.update(build_transport_signature_env(transport, secret=secret))
    return env


class TestCanonicalSerialization:
    def test_deterministic_sorted_keys(self) -> None:
        a = canonical_result_transport_settings(
            mode="object_storage",
            backend="s3",
            bucket="b",
            prefix="p",
            endpoint="https://e",
            allow_insecure=False,
        )
        b = canonical_result_transport_settings(
            allow_insecure=False,
            endpoint="https://e",
            prefix="p",
            bucket="b",
            backend="s3",
            mode="object_storage",
        )
        assert a == b
        # sorted-key compact JSON
        parsed = json.loads(a)
        assert list(parsed.keys()) == sorted(parsed.keys())

    def test_none_preserved_as_null(self) -> None:
        canonical = canonical_result_transport_settings(
            mode="auto",
            backend=None,
            bucket=None,
            prefix=None,
            endpoint=None,
            allow_insecure=False,
        )
        assert json.loads(canonical) == {
            "allow_insecure": False,
            "backend": None,
            "bucket": None,
            "endpoint": None,
            "mode": "auto",
            "prefix": None,
        }


class TestBuildTransportSignatureEnv:
    def test_signed_mode_emits_signature(self) -> None:
        env = build_transport_signature_env(_Transport(), secret=SECRET)
        assert list(env.keys()) == [RESULT_TRANSPORT_SIG_ENV]
        canonical = canonical_result_transport_settings(
            mode="object_storage",
            backend="s3",
            bucket="results",
            prefix="camp-1",
            endpoint="https://minio.example.com",
            allow_insecure=False,
        )
        assert env[RESULT_TRANSPORT_SIG_ENV] == sign_task_payload(canonical, SECRET)
        assert verify_task_payload(canonical, env[RESULT_TRANSPORT_SIG_ENV], SECRET)

    def test_unsigned_mode_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OSIMFLOW_TASK_PAYLOAD_SECRET", raising=False)
        assert build_transport_signature_env(_Transport(), secret=None) == {}

    def test_secret_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", SECRET)
        env = build_transport_signature_env(_Transport())
        assert RESULT_TRANSPORT_SIG_ENV in env


class TestUploadVerifiesTransportSignature:
    """Tampering with any result-transport env var must fail before upload."""

    def _run_upload(self, env: dict[str, str], result: Any = None) -> None:
        import osimflow.remote_runner as rr

        def fake_get(env_key: str, meta_key: str) -> str | None:  # noqa: ANN001
            # Mirror the env-then-meta resolution with our dict.
            return env.get(env_key)

        with (
            patch.object(rr, "_get_env_or_nomad_meta", side_effect=fake_get),
            patch.object(rr, "resolve_payload_secret", return_value=SECRET),
            patch.object(rr, "build_result_storage") as mock_storage,
        ):
            rr._upload_artifacts_for_object_storage(result or {"files": []})
        assert mock_storage.called

    def _assert_tamper_fails_before_upload(self, tamper: dict[str, str]) -> None:
        import osimflow.remote_runner as rr

        env = _signed_env(_Transport())
        env.update(tamper)

        def fake_get(env_key: str, meta_key: str) -> str | None:  # noqa: ANN001
            return env.get(env_key)

        with (
            patch.object(rr, "_get_env_or_nomad_meta", side_effect=fake_get),
            patch.object(rr, "resolve_payload_secret", return_value=SECRET),
            patch.object(rr, "build_result_storage") as mock_storage,
        ):
            with pytest.raises(RuntimeError, match="HMAC verification"):
                rr._upload_artifacts_for_object_storage({"files": []})
        assert not mock_storage.called, "storage backend must not be constructed"

    def test_valid_signature_proceeds_to_upload(self) -> None:
        self._run_upload(_signed_env(_Transport()))

    @pytest.mark.parametrize(
        "tamper",
        [
            {"OSIMFLOW_RESULT_STORAGE_ENDPOINT": "http://attacker.example.com"},
            {"OSIMFLOW_RESULT_STORAGE_BUCKET": "exfil-bucket"},
            {"OSIMFLOW_RESULT_STORAGE_PREFIX": "attacker-prefix"},
            {"OSIMFLOW_RESULT_STORAGE_BACKEND": "gcs"},
            {
                # Attacker rewrites endpoint AND bucket together — still
                # breaks the signature (canonical serialization covers all
                # five fields).
                "OSIMFLOW_RESULT_STORAGE_ENDPOINT": "http://attacker.example.com",
                "OSIMFLOW_RESULT_STORAGE_BUCKET": "exfil-bucket",
            },
            # Tampering with the escape hatch breaks the signature too:
            {"OSIMFLOW_ALLOW_INSECURE_STORAGE_ENDPOINT": "1"},
        ],
    )
    def test_tampered_env_fails_before_upload(self, tamper: dict[str, str]) -> None:
        self._assert_tamper_fails_before_upload(tamper)

    def test_missing_signature_in_signed_mode_fails(self) -> None:
        env = _signed_env(_Transport())
        del env[RESULT_TRANSPORT_SIG_ENV]
        import osimflow.remote_runner as rr

        def fake_get(env_key: str, meta_key: str) -> str | None:  # noqa: ANN001
            return env.get(env_key)

        with (
            patch.object(rr, "_get_env_or_nomad_meta", side_effect=fake_get),
            patch.object(rr, "resolve_payload_secret", return_value=SECRET),
            patch.object(rr, "build_result_storage") as mock_storage,
        ):
            with pytest.raises(RuntimeError, match="HMAC verification"):
                rr._upload_artifacts_for_object_storage({"files": []})
        assert not mock_storage.called

    def test_unsigned_mode_allows_insecure_flag_ignored(self) -> None:
        """Legacy unsigned mode: env allow-insecure is NOT honored (issue #1549)."""
        import osimflow.remote_runner as rr

        env = {
            "OSIMFLOW_RESULT_TRANSPORT_MODE": "object_storage",
            "OSIMFLOW_RESULT_STORAGE_BACKEND": "s3",
            "OSIMFLOW_RESULT_STORAGE_BUCKET": "results",
            "OSIMFLOW_RESULT_STORAGE_ENDPOINT": "http://10.0.0.9:9000",
            "OSIMFLOW_ALLOW_INSECURE_STORAGE_ENDPOINT": "1",
        }

        def fake_get(env_key: str, meta_key: str) -> str | None:  # noqa: ANN001
            return env.get(env_key)

        with (
            patch.object(rr, "_get_env_or_nomad_meta", side_effect=fake_get),
            patch.object(rr, "resolve_payload_secret", return_value=None),
            patch.object(rr, "build_result_storage") as mock_storage,
        ):
            rr._upload_artifacts_for_object_storage({"files": []})
        assert mock_storage.called
        # The unauthenticated escape hatch was dropped at the worker.
        assert mock_storage.call_args.kwargs.get("allow_insecure_endpoint") is False

    def test_signed_mode_allows_authenticated_insecure_flag(self) -> None:
        """Signed mode: an authenticated allow-insecure value is honored."""
        import osimflow.remote_runner as rr

        transport = _Transport(endpoint="http://10.0.0.9:9000")
        env = _signed_env(transport)
        # Re-sign with allow_insecure=True — the orchestrator-side would
        # have signed the flag it intentionally forwarded.
        canonical = canonical_result_transport_settings(
            mode=transport.mode,
            backend=transport.backend,
            bucket=transport.bucket,
            prefix=transport.prefix,
            endpoint=transport.endpoint,
            allow_insecure=True,
        )
        env[RESULT_TRANSPORT_SIG_ENV] = sign_task_payload(canonical, SECRET)
        env["OSIMFLOW_ALLOW_INSECURE_STORAGE_ENDPOINT"] = "1"

        def fake_get(env_key: str, meta_key: str) -> str | None:  # noqa: ANN001
            return env.get(env_key)

        with (
            patch.object(rr, "_get_env_or_nomad_meta", side_effect=fake_get),
            patch.object(rr, "resolve_payload_secret", return_value=SECRET),
            patch.object(rr, "build_result_storage") as mock_storage,
        ):
            rr._upload_artifacts_for_object_storage({"files": []})
        assert mock_storage.called
        assert mock_storage.call_args.kwargs.get("allow_insecure_endpoint") is True


class TestExecutorEnvEmission:
    """Each transport-carrying executor emits the signature next to the env."""

    def test_build_transport_signature_env_matches_runner_canonicalization(
        self,
    ) -> None:
        """The signature helper and the runner agree byte-for-byte."""
        transport = _Transport()
        env = build_transport_signature_env(transport, secret=SECRET)
        canonical = canonical_result_transport_settings(
            mode=transport.mode,
            backend=transport.backend,
            bucket=transport.bucket,
            prefix=transport.prefix,
            endpoint=transport.endpoint,
            allow_insecure=False,
        )
        assert verify_task_payload(canonical, env[RESULT_TRANSPORT_SIG_ENV], SECRET)

    def test_payload_and_transport_signatures_are_independent(self) -> None:
        payload = '{"step": "RUN_OPENSTUDIO_SIM"}'
        payload_env = build_signature_env(payload, secret=SECRET)
        transport_env = build_transport_signature_env(_Transport(), secret=SECRET)
        # The payload signature must not verify the transport canonical and
        # vice versa — they cover different material.
        canonical = canonical_result_transport_settings(
            mode="object_storage",
            backend="s3",
            bucket="results",
            prefix="camp-1",
            endpoint="https://minio.example.com",
            allow_insecure=False,
        )
        assert not verify_task_payload(canonical, payload_env["OSIMFLOW_TASK_PAYLOAD_SIG"], SECRET)
        assert not verify_task_payload(payload, transport_env[RESULT_TRANSPORT_SIG_ENV], SECRET)


def test_result_files_collector_untouched(tmp_path: Path) -> None:
    """Sanity: the upload path still walks file trees (regression guard)."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "artifact.txt").write_text("data")
    from osimflow.remote_runner import _collect_paths

    paths = _collect_paths({"files": [work]})
    assert paths == [work]
