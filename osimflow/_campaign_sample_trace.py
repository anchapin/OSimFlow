"""Per-sample trace assembly and checkpointing (issue #1462 extraction).

Extracted from ``osimflow.campaign``: the per-sample ``SampleTrace``
builder used by ``_finalize_samples`` / ``_checkpoint_sample``, the
lazy per-sample trace-ID minting (issue #436), the campaign-level cost
total accumulation (issue #126), and the consecutive-checkpoint-failure
abort counter (issue #739).

Campaign keeps thin delegating methods (``campaign._finalize_samples()``
etc. are direct test seams).
"""

import logging

from ._campaign_observability import ObservabilityManager
from .cost_tracking import (
    DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR,
    DEFAULT_SPOT_PRICE_PER_VCPU_HOUR,
)
from .monitoring import RunTrace, SampleTrace
from .observability import new_trace_id

log = logging.getLogger("osimflow.campaign")


class CampaignSampleTraceRecorder:
    """Owns per-sample SampleTrace rows, trace IDs, and checkpoint writes."""

    def __init__(
        self,
        trace: RunTrace,
        sample_state: dict[str, dict[str, object]],
        obs: ObservabilityManager,
    ) -> None:
        self._trace = trace
        self._sample_state = sample_state
        self._obs = obs
        # Consecutive checkpoint failure counter (issue #739). After N
        # consecutive failures in checkpoint_sample we abort instead of
        # silently continuing.
        self.consecutive_checkpoint_failures = 0

    def trace_id_for(self, sample_id: str) -> str:
        """Return the per-sample trace ID, minting one on first access.

        The trace ID is stored in ``_sample_state[sample_id]["trace_id"]``
        so every observability call for this sample (cost, status,
        per-step fan-out events) shares the same correlation key.  Minted
        lazily via :func:`osimflow.observability.new_trace_id` — short
        (8 hex chars) and stable across cache hits, retries, and
        incremental checkpoints (issue #436).
        """
        state = self._sample_state.setdefault(sample_id, {})
        tid_obj = state.get("trace_id")
        if isinstance(tid_obj, str):
            return tid_obj
        tid = new_trace_id()
        state["trace_id"] = tid
        return tid

    def finalize_samples(self) -> None:
        """Emit one SampleTrace per sample based on accumulated per-step state.

        Also records per-sample observability metrics (duration, status)
        via the configured backend (issue #132).

        Deduplicates against incremental checkpoints: if a sample was
        already written to run.json by checkpoint_sample (via SSE live
        updates), the existing entry is replaced rather than appended,
        so the per_sample list never grows faster than the sample count.
        """
        existing_ids: set[str] = {s.sample_id for s in self._trace.per_sample}
        for sid, state in self._sample_state.items():
            trace = self._build_sample_trace(sid, state)
            # Deduplicate: replace existing entry from incremental checkpoint.
            if sid in existing_ids:
                for i, existing in enumerate(self._trace.per_sample):
                    if existing.sample_id == sid:
                        self._trace.per_sample[i] = trace
                        break
            else:
                self._trace.per_sample.append(trace)
                existing_ids.add(sid)
            # Observability: record per-sample status metric (issue #132).
            # status="ok" → 1.0, status="failed" → 0.0.  Forward the
            # trace_id so the status metric joins the cost metric under
            # the same per-sample trace (issue #436).
            status = trace.status
            self._obs.record_sample_metric(
                sid,
                "status",
                1.0 if status == "ok" else 0.0,
                trace_id=trace.trace_id,
            )

        # Accumulate campaign-level cost totals (issue #126).
        self.accumulate_cost_summary()

    def accumulate_cost_summary(self) -> None:
        """Sum per-sample costs into campaign-level totals (issue #126).

        Populates ``trace.total_cost_usd`` and ``trace.spot_savings_usd``
        from the individual ``SampleTrace.cost_usd`` values.  Non-cloud
        executors produce ``None`` costs, so both totals remain at 0.0
        for local runs.
        """
        total = 0.0
        for sample in self._trace.per_sample:
            if sample.cost_usd is not None:
                total += sample.cost_usd
        self._trace.total_cost_usd = round(total, 6)
        # Spot savings is the difference between on-demand and spot.
        # The executor already uses on-demand pricing in cost_usd;
        # spot_savings is the theoretical savings if the job ran on Spot
        # instead. For simplicity, we estimate this as a fixed fraction
        # (~40%) of total on-demand cost, matching the default pricing
        # ratio ($0.05 on-demand vs $0.03 spot).
        if total > 0:
            savings_ratio = (
                DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR - DEFAULT_SPOT_PRICE_PER_VCPU_HOUR
            ) / DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR
            self._trace.spot_savings_usd = round(total * savings_ratio, 6)
        else:
            self._trace.spot_savings_usd = 0.0

    def checkpoint_sample(self, sid: str) -> None:
        """Write an incremental run.json checkpoint for a single sample.

        Called after each sample completes (success or failure) inside
        the fan-out loop so SSE clients see live progress without waiting
        for campaign end (issue #275).

        The checkpoint updates only the per-sample entry for *sid* using
        atomic write (temp file + rename).  If run.json does not exist yet
        (campaign not started), this is a no-op.
        """
        state = self._sample_state.get(sid)
        if state is None:
            return

        # NOTE: unlike finalize_samples, the incremental checkpoint does
        # NOT record a per-sample cost metric — it only writes the
        # run.json row (matches the pre-#1462 behaviour exactly).
        trace = self._build_sample_trace(sid, state, record_cost=False)
        try:
            self._trace.update_sample(trace)
        except Exception as exc:
            self.consecutive_checkpoint_failures += 1
            log.warning(
                "checkpoint failed for sample %s (consecutive failures: %d): %s",
                sid,
                self.consecutive_checkpoint_failures,
                exc,
                exc_info=True,
            )
            if self.consecutive_checkpoint_failures >= 3:
                log.error(
                    "too many consecutive checkpoint failures (%d) — aborting campaign",
                    self.consecutive_checkpoint_failures,
                )
                raise
            return
        self.consecutive_checkpoint_failures = 0

    def _build_sample_trace(
        self,
        sid: str,
        state: dict[str, object],
        record_cost: bool = True,
    ) -> SampleTrace:
        """Assemble one SampleTrace row from the per-sample state dict.

        ``record_cost=False`` skips the per-sample cost-metric recording
        (used by the incremental checkpoint path, which historically
        never recorded cost metrics).
        """
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
        # Worker tracking (issue #105): extract from per-sample state.
        worker_id_obj = state.get("worker_id")
        worker_id = None if worker_id_obj is None else str(worker_id_obj)
        worker_ip_obj = state.get("worker_ip")
        worker_ip = None if worker_ip_obj is None else str(worker_ip_obj)
        worker_region_obj = state.get("worker_region")
        worker_region = None if worker_region_obj is None else str(worker_region_obj)
        # Cost tracking (issue #126): extract from per-sample state.
        cost_usd_obj = state.get("cost_usd")
        cost_usd: float | None = None if cost_usd_obj is None else float(str(cost_usd_obj))
        billed_duration_obj = state.get("billed_duration_seconds")
        billed_duration_seconds: float | None = (
            None if billed_duration_obj is None else float(str(billed_duration_obj))
        )
        # Observability: record per-sample cost metric (issue #132).
        # Forward the per-sample trace_id so the metric can be joined
        # to a distributed trace (issue #436).
        trace_id = self.trace_id_for(sid)
        if record_cost:
            self._obs.record_sample_cost(sid, cost_usd, trace_id=trace_id)
        return SampleTrace(
            sample_id=sid,
            status=status,
            elapsed_s=0.0,
            apply_exit_code=int(str(state.get("apply_exit_code", 0))),
            sim_exit_code=int(str(state.get("sim_exit_code", 0))),
            extract_exit_code=int(str(state.get("extract_exit_code", 0))),
            eplusout_sql=eplusout_sql,
            error_summary=error_summary,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            worker_id=worker_id,
            worker_ip=worker_ip,
            worker_region=worker_region,
            cost_usd=cost_usd,
            billed_duration_seconds=billed_duration_seconds,
            trace_id=trace_id,
        )
