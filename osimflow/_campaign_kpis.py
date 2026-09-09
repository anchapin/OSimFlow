"""EXTRACT_KPIS step + worker direct-to-storage publish (issue #1679).

This module extracts the EXTRACT_KPIS per-sample fan-out step and its
companion worker direct-to-storage push helper from
``osimflow.campaign`` (issue #1679, following the #1462 / #1542
extraction wave):

- :meth:`CampaignKpisMixin.step_extract_kpis` — fan-out over the
  simulated samples: phase 1 builds the cache-lookup map, phase 2/3
  bounded-submit-and-await the per-sample ``extract_fn`` work in
  chunks (issues #286, #1533, #1566), phase 4 records any
  failures.  All four sub-phases use the shared
  :func:`osimflow._campaign_fanout.dispatch_step_work` helper
  (issue #1679) to collapse the historical hand-rolled
  ``task_queue-vs-executor`` branching.
- :meth:`CampaignKpisMixin._publish_sample_results` — worker
  direct-to-storage push for ``kpis.json`` + atomic
  ``_manifest.json`` + best-effort Coordinator status (issue
  #625).  Owned by this mixin because the only caller is
  ``step_extract_kpis``'s on-success / on-failure closures; moving
  it here keeps the storage-write path colocated with the step.

Mixin pattern: :class:`CampaignKpisMixin` declares the attribute
surface it relies on as annotation-only class attributes (the same
shape as :class:`osimflow._campaign_analysis.CampaignAnalysisMixin`).
``Campaign`` inherits the mixin so the historical method surface
— ``campaign.step_extract_kpis(...)``,
``campaign._publish_sample_results(...)`` — is unchanged and the
data-driven dispatcher
(``getattr(self, step_info.method)``) still resolves the method.

Issue #1542 rule: no import or type-reference of ``Campaign``.
"""

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._campaign_fanout import dispatch_step_work
from ._campaign_lifecycle import CampaignPauseRequested
from ._campaign_observability import ObservabilityManager
from .cache import CacheKey, sha256_of_dict
from .config import CampaignConfig
from .executors import BaseExecutor, Handle
from .executors.transport import ResultTransportConfig
from .monitoring import RunTrace
from .storage import ResultStorageUploader
from .taskqueue import ConsumerQueue
from .work import publish_kpi_results

log = logging.getLogger("osimflow.campaign")


# Type alias mirroring the campaign-level SampleDict (issue #1542:
# kept as an alias here so this module's annotations read the same
# as the campaign-level definition, no behavioural dependency).
SampleDict = dict[str, Path]


class CampaignKpisMixin:
    """EXTRACT_KPIS fan-out step + worker direct-to-storage push (issue #1679).

    Provides :meth:`step_extract_kpis` (the per-sample ``extract_fn``
    fan-out) and :meth:`_publish_sample_results` (the per-sample
    storage publish used by ``step_extract_kpis``'s on-success and
    on-failure closures).
    """

    # Annotation-only attribute surface the methods rely on.  Campaign
    # provides all of these at runtime; declaring them here keeps the
    # mixin self-contained for mypy --strict without any Campaign
    # back-reference (issue #1542 rule).
    cfg: CampaignConfig
    trace: RunTrace
    cache: Any  # Cache interface — concrete type is build_cache() result
    extract_fn: Callable[..., Path]
    task_queue: ConsumerQueue | None
    executor: BaseExecutor
    _obs: ObservabilityManager
    _sample_state: dict[str, dict[str, object]]
    _cost_tracker: Any  # CampaignCostTracker or None
    _result_storage: ResultStorageUploader | None
    _result_transport_config: ResultTransportConfig | None
    _python_container_image: str
    _python_container_digest: str

    def _code_hash_with_byos(self, byos_key: str) -> str:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _check_cancel_requested(self) -> bool:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _check_pause_requested(self) -> bool:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _check_quota_exceeded(self) -> bool:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _write_paused_trace(self) -> None:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _trace_id_for(self, sample_id: str) -> str:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _maybe_alert(self, event_type: str, context: dict[str, Any]) -> None:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _record_costs(self, step_name: str, cost_usd: float, spot_savings_usd: float) -> None:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _fanout_submit_chunk_size(self, total: int) -> int:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _fanout_submit_interval_s(self) -> float:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _submit_and_await_all(
        self,
        submissions: dict[str, tuple[Handle, Callable[[Any], None]]],
        step_name: str,
    ) -> None:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _coordinator_url(self) -> str | None:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _coordinator_api_key(self) -> str | None:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    # ------------------------------------------------------------------
    # EXTRACT_KPIS step
    # ------------------------------------------------------------------
    def step_extract_kpis(  # noqa: PLR0912, PLR0915
        self,
        simulated: SampleDict,
        generation: int = 0,
    ) -> list[Path]:
        """Fan-out: for each simulated sample, extract KPIs."""
        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before EXTRACT_KPIS")

        # Soft pause (issue #553): skip this step entirely if pause was requested.
        if self._check_pause_requested():
            self._write_paused_trace()
            raise CampaignPauseRequested("pause requested before EXTRACT_KPIS")

        out: list[Path] = []
        n = len(simulated)
        self.trace.step_started("EXTRACT_KPIS", total=n)

        # --- Phase 1: cache check for all samples ---
        pending: dict[str, dict[str, Any]] = {}
        os_version = self.cfg.openstudio_version
        for sid, sim_dir in simulated.items():
            inputs_hash = sha256_of_dict(
                {
                    "sim_dir": str(sim_dir),
                    "sid": sid,
                    "os_version": os_version,
                    # Issue #1082: include the KPI filter in the cache key so
                    # that changing --kpis invalidates stale KPI JSON.
                    "kpis": list(self.cfg.kpis) if self.cfg.kpis else [],
                }
            )
            key = CacheKey(
                step="EXTRACT_KPIS",
                sample_id=sid,
                openstudio_version=os_version,
                inputs_sha256=inputs_hash,
                code_sha256=self._code_hash_with_byos("byos_kpi"),
                container_digest=self._python_container_digest,
                generation=generation,
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
            pending[sid] = {
                "sim_dir": sim_dir,
                "kpi_dir": kpi_dir,
                "key": key,
                "state": state,
                "os_version": os_version,
            }

        # --- Phase 2/3: bounded submit and await in chunks ---
        pending_items = list(pending.items())
        # Zero-based sample index within the campaign (issue #625 manifest field).
        index_map: dict[str, int] = {sid: i for i, (sid, _c) in enumerate(pending_items)}
        chunk_size = (
            self._fanout_submit_chunk_size(len(pending_items)) if pending_items else 1
        )  # 1 is unused when pending_items is empty, but avoids range(0,0,0)
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
                    now = time.monotonic()
                    if now < next_submit_at:
                        time.sleep(next_submit_at - now)
                    next_submit_at = max(next_submit_at, now) + submit_interval_s
                handle = dispatch_step_work(
                    task_queue=self.task_queue,
                    executor=self.executor,
                    fn=self.extract_fn,
                    task_args=(
                        ctx["sim_dir"],
                        sid,
                        ctx["kpi_dir"],
                    ),
                    task_kwargs={
                        "openstudio_version": ctx["os_version"],
                        "kpis": self.cfg.kpis,
                        "max_retries": self.cfg.max_sample_retries,
                    },
                    exec_args=(
                        ctx["sim_dir"],
                        sid,
                        ctx["kpi_dir"],
                    ),
                    exec_kwargs={
                        "name": f"kpi_{sid}",
                        "cpus": 1,
                        "memory_mb": 1024,
                        "time_min": 10,
                        "container": self._python_container_image,
                        "container_digest": self._python_container_digest,
                        "result_hint": Path(ctx["kpi_dir"]) / f"kpi_{sid}.json",
                        "max_retries": self.cfg.max_sample_retries,
                        "transport": self._result_transport_config,
                        "openstudio_version": ctx["os_version"],
                        "kpis": self.cfg.kpis,
                    },
                )

                key = ctx["key"]
                state = ctx["state"]
                # Bind loop variables so each closure captures its own sample.
                _sim_dir = ctx["sim_dir"]
                _sample_index = index_map[sid]

                def _on_success(
                    result_path: Any,
                    _sid: str = sid,
                    _key: CacheKey = key,
                    _state: dict[str, object] = state,
                    _sim_dir: Path = _sim_dir,
                    _index: int = _sample_index,
                ) -> None:
                    self.cache.store(_key, Path(result_path), exit_code=0)
                    out.append(Path(result_path))
                    _state["extract_exit_code"] = 0
                    _state["extract_status"] = "ok"
                    self.trace.step_item_done("EXTRACT_KPIS", status="ok")
                    # Record sample status to observability backend immediately
                    # so completed samples are not missed if campaign crashes
                    # before _finalize_samples (issue #847).
                    self._obs.record_sample_status(_sid, "ok", trace_id=self._trace_id_for(_sid))
                    # Worker direct-to-storage push (issue #625): upload
                    # kpis.json + atomic _manifest.json, then report to the
                    # Coordinator. No-op for the LocalStorage backend.
                    self._publish_sample_results(
                        sample_id=_sid,
                        index=_index,
                        simulation_dir=_sim_dir,
                        kpi_path=Path(result_path),
                        exit_code=0,
                        status="completed",
                    )

                submissions[sid] = (handle, _on_success)
            self._submit_and_await_all(submissions, "EXTRACT_KPIS")
        total_cost, total_savings = self._cost_tracker.sum_sample_costs(self._sample_state)
        self._record_costs("EXTRACT_KPIS", total_cost, total_savings)

        # Record failures for samples that didn't succeed.
        for _sid, ctx in pending.items():
            state = ctx["state"]
            if state.get("extract_exit_code") != 0 and "extract_status" not in state:
                state["extract_exit_code"] = 1
                state["extract_status"] = "failed"
                state["error_summary"] = "EXTRACT: unknown error during concurrent execution"
                self.trace.step_item_done("EXTRACT_KPIS", status="failed")
            # Worker direct-to-storage (issue #625): publish a 'failed'
            # manifest for any sample that did not complete cleanly. Successful
            # samples were already published in _on_success above and are
            # skipped here (extract_status == "ok" / "cached").
            # Also record sample status to observability backend (issue #847).
            if state.get("extract_status") == "failed":
                self._publish_sample_results(
                    sample_id=_sid,
                    index=index_map.get(_sid, -1),
                    simulation_dir=Path(ctx["sim_dir"]),
                    kpi_path=None,
                    exit_code=int(state.get("extract_exit_code", 1) or 1),
                    status="failed",
                )
                self._obs.record_sample_status(_sid, "failed", trace_id=self._trace_id_for(_sid))
                # Send sample failure alert (issue #1180).
                self._maybe_alert(
                    "sample.failed",
                    {
                        "campaign_id": self.trace.campaign_id,
                        "sample_id": _sid,
                        "step": "EXTRACT_KPIS",
                        "status": "failed",
                        "error": "extract exited with non-zero code",
                    },
                )

        self.trace.step_finished(
            "EXTRACT_KPIS",
            cache="MISS×N" if n else "SKIPPED",
            elapsed_s=time.time() - t0,
            exit_code=0,
        )
        self._obs.record_step_duration("EXTRACT_KPIS", time.time() - t0, generation=generation)
        return sorted(out)

    # ------------------------------------------------------------------
    # Worker direct-to-storage push (issue #625).
    # ------------------------------------------------------------------
    def _publish_sample_results(
        self,
        *,
        sample_id: str,
        index: int,
        simulation_dir: Path,
        kpi_path: Path | None,
        exit_code: int,
        status: str,
    ) -> None:
        """Push one sample's results directly to storage (issue #625).

        Uploads ``kpis.json`` + an atomic ``_manifest.json`` to the configured
        :class:`ResultStorage` backend and best-effort reports completion to
        the Coordinator.  This is a no-op when no result storage is configured
        or when the backend is :class:`LocalStorage` (local path unchanged).

        Uses the **raw** synchronous backend (``ResultStorageUploader._storage``)
        rather than the async upload queue, because the manifest must become
        visible strictly after ``kpis.json`` — the async wrapper cannot
        guarantee that ordering.
        """
        if self._result_storage is None:
            return
        # Access the raw sync backend that the async uploader wraps.
        backend = getattr(self._result_storage, "_storage", None)
        if backend is None:
            return
        try:
            publish_kpi_results(
                storage=backend,
                campaign_id=self.trace.campaign_id,
                sample_id=sample_id,
                index=index,
                simulation_dir=simulation_dir,
                kpi_path=kpi_path,
                exit_code=exit_code,
                status=status,
                archive_intermediates=self.cfg.archive_intermediates,
                coordinator_url=self._coordinator_url(),
                api_key=self._coordinator_api_key(),
                allow_insecure_coordinator=bool(
                    getattr(self.cfg, "allow_insecure_storage_endpoint", False)
                ),
            )
        except OSError as exc:
            # Storage failures must not abort the extract step; the manifest
            # is telemetry/coordination, not the primary result.
            log.warning(
                "EXTRACT_KPIS: direct-to-storage publish failed for %s: %s",
                sample_id,
                exc,
                exc_info=True,
            )
