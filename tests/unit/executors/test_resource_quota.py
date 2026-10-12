"""Unit tests for osimflow/config.py ResourceQuota and campaign quota enforcement (issue #446).

Covers:
- ResourceQuota dataclass: all fields, defaults, parsing
- _parse_resource_quota: JSON string → ResourceQuota | None
- _enforce_start_quota: fail-fast at campaign start
- _check_quota_exceeded: mid-campaign quota checks (including
  per-sample cost accrual and the once-per-campaign quota.exceeded
  alert — issue #1533)
- _effective_max_workers: max_concurrent_samples bounding
- QuotaExceededError: attributes and message

End-to-end wiring (run() start check + fan-out chunk checks) is
covered by tests/integration/test_quota_enforcement.py (issue #1533).
"""

from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest

from osimflow import Campaign, CampaignConfig, QuotaExceededError
from osimflow.config import ResourceQuota, _parse_resource_quota
from osimflow.executors import BaseExecutor, Handle

# ---------------------------------------------------------------------------
# ResourceQuota dataclass
# ---------------------------------------------------------------------------


class TestResourceQuotaDataclass:
    def test_all_none_by_default(self) -> None:
        rq = ResourceQuota()
        assert rq.max_samples is None
        assert rq.max_cost_usd is None
        assert rq.max_wall_time_min is None
        assert rq.max_concurrent_samples is None

    def test_all_fields_set(self) -> None:
        rq = ResourceQuota(
            max_samples=100,
            max_cost_usd=500.0,
            max_wall_time_min=120.0,
            max_concurrent_samples=10,
        )
        assert rq.max_samples == 100
        assert rq.max_cost_usd == 500.0
        assert rq.max_wall_time_min == 120.0
        assert rq.max_concurrent_samples == 10

    def test_partial_fields(self) -> None:
        rq = ResourceQuota(max_samples=50)
        assert rq.max_samples == 50
        assert rq.max_cost_usd is None
        assert rq.max_wall_time_min is None
        assert rq.max_concurrent_samples is None

    def test_repr(self) -> None:
        rq = ResourceQuota(max_samples=10)
        assert "max_samples=10" in repr(rq)


# ---------------------------------------------------------------------------
# _parse_resource_quota
# ---------------------------------------------------------------------------


class TestParseResourceQuota:
    def test_none_input(self) -> None:
        assert _parse_resource_quota(None) is None

    def test_empty_dict(self) -> None:
        rq = _parse_resource_quota({})
        assert rq is not None
        assert rq.max_samples is None
        assert rq.max_cost_usd is None
        assert rq.max_wall_time_min is None
        assert rq.max_concurrent_samples is None

    def test_full_dict(self) -> None:
        rq = _parse_resource_quota(
            {
                "max_samples": 100,
                "max_cost_usd": 250.5,
                "max_wall_time_min": 60,
                "max_concurrent_samples": 8,
            }
        )
        assert rq is not None
        assert rq.max_samples == 100
        assert rq.max_cost_usd == 250.5
        assert rq.max_wall_time_min == 60
        assert rq.max_concurrent_samples == 8

    def test_partial_dict(self) -> None:
        rq = _parse_resource_quota({"max_samples": 42})
        assert rq is not None
        assert rq.max_samples == 42
        assert rq.max_cost_usd is None
        assert rq.max_wall_time_min is None
        assert rq.max_concurrent_samples is None

    def test_json_string(self) -> None:
        rq = _parse_resource_quota('{"max_samples": 99}')
        assert rq is not None
        assert rq.max_samples == 99

    def test_int_coerced_from_float(self) -> None:
        rq = _parse_resource_quota({"max_wall_time_min": 90.0})
        assert rq is not None
        assert rq.max_wall_time_min == 90.0


# ---------------------------------------------------------------------------
# QuotaExceededError
# ---------------------------------------------------------------------------


class TestQuotaExceededError:
    def test_attributes(self) -> None:
        err = QuotaExceededError(
            "too many samples",
            quota_type="max_samples",
            limit=10,
            current=15,
        )
        assert err.quota_type == "max_samples"
        assert err.limit == 10
        assert err.current == 15
        assert "too many samples" in str(err)

    def test_inheritance(self) -> None:
        err = QuotaExceededError("boom", quota_type="max_cost_usd", limit=1.0, current=5.0)
        assert isinstance(err, RuntimeError)


# ---------------------------------------------------------------------------
# Campaign._enforce_start_quota
# ---------------------------------------------------------------------------


def _make_handle(result_value: Any) -> Handle:
    fut: Future[Any] = Future()
    fut.set_result(result_value)
    return Handle(job_id="test", _future=fut, worker_id="local", worker_ip="127.0.0.1")


class _NoOpExecutor(BaseExecutor):
    name = "noop"

    def submit(
        self,
        fn: Any,
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        return _make_handle(fn(*args))

    def shutdown(self) -> None:
        pass


def _cfg(
    variables_yml: Path,
    template_pkg: Path,
    outdir: Path,
    **overrides: Any,
) -> CampaignConfig:
    defaults: dict[str, Any] = {
        "input_variables": variables_yml,
        "template_sim_package": template_pkg,
        "n_samples": 3,
        "outdir": outdir,
        "openstudio_version": "3.11.0",
        "dry_run": True,
        "resource_quota": None,
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)


class TestEnforceStartQuota:
    def test_no_quota_passes(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            n_samples=5,
            resource_quota=None,
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        # Should not raise
        campaign._enforce_start_quota()

    def test_max_samples_exceeded_raises(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            n_samples=100,
            resource_quota=ResourceQuota(max_samples=50),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        with pytest.raises(QuotaExceededError) as exc_info:
            campaign._enforce_start_quota()
        assert exc_info.value.quota_type == "max_samples"
        assert exc_info.value.limit == 50
        assert exc_info.value.current == 100

    def test_max_samples_equal_passes(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            n_samples=50,
            resource_quota=ResourceQuota(max_samples=50),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        # Should not raise
        campaign._enforce_start_quota()


# ---------------------------------------------------------------------------
# Campaign._check_quota_exceeded
# ---------------------------------------------------------------------------


class TestCheckQuotaExceeded:
    def test_no_quota_returns_false(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(var_file, pkg, out, resource_quota=None)
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        assert campaign._check_quota_exceeded() is False

    def test_max_samples_not_reached_returns_false(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(var_file, pkg, out, resource_quota=ResourceQuota(max_samples=100))
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        # No samples submitted yet
        assert campaign._check_quota_exceeded() is False

    def test_max_samples_reached_returns_true(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            n_samples=5,
            resource_quota=ResourceQuota(max_samples=3),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        # Simulate 3 samples submitted by marking their states
        for i in range(3):
            campaign._sample_state[f"s{i}"] = {"apply_exit_code": 0}
        assert campaign._check_quota_exceeded() is True

    def test_max_cost_exceeded_returns_true(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            resource_quota=ResourceQuota(max_cost_usd=10.0),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        campaign.trace.total_cost_usd = 15.0
        assert campaign._check_quota_exceeded() is True

    def test_max_cost_accrued_from_sample_state_trips_mid_campaign(self, tmp_path: Path) -> None:
        """Mid-campaign cost check sums per-sample cost_usd (issue #1533).

        ``trace.total_cost_usd`` is only refreshed at finalize time, so
        the mid-campaign check must see the per-sample costs recorded by
        the RUN_OPENSTUDIO_SIM fan-out.
        """
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            resource_quota=ResourceQuota(max_cost_usd=12.0),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        # trace.total_cost_usd is still 0.0 (finalize not run yet) but
        # three sim handles already reported $5 each.
        campaign._sample_state["s0"] = {"sim_exit_code": 0, "cost_usd": 5.0}
        campaign._sample_state["s1"] = {"sim_exit_code": 0, "cost_usd": 5.0}
        campaign._sample_state["s2"] = {"sim_exit_code": 0, "cost_usd": 5.0}
        assert campaign._check_quota_exceeded() is True

    def test_max_cost_below_accrued_returns_false(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            resource_quota=ResourceQuota(max_cost_usd=12.0),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        campaign._sample_state["s0"] = {"sim_exit_code": 0, "cost_usd": 5.0}
        campaign._sample_state["s1"] = {"sim_exit_code": 0, "cost_usd": 5.0}
        assert campaign._check_quota_exceeded() is False

    def test_max_wall_time_exceeded_returns_true(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            resource_quota=ResourceQuota(max_wall_time_min=1.0),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        # Campaign started 2 minutes ago
        campaign.trace.started_at = campaign.trace.started_at - 120.0
        assert campaign._check_quota_exceeded() is True


# ---------------------------------------------------------------------------
# Campaign._check_quota_exceeded — quota.exceeded alert (issue #1533)
# ---------------------------------------------------------------------------


class _RecordingAlertManager:
    """Minimal AlertManager stand-in: records notify() calls."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def notify(self, event_type: str, context: dict[str, Any]) -> None:
        self.events.append((event_type, context))


class TestQuotaExceededAlert:
    def test_mid_campaign_trip_fires_quota_exceeded_alert(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            resource_quota=ResourceQuota(max_cost_usd=10.0),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        alerts = _RecordingAlertManager()
        campaign._alert_manager = alerts
        campaign.trace.total_cost_usd = 15.0

        assert campaign._check_quota_exceeded() is True
        assert len(alerts.events) == 1
        event_type, context = alerts.events[0]
        assert event_type == "quota.exceeded"
        assert context["quota_type"] == "max_cost_usd"
        assert context["limit"] == 10.0
        assert context["current"] == 15.0

    def test_alert_fires_once_per_campaign_across_repeated_checks(self, tmp_path: Path) -> None:
        """Chunk boundaries re-check the quota; the alert must not storm."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            resource_quota=ResourceQuota(max_samples=2),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        alerts = _RecordingAlertManager()
        campaign._alert_manager = alerts
        campaign._sample_state["s0"] = {"apply_exit_code": 0}
        campaign._sample_state["s1"] = {"apply_exit_code": 0}

        # Simulate the three fan-out loops each checking per chunk.
        for _ in range(3):
            assert campaign._check_quota_exceeded() is True
        assert len(alerts.events) == 1

    def test_no_alert_when_quota_not_tripped(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            resource_quota=ResourceQuota(max_cost_usd=100.0),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor())
        alerts = _RecordingAlertManager()
        campaign._alert_manager = alerts

        assert campaign._check_quota_exceeded() is False
        assert alerts.events == []


# ---------------------------------------------------------------------------
# Campaign._effective_max_workers
# ---------------------------------------------------------------------------


class TestEffectiveMaxWorkers:
    def test_no_quota_returns_configured(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(var_file, pkg, out, resource_quota=None)
        campaign = Campaign(cfg, executor=_NoOpExecutor(), max_workers=8)
        assert campaign._effective_max_workers() == 8

    def test_max_concurrent_samples_below_max_workers(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            resource_quota=ResourceQuota(max_concurrent_samples=4),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor(), max_workers=8)
        assert campaign._effective_max_workers() == 4

    def test_max_concurrent_samples_above_max_workers(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        var_file = tmp_path / "variables.yml"
        var_file.write_text("variables: []")
        out = tmp_path / "out"
        out.mkdir()

        cfg = _cfg(
            var_file,
            pkg,
            out,
            resource_quota=ResourceQuota(max_concurrent_samples=16),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor(), max_workers=8)
        assert campaign._effective_max_workers() == 8
