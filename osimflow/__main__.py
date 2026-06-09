"""CLI entry point for OSimFlow.

Usage:
    python -m osimflow run \\
        --executor local \\
        --input_variables variables.yml \\
        --template_sim_package ./example_package \\
        --n_samples 10 \\
        --outdir ./results \\
        --openstudio_version 3.4.0

After `pip install -e .`, also available as:
    osimflow run --executor local ...
"""
import argparse
import inspect
import logging
import sys
from pathlib import Path

from osimflow import (
    AWSBatchExecutor,
    BaseExecutor,
    Campaign,
    CampaignConfig,
    LocalExecutor,
    SlurmExecutor,
    load_config,
)


def _load_user_function(path: Path) -> callable:
    """BYOS loader: import a user .py file and return its `apply_parameters`
    or `extract_kpis` function.

    The function signature is the entire contract — no separate CLI
    surface to maintain. `inspect.signature` is the validator.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"user_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for candidate in ("apply_parameters", "extract_kpis"):
        if hasattr(mod, candidate) and callable(getattr(mod, candidate)):
            return getattr(mod, candidate)
    raise AttributeError(
        f"User script {path} must define `apply_parameters(...)` or "
        f"`extract_kpis(...)`."
    )


def _build_executor(args) -> BaseExecutor:
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
        )
    if args.executor == "aws_batch":
        return AWSBatchExecutor(
            job_queue=args.aws_batch_queue,
            job_definition=args.aws_batch_job_definition,
        )
    raise ValueError(f"unknown executor: {args.executor}")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="osimflow",
        description="OSimFlow — parametric OpenStudio simulation campaigns",
    )
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run a campaign")
    run.add_argument("--executor", choices=["local", "slurm", "aws_batch"], default="local")
    run.add_argument("--max-workers", type=int, default=4, help="Local executor parallelism")
    run.add_argument("--slurm-partition", default="short")
    run.add_argument("--slurm-account", default=None)
    run.add_argument("--slurm-real", action="store_true",
                     help="Submit to real Slurm (default: submitit DebugExecutor)")
    run.add_argument("--aws-batch-queue", default="osimflow-batch-queue")
    run.add_argument("--aws-batch-job-definition", default=None)
    run.add_argument("--input_variables", required=True)
    run.add_argument("--template_sim_package", required=True)
    run.add_argument("--n_samples", type=int, required=True)
    run.add_argument("--outdir", required=True)
    run.add_argument("--openstudio_version", default="3.4.0")
    run.add_argument("--archive_intermediates", action="store_true")
    run.add_argument("--custom_apply_script", default=None,
                     help="Path to a .py file defining apply_parameters(...) (BYOS)")
    run.add_argument("--custom_kpi_extractor", default=None,
                     help="Path to a .py file defining extract_kpis(...) (BYOS)")
    run.add_argument("--log_level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command != "run":
        return 1
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg: CampaignConfig = load_config(vars(args))
    executor = _build_executor(args)
    apply_fn = _load_user_function(Path(args.custom_apply_script)) if args.custom_apply_script else None
    extract_fn = _load_user_function(Path(args.custom_kpi_extractor)) if args.custom_kpi_extractor else None
    campaign = Campaign(cfg, executor, apply_fn=apply_fn, extract_fn=extract_fn)
    try:
        result = campaign.run()
    finally:
        executor.shutdown()
    print("\n=== CAMPAIGN RESULT ===")
    print(f"elapsed_s: {result['elapsed_s']:.2f}")
    print(f"kpis:      {len(result['kpis'])} files")
    print(f"csv:       {result['aggregated']['csv']}")
    print(f"failed:    {result['aggregated']['failed']}")
    print(f"plots:     {result['plots']}")
    print(f"run trace: {result['run_json']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
