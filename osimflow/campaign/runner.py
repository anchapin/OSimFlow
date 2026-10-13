"""Campaign run-path logic (issue #1890).

``CampaignRunMixin`` holds ``Campaign.run`` and its dry-run /
single-sample / full-campaign / finalize helpers, moved verbatim out of
``osimflow.campaign``.  Every module-level name the moved code used is
resolved through the ``osimflow.campaign`` package namespace at call
time (``_c.<name>``) so ``mock.patch("osimflow.campaign.<name>")``
seams keep intercepting the calls.
"""

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import CampaignConfig
from ..observability.monitoring import RunTrace
from .dag import SampleDict
from .types import SampleSpec

log = logging.getLogger("osimflow.campaign")

# Package namespace, resolved at call time so patches on osimflow.campaign.<name> apply.
_c: Any = sys.modules["osimflow.campaign"]


class CampaignRunMixin:
    cfg: CampaignConfig
    trace: RunTrace

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any:
            """Attribute surface provided by ``Campaign`` (typing only)."""
            ...

    def run(self) -> dict[str, object]:  # noqa: PLR0912, PLR0915
        # Fail fast on insecure Coordinator URLs (issue #1550) — mirrors
        # _validate_storage_endpoint (#1386) / validate_redis_url (#1321).
        # The override reuses --allow-insecure-storage-endpoint (no new
        # CLI surface); loopback coordinators are always exempt.
        _c._validate_coordinator_url(
            self._coordinator_url(),
            allow_insecure=bool(getattr(self.cfg, "allow_insecure_storage_endpoint", False)),
        )
        # Issue #1536: container substrates must execute the digest-pinned
        # image.  When the digest could not be resolved (docker absent, the
        # image not pulled locally) and the operator did not pass
        # ``--container-digest``, fail loudly instead of silently resolving
        # the image by its mutable tag — a re-published tag would change
        # what runs while the cache key still describes the old digest.
        if self.executor.name in _c.CONTAINER_SUBSTRATE_EXECUTORS and self._os_digest_ref is None:
            raise RuntimeError(
                f"executor {self.executor.name!r} requires a digest-pinned "
                "container image, but the OpenStudio image digest could not "
                "be resolved (docker unavailable or image not pulled) and no "
                "--container-digest was provided. Pass --container-digest "
                "<repo>@sha256:<hex> (or resolve/pull the image where docker "
                "is reachable) so the executed image matches the cache key "
                "(issue #1536)."
            )
        self._reject_unsupported_remote_hooks()
        _c.log.info("=" * 60)
        _c.log.info("OSimFlow campaign start")
        _c.log.info("  executor:      %s", self.executor.name)
        _c.log.info("  n_samples:     %d", self.cfg.n_samples)
        _c.log.info("  os_version:    %s", self.cfg.openstudio_version)
        _c.log.info("  outdir:        %s", self.cfg.outdir)
        _c.log.info("  work_dir:      %s", self.cfg.work_dir)
        if self.cfg.dry_run:
            _c.log.info("  ** DRY RUN MODE **")
        if self.cfg.sample is not None:
            _c.log.info("  ** SINGLE SAMPLE MODE: sample %d **", self.cfg.sample)
        _c.log.info("=" * 60)

        # Register this campaign for signal handling (issue #255).
        _c._cancel_registry.register(self)

        # Setup signal handlers for graceful shutdown.
        self._setup_signal_handlers()

        # Reset pause flag so a paused campaign can be re-run without
        # restarting (issue #798).
        self._lifecycle.reset_pause()

        # Wire chaos_schedule to RunTrace (issue #1309).
        chaos_cfg_obj = getattr(self.cfg, "chaos", None)
        if chaos_cfg_obj is not None:
            self.trace.chaos_schedule = str(getattr(chaos_cfg_obj, "schedule", "none"))

        run_name = _c.maybe_start_mlflow_run(self.cfg.mlflow_tracking_uri, self.trace.campaign_id)
        if run_name is not None:
            _c.log_mlflow_params(_c._make_mlflow_param_view(self.cfg, self.executor.name))

        t0 = _c.time.time()
        campaign_status = "failure"

        # Auto-register campaign in registry (issue #266).
        self._register_campaign()

        # Write campaign metadata manifest at start (issue #277).
        self._write_campaign_meta()

        # Start periodic observability flush (issue #1186).
        self._obs.start_periodic_flush()

        try:
            # Pre-campaign cancellation check: if cancellation is requested
            # BEFORE the campaign starts, the except/finally blocks below
            # will write run.json, restore signal handlers, and close the
            # cache.  Keeping this inside the try block is essential so that
            # the finally cleanup (WAL checkpoint, registry update, etc.)
            # always runs even for pre-campaign cancellations.
            if self._check_cancel_requested():
                raise KeyboardInterrupt("cancellation requested before campaign start")

            # Start-quota fail-fast (issues #446 → #1533): enforce
            # resource_quota limits before any campaign work — including
            # the init hook — is dispatched.  Raises QuotaExceededError
            # when the configuration already violates a quota.
            self._enforce_start_quota()

            # Init hook (issue #108): runs before the first campaign step.
            # Must succeed (exit 0) or the campaign aborts.
            self._run_init_script()

            # Initialize run.json after init script succeeds so the init hook
            # sees a clean outdir (issue #108).  _checkpoint_sample() handles a
            # missing run.json gracefully (no-op), so this write is only needed
            # for SSE clients to poll status from the start of the fan-out.
            self.trace.write(self.cfg.outdir / "run.json")

            # Crash recovery (issue #263): reset any in-flight jobs from
            # a previous interrupted run back to pending so they are
            # reprocessed during this run.  The cache layer ensures
            # completed work is not re-executed — only truly lost work
            # (where the executor completed but the orchestrator crashed
            # before recording the result) gets retried.
            recovered = self._job_queue.recover()
            if recovered:
                _c.log.info(
                    "crash recovery: %d in-flight job(s) reset to pending",
                    len(recovered),
                )

            if self.cfg.dry_run:
                # Scope the dry-run env var to this execution so it
                # cannot leak across campaigns/tests (issue #976).
                with _c._scoped_dry_run_env():
                    result = self._run_dry_run(t0)
            elif self.cfg.sample is not None:
                result = self._run_single_sample(t0)
            else:
                result = self._run_full_campaign(t0)
            # If cancellation was detected in _finalize_full_campaign,
            # self.trace.status will be "cancelled" — propagate that to
            # campaign_status so the caller sees the correct final state.
            # A "paused" trace (pause raced in after the last pause check)
            # is likewise preserved, never flipped to "success" (issue #1537).
            if self.trace.status == "cancelled":
                campaign_status = "cancelled"
            elif self.trace.status == "paused":
                campaign_status = "paused"
            else:
                campaign_status = "success"
            self.trace.status = campaign_status
            return result
        except _c.CampaignPauseRequested:
            # Soft pause (issue #553 / #1537): NOT a cancellation.
            # - trace.status stays "paused" (set by _write_paused_trace at
            #   the raise site; re-written here defensively so the on-disk
            #   run.json reflects the paused state even if an in-flight
            #   checkpoint raced the unwind).
            # - finished_at is NOT set: pause is non-terminal and the
            #   campaign may be resumed.
            # - active jobs are NOT cancelled: running samples complete
            #   normally per the documented pause semantics.
            # - registry / webhook / finalize hook receive "paused" via
            #   campaign_status in the finally block.
            campaign_status = "paused"
            _c.log.warning("campaign paused — resume with 'osimflow resume'")
            self._write_paused_trace()
            # Do NOT re-raise — pause has been handled; the finally block
            # will run and the campaign exits in the paused state.
            return {
                "samples": [],
                "kpis": [],
                "aggregated": {"csv": None, "failed": None},
                "plots": [],
                "elapsed_s": _c.time.time() - t0,
                "run_json": self.cfg.outdir / "run.json",
                "status": "paused",
                "trace": self.trace,
            }
        except _c.CampaignDetached as detached:
            # Issue #1873: jobs submitted, handoff persisted in S3; exit cleanly.
            campaign_status = "detached"
            self.trace.status = "detached"
            self.trace.finalize()
            self.cfg.outdir.mkdir(parents=True, exist_ok=True)
            self.trace.write(self.cfg.outdir / "run.json")
            return {
                "samples": [],
                "kpis": [],
                "aggregated": {"csv": None, "failed": None},
                "plots": [],
                "elapsed_s": _c.time.time() - t0,
                "run_json": self.cfg.outdir / "run.json",
                "status": "detached",
                "campaign_id": str(detached),
                "trace": self.trace,
            }
        except KeyboardInterrupt:
            campaign_status = "cancelled"
            _c.log.warning("campaign cancelled by user or signal")
            self.trace.status = "cancelled"
            self._cancel_active_jobs()
            # Collect circuit breaker states for run.json (issue #1307)
            self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
            self.trace.finalize()
            # Always write the trace on cancellation so callers can inspect
            # the partial state.  The file may not exist yet when
            # cancellation fires before the first step (e.g. pre-campaign
            # cancel or .stop file present before run()).
            self.cfg.outdir.mkdir(parents=True, exist_ok=True)
            self.trace.write(self.cfg.outdir / "run.json")
            # Do NOT re-raise — cancellation has been handled; the finally
            # block will run and the campaign will exit with the correct
            # status. Re-raising would crash the worker in concurrent mode
            # and cause test_cancel_during_generation_loop_stops to fail.
            return {"status": "cancelled", "trace": self.trace}
        except Exception as exc:
            campaign_status = "failure"
            self.trace.status = "failure"
            self.trace.error_summary = f"{type(exc).__name__}: {exc}"
            # Collect circuit breaker states for run.json (issue #1307)
            self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
            self.trace.finalize()
            self.cfg.outdir.mkdir(parents=True, exist_ok=True)
            _c.log.exception("campaign failed")
            # Send campaign failure alert (issue #1180).
            self._maybe_alert(
                "campaign.failed",
                {
                    "campaign_id": self.trace.campaign_id,
                    "status": "failure",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
        finally:
            # Restore signal handlers FIRST, before any other cleanup.
            self._restore_signal_handlers()
            self._release_detach_claim()
            _c._cancel_registry.clear()

            # Finalize hook (issue #108): best-effort after all steps.
            # Runs even if init script failed, so the user gets a
            # notification that the campaign aborted.
            duration = _c.time.time() - t0
            # Observability: record campaign duration and flush backend.
            # Best-effort (issue #1672): ``ObservabilityManager.flush()``
            # deliberately re-raises the first backend exception after
            # attempting every backend, so an unguarded call here would
            # crash a successful campaign or replace the original
            # in-flight exception. Contained in ``_teardown_observability``
            # (mirroring ``_maybe_alert``) — log at ERROR, never raise.
            self._teardown_observability(duration)
            self._run_finalize_script(campaign_status, duration)
            # Re-write run.json to include finalize hook timing
            if (self.cfg.outdir / "run.json").exists():
                self.trace.write(self.cfg.outdir / "run.json")

            # Write provenance manifest at completion (issue #277).
            self._write_provenance()

            # Write artifact manifest after all files are produced (issue #277).
            # Placed after provenance so the manifest captures all output files.
            self._write_artifact_manifest()

            _c.maybe_end_mlflow_run()

            # Update registry status (issue #266).
            self._update_registry_status(campaign_status)

            # Fire webhook callback (issue #283). Best-effort: delivery
            # failures are logged but do not affect campaign status.
            self._maybe_fire_webhook(campaign_status, duration)

            # Close the SQLite cache. ``close()`` runs a PASSIVE WAL
            # checkpoint and the connection; it never raises and never
            # removes the auxiliary ``.sqlite-wal`` / ``.sqlite-shm``
            # files, so peer worker processes sharing the same cache
            # during campaign cancellation do not crash with
            # ``FileNotFoundError`` (issue #620). Safe to call multiple
            # times (close() is idempotent and thread-safe).
            self.cache.close()

            # Close the result storage uploader (issue #339). Best-effort
            # and bounded (issue #1672): permanently-failed uploads or a
            # wedged storage backend must never raise out of this finally
            # block (masking the campaign outcome) or stall teardown —
            # past the drain deadline the uploader logs-and-abandons
            # whatever remains.
            if self._result_storage is not None:
                try:
                    self._result_storage.close(
                        timeout_s=_c._RESULT_STORAGE_CLOSE_TIMEOUT_S,
                        raise_on_failure=False,
                    )
                except Exception:
                    _c.log.exception("result storage: close failed during teardown")

            # Issue #1673: bounded close of the alert dispatcher. Same
            # containment shape as _teardown_observability above: never
            # raises into the campaign outcome. Past the deadline any
            # in-flight alerts are abandoned (logged by AlertManager).
            if self._alert_manager is not None:
                try:
                    self._alert_manager.close(timeout_s=5.0)
                except Exception:
                    _c.log.exception("alert manager: close failed during teardown")

    def _abort_run_path_cancel(
        self,
        t0: float,
        samples: list[SampleSpec],
        kpi_files: list[Path],
    ) -> dict[str, object]:
        """Write a partial cancelled trace and return a cancelled result dict.

        Called by the dry-run and single-sample run paths (issue #621)
        when an inter-step cancellation check fires.  Both paths bypass
        the generation loop in ``_run_full_campaign``, so they need their
        own mid-flight polling — without it a ``.stop`` file written
        between two steps is ignored until the path returns, by which
        point a ``status="ok"`` trace has already been written.

        The partial-trace write here mirrors the cancellation handling
        in ``_finalize_full_campaign`` (lines ~2355-2368) so the cleanup
        from #620 (PASSIVE WAL checkpoint, thread-safe cache close) runs
        cleanly in ``run()``'s ``finally`` block.  ``run()`` propagates
        ``trace.status == "cancelled"`` into ``campaign_status`` so the
        caller observes the correct final state.
        """
        _c.log.warning("cancellation requested mid-run-path — writing partial trace")
        self._finalize_samples()
        self.trace.status = "cancelled"
        # Collect circuit breaker states for run.json (issue #1307)
        self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
        self.trace.finalize()
        self.cfg.outdir.mkdir(parents=True, exist_ok=True)
        self.trace.write(self.cfg.outdir / "run.json")
        return {
            "samples": samples,
            "kpis": kpi_files,
            "aggregated": {"csv": None, "failed": None},
            "plots": [],
            "elapsed_s": _c.time.time() - t0,
            "run_json": self.cfg.outdir / "run.json",
        }

    def _run_dry_run(self, t0: float) -> dict[str, object]:
        """Dry-run mode: 1 sample, local executor, steps 1-4 only.

        Cancellation is polled between every step (issue #621).  The
        dry-run path bypasses the generation loop in
        ``_run_full_campaign`` (which already polls ``_check_cancel_requested()``
        at the top of each iteration), so these inter-step checks are the
        only place a ``.stop`` file or in-memory cancel flag is honoured
        mid-flight.  On cancel a partial trace is written with
        ``status="cancelled"`` and the method returns cleanly — the
        ``run()`` caller observes the cancelled state without unwinding
        the stack via ``KeyboardInterrupt``.
        """
        original_n = self.cfg.n_samples
        self.cfg = _c.dataclasses.replace(self.cfg, n_samples=1)
        _c.log.info("DRY RUN: overriding n_samples from %d to 1", original_n)

        # Build algorithm kwargs (issue #529: R-NSGA-II support)
        algo_kwargs: dict[str, Any] = {}
        if self.cfg.algorithm == "nsga2":
            if self.cfg.nsga2_reference_points is not None:
                algo_kwargs["ref_points"] = self.cfg.nsga2_reference_points
            if self.cfg.nsga2_reference_directions is not None:
                algo_kwargs["ref_dirs"] = self.cfg.nsga2_reference_directions

        samples: list[SampleSpec] = []
        kpi_files: list[Path] = []

        # Pre-step check: cancel requested before the dry-run starts.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, samples, kpi_files)

        algo = _c.AlgorithmRegistry.get(self.cfg.algorithm, **algo_kwargs)
        samples = self.step_generate_samples(algo)
        # GAP-009: inject per-sample seed_model / weather_file overrides
        # from the DataPointManager before processing.
        samples = self._inject_dp_overrides(samples)
        samples = self._apply_sharding(samples, generation=0)
        samples_path = self._samples_manifest_path()
        samples_path.parent.mkdir(parents=True, exist_ok=True)
        _c.safe_json_dumps({"samples": samples}, samples_path, indent=2, raise_on_error=True)
        self._latest_samples_file = samples_path

        # Inter-step check: cancel requested during sample generation /
        # manifest write, before APPLY_PARAMETERS.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, samples, kpi_files)

        parameterized: SampleDict = self.step_apply_parameters(samples)

        # Inter-step check: cancel requested during APPLY_PARAMETERS,
        # before the long-running RUN_OPENSTUDIO_SIM step.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, samples, kpi_files)

        simulated: SampleDict = self.step_run_openstudio_sim(parameterized).samples

        # Inter-step check: cancel requested during RUN_OPENSTUDIO_SIM,
        # before EXTRACT_KPIS.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, samples, kpi_files)

        kpi_files = self.step_extract_kpis(simulated)

        # Post-step check: cancel requested during EXTRACT_KPIS (after
        # that step's own entry check has passed).  Without this guard
        # the trace.write below would record status="ok" and the cancel
        # signal would be silently lost (issue #621).
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, samples, kpi_files)

        t1 = _c.time.time()

        self._finalize_samples()
        # Collect circuit breaker states for run.json (issue #1307)
        self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
        self.trace.finalize()
        self.trace.write(self.cfg.outdir / "run.json")

        n_ok = sum(1 for s in self.trace.per_sample if s.status == "ok")
        n_failed = sum(1 for s in self.trace.per_sample if s.status == "failed")
        _c.log_mlflow_metrics(t1 - t0, n_ok, n_failed)

        sample_elapsed = t1 - t0
        est_total_s = sample_elapsed * original_n

        summary = ["\n" + "=" * 60, "DRY RUN COMPLETE", "=" * 60]
        summary.append(f"  1/{original_n} samples processed in {sample_elapsed:.1f}s")
        summary.append(f"  Status: {n_ok} succeeded, {n_failed} failed")
        summary.append(f"  Output: {self.cfg.work_dir}")
        if kpi_files:
            summary.append(f"  KPI file: {kpi_files[0]}")
        summary += [
            "",
            "Full campaign estimate:",
            f"  {original_n} samples x {sample_elapsed:.1f}s = ~{est_total_s:.0f}s (~{est_total_s / 60:.1f} min)",
            "",
            "Next steps:",
            f"  1. Review output in {self.cfg.work_dir}",
            "  2. If correct, re-run without --dry-run for full campaign",
            "=" * 60,
        ]
        _c.sys.stdout.write("\n".join(summary) + "\n")

        return {
            "samples": samples,
            "kpis": kpi_files,
            "aggregated": {"csv": None, "failed": None},
            "plots": [],
            "elapsed_s": t1 - t0,
            "run_json": self.cfg.outdir / "run.json",
        }

    def _run_single_sample(self, t0: float) -> dict[str, object]:
        """Single-sample mode: run only sample N through steps 2-4.

        Cancellation is polled between every step (issue #621).  Like the
        dry-run path, this path bypasses the generation loop in
        ``_run_full_campaign``, so the inter-step checks here are the
        only place a ``.stop`` file or in-memory cancel flag is honoured
        mid-flight.
        """
        sample_idx = self.cfg.sample
        assert sample_idx is not None
        samples_file = self._samples_manifest_path()
        if not samples_file.exists():
            raise FileNotFoundError(
                f"Samples.json not found at {samples_file!r}. "
                "Run a full campaign (or --dry-run) first to generate samples."
            )
        all_samples_raw = _c.safe_json_loads(samples_file, default=None, log_warnings=False)
        if all_samples_raw is None:
            raise ValueError(
                f"Samples.json at {samples_file!r} is corrupted or unreadable. "
                "Run a full campaign (or --dry-run) first to generate valid samples."
            )
        all_samples = _c.cast_samples(all_samples_raw["samples"])
        if sample_idx < 0 or sample_idx >= len(all_samples):
            raise IndexError(
                f"Sample index {sample_idx} out of range [0, {len(all_samples) - 1}]. "
                f"Total samples available: {len(all_samples)}"
            )
        target = all_samples[sample_idx]
        _c.log.info("SINGLE SAMPLE: running sample %d (id=%s)", sample_idx, target["sample_id"])

        kpi_files: list[Path] = []

        # Pre-step check: cancel requested before the sample starts.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, [target], kpi_files)

        parameterized: SampleDict = self.step_apply_parameters([target])

        # Inter-step check: cancel requested during APPLY_PARAMETERS,
        # before the long-running RUN_OPENSTUDIO_SIM step.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, [target], kpi_files)

        simulated: SampleDict = self.step_run_openstudio_sim(parameterized).samples

        # Inter-step check: cancel requested during RUN_OPENSTUDIO_SIM,
        # before EXTRACT_KPIS.
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, [target], kpi_files)

        kpi_files = self.step_extract_kpis(simulated)

        # Post-step check: cancel requested during EXTRACT_KPIS (after
        # that step's own entry check has passed).  Without this guard
        # the trace.write below would record status="ok" and the cancel
        # signal would be silently lost (issue #621).
        if self._check_cancel_requested():
            return self._abort_run_path_cancel(t0, [target], kpi_files)

        t1 = _c.time.time()

        self._finalize_samples()
        # Collect circuit breaker states for run.json (issue #1307)
        self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
        self.trace.finalize()
        self.trace.write(self.cfg.outdir / "run.json")

        _c.log.info("=" * 60)
        _c.log.info("SINGLE SAMPLE COMPLETE: sample %d in %.1fs", sample_idx, t1 - t0)
        _c.log.info("=" * 60)

        return {
            "samples": [target],
            "kpis": kpi_files,
            "aggregated": {"csv": None, "failed": None},
            "plots": [],
            "elapsed_s": t1 - t0,
            "run_json": self.cfg.outdir / "run.json",
        }

    def _run_full_campaign(self, t0: float) -> dict[str, object]:
        """Standard full campaign: all steps, all samples.

        For iterative algorithms (``max_generations > 1``), the fan-out
        steps (APPLY_PARAMETERS → RUN_OPENSTUDIO_SIM → EXTRACT_KPIS)
        loop for up to ``cfg.max_generations`` iterations.  Each
        iteration is a "generation" tracked in the cache key.  LHS with
        ``max_generations=1`` is backward-compatible.
        """
        if self.cfg.max_generations < 1:
            raise ValueError(f"max_generations must be >= 1, got {self.cfg.max_generations}")
        if self.cfg.detach_s3 and self.cfg.max_generations > 1:
            raise _c.CampaignError("--detach-s3 supports single-generation campaigns only")

        # Build algorithm kwargs (issue #529: R-NSGA-II support)
        algo_kwargs: dict[str, Any] = {}
        if self.cfg.algorithm == "nsga2":
            if self.cfg.nsga2_reference_points is not None:
                algo_kwargs["ref_points"] = self.cfg.nsga2_reference_points
            if self.cfg.nsga2_reference_directions is not None:
                algo_kwargs["ref_dirs"] = self.cfg.nsga2_reference_directions

        algo = _c.AlgorithmRegistry.get(self.cfg.algorithm, **algo_kwargs)

        # History accumulator: one dict per generation.
        history: list[dict[str, Any]] = []

        all_kpi_files: list[Path] = []
        last_simulated: SampleDict = {}
        last_samples: list[SampleSpec] = []

        for generation in range(self.cfg.max_generations):
            if self._check_cancel_requested():
                _c.log.warning("cancellation requested — stopping generation loop")
                break
            _c.log.info(
                "--- generation %d/%d ---",
                generation + 1,
                self.cfg.max_generations,
            )
            result = self._run_one_generation(algo, history, generation)
            if result is None:
                # Algorithm converged; mark last generation as converged.
                if self.trace.generations:
                    self.trace.generations[-1].converged = True
                break
            samples, kpi_files, simulated = result
            last_samples = samples
            last_simulated = simulated
            all_kpi_files.extend(kpi_files)
            history.append(
                {
                    "generation": generation,
                    "samples": samples,
                    "kpi_files": [str(p) for p in kpi_files],
                }
            )
            # Single-shot algorithms never iterate.
            if not algo.is_iterative():
                break

        return self._finalize_full_campaign(t0, all_kpi_files, last_simulated, last_samples)

    def _finalize_full_campaign(
        self,
        t0: float,
        all_kpi_files: list[Path],
        last_simulated: SampleDict,
        last_samples: list[SampleSpec],
    ) -> dict[str, object]:
        """Aggregate, plot, archive, and write the run trace."""
        if self._check_cancel_requested():
            _c.log.warning("cancellation requested before aggregation — writing partial trace")
            self._finalize_samples()
            self.trace.status = "cancelled"
            # Collect circuit breaker states for run.json (issue #1307)
            self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
            self.trace.finalize()
            self.trace.write(self.cfg.outdir / "run.json")
            return {
                "samples": last_samples,
                "kpis": all_kpi_files,
                "aggregated": {},
                "plots": [],
                "elapsed_s": _c.time.time() - t0,
                "run_json": self.cfg.outdir / "run.json",
            }
        # Soft pause (issue #1537): if pause was detected during the last
        # fan-out (the concurrent path breaks without raising) or lands
        # between the fan-out and aggregation, finalize as paused — NOT as
        # success — so `osimflow resume` can continue from cache replay.
        if self._check_pause_requested():
            _c.log.warning("pause requested before aggregation — writing paused trace")
            self._write_paused_trace()
            raise _c.CampaignPauseRequested("pause requested before AGGREGATE_RESULTS")
        aggregated: dict[str, Path] = self.step_aggregate_results(
            all_kpi_files, last_simulated, baseline_sample_id=self._baseline_sample_id()
        )
        plots: list[Path] = self.step_generate_plots(
            aggregated, baseline_sample_id=self._baseline_sample_id()
        )
        t1 = _c.time.time()

        self._compute_baseline_comparison(all_kpi_files)
        self._maybe_archive_inputs()

        self._finalize_samples()
        # Collect circuit breaker states for run.json (issue #1307)
        self.trace.circuit_breaker_states = self._collect_circuit_breaker_states()
        self.trace.finalize()
        self.trace.write(self.cfg.outdir / "run.json")

        n_succeeded = sum(1 for s in self.trace.per_sample if s.status == "ok")
        n_failed = sum(1 for s in self.trace.per_sample if s.status == "failed")
        _c.log_mlflow_metrics(t1 - t0, n_succeeded, n_failed)
        _c.log_mlflow_artifacts(
            aggregated["csv"],
            aggregated["failed"],
            self.cfg.outdir / "run.json",
        )

        _c.log.info("=" * 60)
        _c.log.info("OSimFlow campaign complete in %.1fs", t1 - t0)
        _c.log.info("  cache stats:   %s", self.cache.stats())
        _c.log.info("  aggregated:    %s", aggregated["csv"])
        _c.log.info("  failed:        %s", aggregated["failed"])
        _c.log.info("  plots:         %s", plots)
        _c.log.info("  run trace:     %s", self.cfg.outdir / "run.json")
        if self.trace.total_cost_usd > 0:
            _c.log.info("  total cost:    $%.4f", self.trace.total_cost_usd)
            _c.log.info("  spot savings:  $%.4f", self.trace.spot_savings_usd)
        _c.log.info("=" * 60)
        return {
            "samples": last_samples,
            "kpis": all_kpi_files,
            "aggregated": aggregated,
            "plots": plots,
            "elapsed_s": t1 - t0,
            "run_json": self.cfg.outdir / "run.json",
        }

    def _maybe_inject_chaos(
        self,
        step_name: str,
        when: str,
        target_id: str | None = None,
    ) -> None:
        """Opt-in chaos fault injection (issue #1013).

        Delegates to :meth:`CampaignChaosWiring.maybe_inject`
        (issue #1462).  No-op unless the campaign has an active
        ``ChaosEngine`` and the configured ``cfg.chaos.schedule``
        matches *when* (``"before_step"`` / ``"after_step"`` /
        ``"per_sample"``); engine failures never propagate.  Invocations
        are recorded under ``run.json.chaos_invocations``.
        """
        self._chaos.maybe_inject(step_name, when, self.trace, target_id=target_id)

    def _maybe_archive_inputs(self) -> None:
        """Archive campaign inputs when ``cfg.archive_intermediates`` is set."""
        self._artifacts.maybe_archive_inputs()

    def _maybe_fire_webhook(self, campaign_status: str, elapsed_s: float) -> None:
        """Fire a webhook callback if ``cfg.webhook_url`` is configured (issue #283).

        Best-effort: delivery failures are logged but do not propagate.
        Fired from the ``finally`` block of ``run()`` regardless of the
        campaign outcome.
        """
        _c.maybe_fire_webhook(self.cfg, self.trace, campaign_status, elapsed_s)
