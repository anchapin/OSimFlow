"""Await deadline flows through ``handle.result(timeout=...)`` (issue #1566).

The campaign's only fan-out await site used to call ``handle.result()``
bare, so the caller-supplied ``timeout`` from
``PollingHandle.result(timeout=...)`` (issue #1465) and the equivalent
Nomad/K8s/AWS implementations only engaged if the caller passed one.
Without an orchestrator-side deadline, a job stuck in a non-terminal
substrate state (Nomad allocation that never reaches terminal,
Docker Swarm service in a non-terminal state, K8s ``time_min=0``
yielding no ``activeDeadlineSeconds``) would park an ``_await_one``
thread forever — a graceful-degradation gap for the HPC/cloud
substrates.

Acceptance criterion: the campaign derives a per-step await deadline
and passes it to ``handle.result(timeout=...)``.  When
``cfg.await_timeout_s`` is set, the deadline is that value (the
opt-in for Nomad / Docker Swarm / mis-configured K8s ``time_min=0``
users).  Otherwise, the deadline is
``max(DEFAULT_STEP_RESOURCES[step_name]["time_min"] * 60, cfg.byos_timeout_s)``
so the orchestrator-side bound accommodates the longest expected
legitimate work.  Returns ``None`` only when the user opted out of
every orchestrator-side bound AND the per-step ``time_min`` resolved
to ``0``/missing — i.e. the user wants the pre-#1566 bare
``handle.result()`` semantics.

A wedged handle whose ``result(timeout=...)`` raises ``TimeoutError``
after the deadline must flow through the existing per-sample failure
recording (``_mark_sample_failed``):

  * ``_job_queue.mark_failed(...)`` is invoked,
  * ``_sample_state[sid]`` carries ``<step>_exit_code=1``,
    ``<step>_status="failed"``, ``error_summary`` with the
    ``TimeoutError`` message,
  * the ``sample.failed`` alert is fired via ``_maybe_alert``,
  * the observability backend receives a ``"failed"`` status metric,
  * the total await time stays within the configured bound (e.g.
    ``await_timeout_s=0.2`` → assert ``total < 1.0s``).

Backwards compatibility (issue #1566 acceptance note):
the deadline is a **floor**, not a strict override.  When the user
supplies ``await_timeout_s=None`` AND ``byos_timeout_s=None``, the
per-step ``DEFAULT_STEP_RESOURCES`` floor still applies (so a wedged
Nomad allocation / Docker Swarm service can no longer park the
campaign forever).  Users with a deliberately unbounded wait must set
``--sample-await-timeout-s`` to a very large value.
"""

from __future__ import annotations

import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from osimflow import Campaign, CampaignConfig
from osimflow.executors import Handle, LocalExecutor


def _make_never_terminal_handle(deadline_s: float) -> Handle:
    """Handle whose ``result(timeout=deadline_s)`` raises ``TimeoutError``.

    Mirrors the substrate behaviour from issue #1465 / #1566: a job
    stuck in a non-terminal state (Nomad allocation / Docker Swarm
    service / mis-configured K8s ``time_min=0``) whose ``PollingHandle``
    polls forever.  When the caller supplies ``timeout=deadline_s``, the
    handle raises ``TimeoutError`` after that many seconds, exactly as
    the substrate-side kill would.
    """

    class _NeverTerminalHandle:
        """Lightweight stand-in for ``PollingHandle`` — only ``.result`` matters here.

        Bypassing the real ``PollingHandle`` keeps the test fast
        (no substrate mock) and focused on the campaign's deadline
        wiring.  ``_future`` is set to an unresolved ``Future`` so
        the ``Handle.result`` base-class path (``self._future.result(timeout=...)``)
        raises ``TimeoutError`` after *deadline_s*, matching what
        ``PollingHandle.result(timeout=...)`` does at the substrate
        level.
        """

        job_id = "stuck-job"
        worker_id: str | None = None
        worker_ip: str | None = None
        worker_region: str | None = None
        cost_usd: float | None = None
        billed_duration_seconds: float | None = None
        error: Exception | None = None
        _future: Future[Any] = Future()  # unresolved → result(timeout=) raises TimeoutError

        def result(self, timeout: float | None = None) -> Any:
            try:
                return self._future.result(timeout=timeout)
            except TimeoutError as exc:
                # PollingHandle.result(timeout=...) raises a TimeoutError
                # with a message ("Timed out after Ns waiting for job
                # 'stuck-job'") so the campaign's ``error_summary`` carries
                # a useful diagnostic.  Mirror that here.
                raise TimeoutError(
                    f"Timed out after {timeout:.1f}s waiting for job 'stuck-job'"
                ) from exc

        def done(self) -> bool:
            return self._future.done()

        def is_failed(self) -> bool:
            return self.error is not None

    return _NeverTerminalHandle()  # type: ignore[return-value]


def _cfg(
    variables_yml: Path,
    template_pkg: Path,
    outdir: Path,
    **overrides: Any,
) -> CampaignConfig:
    defaults: dict[str, Any] = {
        "input_variables": variables_yml,
        "template_sim_package": template_pkg,
        "n_samples": 1,
        "outdir": outdir,
        "openstudio_version": "3.11.0",
        "dry_run": True,
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)


class TestAwaitDeadlineFlow:
    """``_await_one`` must pass a timeout to ``handle.result(...)``."""

    def test_handle_result_receives_timeout_kwarg(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """``handle.result(timeout=...)`` receives the per-step deadline."""
        cfg = _cfg(variables_yml, template_pkg, outdir, await_timeout_s=0.2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        handle = MagicMock()
        handle.result.return_value = "ok"
        handle.job_id = "ok-job"

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (handle, MagicMock()),
        }

        campaign._submit_and_await_all(
            submissions=submissions,
            step_name="EXTRACT_KPIS",
        )

        # ``handle.result`` was called exactly once, with the
        # orchestrator-side deadline as the ``timeout`` kwarg.
        handle.result.assert_called_once()
        call_kwargs = handle.result.call_args.kwargs
        call_args = handle.result.call_args.args
        # ``timeout`` may be passed as positional or keyword.
        if call_kwargs:
            assert "timeout" in call_kwargs
            timeout = call_kwargs["timeout"]
        else:
            assert len(call_args) >= 1
            timeout = call_args[0]
        # When ``await_timeout_s=0.2`` is set, the deadline is exactly
        # that value (the user-supplied deadline wins — issue #1566).
        assert timeout == 0.2

    def test_timeout_error_marks_job_queue_failed(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """A wedged handle whose ``result(timeout=...)`` raises ``TimeoutError`` routes through ``_mark_sample_failed``."""
        cfg = _cfg(variables_yml, template_pkg, outdir, await_timeout_s=0.2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        handle = _make_never_terminal_handle(deadline_s=0.2)

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (handle, MagicMock()),
        }

        with patch.object(campaign, "_job_queue") as mock_jq:
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="EXTRACT_KPIS",
            )

            failed_keys = [call.args[0] for call in mock_jq.mark_failed.call_args_list]
            assert "sample_0_EXTRACT_KPIS" in failed_keys

            # The timed-out sample must NOT be marked completed.
            completed_keys = [
                call.args[0] for call in mock_jq.mark_completed.call_args_list
            ]
            assert "sample_0_EXTRACT_KPIS" not in completed_keys

    def test_timeout_error_records_failure_in_sample_state(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """``_sample_state[sid]`` carries the failure keys for timed-out samples."""
        cfg = _cfg(variables_yml, template_pkg, outdir, await_timeout_s=0.2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        handle = _make_never_terminal_handle(deadline_s=0.2)

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (handle, MagicMock()),
        }

        with patch.object(campaign, "_checkpoint_sample", lambda sid: None):
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="EXTRACT_KPIS",
            )

        state = campaign._sample_state["sample_0"]
        # ``_mark_sample_failed`` writes ``<step_name.lower()>_exit_code``
        # / ``<step_name.lower()>_status`` — for ``EXTRACT_KPIS`` that
        # is ``extract_kpis_*``.
        assert state["extract_kpis_exit_code"] == 1
        assert state["extract_kpis_status"] == "failed"
        # ``error_summary`` carries the TimeoutError message — the
        # substrate-worded "Timed out after Ns waiting for job '...'"
        # raises by ``PollingHandle.result(timeout=...)``.
        assert "timed out" in state["error_summary"].lower()
        assert "stuck-job" in state["error_summary"]

    def test_timeout_error_fires_sample_failed_alert(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """``sample.failed`` alert is invoked for timed-out samples."""
        cfg = _cfg(variables_yml, template_pkg, outdir, await_timeout_s=0.2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        handle = _make_never_terminal_handle(deadline_s=0.2)

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (handle, MagicMock()),
        }

        with (
            patch.object(campaign, "_checkpoint_sample", lambda sid: None),
            patch.object(campaign, "_maybe_alert") as mock_alert,
        ):
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="EXTRACT_KPIS",
            )

        alert_event_types = [call.args[0] for call in mock_alert.call_args_list]
        assert "sample.failed" in alert_event_types

        failure_alerts = [
            call for call in mock_alert.call_args_list if call.args[0] == "sample.failed"
        ]
        assert failure_alerts, "expected at least one sample.failed alert"
        ctx = failure_alerts[0].args[1]
        assert ctx["sample_id"] == "sample_0"
        assert ctx["step"] == "EXTRACT_KPIS"
        assert ctx["status"] == "failed"

    def test_timeout_error_records_observability_status(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """The observability backend sees ``"failed"`` for timed-out samples."""
        cfg = _cfg(variables_yml, template_pkg, outdir, await_timeout_s=0.2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        handle = _make_never_terminal_handle(deadline_s=0.2)

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (handle, MagicMock()),
        }

        with (
            patch.object(campaign, "_checkpoint_sample", lambda sid: None),
            patch.object(campaign._obs, "record_sample_status", autospec=True) as mock_record,
        ):
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="EXTRACT_KPIS",
            )

        failed_calls = [
            call for call in mock_record.call_args_list if call.args[1] == "failed"
        ]
        assert any(call.args[0] == "sample_0" for call in failed_calls)

    def test_await_terminates_within_deadline(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """The fan-out terminates within the configured bound.

        With ``await_timeout_s=0.2``, the wedged handle's ``TimeoutError``
        must propagate to ``_await_one`` within ~0.2 s, not park the
        thread forever.  Asserts the total elapsed is comfortably under
        1s (a generous bound for CI variance).
        """
        cfg = _cfg(variables_yml, template_pkg, outdir, await_timeout_s=0.2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        handle = _make_never_terminal_handle(deadline_s=0.2)

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (handle, MagicMock()),
        }

        with patch.object(campaign, "_checkpoint_sample", lambda sid: None):
            t0 = time.monotonic()
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="EXTRACT_KPIS",
            )
            elapsed = time.monotonic() - t0

        # 1s is a generous bound: with ``await_timeout_s=0.2`` the
        # TimeoutError fires after 0.2s; we allow up to 1s for CI
        # scheduling overhead and the rest of the failure-recording
        # side-effects.
        assert elapsed < 1.0, (
            f"_submit_and_await_all blocked {elapsed:.2f}s; "
            f"await_timeout_s=0.2 should have raised within 0.2s"
        )


class TestAwaitDeadlineDerivation:
    """``_compute_await_deadline`` derives the floor correctly."""

    def test_default_floor_uses_per_step_time_min(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """With neither ``byos_timeout_s`` nor ``await_timeout_s`` set, the per-step ``time_min`` floor applies."""
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        # EXTRACT_KPIS has DEFAULT_STEP_RESOURCES time_min=10 → 600s.
        assert campaign._compute_await_deadline("EXTRACT_KPIS") == 600.0
        # RUN_OPENSTUDIO_SIM has DEFAULT_STEP_RESOURCES time_min=240 → 14400s.
        assert campaign._compute_await_deadline("RUN_OPENSTUDIO_SIM") == 14400.0
        # AGGREGATE_RESULTS has DEFAULT_STEP_RESOURCES time_min=15 → 900s.
        assert campaign._compute_await_deadline("AGGREGATE_RESULTS") == 900.0
        # Unknown step falls back to DEFAULT_STEP_RESOURCES default
        # time_min=60 → 3600s.
        assert campaign._compute_await_deadline("UNKNOWN_STEP") == 3600.0

    def test_await_timeout_s_is_the_deadline(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """``await_timeout_s`` is the deadline (honoured directly when set).

        When ``await_timeout_s`` is set, it is the deadline regardless of
        the per-step ``time_min`` floor or ``byos_timeout_s``.  This is
        the opt-in for Nomad / Docker Swarm / mis-configured K8s
        ``time_min=0`` users who want a deadline regardless of the
        substrate's own enforcement.
        """
        cfg = _cfg(variables_yml, template_pkg, outdir, await_timeout_s=1800.0)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        # EXTRACT_KPIS default is 600s; await_timeout_s=1800 → 1800.
        assert campaign._compute_await_deadline("EXTRACT_KPIS") == 1800.0
        # RUN_OPENSTUDIO_SIM default is 14400s; await_timeout_s=1800 → 1800.
        assert campaign._compute_await_deadline("RUN_OPENSTUDIO_SIM") == 1800.0

    def test_await_timeout_s_wins_over_byos_timeout_s(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """When both ``await_timeout_s`` and ``byos_timeout_s`` are set, ``await_timeout_s`` wins."""
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            await_timeout_s=120.0,
            byos_timeout_s=7200.0,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        # await_timeout_s=120 wins over byos_timeout_s=7200 and the
        # per-step floor (600 for EXTRACT_KPIS, 14400 for sim).
        assert campaign._compute_await_deadline("EXTRACT_KPIS") == 120.0
        assert campaign._compute_await_deadline("RUN_OPENSTUDIO_SIM") == 120.0

    def test_byos_timeout_s_overrides_per_step_floor(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """When ``await_timeout_s`` is unset, ``byos_timeout_s`` participates in the max."""
        cfg = _cfg(variables_yml, template_pkg, outdir, byos_timeout_s=7200.0)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        # EXTRACT_KPIS default 600, byos=7200 → max = 7200.
        assert campaign._compute_await_deadline("EXTRACT_KPIS") == 7200.0
        # RUN_OPENSTUDIO_SIM default 14400, byos=7200 → max = 14400.
        assert campaign._compute_await_deadline("RUN_OPENSTUDIO_SIM") == 14400.0

    def test_max_of_time_min_and_byos_when_await_unset(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """Without ``await_timeout_s``, the deadline is ``max(time_min, byos_timeout_s)``."""
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            byos_timeout_s=7200.0,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        # AGGREGATE_RESULTS default 900; byos=7200 → max = 7200.
        assert campaign._compute_await_deadline("AGGREGATE_RESULTS") == 7200.0

    def test_zero_byos_timeout_s_is_treated_as_unset(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """``byos_timeout_s=0`` (or negative) is treated as unset (excluded from max)."""
        cfg = _cfg(variables_yml, template_pkg, outdir, byos_timeout_s=0.0)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        # byos=0 is excluded; default floor of 600 (EXTRACT_KPIS) applies.
        assert campaign._compute_await_deadline("EXTRACT_KPIS") == 600.0
