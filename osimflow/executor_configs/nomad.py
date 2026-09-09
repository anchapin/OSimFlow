"""Nomad executor configuration + CLI flags (issue #1575).

Owns the ``NomadConfig`` dataclass and the ``--nomad-*`` flags that
``osimflow run``/``osimflow warm-cache`` register for the ``nomad``
executor. Registered as the ``nomad`` argument hook by
``osimflow/executor_configs/__init__.py``.
"""

import argparse
import dataclasses
from pathlib import Path
from typing import Any


@dataclasses.dataclass(frozen=True)
class NomadConfig:
    """Nomad executor configuration.

    Attributes
    ----------
    dispatch_policy
        Dispatch policy for job submission.
    allocation_resolution_timeout_s
        Timeout for allocation ID resolution.
    poll_interval_s
        Polling interval for allocation status.
    max_poll_interval_s
        Maximum polling interval (exponential backoff cap).
    fanout_submit_rate_per_sec
        Rate limit for fan-out submissions (jobs per second).
    fanout_submit_chunk_size
        Chunk size for fan-out submissions.
    tls
        Whether to use TLS for Nomad connection.
    cert
        Path to client certificate file.
    key
        Path to client key file.
    ca_cert
        Path to CA certificate file.
    allow_insecure_token
        Explicit opt-out allowing the Nomad ACL token to transit without
        TLS to a non-local address (issue #1450; dev/test only).
    """

    dispatch_policy: str = "keep_manual"
    allocation_resolution_timeout_s: float = 30.0
    poll_interval_s: float = 5.0
    max_poll_interval_s: float = 60.0
    fanout_submit_rate_per_sec: float | None = None
    fanout_submit_chunk_size: int = 0
    tls: bool = False
    cert: Path | None = None
    key: Path | None = None
    ca_cert: Path | None = None
    allow_insecure_token: bool = False


def add_arguments(parser_group: argparse.ArgumentParser) -> None:
    """Register the ``nomad`` executor's ``run`` flags (issue #1575)."""
    parser_group.add_argument(
        "--nomad-address",
        default=None,
        help=(
            "Nomad cluster HTTP address (e.g. http://nomad.local:4646). "
            "Defaults to the NOMAD_ADDR env var or http://127.0.0.1:4646."
        ),
    )
    parser_group.add_argument(
        "--nomad-datacentre",
        default="dc1",
        help="Nomad datacentre to target (default: dc1).",
    )
    parser_group.add_argument(
        "--nomad-dispatch-policy",
        choices=[
            "keep_manual",
            "force_dispatch",
            "auto_prefer_dispatch",
            "direct",
            "dispatch",
            "auto",
        ],
        default="keep_manual",
        help=(
            "Nomad submission policy. Preferred values: "
            "'keep_manual' (default, no auto-switching), "
            "'force_dispatch' (always dispatch), "
            "'auto_prefer_dispatch' (auto-switch to dispatch for large runs). "
            "Legacy aliases are accepted for compatibility: "
            "'direct'->'keep_manual', 'dispatch'->'force_dispatch', 'auto'->'auto_prefer_dispatch'."
        ),
    )
    parser_group.add_argument(
        "--nomad-allocation-resolution-timeout-s",
        type=float,
        default=30.0,
        help=("Timeout in seconds to resolve Nomad EvalID to Allocation ID (default: 30.0)."),
    )
    parser_group.add_argument(
        "--nomad-poll-interval-s",
        type=float,
        default=5.0,
        help="Initial Nomad allocation polling interval in seconds (default: 5.0).",
    )
    parser_group.add_argument(
        "--nomad-max-poll-interval-s",
        type=float,
        default=60.0,
        help="Maximum Nomad allocation polling interval in seconds (default: 60.0).",
    )
    parser_group.add_argument(
        "--nomad-fanout-submit-rate-per-sec",
        type=float,
        default=None,
        help=(
            "Optional fan-out submit rate limit for Nomad (submissions/sec). "
            "Used by fan-out steps to pace submission and reduce coordinator pressure."
        ),
    )
    parser_group.add_argument(
        "--nomad-fanout-submit-chunk-size",
        type=int,
        default=0,
        help=(
            "Optional fan-out submit chunk size for Nomad (0 disables chunking). "
            "When >0, fan-out submits at most this many tasks per chunk."
        ),
    )
    parser_group.add_argument(
        "--nomad-tls-verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Enable (default) or disable TLS certificate verification for the "
            "Nomad HTTP API. Disable with --nomad-tls-verify=false for "
            "development with self-signed certificates. "
            "SEC-009: protects NOMAD_TOKEN from interception."
        ),
    )
    parser_group.add_argument(
        "--nomad-tls",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Enable TLS for the Nomad HTTP API connection. "
            "When enabled, use --nomad-cert, --nomad-key, and --nomad-ca-cert "
            "to specify client certificate files for mTLS authentication."
        ),
    )
    parser_group.add_argument(
        "--nomad-allow-insecure-token",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Allow NOMAD_TOKEN to be transmitted without TLS to a non-local "
            "Nomad address. Fails closed by default (SEC-009, issue #1450): "
            "without this flag, osimflow run --executor nomad raises when a "
            "token is configured for a non-local address without TLS. "
            "Dev/test only — mirrors --allow-insecure-storage-endpoint "
            "(issue #1386)."
        ),
    )
    parser_group.add_argument(
        "--nomad-vault-secret-path",
        default=None,
        help=(
            "Vault KV path holding the task-payload HMAC secret (issues "
            "#1449/#1535). When set, the Nomad client renders "
            "OSIMFLOW_TASK_PAYLOAD_SECRET from Vault at allocation time "
            "via a template stanza — the raw secret never appears in the "
            "job spec or dispatch meta (where it would persist in the "
            "Nomad state store, readable via `nomad job inspect`). "
            "Requires OSIMFLOW_TASK_PAYLOAD_SECRET on the orchestrator "
            "with the same value for signing."
        ),
    )
    parser_group.add_argument(
        "--nomad-vault-secret-key",
        default="payload_secret",
        help=(
            "Field name inside the Vault KV entry at "
            "--nomad-vault-secret-path holding the HMAC secret "
            "(default: payload_secret). KV v2 paths (containing /data/) "
            "read the field from the wrapped Data.data object "
            "(issues #1449/#1535)."
        ),
    )
    parser_group.add_argument(
        "--nomad-cert",
        default=None,
        help=(
            "Path to the client certificate file (PEM) for mTLS authentication "
            "with the Nomad cluster. Required when --nomad-tls is enabled."
        ),
    )
    parser_group.add_argument(
        "--nomad-key",
        default=None,
        help=(
            "Path to the client private key file (PEM) for mTLS authentication "
            "with the Nomad cluster. Required when --nomad-tls is enabled."
        ),
    )
    parser_group.add_argument(
        "--nomad-ca-cert",
        default=None,
        help=(
            "Path to the CA certificate file (PEM) to verify the Nomad server's "
            "certificate when --nomad-tls is enabled. If not specified, the "
            "system default CA certificates are used."
        ),
    )
    parser_group.add_argument(
        "--nomad-remote-results-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "DEPRECATED compatibility toggle. True (default) keeps Nomad in remote-results mode. "
            "Set --no-nomad-remote-results-only only for temporary migration compatibility; "
            "the legacy local-callable mode is scheduled for removal after one minor release."
        ),
    )
    parser_group.add_argument(
        "--nomad-dispatch-job-id",
        default=None,
        help=(
            "Override the Nomad dispatch job ID in dispatch mode (issue #1316). "
            "When not set, the executor derives a unique ID from the campaign outdir hash. "
            "Setting this is only needed when multiple campaigns must share the same job ID "
            "(e.g., to leverage a pre-registered job spec)."
        ),
    )


def _coerce_path(value: str | Path | None) -> Path | None:
    """Normalise CLI/API path strings to ``pathlib.Path`` (issue #1681).

    The pre-#1681 API mirror wrapped ``body.nomad_cert`` and friends
    in :class:`Path`; the CLI passes them as plain ``str`` (argparse's
    default). ``NomadExecutor`` stores the value on the instance and
    downstream code compares against a :class:`Path` in tests — so
    the shared factory normalises to ``Path`` once, regardless of
    which surface originated the call.
    """
    if value is None or value == "":
        return None
    return value if isinstance(value, Path) else Path(value)


def _dispatch_job_id_from_outdir(outdir: str | Path | None) -> str | None:
    """Mirror ``osimflow.__main__._build_executor``'s per-campaign dispatch id (issue #1316)."""
    if not outdir:
        return None
    return f"osimflow-worker-{abs(hash(str(outdir)))}"


def kwargs_for_executor(**kwargs: Any) -> dict[str, Any]:
    """Translate the flat CLI / API kwargs into ``NomadExecutor`` kwargs (issue #1681).

    Preserves the CLI's per-campaign dispatch-job-id derivation from
    the campaign outdir hash (issue #1316) so multiple concurrent
    campaigns on the same Nomad cluster never overwrite each other's
    parameterized job spec. API callers can override the id explicitly
    via ``--nomad-dispatch-job-id`` (the matching ``CampaignCreateRequest``
    field is not yet surfaced; the CLI flag remains the override path).

    Defaults mirror the ``add_arguments`` registration above so the
    factory can be called without a fully-populated argparse
    Namespace (e.g. the contract test, or a future REST surface that
    lets users opt out of every Nomad-specific knob).
    """
    dispatch_job_id = kwargs.get("nomad_dispatch_job_id") or _dispatch_job_id_from_outdir(
        kwargs.get("outdir")
    )
    nomad_rps = kwargs.get("submit_rps")
    if nomad_rps is None:
        nomad_rps = kwargs.get("nomad_fanout_submit_rate_per_sec")
    poll_interval_s = kwargs.get("nomad_poll_interval_s")
    if poll_interval_s is None:
        poll_interval_s = 5.0
    max_poll_interval_s = kwargs.get("nomad_max_poll_interval_s")
    if max_poll_interval_s is None:
        max_poll_interval_s = 60.0
    allocation_resolution_timeout_s = kwargs.get("nomad_allocation_resolution_timeout_s")
    if allocation_resolution_timeout_s is None:
        allocation_resolution_timeout_s = 30.0
    return {
        "address": kwargs.get("nomad_address"),
        "datacentre": kwargs.get("nomad_datacentre") or "dc1",
        "dispatch_policy": kwargs.get("nomad_dispatch_policy"),
        "estimated_run_size": (
            int(kwargs["n_samples"]) if kwargs.get("n_samples") is not None else None
        ),
        "fanout_submit_chunk_size": int(kwargs.get("nomad_fanout_submit_chunk_size", 0) or 0),
        "allocation_resolution_timeout_s": allocation_resolution_timeout_s,
        "poll_interval_s": poll_interval_s,
        "max_poll_interval_s": max_poll_interval_s,
        "remote_results_only": kwargs.get("nomad_remote_results_only", True),
        "verify_tls": bool(kwargs.get("nomad_tls_verify", True)),
        "tls": bool(kwargs.get("nomad_tls", False)),
        "cert": _coerce_path(kwargs.get("nomad_cert")),
        "key": _coerce_path(kwargs.get("nomad_key")),
        "ca_cert": _coerce_path(kwargs.get("nomad_ca_cert")),
        "dispatch_job_id": dispatch_job_id,
        "allow_insecure_token": bool(kwargs.get("nomad_allow_insecure_token", False)),
        "submit_rps": nomad_rps,
        "vault_secret_path": kwargs.get("nomad_vault_secret_path"),
        "vault_secret_key": kwargs.get("nomad_vault_secret_key") or "payload_secret",
    }
