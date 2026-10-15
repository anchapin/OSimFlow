"""``import-osa`` / ``export`` subcommands."""

import argparse
import logging
import sys
from pathlib import Path

from osimflow import (
    AWSBatchExecutor,  # noqa: F401 — re-exported for the historical ``osimflow.__main__.<X>Executor`` patch seam (pre-#1681).
    AzureBatchExecutor,  # noqa: F401 — see above.
    CampaignConfig,
    DaskJobQueueExecutor,  # noqa: F401 — see above.
    DockerSwarmExecutor,  # noqa: F401 — see above.
    GoogleBatchExecutor,  # noqa: F401 — see above.
    KubernetesExecutor,  # noqa: F401 — see above.
    NomadExecutor,  # noqa: F401 — see above.
    PBSExecutor,  # noqa: F401 — see above.
    SlurmExecutor,  # noqa: F401 — see above.
)
from osimflow.exporters.osa import OSAExporter
from osimflow.importers.osa import (
    OSAImportError,
    osa_to_variables_yml,
    parse_osa,
    stage_osa_template_package,
)

log = logging.getLogger("osimflow.__main__")


def _run_import_osa(args: argparse.Namespace) -> int:
    template_package: Path | None = getattr(args, "template_package", None)
    package_output: Path | None = getattr(args, "package_output", None)
    try:
        osa_data = parse_osa(args.input)
        osa_to_variables_yml(osa_data, args.output)
        if template_package is not None or package_output is not None:
            dest_pkg = (
                package_output
                if package_output is not None
                else Path(args.output).resolve().parent / "template_sim_package"
            )
            stage_osa_template_package(
                args.input,
                dest_pkg,
                template_package=template_package,
                osa_data=osa_data,
            )
    except (FileNotFoundError, OSAImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Converted {args.input} -> {args.output}")
    return 0


def _run_export(args: argparse.Namespace) -> int:
    variables_path = Path(args.variables).resolve()
    if not variables_path.exists():
        print(f"error: variables file not found: {variables_path}", file=sys.stderr)
        return 1

    outdir = Path(args.outdir).resolve()
    archived_pkg = outdir / "inputs" / "template_sim_package"
    template_package: Path | None = getattr(args, "template_package", None)
    if template_package is not None:
        template_package = Path(template_package).resolve()
        if not template_package.is_dir():
            print(f"error: template package not found: {template_package}", file=sys.stderr)
            return 1
    elif archived_pkg.is_dir():
        template_package = archived_pkg

    # Build a minimal CampaignConfig for the exporter.
    # The export subcommand only needs a subset of fields.
    cfg = CampaignConfig(
        input_variables=variables_path,
        # Without a package the (non-existent) archive path acts as a placeholder.
        template_sim_package=template_package or archived_pkg,
        n_samples=args.n_samples,
        outdir=outdir,
        openstudio_version=args.openstudio_version,
        algorithm=args.algorithm,
    )

    exporter = OSAExporter()
    path = exporter.export(cfg, outdir)
    print(f"Exported {path}")
    if template_package is not None:
        osa_path = exporter.pack_osa(cfg, outdir)
        print(f"Packed {osa_path}")
    return 0
