"""Campaign lifecycle subcommands: run-batches, data-point lifecycle, health, cancel, pause."""

import argparse
import json
import logging
import sys
import time
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


def _cmd_run_batches(args: argparse.Namespace) -> int:
    """Handle ``osimflow run-batches`` (issue #1874)."""
    from osimflow.batches import BatchManifestError, SubmitLockTimeout, run_batches  # noqa: PLC0415

    try:
        results = run_batches(
            args.manifest,
            args.root,
            continue_on_error=args.continue_on_error,
            dry_run=args.dry_run,
            lock_timeout_s=args.submit_lock_timeout_s,
        )
    except (BatchManifestError, SubmitLockTimeout) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for r in results:
        print(f"{r.campaign}\t{r.status}\t{r.elapsed_s:.1f}s")
    return 1 if any(r.status in ("failed", "skipped") for r in results) else 0


def _cmd_mark_for_reanalysis(args: argparse.Namespace) -> int:
    """Mark a completed/failed sample for re-running (issue #420)."""
    from osimflow.data_point_manager import DataPointManager  # noqa: PLC0415

    outdir: Path = args.outdir.resolve()
    dp_file = outdir / ".osimflow" / "data_points.json"

    if not dp_file.exists():
        print(f"error: data_points.json not found in {outdir}/.osimflow/", file=sys.stderr)
        return 1

    mgr = DataPointManager(outdir=outdir)
    try:
        new_dp = mgr.mark_for_reanalysis(args.sample_id)
    except KeyError as exc:
        print(
            f"error: sample_id {args.sample_id!r} not found in data_points.json: {exc}",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Reanalysis sample created: {new_dp.sample_id}")
    print(f"  Status: {new_dp.status.value}")
    print(f"  Original: {args.sample_id}")
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    """Merge multiple data points into a single target (issue #418)."""
    from osimflow.data_point_manager import DataPointManager  # noqa: PLC0415

    outdir: Path = args.outdir.resolve()
    dp_file = outdir / ".osimflow" / "data_points.json"

    if not dp_file.exists():
        print(f"error: data_points.json not found in {outdir}/.osimflow/", file=sys.stderr)
        return 1

    mgr = DataPointManager(outdir=outdir)
    try:
        target = mgr.merge(
            source_ids=args.source_ids,
            target_id=args.target_id,
            target_work_dir=args.target_work_dir,
        )
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Merged {len(args.source_ids)} source(s) into: {target.sample_id}")
    print(f"  Status: {target.status.value}")
    print(f"  Merged from: {', '.join(args.source_ids)}")
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    """Run system health checks (issue #411, #1024, #1562)."""
    from osimflow.health import (  # noqa: PLC0415
        format_results,
        get_exit_code,
        run_health_checks,
        to_json,
    )

    outdir = args.outdir if args.outdir else Path.cwd()
    report = run_health_checks(
        outdir=outdir,
        skip_network=args.offline,
        configured_executor=args.executor,
        redis_url=args.redis_url,
    )

    if args.json:
        print(to_json(report))
    else:
        print(format_results(report))

    return get_exit_code(report)


def _cmd_cancel(args: argparse.Namespace) -> int:
    """Request graceful cancellation of a running campaign.

    Writes a ``.stop`` flag file to the campaign's output directory.
    The campaign orchestrator checks for this file between steps and
    initiates a graceful shutdown that stops accepting new samples and
    waits for in-flight samples to complete before writing the final
    ``run.json``.
    """
    import json as json_mod  # noqa: PLC0415

    outdir: Path = args.outdir.resolve()
    run_json_path = outdir / "run.json"

    if not run_json_path.exists():
        print(f"error: run.json not found in {outdir}", file=sys.stderr)
        return 1

    try:
        with open(run_json_path) as f:
            run_data = json_mod.load(f)
    except (OSError, json_mod.JSONDecodeError) as exc:
        print(f"error: could not read run.json: {exc}", file=sys.stderr)
        return 1

    # Check if campaign has already completed
    if run_data.get("finished_at") is not None:
        print(
            f"error: campaign '{run_data.get('campaign_id', 'unknown')}' "
            f"has already completed (finished_at is set)",
            file=sys.stderr,
        )
        return 1

    # Check if .stop file already exists
    stop_file = outdir / ".stop"
    if stop_file.exists():
        print(f"cancellation already requested (--outdir={outdir})")
        return 0

    # Write the .stop flag
    stop_file.write_text(json_mod.dumps({"requested_at": time.time()}))
    campaign_id = run_data.get("campaign_id", outdir.name)
    print(f"cancellation requested for campaign '{campaign_id}'")
    print(f"  outdir:  {outdir}")
    print(f"  stop file: {stop_file}")
    return 0


def _cmd_pause(args: argparse.Namespace) -> int:
    """Request graceful pause of a running campaign.

    Writes a ``.pause`` flag file to the campaign's output directory.
    The campaign orchestrator checks for this file during fan-out and
    waits for in-flight samples to complete before writing the paused
    trace to run.json — after which the ``osimflow run`` process exits
    (there is no background orchestrator holding the campaign open).
    ``osimflow resume`` clears the flag and re-launches the recorded
    run via cache replay (issue #1628).
    """
    import json as json_mod  # noqa: PLC0415

    outdir: Path = args.outdir.resolve()
    run_json_path = outdir / "run.json"

    if not run_json_path.exists():
        print(f"error: run.json not found in {outdir}", file=sys.stderr)
        return 1

    try:
        with open(run_json_path) as f:
            run_data = json_mod.load(f)
    except (OSError, json_mod.JSONDecodeError) as exc:
        print(f"error: could not read run.json: {exc}", file=sys.stderr)
        return 1

    if run_data.get("finished_at") is not None:
        print(
            f"error: campaign '{run_data.get('campaign_id', 'unknown')}' "
            f"has already completed (finished_at is set)",
            file=sys.stderr,
        )
        return 1

    if run_data.get("status") == "paused":
        print(f"campaign '{run_data.get('campaign_id', outdir.name)}' is already paused")
        return 0

    pause_file = outdir / ".pause"
    if pause_file.exists():
        print(f"pause already requested (--outdir={outdir})")
        return 0

    pause_file.write_text(json_mod.dumps({"requested_at": time.time()}))
    campaign_id = run_data.get("campaign_id", outdir.name)
    print(f"pause requested for campaign '{campaign_id}'")
    print(f"  outdir:  {outdir}")
    print(f"  pause file: {pause_file}")
    return 0


# Per-campaign invocation record (issue #1628): written by `osimflow run`
# at start, consumed by `osimflow resume` to re-launch the exact command
# (cache replay) instead of unlinking `.pause` as a silent no-op.
_CAMPAIGN_INVOCATION_FILE = "campaign_invocation.json"


def _persist_run_invocation(tokens: list[str], outdir: Path) -> None:
    """Record the exact ``osimflow run`` argv for ``osimflow resume`` (issue #1628).

    Writes ``<outdir>/campaign_invocation.json`` holding the subcommand
    tokens (starting with ``run``) plus the invoking cwd, so a later
    ``osimflow resume`` can re-launch the identical command — relative
    paths included. Best-effort: a failure to persist only disables
    auto-replay, never the run itself.
    """
    payload: dict[str, object] = {
        "argv": tokens,
        "cwd": str(Path.cwd()),
        "recorded_at": time.time(),
    }
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / _CAMPAIGN_INVOCATION_FILE).write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        log.warning("could not persist run invocation for resume: %s", exc)


def _replay_paused_campaign(outdir: Path) -> int | None:
    """Re-launch a paused campaign's recorded ``osimflow run`` invocation.

    A paused campaign has no live orchestrator — ``Campaign.run()``
    returns once the pause lands (issue #1537) — so the only real
    recovery is cache replay: re-running the same command against the
    same ``--outdir`` (completed steps are cache hits). This spawns
    ``python -m osimflow <recorded argv>`` with the recorded cwd and
    returns the child's exit code.

    Returns ``None`` when no usable invocation record exists (campaign
    started by an older version, via the REST API, or programmatically)
    or when the record cannot be executed — callers fall back to
    printing the manual recovery command.
    """
    import shlex  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    record_path = outdir / _CAMPAIGN_INVOCATION_FILE
    try:
        record = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    argv = record.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or argv[0] != "run"
        or not all(isinstance(tok, str) for tok in argv)
    ):
        return None
    cwd_raw = record.get("cwd")
    if not isinstance(cwd_raw, str) or not Path(cwd_raw).is_dir():
        # The recorded cwd is gone; relative paths in the recorded argv
        # would resolve against the wrong directory — fall back to the
        # manual recovery message instead of mis-replaying.
        return None
    cmd = [sys.executable, "-m", "osimflow", *argv]
    print(f"resuming via cache replay: {shlex.join(cmd)}  (cwd={cwd_raw})")
    try:
        completed = subprocess.run(cmd, cwd=cwd_raw, check=False)
    except OSError as exc:
        print(f"error: could not re-launch the campaign: {exc}", file=sys.stderr)
        return None
    if completed.returncode != 0:
        print(
            f"replayed campaign exited with code {completed.returncode}",
            file=sys.stderr,
        )
    return completed.returncode
