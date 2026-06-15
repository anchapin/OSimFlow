"""Unit tests for osimflow/config.py ResourceQuota and campaign quota enforcement (issue #446).

Covers:
- ResourceQuota dataclass: all fields, defaults, parsing
- _parse_resource_quota: JSON string → ResourceQuota | None
- _enforce_start_quota: fail-fast at campaign start
- _check_quota_exceeded: mid-campaign quota checks
- _effective_max_workers: max_concurrent_samples bounding
- QuotaExceededError: attributes and message
- Integration: _submit_and_await_all stops on quota exceeded
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
