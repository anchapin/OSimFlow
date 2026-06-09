#!/usr/bin/env python3
"""CLI entry point for the OSimFlow custom-Python spike.

Usage:
    python run_campaign.py \\
        --executor local  \\
        --input_variables <path> \\
        --template_sim_package <path> \\
        --n_samples 5 \\
        --outdir <path> \\
        --openstudio_version 3.4.0
"""
from __future__ import annotations

import argparse
import inspect
import logging
import sys
from pathlib import Path

# Add this file's parent to sys.path so `osimflow` resolves when run directly.
sys.path.insert(0, str(Path(__file__).parent))

from osimflow import (  # noqa: E402
    Campaign, CampaignConfig, load_config,
    LocalExecutor, SlurmExecutor, AWSBatchExecutor,
)


def _load_user_function(path: Path, attr: str = "main"):
    """BYOS loader: import a user .py file and return its `apply_parameters`
    or `extract_kpis` function.

    The function signature is the entire contract — no separate CLI surface
    to maintain. `inspect.signature` is the validator.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"user_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for candidate in ("apply_parameters", "extract_kpis", attr):
        if hasattr(mod, candidate) and callable(getattr(mod, candidate)):
            return getattr(mod, candidate)
    raise AttributeError(
        f"User script {path} must define `apply_parameters(...)` or "
        f"`extract_kpis(...)` (or a callable named `{attr}`)."
    )


def _build_executor(args) -> object:
    if args.executor == "local":
        return LocalExecutor(max_workers=args.max_workers)
    if args.executor == "slurm":
        return SlurmExecutor(
            partition=args.slurm_partition,
            account=args.slurm_account,
            cpus_per_task=2,
            mem_gb=4,
            time_h=2,
            debug=not args.slurm_real,  # debug mode unless --slurm_real
        )
    if args.executor == "aws_batch":
        return AWSBatchExecutor(
            job_queue=args.aws_batch_queue,
            job_definition=args.aws_batch_job_definition,
        )
    raise ValueError(f"unknown executor: {args.executor}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OSimFlow custom-Python driver (spike)")
    p.add_argument("--executor", choices=["local", "slurm", "aws_batch"], default="local")
    p.add_argument("--max-workers", type=int, default=4, help="Local executor parallelism")
    p.add_argument("--slurm-partition", default="short")
    p.add_argument("--slurm-account", default=None)
    p.add_argument("--slurm-real", action="store_true",
                   help="Submit to real Slurm (default is submitit DebugExecutor)")
    p.add_argument("--aws-batch-queue", default="osimflow-batch-queue")
    p.add_argument("--aws-batch-job-definition", default=None)
    p.add_argument("--input_variables", required=True)
    p.add_argument("--template_sim_package", required=True)
    p.add_argument("--n_samples", type=int, required=True)
    p.add_argument("--outdir", required=True)
    p.add_argument("--openstudio_version", default="3.4.0")
    p.add_argument("--archive_intermediates", action="store_true")
    p.add_argument("--custom_apply_script", default=None)
    p.add_argument("--custom_kpi_extractor", default=None)
    p.add_argument("--log_level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg: CampaignConfig = load_config(vars(args))
    executor = _build_executor(args)

    apply_fn = None
    extract_fn = None
    if args.custom_apply_script:
        apply_fn = _load_user_function(Path(args.custom_apply_script))
    if args.custom_kpi_extractor:
        extract_fn = _load_user_function(Path(args.custom_kpi_extractor))

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
