"""Campaign orchestrator.

This is the ~300-line class that drives the six-step campaign DAG.
The shape is:

    1. GENERATE_LHS_SAMPLES — one shot, no fan-out.
    2. APPLY_PARAMETERS     — fan out over N samples.
    3. RUN_OPENSTUDIO_SIM   — fan out over N samples (heavy).
    4. EXTRACT_KPIS         — fan out over N samples.
    5. AGGREGATE_RESULTS    — one shot after all KPIs.
    6. GENERATE_BASIC_PLOTS — one shot after aggregation.

Each step is cached via `SQLiteCache`. Each per-sample step submits to
the configured `BaseExecutor`. Fan-out is bounded by the executor's
max_workers so we do not overwhelm the underlying scheduler.

BYOS extension is exposed at the `apply_fn=` / `extract_fn=` constructor
parameters — the user supplies a Python file with a function of the right
signature and we discover + call it via `inspect.signature`.

When `apply_fn` / `extract_fn` are not passed explicitly, the Campaign
falls back to loading them from `cfg.custom_apply_script` /
`cfg.custom_kpi_extractor` via the canonical ``osimflow.byos`` loader.
This ensures `CampaignConfig.custom_apply_script` is always consumed,
even when the Campaign is constructed programmatically (without the CLI
doing the pre-load).

Per `.agents/results/monitoring-decision.md`, the campaign writes a
single `run.json` trace to `${outdir}/run.json` at completion. The trace
includes per-step timing, per-sample status, and cache hit/miss counts.
"""

import inspect
import json
import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict

import yaml

from .apply_params import (
    EPW_FILE_KEY,
    _build_mappings,
    preflight_check,
)
from .cache import CacheKey, SQLiteCache, sha256_of_dict, sha256_of_files
from .config import CampaignConfig
from .executors import BaseExecutor
from .mlflow_hook import (
    log_mlflow_artifacts,
    log_mlflow_metrics,
    log_mlflow_params,
    maybe_end_mlflow_run,
    maybe_start_mlflow_run,
)
from .monitoring import RunTrace, SampleTrace, sample_log_paths
from .work import (
    aggregate_results,
    default_apply_parameters,
    extract_kpis,
    generate_lhs,
    generate_plots,
    run_openstudio_sim,
)

log = logging.getLogger("osimflow.campaign")

# Image registries. The OpenStudio CLI image is consumed directly from
# NREL's upstream `nrel/openstudio` on Docker Hub — see
# `docs/openstudio-image-distribution.md` and ADR-0002 for the rationale.
# The scientific Python image remains a project-owned ghcr.io artifact.
CONTAINER_OS = "docker.io/nrel/openstudio:{version}"
CONTAINER_PY = "ghcr.io/anchapin/scientific_python_image:latest"


# Type aliases — these are the schemas of intermediate DAG outputs.
class SampleSpec(TypedDict):
    sample_id: str
    values: dict[str, object]


class VariableSpec(TypedDict, total=False):
    name: str
    distribution: str
    min: float
    max: float
    mean: float
    sigma: float


SampleDict = dict[str, Path]  # sample_id -> path (per-sample work dir)


class Campaign:
    def __init__(
        self,
        cfg: CampaignConfig,
        executor: BaseExecutor,
        apply_fn: Callable[..., Path] | None = None,
        extract_fn: Callable[..., Path] | None = None,
    ):
        self.cfg = cfg
        self.executor = executor
        # Resolve apply_fn: explicit param > cfg.custom_apply_script > default.
        if apply_fn is not None:
            self.apply_fn = apply_fn
        elif cfg.custom_apply_script is not None:
            from .byos import load_user_function  # noqa: PLC0415

            log.info("loading BYOS apply_fn from %s", cfg.custom_apply_script)
            self.apply_fn = load_user_function(cfg.custom_apply_script)
        else:
            self.apply_fn = default_apply_parameters
        # Resolve extract_fn: explicit param > cfg.custom_kpi_extractor > default.
        if extract_fn is not None:
            self.extract_fn = extract_fn
        elif cfg.custom_kpi_extractor is not None:
            from .byos import load_user_function  # noqa: PLC0415

            log.info("loading BYOS extract_fn from %s", cfg.custom_kpi_extractor)
            self.extract_fn = load_user_function(cfg.custom_kpi_extractor)
        else:
            self.extract_fn = extract_kpis
        self.cache = SQLiteCache(cfg.cache_db)
        # Hash the code that affects per-step behavior so a `bin/*.py` edit
        # invalidates cached results. This is the fix for the
        # "Python glue invisible to cache hash" gotcha in
        # `.agents/results/result-architecture.md` issue #2.
        self.code_hashes = self._compute_code_hashes()
        self.trace = RunTrace(
            campaign_id=time.strftime("%Y-%m-%dT%H-%M-%S"),
            config_summary={
                "executor": executor.name,
                "openstudio_version": cfg.openstudio_version,
                "n_samples": cfg.n_samples,
                "archive_intermediates": cfg.archive_intermediates,
                "custom_apply_script": (
                    str(cfg.custom_apply_script) if cfg.custom_apply_script else None
                ),
                "custom_kpi_extractor": (
                    str(cfg.custom_kpi_extractor) if cfg.custom_kpi_extractor else None
                ),
                "baseline_sample_id": (str(cfg.baseline["sample_id"]) if cfg.baseline else None),
            },
        )
        # Per-sample accumulator. The three per-sample steps write here;
        # we emit SampleTrace rows in _finalize_samples().
        self._sample_state: dict[str, dict[str, object]] = {}

    def _compute_code_hashes(self) -> dict[str, str]:
        """SHA-256 of every bin/*.py file, plus the work.py module.

        The work.py module is included because it is the work layer that
        the Campaign itself depends on; if a contributor edits it, we
        must re-run downstream steps.
        """
        # Lazy import: keeps the work module out of the type-checker
        # top-level chain and avoids importing pandas at osimflow import
        # time for callers that only use Campaign/Cache.
        from . import work  # noqa: PLC0415

        bin_dir = Path(__file__).resolve().parent.parent / "bin"
        files = sorted(bin_dir.glob("*.py"))
        work_file = Path(inspect.getfile(work))
        return {
            "bin": sha256_of_files(files),
            "work": sha256_of_files([work_file]),
        }

    def _finalize_samples(self) -> None:
        """Emit one SampleTrace per sample based on accumulated per-step state."""
        for sid, state in self._sample_state.items():
            apply_ok = state.get("apply_exit_code") == 0
            sim_ok = state.get("sim_exit_code") == 0
            extract_ok = state.get("extract_exit_code") == 0
            # A sample is "ok" if every step that ran succeeded.
            status = "ok" if apply_ok and sim_ok and extract_ok else "failed"
            # Coerce optional stringy fields via str() rather than dropping
            # non-None values: previous code accepted any truthy value, and
            # JSON-serializing Path/str objects in run.json requires str().
            eplusout_sql_obj = state.get("eplusout_sql")
            eplusout_sql = None if eplusout_sql_obj is None else str(eplusout_sql_obj)
            error_summary_obj = state.get("error_summary")
            error_summary = None if error_summary_obj is None else str(error_summary_obj)
            # Per-sample log paths (issue #6). Optional because the
            # fields are only populated by RUN_OPENSTUDIO_SIM; samples
            # that errored out in APPLY_PARAMETERS never reach that
            # step and have no associated log files.
            stdout_log_obj = state.get("stdout_log")
            stdout_log = None if stdout_log_obj is None else str(stdout_log_obj)
            stderr_log_obj = state.get("stderr_log")
            stderr_log = None if stderr_log_obj is None else str(stderr_log_obj)
            self.trace.sample_done(
                SampleTrace(
                    sample_id=sid,
                    status=status,
                    elapsed_s=0.0,  # per-sample total wall-clock — not yet tracked
                    apply_exit_code=int(str(state.get("apply_exit_code", 0))),
                    sim_exit_code=int(str(state.get("sim_exit_code", 0))),
                    extract_exit_code=int(str(state.get("extract_exit_code", 0))),
                    eplusout_sql=eplusout_sql,
                    error_summary=error_summary,
                    stdout_log=stdout_log,
                    stderr_log=stderr_log,
                )
            )

    def _compute_baseline_comparison(self, kpi_files: list[Path]) -> None:
        """Compute baseline comparison metrics and store on the run trace.

        Reads the baseline sample's KPIs and computes improvement statistics
        across all parametric samples. Populates ``self.trace.baseline_comparison``
        (issue #64).

        When no baseline is configured, this is a no-op.
        """
        baseline_sid = self._baseline_sample_id()
        if baseline_sid is None:
            return

        all_kpis = self._read_all_kpis(kpi_files)
        if baseline_sid not in all_kpis:
            log.warning(
                "baseline sample_id=%s not found in KPI files; skipping baseline comparison",
                baseline_sid,
            )
            return

        baseline_kpis = all_kpis[baseline_sid]
        comparison = self._compute_improvement_range(baseline_sid, baseline_kpis, all_kpis)
        if comparison:
            self.trace.baseline_comparison = comparison
            log.info("baseline comparison: %s", comparison)

    @staticmethod
    def _read_all_kpis(kpi_files: list[Path]) -> dict[str, dict[str, float]]:
        """Read KPI files into a {sample_id: {kpi_name: value}} mapping."""
        all_kpis: dict[str, dict[str, float]] = {}
        for kpi_path in kpi_files:
            try:
                data = json.loads(kpi_path.read_text())
                sid = str(data.get("sample_id", kpi_path.stem.replace("kpi_", "")))
                kpis = data.get("kpis", {})
                numeric_kpis = {k: float(v) for k, v in kpis.items() if isinstance(v, (int, float))}
                all_kpis[sid] = numeric_kpis
            except Exception as exc:
                log.warning("could not read KPI file %s for baseline comparison: %s", kpi_path, exc)
        return all_kpis

    @staticmethod
    def _compute_improvement_range(
        baseline_sid: str,
        baseline_kpis: dict[str, float],
        all_kpis: dict[str, dict[str, float]],
    ) -> dict[str, object]:
        """Compute pct improvement range for each KPI relative to baseline."""
        comparison: dict[str, object] = {}
        for kpi_name, baseline_val in baseline_kpis.items():
            if baseline_val == 0:
                continue
            parametric_values = [
                kpis[kpi_name]
                for sid, kpis in all_kpis.items()
                if sid != baseline_sid and kpi_name in kpis
            ]
            if not parametric_values:
                continue
            improvements = [(baseline_val - v) / baseline_val * 100.0 for v in parametric_values]
            comparison[f"baseline_{kpi_name}"] = round(baseline_val, 2)
            comparison[f"min_{kpi_name}_improvement_pct"] = round(min(improvements), 2)
            comparison[f"max_{kpi_name}_improvement_pct"] = round(max(improvements), 2)
        return comparison

    def _archive_sample_artifacts(self, src: Path, dst: Path, patterns: list[str]) -> None:
        """Copy files matching *patterns* from *src* into *dst*.

        Creates *dst* (with parents) and copies each file whose name
        matches one of the glob *patterns*.  Uses ``shutil.copy2`` so
        timestamps are preserved (cross-substrate robustness: works on
        local, NFS, and any substrate that exposes a POSIX filesystem).

        This is a private DRY helper called from the archive-aware step
        methods when ``cfg.archive_intermediates`` is ``True``.
        """
        dst.mkdir(parents=True, exist_ok=True)
        for pattern in patterns:
            for f in src.glob(pattern):
                if f.is_file():
                    shutil.copy2(f, dst / f.name)
                    log.debug("archived %s -> %s", f, dst / f.name)

    def _baseline_sample_id(self) -> str | None:
        """Return the baseline sample_id from config, or None."""
        if self.cfg.baseline is None:
            return None
        return str(self.cfg.baseline.get("sample_id", "baseline"))

    # ------------------------------------------------------------------
    # EPW file helpers (issue #55)
    # ------------------------------------------------------------------
    def _load_variable_defs(self) -> list[dict[str, Any]]:
        """Load variable definitions from ``cfg.input_variables`` (variables.yml).

        Returns the raw ``variables`` list so the Campaign can inspect
        ``target`` and ``mapping`` metadata that the LHS generator does
        not propagate to the sample dicts.
        """
        try:
            raw: Any = yaml.safe_load(self.cfg.input_variables.read_text())
        except Exception as exc:
            log.error("Failed to load variables.yml: %s", exc)
            raise
        if not isinstance(raw, dict):
            return []
        variables: Any = raw.get("variables", [])
        if not isinstance(variables, list):
            return []
        return variables

    def _preflight_validate_epw_files(self, variable_defs: list[dict[str, Any]]) -> None:
        """Pre-flight check: verify all mapped .epw files exist.

        For every variable with ``target: epw_file`` and a ``mapping``
        dict, verify each mapped value is a file that exists inside
        the ``template_sim_package`` directory.  Fail fast with a
        clear error message listing every missing file.

        Raises:
            FileNotFoundError: one or more mapped .epw files are missing.
        """
        template_dir = self.cfg.template_sim_package
        missing: list[str] = []
        for var in variable_defs:
            if var.get("target") != "epw_file":
                continue
            mapping = var.get("mapping")
            if not mapping or not isinstance(mapping, dict):
                continue
            for cat_value, epw_rel_path in mapping.items():
                epw_abs = template_dir / str(epw_rel_path)
                if not epw_abs.is_file():
                    missing.append(
                        f"  variable={var['name']!r} value={cat_value!r} -> {epw_abs} (missing)"
                    )
        if missing:
            raise FileNotFoundError(
                "PRE-FLIGHT EPW VALIDATION FAILED: the following mapped "
                ".epw files were not found in template_sim_package="
                f"{template_dir}:\n" + "\n".join(missing)
            )

    def _resolve_epw_targets(
        self,
        params: dict[str, object],
        variable_defs: list[dict[str, Any]],
    ) -> dict[str, object]:
        """Resolve ``epw_file`` targets in a sample's parameter dict.

        For each variable with ``target: epw_file``, look up the
        parameter value in the variable's ``mapping`` dict and inject
        the resolved .epw path under :data:`EPW_FILE_KEY`
        (``"__epw_file__"``) into a copy of *params*.

        If no ``epw_file`` targets exist, returns *params* unchanged.

        Raises:
            ValueError: a parameter value is not in the variable's mapping.
        """
        epw_vars = [v for v in variable_defs if v.get("target") == "epw_file"]
        if not epw_vars:
            return params
        resolved = dict(params)
        for var in epw_vars:
            name = var["name"]
            mapping = var.get("mapping", {})
            value = params.get(name)
            if value is None:
                continue
            epw_path = mapping.get(value)
            if epw_path is None:
                raise ValueError(
                    f"Parameter {name!r} has value {value!r} which is "
                    f"not in the epw_file mapping. Available keys: "
                    f"{sorted(mapping.keys())}"
                )
            resolved[EPW_FILE_KEY] = str(epw_path)
            log.debug(
                "resolved epw_file: %s=%s -> %s",
                name,
                value,
                epw_path,
            )
        return resolved

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self) -> dict[str, object]:
        log.info("=" * 60)
        log.info("OSimFlow campaign start")
        log.info("  executor:      %s", self.executor.name)
        log.info("  n_samples:     %d", self.cfg.n_samples)
        log.info("  os_version:    %s", self.cfg.openstudio_version)
        log.info("  outdir:        %s", self.cfg.outdir)
        log.info("  work_dir:      %s", self.cfg.work_dir)
        log.info("=" * 60)

        # Optional MLflow tracking (issue #7). Lazy-imports mlflow only
        # when a tracking URI is configured; the no-tracking-URI path is
        # mlflow-free. Cleanup runs in `finally` so a step that raises
        # mid-campaign still closes the MLflow run cleanly (the MLflow
        # UI never shows a stuck RUNNING entry).
        run_name = maybe_start_mlflow_run(self.cfg.mlflow_tracking_uri, self.trace.campaign_id)
        if run_name is not None:
            # Build a small view object the hook can read via getattr;
            # the hook is duck-typed to avoid a circular import on
            # `osimflow.config`.
            log_mlflow_params(_make_mlflow_param_view(self.cfg, self.executor.name))

        t0 = time.time()
        try:
            samples: list[SampleSpec] = self.step_generate_lhs()
            parameterized: SampleDict = self.step_apply_parameters(samples)
            simulated: SampleDict = self.step_run_openstudio_sim(parameterized)
            kpi_files: list[Path] = self.step_extract_kpis(simulated)
            aggregated: dict[str, Path] = self.step_aggregate_results(
                kpi_files, simulated, baseline_sample_id=self._baseline_sample_id()
            )
            plots: list[Path] = self.step_generate_plots(
                aggregated, baseline_sample_id=self._baseline_sample_id()
            )
            t1 = time.time()

            # Baseline comparison metrics (issue #64). Compute after all
            # steps have run so we have KPI data for the baseline sample.
            self._compute_baseline_comparison(kpi_files)

            # Archive campaign inputs (template_sim_package + input_variables)
            # when --archive_intermediates is set.
            if self.cfg.archive_intermediates:
                inputs_archive = self.cfg.outdir / "archive" / "inputs"
                inputs_archive.mkdir(parents=True, exist_ok=True)
                # Copy the entire template_sim_package directory
                pkg_dst = inputs_archive / self.cfg.template_sim_package.name
                if pkg_dst.exists():
                    shutil.rmtree(pkg_dst)
                shutil.copytree(self.cfg.template_sim_package, pkg_dst)
                log.info("archived template_sim_package -> %s", pkg_dst)
                # Copy the input_variables file
                shutil.copy2(
                    self.cfg.input_variables, inputs_archive / self.cfg.input_variables.name
                )
                log.info(
                    "archived input_variables -> %s", inputs_archive / self.cfg.input_variables.name
                )

            # Finalize the trace + run.json so the MLflow artifact is
            # the canonical post-campaign trace. We do this inside the
            # try block (before the finally cleanup) so the run.json
            # file exists at the moment `log_mlflow_artifacts` reads it.
            self._finalize_samples()
            self.trace.finalize()
            self.trace.write(self.cfg.outdir / "run.json")

            # Log metrics + artifacts to MLflow before the run ends.
            # The hooks are no-ops when no run is active, so this is
            # safe to call unconditionally.
            n_succeeded = sum(1 for s in self.trace.per_sample if s.status == "ok")
            n_failed = sum(1 for s in self.trace.per_sample if s.status == "failed")
            log_mlflow_metrics(t1 - t0, n_succeeded, n_failed)
            log_mlflow_artifacts(
                aggregated["csv"],
                aggregated["failed"],
                self.cfg.outdir / "run.json",
            )
        finally:
            # End the MLflow run even if any step raised. The hook is
            # itself exception-safe so a transient MLflow error cannot
            # mask the original failure.
            maybe_end_mlflow_run()

        log.info("=" * 60)
        log.info("OSimFlow campaign complete in %.1fs", t1 - t0)
        log.info("  cache stats:   %s", self.cache.stats())
        log.info("  aggregated:    %s", aggregated["csv"])
        log.info("  failed:        %s", aggregated["failed"])
        log.info("  plots:         %s", plots)
        log.info("  run trace:     %s", self.cfg.outdir / "run.json")
        log.info("=" * 60)
        return {
            "samples": samples,
            "kpis": kpi_files,
            "aggregated": aggregated,
            "plots": plots,
            "elapsed_s": t1 - t0,
            "run_json": self.cfg.outdir / "run.json",
        }

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    def step_generate_lhs(self) -> list[SampleSpec]:
        """Single-shot: read variables.yml, produce N parameter sets.

        Calls `bin/generate_lhs.py` via the executor.
        """
        t0 = time.time()
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
            samples_obj: object = json.loads(cached.read_text())["samples"]
            samples_from_cache = cast_samples(samples_obj)
            self.trace.step_finished(
                "GENERATE_LHS_SAMPLES",
                cache="HIT",
                elapsed_s=time.time() - t0,
                exit_code=0,
            )
            return samples_from_cache

        out_dir = self.cfg.work_dir / "lhs"
        handle = self.executor.submit(
            generate_lhs,
            self.cfg.input_variables,
            self.cfg.n_samples,
            out_dir,
            name="generate_lhs",
            cpus=1,
            memory_mb=1024,
            time_min=5,
            container=CONTAINER_PY,
        )
        try:
            result_path = handle.result(timeout=120)
            self.cache.store(key, Path(result_path), exit_code=0)
            run_samples_obj: object = json.loads(Path(result_path).read_text())["samples"]
            samples = cast_samples(run_samples_obj)

            # Inject baseline sample (issue #64): when cfg.baseline is
            # defined, prepend a fixed-parameter sample to the LHS set.
            baseline_sid = self._baseline_sample_id()
            if baseline_sid is not None and self.cfg.baseline is not None:
                baseline_params = self.cfg.baseline.get("parameters", {})
                # Only inject if not already present in the LHS set.
                existing_ids = {s["sample_id"] for s in samples}
                if baseline_sid not in existing_ids:
                    params_dict: dict[str, object] = (
                        dict(baseline_params) if isinstance(baseline_params, dict) else {}
                    )
                    baseline_sample: SampleSpec = SampleSpec(
                        sample_id=baseline_sid,
                        values=params_dict,
                    )
                    samples.insert(0, baseline_sample)
                    log.info(
                        "injected baseline sample_id=%s with %d parameters",
                        baseline_sid,
                        len(params_dict),
                    )
                else:
                    log.info(
                        "baseline sample_id=%s already in sample set; skipping injection",
                        baseline_sid,
                    )

            self.trace.step_finished(
                "GENERATE_LHS_SAMPLES",
                cache="MISS",
                elapsed_s=time.time() - t0,
                exit_code=0,
            )
            return samples
        except Exception as e:
            log.error("GENERATE_LHS_SAMPLES failed: %s", e)
            self.trace.step_finished(
                "GENERATE_LHS_SAMPLES",
                cache="MISS",
                elapsed_s=time.time() - t0,
                exit_code=1,
            )
            raise

    def step_apply_parameters(self, samples: list[SampleSpec]) -> SampleDict:
        """Fan-out: for each sample, produce a modified sim package.

        Before submitting any work, runs a pre-flight validation pass
        (PRD §1.4) that checks every parameter name across all samples
        against the template's available measure arguments and .osm
        attributes. This ensures a typo in ``variables.yml`` fails fast
        *before* any simulations start.

        Additionally, for variables with ``target: epw_file`` (issue
        #55), verifies that all mapped ``.epw`` files exist in the
        ``template_sim_package`` directory and resolves each sample's
        categorical value to the corresponding weather file path.

        Raises:
            UnmappedParameterError: a parameter name does not map to any
                template attribute or measure argument. The error message
                includes fuzzy-match suggestions for likely typos.
            AmbiguousParameterError: a plain argument name appears in
                multiple measure steps and must be disambiguated via the
                dotted ``MeasureName.argument_name`` form.
            FileNotFoundError: an ``epw_file`` target references a
                ``.epw`` file that does not exist in the template
                simulation package.
        """
        t0 = time.time()

        # Load variable definitions from variables.yml for epw_file
        # target resolution and pre-flight validation (issue #55).
        variable_defs: list[dict[str, Any]] = self._load_variable_defs()

        # Pre-flight EPW validation: verify all mapped .epw files exist
        # in the template_sim_package directory.
        self._preflight_validate_epw_files(variable_defs)

        # Pre-flight validation (PRD §1.4): validate ALL parameter names
        # against the template before submitting any work. Collect the
        # union of parameter names across all samples so a single bad
        # variable in any sample blocks the entire step.
        all_param_keys: dict[str, None] = {}
        for s in samples:
            all_param_keys.update(dict.fromkeys(s["values"].keys()))
        if all_param_keys:
            mappings = _build_mappings(self.cfg.template_sim_package)
            preflight_check(all_param_keys, mappings)
            log.info(
                "pre-flight check passed: %d parameter(s) validated against %s",
                len(all_param_keys),
                self.cfg.template_sim_package,
            )

        out: SampleDict = {}
        cache_label = "MISS×N" if samples else "SKIPPED"
        self.trace.step_started("APPLY_PARAMETERS", total=len(samples))
        for s in samples:
            sid = str(s["sample_id"])
            params = s["values"]
            # Resolve epw_file targets: inject __epw_file__ with the
            # mapped .epw path (issue #55). The apply function extracts
            # this reserved key and uses it to mutate the .osw
            # weather_file field.
            resolved_params = self._resolve_epw_targets(params, variable_defs)
            inputs_hash = sha256_of_dict({"params": resolved_params, "sid": sid})
            key = CacheKey(
                step="APPLY_PARAMETERS",
                sample_id=sid,
                openstudio_version="N/A",
                inputs_sha256=inputs_hash,
                code_sha256=self.code_hashes["bin"],
                container_digest=CONTAINER_PY,
            )
            state = self._sample_state.setdefault(sid, {})
            cached = self.cache.lookup(key)
            if cached:
                out[sid] = cached
                state["apply_exit_code"] = 0
                state["apply_status"] = "cached"
                self.trace.step_item_done("APPLY_PARAMETERS", status="cached")
                continue
            out_dir = self.cfg.work_dir / "apply" / sid
            out_dir.mkdir(parents=True, exist_ok=True)
            handle = self.executor.submit(
                self.apply_fn,
                self.cfg.template_sim_package,
                resolved_params,
                sid,
                out_dir,
                name=f"apply_{sid}",
                cpus=1,
                memory_mb=512,
                time_min=5,
                container=CONTAINER_PY,
            )
            try:
                result_path = handle.result(timeout=120)
                self.cache.store(key, Path(result_path), exit_code=0)
                out[sid] = Path(result_path)
                state["apply_exit_code"] = 0
                state["apply_status"] = "ok"
                self.trace.step_item_done("APPLY_PARAMETERS", status="ok")
                # Archive modified .osw/.osm when flag is set
                if self.cfg.archive_intermediates:
                    archive_dst = self.cfg.outdir / "archive" / "apply" / sid
                    self._archive_sample_artifacts(
                        Path(result_path), archive_dst, ["*.osw", "*.osm"]
                    )
            except Exception as e:
                log.error("APPLY_PARAMETERS %s failed: %s", sid, e)
                self.cache.store(key, out_dir, exit_code=1)
                state["apply_exit_code"] = 1
                state["apply_status"] = "failed"
                state["error_summary"] = f"APPLY: {e}"
                self.trace.step_item_done("APPLY_PARAMETERS", status="failed")
        self.trace.step_finished(
            "APPLY_PARAMETERS",
            cache=cache_label,
            elapsed_s=time.time() - t0,
            exit_code=0,
        )
        return out

    def step_run_openstudio_sim(self, parameterized: SampleDict) -> SampleDict:
        """Fan-out (heavy): for each sample, run the OpenStudio simulation.

        For each sample, this step computes the per-sample stdout/stderr
        log paths via `osimflow.monitoring.sample_log_paths()` (issue #6)
        and passes them as keyword arguments to `executor.submit()`. The
        LocalExecutor's work function uses these to redirect the
        underlying `openstudio.cli` subprocess output to disk, so the
        user can `cat` the per-sample log files to debug a failed run.

        The paths are also recorded on the per-sample state so the
        SampleTrace row in `run.json` references them.
        """
        t0 = time.time()
        out: SampleDict = {}
        os_version = self.cfg.openstudio_version
        n = len(parameterized)
        self.trace.step_started("RUN_OPENSTUDIO_SIM", total=n)
        for sid, mod_pkg in parameterized.items():
            # Per-sample log file paths (issue #6). Computed here so the
            # work function receives them as kwargs. The
            # `sample_log_paths` helper creates the directory and is
            # idempotent, so it is safe to call before submit(). The
            # helper takes the user-facing `outdir` (not `work_dir`)
            # and appends `work/sim/<sid>` per `.agents/results/monitoring-decision.md`.
            stdout_log, stderr_log = sample_log_paths(self.cfg.outdir, sid)
            inputs_hash = sha256_of_dict(
                {
                    "template": str(self.cfg.template_sim_package),
                    "sid": sid,
                    "os_version": os_version,
                    # Hash the log paths into the cache key so a user
                    # who moves the outdir gets a fresh run (paths
                    # change → cache miss → re-run).
                    "stdout_log": str(stdout_log),
                    "stderr_log": str(stderr_log),
                }
            )
            key = CacheKey(
                step="RUN_OPENSTUDIO_SIM",
                sample_id=sid,
                openstudio_version=os_version,
                inputs_sha256=inputs_hash,
                code_sha256=self.code_hashes["bin"],
                container_digest=CONTAINER_OS.format(version=os_version),
            )
            state = self._sample_state.setdefault(sid, {})
            # Always populate the log path fields so consumers of the
            # run.json trace know where to look, even on a cache hit
            # (the previous run's log files are still on disk).
            state["stdout_log"] = str(stdout_log)
            state["stderr_log"] = str(stderr_log)
            cached = self.cache.lookup(key)
            if cached:
                out[sid] = cached
                state["sim_exit_code"] = 0
                state["sim_status"] = "cached"
                state["eplusout_sql"] = str(cached / "eplusout.sql")
                self.trace.step_item_done("RUN_OPENSTUDIO_SIM", status="cached")
                continue
            out_dir = self.cfg.work_dir / "sim"
            out_dir.mkdir(parents=True, exist_ok=True)
            handle = self.executor.submit(
                run_openstudio_sim,
                mod_pkg,
                sid,
                os_version,
                out_dir,
                name=f"sim_{sid}",
                cpus=4,
                memory_mb=8 * 1024,
                time_min=240,
                container=CONTAINER_OS.format(version=os_version),
                openstudio_version=os_version,
                stdout_path=stdout_log,
                stderr_path=stderr_log,
            )
            try:
                result_path = handle.result(timeout=600)
                # Intermediate-file optimization (PRD §1.4): drop empty
                # `.err` (the eplusout.err from the OpenStudio run). The
                # per-sample `stderr.log` is the *replacement* and is
                # preserved regardless of size.
                err = result_path / "eplusout.err"
                if err.exists() and err.stat().st_size == 0:
                    err.unlink()
                self.cache.store(key, Path(result_path), exit_code=0)
                out[sid] = Path(result_path)
                state["sim_exit_code"] = 0
                state["sim_status"] = "ok"
                state["eplusout_sql"] = str(result_path / "eplusout.sql")
                self.trace.step_item_done("RUN_OPENSTUDIO_SIM", status="ok")
                # Archive eplusout.sql when flag is set
                if self.cfg.archive_intermediates:
                    archive_dst = self.cfg.outdir / "archive" / "sim" / sid
                    self._archive_sample_artifacts(Path(result_path), archive_dst, ["eplusout.sql"])
            except Exception as e:
                log.error("RUN_OPENSTUDIO_SIM %s failed: %s", sid, e)
                self.cache.store(key, out_dir, exit_code=1)
                state["sim_exit_code"] = 1
                state["sim_status"] = "failed"
                state["error_summary"] = f"SIM: {e}"
                self.trace.step_item_done("RUN_OPENSTUDIO_SIM", status="failed")
        self.trace.step_finished(
            "RUN_OPENSTUDIO_SIM",
            cache="MISS×N" if n else "SKIPPED",
            elapsed_s=time.time() - t0,
            exit_code=0,
        )
        return out

    def step_extract_kpis(self, simulated: SampleDict) -> list[Path]:
        """Fan-out: for each simulated sample, extract KPIs."""
        t0 = time.time()
        out: list[Path] = []
        n = len(simulated)
        self.trace.step_started("EXTRACT_KPIS", total=n)
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
            state = self._sample_state.setdefault(sid, {})
            cached = self.cache.lookup(key)
            if cached:
                out.append(cached)
                state["extract_exit_code"] = 0
                state["extract_status"] = "cached"
                self.trace.step_item_done("EXTRACT_KPIS", status="cached")
                continue
            kpi_dir = self.cfg.work_dir / "kpis"
            kpi_dir.mkdir(parents=True, exist_ok=True)
            handle = self.executor.submit(
                self.extract_fn,
                sim_dir,
                sid,
                kpi_dir,
                name=f"kpi_{sid}",
                cpus=1,
                memory_mb=1024,
                time_min=10,
                container=CONTAINER_PY,
            )
            try:
                result_path = handle.result(timeout=120)
                self.cache.store(key, Path(result_path), exit_code=0)
                out.append(Path(result_path))
                state["extract_exit_code"] = 0
                state["extract_status"] = "ok"
                self.trace.step_item_done("EXTRACT_KPIS", status="ok")
            except Exception as e:
                log.error("EXTRACT_KPIS %s failed: %s", sid, e)
                state["extract_exit_code"] = 1
                state["extract_status"] = "failed"
                state["error_summary"] = f"EXTRACT: {e}"
                self.trace.step_item_done("EXTRACT_KPIS", status="failed")
        self.trace.step_finished(
            "EXTRACT_KPIS",
            cache="MISS×N" if n else "SKIPPED",
            elapsed_s=time.time() - t0,
            exit_code=0,
        )
        return sorted(out)

    def step_aggregate_results(
        self,
        kpi_files: list[Path],
        simulated: SampleDict,
        baseline_sample_id: str | None = None,
    ) -> dict[str, Path]:
        """Single-shot: aggregate all KPIs into a CSV + Parquet + failed CSV.

        Args:
            kpi_files: per-sample KPI JSON files from step_extract_kpis.
            simulated: mapping of sample_id to simulation output directory.
            baseline_sample_id: optional baseline sample ID (issue #64).
                When provided, the aggregator adds pct improvement columns.
        """
        t0 = time.time()
        sim_dirs = list(simulated.values())
        inputs_hash = sha256_of_dict(
            {
                "kpis": [str(p) for p in kpi_files],
                "sims": [str(p) for p in sim_dirs],
            }
        )
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
            self.trace.step_finished(
                "AGGREGATE_RESULTS",
                cache="HIT",
                elapsed_s=time.time() - t0,
                exit_code=0,
            )
            return {
                "csv": cached,
                "parquet": cached.parent / "aggregated_results.parquet",
                "failed": cached.parent / "failed_simulations.csv",
            }
        handle = self.executor.submit(
            aggregate_results,
            kpi_files,
            sim_dirs,
            self.cfg.outdir,
            baseline_sample_id=baseline_sample_id,
            name="aggregate",
            cpus=2,
            memory_mb=4 * 1024,
            time_min=15,
            container=CONTAINER_PY,
        )
        result_obj: object = handle.result(timeout=300)
        result = cast_aggregate_result(result_obj)
        self.cache.store(key, result["csv"], exit_code=0)
        self.trace.step_finished(
            "AGGREGATE_RESULTS",
            cache="MISS",
            elapsed_s=time.time() - t0,
            exit_code=0,
        )
        return result

    def step_generate_plots(
        self,
        aggregated: dict[str, Path],
        baseline_sample_id: str | None = None,
    ) -> list[Path]:
        """Single-shot: render 1-3 summary plots. Not cached — plots are
        cheap to regenerate and the user may want to tweak styling.

        Args:
            aggregated: dict with 'csv' and 'failed' paths from aggregation.
            baseline_sample_id: optional baseline sample ID (issue #64).
                When provided, the plot generator adds a vertical reference
                line for the baseline EUI on the EUI histogram.
        """
        t0 = time.time()
        plots_dir = self.cfg.outdir / "plots"
        handle = self.executor.submit(
            generate_plots,
            aggregated["csv"],
            aggregated["failed"],
            plots_dir,
            baseline_sample_id=baseline_sample_id,
            name="plots",
            cpus=1,
            memory_mb=1024,
            time_min=10,
            container=CONTAINER_PY,
        )
        result_obj: object = handle.result(timeout=120)
        result = cast_plot_paths(result_obj)
        self.trace.step_finished(
            "GENERATE_BASIC_PLOTS",
            cache="SKIPPED",
            elapsed_s=time.time() - t0,
            exit_code=0,
        )
        return result


# ---------------------------------------------------------------------------
# MLflow param view (issue #7)
# ---------------------------------------------------------------------------
def _make_mlflow_param_view(cfg: CampaignConfig, executor_name: str) -> SimpleNamespace:
    """Build a small adapter for `log_mlflow_params`.

    The hook reads attributes via `getattr`, so a `SimpleNamespace` is
    sufficient. The adapter is built from a `CampaignConfig` + the
    executor's `name` so the hook does not import `osimflow.config`
    (avoiding a circular import).
    """
    return SimpleNamespace(
        executor=executor_name,
        openstudio_version=cfg.openstudio_version,
        n_samples=cfg.n_samples,
        archive_intermediates=cfg.archive_intermediates,
    )


# ---------------------------------------------------------------------------
# Cast helpers — narrow YAML/external `object` types to the strict shapes
# the rest of the code relies on. The cast is a single audit point.
# ---------------------------------------------------------------------------
def cast_samples(obj: object) -> list[SampleSpec]:
    """Narrow a `samples` JSON value to the canonical SampleSpec list."""
    if not isinstance(obj, list):
        raise TypeError(f"samples must be a list, got {type(obj).__name__}")
    out: list[SampleSpec] = []
    for item in obj:
        if not isinstance(item, dict):
            raise TypeError("sample entry must be a dict")
        sid = item.get("sample_id")
        values = item.get("values")
        if not isinstance(sid, str) or not isinstance(values, dict):
            raise TypeError("sample entry must have str 'sample_id' and dict 'values'")
        out.append(SampleSpec(sample_id=sid, values=values))
    return out


def cast_variables(obj: object) -> list[VariableSpec]:
    """Narrow the `variables` list from variables.yml.

    Each entry is a dict with at least `name` and `distribution` keys,
    plus distribution-specific numeric fields.
    """
    if not isinstance(obj, list):
        raise TypeError(f"variables must be a list, got {type(obj).__name__}")
    out: list[VariableSpec] = []
    for item in obj:
        if not isinstance(item, dict) or "name" not in item or "distribution" not in item:
            raise TypeError("variable entry must have 'name' and 'distribution'")
        v: VariableSpec = {
            "name": str(item["name"]),
            "distribution": str(item["distribution"]),
            "min": float(item.get("min", 0.0)),
            "max": float(item.get("max", 0.0)),
            "mean": float(item.get("mean", 0.0)),
            "sigma": float(item.get("sigma", 0.0)),
        }
        out.append(v)
    return out


def cast_aggregate_result(obj: object) -> dict[str, Path]:
    """Narrow aggregate_results() return to a Path-keyed dict."""
    if not isinstance(obj, dict):
        raise TypeError(f"aggregate result must be a dict, got {type(obj).__name__}")
    out: dict[str, Path] = {}
    for k, v in obj.items():
        out[str(k)] = Path(str(v))
    return out


def cast_plot_paths(obj: object) -> list[Path]:
    """Narrow generate_plots() return to a list[Path]."""
    if not isinstance(obj, list):
        raise TypeError(f"plot list must be a list, got {type(obj).__name__}")
    out: list[Path] = []
    for item in obj:
        out.append(Path(str(item)))
    return out
