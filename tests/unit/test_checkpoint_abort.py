"""3-strike checkpoint-failure abort (issues #739 / #1539).

``CampaignSampleTraceRecorder.checkpoint_sample`` aborts the campaign
after 3 consecutive ``run.json`` checkpoint failures. Issue #1539 adds:

- a dedicated ``CampaignAbortError`` raised at the 3rd consecutive
  failure (chained to the underlying write error),
- propagation of that abort through the concurrent fan-out path
  (``Campaign._submit_and_await_all`` with ``max_workers > 1``), where
  it was previously swallowed by the
  ``contextlib.suppress(Exception, CancelledError): future.result()``
  guard around the ``as_completed`` loop.

Scenarios (from the issue's acceptance criteria):
(a) abort fires exactly at 3 consecutive failures — and not at 2,
(b) the failure counter resets to 0 after an intervening success,
(c) the abort propagates out of ``Campaign._checkpoint_sample`` while
    run.json retains the last successful checkpoint,
(d) a concurrent-mode campaign (``max_workers > 1``) with an
    always-failing checkpoint aborts (non-success run.json status,
    error surfaced) instead of completing "successfully".
"""

import json
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow._campaign_sample_trace import (
    CampaignAbortError,
    CampaignSampleTraceRecorder,
)
from osimflow.executors import Handle, LocalExecutor
from osimflow.monitoring import RunTrace

# campaign_workdir / template_pkg / outdir fixtures come from conftest.py.


def _make_recorder(
    run_json: Path,
) -> tuple[CampaignSampleTraceRecorder, RunTrace, dict[str, dict[str, object]]]:
    """Build a recorder whose RunTrace checkpoints to *run_json*."""
    trace = RunTrace(campaign_id="test-checkpoint-abort", config_summary={})
    trace.write(run_json)
    sample_state: dict[str, dict[str, object]] = {
        f"s{i:04d}": {"apply_exit_code": 0, "sim_exit_code": 0, "extract_exit_code": 0}
        for i in range(5)
    }
    recorder = CampaignSampleTraceRecorder(trace=trace, sample_state=sample_state, obs=MagicMock())
    return recorder, trace, sample_state


def _boom(_trace: Any) -> None:
    raise OSError("simulated run.json write failure")


def _boom_method(_self: Any, _trace: Any) -> None:
    """Class-level update_sample patch (receives the bound instance)."""
    raise OSError("simulated run.json write failure")


def _seed_sample_state(campaign: Campaign, sids: list[str]) -> None:
    """Seed per-sample state so checkpoint_sample builds real rows."""
    for sid in sids:
        campaign._sample_state[sid] = {
            "apply_exit_code": 0,
            "sim_exit_code": 0,
            "extract_exit_code": 0,
        }


class TestThreeStrikeAbort:
    """Scenario (a): abort exactly at 3 consecutive failures, not at 2."""

    def test_no_abort_at_two_consecutive_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder, trace, _ = _make_recorder(tmp_path / "run.json")
        monkeypatch.setattr(trace, "update_sample", _boom)

        recorder.checkpoint_sample("s0000")
        recorder.checkpoint_sample("s0001")

        assert recorder.consecutive_checkpoint_failures == 2

    def test_aborts_at_third_consecutive_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder, trace, _ = _make_recorder(tmp_path / "run.json")
        monkeypatch.setattr(trace, "update_sample", _boom)

        recorder.checkpoint_sample("s0000")
        recorder.checkpoint_sample("s0001")
        with pytest.raises(CampaignAbortError, match="3 consecutive checkpoint failures"):
            recorder.checkpoint_sample("s0002")

        assert recorder.consecutive_checkpoint_failures == 3

    def test_abort_chains_underlying_write_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder, trace, _ = _make_recorder(tmp_path / "run.json")
        monkeypatch.setattr(trace, "update_sample", _boom)

        recorder.checkpoint_sample("s0000")
        recorder.checkpoint_sample("s0001")
        with pytest.raises(CampaignAbortError) as excinfo:
            recorder.checkpoint_sample("s0002")
        assert isinstance(excinfo.value.__cause__, OSError)

    def test_abort_error_is_osimflow_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.errors import OSimFlowError

        recorder, trace, _ = _make_recorder(tmp_path / "run.json")
        monkeypatch.setattr(trace, "update_sample", _boom)
        with pytest.raises(OSimFlowError):
            for i in range(5):
                recorder.checkpoint_sample(f"s{i:04d}")


class TestCounterReset:
    """Scenario (b): counter resets to 0 after an intervening success."""

    def test_success_resets_consecutive_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder, trace, _ = _make_recorder(tmp_path / "run.json")

        # Two failures, then a success (real update_sample), then two
        # more failures — still below the threshold, no abort.
        monkeypatch.setattr(trace, "update_sample", _boom)
        recorder.checkpoint_sample("s0000")
        recorder.checkpoint_sample("s0001")
        assert recorder.consecutive_checkpoint_failures == 2

        monkeypatch.undo()
        recorder.checkpoint_sample("s0002")
        assert recorder.consecutive_checkpoint_failures == 0

        monkeypatch.setattr(trace, "update_sample", _boom)
        recorder.checkpoint_sample("s0003")
        recorder.checkpoint_sample("s0004")
        assert recorder.consecutive_checkpoint_failures == 2

    def test_abort_only_after_three_consecutive_post_reset_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder, trace, _ = _make_recorder(tmp_path / "run.json")

        monkeypatch.setattr(trace, "update_sample", _boom)
        recorder.checkpoint_sample("s0000")
        recorder.checkpoint_sample("s0001")

        monkeypatch.undo()  # next checkpoint succeeds -> counter resets
        recorder.checkpoint_sample("s0002")

        monkeypatch.setattr(trace, "update_sample", _boom)
        recorder.checkpoint_sample("s0003")
        recorder.checkpoint_sample("s0004")
        # Third consecutive failure *after the reset* aborts.
        with pytest.raises(CampaignAbortError):
            recorder.checkpoint_sample("s0000")


class TestCampaignCheckpointAbort:
    """Scenario (c): abort propagates out of Campaign._checkpoint_sample
    with run.json still containing the last successful checkpoint."""

    def test_abort_preserves_last_successful_checkpoint(
        self,
        campaign_workdir: Path,
        template_pkg: Path,
        outdir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = CampaignConfig(
            input_variables=campaign_workdir / "variables.yml",
            template_sim_package=template_pkg,
            n_samples=3,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())
        run_json = outdir / "run.json"
        campaign.trace.write(run_json)
        _seed_sample_state(campaign, [f"s{i:04d}" for i in range(4)])

        # First checkpoint succeeds and lands in run.json.
        campaign._checkpoint_sample("s0000")
        data = json.loads(run_json.read_text())
        sample_ids = [s["sample_id"] for s in data["per_sample"]]
        assert "s0000" in sample_ids

        # Now the monitoring plane starts failing.
        monkeypatch.setattr(campaign.trace, "update_sample", _boom)
        campaign._checkpoint_sample("s0001")
        campaign._checkpoint_sample("s0002")
        with pytest.raises(CampaignAbortError):
            campaign._checkpoint_sample("s0003")

        # run.json still contains the last successful checkpoint and
        # none of the failed ones (atomic tmp+rename writes).
        data = json.loads(run_json.read_text())
        sample_ids = [s["sample_id"] for s in data["per_sample"]]
        assert sample_ids == ["s0000"]


def _make_handle(result_value: Any) -> Handle:
    fut: Future[Any] = Future()
    fut.set_result(result_value)
    return Handle(job_id="test-job", _future=fut, worker_id="local", worker_ip="testhost")


class TestConcurrentFanoutPropagation:
    """The core #1539 regression: the abort raised inside an
    ``_await_one`` worker thread must cross the thread boundary."""

    @staticmethod
    def _submissions(n: int) -> dict[str, tuple[Handle, Any]]:
        def _on_success(_result: Any) -> None:
            return None

        return {f"s{i:04d}": (_make_handle(f"out/{i}"), _on_success) for i in range(n)}

    def test_abort_re_raised_from_as_completed_loop(
        self,
        campaign_workdir: Path,
        template_pkg: Path,
        outdir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = CampaignConfig(
            input_variables=campaign_workdir / "variables.yml",
            template_sim_package=template_pkg,
            n_samples=3,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(), max_workers=2)
        assert campaign.max_workers > 1  # guard: concurrent branch active

        calls: list[str] = []
        real_checkpoint = campaign._checkpoint_sample

        def _failing_checkpoint(sid: str) -> None:
            calls.append(sid)
            if len(calls) >= 3:
                raise CampaignAbortError("simulated 3-strike abort")
            real_checkpoint(sid)

        monkeypatch.setattr(campaign, "_checkpoint_sample", _failing_checkpoint)

        with pytest.raises(CampaignAbortError, match="simulated 3-strike abort"):
            campaign._submit_and_await_all(self._submissions(3), "APPLY_PARAMETERS")

    def test_per_sample_errors_still_swallowed_in_concurrent_mode(
        self,
        campaign_workdir: Path,
        template_pkg: Path,
        outdir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ordinary per-sample failures must NOT abort the fan-out —
        only CampaignAbortError crosses the boundary."""
        cfg = CampaignConfig(
            input_variables=campaign_workdir / "variables.yml",
            template_sim_package=template_pkg,
            n_samples=3,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(), max_workers=2)
        monkeypatch.setattr(campaign, "_checkpoint_sample", lambda sid: None)

        def _on_success(_result: Any) -> None:
            raise RuntimeError("per-sample failure")

        submissions = {f"s{i:04d}": (_make_handle(f"out/{i}"), _on_success) for i in range(3)}
        # Must not raise: per-sample errors are logged/recorded inside
        # _await_one, never propagated (pre-existing contract).
        campaign._submit_and_await_all(submissions, "APPLY_PARAMETERS")

    def test_sequential_mode_still_aborts(
        self,
        campaign_workdir: Path,
        template_pkg: Path,
        outdir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """max_workers == 1 keeps the pre-#1539 behaviour: the abort
        propagates directly out of the sequential _await_one loop."""
        cfg = CampaignConfig(
            input_variables=campaign_workdir / "variables.yml",
            template_sim_package=template_pkg,
            n_samples=3,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(), max_workers=1)
        assert campaign.max_workers <= 1
        campaign.trace.write(outdir / "run.json")
        _seed_sample_state(campaign, [f"s{i:04d}" for i in range(3)])
        monkeypatch.setattr(campaign.trace, "update_sample", _boom)

        # Three samples whose checkpoints all fail -> the 3rd raises
        # CampaignAbortError out of the sequential loop.
        with pytest.raises(CampaignAbortError):
            campaign._submit_and_await_all(self._submissions(3), "APPLY_PARAMETERS")


class TestConcurrentModeCampaignAbort:
    """Scenario (d): full campaign with max_workers > 1 and an
    always-failing checkpoint aborts instead of completing."""

    def test_campaign_aborts_with_failure_status(
        self,
        campaign_workdir: Path,
        template_pkg: Path,
        outdir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = CampaignConfig(
            input_variables=campaign_workdir / "variables.yml",
            template_sim_package=template_pkg,
            n_samples=3,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(), max_workers=2)
        # Every incremental checkpoint write fails — the monitoring
        # plane cannot persist run.json (the #739 scenario).
        monkeypatch.setattr(RunTrace, "update_sample", _boom_method)

        with pytest.raises(CampaignAbortError):
            campaign.run()

        run_json = outdir / "run.json"
        assert run_json.exists()
        data = json.loads(run_json.read_text())
        assert data["status"] != "success"
        assert data["status"] == "failure"
        assert "CampaignAbortError" in (data.get("error_summary") or "")
