"""``status`` and ``download`` subcommands."""

import argparse
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
from osimflow.cli.coordinator import (
    _download_coordinator_results,
    _print_coordinator_status,
)
from osimflow.cli.s3 import _cmd_s3
from osimflow.handoff_record import (
    NoHandoffRecordError,
    handoff_record_exists,
    read_handoff_record,
)

log = logging.getLogger("osimflow.__main__")


def _cmd_status(args: argparse.Namespace) -> int:
    """Show detailed status of a campaign.

    For a Coordinator-backed (``--detach``) campaign, resolves the
    ``campaign_id`` from the local handoff record under ``outdir/`` and polls
    the Coordinator for live status — works from a fresh shell / rebooted
    machine (issue #630). Otherwise falls back to the local ``run.json`` view.
    """
    if getattr(args, "from_s3", False):
        return _cmd_s3(args, "status")

    import json as json_mod  # noqa: PLC0415

    outdir: Path = args.outdir.resolve()

    # Coordinator-backed campaign: prefer the live Coordinator status. This
    # path needs no run.json (a detached campaign never ran locally).
    if handoff_record_exists(outdir):
        try:
            record = read_handoff_record(outdir)
        except NoHandoffRecordError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return _print_coordinator_status(record)

    run_json_path = outdir / "run.json"

    if not run_json_path.exists():
        print(f"error: run.json not found in {outdir}", file=sys.stderr)
        print(
            "       (no Coordinator handoff record either; did you run with `--detach`?)",
            file=sys.stderr,
        )
        return 1

    try:
        with open(run_json_path) as f:
            run_data = json_mod.load(f)
    except (OSError, json_mod.JSONDecodeError) as exc:
        print(f"error: could not read run.json: {exc}", file=sys.stderr)
        return 1

    # Top-level summary
    print(f"Campaign:   {run_data.get('campaign_id', 'unknown')}")
    print(f"Outdir:      {outdir}")
    status = run_data.get("status", "unknown")
    print(f"Status:      {status}")
    started = run_data.get("started_at", 0)
    if started:
        print(f"Started:     {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}")
    finished = run_data.get("finished_at")
    if finished:
        elapsed = finished - started if started else 0
        print(f"Finished:    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(finished))}")
        print(f"Elapsed:     {elapsed:.1f}s ({elapsed / 60:.1f} min)")

    # Steps table
    steps = run_data.get("steps", [])
    if steps:
        print("\nSteps:")
        print(f"  {'STEP':<30} {'CACHE':<10} {'ELAPSED':<10} {'EXIT'}")
        print(f"  {'-' * 30} {'-' * 10} {'-' * 10} {'-' * 5}")
        for s in steps:
            print(
                f"  {s.get('step', ''):<30} {s.get('cache', ''):<10} "
                f"{s.get('elapsed_s', 0):<10.2f} {s.get('exit_code', 0)}"
            )

    # Per-sample summary
    per_sample = run_data.get("per_sample", [])
    if per_sample:
        n_ok = sum(1 for s in per_sample if s.get("status") == "ok")
        n_failed = sum(1 for s in per_sample if s.get("status") == "failed")
        n_cached = sum(1 for s in per_sample if s.get("status") == "cached")
        print(
            f"\nSamples:    {len(per_sample)} total  |  {n_ok} ok  "
            f"|  {n_failed} failed  |  {n_cached} cached"
        )

    # Generation info for iterative algorithms
    generations = run_data.get("generations", [])
    if generations:
        print("\nGenerations:")
        for g in generations:
            print(
                f"  gen {g.get('generation', '?')}: "
                f"{g.get('n_succeeded', 0)}/{g.get('n_samples', 0)} succeeded  "
                f"({g.get('elapsed_s', 0):.1f}s)"
            )

    print(f"\nrun.json:    {run_json_path}")
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    """Download results from a completed campaign to a destination directory.

    For a Coordinator-backed (``--detach``) campaign, fetches ONLY the final
    aggregated artifacts via the Coordinator's presigned URL — per-sample
    bytes are not downloaded (issue #630). Otherwise copies the local
    artifacts from ``outdir/``.
    """
    if getattr(args, "from_s3", False):
        return _cmd_s3(args, "download")

    outdir: Path = args.outdir.resolve()

    # Coordinator-backed campaign: fetch aggregated artifacts via presigned
    # URL from the Coordinator (no local run.json needed).
    if handoff_record_exists(outdir):
        try:
            record = read_handoff_record(outdir)
        except NoHandoffRecordError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        output_dir: Path = (
            args.output_dir.resolve()
            if args.output_dir
            else outdir.parent / f"{outdir.name}-downloads" / record.campaign_id
        )
        return _download_coordinator_results(record, output_dir)

    return _download_local_results(args, outdir)


def _download_local_results(args: argparse.Namespace, outdir: Path) -> int:
    """Copy local artifacts (run.json, CSVs, plots/, kpis/) from ``outdir/``.

    This is the original ``osimflow download`` behaviour for campaigns that ran
    locally (no Coordinator). Extracted from ``_cmd_download`` so the
    Coordinator-backed path can short-circuit above.
    """
    import json as json_mod  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    run_json_path = outdir / "run.json"

    if not run_json_path.exists():
        print(f"error: run.json not found in {outdir}", file=sys.stderr)
        print(
            "       (no Coordinator handoff record either; did you run with `--detach`?)",
            file=sys.stderr,
        )
        return 1

    try:
        with open(run_json_path) as f:
            run_data = json_mod.load(f)
    except (OSError, json_mod.JSONDecodeError) as exc:
        print(f"error: could not read run.json: {exc}", file=sys.stderr)
        return 1

    campaign_id = run_data.get("campaign_id", outdir.name)
    output_dir: Path = (
        args.output_dir.resolve()
        if args.output_dir
        else outdir.parent / f"{outdir.name}-downloads" / campaign_id
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    log.info("Downloading campaign %s to %s", campaign_id, output_dir)

    # Always copy: run.json, aggregated_results.csv, failed_simulations.csv, plots/
    artifacts = [
        ("run.json", run_json_path),
    ]

    csv_path = outdir / "aggregated_results.csv"
    if csv_path.exists():
        artifacts.append(("aggregated_results.csv", csv_path))

    failed_csv = outdir / "failed_simulations.csv"
    if failed_csv.exists():
        artifacts.append(("failed_simulations.csv", failed_csv))

    plots_dir = outdir / "plots"
    if plots_dir.is_dir():
        dest_plots = output_dir / "plots"
        shutil.copytree(plots_dir, dest_plots, dirs_exist_ok=True)
        log.info("  plots/ -> %s", dest_plots)

    kpis_dir = outdir / "kpis"
    if kpis_dir.is_dir():
        dest_kpis = output_dir / "kpis"
        shutil.copytree(kpis_dir, dest_kpis, dirs_exist_ok=True)
        log.info("  kpis/ -> %s", dest_kpis)

    for name, src in artifacts:
        dest = output_dir / name
        shutil.copy2(src, dest)
        log.info("  %s -> %s", name, dest)

    # Optionally copy per-sample intermediates
    if args.include_intermediates:
        work_dir = outdir / "work"
        if work_dir.is_dir():
            dest_work = output_dir / "work"
            shutil.copytree(work_dir, dest_work, dirs_exist_ok=True)
            log.info("  work/ -> %s", dest_work)

    print(f"\nDownloaded campaign '{campaign_id}' to:\n  {output_dir}")
    return 0


# ---------------------------------------------------------------------------
# Coordinator (``--detach``) reconnect helpers — issue #630, Epic #624
# ---------------------------------------------------------------------------
#
# These functions implement the detach handoff + CLI reconnect UX:
#
#   * ``_build_coordinator_payload`` / ``_coordinator_idempotency_key`` build
#     a deterministic handoff payload + ``Idempotency-Key`` so a retried
#     handoff (e.g. after a lost response) returns the *same* campaign.
#   * ``_perform_detach_handoff`` is the ``osimflow run --detach`` flow: it
#     POSTs to the Coordinator, writes a local handoff record, and exits.
#   * ``_print_coordinator_status`` / ``_download_coordinator_results`` power
#     ``osimflow status`` / ``osimflow download`` for Coordinator-backed
#     campaigns, reading the campaign_id from the local handoff record so
#     they work from a fresh shell / rebooted machine.
#
# httpx is imported at module top; boto3 / json / hashlib are lazy-imported
# inside the few helpers that need them to keep CLI cold-start cheap.
