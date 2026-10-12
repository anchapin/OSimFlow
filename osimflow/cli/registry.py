"""Registry subcommands: ``list``, ``show``, ``compare``, ``aggregate-runs``, ``backup``, ``restore``."""

import argparse
import logging
import sys
import time
from pathlib import Path

from osimflow import (
    AWSBatchExecutor,  # noqa: F401 — re-exported for the historical ``osimflow.__main__.<X>Executor`` patch seam (pre-#1681).
    AzureBatchExecutor,  # noqa: F401 — see above.
    CampaignRecord,
    CampaignRegistry,
    DaskJobQueueExecutor,  # noqa: F401 — see above.
    DockerSwarmExecutor,  # noqa: F401 — see above.
    GoogleBatchExecutor,  # noqa: F401 — see above.
    KubernetesExecutor,  # noqa: F401 — see above.
    NomadExecutor,  # noqa: F401 — see above.
    PBSExecutor,  # noqa: F401 — see above.
    SlurmExecutor,  # noqa: F401 — see above.
)
from osimflow.cli.s3 import _cmd_s3
from osimflow.registry.cross_run_aggregator import CrossRunAggregator

log = logging.getLogger("osimflow.__main__")


def _cmd_list(args: argparse.Namespace) -> int:
    """List all registered campaigns."""
    import json as json_mod  # noqa: PLC0415

    if getattr(args, "from_s3", False):
        return _cmd_s3(args, "list")

    registry_path = args.registry if args.registry else None
    reg = CampaignRegistry(db_path=registry_path)
    campaigns = reg.list_campaigns(status=args.status, project=args.project, limit=args.limit)

    if not campaigns:
        print("No campaigns registered.")
        return 0

    if args.format == "json":
        print(
            json_mod.dumps(
                [c.to_dict() for c in campaigns],
                indent=2,
                default=str,
            )
        )
        return 0

    # table format (default)
    print(
        f"{'ID':<25} {'PROJECT':<20} {'STATUS':<10} "
        f"{'ALGO':<8} {'N':<6} {'EXECUTOR':<10} {'CREATED'}"
    )
    print("-" * 100)
    for c in campaigns:
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at))
        project = c.project[:18] + ".." if len(c.project) > 20 else c.project
        print(
            f"{c.id:<25} {project:<20} {c.status:<10} {c.algorithm:<8} {c.n_samples:<6} "
            f"{c.executor:<10} {created}"
        )
    print(f"\nTotal: {len(campaigns)} campaign(s)")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Show detailed info for a single campaign."""
    import json as json_mod  # noqa: PLC0415

    registry_path = args.registry if args.registry else None
    reg = CampaignRegistry(db_path=registry_path)
    record = reg.get_campaign(args.campaign_id)

    if record is None:
        print(f"Campaign '{args.campaign_id}' not found in registry.", file=sys.stderr)
        return 1

    print(f"Campaign: {record.id}")
    print(f"  Name:               {record.name}")
    print(f"  Status:             {record.status}")
    print(f"  Output directory:   {record.outdir}")
    print(f"  Algorithm:          {record.algorithm}")
    print(f"  Samples:            {record.n_samples}")
    print(f"  Executor:           {record.executor}")
    print(f"  OpenStudio version: {record.openstudio_version}")
    created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created_at))
    print(f"  Created at:         {created}")
    if record.completed_at is not None:
        completed = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.completed_at))
        elapsed = record.completed_at - record.created_at
        print(f"  Completed at:       {completed}")
        print(f"  Duration:           {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    if record.metadata:
        print(f"  Metadata:           {json_mod.dumps(record.metadata, indent=4)}")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    """Compare two or more campaigns side by side.

    When ``--outdirs`` is given, uses CrossRunAggregator to produce
    KPI-level statistics across all campaigns.
    Otherwise falls back to registry-based metadata comparison for
    two campaigns identified by registry IDs.
    """
    if args.outdirs:
        return _cmd_compare_outdirs(args)

    if args.id1 is None or args.id2 is None:
        print(
            "error: compare requires either --outdirs or two campaign IDs",
            file=sys.stderr,
        )
        return 1

    registry_path = args.registry if args.registry else None
    reg = CampaignRegistry(db_path=registry_path)
    result = reg.compare(args.id1, args.id2)

    left = result["left"]
    right = result["right"]

    if left is None:
        print(f"Campaign '{args.id1}' not found in registry.", file=sys.stderr)
        return 1
    if right is None:
        print(f"Campaign '{args.id2}' not found in registry.", file=sys.stderr)
        return 1

    # Side-by-side comparison table
    fields = [
        ("ID", "id"),
        ("Status", "status"),
        ("Algorithm", "algorithm"),
        ("Samples", "n_samples"),
        ("Executor", "executor"),
        ("OS version", "openstudio_version"),
        ("Created", "_created_fmt"),
        ("Duration", "_duration_fmt"),
    ]

    col_w = 18
    print(f"{'Field':<16} {'Left':<{col_w}} {'Right':<{col_w}}")
    print("-" * (16 + col_w * 2 + 4))

    for label, key in fields:
        lv = _format_field(left, key)
        rv = _format_field(right, key)
        print(f"{label:<16} {lv:<{col_w}} {rv:<{col_w}}")

    return 0


def _cmd_compare_outdirs(args: argparse.Namespace) -> int:  # noqa: PLR0912
    """Compare N campaigns using CrossRunAggregator (KPI-level comparison)."""
    import json as json_mod  # noqa: PLC0415

    if len(args.outdirs) < 2:
        print("error: --outdirs requires at least two campaign directories", file=sys.stderr)
        return 1

    # Build label list
    labels: list[str | None]
    if args.labels:
        if len(args.labels) != len(args.outdirs):
            print(
                f"error: --labels ({len(args.labels)}) must match --outdirs ({len(args.outdirs)})",
                file=sys.stderr,
            )
            return 1
        labels = list(args.labels)
    else:
        labels = [None] * len(args.outdirs)

    aggregator = CrossRunAggregator(campaigns=list(zip(args.outdirs, labels, strict=True)))
    aggregator.load()
    if not aggregator._runs:
        print("error: No valid campaigns found (missing aggregated_results.csv)", file=sys.stderr)
        return 1

    stats = aggregator.compute_cross_run_stats()
    combined_df = aggregator.get_combined_dataframe()

    # Optional CSV export
    if args.export:
        aggregator.export_combined_csv(args.export)
        print(f"Exported combined CSV to {args.export}")

    # Filter KPIs if requested
    kpis_to_show = set(args.kpis) if args.kpis else set(stats.keys())

    # Header
    campaign_labels = list(aggregator._runs.keys())
    col_w = 14
    kpi_col_w = 16

    print(f"\nCross-run KPI comparison ({len(campaign_labels)} campaigns)")
    print("=" * (kpi_col_w + col_w * len(campaign_labels) + 4))

    # KPI comparison table
    print(f"{'KPI':<{kpi_col_w}}", end="")
    for lbl in campaign_labels:
        print(f" {lbl[:col_w]:<{col_w}}", end="")
    print()
    print("-" * (kpi_col_w + col_w * len(campaign_labels) + 4))

    for kpi, s in stats.items():
        if kpi not in kpis_to_show:
            continue
        print(f"{kpi[:kpi_col_w]:<{kpi_col_w}}", end="")
        for label in campaign_labels:
            val = s.values.get(label)
            cell = "N/A" if val is None else f"{val:.4f}"
            print(f" {cell[:col_w]:<{col_w}}", end="")
        print()

    # Best/worst per KPI
    print("\nBest/worst per KPI:")
    print("-" * 60)
    for kpi, s in stats.items():
        if kpi not in kpis_to_show:
            continue
        best_val = s.values.get(s.best_campaign) if s.best_campaign else None
        if best_val is not None:
            print(
                f"  {kpi:<20} best={s.best_campaign} ({best_val:.4f}), "
                f"worst={s.worst_campaign} ({s.values.get(s.worst_campaign or '') or 0:.4f})"
            )

    # Cross-run summary statistics
    print("\nCross-run statistics:")
    print("-" * 60)
    for kpi, s in stats.items():
        if kpi not in kpis_to_show:
            continue
        print(
            f"  {kpi:<20} mean={s.overall_mean:.4f} std={s.overall_std:.4f} "
            f"min={s.overall_min:.4f} max={s.overall_max:.4f}"
        )

    # Combined DataFrame info
    print(
        f"\nCombined dataset: {len(combined_df)} total rows "
        f"({', '.join(f'{lbl}={r.n_samples}' for lbl, r in aggregator._runs.items())})"
    )

    # JSON summary
    summary = aggregator.summary()
    print(f"\nJSON summary: {json_mod.dumps(summary, indent=2, default=str)}")

    return 0


def _cmd_aggregate_runs(args: argparse.Namespace) -> int:
    """Aggregate KPI results from two or more campaign runs into a combined dataset.

    Loads ``aggregated_results.csv`` from each campaign directory, merges them
    into a single DataFrame with campaign labels, and prints cross-run statistics.
    Optionally exports the combined CSV to a user-specified path.
    """
    if len(args.outdirs) < 2:
        print(
            "error: aggregate-runs requires at least two campaign directories",
            file=sys.stderr,
        )
        return 1

    # Build label list
    labels: list[str | None]
    if args.labels is not None and len(args.labels) > 0:
        if len(args.labels) != len(args.outdirs):
            print(
                f"error: --labels ({len(args.labels)}) must match "
                f"positional outdirs ({len(args.outdirs)})",
                file=sys.stderr,
            )
            return 1
        labels = list(args.labels)
    else:
        labels = [None] * len(args.outdirs)

    aggregator = CrossRunAggregator(campaigns=list(zip(args.outdirs, labels, strict=True)))
    aggregator.load()

    loaded = aggregator._runs
    if not loaded:
        print(
            "error: No valid campaigns found. Check that each outdir contains "
            "an aggregated_results.csv file.",
            file=sys.stderr,
        )
        return 1

    # Determine output path
    output_path = args.output
    if output_path is None:
        first = args.outdirs[0].resolve()
        output_path = first.parent / "aggregated_combined.csv"

    aggregator.export_combined_csv(output_path)

    stats = aggregator.compute_cross_run_stats()
    combined_df = aggregator.get_combined_dataframe()

    print(f"\nAggregated {len(loaded)} campaigns:")
    for label, run in loaded.items():
        print(f"  - {label}: {run.n_samples} samples from {run.outdir}")

    print(f"\nCombined dataset: {len(combined_df)} rows -> {output_path}")

    # Filter KPIs if requested
    kpis_to_show = set(args.kpis) if args.kpis else set(stats.keys())

    if stats:
        print(f"\nCross-run statistics ({len(kpis_to_show)} KPIs):")
        print("-" * 80)
        for kpi, s in stats.items():
            if kpi not in kpis_to_show:
                continue
            vals = {k: v for k, v in s.values.items() if v is not None}
            if vals:
                print(
                    f"  {kpi:<24} overall_mean={s.overall_mean:>10.4f}  "
                    f"std={s.overall_std:>8.4f}  "
                    f"range=[{s.overall_min:>10.4f}, {s.overall_max:>10.4f}]"
                )
                for lbl, val in sorted(vals.items(), key=lambda x: x[1]):
                    print(f"    {lbl:<22} mean={val:.4f}")

    return 0


def _format_field(record: CampaignRecord, key: str) -> str:
    """Format a campaign record field for display."""
    if key == "_created_fmt":
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(record.created_at))
    if key == "_duration_fmt":
        if record.completed_at is not None:
            elapsed = record.completed_at - record.created_at
            return f"{elapsed:.1f}s"
        return "(running)"
    return str(getattr(record, key, ""))


def _cmd_backup(args: argparse.Namespace) -> int:
    """Create a backup of the campaign registry database (issue #440)."""
    registry_path = args.registry if args.registry else None
    reg = CampaignRegistry(db_path=registry_path)
    if args.output:
        backup_path = Path(args.output)
        reg.export_registry(backup_path)
    else:
        backup_path = reg.backup()
    print(f"Backup created: {backup_path}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    """Restore/import the campaign registry from a backup file (issue #440)."""
    backup_file: Path = Path(args.backup_file)
    if not backup_file.exists():
        print(f"error: backup file not found: {backup_file}", file=sys.stderr)
        return 1

    registry_path = args.registry if args.registry else None
    reg = CampaignRegistry(db_path=registry_path)
    try:
        count = reg.import_registry(backup_file, merge=args.merge)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mode = "merged" if args.merge else "replaced"
    print(f"Registry {mode} with {count} campaign(s) from {backup_file}")
    return 0
