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
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from osimflow import (
    AWSBatchExecutor,
    BaseExecutor,
    Campaign,
    CampaignConfig,
    LocalExecutor,
    SlurmExecutor,
    load_config,
)


def _load_user_function(path: Path) -> Callable[..., Any]:
    """BYOS loader: import a user .py file and return its `apply_parameters`
    or `extract_kpis` function.

    The function signature is the entire contract — no separate CLI
    surface to maintain. `inspect.signature` is the validator.
    """
    # Lazy import: only the BYOS path pays the importlib.util cost.
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(f"user_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for candidate in ("apply_parameters", "extract_kpis"):
        candidate_obj = getattr(mod, candidate, None)
        if callable(candidate_obj):
            # Cast: get_type_hints() at call site, but for the loader we
            # trust the BYOS contract; mypy can't prove the module's attr.
            return cast(Callable[..., Any], candidate_obj)
    raise AttributeError(
        f"User script {path} must define `apply_parameters(...)` or `extract_kpis(...)`."
    )


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
    run.add_argument(
        "--slurm-real",
        action="store_true",
        help="Submit to real Slurm (default: submitit DebugExecutor)",
    )
    run.add_argument("--aws-batch-queue", default="osimflow-batch-queue")
    run.add_argument("--aws-batch-job-definition", default=None)
    run.add_argument("--input_variables", required=True)
    run.add_argument("--template_sim_package", required=True)
    run.add_argument("--n_samples", type=int, required=True)
    run.add_argument("--outdir", required=True)
    run.add_argument("--openstudio_version", default="3.4.0")
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
    apply_fn = (
        _load_user_function(Path(args.custom_apply_script)) if args.custom_apply_script else None
    )
    extract_fn = (
        _load_user_function(Path(args.custom_kpi_extractor)) if args.custom_kpi_extractor else None
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
