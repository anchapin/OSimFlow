"""Chaos wiring for Campaign (issue #1462 extraction).

This module extracts chaos-engine wiring from the Campaign class
(issue #1013 originally introduced the integration):

- ``build_default_chaos_engine`` turns the user-facing
  :class:`~osimflow.config.ChaosConfig` knobs into the matching fault
  injectors registered on a fresh :class:`~osimflow.chaos.ChaosEngine`,
- :class:`CampaignChaosWiring` owns the engine-selection logic
  (explicitly supplied engine → ``cfg.chaos`` → disabled) and the
  schedule-aware ``maybe_inject`` hook invoked around every DAG step
  boundary and per-sample submission.

The wiring is invisible to non-chaos campaigns: the engine stays
``None`` for every campaign that does not opt in, and failures inside
a user-supplied engine never propagate to the campaign.
"""

import logging
from typing import Any

from .chaos import (
    ChaosEngine,
    CPUSpikeInjector,
    KillSwitchSimulator,
    MemoryPressureInjector,
    NetworkDelayInjector,
)
from .monitoring import RunTrace

log = logging.getLogger("osimflow.campaign")


def build_default_chaos_engine(cfg: Any) -> ChaosEngine:
    """Build a :class:`ChaosEngine` from :class:`ChaosConfig` settings.

    Issue #1013 wires the chaos module into :class:`Campaign`; this
    helper turns the user-facing config knobs into the matching
    fault injectors and registers them on a fresh ``ChaosEngine``.
    The schedule is *not* enforced here — it lives in
    :meth:`CampaignChaosWiring.maybe_inject` so the engine itself stays
    neutral.

    All scenario names are validated at config parse time in
    :func:`osimflow.config._parse_chaos_scenarios` (issue #1209), so
    no further unknown-name handling is needed here.
    """
    engine = ChaosEngine(enabled=True)
    scenarios = list(cfg.scenarios)
    for name in scenarios:
        if name in ("kill_switch", "kill_switch_simulator"):
            engine.register(KillSwitchSimulator(fail_after=cfg.fail_after))
        elif name == "network_delay":
            engine.register(
                NetworkDelayInjector(
                    delay_s=cfg.delay_s,
                    jitter_s=cfg.jitter_s,
                    probability=cfg.probability,
                )
            )
        elif name == "cpu_spike":
            engine.register(
                CPUSpikeInjector(
                    duration_s=cfg.duration_s,
                    intensity=cfg.intensity,
                    probability=cfg.probability,
                )
            )
        elif name == "memory_pressure":
            engine.register(
                MemoryPressureInjector(
                    size_mb=cfg.size_mb,
                    duration_s=cfg.duration_s,
                    probability=cfg.probability,
                )
            )
    return engine


class CampaignChaosWiring:
    """Owns the ChaosEngine lifecycle and schedule-aware injection.

    Constructed from the :class:`~osimflow.config.CampaignConfig` and
    the optional explicitly-supplied engine (the ``chaos_engine=``
    parameter of :class:`~osimflow.campaign.Campaign`).  Mirrors the
    ``_campaign_cost_tracker.py`` collaborator pattern.
    """

    def __init__(self, cfg: Any, chaos_engine: ChaosEngine | None = None) -> None:
        self._cfg = cfg
        chaos_cfg = getattr(cfg, "chaos", None)
        if chaos_engine is not None:
            self.engine: ChaosEngine | None = chaos_engine
        elif chaos_cfg is not None and chaos_cfg.enabled and chaos_cfg.scenarios:
            self.engine = build_default_chaos_engine(chaos_cfg)
        else:
            self.engine = None
        if self.engine is not None:
            scenarios_repr: object
            schedule_repr: object
            if chaos_cfg is not None and chaos_cfg.scenarios:
                scenarios_repr = list(chaos_cfg.scenarios)
                schedule_repr = chaos_cfg.schedule
            else:
                # User supplied a custom engine; describe what is
                # actually registered so the log line is not a lie.
                scenarios_repr = [
                    getattr(inj, "fault_type", None)
                    and getattr(inj.fault_type, "value", None)
                    or type(inj).__name__
                    for inj in self.engine._injectors
                ]
                schedule_repr = "custom"
            log.info(
                "chaos engine enabled: scenarios=%s schedule=%s",
                scenarios_repr,
                schedule_repr,
            )

    def maybe_inject(
        self,
        step_name: str,
        when: str,
        trace: RunTrace,
        target_id: str | None = None,
    ) -> None:
        """Opt-in chaos fault injection (issue #1013).

        Called from ``_run_one_generation`` before/after each DAG step
        and from the per-sample fan-out loops. No-op unless the
        campaign has an active ``ChaosEngine`` and the configured
        ``cfg.chaos.schedule`` matches *when*. The schedule string
        is intentionally single-valued so a campaign either fires
        on step boundaries, on per-sample boundaries, or never —
        combining schedules is not supported in this iteration.

        Failures inside the engine never propagate: every injector
        is wrapped in its own try/except in
        :meth:`ChaosEngine.inject`, and we wrap the call here for
        defence in depth so a buggy user-supplied ``chaos_engine``
        cannot break the campaign.

        Parameters
        ----------
        step_name
            The DAG step the fault is being attached to. Used both
            for logging and for the ``step`` field of the recorded
            invocation.
        when
            One of ``"before_step"``, ``"after_step"``, or
            ``"per_sample"``. Other values are silently ignored.
        trace
            The live campaign RunTrace — chaos invocations are
            recorded under ``run.json.chaos_invocations``.
        target_id
            Identifier of the injection target — typically the
            sample ID for ``per_sample`` injections and the step
            name for step-boundary injections. Defaults to
            ``step_name`` so callers don't have to invent an ID.
        """
        engine = self.engine
        if engine is None or not engine.enabled:
            return
        chaos_cfg = getattr(self._cfg, "chaos", None)
        schedule = getattr(chaos_cfg, "schedule", "none")
        if schedule == "none" or schedule != when:
            return
        tid = target_id if target_id is not None else step_name
        try:
            results = engine.inject(tid)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "chaos inject failed for %s/%s: %s (continuing)",
                step_name,
                tid,
                exc,
                exc_info=True,
            )
            return
        if results:
            trace.record_chaos_invocation(
                step=step_name,
                when=when,
                target_id=tid,
                results=results,
            )
            log.info(
                "chaos: %s @ %s target=%s injected=%d",
                step_name,
                when,
                tid,
                sum(1 for r in results if r.injected),
            )
