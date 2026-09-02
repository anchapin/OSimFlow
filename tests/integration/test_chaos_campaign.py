"""End-to-end integration test: chaos wiring through a Campaign (issue #1013).

Acceptance criteria (issue #1013):

* ``Campaign`` invokes ``ChaosEngine`` at the configured schedule
  (``before_step``, ``after_step``, ``per_sample``).  At present this
  test covers ``per_sample`` and ``before_step``; ``after_step`` is
  tracked in issue #1336.
* When the engine fires, every invocation is recorded in
  ``run.json`` under the ``chaos_invocations`` key.
* A non-chaos campaign (``chaos_enabled=False``) is byte-for-byte
  indistinguishable from the pre-wiring behaviour — no extra engine
  hooks, no extra ``chaos_invocations`` entries beyond an empty list.
* The kill switch / network delay / CPU spike scenarios never crash
  the campaign; the orchestrator process keeps running so the rest
  of the fan-out completes.

This test is gated by ``@pytest.mark.chaos`` so the default fast CI
run (``make test-fast``) skips it.  To exercise the chaos wiring
locally::

    .venv/bin/pytest tests/integration/test_chaos_campaign.py -m chaos -v
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from osimflow import (
    Campaign,
    CampaignConfig,
    ChaosEngine,
    CPUSpikeInjector,
    KillSwitchInjector,
    MemoryPressureInjector,
    NetworkDelayInjector,
)
from osimflow.chaos import FaultType
from osimflow.config import ChaosConfig
from osimflow.executors import LocalExecutor

pytestmark = pytest.mark.chaos

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: wwr\n"
        "    distribution: uniform\n"
        "    min: 0.2\n"
        "    max: 0.6\n"
        "    measure_argument: SetEnvelopePerformance.wwr\n"
    )
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


def _make_cfg(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    *,
    chaos_enabled: bool = False,
    chaos_scenarios: list[str] | None = None,
    chaos_schedule: str = "none",
    chaos_delay_s: float = 0.1,
    chaos_jitter_s: float = 0.05,
    chaos_probability: float = 1.0,
    chaos_fail_after: int = 2,
) -> CampaignConfig:
    # Issue #1474: build the single ChaosConfig object instead of
    # passing flat ``chaos_*`` kwargs (which were removed from
    # CampaignConfig).
    scenarios = list(chaos_scenarios) if chaos_scenarios else []
    if chaos_enabled and not scenarios:
        scenarios = ["kill_switch"]
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        skip_preflight=True,
        chaos=ChaosConfig(
            enabled=chaos_enabled,
            scenarios=scenarios,
            schedule=chaos_schedule,
            delay_s=chaos_delay_s,
            jitter_s=chaos_jitter_s,
            probability=chaos_probability,
            fail_after=chaos_fail_after,
        ),
    )


def test_chaos_disabled_produces_empty_invocations(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """No chaos flags → run.json.chaos_invocations == [].

    Regression guard: the wiring must be invisible for non-chaos
    campaigns. The chaos_invocations key is always present (per the
    issue's monitoring-schema decision) but the list is empty.
    """
    cfg = _make_cfg(workdir, template_pkg, outdir)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    assert campaign._chaos_engine is None

    campaign.run()

    trace = json.loads((outdir / "run.json").read_text())
    assert "chaos_invocations" in trace
    assert trace["chaos_invocations"] == []
    assert trace["config"]["chaos"] == {
        "enabled": False,
        "scenarios": [],
        "schedule": "none",
    }


def test_chaos_per_sample_kill_switch_records_invocations(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``per_sample`` schedule with ``kill_switch`` records per-sample entries.

    Mirrors the acceptance criterion from issue #1013: a 3-sample
    campaign with ``ChaosEngine`` registered and ``KillSwitchInjector``
    on the per-sample schedule runs to completion, every sample
    fires a chaos invocation, and ``run.json`` carries the proof.
    """
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["kill_switch"],
        chaos_schedule="per_sample",
        chaos_fail_after=1,  # kill switch fires on first inject
    )

    # Build the engine the same way Campaign would, then pass it in.
    engine = ChaosEngine(enabled=True)
    engine.register(KillSwitchInjector(fail_after=cfg.chaos.fail_after))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()

    trace = json.loads((outdir / "run.json").read_text())
    assert trace["config"]["chaos"]["enabled"] is True
    assert trace["config"]["chaos"]["schedule"] == "per_sample"
    assert "kill_switch" in trace["config"]["chaos"]["scenarios"]

    # Every sample should have produced a per-sample chaos invocation
    # for both the sim and the extract-kpi fan-outs.
    per_sample_invocations = [
        inv for inv in trace["chaos_invocations"] if inv["when"] == "per_sample"
    ]
    assert per_sample_invocations, "expected at least one per_sample chaos invocation"
    # We registered one injector — every invocation list should hold
    # exactly one ChaosResult dict.
    for inv in per_sample_invocations:
        assert inv["step"] in {"RUN_OPENSTUDIO_SIM", "EXTRACT_KPIS"}
        assert len(inv["results"]) == 1
        assert inv["results"][0]["fault_type"] == "kill_switch"


def test_chaos_before_step_network_delay_completes(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``before_step`` schedule with ``network_delay`` does not break the campaign.

    NetworkDelayInjector spawns a short daemon thread; the campaign
    must still complete and emit the per-step chaos invocations. We
    keep the delay tiny so the test stays under the per-test budget.
    """
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["network_delay"],
        chaos_schedule="before_step",
        chaos_delay_s=0.01,
        chaos_jitter_s=0.0,
        chaos_probability=1.0,
    )

    engine = ChaosEngine(enabled=True)
    engine.register(NetworkDelayInjector(delay_s=cfg.chaos.delay_s, jitter_s=0.0, probability=1.0))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()

    trace = json.loads((outdir / "run.json").read_text())
    assert trace["config"]["chaos"]["schedule"] == "before_step"
    invocations = [inv for inv in trace["chaos_invocations"] if inv["when"] == "before_step"]
    assert invocations, "expected at least one before_step chaos invocation"
    # The network_delay injector is probability-aware; with probability=1.0
    # every call should report injected=True.
    for inv in invocations:
        assert inv["results"][0]["fault_type"] == FaultType.NETWORK_DELAY.value
        assert inv["results"][0]["injected"] is True


def test_chaos_engine_failure_does_not_crash_campaign(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """A buggy custom ``chaos_engine`` cannot abort the campaign.

    The wiring wraps every engine call in try/except, so even if the
    engine raises (or the injector does), the campaign finishes and
    the failure is captured in the trace.
    """

    class _ExplodingEngine(ChaosEngine):
        def inject(self, target_id: str) -> list[object]:
            raise RuntimeError("simulated chaos engine failure")

    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["kill_switch"],
        chaos_schedule="per_sample",
    )

    campaign = Campaign(
        cfg=cfg,
        executor=LocalExecutor(max_workers=3),
        chaos_engine=_ExplodingEngine(),
    )

    campaign.run()  # must not raise

    trace = json.loads((outdir / "run.json").read_text())
    # No invocations recorded because the engine exploded every time;
    # the campaign still finalised cleanly.
    assert trace["status"] == "success"
    assert trace["chaos_invocations"] == []


def test_chaos_per_sample_cpu_spike_records_invocations(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``per_sample`` schedule with ``cpu_spike`` records per-sample entries.

    CPUSpikeInjector spawns CPU-burning threads; the campaign must still
    complete and emit per-sample chaos invocations with the correct fault_type.
    Duration is kept short so the test stays within budget.
    """
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["cpu_spike"],
        chaos_schedule="per_sample",
        chaos_delay_s=0.0,
        chaos_jitter_s=0.0,
        chaos_probability=1.0,
    )

    engine = ChaosEngine(enabled=True)
    engine.register(CPUSpikeInjector(duration_s=0.05, intensity=0.1, probability=1.0))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()

    trace = json.loads((outdir / "run.json").read_text())
    assert trace["config"]["chaos"]["enabled"] is True
    assert trace["config"]["chaos"]["schedule"] == "per_sample"
    assert "cpu_spike" in trace["config"]["chaos"]["scenarios"]

    per_sample_invocations = [
        inv for inv in trace["chaos_invocations"] if inv["when"] == "per_sample"
    ]
    assert per_sample_invocations, "expected at least one per_sample chaos invocation"
    for inv in per_sample_invocations:
        assert inv["step"] in {"RUN_OPENSTUDIO_SIM", "EXTRACT_KPIS"}
        assert len(inv["results"]) == 1
        assert inv["results"][0]["fault_type"] == FaultType.CPU_SPIKE.value


def test_chaos_before_step_memory_pressure_completes(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``before_step`` schedule with ``memory_pressure`` does not break the campaign.

    MemoryPressureInjector allocates memory temporarily; the campaign must still
    complete and emit the per-step chaos invocations.  The allocation is small
    (50 MB) so the test stays under the per-test budget.
    """
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["memory_pressure"],
        chaos_schedule="before_step",
        chaos_delay_s=0.0,
        chaos_jitter_s=0.0,
        chaos_probability=1.0,
    )

    engine = ChaosEngine(enabled=True)
    engine.register(MemoryPressureInjector(size_mb=50, duration_s=0.1, probability=1.0))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()

    trace = json.loads((outdir / "run.json").read_text())
    assert trace["config"]["chaos"]["schedule"] == "before_step"
    invocations = [inv for inv in trace["chaos_invocations"] if inv["when"] == "before_step"]
    assert invocations, "expected at least one before_step chaos invocation"
    for inv in invocations:
        assert inv["results"][0]["fault_type"] == FaultType.MEMORY_PRESSURE.value
        assert inv["results"][0]["injected"] is True


def test_chaos_after_step_memory_pressure_completes(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``after_step`` schedule with ``memory_pressure`` does not break the campaign.

    This is the missing third-schedule regression test for issue #1322.
    MemoryPressureInjector is registered on the ``after_step`` schedule;
    the campaign must still complete and emit per-step chaos invocations
    with ``when == "after_step"``.
    """
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["memory_pressure"],
        chaos_schedule="after_step",
        chaos_delay_s=0.0,
        chaos_jitter_s=0.0,
        chaos_probability=1.0,
    )

    engine = ChaosEngine(enabled=True)
    engine.register(MemoryPressureInjector(size_mb=50, duration_s=0.1, probability=1.0))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()

    trace = json.loads((outdir / "run.json").read_text())
    assert trace["config"]["chaos"]["schedule"] == "after_step"
    invocations = [inv for inv in trace["chaos_invocations"] if inv["when"] == "after_step"]
    assert invocations, "expected at least one after_step chaos invocation"
    for inv in invocations:
        assert inv["results"][0]["fault_type"] == FaultType.MEMORY_PRESSURE.value
        assert inv["results"][0]["injected"] is True


# ---------------------------------------------------------------------------
# Full scenario × schedule matrix (issue #1390)
#
# The tests above grew organically and covered 5 of the 12 valid
# scenario × schedule combinations. The seven tests below complete the
# matrix so a regression in any ``_maybe_inject_chaos`` branch fails
# the suite instead of silently rotting. Each test asserts the same
# four invariants: (a) the campaign completes without raising, (b) the
# ``chaos_invocations`` entry carries the expected ``when``, (c) the
# injected ``fault_type`` matches the registered injector, and (d)
# ``run.json.config.chaos`` records the correct scenario and schedule.
# ---------------------------------------------------------------------------


def _assert_matrix_invariants(
    outdir: Path,
    *,
    scenario: str,
    schedule: str,
    expected_fault_type: str,
    step_set: set[str] | None = None,
) -> None:
    """Shared invariants for one scenario × schedule combo (issue #1390)."""
    trace = json.loads((outdir / "run.json").read_text())
    assert trace["status"] == "success"
    assert trace["config"]["chaos"]["enabled"] is True
    assert trace["config"]["chaos"]["schedule"] == schedule
    assert scenario in trace["config"]["chaos"]["scenarios"]

    invocations = [inv for inv in trace["chaos_invocations"] if inv["when"] == schedule]
    assert invocations, f"expected at least one {schedule} chaos invocation"
    for inv in invocations:
        if step_set is not None:
            assert inv["step"] in step_set
        assert len(inv["results"]) == 1
        assert inv["results"][0]["fault_type"] == expected_fault_type


def test_chaos_before_step_kill_switch_completes(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``before_step`` schedule with ``kill_switch`` completes (matrix, #1390)."""
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["kill_switch"],
        chaos_schedule="before_step",
        chaos_fail_after=1,
    )

    engine = ChaosEngine(enabled=True)
    engine.register(KillSwitchInjector(fail_after=cfg.chaos.fail_after))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()  # must not raise

    _assert_matrix_invariants(
        outdir,
        scenario="kill_switch",
        schedule="before_step",
        expected_fault_type=FaultType.KILL_SWITCH.value,
    )


def test_chaos_after_step_kill_switch_completes(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``after_step`` schedule with ``kill_switch`` completes (matrix, #1390)."""
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["kill_switch"],
        chaos_schedule="after_step",
        chaos_fail_after=1,
    )

    engine = ChaosEngine(enabled=True)
    engine.register(KillSwitchInjector(fail_after=cfg.chaos.fail_after))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()  # must not raise

    _assert_matrix_invariants(
        outdir,
        scenario="kill_switch",
        schedule="after_step",
        expected_fault_type=FaultType.KILL_SWITCH.value,
    )


def test_chaos_per_sample_network_delay_completes(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``per_sample`` schedule with ``network_delay`` completes (matrix, #1390)."""
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["network_delay"],
        chaos_schedule="per_sample",
        chaos_delay_s=0.01,
        chaos_jitter_s=0.0,
        chaos_probability=1.0,
    )

    engine = ChaosEngine(enabled=True)
    engine.register(NetworkDelayInjector(delay_s=cfg.chaos.delay_s, jitter_s=0.0, probability=1.0))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()  # must not raise

    _assert_matrix_invariants(
        outdir,
        scenario="network_delay",
        schedule="per_sample",
        expected_fault_type=FaultType.NETWORK_DELAY.value,
        step_set={"RUN_OPENSTUDIO_SIM", "EXTRACT_KPIS"},
    )


def test_chaos_after_step_network_delay_completes(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``after_step`` schedule with ``network_delay`` completes (matrix, #1390)."""
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["network_delay"],
        chaos_schedule="after_step",
        chaos_delay_s=0.01,
        chaos_jitter_s=0.0,
        chaos_probability=1.0,
    )

    engine = ChaosEngine(enabled=True)
    engine.register(NetworkDelayInjector(delay_s=cfg.chaos.delay_s, jitter_s=0.0, probability=1.0))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()  # must not raise

    _assert_matrix_invariants(
        outdir,
        scenario="network_delay",
        schedule="after_step",
        expected_fault_type=FaultType.NETWORK_DELAY.value,
    )


def test_chaos_before_step_cpu_spike_completes(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``before_step`` schedule with ``cpu_spike`` completes (matrix, #1390)."""
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["cpu_spike"],
        chaos_schedule="before_step",
        chaos_delay_s=0.0,
        chaos_jitter_s=0.0,
        chaos_probability=1.0,
    )

    engine = ChaosEngine(enabled=True)
    engine.register(CPUSpikeInjector(duration_s=0.05, intensity=0.1, probability=1.0))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()  # must not raise

    _assert_matrix_invariants(
        outdir,
        scenario="cpu_spike",
        schedule="before_step",
        expected_fault_type=FaultType.CPU_SPIKE.value,
    )


def test_chaos_after_step_cpu_spike_completes(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``after_step`` schedule with ``cpu_spike`` completes (matrix, #1390)."""
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["cpu_spike"],
        chaos_schedule="after_step",
        chaos_delay_s=0.0,
        chaos_jitter_s=0.0,
        chaos_probability=1.0,
    )

    engine = ChaosEngine(enabled=True)
    engine.register(CPUSpikeInjector(duration_s=0.05, intensity=0.1, probability=1.0))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()  # must not raise

    _assert_matrix_invariants(
        outdir,
        scenario="cpu_spike",
        schedule="after_step",
        expected_fault_type=FaultType.CPU_SPIKE.value,
    )


def test_chaos_per_sample_memory_pressure_completes(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """``per_sample`` schedule with ``memory_pressure`` completes (matrix, #1390)."""
    cfg = _make_cfg(
        workdir,
        template_pkg,
        outdir,
        chaos_enabled=True,
        chaos_scenarios=["memory_pressure"],
        chaos_schedule="per_sample",
        chaos_delay_s=0.0,
        chaos_jitter_s=0.0,
        chaos_probability=1.0,
    )

    engine = ChaosEngine(enabled=True)
    engine.register(MemoryPressureInjector(size_mb=50, duration_s=0.1, probability=1.0))
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3), chaos_engine=engine)

    campaign.run()  # must not raise

    _assert_matrix_invariants(
        outdir,
        scenario="memory_pressure",
        schedule="per_sample",
        expected_fault_type=FaultType.MEMORY_PRESSURE.value,
        step_set={"RUN_OPENSTUDIO_SIM", "EXTRACT_KPIS"},
    )
