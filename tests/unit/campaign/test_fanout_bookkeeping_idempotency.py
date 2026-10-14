"""Double invocation of fan-out success callbacks is idempotent (issue #2016)."""

from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.campaign.fanout import BookkeepingOnce
from osimflow.executors import LocalExecutor


class TestBookkeepingOnce:
    def test_runs_action_once(self) -> None:
        once = BookkeepingOnce()
        calls: list[int] = []
        once.run("a", lambda: calls.append(1))
        once.run("a", lambda: calls.append(2))
        assert calls == [1]

    def test_failed_action_is_retried(self) -> None:
        once = BookkeepingOnce()
        action = MagicMock(side_effect=[OSError("boom"), None])
        with pytest.raises(OSError):
            once.run("a", action)
        once.run("a", action)
        once.run("a", action)
        assert action.call_count == 2


def test_campaign_callbacks_idempotent_under_double_invocation(
    monkeypatch: pytest.MonkeyPatch, workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))

    items: Counter[tuple[str, str]] = Counter()
    statuses: Counter[str] = Counter()
    real_item_done = campaign.trace.step_item_done

    def _item_done(step: str, status: str = "ok") -> None:
        items[(step, status)] += 1
        real_item_done(step, status=status)

    def _record(sid: str, status: str, trace_id: str | None = None) -> None:
        statuses[sid] += 1

    monkeypatch.setattr(campaign.trace, "step_item_done", _item_done)
    monkeypatch.setattr(campaign._obs, "record_sample_status", _record)

    real_submit = campaign._submit_and_await_all

    def _double(submissions: dict[str, Any], step_name: str, *a: Any, **k: Any) -> None:
        def _twice(cb: Any) -> Any:
            def _run(result: Any) -> None:
                cb(result)
                cb(result)

            return _run

        doubled = {sid: (h, _twice(cb)) for sid, (h, cb) in submissions.items()}
        real_submit(doubled, step_name, *a, **k)

    monkeypatch.setattr(campaign, "_submit_and_await_all", _double)
    campaign.run()

    for step in ("APPLY_PARAMETERS", "RUN_OPENSTUDIO_SIM", "EXTRACT_KPIS"):
        assert items[(step, "ok")] == 2, step
    # One status record per sample per step (3 steps).
    assert set(statuses.values()) == {3}
