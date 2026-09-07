"""HMAC-SHA256 signing and verification for remote task payloads (issue #1177).

``OSIMFLOW_TASK_PAYLOAD`` travels through Nomad/Kubernetes job
environment variables (or Nomad dispatch meta) in plain sight: any
process that can read or modify the job env can inject arbitrary step
calls into ``osimflow.remote_runner``. This module provides the shared
secret HMAC-SHA256 contract that closes that hole:

* the **submission side** (``KubernetesExecutor`` / ``NomadExecutor``)
  computes a signature over the exact payload string bytes and ships it
  next to the payload in ``OSIMFLOW_TASK_PAYLOAD_SIG``, propagating the
  shared secret via ``OSIMFLOW_TASK_PAYLOAD_SECRET`` so the remote
  worker can verify;
* the **verification side** (``osimflow.remote_runner``) re-computes
  the digest with ``hmac.compare_digest`` *before* decoding or
  executing anything, and fails closed on missing/tampered signatures.

The module is stdlib-only (``hmac`` + ``hashlib``) so the remote runner
keeps its no-extra-dependencies property. For maximum benefit on real
clusters, inject ``OSIMFLOW_TASK_PAYLOAD_SECRET`` into worker pods via
the substrate's secret store (Kubernetes Secret / Nomad Vault template)
rather than a literal orchestrator environment variable.
"""

import hashlib
import hmac
import json
import os
from typing import Any

#: Serialized step call carried in the job environment.
TASK_PAYLOAD_ENV = "OSIMFLOW_TASK_PAYLOAD"
#: Hex HMAC-SHA256 digest over the exact ``TASK_PAYLOAD_ENV`` string bytes.
TASK_PAYLOAD_SIG_ENV = "OSIMFLOW_TASK_PAYLOAD_SIG"
#: Shared secret used for signing (submission) and verification (runner).
TASK_PAYLOAD_SECRET_ENV = "OSIMFLOW_TASK_PAYLOAD_SECRET"

#: Nomad dispatch-meta key mirroring ``TASK_PAYLOAD_ENV``.
TASK_PAYLOAD_META_KEY = "task_payload"
#: Nomad dispatch-meta key mirroring ``TASK_PAYLOAD_SIG_ENV``.
TASK_PAYLOAD_SIG_META_KEY = "task_payload_sig"
#: Nomad dispatch-meta key mirroring ``TASK_PAYLOAD_SECRET_ENV``.
TASK_PAYLOAD_SECRET_META_KEY = "task_payload_secret"

#: Second HMAC over the canonical result-transport settings (issue #1549).
#: Guards the ``OSIMFLOW_RESULT_*`` job-environment values the remote
#: runner consumes — without it an attacker who cannot forge the payload
#: signature can still rewrite the result endpoint to an attacker-controlled
#: host and have the runner upload all campaign artifacts there, or set the
#: allow-insecure flag to downgrade the https enforcement added in #1386.
RESULT_TRANSPORT_SIG_ENV = "OSIMFLOW_RESULT_TRANSPORT_SIG"
#: Nomad dispatch-meta key mirroring ``RESULT_TRANSPORT_SIG_ENV``.
RESULT_TRANSPORT_SIG_META_KEY = "result_transport_sig"

#: The result-transport settings covered by the second HMAC, in canonical
#: (sorted) order.  ``allow_insecure`` mirrors the worker-side read of
#: ``OSIMFLOW_ALLOW_INSECURE_STORAGE_ENDPOINT`` (issue #1386).
RESULT_TRANSPORT_SIGNED_FIELDS: tuple[str, ...] = (
    "allow_insecure",
    "backend",
    "bucket",
    "endpoint",
    "mode",
    "prefix",
)


def resolve_payload_secret() -> str | None:
    """Return the configured shared secret, if any.

    Checks ``OSIMFLOW_TASK_PAYLOAD_SECRET`` first, then the Nomad
    dispatch-meta fallback (``NOMAD_META_task_payload_secret``) — the
    same env-then-meta resolution order ``remote_runner`` uses for the
    payload itself. Returns ``None`` in legacy (unsigned) mode.
    """
    value = os.environ.get(TASK_PAYLOAD_SECRET_ENV)
    if value is not None:
        return value
    return os.environ.get(f"NOMAD_META_{TASK_PAYLOAD_SECRET_META_KEY}")


def sign_task_payload(payload: str, secret: str) -> str:
    """Compute the hex HMAC-SHA256 digest over the exact payload bytes.

    The signature is computed over the payload *string* as submitted
    (UTF-8 encoded) — not over a re-serialized variant — so the runner
    can verify against the raw env value byte-for-byte.
    """
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_task_payload(payload: str, signature: str | None, secret: str) -> bool:
    """Return True iff *signature* is a valid HMAC for *payload*.

    Uses ``hmac.compare_digest`` (constant-time comparison) and fails
    closed on a missing/empty signature.
    """
    if not signature:
        return False
    expected = sign_task_payload(payload, secret)
    return hmac.compare_digest(expected, signature)


def build_signature_env(payload: str, *, secret: str | None = None) -> dict[str, str]:
    """Return the env vars to attach alongside ``OSIMFLOW_TASK_PAYLOAD``.

    When a shared secret is configured (explicit *secret* or the
    ``OSIMFLOW_TASK_PAYLOAD_SECRET`` environment variable), returns both
    ``OSIMFLOW_TASK_PAYLOAD_SIG`` (the signature) and
    ``OSIMFLOW_TASK_PAYLOAD_SECRET`` (so the remote worker can verify).
    Returns an empty dict in legacy unsigned mode so executor env
    builders stay byte-identical when no secret is configured.
    """
    resolved = secret if secret is not None else os.environ.get(TASK_PAYLOAD_SECRET_ENV)
    if not resolved:
        return {}
    return {
        TASK_PAYLOAD_SIG_ENV: sign_task_payload(payload, resolved),
        TASK_PAYLOAD_SECRET_ENV: resolved,
    }


def canonical_result_transport_settings(
    *,
    mode: str | None,
    backend: str | None,
    bucket: str | None,
    prefix: str | None,
    endpoint: str | None,
    allow_insecure: bool,
) -> str:
    """Canonical JSON serialization of the result-transport settings (issue #1549).

    Deterministic across orchestrator and worker: sorted keys, compact
    separators, ``None`` preserved as JSON ``null``, booleans as JSON
    literals.  Both sides canonicalize the *coerced* transport mode, so
    ``coerce_transport_mode`` must run before this call (the executors
    sign ``transport.mode`` which is already coerced; the runner coerces
    the raw env value first).
    """
    return json.dumps(
        {
            "allow_insecure": bool(allow_insecure),
            "backend": backend,
            "bucket": bucket,
            "endpoint": endpoint,
            "mode": mode,
            "prefix": prefix,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def build_transport_signature_env(
    transport: Any,  # noqa: ANN401 — ResultTransportConfig (duck-typed; avoids a circular import)
    *,
    secret: str | None = None,
    allow_insecure: bool = False,
) -> dict[str, str]:
    """Return the env vars guarding the ``OSIMFLOW_RESULT_*`` settings (issue #1549).

    Returns ``{RESULT_TRANSPORT_SIG_ENV: <hex hmac>}`` when a shared
    secret is configured (explicit *secret* or the
    ``OSIMFLOW_TASK_PAYLOAD_SECRET`` environment variable), covering the
    canonical serialization of *transport*'s five result fields plus the
    *allow_insecure* escape-hatch flag.  Returns an empty dict in legacy
    unsigned mode so executor env builders stay byte-identical when no
    secret is configured.
    """
    resolved = secret if secret is not None else os.environ.get(TASK_PAYLOAD_SECRET_ENV)
    if not resolved:
        return {}
    canonical = canonical_result_transport_settings(
        mode=getattr(transport, "mode", None),
        backend=getattr(transport, "backend", None),
        bucket=getattr(transport, "bucket", None),
        prefix=getattr(transport, "prefix", None),
        endpoint=getattr(transport, "endpoint", None),
        allow_insecure=allow_insecure,
    )
    return {RESULT_TRANSPORT_SIG_ENV: sign_task_payload(canonical, resolved)}
