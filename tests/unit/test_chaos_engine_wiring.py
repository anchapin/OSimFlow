"""Unit tests for ``osimflow/_campaign_chaos.py`` (issue #1692).

Closes the coverage gap on :func:`osimflow._campaign_chaos.build_default_chaos_engine`
and :class:`osimflow._campaign_chaos.CampaignChaosWiring`. The full
``Campaign`` integration tests in ``tests/integration/test_chaos_campaign.py``
hand-build engines, so the wiring function a user gets from
``--chaos-scenarios cpu_spike,memory_pressure --chaos-schedule per_sample``
was never exercised directly. These tests:

* assert ``build_default_chaos_engine(cfg)`` registers the correct
  injector set for each documented scenario, an unknown scenario
  name, and an empty scenario list (no-op);
* assert ``_parse_chaos_scenarios`` boundary cases propagate cleanly
  through to the engine (empty / trailing comma / comma string);
* cover the schedule-aware ``maybe_inject`` hook for
  ``before_step`` / ``after_step`` / ``per_sample`` schedules
  (``schedule="none"`` is a no-op) and confirm engine failures are
  contained (the campaign trace keeps running);
* cover the explicit ``chaos_engine=`` constructor path (user-supplied
  engine wins over ``cfg.chaos``, log line reads ``schedule="custom"``);
* cover the ``chaos`` config-disabled branch (``enabled=False`` or
  empty scenarios produce ``engine is None``).

The acceptance criterion is that ``_campaign_chaos.py`` coverage is
high enough to ratchet the per-module floor above the previous
seed (28.33%).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from osimflow._campaign_chaos import (
    CampaignChaosWiring,
    build_default_chaos_engine,
)
from osimflow.chaos import (
    ChaosEngine,
    CPUSpikeInjector,
    FaultType,
    KillSwitchSimulator,
    MemoryPressureInjector,
    NetworkDelayInjector,
)
from osimflow.config import (
    CampaignConfig,
    ChaosConfig,
    _parse_chaos_scenarios,
    load_config,
)
from osimflow.monitoring import RunTrace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chaos_cfg(**overrides: object) -> ChaosConfig:
    """Return a :class:`ChaosConfig` populated with sensible defaults for tests."""
    base = ChaosConfig(
        enabled=True,
        scenarios=["kill_switch"],
        schedule="per_sample",
        probability=1.0,
        delay_s=0.01,
        jitter_s=0.0,
        duration_s=0.05,
        intensity=0.1,
        size_mb=5,
        fail_after=1,
    )
    for key, value in overrides.items():
        object.__setattr__(base, key, value)
    return base


def _minimal_cfg(chaos: ChaosConfig) -> CampaignConfig:
    """Build a minimal ``CampaignConfig`` carrying the given ``chaos`` knob."""
    return CampaignConfig(
        input_variables=Path("/tmp/variables.yml"),
        template_sim_package=Path("/tmp/template"),
        n_samples=1,
        outdir=Path("/tmp/out"),
        openstudio_version="3.11.0",
        chaos=chaos,
    )


def _injector_types(engine: ChaosEngine) -> list[type]:
    """Return the concrete injector classes registered on ``engine``."""
    return [type(inj) for inj in engine._injectors]


def _trace() -> RunTrace:
    """Return a fresh :class:`RunTrace` suitable for ``maybe_inject`` assertions."""
    return RunTrace(
        campaign_id="test-campaign",
        config_summary={"chaos": {"enabled": True}},
    )


def _as_invocation(obj: object) -> dict[str, object]:
    """Cast a ``chaos_invocations`` entry to a dict (mypy escape hatch)."""
    assert isinstance(obj, dict)
    return obj


# ---------------------------------------------------------------------------
# build_default_chaos_engine — single-scenario coverage
# ---------------------------------------------------------------------------


class TestBuildDefaultChaosEngineSingleScenario:
    """Each documented scenario name registers exactly one injector."""

    def test_kill_switch_simulator_registers_kill_switch(self) -> None:
        cfg = _chaos_cfg(scenarios=["kill_switch_simulator"], fail_after=2)
        engine = build_default_chaos_engine(cfg)
        assert _injector_types(engine) == [KillSwitchSimulator]
        # fail_after is plumbed through
        kill = engine._injectors[0]
        assert isinstance(kill, KillSwitchSimulator)
        assert kill._fail_after == 2

    def test_kill_switch_alias_registers_kill_switch(self) -> None:
        """``kill_switch`` is the deprecated alias for ``kill_switch_simulator``."""
        cfg = _chaos_cfg(scenarios=["kill_switch"], fail_after=4)
        engine = build_default_chaos_engine(cfg)
        assert _injector_types(engine) == [KillSwitchSimulator]
        kill = engine._injectors[0]
        assert isinstance(kill, KillSwitchSimulator)
        assert kill._fail_after == 4

    def test_network_delay_registers_network_delay(self) -> None:
        cfg = _chaos_cfg(scenarios=["network_delay"], delay_s=0.25, jitter_s=0.05, probability=0.7)
        engine = build_default_chaos_engine(cfg)
        assert _injector_types(engine) == [NetworkDelayInjector]
        inj = engine._injectors[0]
        assert isinstance(inj, NetworkDelayInjector)
        assert inj._delay_s == 0.25
        assert inj._jitter_s == 0.05
        assert inj._probability == 0.7

    def test_cpu_spike_registers_cpu_spike(self) -> None:
        cfg = _chaos_cfg(scenarios=["cpu_spike"], duration_s=1.5, intensity=0.8, probability=0.6)
        engine = build_default_chaos_engine(cfg)
        assert _injector_types(engine) == [CPUSpikeInjector]
        inj = engine._injectors[0]
        assert isinstance(inj, CPUSpikeInjector)
        assert inj._duration_s == 1.5
        assert inj._intensity == 0.8
        assert inj._probability == 0.6

    def test_memory_pressure_registers_memory_pressure(self) -> None:
        cfg = _chaos_cfg(
            scenarios=["memory_pressure"], size_mb=128, duration_s=0.5, probability=0.4
        )
        engine = build_default_chaos_engine(cfg)
        assert _injector_types(engine) == [MemoryPressureInjector]
        inj = engine._injectors[0]
        assert isinstance(inj, MemoryPressureInjector)
        assert inj._size_mb == 128
        assert inj._duration_s == 0.5
        assert inj._probability == 0.4


# ---------------------------------------------------------------------------
# build_default_chaos_engine — multiple scenarios
# ---------------------------------------------------------------------------


class TestBuildDefaultChaosEngineMultipleScenarios:
    """Multiple scenario names register one injector each, in order."""

    def test_two_scenarios_register_two_injectors(self) -> None:
        cfg = _chaos_cfg(scenarios=["cpu_spike", "memory_pressure"], probability=1.0)
        engine = build_default_chaos_engine(cfg)
        assert _injector_types(engine) == [CPUSpikeInjector, MemoryPressureInjector]

    def test_all_four_scenarios_register_all_four(self) -> None:
        cfg = _chaos_cfg(
            scenarios=[
                "kill_switch_simulator",
                "network_delay",
                "cpu_spike",
                "memory_pressure",
            ],
            probability=1.0,
        )
        engine = build_default_chaos_engine(cfg)
        assert _injector_types(engine) == [
            KillSwitchSimulator,
            NetworkDelayInjector,
            CPUSpikeInjector,
            MemoryPressureInjector,
        ]

    def test_engine_enabled_flag_is_true(self) -> None:
        """``build_default_chaos_engine`` always returns an enabled engine."""
        cfg = _chaos_cfg(scenarios=["cpu_spike"])
        engine = build_default_chaos_engine(cfg)
        assert engine.enabled is True


# ---------------------------------------------------------------------------
# build_default_chaos_engine — empty + unknown scenarios
# ---------------------------------------------------------------------------


class TestBuildDefaultChaosEngineEdgeCases:
    """Empty scenario list produces an empty (but enabled) engine.

    Unknown scenario names fall through every ``if``/``elif`` branch and
    register nothing — the parser at ``_parse_chaos_scenarios`` is the
    single point where validation happens (issue #1209); the wiring
    helper is intentionally tolerant so a future scenario addition
    fails safe (no injector → no fault) instead of raising mid-campaign.
    """

    def test_empty_scenarios_produces_enabled_engine_with_no_injectors(self) -> None:
        cfg = _chaos_cfg(scenarios=[])
        engine = build_default_chaos_engine(cfg)
        assert engine._injectors == []
        assert engine.enabled is True

    def test_unknown_scenario_name_produces_no_injector(self) -> None:
        cfg = _chaos_cfg(scenarios=["not_a_real_scenario"])
        engine = build_default_chaos_engine(cfg)
        # Unknown names silently drop (validation lives in _parse_chaos_scenarios)
        assert engine._injectors == []

    def test_mixed_known_unknown_registers_known_only(self) -> None:
        cfg = _chaos_cfg(scenarios=["cpu_spike", "bogus_scenario", "memory_pressure"])
        engine = build_default_chaos_engine(cfg)
        assert _injector_types(engine) == [CPUSpikeInjector, MemoryPressureInjector]

    def test_input_scenarios_list_is_not_mutated(self) -> None:
        """The wiring helper reads the scenario list via ``list(...)`` copy."""
        scenarios = ["cpu_spike", "memory_pressure"]
        cfg = _chaos_cfg(scenarios=scenarios)
        build_default_chaos_engine(cfg)
        assert scenarios == ["cpu_spike", "memory_pressure"]


# ---------------------------------------------------------------------------
# _parse_chaos_scenarios boundary cases — issue #1209
# ---------------------------------------------------------------------------


class TestParseChaosScenariosBoundaries:
    """The boundary cases at ``_parse_chaos_scenarios`` propagate correctly."""

    def test_none_returns_empty_list(self) -> None:
        assert _parse_chaos_scenarios(None) == []

    def test_empty_string_returns_empty_list(self) -> None:
        assert _parse_chaos_scenarios("") == []

    def test_comma_string_is_split_and_stripped(self) -> None:
        assert _parse_chaos_scenarios("cpu_spike,memory_pressure") == [
            "cpu_spike",
            "memory_pressure",
        ]

    def test_trailing_comma_is_dropped(self) -> None:
        assert _parse_chaos_scenarios("cpu_spike,") == ["cpu_spike"]

    def test_whitespace_around_names_is_stripped(self) -> None:
        assert _parse_chaos_scenarios("  cpu_spike ,  memory_pressure  ") == [
            "cpu_spike",
            "memory_pressure",
        ]

    def test_list_input_is_passthrough(self) -> None:
        assert _parse_chaos_scenarios(["cpu_spike", "memory_pressure"]) == [
            "cpu_spike",
            "memory_pressure",
        ]

    def test_tuple_input_is_accepted(self) -> None:
        assert _parse_chaos_scenarios(("cpu_spike", "memory_pressure")) == [
            "cpu_spike",
            "memory_pressure",
        ]

    def test_list_with_empty_entries_filters_them_out(self) -> None:
        assert _parse_chaos_scenarios(["cpu_spike", "", "memory_pressure"]) == [
            "cpu_spike",
            "memory_pressure",
        ]


# ---------------------------------------------------------------------------
# load_config wires --chaos-scenarios through (issue #1209)
# ---------------------------------------------------------------------------


@pytest.fixture
def example_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A minimal variables.yml + template package fixture (issue #1209 boundary)."""
    REPO_ROOT = Path(__file__).resolve().parents[2]
    EXAMPLE_PKG = REPO_ROOT / "example_package"
    variables_yml = tmp_path / "variables.yml"
    variables_yml.write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: wwr\n"
        "    distribution: uniform\n"
        "    min: 0.2\n"
        "    max: 0.6\n"
        "    measure_argument: SetEnvelopePerformance.wwr\n"
    )
    template = tmp_path / "template"
    import shutil

    shutil.copytree(EXAMPLE_PKG, template)
    outdir = tmp_path / "out"
    outdir.mkdir()
    return variables_yml, template, outdir


class TestLoadConfigChaosScenarios:
    """The CLI flag reaches ``cfg.chaos.scenarios`` via :func:`load_config`."""

    def test_cli_string_round_trips_to_chaos_config(
        self, example_fixture: tuple[Path, Path, Path]
    ) -> None:
        variables_yml, template, outdir = example_fixture
        cfg = load_config(
            {
                "input_variables": variables_yml,
                "template_sim_package": template,
                "n_samples": 1,
                "outdir": outdir,
                "openstudio_version": "3.11.0",
                "chaos_enabled": True,
                "chaos_scenarios": "cpu_spike,memory_pressure",
                "chaos_schedule": "per_sample",
                "chaos_probability": "1.0",
            }
        )
        assert cfg.chaos.enabled is True
        assert cfg.chaos.scenarios == ["cpu_spike", "memory_pressure"]
        assert cfg.chaos.schedule == "per_sample"

    def test_cli_string_with_trailing_comma_is_cleaned(
        self, example_fixture: tuple[Path, Path, Path]
    ) -> None:
        variables_yml, template, outdir = example_fixture
        cfg = load_config(
            {
                "input_variables": variables_yml,
                "template_sim_package": template,
                "n_samples": 1,
                "outdir": outdir,
                "openstudio_version": "3.11.0",
                "chaos_enabled": True,
                "chaos_scenarios": "cpu_spike,",
            }
        )
        assert cfg.chaos.scenarios == ["cpu_spike"]

    def test_load_config_throws_on_unknown_scenario(
        self, example_fixture: tuple[Path, Path, Path]
    ) -> None:
        from osimflow.validation import ValidationError

        variables_yml, template, outdir = example_fixture
        with pytest.raises(ValidationError) as exc:
            load_config(
                {
                    "input_variables": variables_yml,
                    "template_sim_package": template,
                    "n_samples": 1,
                    "outdir": outdir,
                    "openstudio_version": "3.11.0",
                    "chaos_enabled": True,
                    "chaos_scenarios": "not_a_real_scenario",
                }
            )
        assert "unknown scenario" in str(exc.value).lower()

    def test_empty_cli_string_defaults_to_kill_switch(
        self, example_fixture: tuple[Path, Path, Path]
    ) -> None:
        """`--chaos-enabled` with no scenarios defaults to ``kill_switch``.

        The default lives in ``load_config``: a typo in the operator's
        ``--chaos-scenarios`` flag cannot leave the engine enabled-but-empty.
        """
        variables_yml, template, outdir = example_fixture
        cfg = load_config(
            {
                "input_variables": variables_yml,
                "template_sim_package": template,
                "n_samples": 1,
                "outdir": outdir,
                "openstudio_version": "3.11.0",
                "chaos_enabled": True,
                "chaos_scenarios": "",
            }
        )
        assert cfg.chaos.scenarios == ["kill_switch"]


# ---------------------------------------------------------------------------
# CampaignChaosWiring.__init__ — engine selection branches
# ---------------------------------------------------------------------------


class TestCampaignChaosWiringInit:
    """The wiring's __init__ selects the engine correctly."""

    def test_chaos_disabled_yields_no_engine(self) -> None:
        cfg = _minimal_cfg(ChaosConfig(enabled=False, scenarios=["cpu_spike"]))
        wiring = CampaignChaosWiring(cfg)
        assert wiring.engine is None

    def test_chaos_enabled_with_scenarios_builds_default_engine(self) -> None:
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["cpu_spike"],
                probability=1.0,
            )
        )
        wiring = CampaignChaosWiring(cfg)
        assert wiring.engine is not None
        assert _injector_types(wiring.engine) == [CPUSpikeInjector]

    def test_chaos_enabled_with_no_scenarios_yields_no_engine(self) -> None:
        """Empty scenarios with enabled=True is a documented no-op."""
        cfg = _minimal_cfg(ChaosConfig(enabled=True, scenarios=[]))
        wiring = CampaignChaosWiring(cfg)
        assert wiring.engine is None

    def test_no_chaos_attr_on_cfg_yields_no_engine(self) -> None:
        cfg = _minimal_cfg(ChaosConfig())
        # Strip the ``chaos`` attribute to simulate a config object
        # without one.
        object.__delattr__(cfg, "chaos")
        wiring = CampaignChaosWiring(cfg)
        assert wiring.engine is None

    def test_explicit_chaos_engine_wins_over_cfg(self) -> None:
        """User-supplied engine wins over the default engine built from cfg."""
        cfg = _minimal_cfg(ChaosConfig(enabled=True, scenarios=["cpu_spike"], probability=1.0))
        custom_engine = ChaosEngine()
        custom_engine.register(MemoryPressureInjector(size_mb=1, duration_s=0.01))
        wiring = CampaignChaosWiring(cfg, chaos_engine=custom_engine)
        assert wiring.engine is custom_engine
        assert _injector_types(wiring.engine) == [MemoryPressureInjector]


# ---------------------------------------------------------------------------
# CampaignChaosWiring.__init__ — log line shapes (issue #1013)
# ---------------------------------------------------------------------------


class TestCampaignChaosWiringLogLine:
    """The "chaos engine enabled" log line shapes for both branches."""

    def test_default_engine_log_includes_scenarios_and_schedule(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["cpu_spike", "memory_pressure"],
                schedule="per_sample",
            )
        )
        with caplog.at_level(logging.INFO, logger="osimflow.campaign"):
            CampaignChaosWiring(cfg)
        record = next(r for r in caplog.records if "chaos engine enabled" in r.getMessage())
        msg = record.getMessage()
        assert "cpu_spike" in msg
        assert "memory_pressure" in msg
        assert "per_sample" in msg

    def test_custom_engine_log_uses_custom_schedule(self, caplog: pytest.LogCaptureFixture) -> None:
        """Custom engine branch logs ``schedule="custom"`` when cfg.scenarios is empty."""
        # With cfg.chaos.scenarios empty, the wiring logs ``schedule=custom``
        # and enumerates the registered injector type names.
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=[],
            )
        )
        custom_engine = ChaosEngine()
        custom_engine.register(KillSwitchSimulator(fail_after=2))
        with caplog.at_level(logging.INFO, logger="osimflow.campaign"):
            CampaignChaosWiring(cfg, chaos_engine=custom_engine)
        record = next(r for r in caplog.records if "chaos engine enabled" in r.getMessage())
        msg = record.getMessage()
        assert "custom" in msg
        # The injector enumeration reads ``fault_type.value`` first
        # (e.g. ``kill_switch`` for KillSwitchSimulator); the log carries it.
        assert "kill_switch" in msg

    def test_custom_engine_log_falls_through_when_cfg_also_has_scenarios(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If cfg.scenarios is non-empty, cfg drives the log (not the custom engine)."""
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["cpu_spike"],
                schedule="per_sample",
            )
        )
        custom_engine = ChaosEngine()
        custom_engine.register(KillSwitchSimulator(fail_after=2))
        with caplog.at_level(logging.INFO, logger="osimflow.campaign"):
            CampaignChaosWiring(cfg, chaos_engine=custom_engine)
        record = next(r for r in caplog.records if "chaos engine enabled" in r.getMessage())
        msg = record.getMessage()
        # cfg wins because its scenarios list is non-empty — the schedule is
        # the cfg schedule, not "custom".
        assert "per_sample" in msg
        assert "custom" not in msg


# ---------------------------------------------------------------------------
# CampaignChaosWiring.maybe_inject — schedule gating
# ---------------------------------------------------------------------------


class TestCampaignChaosWiringMaybeInject:
    """``maybe_inject`` fires only on the configured schedule branch."""

    def test_no_engine_means_no_injection(self) -> None:
        cfg = _minimal_cfg(ChaosConfig(enabled=False))
        wiring = CampaignChaosWiring(cfg)
        trace = _trace()
        # All three schedules must be silent no-ops
        for when in ("before_step", "after_step", "per_sample"):
            wiring.maybe_inject("RUN_OPENSTUDIO_SIM", when, trace)
        assert trace.chaos_invocations == []

    def test_schedule_none_means_no_injection_on_any_when(self) -> None:
        """`schedule="none"` is a documented no-op even with engine registered."""
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["cpu_spike"],
                schedule="none",
                probability=1.0,
                duration_s=0.01,
            )
        )
        wiring = CampaignChaosWiring(cfg)
        assert wiring.engine is not None  # registered, just gated
        trace = _trace()
        for when in ("before_step", "after_step", "per_sample"):
            wiring.maybe_inject("RUN_OPENSTUDIO_SIM", when, trace)
        assert trace.chaos_invocations == []

    def test_unknown_when_string_is_silently_dropped(self) -> None:
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["cpu_spike"],
                schedule="before_step",
                probability=1.0,
                duration_s=0.01,
            )
        )
        wiring = CampaignChaosWiring(cfg)
        trace = _trace()
        wiring.maybe_inject("RUN_OPENSTUDIO_SIM", "not_a_real_when", trace)
        assert trace.chaos_invocations == []

    def test_per_sample_schedule_fires_only_on_per_sample(self) -> None:
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["cpu_spike"],
                schedule="per_sample",
                probability=1.0,
                duration_s=0.01,
            )
        )
        wiring = CampaignChaosWiring(cfg)
        trace = _trace()
        # before_step is gated, per_sample is not
        wiring.maybe_inject("RUN_OPENSTUDIO_SIM", "before_step", trace, "s001")
        wiring.maybe_inject("RUN_OPENSTUDIO_SIM", "per_sample", trace, "s001")
        # Only the per_sample injection landed
        assert len(trace.chaos_invocations) == 1
        inv = _as_invocation(trace.chaos_invocations[0])
        assert inv["when"] == "per_sample"
        assert inv["step"] == "RUN_OPENSTUDIO_SIM"
        assert inv["target_id"] == "s001"
        # The recorded fault_type is the cpu_spike value
        results = inv["results"]
        assert isinstance(results, list)
        result = _as_invocation(results[0])
        assert result["fault_type"] == FaultType.CPU_SPIKE.value
        assert result["injected"] is True

    def test_before_step_schedule_fires_only_on_before_step(self) -> None:
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["memory_pressure"],
                schedule="before_step",
                size_mb=1,
                duration_s=0.01,
                probability=1.0,
            )
        )
        wiring = CampaignChaosWiring(cfg)
        trace = _trace()
        wiring.maybe_inject("AGGREGATE_RESULTS", "after_step", trace)
        wiring.maybe_inject("AGGREGATE_RESULTS", "before_step", trace)
        invocations = [
            _as_invocation(inv)
            for inv in trace.chaos_invocations
            if _as_invocation(inv)["when"] == "before_step"
        ]
        assert len(invocations) == 1
        # memory_pressure fires immediately — injector is synchronous
        results = invocations[0]["results"]
        assert isinstance(results, list)
        assert _as_invocation(results[0])["injected"] is True

    def test_after_step_schedule_fires_only_on_after_step(self) -> None:
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["network_delay"],
                schedule="after_step",
                delay_s=0.0,
                jitter_s=0.0,
                probability=1.0,
            )
        )
        wiring = CampaignChaosWiring(cfg)
        trace = _trace()
        wiring.maybe_inject("AGGREGATE_RESULTS", "before_step", trace)
        wiring.maybe_inject("AGGREGATE_RESULTS", "after_step", trace)
        assert len(trace.chaos_invocations) == 1
        assert _as_invocation(trace.chaos_invocations[0])["when"] == "after_step"

    def test_default_target_id_falls_back_to_step_name(self) -> None:
        """Omitting ``target_id`` defaults to the step name."""
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["cpu_spike"],
                schedule="before_step",
                probability=1.0,
                duration_s=0.01,
            )
        )
        wiring = CampaignChaosWiring(cfg)
        trace = _trace()
        wiring.maybe_inject("GENERATE_LHS_SAMPLES", "before_step", trace)
        assert _as_invocation(trace.chaos_invocations[0])["target_id"] == "GENERATE_LHS_SAMPLES"

    def test_multiple_invocations_recorded_in_order(self) -> None:
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["cpu_spike"],
                schedule="per_sample",
                probability=1.0,
                duration_s=0.01,
            )
        )
        wiring = CampaignChaosWiring(cfg)
        trace = _trace()
        for sample_id in ("s001", "s002", "s003"):
            wiring.maybe_inject("RUN_OPENSTUDIO_SIM", "per_sample", trace, sample_id)
        assert len(trace.chaos_invocations) == 3
        assert [inv["target_id"] for inv in trace.chaos_invocations] == [
            "s001",
            "s002",
            "s003",
        ]


# ---------------------------------------------------------------------------
# CampaignChaosWiring.maybe_inject — engine failure containment
# ---------------------------------------------------------------------------


class TestCampaignChaosWiringFailureContainment:
    """Engine exceptions are swallowed; the campaign trace keeps running."""

    def test_engine_raising_does_not_propagate(self, caplog: pytest.LogCaptureFixture) -> None:
        class _ExplodingEngine(ChaosEngine):
            def inject(self, target_id: str) -> list[object]:  # type: ignore[override]
                raise RuntimeError("simulated chaos engine failure")

        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["cpu_spike"],
                schedule="per_sample",
            )
        )
        wiring = CampaignChaosWiring(cfg, chaos_engine=_ExplodingEngine())
        trace = _trace()
        with caplog.at_level(logging.WARNING, logger="osimflow.campaign"):
            wiring.maybe_inject("RUN_OPENSTUDIO_SIM", "per_sample", trace, "s001")
        # The campaign trace is unchanged
        assert trace.chaos_invocations == []
        # The warning was logged with the step name + target id
        warn_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("chaos inject failed" in m and "s001" in m for m in warn_msgs), warn_msgs


# ---------------------------------------------------------------------------
# CampaignChaosWiring — disabled engine still no-ops
# ---------------------------------------------------------------------------


class TestCampaignChaosWiringDisabledEngine:
    """The ``engine.enabled`` flag short-circuits ``maybe_inject``."""

    def test_disabled_engine_is_silent_no_op(self) -> None:
        cfg = _minimal_cfg(
            ChaosConfig(
                enabled=True,
                scenarios=["cpu_spike"],
                schedule="per_sample",
                probability=1.0,
                duration_s=0.01,
            )
        )
        # Build the default engine and disable it after the fact
        wiring = CampaignChaosWiring(cfg)
        assert wiring.engine is not None
        wiring.engine.enabled = False
        trace = _trace()
        wiring.maybe_inject("RUN_OPENSTUDIO_SIM", "per_sample", trace, "s001")
        assert trace.chaos_invocations == []
