"""Nomad executor configuration + CLI flags (issue #1575).

Owns the ``NomadConfig`` dataclass and the ``--nomad-*`` flags that
``osimflow run``/``osimflow warm-cache`` register for the ``nomad``
executor. Registered as the ``nomad`` argument hook by
``osimflow/executor_configs/__init__.py``.
"""

import argparse
import dataclasses
from pathlib import Path


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
