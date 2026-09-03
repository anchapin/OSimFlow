"""Integration tests for resource-quota enforcement in ``Campaign.run()``.

Issue #1533: the quota guard extracted in #1462 was dead code in
production — ``run()`` never called ``_enforce_start_quota()`` and the
fan-out submission loops never called ``_check_quota_exceeded()``.
These tests assert the wiring end-to-end through the public
``Campaign.run()`` surface:

- ``run()`` fails fast with ``QuotaExceededError`` *before* the init
  hook runs when the configuration already violates ``max_samples``.
- A campaign whose ``max_cost_usd`` is exceeded mid-fan-out stops
  submitting further samples (per chunk boundary) instead of running
  to completion, and fires the documented ``quota.exceeded`` alert.

The cost scenario uses a ``LocalExecutor`` subclass that reports a
fixed ``cost_usd`` on every simulation handle and fans out one sample
per chunk, so the budget trips deterministically part-way through the
RUN_OPENSTUDIO_SIM fan-out.
"""

import json
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from osimflow import Campaign, CampaignConfig, QuotaExceededError
from osimflow.config import ResourceQuota
from osimflow.executors import Handle, LocalExecutor

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"

SIM_COST_USD = 5.0


class _RecordingAlertManager:
    """Minimal AlertManager stand-in: records notify() calls."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def notify(self, event_type: str, context: dict[str, Any]) -> None:
        self.events.append((event_type, context))


class CostReportingLocalExecutor(LocalExecutor):
    """LocalExecutor that reports per-sim costs and fans out one sample per chunk.

    - ``fanout_submit_chunk_size`` returns 1 so the campaign's per-chunk
      quota checks run between every sample submission.
    - Every ``sim_<sid>`` submission reports ``cost_usd=SIM_COST_USD``
      on its Handle, mirroring how cloud executors surface cost
      attribution (issue #105) that the quota guard accrues.
    - All submission names are recorded so the tests can assert exactly
      how far the fan-out got before the quota stopped it.
    """

    def __init__(self) -> None:
        super().__init__(max_workers=2)
        self.submitted_names: list[str] = []

    def fanout_submit_chunk_size(self, total: int) -> int:
        return 1

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        **kwargs: Any,
    ) -> Handle:
        handle = super().submit(fn, *args, name=name, **kwargs)
        self.submitted_names.append(name)
        if name.startswith("sim_"):
            handle.cost_usd = SIM_COST_USD
        return handle


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


def _cfg(workdir: Path, outdir: Path, **overrides: Any) -> CampaignConfig:
    defaults: dict[str, Any] = {
        "input_variables": workdir / "variables.yml",
        "template_sim_package": workdir / "template",
        "n_samples": 5,
        "outdir": outdir,
        "openstudio_version": "3.11.0",
        "skip_preflight": True,
        "archive_intermediates": False,
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)


class TestStartQuotaFailFast:
    def test_run_raises_quota_exceeded_before_init_hook(
        self,
        workdir: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """run() enforces the start quota before the init hook (issue #1533).

        The init script touches a marker file; if the quota check ran
        after the hook (or not at all) the marker would exist.
        """
        marker = workdir / "init_hook_ran"
        init_script = workdir / "init.sh"
        init_script.write_text(f"#!/bin/sh\ntouch {marker}\n")
        init_script.chmod(init_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        cfg = _cfg(
            workdir,
            outdir,
            n_samples=100,
            init_script=init_script,
            resource_quota=ResourceQuota(max_samples=50),
        )
        executor = CostReportingLocalExecutor()
        campaign = Campaign(cfg, executor=executor)

        with pytest.raises(QuotaExceededError) as exc_info:
            campaign.run()

        assert exc_info.value.quota_type == "max_samples"
        assert exc_info.value.limit == 50
        assert exc_info.value.current == 100
        # The init hook must NOT have run — the quota check precedes it.
        assert not marker.exists()
        # Nothing was submitted to the executor.
        assert executor.submitted_names == []

    def test_run_without_quota_violation_runs_init_hook(
        self,
        workdir: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """Sanity counterpart: no quota violation → init hook runs."""
        marker = workdir / "init_hook_ran"
        init_script = workdir / "init.sh"
        init_script.write_text(f"#!/bin/sh\ntouch {marker}\n")
        init_script.chmod(init_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        cfg = _cfg(
            workdir,
            outdir,
            n_samples=2,
            init_script=init_script,
            resource_quota=ResourceQuota(max_samples=50),
        )
        campaign = Campaign(cfg, executor=CostReportingLocalExecutor())
        campaign.run()

        assert marker.exists()


class TestCostQuotaStopsFanOut:
    def test_cost_quota_exceeded_mid_fanout_stops_submissions(
        self,
        workdir: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """A campaign whose budget trips mid-fan-out stops submitting.

        5 samples at $5/sim with ``max_cost_usd=12``: chunk boundaries
        check the accrued cost before each new submission, so the
        campaign submits s0 ($5), s1 ($10), s2 ($15) and then stops —
        the remaining samples are never submitted and the campaign
        finalises with partial results instead of running to completion.
        """
        cfg = _cfg(
            workdir,
            outdir,
            resource_quota=ResourceQuota(max_cost_usd=12.0),
        )
        executor = CostReportingLocalExecutor()
        campaign = Campaign(cfg, executor=executor)
        alerts = _RecordingAlertManager()
        campaign._alert_manager = alerts

        result = campaign.run()

        # APPLY has no cost attribution → all 5 samples parameterised.
        apply_submissions = [n for n in executor.submitted_names if n.startswith("apply_")]
        assert len(apply_submissions) == 5
        # RUN_OPENSTUDIO_SIM stopped after the budget tripped: 3 sims
        # accrued $15 ≥ $12, so the 4th/5th chunk checks stop submission.
        sim_submissions = [n for n in executor.submitted_names if n.startswith("sim_")]
        assert len(sim_submissions) == 3, (
            f"expected the cost quota to stop submissions at 3 sims, "
            f"got {len(sim_submissions)}: {executor.submitted_names}"
        )
        # EXTRACT also honours the (still-tripped) quota per chunk —
        # the cost total does not go down, so no KPI extraction submits.
        kpi_submissions = [n for n in executor.submitted_names if n.startswith("kpi_")]
        assert kpi_submissions == []

        # The quota.exceeded alert fired (exactly once).
        quota_alerts = [e for e in alerts.events if e[0] == "quota.exceeded"]
        assert len(quota_alerts) == 1
        _event, context = quota_alerts[0]
        assert context["quota_type"] == "max_cost_usd"
        assert context["limit"] == 12.0

        # The campaign finalised with partial results (did not crash
        # and did not run the full 5-sample completion).
        run_json = outdir / "run.json"
        assert run_json.is_file()
        trace = json.loads(run_json.read_text())
        assert trace["summary"]["n_samples"] == 5
        assert trace["summary"]["n_succeeded"] == 0, (
            "samples whose KPI extraction was skipped must not count as succeeded"
        )
        assert len(result["kpis"]) == 0
        # The budget is visible in the trace cost totals.
        assert trace["total_cost_usd"] == pytest.approx(15.0)

    def test_cost_quota_not_reached_runs_to_completion(
        self,
        workdir: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """Sanity counterpart: a generous budget does not stop the fan-out."""
        cfg = _cfg(
            workdir,
            outdir,
            resource_quota=ResourceQuota(max_cost_usd=1_000.0),
        )
        executor = CostReportingLocalExecutor()
        campaign = Campaign(cfg, executor=executor)
        alerts = _RecordingAlertManager()
        campaign._alert_manager = alerts

        campaign.run()

        sim_submissions = [n for n in executor.submitted_names if n.startswith("sim_")]
        kpi_submissions = [n for n in executor.submitted_names if n.startswith("kpi_")]
        assert len(sim_submissions) == 5
        assert len(kpi_submissions) == 5
        assert [e[0] for e in alerts.events if e[0] == "quota.exceeded"] == []
