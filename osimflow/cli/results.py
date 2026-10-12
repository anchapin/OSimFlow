"""``measure``, ``query-results`` and ``export-results`` subcommands."""

import argparse
import logging
import sys
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


def _cmd_measure_list(args: argparse.Namespace) -> int:
    """List available measures in a template simulation package (issue #532)."""
    import json as json_mod  # noqa: PLC0415

    from osimflow import MeasureRegistry  # noqa: PLC0415

    template_path: Path = args.template
    if not template_path.exists():
        print(f"error: template path not found: {template_path}", file=sys.stderr)
        return 1
    if not template_path.is_dir():
        print(f"error: template path is not a directory: {template_path}", file=sys.stderr)
        return 1

    registry = MeasureRegistry()
    registry.index_measures(template_path)
    measures = registry.list_available_measures()

    if not measures:
        measures_dir = template_path / "measures"
        if measures_dir.is_dir():
            print(f"No measures found in {measures_dir}")
        else:
            print(f"No measures/ directory found in {template_path}")
        return 0

    if args.format == "json":
        print(json_mod.dumps(measures, indent=2, default=str))
        return 0

    # table format (default)
    print(f"Available Measures in {template_path / 'measures/'}:")
    print()
    for m in measures:
        print(f"  - {m['name']} ({m['language']})")
        for arg in m.get("arguments", []):
            default_str = ""
            if arg.get("default") is not None:
                default_str = f", default: {arg['default']}"
            required_str = " [required]" if arg.get("required") else ""
            print(f"    Args: {arg['name']} [{arg['type']}]{default_str}{required_str}")
        if not m.get("arguments"):
            print("    (no arguments discovered)")
        print()

    print(f"Total: {len(measures)} measure(s)")
    return 0


def _cmd_query_results(args: argparse.Namespace) -> int:
    """Query aggregated results across multiple campaigns (issue #585)."""
    import json as json_mod  # noqa: PLC0415

    from osimflow.registry.results_query import query_results_cli  # noqa: PLC0415

    campaign_ids = None
    if args.campaign_ids:
        campaign_ids = [c.strip() for c in args.campaign_ids.split(",") if c.strip()]

    outdirs = None
    if args.outdirs:
        outdirs = [o.strip() for o in args.outdirs.split(",") if o.strip()]

    result = query_results_cli(
        campaign_ids=campaign_ids,
        outdirs=outdirs,
        filter_expr=args.filter_expr,
        page=args.page,
        per_page=args.per_page,
        format=args.format,
    )

    if not result["rows"]:
        print("No results found.")
        return 0

    if args.format == "json":
        print(
            json_mod.dumps(
                {
                    "rows": result["rows"],
                    "total": result["total"],
                    "columns": result["columns"],
                    "campaigns_queried": result["campaigns_queried"],
                },
                indent=2,
                default=str,
            )
        )
        return 0

    # table format
    columns = result["columns"]
    if not columns:
        print("No columns available.")
        return 0

    # Print header
    header = "  ".join(f"{c[:20]:<20}" for c in columns)
    print(f"{'COLUMN':<20}  " + header)
    print("-" * 100)

    for i, row in enumerate(result["rows"]):
        print(f"Row {i + 1}: " + "  ".join(f"{str(row.get(c, ''))[:20]:<20}" for c in columns))

    print(
        f"\nTotal: {result['total']} result(s), page "
        f"{args.page}/{max(1, (result['total'] + args.per_page - 1) // args.per_page)}"
    )
    print(f"Campaigns queried: {result['campaigns_queried']}")
    return 0


def _cmd_export_results(args: argparse.Namespace) -> int:
    """Export aggregated results to CSV or JSON (issue #585)."""
    from osimflow.registry.results_query import export_results_cli  # noqa: PLC0415

    campaign_ids = None
    if args.campaign_ids:
        campaign_ids = [c.strip() for c in args.campaign_ids.split(",") if c.strip()]

    outdirs = None
    if args.outdirs:
        outdirs = [o.strip() for o in args.outdirs.split(",") if o.strip()]

    return export_results_cli(
        campaign_ids=campaign_ids,
        outdirs=outdirs,
        filter_expr=args.filter_expr,
        format=args.format,
        output_path=args.output,
        include_failed=args.include_failed,
    )
