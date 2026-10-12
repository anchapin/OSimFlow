"""``import-osa`` / ``import-gem-osa`` / ``export`` subcommands."""

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
from osimflow.importers.osa import OSAImportError, osa_to_variables_yml, parse_osa

log = logging.getLogger("osimflow.__main__")


def _run_import_osa(args: argparse.Namespace) -> int:
    try:
        osa_data = parse_osa(args.input)
        osa_to_variables_yml(osa_data, args.output)
    except (FileNotFoundError, OSAImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Converted {args.input} -> {args.output}")
    return 0


def _run_import_gem_osa(args: argparse.Namespace) -> int:
    from osimflow.importers.gem_osa import GemImportError, import_gem_project  # noqa: PLC0415

    try:
        manifest = import_gem_project(
            args.project_dir,
            args.output_dir,
            batches=args.batch,
            template_package=args.template_package,
            configs=args.configs,
        )
    except GemImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {len(manifest['batches'])} batch(es) -> {args.output_dir}/batches.json")
    return 0


def _run_export(args: argparse.Namespace) -> int:
    variables_path = Path(args.variables).resolve()
    if not variables_path.exists():
        print(f"error: variables file not found: {variables_path}", file=sys.stderr)
        return 1

    outdir = Path(args.outdir).resolve()
    # Build a minimal CampaignConfig for the exporter.
    # The export subcommand only needs a subset of fields.
    cfg = CampaignConfig(
        input_variables=variables_path,
        template_sim_package=outdir,  # placeholder — not used by export
        n_samples=args.n_samples,
        outdir=outdir,
        openstudio_version=args.openstudio_version,
        algorithm=args.algorithm,
    )

    exporter = OSAExporter()
    path = exporter.export(cfg, outdir)
    print(f"Exported {path}")
    return 0
