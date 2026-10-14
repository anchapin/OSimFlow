"""Double invocation of fan-out success callbacks is idempotent (issue #2016)."""

from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.algorithms import AlgorithmRegistry
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


def test_apply_parameters_bookkeeping_failure_avoids_partial_ok_state(
    monkeypatch: pytest.MonkeyPatch, workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Issue #2023: if archiving fails all 3 attempts in APPLY_PARAMETERS, the
    sample is omitted from ``out``, ``apply_status`` is ``'failed'`` (never
    ``'ok'``), and ``trace.step_item_done`` only records ``status='failed'``.
    """
    monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    samples = campaign.step_generate_samples(AlgorithmRegistry.get("lhs"))
    s0, s1 = [str(s["sample_id"]) for s in samples]

    item_events: list[tuple[str, str]] = []
    real_item_done = campaign.trace.step_item_done

    def _item_done(step: str, status: str = "ok") -> None:
        item_events.append((step, status))
        real_item_done(step, status=status)

    monkeypatch.setattr(campaign.trace, "step_item_done", _item_done)

    real_archive = campaign._archive_sample_artifacts
    archive_attempts = 0

    def _failing_archive(src_dir: Path, dst_dir: Path, patterns: list[str]) -> None:
        nonlocal archive_attempts
        if dst_dir.name == s0:
            archive_attempts += 1
            raise OSError("disk full during apply archive")
        real_archive(src_dir, dst_dir, patterns)

    monkeypatch.setattr(campaign, "_archive_sample_artifacts", _failing_archive)

    out = campaign.step_apply_parameters(samples)

    assert archive_attempts == 3
    assert s0 not in out
    assert s1 in out
    s0_state = campaign._sample_state[s0]
    assert s0_state["apply_status"] == "failed"
    assert s0_state["apply_exit_code"] == 1
    assert s0_state["apply_parameters_status"] == "failed"
    assert s0_state["failure_class"] == "bookkeeping_error"
    assert campaign._sample_state[s1]["apply_status"] == "ok"

    apply_events = [status for step, status in item_events if step == "APPLY_PARAMETERS"]
    assert apply_events.count("ok") == 1
    assert apply_events.count("failed") == 1


def test_run_openstudio_sim_bookkeeping_failure_avoids_partial_ok_state(
    monkeypatch: pytest.MonkeyPatch, workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Issue #2023: if archiving fails all 3 attempts in RUN_OPENSTUDIO_SIM, the
    sample is omitted from ``out``, ``sim_status`` is ``'failed'`` (never
    ``'ok'``), and ``trace.step_item_done`` only records ``status='failed'``.
    """
    monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    samples = campaign.step_generate_samples(AlgorithmRegistry.get("lhs"))
    s0, s1 = [str(s["sample_id"]) for s in samples]
    parameterized = campaign.step_apply_parameters(samples)

    item_events: list[tuple[str, str]] = []
    real_item_done = campaign.trace.step_item_done

    def _item_done(step: str, status: str = "ok") -> None:
        item_events.append((step, status))
        real_item_done(step, status=status)

    monkeypatch.setattr(campaign.trace, "step_item_done", _item_done)

    real_archive = campaign._archive_sample_artifacts
    archive_attempts = 0

    def _failing_archive(src_dir: Path, dst_dir: Path, patterns: list[str]) -> None:
        nonlocal archive_attempts
        if dst_dir.name == s0 and dst_dir.parent.name == "sim":
            archive_attempts += 1
            raise OSError("disk full during sim archive")
        real_archive(src_dir, dst_dir, patterns)

    monkeypatch.setattr(campaign, "_archive_sample_artifacts", _failing_archive)

    sim_result = campaign.step_run_openstudio_sim(parameterized)

    assert archive_attempts == 3
    assert s0 not in sim_result.samples
    assert s1 in sim_result.samples
    assert not sim_result.success
    s0_state = campaign._sample_state[s0]
    assert s0_state["sim_status"] == "failed"
    assert s0_state["sim_exit_code"] == 1
    assert s0_state["run_openstudio_sim_status"] == "failed"
    assert s0_state["failure_class"] == "bookkeeping_error"
    assert campaign._sample_state[s1]["sim_status"] == "ok"

    sim_events = [status for step, status in item_events if step == "RUN_OPENSTUDIO_SIM"]
    assert sim_events.count("ok") == 1
    assert sim_events.count("failed") == 1


def test_extract_kpis_bookkeeping_failure_avoids_partial_ok_state(
    monkeypatch: pytest.MonkeyPatch, workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Issue #2023: if ``_publish_sample_results`` fails all 3 attempts in
    EXTRACT_KPIS, the sample is omitted from ``out``, ``extract_status`` is
    ``'failed'`` (never ``'ok'``), and ``trace.step_item_done`` only records
    ``status='failed'``.
    """
    monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    samples = campaign.step_generate_samples(AlgorithmRegistry.get("lhs"))
    s0, s1 = [str(s["sample_id"]) for s in samples]
    parameterized = campaign.step_apply_parameters(samples)
    sim_result = campaign.step_run_openstudio_sim(parameterized)

    item_events: list[tuple[str, str]] = []
    real_item_done = campaign.trace.step_item_done

    def _item_done(step: str, status: str = "ok") -> None:
        item_events.append((step, status))
        real_item_done(step, status=status)

    monkeypatch.setattr(campaign.trace, "step_item_done", _item_done)

    real_publish = campaign._publish_sample_results
    publish_attempts = 0

    def _failing_publish(*args: Any, **kwargs: Any) -> None:
        nonlocal publish_attempts
        if kwargs.get("sample_id") == s0:
            publish_attempts += 1
            raise OSError("remote storage publish failed")
        real_publish(*args, **kwargs)

    monkeypatch.setattr(campaign, "_publish_sample_results", _failing_publish)

    out = campaign.step_extract_kpis(sim_result.samples)

    assert publish_attempts == 3
    out_names = [p.name for p in out]
    assert f"kpi_{s0}.json" not in out_names
    assert f"kpi_{s1}.json" in out_names
    s0_state = campaign._sample_state[s0]
    assert s0_state["extract_status"] == "failed"
    assert s0_state["extract_exit_code"] == 1
    assert s0_state["extract_kpis_status"] == "failed"
    assert s0_state["failure_class"] == "bookkeeping_error"
    assert campaign._sample_state[s1]["extract_status"] == "ok"

    extract_events = [status for step, status in item_events if step == "EXTRACT_KPIS"]
    assert extract_events.count("ok") == 1
    assert extract_events.count("failed") == 1


def test_cache_store_bookkeeping_failure_avoids_partial_ok_state(
    monkeypatch: pytest.MonkeyPatch, workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """When cache.store fails all 3 attempts in _on_success, the sample is
    not added to out, its canonical status is 'failed', and trace records 'failed'."""
    monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    samples = campaign.step_generate_samples(AlgorithmRegistry.get("lhs"))
    s0, s1 = [str(s["sample_id"]) for s in samples]

    item_events: list[tuple[str, str]] = []
    real_item_done = campaign.trace.step_item_done

    def _item_done(step: str, status: str = "ok") -> None:
        item_events.append((step, status))
        real_item_done(step, status=status)

    monkeypatch.setattr(campaign.trace, "step_item_done", _item_done)

    real_store = campaign.cache.store
    store_attempts = 0

    def _failing_store(key: Any, path: Path, exit_code: int = 0) -> None:
        nonlocal store_attempts
        if (
            getattr(key, "step", None) == "APPLY_PARAMETERS"
            and getattr(key, "sample_id", None) == s0
        ):
            store_attempts += 1
            raise OSError("disk full during cache.store")
        real_store(key, path, exit_code=exit_code)

    monkeypatch.setattr(campaign.cache, "store", _failing_store)

    out = campaign.step_apply_parameters(samples)

    assert store_attempts == 3
    assert s0 not in out
    assert s1 in out
    s0_state = campaign._sample_state[s0]
    assert s0_state["apply_status"] == "failed"
    assert s0_state["apply_exit_code"] == 1
    assert s0_state["failure_class"] == "bookkeeping_error"
    assert campaign._sample_state[s1]["apply_status"] == "ok"

    apply_events = [status for step, status in item_events if step == "APPLY_PARAMETERS"]
    assert apply_events.count("ok") == 1
    assert apply_events.count("failed") == 1
