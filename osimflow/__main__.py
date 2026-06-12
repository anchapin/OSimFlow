"""CLI entry point for OSimFlow.

Usage:
    python -m osimflow run \\
        --executor local \\
        --input_variables variables.yml \\
        --template_sim_package ./example_package \\
        --n_samples 10 \\
        --outdir ./results \\
        --openstudio_version 3.11.0

After `pip install -e .`, also available as:
    osimflow run --executor local ...
"""

import argparse
import logging
import sys
from pathlib import Path

from osimflow import (
    AWSBatchExecutor,
    BaseExecutor,
    Campaign,
    CampaignConfig,
    LocalExecutor,
    NomadExecutor,
    SlurmExecutor,
    load_config,
)
from osimflow.byos import load_user_function
from osimflow.exporters.osa import OSAExporter
from osimflow.importers.osa import OSAImportError, osa_to_variables_yml, parse_osa

log = logging.getLogger("osimflow.__main__")


def _build_executor(args: argparse.Namespace) -> BaseExecutor:
    if args.executor == "local":
        return LocalExecutor(max_workers=args.max_workers)
    if args.executor == "slurm":
        return SlurmExecutor(
            partition=args.slurm_partition,
            account=args.slurm_account,
            cpus_per_task=2,
            mem_gb=4,
            time_h=2,
            debug=not args.slurm_real,  # debug unless --slurm_real
            qos=args.slurm_qos,
            constraint=args.slurm_constraint,
            gres=args.slurm_gres,
        )
    if args.executor == "aws_batch":
        return AWSBatchExecutor(
            job_queue=args.aws_batch_queue,
            job_definition=args.aws_batch_job_definition,
            max_spot_price_usd=(
                args.aws_batch_max_spot_price_usd
                if args.aws_batch_max_spot_price_usd is not None
                else None
            ),
            fallback_to_on_demand=args.aws_batch_fallback_to_on_demand,
            max_retries=args.aws_batch_max_retries,
        )
    if args.executor == "nomad":
        return NomadExecutor(
            address=args.nomad_address,
            datacentre=args.nomad_datacentre,
        )
    raise ValueError(f"unknown executor: {args.executor}")


def _add_run_args(run: argparse.ArgumentParser) -> None:
    run.add_argument(
        "--executor", choices=["local", "slurm", "aws_batch", "nomad"], default="local"
    )
    run.add_argument("--max-workers", type=int, default=4, help="Local executor parallelism")
    run.add_argument("--slurm-partition", default="short")
    run.add_argument("--slurm-account", default=None)
    run.add_argument(
        "--slurm-real",
        action="store_true",
        help="Submit to real Slurm (default: submitit DebugExecutor)",
    )
    run.add_argument(
        "--slurm-qos",
        default=None,
        help="Slurm QoS (e.g. 'high'). Requires submitit >= 1.5.",
    )
    run.add_argument(
        "--slurm-constraint",
        default=None,
        help="Slurm constraint feature (e.g. 'gpu'). Requires submitit >= 1.5.",
    )
    run.add_argument(
        "--slurm-gres",
        default=None,
        help="Slurm generic resources (e.g. 'gpu:1'). Requires submitit >= 1.5.",
    )
    run.add_argument("--aws-batch-queue", default="osimflow-batch-queue")
    run.add_argument("--aws-batch-job-definition", default=None)
    run.add_argument(
        "--aws-batch-max-spot-price-usd",
        type=float,
        default=None,
        help=(
            "Maximum Spot price ceiling in USD per vCPU-hour. "
            "When set, the executor checks the current Spot price before "
            "submitting and rejects jobs that would exceed the ceiling "
            "(unless --aws-batch-fallback-to-on-demand is also set)."
        ),
    )
    run.add_argument(
        "--aws-batch-fallback-to-on-demand",
        action="store_true",
        help=(
            "When the Spot price exceeds the ceiling or max retries are "
            "exhausted, fall back to the on-demand job queue instead of "
            "failing. Requires --aws-batch-max-spot-price-usd or spot "
            "interruption retries."
        ),
    )
    run.add_argument(
        "--aws-batch-max-retries",
        type=int,
        default=3,
        help=(
            "Maximum number of retries on Spot interruption (default: 3). "
            "Each retry uses exponential backoff. After exhausting retries, "
            "the job fails unless --aws-batch-fallback-to-on-demand is set."
        ),
    )
    run.add_argument(
        "--ecr-repository",
        default=None,
        help=(
            "ECR repository URI for OpenStudio container images "
            "(e.g. 123456.dkr.ecr.us-east-1.amazonaws.com/osimflow/openstudio). "
            "When set, the Batch executor pulls from ECR instead of Docker Hub."
        ),
    )
    run.add_argument(
        "--nomad-address",
        default=None,
        help=(
            "Nomad cluster HTTP address (e.g. http://nomad.local:4646). "
            "Defaults to the NOMAD_ADDR env var or http://127.0.0.1:4646."
        ),
    )
    run.add_argument(
        "--nomad-datacentre",
        default="dc1",
        help="Nomad datacentre to target (default: dc1).",
    )
    run.add_argument("--input_variables", required=True)
    run.add_argument("--template_sim_package", required=True)
    run.add_argument("--n_samples", type=int, required=True)
    run.add_argument("--outdir", required=True)
    run.add_argument("--openstudio_version", default="3.11.0")
    run.add_argument("--archive_intermediates", action="store_true")
    run.add_argument(
        "--custom_apply_script",
        default=None,
        help="Path to a .py file defining apply_parameters(...) (BYOS)",
    )
    run.add_argument(
        "--custom_kpi_extractor",
        default=None,
        help="Path to a .py file defining extract_kpis(...) (BYOS)",
    )
    run.add_argument("--log_level", default="INFO")
    run.add_argument(
        "--algorithm",
        default="lhs",
        help=(
            "Sampling algorithm to use (default: lhs). "
            "Available algorithms are registered in AlgorithmRegistry."
        ),
    )
    run.add_argument(
        "--mlflow_tracking_uri",
        default=None,
        help=(
            "MLflow tracking server URI (e.g. http://localhost:5000). "
            "When set, the campaign logs params/metrics/artifacts to MLflow. "
            "Requires `pip install osimflow[mlflow]`."
        ),
    )
    run.add_argument(
        "--weather_dir",
        default="weather",
        help=(
            "Name of the weather subdirectory inside template_sim_package "
            "that holds .epw files. Default: weather."
        ),
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run a single sample in local mode to validate setup before "
            "full campaign. Forces n_samples=1, local executor, steps 1-4 only."
        ),
    )
    run.add_argument(
        "--sample",
        type=int,
        default=None,
        help=(
            "Run only sample N (0-indexed) through steps 2-4. "
            "Skips GENERATE_LHS_SAMPLES; reuses existing samples.json. "
            "Useful for debugging a specific failed sample."
        ),
    )
    run.add_argument(
        "--init-script",
        default=None,
        type=Path,
        help=(
            "Path to a shell script to run before the campaign starts. "
            "Must exit 0 or the campaign aborts. Environment variables "
            "OSIMFLOW_OUTDIR, OSIMFLOW_N_SAMPLES, OSIMFLOW_EXECUTOR, "
            "and OSIMFLOW_ALGORITHM are set."
        ),
    )
    run.add_argument(
        "--finalize-script",
        default=None,
        type=Path,
        help=(
            "Path to a shell script to run after the campaign completes. "
            "Best-effort: a non-zero exit is logged but does NOT fail "
            "the campaign. Receives the same env vars as --init-script "
            "plus OSIMFLOW_STATUS (success/failure) and "
            "OSIMFLOW_DURATION_S."
        ),
    )
    run.add_argument(
        "--skip-preflight",
        action="store_true",
        help=(
            "Skip the PREFLIGHT_RUN_MODEL step that validates the seed "
            "model before the full campaign. Useful when the model is "
            "known-good or when iterating on downstream steps."
        ),
    )
    run.add_argument(
        "--max-generations",
        type=int,
        default=1,
        help=(
            "Maximum number of DAG generations to run (default: 1). "
            "Iterative algorithms (NSGA-II, DE) loop the fan-out steps "
            "for multiple generations. LHS is single-generation."
        ),
    )
    run.add_argument(
        "--observability",
        choices=["none", "cloudwatch", "prometheus", "opentelemetry"],
        default="none",
        help=(
            "Observability backend for campaign metrics (default: none). "
            "none = no metrics emitted (zero overhead). "
            "cloudwatch = AWS CloudWatch (requires osimflow[aws]). "
            "prometheus = Prometheus pushgateway. "
            "opentelemetry = OTLP gRPC exporter."
        ),
    )
    run.add_argument(
        "--cloudwatch-namespace",
        default="OSimFlow",
        help="CloudWatch namespace for metrics (default: OSimFlow).",
    )
    run.add_argument(
        "--cloudwatch-log-group",
        default=None,
        help="CloudWatch log group name (optional).",
    )
    run.add_argument(
        "--prometheus-port",
        type=int,
        default=9090,
        help="Prometheus pushgateway port (default: 9090).",
    )
    run.add_argument(
        "--otel-endpoint",
        default=None,
        help='OpenTelemetry OTLP gRPC endpoint (e.g. "http://localhost:4317").',
    )


def _add_import_osa_args(imp: argparse.ArgumentParser) -> None:
    imp.add_argument(
        "input",
        type=Path,
        help="Path to the .osa or analysis.json file to import",
    )
    imp.add_argument(
        "--output",
        type=Path,
        default=Path("variables.yml"),
        help="Output path for the converted variables.yml (default: variables.yml)",
    )
    imp.add_argument("--log_level", default="INFO")


def _add_export_args(exp: argparse.ArgumentParser) -> None:
    exp.add_argument(
        "--target",
        choices=["pat"],
        required=True,
        help="Export format (currently only 'pat' for PAT-compatible analysis.json)",
    )
    exp.add_argument(
        "--variables",
        type=Path,
        required=True,
        help="Path to variables.yml to export",
    )
    exp.add_argument(
        "--outdir",
        type=Path,
        default=Path("."),
        help="Output directory for exported files (default: current directory)",
    )
    exp.add_argument(
        "--n_samples",
        type=int,
        default=10,
        help="Number of samples to record in the analysis (default: 10)",
    )
    exp.add_argument(
        "--algorithm",
        default="lhs",
        help="Sampling algorithm name to export (default: lhs)",
    )
    exp.add_argument(
        "--openstudio_version",
        default="3.11.0",
        help="OpenStudio CLI version (default: 3.11.0)",
    )
    exp.add_argument("--log_level", default="INFO")


def _add_serve_args(serve: argparse.ArgumentParser) -> None:
    serve.add_argument("--outdir", type=Path, required=True, help="Campaign output directory")
    serve.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    serve.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    serve.add_argument(
        "--read-only",
        action="store_true",
        default=True,
        help="Read-only mode (default). Disable with --read-write.",
    )
    serve.add_argument(
        "--read-write",
        dest="read_only",
        action="store_false",
        help="Allow campaign control (stop, live events). Disables --read-only.",
    )
    serve.add_argument("--log_level", default="INFO")
    serve.set_defaults(func=_cmd_serve)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="osimflow",
        description="OSimFlow — parametric OpenStudio simulation campaigns",
    )
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run a campaign")
    _add_run_args(run)
    imp = sub.add_parser(
        "import-osa",
        help="Import an OpenStudio Analysis (.osa / analysis.json) file",
    )
    _add_import_osa_args(imp)
    exp = sub.add_parser(
        "export",
        help="Export campaign state to an external format",
    )
    _add_export_args(exp)
    serve = sub.add_parser("serve", help="Start REST API server")
    _add_serve_args(serve)
    return p


def _cmd_serve(args: argparse.Namespace) -> int:
    """Start the REST API server."""
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError:
        print(
            "Error: osimflow[api] extra required. Install with: pip install osimflow[api]",
            file=sys.stderr,
        )
        return 1

    from osimflow.api import create_app  # noqa: PLC0415

    app = create_app(outdir=args.outdir, read_only=args.read_only)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _run_import_osa(args: argparse.Namespace) -> int:
    try:
        osa_data = parse_osa(args.input)
        osa_to_variables_yml(osa_data, args.output)
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "import-osa":
        return _run_import_osa(args)
    if args.command == "export":
        return _run_export(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command != "run":
        return 1
    cfg: CampaignConfig = load_config(vars(args))
    executor: BaseExecutor
    if cfg.dry_run:
        executor = LocalExecutor(max_workers=1)
        log.info("DRY RUN: forcing LocalExecutor with 1 worker")
    else:
        executor = _build_executor(args)
    apply_fn = (
        load_user_function(Path(args.custom_apply_script)) if args.custom_apply_script else None
    )
    extract_fn = (
        load_user_function(Path(args.custom_kpi_extractor)) if args.custom_kpi_extractor else None
    )
    campaign = Campaign(cfg, executor, apply_fn=apply_fn, extract_fn=extract_fn)
    try:
        result = campaign.run()
    finally:
        executor.shutdown()
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
