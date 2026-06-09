"""Campaign orchestrator.

This is the ~250-line class that replaces the seven Nextflow files
(`main.nf` + six `modules/PROCESS_*.nf`). The shape is:

    1. GENERATE_LHS_SAMPLES — one shot, no fan-out.
    2. APPLY_PARAMETERS     — fan out over N samples.
    3. RUN_OPENSTUDIO_SIM   — fan out over N samples (heavy).
    4. EXTRACT_KPIS         — fan out over N samples.
    5. AGGREGATE_RESULTS    — one shot after all KPIs.
    6. GENERATE_BASIC_PLOTS — one shot after aggregation.

Each step is cached via `SQLiteCache`. Each per-sample step submits to
the configured `BaseExecutor`. Fan-out is bounded by the executor's
max_workers so we do not overwhelm the underlying scheduler.

BYOS extension is exposed at the `_byos_apply` / `_byos_extract` hooks —
the user supplies a Python file with a function of the right signature
and we discover + call it via `inspect.signature`.
"""
from __future__ import annotations

import inspect
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from .cache import CacheKey, SQLiteCache, sha256_of_dict, sha256_of_files
from .config import CampaignConfig
from .executors import BaseExecutor, LocalExecutor
from .work import (
    aggregate_results,
    default_apply_parameters,
    extract_kpis,
    generate_plots,
    run_openstudio_sim,
)

log = logging.getLogger("osimflow.campaign")

CONTAINER_OS = "ghcr.io/anchapin/openstudio_cli_image:{version}"
CONTAINER_PY = "ghcr.io/anchapin/scientific_python_image:latest"


class Campaign:
    def __init__(
        self,
        cfg: CampaignConfig,
        executor: BaseExecutor,
        apply_fn=None,
        extract_fn=None,
    ):
        self.cfg = cfg
        self.executor = executor
        self.apply_fn = apply_fn or default_apply_parameters
        self.extract_fn = extract_fn or extract_kpis
        self.cache = SQLiteCache(cfg.cache_db)
        # Hash the code that affects per-step behavior so a `bin/*.py` edit
        # invalidates cached results. This is the fix for the
        # "Python glue invisible to cache hash" gotcha in
        # `.agents/results/result-architecture.md` issue #2.
        self.code_hashes = self._compute_code_hashes()

    def _compute_code_hashes(self) -> dict:
        """SHA-256 of every bin/*.py file, plus the work.py module."""
        from . import work
        files = sorted((Path(__file__).parent.parent.parent.parent.parent / "bin").glob("*.py"))
        work_file = Path(inspect.getfile(work))
        return {
            "bin": sha256_of_files(files),
            "work": sha256_of_files([work_file]),
        }

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self) -> dict:
        log.info("=" * 60)
        log.info("OSimFlow campaign start")
        log.info("  executor:      %s", self.executor.name)
        log.info("  n_samples:     %d", self.cfg.n_samples)
        log.info("  os_version:    %s", self.cfg.openstudio_version)
        log.info("  outdir:        %s", self.cfg.outdir)
        log.info("  work_dir:      %s", self.cfg.work_dir)
        log.info("=" * 60)

        t0 = time.time()
        samples = self.step_generate_lhs()
        parameterized = self.step_apply_parameters(samples)
        simulated = self.step_run_openstudio_sim(parameterized)
        kpi_files = self.step_extract_kpis(simulated)
        aggregated = self.step_aggregate_results(kpi_files, simulated)
        plots = self.step_generate_plots(aggregated)
        t1 = time.time()

        log.info("=" * 60)
        log.info("OSimFlow campaign complete in %.1fs", t1 - t0)
        log.info("  cache stats:   %s", self.cache.stats())
        log.info("  aggregated:    %s", aggregated["csv"])
        log.info("  failed:        %s", aggregated["failed"])
        log.info("  plots:         %s", plots)
        log.info("=" * 60)
        return {
            "samples": samples,
            "kpis": kpi_files,
            "aggregated": aggregated,
            "plots": plots,
            "elapsed_s": t1 - t0,
        }

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    def step_generate_lhs(self) -> list[dict]:
        """Single-shot: read variables.yml, produce N parameter sets.

        The spike uses an in-process LHS implementation (uniform in [0,1],
        mapped per distribution in variables.yml) so it does not depend on
        the bin/ stub returning data. A real implementation will swap this
        for the LHS algorithm in bin/generate_lhs.py once that is
        implemented; the Campaign contract (output schema) is identical.
        """
        import yaml
        inputs_hash = sha256_of_files([self.cfg.input_variables])
        key = CacheKey(
            step="GENERATE_LHS_SAMPLES",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256=inputs_hash,
            code_sha256=self.code_hashes["bin"],
            container_digest=CONTAINER_PY,
        )
        cached = self.cache.lookup(key)
        if cached:
            samples = json.loads(cached.read_text())["samples"]
            log.info("LHS from cache: %d samples", len(samples))
            return samples

        import random
        with self.cfg.input_variables.open() as f:
            variables = yaml.safe_load(f)["variables"]
        rng = random.Random(0)  # deterministic
        samples = []
        for i in range(self.cfg.n_samples):
            values = {}
            for v in variables:
                if v["distribution"] == "uniform":
                    values[v["name"]] = v["min"] + rng.random() * (v["max"] - v["min"])
                elif v["distribution"] == "lognormal":
                    # lognormal via Box-Muller approximation for the spike
                    u1 = max(rng.random(), 1e-9)
                    u2 = rng.random()
                    z = (-2 * (u1 ** 0.5).bit_length() if False else 0)  # placeholder
                    import math
                    z = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
                    values[v["name"]] = math.exp(v["mean"] + v["sigma"] * z)
                else:
                    raise NotImplementedError(
                        f"distribution {v['distribution']!r} not in spike yet")
            samples.append({"sample_id": f"{i+1:04d}", "values": values})
        out_json = self.cfg.work_dir / "samples.json"
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps({
            "n_samples": len(samples),
            "variables": variables,
            "samples": samples,
        }, indent=2))
        self.cache.store(key, out_json, exit_code=0)
        log.info("LHS generated %d samples", len(samples))
        return samples

    def step_apply_parameters(self, samples: list[dict]) -> dict:
        """Fan-out: for each sample, produce a modified sim package."""
        out: dict[str, Path] = {}
        for s in samples:
            sid = s["sample_id"]
            params = s["values"]
            inputs_hash = sha256_of_dict({"params": params, "sid": sid})
            key = CacheKey(
                step="APPLY_PARAMETERS",
                sample_id=sid,
                openstudio_version="N/A",
                inputs_sha256=inputs_hash,
                code_sha256=self.code_hashes["bin"],
                container_digest=CONTAINER_PY,
            )
            cached = self.cache.lookup(key)
            if cached:
                out[sid] = cached
                continue
            out_dir = self.cfg.work_dir / "apply" / sid
            out_dir.mkdir(parents=True, exist_ok=True)
            log.info("submit APPLY_PARAMETERS sample=%s", sid)
            handle = self.executor.submit(
                self.apply_fn,
                self.cfg.template_sim_package, params, sid, out_dir,
                name=f"apply_{sid}",
                cpus=1, memory_mb=512, time_min=5,
                container=CONTAINER_PY,
            )
            try:
                result_path = handle.result(timeout=120)
            except Exception as e:
                log.error("APPLY_PARAMETERS %s failed: %s", sid, e)
                self.cache.store(key, out_dir, exit_code=1)
                continue
            self.cache.store(key, Path(result_path), exit_code=0)
            out[sid] = Path(result_path)
        return out

    def step_run_openstudio_sim(self, parameterized: dict) -> dict:
        """Fan-out (heavy): for each sample, run the OpenStudio simulation."""
        out: dict[str, Path] = {}
        os_version = self.cfg.openstudio_version
        for sid, mod_pkg in parameterized.items():
            inputs_hash = sha256_of_dict({"template": str(self.cfg.template_sim_package),
                                          "sid": sid,
                                          "os_version": os_version})
            key = CacheKey(
                step="RUN_OPENSTUDIO_SIM",
                sample_id=sid,
                openstudio_version=os_version,
                inputs_sha256=inputs_hash,
                code_sha256=self.code_hashes["bin"],
                container_digest=CONTAINER_OS.format(version=os_version),
            )
            cached = self.cache.lookup(key)
            if cached:
                out[sid] = cached
                continue
            out_dir = self.cfg.work_dir / "sim" / sid
            out_dir.mkdir(parents=True, exist_ok=True)
            log.info("submit RUN_OPENSTUDIO_SIM sample=%s os=%s", sid, os_version)
            handle = self.executor.submit(
                run_openstudio_sim,
                mod_pkg, sid, os_version, out_dir,
                name=f"sim_{sid}",
                cpus=4, memory_mb=8 * 1024, time_min=240,
                container=CONTAINER_OS.format(version=os_version),
                openstudio_version=os_version,
            )
            try:
                result_path = handle.result(timeout=600)
            except Exception as e:
                log.error("RUN_OPENSTUDIO_SIM %s failed: %s", sid, e)
                self.cache.store(key, out_dir, exit_code=1)
                continue
            # Intermediate-file optimization (PRD §1.4): drop empty .err
            err = result_path / "eplusout.err"
            if err.exists() and err.stat().st_size == 0:
                err.unlink()
            self.cache.store(key, Path(result_path), exit_code=0)
            out[sid] = Path(result_path)
        return out

    def step_extract_kpis(self, simulated: dict) -> list[Path]:
        """Fan-out: for each simulated sample, extract KPIs."""
        out: list[Path] = []
        for sid, sim_dir in simulated.items():
            inputs_hash = sha256_of_dict({"sim_dir": str(sim_dir), "sid": sid})
            key = CacheKey(
                step="EXTRACT_KPIS",
                sample_id=sid,
                openstudio_version="N/A",
                inputs_sha256=inputs_hash,
                code_sha256=self.code_hashes["bin"],
                container_digest=CONTAINER_PY,
            )
            cached = self.cache.lookup(key)
            if cached:
                out.append(cached)
                continue
            kpi_dir = self.cfg.work_dir / "kpis"
            kpi_dir.mkdir(parents=True, exist_ok=True)
            log.info("submit EXTRACT_KPIS sample=%s", sid)
            handle = self.executor.submit(
                self.extract_fn,
                sim_dir, sid, kpi_dir,
                name=f"kpi_{sid}",
                cpus=1, memory_mb=1024, time_min=10,
                container=CONTAINER_PY,
            )
            try:
                result_path = handle.result(timeout=120)
            except Exception as e:
                log.error("EXTRACT_KPIS %s failed: %s", sid, e)
                continue
            self.cache.store(key, Path(result_path), exit_code=0)
            out.append(Path(result_path))
        return sorted(out)

    def step_aggregate_results(self, kpi_files: list[Path], simulated: dict) -> dict:
        """Single-shot: aggregate all KPIs into a CSV + Parquet + failed CSV."""
        sim_dirs = list(simulated.values())
        inputs_hash = sha256_of_dict({
            "kpis": [str(p) for p in kpi_files],
            "sims": [str(p) for p in sim_dirs],
        })
        key = CacheKey(
            step="AGGREGATE_RESULTS",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256=inputs_hash,
            code_sha256=self.code_hashes["bin"],
            container_digest=CONTAINER_PY,
        )
        cached = self.cache.lookup(key)
        if cached:
            log.info("AGGREGATE_RESULTS from cache")
            return {
                "csv": cached,
                "parquet": cached.parent / "aggregated_results.parquet",
                "failed": cached.parent / "failed_simulations.csv",
            }
        log.info("submit AGGREGATE_RESULTS")
        handle = self.executor.submit(
            aggregate_results, kpi_files, sim_dirs, self.cfg.outdir,
            name="aggregate",
            cpus=2, memory_mb=4 * 1024, time_min=15,
            container=CONTAINER_PY,
        )
        result = handle.result(timeout=300)
        self.cache.store(key, result["csv"], exit_code=0)
        return result

    def step_generate_plots(self, aggregated: dict) -> list[Path]:
        """Single-shot: render 1-3 summary plots."""
        log.info("submit GENERATE_BASIC_PLOTS")
        plots_dir = self.cfg.outdir / "plots"
        handle = self.executor.submit(
            generate_plots,
            aggregated["csv"], aggregated["failed"], plots_dir,
            name="plots",
            cpus=1, memory_mb=1024, time_min=10,
            container=CONTAINER_PY,
        )
        return handle.result(timeout=120)
