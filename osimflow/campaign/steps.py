"""Campaign DAG step bodies (issue #1890).

``CampaignStepsMixin`` holds the ``step_*`` methods (sample generation,
pre-flight, apply-parameters, run-sim, aggregate, plots), the detached
submit helpers and ``warm_cache``, moved verbatim out of
``osimflow.campaign``.  Module-level names are resolved through the
``osimflow.campaign`` package namespace at call time (``_c.<name>``) so
``mock.patch("osimflow.campaign.<name>")`` seams keep working.
"""

import hashlib
import inspect
import logging
import sys
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..algorithms import BaseAlgorithm
from ..apply_params import (
    EPW_FILE_KEY,
    OpenStudioBindingsMissingError,
    _build_mappings_without_osm_bindings,
    build_measure_argument_alias_map,
    resolve_measure_argument_keys,
)
from ..cache import CacheKey
from ..config import CampaignConfig
from ..executors import Handle
from ..observability.monitoring import RunTrace, WorkerRecoveryManager
from ..s3_campaign import S3CampaignStore, SubmitClaim
from ..weather import find_weather_file
from ..work import _PENDING_OSM_PARAMS_FILE, _apply_parameters_with_osm_deferral
from .dag import SampleDict, SimResult
from .fanout import BookkeepingOnce
from .types import SampleSpec


def _parameterized_package_content_digest(
    pkg: Path,
    base_digest_fn: Callable[[Path], str],
) -> str:
    """Compute the parameterized package digest including any deferred ``.osm`` mutations."""
    base = base_digest_fn(pkg)
    pending_file = pkg / _PENDING_OSM_PARAMS_FILE
    if not pending_file.is_file():
        return base
    h = hashlib.sha256()
    h.update(base.encode())
    h.update(b":")
    h.update(pending_file.read_bytes())
    return h.hexdigest()


def kpi_content_digests(kpi_files: list[Path]) -> list[str]:
    """Per-KPI-file ``<parent>/<name>:<sha256>`` entries (issue #1962).

    Content-addressed so in-place KPI rewrites (e.g. ``mark-for-reanalysis``)
    invalidate the AGGREGATE_RESULTS key; the parent dir name keeps sample
    identity without embedding absolute paths.
    """
    return sorted(
        f"{p.parent.name}/{p.name}:{hashlib.sha256(p.read_bytes()).hexdigest()}" for p in kpi_files
    )


# Files aggregate_results reads from each sim dir (SQL series, severe errors, failure summary).
_AGGREGATE_SIM_FILES = ("eplusout.sql", "eplusout.err", "out.osw", "run.log")


def sim_dir_content_digests(sim_dirs: list[Path]) -> list[str]:
    """Per-sim-dir ``<name>/<file>:<sha256|absent>`` entries (issue #1998).

    Content-addressed so an in-place re-simulation that reuses the same
    directory path still invalidates the AGGREGATE_RESULTS key.
    """
    entries: list[str] = []
    for d in sim_dirs:
        for fname in _AGGREGATE_SIM_FILES:
            f = Path(d) / fname
            if f.is_file():
                h = hashlib.sha256()
                with f.open("rb") as fh:
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        h.update(block)
                entries.append(f"{Path(d).name}/{fname}:{h.hexdigest()}")
            else:
                entries.append(f"{Path(d).name}/{fname}:absent")
    return sorted(entries)


log = logging.getLogger("osimflow.campaign")

# Package namespace, resolved at call time so patches on osimflow.campaign.<name> apply.
_c: Any = sys.modules["osimflow.campaign"]


class CampaignStepsMixin:
    cfg: CampaignConfig
    trace: RunTrace

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any:
            """Attribute surface provided by ``Campaign`` (typing only)."""
            ...

    @staticmethod
    def _resolve_sampling_seed(variables: dict[str, Any]) -> int | None:
        """Resolve and validate top-level ``seed`` from ``variables.yml``."""
        raw_seed = variables.get("seed")
        if raw_seed is None:
            metadata = variables.get("_metadata")
            raw_seed = metadata.get("seed") if isinstance(metadata, dict) else None
        if raw_seed is None:
            return None
        if isinstance(raw_seed, bool):
            raise ValueError(f"variables.yml 'seed' must be an integer, got boolean {raw_seed!r}")
        if isinstance(raw_seed, int):
            return int(raw_seed)
        raise ValueError(f"variables.yml 'seed' must be an integer, got {raw_seed!r}")

    def step_generate_samples(
        self,
        algo: BaseAlgorithm,
        generation: int = 0,
    ) -> list[SampleSpec]:
        """Generate samples via the pluggable algorithm framework.

        Dispatches to ``algo.generate_samples()`` with the campaign's
        variables and sample count.  The result is cached under a key
        that includes the algorithm name *and* the generation number so
        each generation's samples are independently cacheable.  The
        step's ``code_sha256`` additionally folds in a digest of the
        resolved algorithm implementation's own source files (issue
        #1636), so upgrading a same-named third-party plug-in
        invalidates cached sample sets instead of silently replaying
        them.

        This is the preferred entry point.  ``step_generate_lhs()`` is
        kept as a deprecated convenience wrapper.
        """
        t0 = _c.time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before GENERATE_SAMPLES")

        algo_name = algo.name().upper()
        step_label = f"GENERATE_{algo_name}_SAMPLES"

        # Read variables.yml once for the algorithm.
        variables: dict[str, Any] = {}
        if self.cfg.input_variables.exists():
            with self.cfg.input_variables.open() as fh:
                raw = _c.yaml.safe_load(fh)
                if isinstance(raw, dict):
                    variables = raw

        resolved_seed = self._resolve_sampling_seed(variables)

        inputs_hash = _c.sha256_of_files([self.cfg.input_variables])
        # Issue #1636: fold the resolved algorithm implementation's own
        # source digest into the step's code hash. Third-party plug-ins
        # (entry-point group ``osimflow.algorithms``) live outside the
        # osimflow package, so the ``bin`` closure never covers their
        # code — without this digest, upgrading a same-named plug-in
        # replayed the OLD samples from cache. The digest changes with
        # any edit to the plug-in's implementation files.
        code_sha = _c._combine_code_hash(
            self.code_hashes["bin"],
            _c._algorithm_code_digest(algo),
        )
        key = _c.CacheKey(
            step=step_label,
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256=inputs_hash,
            code_sha256=code_sha,
            container_digest=self._python_container_digest,
            generation=generation,
            n_samples=self.cfg.n_samples,
        )
        cached = self.cache.lookup(key)
        if cached:
            samples_data = _c.safe_json_loads(cached, default=None)
            if samples_data is not None:
                samples_obj: object = samples_data["samples"]
                samples_from_cache = _c.cast_samples(samples_obj)
                self.trace.step_finished(
                    step_label,
                    cache="HIT",
                    elapsed_s=_c.time.time() - t0,
                    exit_code=0,
                )
                self._obs.record_step_duration(
                    step_label, _c.time.time() - t0, generation=generation
                )
                return cast(list[SampleSpec], samples_from_cache)
            _c.log.warning("Cache entry %s is corrupted; treating as cache-miss", key)

        out_dir = self.cfg.work_dir / algo.name()
        if self.cfg.prebuilt_workflow or variables.get("prebuilt_workflow") is True:
            # Issue #1812 / #2056: one empty-valued sample per prebuilt package run,
            # with stable zero-padded IDs; no sampler is involved.
            out_dir.mkdir(parents=True, exist_ok=True)
            result_path = out_dir / "samples.json"
            result_path.write_text(
                _c.json.dumps(
                    {
                        "samples": [
                            {"sample_id": f"{i + 1:04d}", "values": {}}
                            for i in range(max(1, self.cfg.n_samples))
                        ]
                    },
                    indent=2,
                )
            )
        else:
            result_path = algo.generate_samples(
                variables=variables,
                n_samples=self.cfg.n_samples,
                seed=resolved_seed,
                outdir=out_dir,
            )
        try:
            self.cache.store(key, _c.Path(result_path), exit_code=0)
            run_samples_data = _c.safe_json_loads(
                _c.Path(result_path), default=None, log_warnings=False
            )
            if run_samples_data is None:
                raise ValueError(f"Generated samples file {result_path!r} is corrupted")
            run_samples_obj: object = run_samples_data["samples"]
            samples = _c.cast_samples(run_samples_obj)

            # Inject baseline sample (issue #64): when cfg.baseline is
            # defined, prepend a fixed-parameter sample to the sample set.
            baseline_sid = self._baseline_sample_id()
            if baseline_sid is not None and self.cfg.baseline is not None:
                baseline_params = self.cfg.baseline.get("parameters", {})
                # Only inject if not already present in the sample set.
                existing_ids = {s["sample_id"] for s in samples}
                if baseline_sid not in existing_ids:
                    params_dict: dict[str, object] = (
                        dict(baseline_params) if isinstance(baseline_params, dict) else {}
                    )
                    baseline_sample: SampleSpec = _c.SampleSpec(
                        sample_id=baseline_sid,
                        values=params_dict,
                    )
                    samples.insert(0, baseline_sample)
                    _c.log.info(
                        "injected baseline sample_id=%s with %d parameters",
                        baseline_sid,
                        len(params_dict),
                    )
                else:
                    _c.log.info(
                        "baseline sample_id=%s already in sample set; skipping injection",
                        baseline_sid,
                    )

            self.trace.step_finished(
                step_label,
                cache="MISS",
                elapsed_s=_c.time.time() - t0,
                exit_code=0,
            )
            self._obs.record_step_duration(step_label, _c.time.time() - t0, generation=generation)
            return cast(list[SampleSpec], samples)
        except Exception as e:
            _c.log.error("%s failed: %s", step_label, e)
            self.trace.step_finished(
                step_label,
                cache="MISS",
                elapsed_s=_c.time.time() - t0,
                exit_code=1,
            )
            raise

    def step_generate_lhs(self) -> list[SampleSpec]:
        """Single-shot: read variables.yml, produce N parameter sets.

        .. deprecated::
            Use ``step_generate_samples(algo)`` instead.  This method
            is retained for backward compatibility and calls
            ``step_generate_samples`` with ``LHSAlgorithm``.
        """
        _c.warnings.warn(
            "step_generate_lhs() is deprecated; use step_generate_samples(algo) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        algo = _c.AlgorithmRegistry.get("lhs")
        return self.step_generate_samples(algo)

    def step_preflight_run_model(self) -> None:
        """Single-shot: run a throwaway simulation of the seed model.

        Makes a temporary copy of the ``template_sim_package`` and runs
        ``openstudio.cli run -w workflow.osw`` (or the stub when the CLI
        is not available).  If the run encounters severe errors, raises
        :class:`SevereEnergyPlusError` to abort the campaign before
        spending cloud budget.

        This step is cached: a successful preflight result is stored in
        the cache so a re-run with the same template and version does not
        re-execute the simulation.  The cache key includes the template
        package hash and the OpenStudio version.

        Skipped only when ``cfg.skip_preflight`` is ``True`` (the
        ``--skip-preflight`` CLI flag); ``--detach-s3`` does not imply it
        (issue #2118).

        Raises:
            SevereEnergyPlusError: the seed model has errors that would
                cause every sample to fail.
        """
        if self.cfg.skip_preflight:
            _c.log.info("PREFLIGHT_RUN_MODEL: skipped (--skip-preflight)")
            self.trace.step_finished(
                "PREFLIGHT_RUN_MODEL",
                cache="SKIPPED",
                elapsed_s=0.0,
                exit_code=0,
            )
            return

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before PREFLIGHT_RUN_MODEL")

        t0 = _c.time.time()
        os_version = self.cfg.openstudio_version
        inputs_hash = _c.sha256_of_files(sorted(self.cfg.template_sim_package.rglob("*")))
        key = _c.CacheKey(
            step="PREFLIGHT_RUN_MODEL",
            sample_id="ALL",
            openstudio_version=os_version,
            inputs_sha256=inputs_hash,
            code_sha256=self.code_hashes["bin"],
            container_digest=self._os_container_digest,
        )
        cached = self.cache.lookup(key)
        if cached:
            _c.log.info("PREFLIGHT_RUN_MODEL: cache HIT")
            elapsed = _c.time.time() - t0
            self.trace.step_finished(
                "PREFLIGHT_RUN_MODEL",
                cache="HIT",
                elapsed_s=elapsed,
                exit_code=0,
            )
            self._obs.record_step_duration("PREFLIGHT_RUN_MODEL", elapsed)
            return

        try:
            _c.preflight_run_model(
                self.cfg.template_sim_package,
                os_version,
            )
        except _c.SevereEnergyPlusError:
            _c.log.error("PREFLIGHT_RUN_MODEL: seed model has severe errors")
            self.trace.step_finished(
                "PREFLIGHT_RUN_MODEL",
                cache="MISS",
                elapsed_s=_c.time.time() - t0,
                exit_code=1,
            )
            raise

        # Store a marker file so the cache has a path to record.
        preflight_marker = self.cfg.work_dir / "preflight_OK"
        preflight_marker.parent.mkdir(parents=True, exist_ok=True)
        preflight_marker.write_text(f"preflight passed at {_c.time.strftime('%Y-%m-%dT%H:%M:%S')}")
        self.cache.store(key, preflight_marker, exit_code=0)
        elapsed = _c.time.time() - t0
        self.trace.step_finished(
            "PREFLIGHT_RUN_MODEL",
            cache="MISS",
            elapsed_s=elapsed,
            exit_code=0,
        )
        self._obs.record_step_duration("PREFLIGHT_RUN_MODEL", elapsed)

    def step_validate_measure_variables(self) -> None:
        """Validate variables.yml against discovered measure arguments.

        Runs before ``RUN_OPENSTUDIO_SIM`` as a pre-flight check (GAP-003).
        Raises ``UnmappedVariableError`` if any variable name in
        ``variables.yml`` does not correspond to a discovered measure
        argument.

        Note: Variable validation always runs regardless of ``skip_preflight``.
        Only the actual preflight OpenStudio simulation run is skipped when
        ``skip_preflight=True``.
        """
        if not self.cfg.input_variables.exists():
            return

        t0 = _c.time.time()
        try:
            with self.cfg.input_variables.open() as fh:
                raw = _c.yaml.safe_load(fh)
            variables: list[dict[str, Any]]
            if isinstance(raw, dict):
                variables = raw.get("variables", [])
            else:
                variables = []
        except Exception as exc:
            _c.log.error(f"Variable validation failed: {exc}")
            raise _c.CampaignError(f"Variable validation failed: {exc}") from exc

        if not variables:
            return

        registry = _c.MeasureRegistry()
        registry.index_measures(self.cfg.template_sim_package)

        if not registry._measures:
            return

        has_osm = (
            (self.cfg.template_sim_package / "model.osm").is_file()
            or any(self.cfg.template_sim_package.glob("*.osm"))
            or _c.resolve_seed_osm(self.cfg.template_sim_package) is not None
        )

        try:
            registry.validate_variables_mapping(variables, registry, has_osm=has_osm)
        except _c.UnmappedVariableError:
            self.trace.step_finished(
                "VALIDATE_MEASURE_VARIABLES",
                cache="MISS",
                elapsed_s=_c.time.time() - t0,
                exit_code=1,
            )
            self._obs.record_step_duration("VALIDATE_MEASURE_VARIABLES", _c.time.time() - t0)
            raise

        self.trace.step_finished(
            "VALIDATE_MEASURE_VARIABLES",
            cache="MISS",
            elapsed_s=_c.time.time() - t0,
            exit_code=0,
        )
        self._obs.record_step_duration("VALIDATE_MEASURE_VARIABLES", _c.time.time() - t0)

    @staticmethod
    def _stage_external_epw(
        params: dict[str, Any], template_pkg: Path, apply_out_dir: Path
    ) -> dict[str, Any]:
        """Copy a weather override living outside the package into the sample package.

        Remote executors only see the staged package directory, so an
        absolute ``__epw_file__`` is copied to ``<package>/weather/`` and the
        parameter rewritten to that relative path (issue #1869). Package-relative
        values are validated to exist.
        """
        raw = params.get("__epw_file__")
        if not isinstance(raw, str) or not raw:
            return params
        src = _c.Path(raw)
        resolved = dict(params)
        if src.is_absolute():
            if not src.is_file():
                raise FileNotFoundError(f"weather override not found: {raw}")
            dest_dir = apply_out_dir / "weather"
            dest_dir.mkdir(parents=True, exist_ok=True)
            _c.shutil.copy2(src, dest_dir / src.name)
            resolved["__epw_file__"] = f"weather/{src.name}"
        elif (
            find_weather_file(template_pkg, src) is None
            and find_weather_file(apply_out_dir, src) is None
        ):
            raise FileNotFoundError(
                f"weather override {raw!r} not found in the sample package {template_pkg}"
            )
        return resolved

    def step_apply_parameters(  # noqa: PLR0912, PLR0915
        self,
        samples: list[SampleSpec],
        generation: int = 0,
    ) -> SampleDict:
        """Fan-out: for each sample, produce a modified sim package.

        Parameters
        ----------
        samples
            The sample specifications to parameterise.
        generation
            The generation number (0-based).  Included in the cache key
            so that the same sample in different generations gets
            independently cached entries.

        Before submitting any work, runs a pre-flight validation pass
        that checks every parameter name across all samples
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
        t0 = _c.time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before APPLY_PARAMETERS")

        # Soft pause (issue #553): skip this step entirely if pause was requested.
        if self._check_pause_requested():
            self._write_paused_trace()
            raise _c.CampaignPauseRequested("pause requested before APPLY_PARAMETERS")

        # Load variable definitions from variables.yml for epw_file
        # target resolution and pre-flight validation (issue #55).
        variable_defs: list[dict[str, Any]] = self._load_variable_defs()

        # Pre-flight EPW validation: verify all mapped .epw files exist
        # in the template_sim_package directory.
        self._preflight_validate_epw_files(
            variable_defs,
            osw_weather_replaced=bool(samples) and all(s.get("weather_file") for s in samples),
        )

        # Pre-flight validation: validate ALL parameter names
        # against the template before submitting any work. Collect the
        # union of parameter names across all samples so a single bad
        # variable in any sample blocks the entire step.
        epw_var_names = {
            str(v["name"]) for v in variable_defs if v.get("target") == "epw_file" and "name" in v
        }
        # Variables with an explicit ``measure_argument`` target that map onto
        # that ``<Measure>.<argument>`` in preflight and OSW mutation (#2078).
        alias_map = build_measure_argument_alias_map(variable_defs)
        all_param_keys: dict[str, None] = {}
        for s in samples:
            for k in s["values"]:
                if k not in epw_var_names and k != EPW_FILE_KEY:
                    all_param_keys[alias_map.get(k, k)] = None
        mappings: dict[str, Any] = {}
        if all_param_keys:
            try:
                mappings = _c._build_mappings(self.cfg.template_sim_package)
            except OpenStudioBindingsMissingError:
                mappings = _build_mappings_without_osm_bindings(
                    self.cfg.template_sim_package,
                    all_param_keys,
                )
            _c.preflight_check(all_param_keys, mappings)
            _c.log.info(
                "pre-flight check passed: %d parameter(s) validated against %s",
                len(all_param_keys),
                self.cfg.template_sim_package,
            )

        if self.cfg.prebuilt_workflow and all_param_keys:
            raise ValueError(
                "--prebuilt-workflow executes the OSW as supplied and cannot apply "
                f"variables; got {sorted(all_param_keys)} (issue #1812)"
            )

        out: SampleDict = {}
        cache_label = "MISS×N" if samples else "SKIPPED"
        self.trace.step_started("APPLY_PARAMETERS", total=len(samples))

        # --- Phase 1: cache check for all samples ---
        # Collect non-cached samples and their per-sample context.
        pending: dict[str, dict[str, Any]] = {}
        for s in samples:
            sid = str(s["sample_id"])
            params = s["values"]
            # GAP-009: per-sample seed_model override. When set, this
            # replaces the campaign-level template_sim_package for this
            # sample's apply_parameters step.
            seed_model_override: str | None = s.get("seed_model")
            # Resolve epw_file targets: inject __epw_file__ with the
            # mapped .epw path (issue #55). The apply function extracts
            # this reserved key and uses it to mutate the .osw
            # weather_file field.
            # GAP-009: if a per-sample weather_file override is set, use it
            # for epw resolution instead of the campaign-level weather file.
            weather_file_override: str | None = s.get("weather_file")
            resolved_params = self._resolve_epw_targets(
                params, variable_defs, weather_file_override=weather_file_override
            )
            # Custom (BYOS) apply functions keep receiving the variable names.
            if self.apply_fn is _c.default_apply_parameters:
                resolved_params = resolve_measure_argument_keys(resolved_params, alias_map)
            # Include seed_model_override in the cache key so a different
            # seed model for the same sample params is a distinct cache entry.
            apply_inputs: dict[str, Any] = {
                "params": resolved_params,
                "sid": sid,
                "seed_model": seed_model_override,
            }
            if self.cfg.prebuilt_workflow:
                apply_inputs["prebuilt_workflow"] = True
            inputs_hash = _c.sha256_of_dict(apply_inputs)
            key = _c.CacheKey(
                step="APPLY_PARAMETERS",
                sample_id=sid,
                openstudio_version=self.cfg.openstudio_version,
                inputs_sha256=inputs_hash,
                code_sha256=self._code_hash_with_byos("byos_apply"),
                container_digest=self._python_container_digest,
                generation=generation,
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
            pending[sid] = {
                "resolved_params": resolved_params,
                "out_dir": out_dir,
                "key": key,
                "state": state,
                "seed_model_override": seed_model_override,
            }

        # --- Phase 2/3: bounded submit and await in chunks ---
        pending_items = list(pending.items())
        if pending_items:
            chunk_size = self._fanout_submit_chunk_size(len(pending_items))
        else:
            chunk_size = 1  # unused when pending_items is empty, but avoids range(0,0,0)
        submit_interval_s = self._fanout_submit_interval_s()
        next_submit_at = 0.0
        for chunk_start in range(0, len(pending_items), chunk_size):
            if self._check_pause_requested():
                break
            # Quota enforcement (issue #1533): stop submitting new
            # chunks once a hard resource-quota limit trips.  Samples
            # already submitted run to completion; the skipped pending
            # samples fall through to the failure recording below
            # (mirroring the pause-break path).
            if self._check_quota_exceeded():
                break
            submissions: dict[str, tuple[Handle, Callable[[Any], None]]] = {}
            chunk = pending_items[chunk_start : chunk_start + chunk_size]
            for sid, ctx in chunk:
                if self._check_pause_requested():
                    break
                if submit_interval_s > 0.0:
                    now = _c.time.monotonic()
                    if now < next_submit_at:
                        _c.time.sleep(next_submit_at - now)
                    next_submit_at = max(next_submit_at, now) + submit_interval_s
                handle: Handle
                # GAP-009: use per-sample seed_model override if set,
                # otherwise fall back to the campaign-level template_sim_package.
                template_pkg: Path = (
                    _c.Path(ctx["seed_model_override"])
                    if ctx["seed_model_override"]
                    else self.cfg.template_sim_package
                )
                apply_out_dir: Path = ctx["out_dir"]
                _c.shutil.copytree(template_pkg, apply_out_dir, dirs_exist_ok=True)
                ctx["resolved_params"] = self._stage_external_epw(
                    ctx["resolved_params"], template_pkg, apply_out_dir
                )
                exec_params: dict[str, Any] = dict(ctx["resolved_params"])
                if self.apply_fn is _c.default_apply_parameters and epw_var_names:
                    for epw_var in epw_var_names:
                        if epw_var not in mappings:
                            exec_params.pop(epw_var, None)
                if self.cfg.detach_s3:
                    handle = self._run_local_apply_handle(
                        template_pkg,
                        exec_params,
                        sid,
                        apply_out_dir,
                    )
                else:
                    if self.executor.python_steps_use_openstudio_image:
                        apply_container = self._os_digest_ref or _c.CONTAINER_OS.format(
                            version=self.cfg.openstudio_version
                        )
                        apply_container_digest = self._os_container_digest
                    else:
                        apply_container = self._python_container_image
                        apply_container_digest = self._python_container_digest
                    handle = self._dispatch_step_work(
                        fn=self.apply_fn,
                        task_args=(template_pkg, exec_params, sid, apply_out_dir),
                        task_kwargs={
                            "max_retries": self.cfg.max_sample_retries,
                        },
                        exec_args=(template_pkg, exec_params, sid, apply_out_dir),
                        exec_kwargs={
                            "name": f"apply_{sid}",
                            "cpus": 1,
                            "memory_mb": 2048,
                            "time_min": 5,
                            "container": apply_container,
                            "container_digest": apply_container_digest,
                            "result_hint": apply_out_dir,
                            "max_retries": self.cfg.max_sample_retries,
                            "transport": self._result_transport_config,
                        },
                    )

                # Build the on-success callback (captures per-sample context).
                key = ctx["key"]
                state = ctx["state"]
                archive = self.cfg.archive_intermediates

                def _on_success(
                    result_path: Any,
                    _sid: str = sid,
                    _key: CacheKey = key,
                    _state: dict[str, object] = state,
                    _apply_out_dir: Path = apply_out_dir,
                    _archive: bool = archive,
                    _once: BookkeepingOnce = BookkeepingOnce(),
                ) -> None:
                    _once.run(
                        "cache_store",
                        lambda: self.cache.store(_key, _apply_out_dir, exit_code=0),
                    )
                    if _archive:
                        archive_dst = self.cfg.outdir / "archive" / "apply" / _sid
                        _once.run(
                            "archive_apply",
                            lambda: self._archive_sample_artifacts(
                                _apply_out_dir, archive_dst, ["*.osw", "*.osm"]
                            ),
                        )
                    # Record sample status to observability backend immediately
                    # so completed samples are not missed if campaign crashes
                    # before _finalize_samples (issue #847).
                    _once.run(
                        "obs",
                        lambda: self._obs.record_sample_status(
                            _sid, "ok", trace_id=self._trace_id_for(_sid)
                        ),
                    )
                    out[_sid] = _apply_out_dir
                    _state["apply_exit_code"] = 0
                    _state["apply_status"] = "ok"
                    _once.run(
                        "trace",
                        lambda: self.trace.step_item_done("APPLY_PARAMETERS", status="ok"),
                    )

                submissions[sid] = (handle, _on_success)
            self._submit_and_await_all(submissions, "APPLY_PARAMETERS")
        total_cost, total_savings = self._cost_tracker.sum_sample_costs(self._sample_state)
        self._record_costs("APPLY_PARAMETERS", total_cost, total_savings)

        # Record failures for samples that didn't succeed.
        for _sid, ctx in pending.items():
            state = ctx["state"]
            if state.get("apply_status") != "ok":
                out.pop(_sid, None)
                already_marked = state.get("apply_status") == "failed"
                state["apply_exit_code"] = 1
                state["apply_status"] = "failed"
                state.setdefault(
                    "error_summary", "APPLY: unknown error during concurrent execution"
                )
                if state.get("failure_class") != "bookkeeping_error":
                    self.cache.store(ctx["key"], ctx["out_dir"], exit_code=1)
                self.trace.step_item_done("APPLY_PARAMETERS", status="failed")
                if not already_marked:
                    # Record sample status to observability backend (issue #847).
                    self._obs.record_sample_status(
                        _sid, "failed", trace_id=self._trace_id_for(_sid)
                    )
                    # Send sample failure alert (issue #1180).
                    self._maybe_alert(
                        "sample.failed",
                        {
                            "campaign_id": self.trace.campaign_id,
                            "sample_id": _sid,
                            "step": "APPLY_PARAMETERS",
                            "status": "failed",
                            "error": "apply exited with non-zero code",
                        },
                    )

        self.trace.step_finished(
            "APPLY_PARAMETERS",
            cache=cache_label,
            elapsed_s=_c.time.time() - t0,
            exit_code=0,
        )
        self._obs.record_step_duration(
            "APPLY_PARAMETERS", _c.time.time() - t0, generation=generation
        )
        return out

    def _run_local_apply_handle(
        self,
        template_pkg: Path,
        resolved_params: dict[str, Any],
        sid: str,
        apply_out_dir: Path,
    ) -> Handle:
        """Execute ``APPLY_PARAMETERS`` locally on the submit host (issue #2024).

        Avoids dispatching synchronous ``scientific_python_image`` jobs to
        ``AWSBatchExecutor`` during ``--detach-s3`` or OSW-only ``aws_batch``
        runs while preserving the ``Handle`` / ``_submit_and_await_all``
        bookkeeping contract.
        """
        fut: Future[Any] = Future()
        try:
            if self.apply_fn is _c.default_apply_parameters:
                result = _apply_parameters_with_osm_deferral(
                    template_pkg,
                    resolved_params,
                    sid,
                    apply_out_dir,
                    max_retries=self.cfg.max_sample_retries,
                )
            else:
                sig = inspect.signature(self.apply_fn)
                accepts_max_retries = "max_retries" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                )
                if accepts_max_retries:
                    result = self.apply_fn(
                        template_pkg,
                        resolved_params,
                        sid,
                        apply_out_dir,
                        max_retries=self.cfg.max_sample_retries,
                    )
                else:
                    result = self.apply_fn(
                        template_pkg,
                        resolved_params,
                        sid,
                        apply_out_dir,
                    )
        except Exception as exc:  # noqa: BLE001 - captured onto Future so _submit_and_await_all records per-sample failure identically
            fut.set_exception(exc)
        else:
            fut.set_result(result)
        return Handle(
            job_id=f"local-apply-{sid}",
            _future=fut,
            worker_id="local",
        )

    def _build_run_sim_submit_kwargs(
        self,
        ctx: dict[str, Any],
        sid: str,
        os_version: str,
    ) -> dict[str, Any]:
        """Build the kwargs for ``run_openstudio_sim`` executor submission.

        Shared by the primary fan-out submit
        (:meth:`step_run_openstudio_sim`) and the auto-recovery resubmit
        callback so the two paths cannot diverge again (issue #1567).
        The previous resubmit path silently dropped ``timeout_s`` and
        other kwargs, causing recovered samples to fall back to the
        default 600 s function-level timeout.

        ``ctx`` is the per-sample ``pending[sid]`` dict assembled in
        ``step_run_openstudio_sim`` (Phase 1).  Returns a fresh dict so
        callers can safely ``**`` it without aliasing.
        """
        return {
            "name": f"sim_{sid}",
            "cpus": 4,
            "memory_mb": 8 * 1024,
            "time_min": 240,
            # Issue #1536: submit the digest-pinned ref when one is known —
            # a re-published tag must not change the image the campaign
            # executes while the cache key still describes the old digest.
            "container": self._os_digest_ref or _c.CONTAINER_OS.format(version=os_version),
            "container_digest": self._os_container_digest,
            "openstudio_version": os_version,
            "stdout_path": ctx["stdout_log"],
            "stderr_path": ctx["stderr_log"],
            "max_retries": self.cfg.max_sample_retries,
            "timeout_s": self.cfg.byos_timeout_s,
            "worker_id": "local",
            "result_hint": _c.Path(ctx["out_dir"]) / sid,
        }

    def step_run_openstudio_sim(  # noqa: PLR0912, PLR0915
        self,
        parameterized: SampleDict,
        generation: int = 0,
    ) -> SimResult:
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
        t0 = _c.time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before RUN_OPENSTUDIO_SIM")

        # Soft pause (issue #553): skip this step entirely if pause was requested.
        if self._check_pause_requested():
            self._write_paused_trace()
            raise _c.CampaignPauseRequested("pause requested before RUN_OPENSTUDIO_SIM")

        out: SampleDict = {}
        os_version = self.cfg.openstudio_version
        n = len(parameterized)
        self.trace.step_started("RUN_OPENSTUDIO_SIM", total=n)

        # --- Phase 1: cache check for all samples ---
        pending: dict[str, dict[str, Any]] = {}
        for sid, mod_pkg in parameterized.items():
            # Per-sample log file paths (issue #6). Computed here so the
            # work function receives them as kwargs. The
            # `sample_log_paths` helper creates the directory and is
            # idempotent, so it is safe to call before submit(). The
            # helper takes the user-facing `outdir` (not `work_dir`)
            # and appends `work/sim/<sid>` per `.agents/results/monitoring-decision.md`.
            stdout_log, stderr_log = _c.sample_log_paths(self.cfg.outdir, sid)
            inputs_hash = _c.sha256_of_dict(
                {
                    "template": str(self.cfg.template_sim_package),
                    # Per-sample modified_sim_package (issue #783). When
                    # GAP-009 seed_model_override assigns different seed
                    # models per sample, each sample must get its own
                    # cache entry.
                    "modified_sim_package": str(mod_pkg),
                    "sid": sid,
                    "os_version": os_version,
                    # Content of the parameterized package (issue #1869): the
                    # path is stable across re-runs, so changed variable
                    # values must still miss the cache.
                    "package_content": _parameterized_package_content_digest(
                        _c.Path(str(mod_pkg)),
                        _c._package_content_digest,
                    ),
                    # Hash the log paths into the cache key so a user
                    # who moves the outdir gets a fresh run (paths
                    # change → cache miss → re-run).
                    "stdout_log": str(stdout_log),
                    "stderr_log": str(stderr_log),
                }
            )
            key = _c.CacheKey(
                step="RUN_OPENSTUDIO_SIM",
                sample_id=sid,
                openstudio_version=os_version,
                inputs_sha256=inputs_hash,
                code_sha256=self.code_hashes["bin"],
                container_digest=self._os_container_digest,
                generation=generation,
            )
            state = self._sample_state.setdefault(sid, {})
            # Always populate the log path fields so consumers of the
            # run.json trace know where to look, even on a cache hit
            # (the previous run's log files are still on disk).
            state["stdout_log"] = str(stdout_log)
            state["stderr_log"] = str(stderr_log)
            cached = None if self.cfg.detach_s3 else self.cache.lookup(key)
            if cached:
                out[sid] = cached
                state["sim_exit_code"] = 0
                state["sim_status"] = "cached"
                state["eplusout_sql"] = str(cached / "eplusout.sql")
                self.trace.step_item_done("RUN_OPENSTUDIO_SIM", status="cached")
                continue
            out_dir = self.cfg.work_dir / "sim"
            out_dir.mkdir(parents=True, exist_ok=True)
            pending[sid] = {
                "mod_pkg": mod_pkg,
                "out_dir": out_dir,
                "key": key,
                "state": state,
                "stdout_log": stdout_log,
                "stderr_log": stderr_log,
            }

        # --- Phase 2/3: bounded submit and await in chunks ---
        # Worker auto-recovery (issue #443): set up recovery manager and resubmit
        # callback when auto-recovery is enabled.
        recovery_manager: WorkerRecoveryManager | None = None
        resubmit_callback: Callable[[str], Handle | None] | None = None

        if self.cfg.worker_auto_recovery:
            recovery_manager = _c.WorkerRecoveryManager(self.cfg.outdir)

            def resubmit_callback(sid: str) -> _c.Handle | None:
                """Resubmit a failed job for auto-recovery."""
                ctx = pending.get(sid)
                if ctx is None:
                    _c.log.error("auto-recovery: no pending context for sample %s", sid)
                    return None
                _c.log.info(
                    "auto-recovery: resubmitting sample %s (outdir=%s)",
                    sid,
                    ctx["out_dir"],
                )
                # Issue #1679: collapsed via ``_dispatch_step_work``.
                # The executor kwargs builder (issue #1567) stays in
                # place so the resubmit path cannot silently drop
                # ``timeout_s`` or any other submit kwarg the primary
                # path sets.
                return self._dispatch_step_work(
                    fn=_c.run_openstudio_sim,
                    task_args=(ctx["mod_pkg"], sid, os_version, ctx["out_dir"]),
                    task_kwargs={
                        "stdout_path": ctx["stdout_log"],
                        "stderr_path": ctx["stderr_log"],
                        "max_retries": self.cfg.max_sample_retries,
                        "timeout_s": self.cfg.byos_timeout_s,
                        "worker_id": "local",
                    },
                    exec_args=(ctx["mod_pkg"], sid, os_version, ctx["out_dir"]),
                    exec_kwargs={
                        **self._build_run_sim_submit_kwargs(ctx, sid, os_version),
                        "transport": self._result_transport_config,
                    },
                )

        detached_jobs: dict[str, str | None] = {}
        detach_claim: SubmitClaim | None = None
        recorded_jobs: dict[str, str] = {}
        if self.cfg.detach_s3:
            detach_claim = self._acquire_detach_claim()
            recorded_jobs = self._detach_store().recorded_jobs(self.cfg.outdir.name)
        pending_items = list(pending.items())
        if pending_items:
            chunk_size = self._fanout_submit_chunk_size(len(pending_items))
        else:
            chunk_size = 1  # unused when pending_items is empty, but avoids range(0,0,0)
        submit_interval_s = self._fanout_submit_interval_s()
        next_submit_at = 0.0
        for chunk_start in range(0, len(pending_items), chunk_size):
            if self._check_pause_requested():
                break
            # Quota enforcement (issue #1533): stop submitting new
            # chunks once a hard resource-quota limit trips (cost /
            # wall-time limits usually bite here — this is the
            # expensive step).  Samples already submitted run to
            # completion; the skipped pending samples fall through to
            # the failure recording below (mirroring the pause-break
            # path).
            if self._check_quota_exceeded():
                break
            submissions: dict[str, tuple[Handle, Callable[[Any], None]]] = {}
            chunk = pending_items[chunk_start : chunk_start + chunk_size]
            for sid, ctx in chunk:
                if self._check_pause_requested():
                    break
                if submit_interval_s > 0.0:
                    now = _c.time.monotonic()
                    if now < next_submit_at:
                        _c.time.sleep(next_submit_at - now)
                    next_submit_at = max(next_submit_at, now) + submit_interval_s
                # Chaos wiring (issue #1013): per-sample injection. No-op
                # unless ``cfg.chaos.schedule == "per_sample"``, in which
                # case every sample submits under the configured fault
                # schedule (network_delay / cpu_spike / kill_switch).
                if detach_claim is not None and sid in recorded_jobs:
                    detached_jobs[sid] = recorded_jobs[sid]
                    continue
                self._maybe_inject_chaos("RUN_OPENSTUDIO_SIM", "per_sample", target_id=sid)
                handle: Handle
                # RUN_OPENSTUDIO_SIM consumes ``--max-sample-retries`` at both
                # the auto-recovery resubmit (above) and the per-chunk fan-out
                # submit (below) — issue #1394.  Documented in
                # docs/user-guide.md §"--max-sample-retries: which DAG steps
                # honor it".
                # Issue #1679: collapsed via ``_dispatch_step_work``.
                handle = self._dispatch_step_work(
                    fn=_c.run_openstudio_sim,
                    task_args=(ctx["mod_pkg"], sid, os_version, ctx["out_dir"]),
                    task_kwargs={
                        "stdout_path": ctx["stdout_log"],
                        "stderr_path": ctx["stderr_log"],
                        "max_retries": self.cfg.max_sample_retries,
                        "timeout_s": self.cfg.byos_timeout_s,
                        "worker_id": "local",
                    },
                    exec_args=(ctx["mod_pkg"], sid, os_version, ctx["out_dir"]),
                    exec_kwargs={
                        **self._build_run_sim_submit_kwargs(ctx, sid, os_version),
                        **self._detach_submit_kwargs(sid, detach_claim),
                        "transport": self._result_transport_config,
                    },
                )

                if detach_claim is not None:
                    job_id = getattr(handle, "job_id", None)
                    detached_jobs[sid] = job_id
                    self._record_detached_job(detach_claim, sid, job_id)
                    continue

                key = ctx["key"]
                state = ctx["state"]
                archive = self.cfg.archive_intermediates
                h = handle

                def _on_success(
                    result_path: Any,
                    _sid: str = sid,
                    _key: CacheKey = key,
                    _state: dict[str, object] = state,
                    _archive: bool = archive,
                    _handle: Handle = h,
                    _once: BookkeepingOnce = BookkeepingOnce(),
                ) -> None:
                    err = result_path / "eplusout.err"
                    if err.exists():
                        err.unlink()
                    _once.run(
                        "cache_store",
                        lambda: self.cache.store(_key, _c.Path(result_path), exit_code=0),
                    )
                    if _archive:
                        archive_dst = self.cfg.outdir / "archive" / "sim" / _sid
                        _once.run(
                            "archive_sim",
                            lambda: self._archive_sample_artifacts(
                                _c.Path(result_path),
                                archive_dst,
                                ["*.osw", "*.osm", "eplusout.sql"],
                            ),
                        )
                    if self._result_storage is not None:
                        sql_path = result_path / "eplusout.sql"
                        if sql_path.is_file():
                            try:
                                self._result_storage.upload_file(
                                    sql_path,
                                    f"sim/{_sid}/eplusout.sql",
                                )
                                _c.log.debug(
                                    "result storage: uploaded eplusout.sql for sample %s",
                                    _sid,
                                )
                            except OSError as exc:
                                _c.log.warning(
                                    "result storage: upload failed for sample %s: %s",
                                    _sid,
                                    exc,
                                    exc_info=True,
                                )
                    # Record sample status to observability backend immediately
                    # so completed samples are not missed if campaign crashes
                    # before _finalize_samples (issue #847).
                    _once.run(
                        "obs",
                        lambda: self._obs.record_sample_status(
                            _sid, "ok", trace_id=self._trace_id_for(_sid)
                        ),
                    )
                    out[_sid] = _c.Path(result_path)
                    _state["sim_exit_code"] = 0
                    _state["sim_status"] = "ok"
                    _state["eplusout_sql"] = str(result_path / "eplusout.sql")
                    _state["worker_id"] = _handle.worker_id
                    _state["worker_ip"] = getattr(_handle, "worker_ip", None)
                    _state["worker_region"] = getattr(_handle, "worker_region", None)
                    _state["cost_usd"] = getattr(_handle, "cost_usd", None)
                    _state["billed_duration_seconds"] = getattr(
                        _handle, "billed_duration_seconds", None
                    )
                    _once.run(
                        "trace",
                        lambda: self.trace.step_item_done("RUN_OPENSTUDIO_SIM", status="ok"),
                    )

                submissions[sid] = (handle, _on_success)

            if not self.cfg.detach_s3:
                self._submit_and_await_all(
                    submissions,
                    "RUN_OPENSTUDIO_SIM",
                    recovery_manager=recovery_manager,
                    resubmit_callback=resubmit_callback,
                )
        if self.cfg.detach_s3:
            self._finish_detached_submit(list(parameterized), detached_jobs)
        # Issue #1871: remember failed-sample sim dirs so AGGREGATE_RESULTS can
        # report their measure failure messages (out.osw / run.log).
        failed_dirs: dict[str, Path] = getattr(self, "_failed_sim_dirs", None) or {}
        self._failed_sim_dirs = failed_dirs
        failed_dirs.update({_s: c["out_dir"] for _s, c in pending.items() if _s not in out})
        total_cost, total_savings = self._cost_tracker.sum_sample_costs(self._sample_state)
        self._record_costs("RUN_OPENSTUDIO_SIM", total_cost, total_savings)

        # Record failures for samples that didn't succeed.
        for _sid, ctx in pending.items():
            state = ctx["state"]
            if state.get("sim_status") != "ok":
                out.pop(_sid, None)
                already_marked = state.get("sim_status") == "failed"
                state["sim_exit_code"] = 1
                state["sim_status"] = "failed"
                state.setdefault("error_summary", "SIM: unknown error during concurrent execution")
                if state.get("failure_class") != "bookkeeping_error":
                    self.cache.store(ctx["key"], ctx["out_dir"], exit_code=1)
                self.trace.step_item_done("RUN_OPENSTUDIO_SIM", status="failed")
                if not already_marked:
                    # Record sample status to observability backend (issue #847).
                    self._obs.record_sample_status(
                        _sid, "failed", trace_id=self._trace_id_for(_sid)
                    )
                    # Send sample failure alert (issue #1180).
                    self._maybe_alert(
                        "sample.failed",
                        {
                            "campaign_id": self.trace.campaign_id,
                            "sample_id": _sid,
                            "step": "RUN_OPENSTUDIO_SIM",
                            "status": "failed",
                            "error": "sim exited with non-zero code",
                        },
                    )

        any_failed = any(ctx["state"].get("sim_exit_code", 0) != 0 for ctx in pending.values())
        self.trace.step_finished(
            "RUN_OPENSTUDIO_SIM",
            cache="MISS×N" if n else "SKIPPED",
            elapsed_s=_c.time.time() - t0,
            exit_code=1 if any_failed else 0,
        )
        self._obs.record_step_duration(
            "RUN_OPENSTUDIO_SIM", _c.time.time() - t0, generation=generation
        )
        return SimResult(samples=out, success=not any_failed)

    def _detach_store(self) -> S3CampaignStore:
        cfg = self.cfg
        if cfg.result_storage_backend != "s3" or not cfg.result_storage_bucket:
            raise _c.CampaignError(
                "--detach-s3 requires --result-storage-backend s3 and --result-storage-bucket"
            )
        store: S3CampaignStore | None = getattr(self, "_detach_store_obj", None)
        if store is None:
            storage = _c.build_result_storage(
                "s3",
                cfg.result_storage_bucket,
                endpoint_url=cfg.result_storage_endpoint,
                allow_insecure_endpoint=bool(cfg.allow_insecure_storage_endpoint),
            )
            store = _c.S3CampaignStore(storage, cfg.outdir / ".s3_scratch")
            self._detach_store_obj = store
        return store

    def _acquire_detach_claim(self) -> SubmitClaim:
        """Claim the single-submitter lease before any Batch submit (issue #1881)."""
        cid = self.cfg.outdir.name
        outcome, claim = self._detach_store().claim_submission(cid)
        if outcome == _c.CLAIM_ALREADY_SUBMITTED:
            raise _c.CampaignDetached(cid)
        if outcome != _c.CLAIM_ACQUIRED or claim is None:
            raise _c.CampaignError(
                f"campaign {cid} is being submitted by another process (live claim in S3); "
                "retry after its lease expires if that submitter crashed"
            )
        self._detach_claim = claim
        return claim

    def _detach_submit_kwargs(self, sid: str, claim: SubmitClaim | None) -> dict[str, Any]:
        """Deterministic Batch job name; dedupe by name when resuming a crashed submit."""
        if claim is None:
            return {}
        return {
            "job_name": _c.batch_job_name(self.cfg.outdir.name, sid),
            "reuse_existing_job": claim.resumed,
        }

    def _record_detached_job(self, claim: SubmitClaim, sid: str, job_id: str | None) -> None:
        store = self._detach_store()
        cid = self.cfg.outdir.name
        if job_id:
            store.record_job(cid, sid, job_id, _c.batch_job_name(cid, sid))
        store.renew_claim(claim)

    def _release_detach_claim(self) -> None:
        claim: SubmitClaim | None = getattr(self, "_detach_claim", None)
        if claim is not None:
            self._detach_store().release_claim(claim)

    def _finish_detached_submit(
        self, sample_ids: list[str], job_ids: dict[str, str | None]
    ) -> None:
        """Write the S3 handoff record and detach (issue #1873)."""
        cfg = self.cfg
        store = self._detach_store()
        record = _c.new_handoff(
            cfg.outdir.name,
            sample_ids,
            openstudio_version=cfg.openstudio_version,
            kpis=list(cfg.kpis) if cfg.kpis else None,
            job_ids=job_ids,
            executor=self.executor.name,
        )
        store.write_handoff(record, self._latest_samples_file)
        claim: SubmitClaim | None = getattr(self, "_detach_claim", None)
        if claim is not None:
            store.finish_claim(claim)
        _c.shutil.rmtree(cfg.outdir / ".s3_scratch", ignore_errors=True)
        _c.log.info(
            "detached: submitted %d job(s); campaign id %s (bucket %s)",
            len(job_ids),
            record.campaign_id,
            cfg.result_storage_bucket,
        )
        raise _c.CampaignDetached(record.campaign_id)

    # ------------------------------------------------------------------
    # Worker direct-to-storage push (issue #625, Epic #624)
    # ------------------------------------------------------------------
    def _coordinator_url(self) -> str | None:
        """Resolve the Coordinator base URL for worker status reporting.

        Reads ``cfg.coordinator_url`` when present (forward-compatible with a
        future CampaignConfig field) and otherwise falls back to the
        ``OSIMFLOW_COORDINATOR_URL`` environment variable, which is the
        established pattern for forwarding per-job config to distributed
        workers (AGENTS.md §10).
        """
        cfg_url = getattr(self.cfg, "coordinator_url", None)
        if cfg_url:
            return str(cfg_url)
        url: str | None = _c.os.environ.get("OSIMFLOW_COORDINATOR_URL")
        return url

    def _coordinator_api_key(self) -> str | None:
        """Resolve an optional bearer token for Coordinator PATCH calls."""
        key: str | None = _c.os.environ.get("OSIMFLOW_API_KEY")
        return key

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
        t0 = _c.time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before AGGREGATE_RESULTS")

        # Cross-step data dependency check (issue #850): verify all KPI files
        # are present before aggregating. This prevents AGGREGATE_RESULTS from
        # running against a stale or incomplete set of upstream outputs.
        self._verify_step_inputs("AGGREGATE_RESULTS")
        for kpi_file in kpi_files:
            if not kpi_file.is_file():
                raise FileNotFoundError(
                    f"AGGREGATE_RESULTS requires KPI file {kpi_file} which was not found. "
                    f"Ensure all EXTRACT_KPIS samples completed successfully."
                )

        sim_dirs = list(simulated.values())
        for _sid, failed_dir in getattr(self, "_failed_sim_dirs", {}).items():
            fd = _c.Path(failed_dir)
            if _sid not in simulated and any((fd / n).is_file() for n in ("out.osw", "run.log")):
                sim_dirs.append(fd)
        inputs_hash = _c.sha256_of_dict(
            {
                "kpis": kpi_content_digests(kpi_files),
                "sims": [str(p) for p in sim_dirs],
                "sim_contents": sim_dir_content_digests(sim_dirs),
                "baseline_sample_id": baseline_sample_id or "None",
            }
        )
        key = _c.CacheKey(
            step="AGGREGATE_RESULTS",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256=inputs_hash,
            code_sha256=self.code_hashes["work"],
            container_digest=self._python_container_digest,
        )
        cached = self.cache.lookup(key)
        if cached:
            elapsed = _c.time.time() - t0
            self.trace.step_finished(
                "AGGREGATE_RESULTS",
                cache="HIT",
                elapsed_s=elapsed,
                exit_code=0,
            )
            self._obs.record_step_duration("AGGREGATE_RESULTS", elapsed)
            return {
                "csv": cached,
                "parquet": cached.parent / "aggregated_results.parquet",
                "failed": cached.parent / "failed_simulations.csv",
            }
        handle = self.executor.submit(
            _c.aggregate_results,
            kpi_files,
            sim_dirs,
            self.cfg.outdir,
            baseline_sample_id=baseline_sample_id,
            samples_json=self.cfg.samples_file,
            name="aggregate",
            cpus=2,
            memory_mb=4 * 1024,
            time_min=15,
            container=self._python_container_image,
            container_digest=self._python_container_digest,
            result_hint={
                "csv": self.cfg.outdir / "aggregated_results.csv",
                "parquet": self.cfg.outdir / "aggregated_results.parquet",
                "failed": self.cfg.outdir / "failed_simulations.csv",
            },
            transport=self._result_transport_config,
        )
        result_obj: object = handle.result(timeout=900)
        result = _c.cast_aggregate_result(result_obj)
        self.cache.store(key, result["csv"], exit_code=0)
        elapsed = _c.time.time() - t0
        self.trace.step_finished(
            "AGGREGATE_RESULTS",
            cache="MISS",
            elapsed_s=elapsed,
            exit_code=0,
        )
        self._obs.record_step_duration("AGGREGATE_RESULTS", elapsed)
        return cast(dict[str, Path], result)

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
        t0 = _c.time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before GENERATE_BASIC_PLOTS")

        plots_dir = self.cfg.outdir / "plots"
        handle = self.executor.submit(
            _c.generate_plots,
            aggregated["csv"],
            aggregated["failed"],
            plots_dir,
            baseline_sample_id=baseline_sample_id,
            name="plots",
            cpus=1,
            memory_mb=2048,
            time_min=10,
            container=self._python_container_image,
            container_digest=self._python_container_digest,
            # The plot file names are unknown up front, so hint the directory;
            # object-storage transport downloads it for remote workers.
            result_hint=[plots_dir],
            transport=self._result_transport_config,
        )
        result_obj: object = handle.result(timeout=900)
        result = []
        for item in _c.cast_plot_paths(result_obj):
            if item.is_dir():
                result.extend(sorted(p for p in item.iterdir() if p.is_file()))
            else:
                result.append(item)
        elapsed = _c.time.time() - t0
        self.trace.step_finished(
            "GENERATE_BASIC_PLOTS",
            cache="SKIPPED",
            elapsed_s=elapsed,
            exit_code=0,
        )
        self._obs.record_step_duration("GENERATE_BASIC_PLOTS", elapsed)
        return result

    def warm_cache(self, n_warm: int = 10) -> dict[str, object]:
        """Run N pilot samples to pre-populate the cache before a campaign.

        Generates N pilot samples using the configured sampling algorithm,
        runs ``APPLY_PARAMETERS`` and ``RUN_OPENSTUDIO_SIM`` for each, and
        stores results in the SQLite cache.  When the real campaign starts
        with the same configuration, those steps will hit the cache instead
        of re-running the simulation.

        This method intentionally skips KPI extraction and result aggregation
        — those steps are not cached and running them for pilot samples would
        add latency for no cache benefit.

        Args:
            n_warm: number of pilot samples to run (default 10).

        Returns:
            dict with ``n_samples`` and ``cache_stats``.
        """
        _c.log.info("Cache warming: generating %d pilot samples", n_warm)
        algo = _c.AlgorithmRegistry.get(self.cfg.algorithm)
        warm_dir = self.cfg.outdir / ".osimflow_warm"
        warm_dir.mkdir(parents=True, exist_ok=True)

        variables: dict[str, Any] = {}
        if self.cfg.input_variables.exists():
            with self.cfg.input_variables.open() as fh:
                raw = _c.yaml.safe_load(fh)
                if isinstance(raw, dict):
                    variables = raw

        result_path = algo.generate_samples(
            variables=variables,
            n_samples=n_warm,
            seed=None,
            outdir=warm_dir,
        )
        samples_obj: object = _c.json.loads(_c.Path(result_path).read_text())["samples"]
        pilot_specs: list[SampleSpec] = _c.cast_samples(samples_obj)

        _c.log.info(
            "Cache warming: running apply + sim for %d pilot samples (populating cache)",
            len(pilot_specs),
        )
        parameterized: SampleDict = self.step_apply_parameters(pilot_specs)
        self.step_run_openstudio_sim(parameterized, generation=0)
        stats = self.cache.get_stats()
        _c.log.info(
            "Cache warming complete: %d/%d steps cached",
            stats.hits,
            stats.hits + stats.misses,
        )
        return {
            "n_samples": len(pilot_specs),
            "cache_stats": {
                "hits": stats.hits,
                "misses": stats.misses,
                "total_keys": stats.total_keys,
            },
        }
