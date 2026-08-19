"""Regression test for issue #1009: resource_quota.max_concurrent_samples
must actually bound the ThreadPoolExecutor used by Campaign._submit_and_await_all.

Pre-fix, the ThreadPoolExecutor was constructed with `max_workers=self.max_workers`
ignoring `resource_quota.max_concurrent_samples` entirely. This test verifies the
fan-out site passes `_effective_max_workers()` (which honours the quota) rather
than the raw `self.max_workers`.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from osimflow import Campaign, CampaignConfig
from osimflow.config import ResourceQuota
from osimflow.executors import BaseExecutor, Handle


class _NoOpExecutor(BaseExecutor):
    """A no-op executor for testing — accepts any submit."""

    name = "noop-test-quota"

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
        h = Handle()
        return h

    def shutdown(self) -> None:
        pass

    def _resolve_handle(self, handle: Handle) -> Handle:
        return handle


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


def _make_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text("{}")
    var_file = tmp_path / "variables.yml"
    var_file.write_text("variables: []")
    out = tmp_path / "out"
    out.mkdir()
    return var_file, pkg, out


class TestThreadPoolReceivesBoundedValue:
    """The ThreadPoolExecutor constructed inside _submit_and_await_all must
    receive max_workers = Campaign._effective_max_workers() so that
    resource_quota.max_concurrent_samples actually caps fan-out (issue #1009)."""

    def test_quota_caps_helper(self, tmp_path: Path) -> None:
        var_file, pkg, out = _make_inputs(tmp_path)
        cfg = _cfg(
            var_file, pkg, out,
            resource_quota=ResourceQuota(max_concurrent_samples=3),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor(), max_workers=10)
        assert campaign._effective_max_workers() == 3

    def test_no_quota_passes_max_workers(self, tmp_path: Path) -> None:
        var_file, pkg, out = _make_inputs(tmp_path)
        cfg = _cfg(var_file, pkg, out, resource_quota=None)
        campaign = Campaign(cfg, executor=_NoOpExecutor(), max_workers=8)
        assert campaign._effective_max_workers() == 8

    def test_quota_above_max_workers_falls_back(self, tmp_path: Path) -> None:
        var_file, pkg, out = _make_inputs(tmp_path)
        cfg = _cfg(
            var_file, pkg, out,
            resource_quota=ResourceQuota(max_concurrent_samples=16),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor(), max_workers=8)
        assert campaign._effective_max_workers() == 8

    def test_fan_out_source_uses_bounded_helper(
        self, tmp_path: Path
    ) -> None:
        """The fan-out site at ~line 1063 must read `self._effective_max_workers()`,
        not `self.max_workers`. Inspect the source to guard against regression."""
        var_file, pkg, out = _make_inputs(tmp_path)
        cfg = _cfg(
            var_file, pkg, out,
            resource_quota=ResourceQuota(max_concurrent_samples=3),
        )
        campaign = Campaign(cfg, executor=_NoOpExecutor(), max_workers=10)
        source = inspect.getsource(campaign._submit_and_await_all)
        assert "self._effective_max_workers()" in source, (
            "Regression: _submit_and_await_all must pass "
            "max_workers=self._effective_max_workers() (issue #1009)"
        )
        assert "max_workers=self.max_workers" not in source, (
            "Regression: _submit_and_await_all still uses raw self.max_workers, "
            "ignoring resource_quota.max_concurrent_samples (issue #1009)"
        )
