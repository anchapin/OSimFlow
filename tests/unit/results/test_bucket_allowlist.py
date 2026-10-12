"""Issue #1670: Coordinator bucket allowlist + endpoint policy.

Operators gate the Coordinator's result-storage surface (handoff bucket,
storage endpoint, plaintext-endpoint escape hatch) through three server-side
env vars read once by ``create_app``:

- ``OSIMFLOW_ALLOWED_RESULT_BUCKETS`` — comma-separated bucket/container names.
  Handoff payloads naming any other bucket are rejected with 403 before any
  record is persisted; result-presign endpoints refuse the same. ``None`` /
  unset preserves legacy behavior with a startup WARNING.
- ``OSIMFLOW_RESULT_STORAGE_ENDPOINT`` — operator override for
  ``result_storage_endpoint`` (the handoff payload's ``extra`` value is
  IGNORED when the allowlist is set).
- ``OSIMFLOW_ALLOW_INSECURE_STORAGE_ENDPOINT`` — operator override for
  ``allow_insecure_storage_endpoint`` (the same payload field is ignored
  when the allowlist is set).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from osimflow.api import coordinator as coord
from osimflow.api import create_app
from osimflow.handoff_record import IDEMPOTENCY_KEY_HEADER

HANDOFF_URL = "/api/v1/coordinator/handoff"


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "test-campaign",
        "n_samples": 4,
        "executor": "aws_batch",
        "openstudio_version": "3.11.0",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clear_module_state() -> None:
    coord._campaigns.clear()
    coord._idempotency_keys.clear()
    # Reset the process-wide endpoint policy so test ordering doesn't leak
    # allowlist state across files.
    coord.set_coordinator_server_endpoint_policy(
        allowed_result_buckets=None,
        result_storage_endpoint=None,
        allow_insecure_storage_endpoint=False,
    )
    yield
    coord._campaigns.clear()
    coord._idempotency_keys.clear()
    coord.set_coordinator_server_endpoint_policy(
        allowed_result_buckets=None,
        result_storage_endpoint=None,
        allow_insecure_storage_endpoint=False,
    )


def _allowed_client(tmp_path: Path, *, allowed: str | None) -> TestClient:
    """Build a TestClient with the allowlist env var set (or unset)."""
    import os

    saved = os.environ.pop("OSIMFLOW_ALLOWED_RESULT_BUCKETS", None)
    if allowed is not None:
        os.environ["OSIMFLOW_ALLOWED_RESULT_BUCKETS"] = allowed
    try:
        app = create_app(outdir=tmp_path, read_only=False)
    finally:
        # create_app reads env at startup; restore AFTER so the env var
        # actually reached it during construction.
        if saved is None:
            os.environ.pop("OSIMFLOW_ALLOWED_RESULT_BUCKETS", None)
        else:
            os.environ["OSIMFLOW_ALLOWED_RESULT_BUCKETS"] = saved
    return TestClient(app)


class TestHandoffBucketAllowlist:
    """The Coordinator handoff endpoint refuses non-allowlisted buckets."""

    def test_handoff_without_allowlist_accepts_any_bucket(self, tmp_path: Path) -> None:
        """Legacy behavior: with no allowlist set, any bucket name is
        accepted. The startup WARNING is emitted by ``create_app`` but
        doesn't block the handoff."""
        client = _allowed_client(tmp_path, allowed=None)
        resp = client.post(
            HANDOFF_URL,
            json=_payload(result_storage_bucket="any-bucket-name"),
            headers={IDEMPOTENCY_KEY_HEADER: "k-any"},
        )
        assert resp.status_code == 202

    def test_handoff_with_allowlist_accepts_allowlisted_bucket(self, tmp_path: Path) -> None:
        client = _allowed_client(tmp_path, allowed="team-a-results,team-b-results")
        resp = client.post(
            HANDOFF_URL,
            json=_payload(result_storage_bucket="team-a-results"),
            headers={IDEMPOTENCY_KEY_HEADER: "k-a"},
        )
        assert resp.status_code == 202

    def test_handoff_with_allowlist_rejects_non_allowlisted_bucket(self, tmp_path: Path) -> None:
        client = _allowed_client(tmp_path, allowed="team-a-results")
        resp = client.post(
            HANDOFF_URL,
            json=_payload(result_storage_bucket="attacker-controlled-bucket"),
            headers={IDEMPOTENCY_KEY_HEADER: "k-attacker"},
        )
        assert resp.status_code == 403
        body = resp.json()
        # The 403 message names both the offending bucket and the env var
        # the operator should set — keeps the failure actionable.
        assert "attacker-controlled-bucket" in body["detail"]
        assert "OSIMFLOW_ALLOWED_RESULT_BUCKETS" in body["detail"]
        # Critically, NO record was persisted — the bucket rejection
        # happens before the record dict is built.
        assert not coord._campaigns

    def test_handoff_with_allowlist_when_bucket_is_none(self, tmp_path: Path) -> None:
        """When the allowlist is set but the handoff payload omits the
        bucket entirely, the handoff still succeeds (the bucket is
        optional for non-storage campaigns)."""
        client = _allowed_client(tmp_path, allowed="team-a-results")
        resp = client.post(
            HANDOFF_URL,
            json=_payload(),  # no result_storage_bucket
            headers={IDEMPOTENCY_KEY_HEADER: "k-nobucket"},
        )
        assert resp.status_code == 202


class TestResultStorageEndpointPolicy:
    """``_storage_from_campaign`` honors the operator endpoint policy."""

    def test_storage_uses_payload_endpoint_when_allowlist_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OSIMFLOW_ALLOWED_RESULT_BUCKETS", raising=False)
        # Use the local /dev/null endpoint with allow_insecure so
        # build_result_storage doesn't try to validate https.
        payload = _payload(
            result_storage_bucket="solo-bucket",
            extra={
                "result_storage_endpoint": "http://localhost:9000",
            },
            allow_insecure_storage_endpoint=True,
        )
        app = create_app(outdir=tmp_path, read_only=False)
        client = TestClient(app)
        resp = client.post(
            HANDOFF_URL,
            json=payload,
            headers={IDEMPOTENCY_KEY_HEADER: "k-payload-endpoint"},
        )
        assert resp.status_code == 202
        # _storage_from_campaign should accept the payload-supplied endpoint
        # because the allowlist is unset. Build a storage to confirm the
        # module wired through the payload values, not the (unset)
        # server-policy values.
        rec = next(iter(coord._campaigns.values()))
        storage = coord._storage_from_campaign(rec)
        # boto3 / azure / gcs clients may or may not succeed building against
        # localhost; the key contract here is that the function didn't return
        # None just because of an allowlist mismatch.
        if storage is not None:
            # Local backend in this test path is "s3" (the default) — verify
            # the endpoint made it through.
            endpoint = getattr(storage, "_endpoint_url", None) or getattr(
                storage, "endpoint_url", None
            )
            assert endpoint == "http://localhost:9000"

    def test_storage_ignores_payload_endpoint_when_allowlist_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OSIMFLOW_ALLOWED_RESULT_BUCKETS", "ops-bucket")
        monkeypatch.setenv(
            "OSIMFLOW_RESULT_STORAGE_ENDPOINT", "https://operator-controlled.example"
        )
        monkeypatch.setenv("OSIMFLOW_ALLOW_INSECURE_STORAGE_ENDPOINT", "0")
        app = create_app(outdir=tmp_path, read_only=False)
        client = TestClient(app)
        # Handoff supplies a different endpoint and allow_insecure=true;
        # both MUST be overridden by the operator env vars when the
        # allowlist is set.
        resp = client.post(
            HANDOFF_URL,
            json=_payload(
                result_storage_bucket="ops-bucket",
                extra={"result_storage_endpoint": "http://attacker-bucket.example"},
                allow_insecure_storage_endpoint=True,
            ),
            headers={IDEMPOTENCY_KEY_HEADER: "k-policy-wins"},
        )
        assert resp.status_code == 202
        rec = next(iter(coord._campaigns.values()))
        storage = coord._storage_from_campaign(rec)
        # Confirm the server-policy endpoint replaced the payload one (the
        # boto3 client property name varies by backend version).
        if storage is not None:
            endpoint = getattr(storage, "_endpoint_url", None) or getattr(
                storage, "endpoint_url", None
            )
            assert endpoint == "https://operator-controlled.example"

    def test_storage_returns_none_when_bucket_later_removed_from_allowlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defensive: a record that names a bucket the operator subsequently
        removed from the allowlist cannot be served by the storage helper."""
        monkeypatch.setenv("OSIMFLOW_ALLOWED_RESULT_BUCKETS", "ops-bucket")
        app = create_app(outdir=tmp_path, read_only=False)
        client = TestClient(app)
        resp = client.post(
            HANDOFF_URL,
            json=_payload(result_storage_bucket="ops-bucket"),
            headers={IDEMPOTENCY_KEY_HEADER: "k-then-tightened"},
        )
        assert resp.status_code == 202
        rec = next(iter(coord._campaigns.values()))
        # Operator tightens the allowlist after handoff: drop the bucket.
        coord.set_coordinator_server_endpoint_policy(
            allowed_result_buckets=frozenset({"some-other-bucket"}),
            result_storage_endpoint=None,
            allow_insecure_storage_endpoint=False,
        )
        assert coord._storage_from_campaign(rec) is None


class TestStartupWarning:
    """``create_app`` emits a SECURITY WARNING when the allowlist is unset."""

    def test_warning_logged_when_allowlist_unset(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        import os

        os.environ.pop("OSIMFLOW_ALLOWED_RESULT_BUCKETS", None)
        with caplog.at_level(logging.WARNING, logger="osimflow.api.app"):
            create_app(outdir=tmp_path, read_only=False)
        # Issue #1670
        assert any("OSIMFLOW_ALLOWED_RESULT_BUCKETS" in record.message for record in caplog.records)

    def test_no_warning_when_allowlist_set(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging
        import os

        os.environ["OSIMFLOW_ALLOWED_RESULT_BUCKETS"] = "team-a"
        try:
            with caplog.at_level(logging.WARNING, logger="osimflow.api.app"):
                create_app(outdir=tmp_path, read_only=False)
            assert not any(
                "OSIMFLOW_ALLOWED_RESULT_BUCKETS" in record.message for record in caplog.records
            )
        finally:
            os.environ.pop("OSIMFLOW_ALLOWED_RESULT_BUCKETS", None)
