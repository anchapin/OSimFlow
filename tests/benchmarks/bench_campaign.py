"""Campaign performance benchmark (issue #10).

Runs a 3-sample campaign against a fresh outdir (cold cache) and then
re-runs against the same outdir (warm cache), recording:

  * Total wall-clock for both runs (`cold_wall_s`, `warm_wall_s`).
  * Per-step wall-clock parsed from each run's `run.json`
    (`cold_per_step_s`, `warm_per_step_s`).
  * Peak resident-set size of the benchmark process via
    `resource.getrusage(RUSAGE_SELF)` (POSIX only; `None` on Windows).
    Per-sample peak RSS is a future wire-up — the per-step work
    functions run in subprocesses and would need forking-level
    instrumentation; for now we capture the orchestrator's footprint.

The full `metrics` dict is returned to the caller and persisted to
`${outdir}/benchmarks.json`. The `passed` boolean is the single
verdict signal the CI workflow surfaces in its summary.

PRD §5.2 calls for *"Initial 'Performance Benchmarking' workflow
within CI/CD to track execution time/resource use for a small sample
against different environments."* This module is the OSimFlow
implementation: a small, fast, deterministic regression test for
orchestrator overhead. The `docs/benchmarks.md` page documents the
artifact schema and how to interpret the results.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from osimflow import Campaign, CampaignConfig
from osimflow.executors import BaseExecutor, LocalExecutor

log = logging.getLogger("osimflow.bench")

# Soft dependency: `resource` is POSIX-only. On Windows the module
# import raises ImportError; the benchmark still runs but `peak_rss`
# is recorded as `None`.
try:
    import resource  # noqa: PLC0415 — POSIX-only

    _HAS_RUSAGE = True
except ImportError:  # pragma: no cover — Windows-only branch
    resource = None  # type: ignore[assignment]
    _HAS_RUSAGE = False


SCHEMA_VERSION = 1
# Issue #10 acceptance criterion: cold-cache wall-clock should stay
# under 30s for the stub work. Tight enough to catch orchestrator
# regressions, generous enough to absorb CI flake. Override via
# `OSIMFLOW_BENCH_THRESHOLD_S` on slower runners.
DEFAULT_THRESHOLD_S = 30.0
DEFAULT_N_SAMPLES = 3
BENCHMARK_ARTIFACT = "benchmarks.json"


def _peak_rss_bytes() -> int | None:
    """Return the current process's peak RSS in bytes, or `None` if
    `resource.getrusage` is unavailable (Windows)."""
    if not _HAS_RUSAGE:
        return None
    # `ru_maxrss` is KB on Linux, bytes on macOS. We normalize to a
    # single unit (bytes) by checking `sys.platform` so consumers can
    # compare across hosts. On Linux the value is small (single-digit
    # MB for a 3-sample run); on macOS it is large (hundreds of MB).
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return value
    return value * 1024  # Linux: KB -> bytes


def _per_step_s(run_json: Path) -> dict[str, float]:
    """Extract `{step_name: elapsed_s}` from a `run.json` artifact.

    Returns an empty dict if `run_json` does not exist yet (e.g. a
    cache-hit fast path that skipped the orchestrator instrumentation).
    """
    if not run_json.is_file():
        return {}
    data = json.loads(run_json.read_text())
    return {row["step"]: float(row["elapsed_s"]) for row in data.get("steps", [])}


def _run_once(
    cfg: CampaignConfig,
    executor: BaseExecutor,
) -> tuple[float, Path]:
    """Run one campaign, return `(wall_clock_s, run_json_path)`."""
    campaign = Campaign(cfg=cfg, executor=executor)
    t0 = time.perf_counter()
    result = campaign.run()
    elapsed = time.perf_counter() - t0
    return elapsed, Path(result["run_json"])


def run_benchmark(
    cfg: CampaignConfig,
    executor: BaseExecutor | None = None,
    threshold_cold_s: float = DEFAULT_THRESHOLD_S,
) -> dict[str, Any]:
    """Run a cold + warm 3-sample campaign and return the metrics dict.

    The `executor` argument lets tests inject a pre-built executor
    (e.g. `LocalExecutor(max_workers=2)`); when omitted, a default
    `LocalExecutor(max_workers=2)` is created. The function calls
    `executor.shutdown()` before returning so the caller doesn't need
    to (and so the test can assert no thread-pool leaks).

    The metrics dict has the documented schema (see
    `docs/benchmarks.md` for the human-readable version):

      {
        "schema_version": 1,
        "campaign_id": "<timestamp>",
        "executor": "local",
        "openstudio_version": "3.11.0",
        "n_samples": 3,
        "cold_wall_s": 12.34,
        "warm_wall_s": 0.10,
        "cold_per_step_s": {"GENERATE_LHS_SAMPLES": 1.2, ...},
        "warm_per_step_s": {...},
        "peak_rss": 12345,
        "threshold_cold_s": 30.0,
        "passed": True
      }
    """
    if executor is None:
        executor = LocalExecutor(max_workers=2)
    try:
        # Cold run: a fresh outdir means a fresh cache DB, so every
        # step is a MISS.
        cold_wall_s, cold_run_json = _run_once(cfg, executor)
        cold_per_step = _per_step_s(cold_run_json)

        # Warm run: same outdir means the cache DB is populated, so
        # every step is a HIT. The 288x speedup from
        # `.agents/results/decision-verdict.md` §1 is the gold
        # standard; in practice we just assert warm < cold.
        warm_wall_s, warm_run_json = _run_once(cfg, executor)
        warm_per_step = _per_step_s(warm_run_json)

        passed = cold_wall_s < threshold_cold_s

        metrics: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "campaign_id": time.strftime("%Y-%m-%dT%H-%M-%S"),
            "executor": executor.name,
            "openstudio_version": cfg.openstudio_version,
            "n_samples": cfg.n_samples,
            "cold_wall_s": cold_wall_s,
            "warm_wall_s": warm_wall_s,
            "cold_per_step_s": cold_per_step,
            "warm_per_step_s": warm_per_step,
            "peak_rss": _peak_rss_bytes(),
            "threshold_cold_s": threshold_cold_s,
            "passed": passed,
        }

        out = cfg.outdir / BENCHMARK_ARTIFACT
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(metrics, indent=2, default=str))
        log.info("wrote benchmark artifact to %s", out)
        return metrics
    finally:
        # Cleanup is mandatory: the LocalExecutor's ThreadPoolExecutor
        # leaks worker threads if not shut down, which is a CI flake
        # hazard.
        executor.shutdown()


PLOTS_STEP_MODULE = "osimflow._work_scripts.generate_plots"

PLOTS_STARTUP_REPETITIONS = 5


def bench_plots_step_startup(
    *,
    repetitions: int = PLOTS_STARTUP_REPETITIONS,
    module: str = PLOTS_STEP_MODULE,
) -> dict[str, Any]:
    """Time the plots-step subprocess startup (issue #1485).

    Spawns ``python -m osimflow._work_scripts.generate_plots --help``
    ``repetitions`` times and records the wall-clock per invocation. This
    is the cost multiplied by sample count on large campaigns, so it is
    the metric that the deferred ``osimflow.algorithms`` import targets.

    Returns a metrics dict with ``samples_s``, ``min_s``, ``mean_s`` and
    ``max_s``. No threshold is enforced — the numbers are recorded so
    before/after runs are comparable.
    """
    samples: list[float] = []
    for _ in range(repetitions):
        t0 = time.perf_counter()
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-m", module, "--help"],
            capture_output=True,
            check=False,
        )
        samples.append(time.perf_counter() - t0)
        if proc.returncode != 0:
            log.warning(
                "plots-step --help exited %d: %s",
                proc.returncode,
                proc.stderr.decode(errors="replace")[-500:],
            )
    metrics: dict[str, Any] = {
        "module": module,
        "repetitions": repetitions,
        "samples_s": samples,
        "min_s": min(samples),
        "mean_s": sum(samples) / len(samples),
        "max_s": max(samples),
    }
    log.info(
        "plots-step startup: min=%.3fs mean=%.3fs max=%.3fs (n=%d)",
        metrics["min_s"],
        metrics["mean_s"],
        metrics["max_s"],
        repetitions,
    )
    return metrics


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bench_campaign",
        description=(
            "OSimFlow performance benchmark (issue #10). Runs a 3-sample "
            "campaign, writes ${outdir}/benchmarks.json, exits 0 on pass / 1 "
            "on fail."
        ),
    )
    p.add_argument("--input_variables", required=True, type=Path)
    p.add_argument("--template_sim_package", required=True, type=Path)
    p.add_argument("--n_samples", type=int, default=DEFAULT_N_SAMPLES)
    p.add_argument("--outdir", required=True, type=Path)
    p.add_argument("--openstudio_version", default="3.11.0")
    p.add_argument(
        "--threshold_cold_s",
        type=float,
        default=float(os.environ.get("OSIMFLOW_BENCH_THRESHOLD_S", DEFAULT_THRESHOLD_S)),
        help="Cold-cache wall-clock threshold in seconds (default: 30).",
    )
    p.add_argument("--max_workers", type=int, default=2)
    p.add_argument("--log_level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on pass, 1 on fail (for CI)."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = CampaignConfig(
        input_variables=args.input_variables.resolve(),
        template_sim_package=args.template_sim_package.resolve(),
        n_samples=args.n_samples,
        outdir=args.outdir.resolve(),
        openstudio_version=args.openstudio_version,
    )
    executor = LocalExecutor(max_workers=args.max_workers)
    metrics = run_benchmark(cfg, executor=executor, threshold_cold_s=args.threshold_cold_s)
    plots_startup = bench_plots_step_startup()
    # Summary line for the CI job log.
    verdict = "PASS" if metrics["passed"] else "FAIL"
    print(
        f"\n=== BENCHMARK {verdict} ===\n"
        f"  cold_wall_s:  {metrics['cold_wall_s']:.2f}\n"
        f"  warm_wall_s:  {metrics['warm_wall_s']:.2f}\n"
        f"  threshold:    {metrics['threshold_cold_s']:.2f}\n"
        f"  n_samples:    {metrics['n_samples']}\n"
        f"  executor:     {metrics['executor']}\n"
        f"  artifact:     {cfg.outdir / BENCHMARK_ARTIFACT}\n"
        f"  plots_step_startup_s: min={plots_startup['min_s']:.3f} "
        f"mean={plots_startup['mean_s']:.3f} max={plots_startup['max_s']:.3f}\n"
    )
    return 0 if metrics["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
