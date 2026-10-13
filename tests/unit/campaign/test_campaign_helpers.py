"""Unit tests for small Campaign orchestrator helpers (issue #1926)."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.campaign import _osimflow_version
from osimflow.executors import LocalExecutor


@pytest.fixture
def campaign(workdir: Path, template_pkg: Path, outdir: Path) -> Campaign:
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
    )
    return Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))


def test_osimflow_version_unknown_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib import metadata

    def _raise(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", _raise)
    assert _osimflow_version() == "unknown"


def test_teardown_observability_contains_every_failure(campaign: Campaign) -> None:
    obs = MagicMock()
    obs.record_campaign_duration.side_effect = RuntimeError("a")
    obs.stop_periodic_flush.side_effect = RuntimeError("b")
    obs.flush.side_effect = RuntimeError("c")
    campaign._obs = obs
    campaign._teardown_observability(1.0)
    obs.flush.assert_called_once()


def test_maybe_alert_swallows_notify_errors(campaign: Campaign) -> None:
    manager = MagicMock()
    manager.notify.side_effect = RuntimeError("down")
    campaign._alert_manager = manager
    campaign._maybe_alert("campaign_failed", {})
    manager.notify.assert_called_once()


def test_registry_none_is_noop(campaign: Campaign) -> None:
    campaign._registry = None
    campaign._register_campaign()
    campaign._update_registry_status("completed")


def test_registry_errors_are_logged_not_raised(campaign: Campaign) -> None:
    registry = MagicMock()
    registry.update_status.side_effect = RuntimeError("db")
    registry.register.side_effect = RuntimeError("db")
    registry.register_campaign.side_effect = RuntimeError("db")
    campaign._registry = registry
    campaign._register_campaign()
    campaign._update_registry_status("completed")
    registry.update_status.assert_called_once()


def test_cost_tracker_none_is_noop(campaign: Campaign) -> None:
    campaign._cost_tracker = None
    campaign._record_costs("S", 1.0, 0.0)
    campaign._finalize_costs()


def test_record_costs_delegates_to_tracker(campaign: Campaign) -> None:
    tracker = MagicMock()
    campaign._cost_tracker = tracker
    campaign._record_costs("S", 1.0, 0.5)
    tracker.record_step_costs.assert_called_once_with("S", 1.0, 0.5)


def test_finalize_costs_writes_summary_to_trace(campaign: Campaign) -> None:
    tracker = MagicMock()
    tracker.finalize.return_value = {"total_cost_usd": 1.5}
    campaign._cost_tracker = tracker
    campaign._finalize_costs()
    assert campaign.trace.cost_summary == {"total_cost_usd": 1.5}


def test_pause_resume_aliases_delegate(campaign: Campaign, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(campaign, "pause", lambda: calls.append("pause"))
    monkeypatch.setattr(campaign, "resume", lambda: calls.append("resume"))
    campaign.request_pause()
    campaign.request_resume()
    assert calls == ["pause", "resume"]


def test_pause_resume_and_shutdown_trace_delegate_to_lifecycle(campaign: Campaign) -> None:
    lifecycle = MagicMock()
    campaign._lifecycle = lifecycle
    campaign.pause()
    campaign.resume()
    campaign._write_shutdown_trace()
    lifecycle.pause.assert_called_once()
    lifecycle.resume.assert_called_once()
    args: Any = lifecycle.write_shutdown_trace.call_args
    assert args.args[-1] == "cancelled"


def test_accumulate_cost_summary_delegates(campaign: Campaign) -> None:
    traces = MagicMock()
    campaign._sample_traces = traces
    campaign._accumulate_cost_summary()
    traces.accumulate_cost_summary.assert_called_once()
