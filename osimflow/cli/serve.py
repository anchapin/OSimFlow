"""``serve`` and ``dashboard`` subcommands."""

import argparse
import logging
import sys
import threading
import warnings
from pathlib import Path

from osimflow import (
    AWSBatchExecutor,  # noqa: F401 — re-exported for the historical ``osimflow.__main__.<X>Executor`` patch seam (pre-#1681).
    AzureBatchExecutor,  # noqa: F401 — see above.
    DaskJobQueueExecutor,  # noqa: F401 — see above.
    DockerSwarmExecutor,  # noqa: F401 — see above.
    GoogleBatchExecutor,  # noqa: F401 — see above.
    KubernetesExecutor,  # noqa: F401 — see above.
    NomadExecutor,  # noqa: F401 — see above.
    PBSExecutor,  # noqa: F401 — see above.
    SlurmExecutor,  # noqa: F401 — see above.
)

log = logging.getLogger("osimflow.__main__")


def _is_local_host(host: str) -> bool:
    """True when *host* binds only to the local machine (issue #1113).

    Loopback binds are exempt from the cleartext-traffic warning because
    that traffic never leaves the host. ``*`` / ``empty`` (all
    interfaces) are treated as non-local.
    """
    normalized = (host or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"localhost", "::1", "[::1]", "0:0:0:0:0:0:0:1"}:
        return True
    return normalized.startswith("127.")


def _warn_if_auth_disabled_nonlocal(
    api_key: str | None,
    api_keys_file: Path | None,
    host: str,
    port: int,
) -> None:
    """Warn loudly when the serve bind is network-accessible with no auth (issue #1095).

    ``APIKeyMiddleware`` passes every request through when no key store is
    configured (``api_key is None and api_keys_file is None``), so all
    endpoints — campaign data, file downloads, time-series queries — are
    reachable without credentials. Loopback binds are exempt because that
    traffic never leaves the host; ``0.0.0.0`` / ``*`` / empty are not.
    """
    auth_enabled = api_key is not None or api_keys_file is not None
    if auth_enabled or _is_local_host(host):
        return
    warnings.warn(
        f"Authentication is DISABLED on non-local bind {host}:{port}: "
        "every endpoint (campaign data, file downloads, time-series "
        "queries) is reachable without credentials. Pass --api-key or "
        "--api-keys-file to require authentication (issue #1095).",
        UserWarning,
        stacklevel=2,
    )
    log.warning(
        "SEC-001: auth disabled for non-local bind %s:%d — all endpoints "
        "are publicly reachable without credentials (issue #1095). "
        "Pass --api-key or --api-keys-file.",
        host,
        port,
    )


def _warn_if_cleartext_nonlocal(tls_cert: Path | None, host: str, port: int) -> None:
    """Warn loudly when a network-accessible serve bind lacks TLS (SEC-004).

    Plain HTTP on a non-local interface transmits everything — including
    API keys — in cleartext. TLS stays opt-in for backwards compatibility
    with local tooling, but non-local binds without it are almost
    certainly a misconfiguration (issue #1113).
    """
    if tls_cert is not None or _is_local_host(host):
        return
    warnings.warn(
        f"TLS is DISABLED for non-local bind {host}:{port}: "
        "traffic (including API keys) is transmitted in cleartext and can "
        "be intercepted (SEC-004). Enable TLS with --tls-cert/--tls-key.",
        UserWarning,
        stacklevel=2,
    )
    log.warning("SEC-004: TLS disabled for non-local bind %s:%d — traffic is cleartext", host, port)


def _cmd_serve(args: argparse.Namespace) -> int:  # noqa: PLR0911, PLR0912
    """Start the REST API server."""
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError:
        print(
            "Error: osimflow[api] extra required. Install with: pip install osimflow[api]",
            file=sys.stderr,
        )
        return 1

    from osimflow.api import create_app, generate_api_key  # noqa: PLC0415

    # Resolve the API key.
    api_key = args.api_key
    api_keys_file = args.api_keys_file

    # When using multi-user mode, don't auto-generate a key
    if api_keys_file is not None:
        if api_key is not None:
            log.warning("--api-key is ignored when --api-keys-file is set")
        api_key = None
    elif api_key is None:
        # No key store configured at all — close the SEC-001 localhost gap
        # (issue #1553): previously a read-only ``osimflow serve`` on
        # 127.0.0.1 ran fully unauthenticated, so every local account on a
        # shared host could read ``run.json`` / KPI results / registry
        # listings. Reuse the #1117 generation mechanism (same
        # ``generate_api_key()`` call the writes path already uses) and
        # surface the key once on stderr so the operator can capture it.
        # Failure to generate must abort startup — never serve open.
        try:
            api_key = generate_api_key()
        except Exception as exc:  # noqa: BLE001 — fail closed, never serve open
            print(
                f"Error: failed to auto-generate API key for serve ({exc}). "
                "Refusing to start an unauthenticated serve — pass --api-key "
                "explicitly or fix the underlying key-generation failure.",
                file=sys.stderr,
            )
            return 1
        # Single-line stderr notice so the operator can capture it
        # programmatically (issue #1117) without polluting stdout (which
        # is reserved for uvicorn access logs when piped). Distinct from
        # the legacy --enable-writes banner — this message is intentionally
        # terse and always prints because every unauthenticated serve is a
        # bug, not a normal write-mode feature.
        print(
            f"Generated ephemeral API key for localhost serve: {api_key} — "
            "pass --api-key <key> to pin it on subsequent serves.",
            file=sys.stderr,
        )
        log.debug("API key auto-generated (issue #1553 SEC-001 localhost gap)")
        # Multi-user host warning (issue #1553): on a shared HPC login
        # node or multi-tenant host every local account can reach
        # 127.0.0.1:<port>; an auto-generated key is the *minimum*
        # mitigation, not a substitute for host-level isolation. Always
        # emit at WARNING when auto-gen fires so the message is visible
        # regardless of log_level (default INFO already surfaces it).
        log.warning(
            "SEC-001: serve bound on %s:%d with an auto-generated API key. "
            "On multi-user hosts every local account can reach the loopback "
            "interface — restrict with --host 127.0.0.1 (already set), "
            "tighter firewall rules, or stop other local users from "
            "running on the same login node. The key is ephemeral and "
            "shown once above; pin it with --api-key <key> on the next "
            "serve. (issue #1553)",
            args.host,
            args.port,
        )

    # Parse CORS origins.
    cors_origins: list[str] | None = None
    if args.cors_origins:
        cors_origins = [o.strip() for o in args.cors_origins.split(",") if o.strip()]

    read_only = not args.enable_writes and args.read_only
    app = create_app(
        outdir=args.outdir,
        read_only=read_only,
        api_key=api_key,
        api_keys_file=api_keys_file,
        allow_insecure_api_keys_file=args.allow_insecure_api_keys_file,
        cors_origins=cors_origins,
        rate_limit=args.rate_limit,
        rate_limit_key=args.rate_limit_key,
        trust_x_forwarded_for=args.rate_limit_trust_x_forwarded_for,
        trusted_proxies=(
            [p.strip() for p in args.trusted_proxies.split(",") if p.strip()]
            if args.trusted_proxies
            else None
        ),
        ui_enabled=args.ui,
        variable_editor=args.editor,
        results_viewer=args.dashboard,
        dashboard=args.dashboard,
        registry_path=args.registry,
        redis_url=args.api_redis_url,
    )
    if args.host not in ("127.0.0.1", "localhost"):
        log.warning("Binding to %s — the API is now network-accessible.", args.host)
        # SEC-001: require auth on non-local binds (issue #1319)  # noqa: PLR0912
        if not api_key and not api_keys_file:
            print(
                "ERROR: SEC-001 — authentication required on non-localhost bind.\n"
                "Pass --api-key (or --api-keys-file) to enable authentication.\n"
                "See issue #1319.",
                file=sys.stderr,
            )
            return 1

    tls_cert: Path | None = args.tls_cert if args.tls_cert else None
    tls_key: Path | None = args.tls_key if args.tls_key else None

    if tls_cert is not None and tls_key is None:
        print("Error: --tls-key is required when --tls-cert is provided.", file=sys.stderr)
        return 1
    if tls_key is not None and tls_cert is None:
        print("Error: --tls-cert is required when --tls-key is provided.", file=sys.stderr)
        return 1

    # SEC-004: warn loudly on cleartext non-local binds (issue #1113).
    _warn_if_cleartext_nonlocal(tls_cert, args.host, args.port)

    # SEC-001: warn loudly when a non-local bind has no authentication at
    # all — every endpoint is reachable without credentials (issue #1095).
    _warn_if_auth_disabled_nonlocal(api_key, api_keys_file, args.host, args.port)

    # Launch Streamlit dashboard in a background thread if --dashboard is set.
    dashboard_thread: threading.Thread | None = None
    if args.dashboard:
        try:
            from osimflow.viz.dashboard import create_dashboard_app  # noqa: PLC0415
        except ImportError:
            print(
                "Error: osimflow[viz] extra required for --dashboard. "
                "Install with: pip install osimflow[viz]",
                file=sys.stderr,
            )
            return 1

        def run_dashboard() -> None:
            create_dashboard_app(outdir=args.outdir.resolve(), port=8501)

        dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
        dashboard_thread.start()
        log.info("Streamlit dashboard started on port 8501")

    if tls_cert is not None and tls_key is not None:
        log.warning("TLS enabled: serving on https://%s:%d", args.host, args.port)
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            ssl_certfile=tls_cert,
            ssl_keyfile=tls_key,
        )
    else:
        uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Launch the local ephemeral Streamlit dashboard."""
    outdir: Path = args.outdir.resolve()
    if not outdir.is_dir():
        print(f"error: {outdir} is not a directory", file=sys.stderr)
        return 1

    try:
        from osimflow.viz.dashboard import create_dashboard_app  # noqa: PLC0415
    except ImportError:
        print(
            "Error: osimflow[viz] extra required. Install with: pip install osimflow[viz]",
            file=sys.stderr,
        )
        return 1

    create_dashboard_app(outdir=outdir, port=args.port)
    return 0
