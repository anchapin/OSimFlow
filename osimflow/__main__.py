"""CLI entry point for OSimFlow.

Usage:
    python -m osimflow run \\
        --executor local \\
        --input_variables variables.yml \\
        --template_sim_package ./example_package \\
        --n_samples 10 \\
        --outdir ./results \\
        --openstudio_version 3.10.0

After `pip install -e .`, also available as:
    osimflow run --executor local ...
"""

import argparse
import json
import logging
import sys
import threading  # noqa: F401
import time
import warnings
from pathlib import Path
from typing import Any

import httpx
import yaml

from osimflow import (
    AWSBatchExecutor,  # noqa: F401 — re-exported for the historical ``osimflow.__main__.<X>Executor`` patch seam (pre-#1681).
    AzureBatchExecutor,  # noqa: F401 — see above.
    BaseExecutor,
    Campaign,
    CampaignConfig,
    CampaignRecord,  # noqa: F401
    CampaignRegistry,  # noqa: F401
    DaskJobQueueExecutor,  # noqa: F401 — see above.
    DockerSwarmExecutor,  # noqa: F401 — see above.
    GoogleBatchExecutor,  # noqa: F401 — see above.
    KubernetesExecutor,  # noqa: F401 — see above.
    LocalExecutor,
    NomadExecutor,  # noqa: F401 — see above.
    PBSExecutor,  # noqa: F401 — see above.
    SlurmExecutor,  # noqa: F401 — see above.
    build_task_queue,
    load_config,
)
from osimflow.audit import AuditLogger  # noqa: F401
from osimflow.byos import ByosTrustLevel, load_user_function, validate_trust_level
from osimflow.cli.coordinator import (  # noqa: F401
    _build_coordinator_payload,
    _coordinator_idempotency_key,
    _coordinator_status_url,
    _download_coordinator_results,
    _print_coordinator_status,
)
from osimflow.cli.executor import (  # noqa: F401
    _CLOUD_EXECUTORS,
    _build_executor,
    _byos_audit_logger,
    _warn_if_mutable_tag,
)
from osimflow.cli.io import (  # noqa: F401
    _run_export,
    _run_import_gem_osa,
    _run_import_osa,
)
from osimflow.cli.lifecycle import (  # noqa: F401
    _CAMPAIGN_INVOCATION_FILE,
    _cmd_cancel,
    _cmd_health,
    _cmd_mark_for_reanalysis,
    _cmd_merge,
    _cmd_pause,
    _cmd_run_batches,
    _persist_run_invocation,
    _replay_paused_campaign,
)
from osimflow.cli.parser import (  # noqa: F401
    PRESETS,
    _add_aggregate_runs_args,
    _add_backup_args,
    _add_cancel_args,
    _add_compare_args,
    _add_dashboard_args,
    _add_download_args,
    _add_export_args,
    _add_export_results_args,
    _add_from_s3_args,
    _add_health_args,
    _add_import_gem_osa_args,
    _add_import_osa_args,
    _add_list_args,
    _add_mark_for_reanalysis_args,
    _add_measure_args,
    _add_merge_args,
    _add_pause_args,
    _add_query_results_args,
    _add_restore_args,
    _add_resume_args,
    _add_run_args,
    _add_run_batches_args,
    _add_serve_args,
    _add_show_args,
    _add_status_args,
    _apply_preset,
    _build_parser,
    _extract_max_concurrent_samples,
    _get_arg_defaults,
    _PresetDefaultCache,
)
from osimflow.cli.registry import (  # noqa: F401
    _cmd_aggregate_runs,
    _cmd_backup,
    _cmd_compare,
    _cmd_compare_outdirs,
    _cmd_list,
    _cmd_restore,
    _cmd_show,
    _format_field,
)
from osimflow.cli.results import (  # noqa: F401
    _cmd_export_results,
    _cmd_measure_list,
    _cmd_query_results,
)
from osimflow.cli.s3 import (  # noqa: F401
    _cmd_s3,
    _detach_s3_already_submitted,
    _s3_store,
)
from osimflow.cli.serve import (  # noqa: F401
    _cmd_dashboard,
    _cmd_serve,
    _is_local_host,
    _warn_if_auth_disabled_nonlocal,
    _warn_if_cleartext_nonlocal,
)
from osimflow.cli.status import (  # noqa: F401
    _cmd_download,
    _cmd_status,
    _download_local_results,
)
from osimflow.cosign import DEFAULT_COSIGN_OIDC_ISSUER  # noqa: F401
from osimflow.cross_run_aggregator import CrossRunAggregator  # noqa: F401
from osimflow.executor_configs import add_executor_arguments  # noqa: F401
from osimflow.exporters.osa import OSAExporter  # noqa: F401
from osimflow.handoff_record import (
    IDEMPOTENCY_KEY_HEADER,
    HandoffRecord,
    NoHandoffRecordError,  # noqa: F401
    handoff_record_exists,
    read_handoff_record,
    write_handoff_record,
)
from osimflow.importers.osa import OSAImportError, osa_to_variables_yml, parse_osa  # noqa: F401
from osimflow.manifest import _validate_coordinator_url
from osimflow.offline_bundle import OfflineBundleError, verify_offline_bundle
from osimflow.validation import ValidationError

log = logging.getLogger("osimflow.__main__")


def _perform_detach_handoff(args: argparse.Namespace) -> int:  # noqa: PLR0911
    """Execute the ``osimflow run --detach`` fire-and-forget handoff.

    Posts the campaign config to the Coordinator with a deterministic
    ``Idempotency-Key``, writes a local handoff record under ``outdir``, and
    exits cleanly. Failure modes (Coordinator unreachable, 4xx config error,
    5xx server error) surface a clear, actionable message; 4xx leaves no
    remote state, while 5xx/idempotency lets the user safely re-run the same
    command to recover.
    """
    coordinator_url = (args.coordinator_url or "").rstrip("/")
    if not coordinator_url:
        print("error: --detach requires --coordinator-url", file=sys.stderr)
        return 1

    # Fail-closed transport check (issue #1550): a non-loopback http://
    # Coordinator would carry the campaign config (and any API key) in
    # cleartext.  Mirrors the storage-endpoint gate (#1386); the escape
    # hatch reuses --allow-insecure-storage-endpoint (no new flag).
    try:
        _validate_coordinator_url(
            coordinator_url,
            allow_insecure=bool(getattr(args, "allow_insecure_storage_endpoint", False)),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Idempotent fast path: this outdir is already associated with a remote
    # campaign. Return the existing campaign_id without loading the config or
    # hitting the network. (Checked before load_config so a stale/invalid
    # config doesn't block reconnecting to an already-handed-off campaign.)
    outdir_raw = getattr(args, "outdir", None)
    outdir: Path | None = Path(outdir_raw).resolve() if outdir_raw else None
    if outdir is not None and handoff_record_exists(outdir):
        rec = read_handoff_record(outdir)
        print(f"Campaign already handed off to Coordinator: {rec.campaign_id}")
        print(f"  Status URL: {rec.status_url}")
        print(
            "  (outdir already associated with a Coordinator campaign; "
            "use `osimflow status <outdir>` to monitor it.)"
        )
        return 0

    cfg = load_config(vars(args))
    outdir = cfg.outdir

    payload = _build_coordinator_payload(args, cfg)
    idem_key = _coordinator_idempotency_key(payload, outdir)
    handoff_url = f"{coordinator_url}/api/v1/coordinator/handoff"

    try:
        response = httpx.post(
            handoff_url,
            json=payload,
            headers={IDEMPOTENCY_KEY_HEADER: idem_key},
            timeout=30.0,
        )
    except httpx.RequestError as exc:
        # ConnectError is a subclass of RequestError; surface the more
        # actionable "is it running?" hint for connection failures.
        if isinstance(exc, httpx.ConnectError):
            detail = f"could not reach Coordinator at {coordinator_url}: {exc}"
            hint = "Is the Coordinator running and reachable?"
        else:
            detail = f"handoff request to {coordinator_url} failed: {exc}"
            hint = "Re-run the same command to retry."
        print(
            f"error: {detail}\n"
            f"       {hint} No campaign was created; the handoff is "
            "idempotent, so re-running the same command is safe.",
            file=sys.stderr,
        )
        return 1

    # 4xx: the Coordinator rejected the config before creating any state.
    if 400 <= response.status_code < 500:
        print(
            f"error: Coordinator rejected the handoff "
            f"(HTTP {response.status_code}).\n"
            f"       Response: {response.text}\n"
            "       Fix the campaign configuration and re-run. "
            "No campaign was created.",
            file=sys.stderr,
        )
        return 1

    # 5xx: server error — the campaign may or may not exist on the server.
    if response.status_code >= 500:
        print(
            f"error: Coordinator returned HTTP {response.status_code}: "
            f"{response.text}\n"
            "       The campaign may or may not have been created on the "
            "server.\n"
            "       Re-run the SAME command to recover — the Idempotency-Key "
            "reuses any\n"
            "       campaign already created for this configuration.",
            file=sys.stderr,
        )
        return 1

    # 2xx (202 Accepted expected) — success. Persist the handoff record so
    # ``osimflow status`` / ``osimflow download`` can reconnect later.
    result_data = response.json()
    campaign_id = str(result_data.get("campaign_id", ""))
    status_url = str(
        result_data.get("status_url") or _coordinator_status_url(coordinator_url, campaign_id)
    )
    if outdir is not None:
        write_handoff_record(
            outdir,
            HandoffRecord(
                campaign_id=campaign_id,
                coordinator_url=coordinator_url,
                submitted_at=time.time(),
                status_url=status_url,
                idempotency_key=idem_key,
            ),
        )

    print(f"Campaign handed off to Coordinator: {campaign_id}")
    print(f"  Status URL: {status_url}")
    print("  The CLI has exited; the campaign runs on the Coordinator.")
    if outdir is not None:
        print(f"  Monitor with: osimflow status {outdir}")
        print(f"  Download with: osimflow download {outdir}")
    else:
        print("  Monitor with: osimflow status <outdir>")
    return 0


def _cmd_warm_cache(args: argparse.Namespace) -> int:
    """Pre-populate the simulation cache before a campaign (issue #427)."""
    _apply_preset(args)
    cfg: CampaignConfig = load_config(vars(args))
    executor = _build_executor(args)
    task_queue = build_task_queue(cfg.task_queue, cfg.dask_scheduler_address)
    campaign = Campaign(
        cfg,
        executor,
        apply_fn=None,
        extract_fn=None,
        max_workers=args.max_workers,
        task_queue=task_queue,
    )
    result: Any = campaign.warm_cache(n_warm=args.n_warm)
    cache_stats: Any = result["cache_stats"]
    print(
        f"Cache warming complete: {result['n_samples']} samples warm, "
        f"{cache_stats['hits']} cache hits, {cache_stats['misses']} misses, "
        f"{cache_stats['total_keys']} total keys"
    )
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    """Resume a paused campaign by driving the real recovery (issue #1628).

    A paused campaign has no live orchestrator: when the pause lands,
    ``Campaign.run()`` writes the paused ``run.json`` and returns, and
    the ``osimflow run`` process exits (issue #1537). Clearing the
    ``.pause`` flag alone therefore continues nothing.

    This command:

    1. removes the ``.pause`` flag file so the next run is not born
       paused, and
    2. re-launches the campaign recorded in ``campaign_invocation.json``
       (written at ``osimflow run`` start). Completed steps replay from
       the cache and the campaign continues where it left off.

    When no invocation record exists (campaign started by an older
    version, via the REST API, or programmatically), the manual
    recovery is printed instead: ``osimflow run --outdir <same>``
    with the original flags.
    """
    outdir: Path = args.outdir.resolve()
    run_json_path = outdir / "run.json"

    if not run_json_path.exists():
        print(f"error: run.json not found in {outdir}", file=sys.stderr)
        return 1

    try:
        with open(run_json_path) as f:
            run_data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read run.json: {exc}", file=sys.stderr)
        return 1

    if run_data.get("status") != "paused":
        print(
            f"error: campaign '{run_data.get('campaign_id', outdir.name)}' "
            f"is not paused (status={run_data.get('status')})",
            file=sys.stderr,
        )
        return 1

    campaign_id = run_data.get("campaign_id", outdir.name)
    pause_file = outdir / ".pause"
    if pause_file.exists():
        pause_file.unlink()
        print(f"pause flag removed from {pause_file}")
    else:
        print(
            f"warning: no .pause flag file in {outdir} — the campaign is "
            "not flagged for pause; driving the recovery replay anyway",
            file=sys.stderr,
        )
    print(f"resume requested for campaign '{campaign_id}'")
    replay_rc = _replay_paused_campaign(outdir)
    if replay_rc is None:
        print("no recorded 'osimflow run' invocation found for this campaign.")
        print("Complete the recovery manually with cache replay (same outdir):")
        print(f"  osimflow run --outdir {outdir}   # plus the original flags")
        print("Completed steps are cache hits; only remaining work re-executes.")
        return 0
    return replay_rc


def _friendly_run_input_error(exc: Exception) -> str:
    """Translate a run-input loading failure into a one-line, flag-referencing
    error message (issue #1461).

    ``osimflow.config.load_config`` raises exceptions whose messages name the
    internal snake_case parameters (``variables_yml``, ``template_sim_package``).
    This maps them onto the user-facing CLI flags so the most common first-run
    mistakes surface as one actionable line instead of a raw traceback.
    Multi-line exception text (e.g. YAML parse errors carrying line/column
    context) is flattened to a single line.
    """
    message = " ".join(str(exc).split())

    if isinstance(exc, FileNotFoundError):
        for prefix, prefix_flag in (
            ("variables_yml not found: ", "--input_variables"),
            ("template_sim_package not found: ", "--template_sim_package"),
        ):
            if message.startswith(prefix):
                return f"{prefix_flag}: file not found: {message.removeprefix(prefix)}"
        return f"file not found: {message}"

    flag: str | None = None
    if isinstance(exc, ValidationError):
        field = exc.field
        if field == "template_sim_package":
            flag = "--template_sim_package"
        elif field in ("variables", None) or "variables.yml" in message:
            flag = "--input_variables"
    elif isinstance(exc, yaml.YAMLError):
        flag = "--input_variables"
    if flag is not None:
        return f"{flag}: {message}"
    return f"invalid configuration: {message}"


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912, PLR0915
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    dispatch = {
        "import-osa": _run_import_osa,
        "import-gem-osa": _run_import_gem_osa,
        "export": _run_export,
        "serve": _cmd_serve,
        "dashboard": _cmd_dashboard,
        "list": _cmd_list,
        "show": _cmd_show,
        "compare": _cmd_compare,
        "aggregate-runs": _cmd_aggregate_runs,
        "status": _cmd_status,
        "download": _cmd_download,
        "cancel": _cmd_cancel,
        "run-batches": _cmd_run_batches,
        "mark-for-reanalysis": _cmd_mark_for_reanalysis,
        "merge": _cmd_merge,
        "pause": _cmd_pause,
        "resume": _cmd_resume,
        "backup": _cmd_backup,
        "restore": _cmd_restore,
        "health": _cmd_health,
        "warm-cache": _cmd_warm_cache,
        "measure": _cmd_measure_list,
        "query-results": _cmd_query_results,
        "export-results": _cmd_export_results,
    }
    handler = dispatch.get(args.command)
    if handler is not None:
        return handler(args)
    if args.command != "run":
        return 1

    # Apply preset before load_config so preset values are in place.
    _apply_preset(args)

    # --- BYOS trust-level hardening (issues #908, #1207) -----------------
    # Reject --byos-trust-level inprocess when --require-trusted-scripts is
    # not explicitly set.  This is the production-hardening guard: it fails
    # fast before any config loading or executor construction so operators
    # get a clear error.
    try:
        validate_trust_level(
            ByosTrustLevel(args.byos_trust_level),
            getattr(args, "require_trusted_scripts", None),
            audit_logger=_byos_audit_logger(args),
            script_path=Path(args.custom_apply_script)
            if getattr(args, "custom_apply_script", None)
            else (
                Path(args.custom_kpi_extractor)
                if getattr(args, "custom_kpi_extractor", None)
                else None
            ),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Fire-and-forget handoff (Phase 2 — issue #602; hardened #630) ---
    # Idempotent POST with a deterministic Idempotency-Key, local handoff
    # record under outdir/, clean exit. See _perform_detach_handoff.
    if args.detach:
        return _perform_detach_handoff(args)

    if getattr(args, "detach_s3", False):
        if args.executor != "aws_batch":
            print("error: --detach-s3 requires --executor aws_batch", file=sys.stderr)
            return 2
        args.result_storage_backend = "s3"
        _s3_store(args, Path(args.outdir) / ".s3_scratch")
        if _detach_s3_already_submitted(args):
            return 0

    # Issue #1461: config/input-loading failures (missing --input_variables
    # file, missing --template_sim_package dir, malformed YAML) must surface
    # as a one-line, flag-referencing error — never a raw traceback. The
    # exception types raised by load_config stay untouched; they are
    # translated here at the CLI boundary only.
    try:
        cfg: CampaignConfig = load_config(vars(args))
    except (FileNotFoundError, ValidationError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {_friendly_run_input_error(exc)}", file=sys.stderr)
        print("See 'osimflow run --help' for usage.", file=sys.stderr)
        sys.exit(1)
    # --- Offline-bundle integrity gate (issue #1640) ----------------------
    # scripts/bundle_offline.py writes per-asset SHA-256 digests into the
    # bundle's bundle_manifest.json; they were previously generated but
    # never verified. The bundle is the USB/relay-transferred artifact of
    # air-gapped installs, so verify every listed asset BEFORE anything
    # consumes it (wheel install / image load / weather copy). Fail
    # closed: a mismatched byte or a missing manifest aborts the run.
    if cfg.offline_bundle is not None:
        try:
            verify_offline_bundle(cfg.offline_bundle)
        except OfflineBundleError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
    # Persist the exact run invocation so `osimflow resume` can re-launch
    # this campaign via cache replay (issue #1628). The raw argv is
    # recorded pre-preset (replay re-applies the preset) and the
    # subcommand token is normalized to the canonical "run".
    _persist_run_invocation(["run", *raw_argv[1:]], cfg.outdir)
    executor: BaseExecutor
    if cfg.dry_run:
        executor = LocalExecutor(max_workers=1)
        log.info("DRY RUN: forcing LocalExecutor with 1 worker")
    else:
        executor = _build_executor(args)
    # Issue #1320: cloud executors pull `nrel/openstudio:<version>` from a
    # public registry by MUTABLE tag when --container-digest is not set.
    _warn_if_mutable_tag(args.executor, cfg.container_digest)
    trust_level = ByosTrustLevel(args.byos_trust_level)
    if trust_level == ByosTrustLevel.INPROCESS:
        warnings.warn(
            "BYOS trust level 'inprocess' is a production security concern. "
            "User scripts run in the orchestrator process with full access to "
            "memory, credentials, and the filesystem. Only use 'inprocess' in "
            "trusted development environments. The default 'subprocess' mode "
            "(issue #908) is recommended for production. "
            "Set --byos-trust-level=subprocess to silence this warning.",
            UserWarning,
            stacklevel=1,
        )
    byos_resource_limits: dict[str, int] | None = None
    if args.byos_resource_limits:
        try:
            import json as json_mod  # noqa: PLC0415

            byos_resource_limits = json_mod.loads(args.byos_resource_limits)
        except (json_mod.JSONDecodeError, TypeError) as exc:
            print(
                f"error: --byos-resource-limits must be a valid JSON dict: {exc}", file=sys.stderr
            )
            return 1
    _byos_audit = _byos_audit_logger(args)
    apply_fn = (
        load_user_function(
            Path(args.custom_apply_script),
            trust_level=trust_level,
            resource_limits=byos_resource_limits,
            timeout_s=args.byos_timeout_s,
            audit_logger=_byos_audit,
        )
        if args.custom_apply_script
        else None
    )
    extract_fn = (
        load_user_function(
            Path(args.custom_kpi_extractor),
            trust_level=trust_level,
            resource_limits=byos_resource_limits,
            timeout_s=args.byos_timeout_s,
            audit_logger=_byos_audit,
        )
        if args.custom_kpi_extractor
        else None
    )
    task_queue = build_task_queue(cfg.task_queue, cfg.dask_scheduler_address)
    if cfg.task_queue != "none":
        log.info("task queue enabled: backend=%s", cfg.task_queue)
    campaign = Campaign(
        cfg,
        executor,
        apply_fn=apply_fn,
        extract_fn=extract_fn,
        max_workers=args.max_workers,
        task_queue=task_queue,
    )
    # TUI: wrap campaign execution with Rich TUI when available.
    # The TUI is a passive observer (reads run.json) and does not
    # modify campaign state.  It auto-degrades when rich is not
    # installed, stdout is not a TTY, or --no-tui is passed.
    use_tui = not args.no_tui
    tui: object = None
    if use_tui:
        try:
            from osimflow.tui import RichTUI, is_tui_available  # noqa: PLC0415

            if is_tui_available():
                tui = RichTUI(cfg.outdir)
                log.info("Rich TUI enabled")
        except Exception:  # noqa: BLE001 - TUI is optional; degrade to plain output
            log.debug("Rich TUI unavailable", exc_info=True)
    try:
        if tui is not None:
            from osimflow.tui import RichTUI  # noqa: PLC0415, F811

            assert isinstance(tui, RichTUI)
            tui.start()
        try:
            result = campaign.run()
        finally:
            if tui is not None:
                from osimflow.tui import RichTUI  # noqa: PLC0415, F811

                assert isinstance(tui, RichTUI)
                tui.stop()
            executor.shutdown()
    except Exception:
        executor.shutdown()
        raise
    print("\n=== CAMPAIGN RESULT ===")
    # The result is dict[str, object] at the type-checker level; the values
    # are well-known at runtime (see Campaign.run() return shape). Cast for
    # the formatter.
    elapsed_s = float(str(result["elapsed_s"]))
    kpis = result["kpis"]
    aggregated = result["aggregated"]
    plots = result["plots"]
    run_json = result["run_json"]
    print(f"elapsed_s: {elapsed_s:.2f}")
    if hasattr(kpis, "__len__"):
        print(f"kpis:      {len(kpis)} files")
    else:
        print(f"kpis:      {kpis}")
    if isinstance(aggregated, dict):
        print(f"csv:       {aggregated.get('csv')}")
        print(f"failed:    {aggregated.get('failed')}")
    else:
        print(f"aggregated: {aggregated}")
    print(f"plots:     {plots}")
    print(f"run trace: {run_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
