"""Record-path exception containment in ObservabilityManager (issue #1668).

Acceptance criteria (issue #1668):

  * Every ``ObservabilityManager.record_*`` method catches and logs
    backend exceptions and never propagates them to the Campaign hot
    path.
  * A mocked CloudWatch backend whose ``flush()`` raises at the
    20-metric buffer boundary (the inline flush performed by
    ``CloudWatchBackend._add_metric``) must not disturb sample
    accounting: the fan-out success callback still completes, the
    sample is marked ``sim_status="ok"``, ``mark_completed`` (not
    ``mark_failed``) is recorded, and no ``sample.failed`` alert
    fires.
  * A failing backend does not suppress the remaining backends
    (per-backend containment, mirroring the coordinated flush of
    issue #1332).

Motivation: the Campaign calls ``record_sample_status`` from inside
the fan-out success callback ``_on_success`` (campaign.py, the
RUN_OPENSTUDIO_SIM / APPLY_PARAMETERS / EXTRACT_KPIS steps).  Before
containment, a CloudWatch throttling error raised by the inline
buffer flush escaped into ``_await_one``'s ``except Exception`` and
routed a successful sample through ``mark_sample_failed`` — recording
hours of compute as failed in ``run.json`` / ``failed_simulations.csv``
and firing a false ``sample.failed`` alert.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow import CampaignConfig, CloudWatchBackend, ObservabilityManager
from osimflow._campaign_fanout import FanoutDeps, mark_sample_failed, submit_and_await_all
from osimflow.executors import Handle
from osimflow.monitoring import RunTrace
from osimflow.observability import ObservabilityBackend


def _make_cfg(**overrides: object) -> CampaignConfig:
    """Create a CampaignConfig with sensible defaults for testing."""
    defaults: dict[str, object] = {
        "input_variables": Path("/tmp/variables.yml"),
        "template_sim_package": Path("/tmp/template"),
        "n_samples": 2,
        "outdir": Path("/tmp/results"),
        "openstudio_version": "3.11.0",
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)  # type: ignore[arg-type]


class _RaisingBackend(ObservabilityBackend):
    """A backend whose every record method raises — simulates a dead backend."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or RuntimeError("backend exploded")
        self.flush_calls = 0

    def record_step_duration(
        self, step_name: str, duration_s: float, generation: int = 0, *, trace_id: str | None = None
    ) -> None:
        raise self.exc

    def record_sample_metric(
        self, sample_id: str, metric_name: str, value: float, *, trace_id: str | None = None
    ) -> None:
        raise self.exc

    def record_campaign_duration(self, duration_s: float, *, trace_id: str | None = None) -> None:
        raise self.exc

    def flush(self) -> None:
        self.flush_calls += 1
        raise self.exc


class _SpyBackend(ObservabilityBackend):
    """A backend that records every call it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def record_step_duration(
        self, step_name: str, duration_s: float, generation: int = 0, *, trace_id: str | None = None
    ) -> None:
        self.calls.append(("step_duration", step_name, duration_s, generation, trace_id))

    def record_sample_metric(
        self, sample_id: str, metric_name: str, value: float, *, trace_id: str | None = None
    ) -> None:
        self.calls.append(("sample_metric", sample_id, metric_name, value, trace_id))

    def record_campaign_duration(self, duration_s: float, *, trace_id: str | None = None) -> None:
        self.calls.append(("campaign_duration", duration_s, trace_id))

    def flush(self) -> None:
        pass


def _manager_with_backends(*backends: ObservabilityBackend) -> ObservabilityManager:
    """Build an ObservabilityManager wired to the given backends directly."""
    mgr = ObservabilityManager(_make_cfg(observability="none"))
    mgr._backends = list(backends)
    return mgr


# ---------------------------------------------------------------------------
# CloudWatch inline buffer-flush failure (the motivating scenario)
# ---------------------------------------------------------------------------
class TestCloudWatchInlineFlushContainment:
    """A raising inline CloudWatch flush must not escape the record path."""

    def test_record_sample_status_at_buffer_boundary_does_not_raise(self) -> None:
        """Drive record_sample_status until the 20-metric buffer boundary.

        The 20th call triggers ``CloudWatchBackend._add_metric``'s inline
        ``flush()``; the mocked client raises a throttling-style error on
        ``put_metric_data``.  The exception must be contained at the
        ObservabilityManager layer — none of the 20 record calls may
        propagate it into the Campaign hot path.
        """
        backend = CloudWatchBackend(namespace="Test/Containment")
        fake_cw = MagicMock()
        fake_cw.put_metric_data.side_effect = RuntimeError(
            "An error occurred (ThrottlingException) when calling the "
            "PutMetricData operation: Rate exceeded"
        )
        backend._client = fake_cw

        mgr = _manager_with_backends(backend)

        # _FLUSH_SIZE record calls: the last one crosses the buffer
        # boundary and performs the inline flush that raises.
        for i in range(CloudWatchBackend._FLUSH_SIZE):
            mgr.record_sample_status(f"s{i:03d}", "ok", trace_id=f"t{i:03d}")

        # The inline flush was attempted (behavior intact) ...
        assert fake_cw.put_metric_data.call_count == 1
        # ... and the failed buffer is retained for a later retry flush
        # (no metric silently discarded by the backend itself).
        assert len(backend._buffer) == CloudWatchBackend._FLUSH_SIZE

    def test_record_sample_metric_at_buffer_boundary_does_not_raise(self) -> None:
        backend = CloudWatchBackend(namespace="Test/Containment")
        fake_cw = MagicMock()
        fake_cw.put_metric_data.side_effect = RuntimeError("network unreachable")
        backend._client = fake_cw

        mgr = _manager_with_backends(backend)
        for i in range(CloudWatchBackend._FLUSH_SIZE):
            mgr.record_sample_metric(f"s{i:03d}", "eui", 120.0 + i)
        assert fake_cw.put_metric_data.call_count == 1

    def test_recovery_after_transient_failure(self) -> None:
        """After the throttled inline flush, a successful flush clears the buffer.

        The failed flush retains the buffer (CloudWatchBackend.flush only
        clears on success), so the periodic flush (issue #1186) can
        retry delivery once the transient error clears.
        """
        backend = CloudWatchBackend(namespace="Test/Containment")
        fake_cw = MagicMock()
        calls = {"n": 0}

        def _flaky_put_metric_data(*args: Any, **kwargs: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("ThrottlingException: Rate exceeded")

        fake_cw.put_metric_data.side_effect = _flaky_put_metric_data
        backend._client = fake_cw

        mgr = _manager_with_backends(backend)
        for i in range(CloudWatchBackend._FLUSH_SIZE):
            mgr.record_sample_status(f"s{i:03d}", "ok")

        # First (inline) flush raised; buffer retained.
        assert len(backend._buffer) == CloudWatchBackend._FLUSH_SIZE
        # A later explicit flush succeeds and delivers the retained batch.
        mgr.flush()
        assert len(backend._buffer) == 0
        assert calls["n"] == 2
        delivered = fake_cw.put_metric_data.call_args.kwargs["MetricData"]
        assert len(delivered) == CloudWatchBackend._FLUSH_SIZE


# ---------------------------------------------------------------------------
# Every record_* method contains backend exceptions
# ---------------------------------------------------------------------------
class TestRecordMethodContainment:
    """No record_* method propagates a backend exception (issue #1668)."""

    @pytest.mark.parametrize(
        "record_call",
        [
            pytest.param(
                lambda mgr: mgr.record_step_duration("RUN_OPENSTUDIO_SIM", 42.0, generation=1),
                id="record_step_duration",
            ),
            pytest.param(
                lambda mgr: mgr.record_sample_metric("s001", "eui", 120.0, trace_id="t1"),
                id="record_sample_metric",
            ),
            pytest.param(
                lambda mgr: mgr.record_sample_cost("s001", 0.05, trace_id="t1"),
                id="record_sample_cost",
            ),
            pytest.param(
                lambda mgr: mgr.record_sample_status("s001", "ok", trace_id="t1"),
                id="record_sample_status_ok",
            ),
            pytest.param(
                lambda mgr: mgr.record_sample_status("s001", "failed", trace_id="t1"),
                id="record_sample_status_failed",
            ),
            pytest.param(
                lambda mgr: mgr.record_campaign_duration(3600.0),
                id="record_campaign_duration",
            ),
        ],
    )
    def test_record_methods_never_propagate(
        self, record_call: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        mgr = _manager_with_backends(_RaisingBackend())
        with caplog.at_level(logging.ERROR, logger="osimflow.campaign"):
            record_call(mgr)  # must not raise
        assert any("failed to record" in record.getMessage() for record in caplog.records), (
            "backend failure must be logged, not silently swallowed"
        )

    def test_raising_backend_failure_is_logged_with_exception_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        mgr = _manager_with_backends(_RaisingBackend(RuntimeError("cw throttled")))
        with caplog.at_level(logging.ERROR, logger="osimflow.campaign"):
            mgr.record_sample_status("s001", "ok")
        matching = [r for r in caplog.records if "failed to record" in r.getMessage()]
        assert len(matching) == 1
        assert "cw throttled" in matching[0].getMessage()
        assert "_RaisingBackend" in matching[0].getMessage()  # backend class name logged
        assert matching[0].exc_info is not None  # traceback attached for debugging


# ---------------------------------------------------------------------------
# Per-backend containment — one failing backend must not starve the rest
# ---------------------------------------------------------------------------
class TestMultiBackendContainment:
    """A failing backend does not suppress recording on remaining backends."""

    def test_first_backend_failure_does_not_suppress_second_backend(self) -> None:
        spy = _SpyBackend()
        mgr = _manager_with_backends(_RaisingBackend(), spy)

        mgr.record_sample_status("s042", "ok", trace_id="t042")

        assert spy.calls == [("sample_metric", "s042", "status", 1.0, "t042")]

    def test_all_backends_attempted_when_middle_backend_fails(self) -> None:
        first, third = _SpyBackend(), _SpyBackend()
        mgr = _manager_with_backends(first, _RaisingBackend(), third)

        mgr.record_step_duration("EXTRACT_KPIS", 5.0)

        assert first.calls == [("step_duration", "EXTRACT_KPIS", 5.0, 0, None)]
        assert third.calls == [("step_duration", "EXTRACT_KPIS", 5.0, 0, None)]

    def test_null_backend_still_works_alongside_raising_backend(self) -> None:
        from osimflow.observability import NullBackend

        null = NullBackend()
        mgr = _manager_with_backends(_RaisingBackend(), null)
        # NullBackend accepts everything; the raising neighbour is contained.
        mgr.record_sample_status("s001", "failed")
        mgr.record_campaign_duration(1.0)


# ---------------------------------------------------------------------------
# Fan-out integration — the acceptance-criteria scenario
# ---------------------------------------------------------------------------
class TestFanoutRecordPath:
    """The success callback's record_sample_status must not fail the sample.

    Mirrors the Campaign's ``_on_success`` for RUN_OPENSTUDIO_SIM
    (campaign.py): the callback updates ``sample_state`` with
    ``sim_status="ok"`` and then calls
    ``obs.record_sample_status(sid, "ok", trace_id=...)``.  With a
    CloudWatch backend whose inline buffer flush raises, the sample
    must still complete: ``mark_completed`` (not ``mark_failed``), no
    ``sample.failed`` alert, and ``sim_status`` stays ``"ok"``.
    """

    def _make_deps(
        self, cfg: CampaignConfig, obs: ObservabilityManager
    ) -> tuple[FanoutDeps, dict[str, Any]]:
        job_queue = MagicMock()
        maybe_alert = MagicMock()
        checkpoint = MagicMock()
        trace = RunTrace(campaign_id="containment-test", config_summary={})
        state: dict[str, dict[str, object]] = {}
        trace_ids: dict[str, str] = {}

        def _build_deps(mark_failed_cb: Any) -> FanoutDeps:
            return FanoutDeps(
                cfg=cfg,
                trace=trace,
                obs=obs,
                job_queue=job_queue,
                sample_state=state,
                max_workers=1,
                effective_max_workers=lambda: 1,
                compute_await_deadline=lambda step: None,
                trace_id_for=lambda sid: trace_ids.setdefault(sid, f"trace-{sid}"),
                checkpoint_sample=checkpoint,
                mark_sample_failed=mark_failed_cb,
                maybe_alert=maybe_alert,
                check_cancel_requested=lambda: False,
                cancel_requested=lambda: False,
                check_pause_requested=lambda: False,
                write_paused_trace=lambda: None,
            )

        def _mark_failed(sid: str, step: str, err: BaseException, tid: str) -> None:
            mark_sample_failed(_build_deps(lambda *a: None), sid, step, err, tid)

        deps = _build_deps(_mark_failed)
        spies: dict[str, Any] = {
            "job_queue": job_queue,
            "maybe_alert": maybe_alert,
            "checkpoint": checkpoint,
            "state": state,
        }
        return deps, spies

    def _flush_raising_cloudwatch_manager(self) -> tuple[ObservabilityManager, MagicMock]:
        backend = CloudWatchBackend(namespace="Test/Fanout")
        fake_cw = MagicMock()
        fake_cw.put_metric_data.side_effect = RuntimeError("ThrottlingException: Rate exceeded")
        backend._client = fake_cw
        return _manager_with_backends(backend), fake_cw

    def test_successful_sample_not_marked_failed_when_flush_raises(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_pkg,
            n_samples=1,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        obs, fake_cw = self._flush_raising_cloudwatch_manager()
        deps, spies = self._make_deps(cfg, obs)

        fut: Future[Any] = Future()
        fut.set_result(Path("/tmp/work/sim/sample_0"))
        handle = Handle(job_id="job-0", _future=fut, worker_id="local", worker_ip="testhost")

        def _on_success(result_path: Any, sid: str = "sample_0") -> None:
            # Mirror of the Campaign's RUN_OPENSTUDIO_SIM success path.
            state = deps.sample_state.setdefault(sid, {})
            state["sim_exit_code"] = 0
            state["sim_status"] = "ok"
            deps.obs.record_sample_status(sid, "ok", trace_id=deps.trace_id_for(sid))

        submit_and_await_all(
            deps,
            {"sample_0": (handle, _on_success)},
            "RUN_OPENSTUDIO_SIM",
        )

        # With a single buffered metric the flush boundary is never
        # reached, so no network call is attempted at all:
        assert fake_cw.put_metric_data.call_count == 0
        # ... but the sample still completed successfully:
        spies["job_queue"].mark_completed.assert_called_once_with("sample_0_RUN_OPENSTUDIO_SIM")
        spies["job_queue"].mark_failed.assert_not_called()
        spies["maybe_alert"].assert_not_called()  # no sample.failed alert
        assert spies["state"]["sample_0"]["sim_status"] == "ok"

    def test_sample_at_buffer_boundary_still_completes_ok(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """The exact issue #1668 scenario: flush raises AT the boundary.

        19 prior record calls fill the buffer; the success callback's
        record_sample_status is the 20th metric and triggers the inline
        flush, which raises.  The sample must still complete with
        ``sim_status="ok"`` and no ``sample.failed`` alert.
        """
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_pkg,
            n_samples=1,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        obs, fake_cw = self._flush_raising_cloudwatch_manager()
        deps, spies = self._make_deps(cfg, obs)

        # Fill the buffer to one below the flush boundary.
        for i in range(CloudWatchBackend._FLUSH_SIZE - 1):
            obs.record_sample_status(f"warmup_{i:03d}", "ok")

        fut: Future[Any] = Future()
        fut.set_result(Path("/tmp/work/sim/sample_0"))
        handle = Handle(job_id="job-0", _future=fut, worker_id="local", worker_ip="testhost")

        def _on_success(result_path: Any, sid: str = "sample_0") -> None:
            state = deps.sample_state.setdefault(sid, {})
            state["sim_exit_code"] = 0
            state["sim_status"] = "ok"
            deps.obs.record_sample_status(sid, "ok", trace_id=deps.trace_id_for(sid))

        submit_and_await_all(
            deps,
            {"sample_0": (handle, _on_success)},
            "RUN_OPENSTUDIO_SIM",
        )

        # The inline flush fired from inside the success callback and raised.
        assert fake_cw.put_metric_data.call_count == 1
        # Yet the sample completed — the acceptance criterion of #1668.
        spies["job_queue"].mark_completed.assert_called_once_with("sample_0_RUN_OPENSTUDIO_SIM")
        spies["job_queue"].mark_failed.assert_not_called()
        spies["maybe_alert"].assert_not_called()
        assert spies["state"]["sample_0"]["sim_status"] == "ok"
