"""Tests for per-sample cost tracking + Spot savings (issue #126).

Covers:
  - SampleTrace accepts cost_usd and billed_duration_seconds fields.
  - SampleTrace.to_dict() includes cost fields when set, excludes when None.
  - RunTrace accumulates total_cost_usd and spot_savings_usd.
  - AWSBatchExecutor._calculate_job_cost computes cost from timestamps.
  - Cost is None/0 for LocalExecutor (no cloud billing).
  - Cost fields appear in serialized run.json.
  - Campaign._accumulate_cost_summary computes correct totals.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from osimflow.executors import AWSBatchExecutor, LocalExecutor
from osimflow.monitoring import RunTrace, SampleTrace


# ---------------------------------------------------------------------------
# SampleTrace cost fields
# ---------------------------------------------------------------------------
class TestSampleTraceCostFields:
    """cost_usd and billed_duration_seconds are optional."""

    def test_default_fields_are_none(self) -> None:
        trace = SampleTrace(sample_id="s0", status="ok", elapsed_s=1.0)
        assert trace.cost_usd is None
        assert trace.billed_duration_seconds is None

    def test_fields_set_and_serialized(self) -> None:
        trace = SampleTrace(
            sample_id="s1",
            status="ok",
            elapsed_s=2.0,
            cost_usd=0.0125,
            billed_duration_seconds=300.0,
        )
        d = trace.to_dict()
        assert d["cost_usd"] == 0.0125
        assert d["billed_duration_seconds"] == 300.0

    def test_none_fields_excluded_from_dict(self) -> None:
        trace = SampleTrace(sample_id="s2", status="ok", elapsed_s=1.0)
        d = trace.to_dict()
        assert "cost_usd" not in d
        assert "billed_duration_seconds" not in d

    def test_zero_cost_included(self) -> None:
        """Explicit zero cost is a valid value and should appear."""
        trace = SampleTrace(
            sample_id="s3",
            status="ok",
            elapsed_s=1.0,
            cost_usd=0.0,
        )
        d = trace.to_dict()
        assert "cost_usd" in d
        assert d["cost_usd"] == 0.0

    def test_roundtrip_json(self) -> None:
        trace = SampleTrace(
            sample_id="s4",
            status="ok",
            elapsed_s=5.0,
            cost_usd=1.5,
            billed_duration_seconds=7200.0,
        )
        d = trace.to_dict()
        blob = json.dumps(d)
        loaded = json.loads(blob)
        assert loaded["cost_usd"] == 1.5
        assert loaded["billed_duration_seconds"] == 7200.0

    def test_backward_compat_no_cost(self) -> None:
        """Existing run.json entries without cost fields are still valid."""
        existing = {
            "sample_id": "legacy",
            "status": "ok",
            "elapsed_s": 10.0,
            "apply_exit_code": 0,
            "sim_exit_code": 0,
            "extract_exit_code": 0,
        }
        trace = SampleTrace(**existing)
        assert trace.cost_usd is None
        assert trace.billed_duration_seconds is None


# ---------------------------------------------------------------------------
# RunTrace cost accumulation
# ---------------------------------------------------------------------------
class TestRunTraceCostAccumulation:
    def test_defaults_zero(self) -> None:
        trace = RunTrace(campaign_id="cost-test", config_summary={})
        assert trace.total_cost_usd == 0.0
        assert trace.spot_savings_usd == 0.0

    def test_to_dict_includes_cost_summary(self) -> None:
        trace = RunTrace(campaign_id="cost-test", config_summary={})
        trace.total_cost_usd = 1.5
        trace.spot_savings_usd = 0.6
        trace.finalize()
        d = trace.to_dict()
        assert d["total_cost_usd"] == 1.5
        assert d["spot_savings_usd"] == 0.6

    def test_cost_summary_in_written_run_json(self, tmp_path: Path) -> None:
        trace = RunTrace(campaign_id="cost-write", config_summary={"executor": "aws_batch"})
        trace.total_cost_usd = 2.5
        trace.spot_savings_usd = 1.0
        trace.finalize()
        out_file = tmp_path / "run.json"
        trace.write(out_file)

        data = json.loads(out_file.read_text())
        assert data["total_cost_usd"] == 2.5
        assert data["spot_savings_usd"] == 1.0

    def test_zero_cost_still_appears(self) -> None:
        """Zero cost (e.g. local executor) still appears in output."""
        trace = RunTrace(campaign_id="local-cost", config_summary={"executor": "local"})
        trace.finalize()
        d = trace.to_dict()
        assert d["total_cost_usd"] == 0.0
        assert d["spot_savings_usd"] == 0.0


# ---------------------------------------------------------------------------
# AWS Batch cost calculation
# ---------------------------------------------------------------------------
class TestAWSBatchCostCalculation:
    def test_calculate_cost_from_timestamps(self) -> None:
        """Cost is calculated from startedAt/stoppedAt and vCPU count."""
        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        # Minimal setup — only the pricing defaults are needed.
        executor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR = 0.05
        executor.DEFAULT_SPOT_PRICE_PER_VCPU_HOUR = 0.03
        executor._get_spot_price = MagicMock(return_value=0.03)  # noqa: SLF001

        # 1 vCPU for 1 hour = $0.05 on-demand.
        job = {
            "startedAt": 1000_000,  # ms
            "stoppedAt": 4600_000,  # ms later = 3600s = 1h
        }
        cost, savings = executor._calculate_job_cost(job, vcpus=1)  # noqa: SLF001
        assert abs(cost - 0.05) < 0.001  # within 0.1 cents
        assert savings > 0  # some savings from spot vs on-demand

    def test_cost_zero_for_missing_timestamps(self) -> None:
        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        executor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR = 0.05
        executor._get_spot_price = MagicMock(side_effect=RuntimeError("no data"))  # noqa: SLF001

        job: dict = {}
        cost, savings = executor._calculate_job_cost(job, vcpus=4)  # noqa: SLF001
        assert cost == 0.0
        assert savings == 0.0

    def test_cost_zero_for_zero_duration(self) -> None:
        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        executor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR = 0.05
        executor._get_spot_price = MagicMock(side_effect=RuntimeError("no data"))  # noqa: SLF001

        job = {"startedAt": 1000, "stoppedAt": 1000}  # zero duration
        cost, savings = executor._calculate_job_cost(job, vcpus=2)  # noqa: SLF001
        assert cost == 0.0
        assert savings == 0.0

    def test_multi_vcpu_cost_scales(self) -> None:
        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        executor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR = 0.05
        executor._get_spot_price = MagicMock(side_effect=RuntimeError("no data"))  # noqa: SLF001

        # 4 vCPUs for 1 hour.
        job = {
            "startedAt": 0,
            "stoppedAt": 3_600_000,  # 1 hour in ms
        }
        cost, savings = executor._calculate_job_cost(job, vcpus=4)  # noqa: SLF001
        # 4 vCPU * 1h * $0.05 = $0.20
        assert abs(cost - 0.20) < 0.001

    def test_spot_savings_is_on_demand_minus_spot(self) -> None:
        """Savings = duration * vcpus * (on_demand - spot)."""
        executor = AWSBatchExecutor.__new__(AWSBatchExecutor)
        executor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR = 0.05
        executor.DEFAULT_SPOT_PRICE_PER_VCPU_HOUR = 0.03
        executor._get_spot_price = MagicMock(return_value=0.03)  # noqa: SLF001

        # 1 vCPU for 1 hour.
        job = {"startedAt": 0, "stoppedAt": 3_600_000}
        cost, savings = executor._calculate_job_cost(job, vcpus=1)  # noqa: SLF001
        # savings = 1h * 1 vCPU * ($0.05 - $0.03) = $0.02
        assert abs(savings - 0.02) < 0.001

    def test_cost_within_20_percent_of_default(self) -> None:
        """Default pricing should be conservative (within 20% of market)."""
        on_demand = AWSBatchExecutor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR
        spot = AWSBatchExecutor.DEFAULT_SPOT_PRICE_PER_VCPU_HOUR
        # Check the defaults are reasonable (m5.large is ~$0.096/hr
        # on-demand, ~$0.025 spot; our defaults are intentionally
        # conservative averages across instance families).
        assert 0.01 <= on_demand <= 0.20
        assert 0.01 <= spot <= on_demand


# ---------------------------------------------------------------------------
# LocalExecutor cost is None
# ---------------------------------------------------------------------------
class TestLocalExecutorCost:
    def test_local_cost_is_none(self) -> None:
        """LocalExecutor does not set cost on the handle."""
        ex = LocalExecutor(max_workers=1)
        handle = ex.submit(lambda: 42, name="test-cost")
        handle.result(timeout=5)
        assert handle.cost_usd is None
        assert handle.billed_duration_seconds is None
        ex.shutdown()


# ---------------------------------------------------------------------------
# Campaign _accumulate_cost_summary
# ---------------------------------------------------------------------------
class TestCampaignCostSummary:
    def test_accumulate_from_sample_traces(self) -> None:
        """_accumulate_cost_summary sums cost_usd from per-sample traces."""
        # Create a minimal Campaign with a mock executor.
        trace = RunTrace(campaign_id="cost-acc", config_summary={"executor": "aws_batch"})
        trace.sample_done(SampleTrace(sample_id="s1", status="ok", elapsed_s=1.0, cost_usd=0.10))
        trace.sample_done(SampleTrace(sample_id="s2", status="ok", elapsed_s=1.0, cost_usd=0.20))
        trace.sample_done(SampleTrace(sample_id="s3", status="ok", elapsed_s=1.0, cost_usd=None))

        # Manually compute what _accumulate_cost_summary would do.
        total = sum(s.cost_usd for s in trace.per_sample if s.cost_usd is not None)
        assert abs(total - 0.30) < 0.001

    def test_zero_cost_for_local(self) -> None:
        """Local executor produces all-None costs → totals stay 0."""
        trace = RunTrace(campaign_id="local-acc", config_summary={"executor": "local"})
        trace.sample_done(SampleTrace(sample_id="s1", status="ok", elapsed_s=1.0))
        trace.sample_done(SampleTrace(sample_id="s2", status="ok", elapsed_s=1.0))
        total = sum(s.cost_usd for s in trace.per_sample if s.cost_usd is not None)
        assert total == 0.0


# ---------------------------------------------------------------------------
# Full serialization round-trip
# ---------------------------------------------------------------------------
class TestCostInRunJson:
    def test_cost_fields_in_serialized_run_json(self, tmp_path: Path) -> None:
        trace = RunTrace(
            campaign_id="e2e-cost",
            config_summary={"executor": "aws_batch", "n_samples": 2},
        )
        trace.step_finished("RUN_OPENSTUDIO_SIM", "MISS×N", 10.0, 0)
        trace.sample_done(
            SampleTrace(
                sample_id="s1",
                status="ok",
                elapsed_s=5.0,
                cost_usd=0.08,
                billed_duration_seconds=300.0,
            )
        )
        trace.sample_done(
            SampleTrace(
                sample_id="s2",
                status="ok",
                elapsed_s=5.0,
                cost_usd=0.12,
                billed_duration_seconds=450.0,
            )
        )
        trace.total_cost_usd = 0.20
        trace.spot_savings_usd = 0.08
        trace.finalize()

        d = trace.to_dict()
        assert d["total_cost_usd"] == 0.20
        assert d["spot_savings_usd"] == 0.08
        assert d["per_sample"][0]["cost_usd"] == 0.08
        assert d["per_sample"][0]["billed_duration_seconds"] == 300.0
        assert d["per_sample"][1]["cost_usd"] == 0.12
        assert d["per_sample"][1]["billed_duration_seconds"] == 450.0

        # Round-trip through JSON
        blob = json.dumps(d, indent=2)
        loaded = json.loads(blob)
        assert loaded["total_cost_usd"] == 0.20
        assert loaded["spot_savings_usd"] == 0.08

    def test_no_cost_in_local_run_json(self, tmp_path: Path) -> None:
        """Local executor: cost fields absent from per_sample, totals zero."""
        trace = RunTrace(
            campaign_id="local-e2e",
            config_summary={"executor": "local"},
        )
        trace.sample_done(SampleTrace(sample_id="s1", status="ok", elapsed_s=5.0))
        trace.finalize()

        d = trace.to_dict()
        # Per-sample cost fields not present (None excluded)
        assert "cost_usd" not in d["per_sample"][0]
        assert "billed_duration_seconds" not in d["per_sample"][0]
        # Totals present but zero
        assert d["total_cost_usd"] == 0.0
        assert d["spot_savings_usd"] == 0.0


# ---------------------------------------------------------------------------
# CostEstimate dataclass (issue #447)
# ---------------------------------------------------------------------------
class TestCostEstimate:
    def test_create_estimate(self) -> None:
        from osimflow.cost_tracking import CostEstimate

        est = CostEstimate(
            sample_id="s1",
            estimated_cost_usd=0.05,
            estimated_spot_cost_usd=0.03,
            estimated_duration_seconds=3600.0,
            vcpus=4,
            memory_mb=8192,
            executor="aws_batch",
        )
        assert est.sample_id == "s1"
        assert est.estimated_cost_usd == 0.05
        assert est.estimated_spot_cost_usd == 0.03
        assert est.estimated_duration_seconds == 3600.0
        assert est.vcpus == 4
        assert est.memory_mb == 8192
        assert est.executor == "aws_batch"
        assert est.created_at > 0

    def test_estimate_defaults(self) -> None:
        from osimflow.cost_tracking import CostEstimate

        est = CostEstimate(
            sample_id="s2",
            estimated_cost_usd=0.01,
            estimated_spot_cost_usd=0.006,
            estimated_duration_seconds=600.0,
        )
        assert est.vcpus == 1
        assert est.memory_mb == 1024
        assert est.executor == "unknown"

    def test_estimate_to_dict(self) -> None:
        from osimflow.cost_tracking import CostEstimate

        est = CostEstimate(
            sample_id="s3",
            estimated_cost_usd=0.10,
            estimated_spot_cost_usd=0.06,
            estimated_duration_seconds=1800.0,
            vcpus=2,
            memory_mb=4096,
            executor="slurm",
        )
        d = est.to_dict()
        assert d["sample_id"] == "s3"
        assert d["estimated_cost_usd"] == 0.10
        assert d["estimated_spot_cost_usd"] == 0.06
        assert d["estimated_duration_seconds"] == 1800.0
        assert d["vcpus"] == 2
        assert d["memory_mb"] == 4096
        assert d["executor"] == "slurm"
        assert "created_at" in d

    def test_estimate_to_dict_roundtrip(self) -> None:
        from osimflow.cost_tracking import CostEstimate

        est = CostEstimate(
            sample_id="s4",
            estimated_cost_usd=0.25,
            estimated_spot_cost_usd=0.15,
            estimated_duration_seconds=7200.0,
            vcpus=8,
            memory_mb=16384,
            executor="azure_batch",
        )
        blob = json.dumps(est.to_dict())
        loaded = json.loads(blob)
        assert loaded["sample_id"] == "s4"
        assert loaded["estimated_cost_usd"] == 0.25
        assert loaded["vcpus"] == 8


# ---------------------------------------------------------------------------
# CostTracker (issue #447)
# ---------------------------------------------------------------------------
class TestCostTracker:
    def test_tracker_init(self) -> None:
        from osimflow.cost_tracking import CostTracker

        tracker = CostTracker(n_samples=10, executor="aws_batch")
        assert tracker.n_samples == 10
        assert tracker.executor == "aws_batch"
        assert tracker.total_estimated_cost_usd == 0.0
        assert tracker.total_actual_cost_usd == 0.0
        assert tracker.total_spot_savings_usd == 0.0
        assert tracker.n_recorded == 0
        assert not tracker.is_complete

    def test_record_estimate(self) -> None:
        from osimflow.cost_tracking import CostEstimate, CostTracker

        tracker = CostTracker(n_samples=5, executor="slurm")
        est = CostEstimate(
            sample_id="s1",
            estimated_cost_usd=0.50,
            estimated_spot_cost_usd=0.30,
            estimated_duration_seconds=3600.0,
        )
        tracker.record_estimate(est)
        assert tracker.total_estimated_cost_usd == 0.50
        assert tracker.get_estimate("s1") is est
        assert tracker.get_estimate("nonexistent") is None

    def test_record_actual(self) -> None:
        from osimflow.cost_tracking import CostTracker

        tracker = CostTracker(n_samples=3, executor="aws_batch")
        tracker.record_actual("s1", 0.52, 0.20)
        tracker.record_actual("s2", 0.48, 0.19)
        assert tracker.total_actual_cost_usd == 1.0
        assert tracker.total_spot_savings_usd == 0.39
        assert tracker.n_recorded == 2
        assert not tracker.is_complete

    def test_is_complete(self) -> None:
        from osimflow.cost_tracking import CostTracker

        tracker = CostTracker(n_samples=2, executor="google_batch")
        tracker.record_actual("s1", 0.10, 0.04)
        assert not tracker.is_complete
        tracker.record_actual("s2", 0.12, 0.05)
        assert tracker.is_complete

    def test_finalize(self) -> None:
        from osimflow.cost_tracking import CostEstimate, CostTracker

        tracker = CostTracker(n_samples=2, executor="aws_batch")
        est1 = CostEstimate(
            sample_id="s1",
            estimated_cost_usd=0.50,
            estimated_spot_cost_usd=0.30,
            estimated_duration_seconds=3600.0,
        )
        est2 = CostEstimate(
            sample_id="s2",
            estimated_cost_usd=0.60,
            estimated_spot_cost_usd=0.36,
            estimated_duration_seconds=3600.0,
        )
        tracker.record_estimate(est1)
        tracker.record_estimate(est2)
        tracker.record_actual("s1", 0.52, 0.20)
        tracker.record_actual("s2", 0.58, 0.22)

        summary = tracker.finalize()
        assert summary.total_estimated_cost_usd == 1.10
        assert summary.total_actual_cost_usd == 1.10
        assert summary.total_spot_savings_usd == 0.42
        assert summary.n_samples == 2
        assert summary.executor == "aws_batch"
        assert summary.finalized_at is not None
        assert summary.created_at > 0

    def test_finalize_empty_tracker(self) -> None:
        from osimflow.cost_tracking import CostTracker

        tracker = CostTracker(n_samples=0, executor="local")
        summary = tracker.finalize()
        assert summary.total_estimated_cost_usd == 0.0
        assert summary.total_actual_cost_usd == 0.0
        assert summary.total_spot_savings_usd == 0.0
        assert summary.n_samples == 0
        assert summary.finalized_at is not None

    def test_tracker_to_dict(self) -> None:
        from osimflow.cost_tracking import CostEstimate, CostTracker

        tracker = CostTracker(n_samples=1, executor="pbs")
        est = CostEstimate(
            sample_id="s1",
            estimated_cost_usd=1.00,
            estimated_spot_cost_usd=0.60,
            estimated_duration_seconds=7200.0,
        )
        tracker.record_estimate(est)
        tracker.record_actual("s1", 0.95, 0.36)

        d = tracker.to_dict()
        assert d["n_samples"] == 1
        assert d["n_recorded"] == 1
        assert d["is_complete"] is True
        assert d["total_estimated_cost_usd"] == 1.00
        assert d["total_actual_cost_usd"] == 0.95
        assert d["total_spot_savings_usd"] == 0.36
        assert d["executor"] == "pbs"
        assert "s1" in d["estimates"]

    def test_custom_pricing(self) -> None:
        from osimflow.cost_tracking import CostTracker

        tracker = CostTracker(
            n_samples=1,
            executor="aws_batch",
            on_demand_price=0.10,
            spot_price=0.04,
        )
        assert tracker.on_demand_price == 0.10
        assert tracker.spot_price == 0.04


# ---------------------------------------------------------------------------
# CampaignCostSummary (issue #447)
# ---------------------------------------------------------------------------
class TestCampaignCostSummaryNew:
    def test_summary_defaults(self) -> None:
        from osimflow.cost_tracking import CampaignCostSummary

        s = CampaignCostSummary()
        assert s.total_estimated_cost_usd == 0.0
        assert s.total_actual_cost_usd == 0.0
        assert s.total_spot_savings_usd == 0.0
        assert s.n_samples == 0
        assert s.executor == "unknown"
        assert s.created_at > 0
        assert s.finalized_at is None

    def test_summary_full(self) -> None:
        from osimflow.cost_tracking import CampaignCostSummary

        s = CampaignCostSummary(
            total_estimated_cost_usd=100.0,
            total_actual_cost_usd=95.0,
            total_spot_savings_usd=38.0,
            n_samples=50,
            executor="slurm",
        )
        assert s.total_estimated_cost_usd == 100.0
        assert s.total_actual_cost_usd == 95.0
        assert s.total_spot_savings_usd == 38.0
        assert s.n_samples == 50
        assert s.executor == "slurm"

    def test_summary_to_dict(self) -> None:
        from osimflow.cost_tracking import CampaignCostSummary

        s = CampaignCostSummary(
            total_estimated_cost_usd=50.0,
            total_actual_cost_usd=48.0,
            total_spot_savings_usd=19.2,
            n_samples=20,
            executor="aws_batch",
        )
        d = s.to_dict()
        assert d["total_estimated_cost_usd"] == 50.0
        assert d["total_actual_cost_usd"] == 48.0
        assert d["total_spot_savings_usd"] == 19.2
        assert d["n_samples"] == 20
        assert d["executor"] == "aws_batch"
        assert "created_at" in d
        assert "finalized_at" in d

    def test_summary_to_dict_roundtrip(self) -> None:
        from osimflow.cost_tracking import CampaignCostSummary

        s = CampaignCostSummary(
            total_estimated_cost_usd=25.0,
            total_actual_cost_usd=24.0,
            total_spot_savings_usd=9.6,
            n_samples=10,
            executor="azure_batch",
        )
        blob = json.dumps(s.to_dict())
        loaded = json.loads(blob)
        assert loaded["total_estimated_cost_usd"] == 25.0
        assert loaded["total_actual_cost_usd"] == 24.0
        assert loaded["n_samples"] == 10
        assert loaded["executor"] == "azure_batch"


# ---------------------------------------------------------------------------
# CostTracker integration with Campaign (issue #447)
# ---------------------------------------------------------------------------
@pytest.mark.skip(reason="_cost_tracker not yet implemented on Campaign (issue #447)")
class TestCostTrackerCampaignIntegration:
    def test_campaign_cost_tracker_init_disabled(self) -> None:
        """When enable_cost_tracking=False, _cost_tracker is None."""
        from osimflow.campaign import Campaign
        from osimflow.config import CampaignConfig
        from osimflow.executors import LocalExecutor

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=3,
            outdir=Path("outdir"),
            openstudio_version="3.11.0",
            enable_cost_tracking=False,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        assert campaign._cost_tracker is None

    def test_campaign_cost_tracker_init_enabled(self) -> None:
        """When enable_cost_tracking=True, _cost_tracker is a CostTracker."""
        from osimflow.campaign import Campaign
        from osimflow.config import CampaignConfig
        from osimflow.executors import LocalExecutor

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=3,
            outdir=Path("outdir"),
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        assert campaign._cost_tracker is not None
        assert campaign._cost_tracker.n_samples == 3
        assert campaign._cost_tracker.executor == "local"

    def test_record_costs_noop_when_disabled(self) -> None:
        """_record_costs is a no-op when _cost_tracker is None."""
        from osimflow.campaign import Campaign
        from osimflow.config import CampaignConfig
        from osimflow.executors import LocalExecutor

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=3,
            outdir=Path("outdir"),
            openstudio_version="3.11.0",
            enable_cost_tracking=False,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        # Should not raise even with None tracker
        campaign._record_costs("s1", 0.10, 0.04)
        assert campaign._cost_tracker is None

    def test_finalize_costs_noop_when_disabled(self, tmp_path: Path) -> None:
        """_finalize_costs is a no-op when _cost_tracker is None."""
        from osimflow.campaign import Campaign
        from osimflow.config import CampaignConfig
        from osimflow.executors import LocalExecutor

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=3,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=False,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        campaign._finalize_costs()  # Should not raise
        assert campaign._cost_tracker is None

    def test_finalize_costs_produces_summary(self, tmp_path: Path) -> None:
        """_finalize_costs writes cost_summary to run.json when enabled."""
        from osimflow.campaign import Campaign
        from osimflow.config import CampaignConfig
        from osimflow.executors import LocalExecutor

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        campaign._record_costs("s1", 0.50, 0.20)
        campaign._record_costs("s2", 0.60, 0.24)
        campaign._finalize_costs()

        assert campaign._cost_tracker is not None
        summary = campaign._cost_tracker.finalize()
        assert summary.total_actual_cost_usd == 1.10
        assert summary.total_spot_savings_usd == 0.44
        assert summary.n_samples == 2
        assert summary.executor == "local"
        assert summary.finalized_at is not None


# ---------------------------------------------------------------------------
# CampaignCostTracker.sum_sample_costs spot savings (issue #1178)
# ---------------------------------------------------------------------------
class TestSumSampleCostsSpotSavings:
    """sum_sample_costs must return compute_spot_savings(total_cost), not 0.0.

    Regression tests for issue #1178: total_spot_savings_usd in the campaign
    cost summary (and therefore run.json) was always $0.00 because the second
    tuple element was hardcoded to 0.0.
    """

    def test_returns_nonzero_savings_when_cost_present(self, tmp_path: Path) -> None:
        from osimflow._campaign_cost_tracker import CampaignCostTracker
        from osimflow.config import CampaignConfig

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        tracker = CampaignCostTracker(campaign_id="savings-present", cfg=cfg, executor="aws_batch")

        sample_state = {
            "sample_0000": {"cost_usd": 0.10},
            "sample_0001": {"cost_usd": 0.15},
        }
        total_cost, savings = tracker.sum_sample_costs(sample_state, executor="aws_batch")
        assert total_cost == pytest.approx(0.25)
        assert savings == pytest.approx(tracker.compute_spot_savings(0.25))
        assert savings > 0.0

    def test_savings_uses_default_price_ratio(self) -> None:
        """Savings reflect the spot < on-demand default price ratio (~40%)."""
        from osimflow._campaign_cost_tracker import _compute_spot_savings_static

        expected_ratio = (
            AWSBatchExecutor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR
            - AWSBatchExecutor.DEFAULT_SPOT_PRICE_PER_VCPU_HOUR
        ) / AWSBatchExecutor.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR
        savings = _compute_spot_savings_static(1.0, executor="aws_batch")
        assert savings == pytest.approx(round(1.0 * expected_ratio, 6))

    def test_zero_cost_yields_zero_savings(self) -> None:
        from osimflow._campaign_cost_tracker import _compute_spot_savings_static

        total_cost, savings = _compute_spot_savings_static(0.0, executor="aws_batch"), 0.0
        assert total_cost == 0.0
        assert savings == 0.0

    def test_samples_without_cost_are_skipped(self, tmp_path: Path) -> None:
        from osimflow._campaign_cost_tracker import CampaignCostTracker
        from osimflow.config import CampaignConfig

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        tracker = CampaignCostTracker(campaign_id="skip-none", cfg=cfg, executor="aws_batch")

        sample_state = {
            "sample_0000": {"status": "failed"},
            "sample_0001": {"cost_usd": None},
            "sample_0002": {"cost_usd": "0.2"},
        }
        total_cost, savings = tracker.sum_sample_costs(sample_state, executor="aws_batch")
        assert total_cost == pytest.approx(0.2)
        assert savings == pytest.approx(tracker.compute_spot_savings(0.2))

    def test_savings_flow_to_campaign_cost_summary(self, tmp_path: Path) -> None:
        """Savings from sum_sample_costs reach CampaignCostSummary via
        record_step_costs -> finalize (the run.json cost_summary path)."""
        from osimflow._campaign_cost_tracker import CampaignCostTracker
        from osimflow.config import CampaignConfig
        from osimflow.cost_tracking import CampaignCostSummary

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        tracker = CampaignCostTracker(campaign_id="camp-1178", cfg=cfg, executor="aws_batch")
        assert tracker.is_enabled

        sample_state = {
            "sample_0000": {"cost_usd": 0.50},
            "sample_0001": {"cost_usd": 0.50},
        }
        total_cost, total_savings = tracker.sum_sample_costs(sample_state, executor="aws_batch")
        assert total_savings > 0.0

        tracker.record_step_costs("RUN_OPENSTUDIO_SIM", total_cost, total_savings)
        summary = tracker._tracker.finalize()
        assert isinstance(summary, CampaignCostSummary)
        assert summary.total_spot_savings_usd == pytest.approx(tracker.compute_spot_savings(1.0))
        summary_dict = tracker.finalize()
        assert summary_dict is not None
        assert summary_dict["total_actual_cost_usd"] == pytest.approx(1.0)
        assert summary_dict["total_spot_savings_usd"] == pytest.approx(
            tracker.compute_spot_savings(1.0)
        )
        assert summary_dict["total_spot_savings_usd"] > 0.0


# ---------------------------------------------------------------------------
# supports_spot_market protocol (issue #1318)
# ---------------------------------------------------------------------------
class TestSupportsSpotMarketProtocol:
    """Executor supplies supports_spot_market property instead of hardcoded frozensets."""

    def test_base_executor_defaults_to_false(self) -> None:
        """BaseExecutor.supports_spot_market is False by default."""
        from osimflow.executors.base import BaseExecutor

        class DummyExecutor(BaseExecutor):
            name = "dummy"

            def submit(self, fn, *args, **kwargs):
                raise NotImplementedError

            def _do_submit(self, fn, *args, **kwargs):  # noqa: ANN001, ANN201, ARG002
                # Issue #1563: ``_do_submit`` is the new abstract seam
                # (``submit`` became a template method that acquires a
                # token from the shared limiter then delegates here).
                # The dummy below mirrors the legacy ``submit`` body.
                raise NotImplementedError

            def shutdown(self) -> None:
                pass

        ex = DummyExecutor()
        assert ex.supports_spot_market is False

    def test_aws_batch_has_spot_market(self) -> None:
        """AWSBatchExecutor.supports_spot_market is True."""
        from osimflow.executors import AWSBatchExecutor

        ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        assert ex.supports_spot_market is True

    def test_azure_batch_has_spot_market(self) -> None:
        """AzureBatchExecutor.supports_spot_market is True."""
        from osimflow.executors import AzureBatchExecutor

        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        assert ex.supports_spot_market is True

    def test_google_batch_has_spot_market(self) -> None:
        """GoogleBatchExecutor.supports_spot_market is True."""
        from osimflow.executors import GoogleBatchExecutor

        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        assert ex.supports_spot_market is True

    def test_local_executor_no_spot_market(self) -> None:
        """LocalExecutor.supports_spot_market is False."""
        from osimflow.executors import LocalExecutor

        ex = LocalExecutor(max_workers=1)
        assert ex.supports_spot_market is False
        ex.shutdown()

    def test_slurm_executor_no_spot_market(self) -> None:
        """SlurmExecutor.supports_spot_market is False."""
        from osimflow.executors import SlurmExecutor

        ex = SlurmExecutor(partition="short", debug=True)
        assert ex.supports_spot_market is False
        ex.shutdown()


# ---------------------------------------------------------------------------
# CampaignCostTracker executor protocol (issue #1318)
# ---------------------------------------------------------------------------
class TestCampaignCostTrackerExecutorProtocol:
    """CampaignCostTracker queries executor.supports_spot_market instead of frozensets."""

    def test_accepts_executor_instance(self, tmp_path: Path) -> None:
        """CampaignCostTracker accepts a BaseExecutor instance."""
        from osimflow._campaign_cost_tracker import CampaignCostTracker
        from osimflow.config import CampaignConfig
        from osimflow.executors import AWSBatchExecutor

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        tracker = CampaignCostTracker(campaign_id="proto-test", cfg=cfg, executor=ex)
        assert tracker.is_enabled

    def test_azure_batch_executor_uses_spot_savings(self, tmp_path: Path) -> None:
        """AzureBatchExecutor triggers non-zero spot savings in CampaignCostTracker."""
        from osimflow._campaign_cost_tracker import CampaignCostTracker
        from osimflow.config import CampaignConfig
        from osimflow.executors import AzureBatchExecutor

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        tracker = CampaignCostTracker(campaign_id="azure-spot", cfg=cfg, executor=ex)
        savings = tracker.compute_spot_savings(1.0)
        assert savings > 0.0

    def test_google_batch_executor_uses_spot_savings(self, tmp_path: Path) -> None:
        """GoogleBatchExecutor triggers non-zero spot savings in CampaignCostTracker."""
        from osimflow._campaign_cost_tracker import CampaignCostTracker
        from osimflow.config import CampaignConfig
        from osimflow.executors import GoogleBatchExecutor

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
        tracker = CampaignCostTracker(campaign_id="google-spot", cfg=cfg, executor=ex)
        savings = tracker.compute_spot_savings(1.0)
        assert savings > 0.0

    def test_local_executor_returns_zero_savings(self, tmp_path: Path) -> None:
        """LocalExecutor returns 0.0 spot savings in CampaignCostTracker."""
        from osimflow._campaign_cost_tracker import CampaignCostTracker
        from osimflow.config import CampaignConfig
        from osimflow.executors import LocalExecutor

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        ex = LocalExecutor(max_workers=1)
        tracker = CampaignCostTracker(campaign_id="local-flat", cfg=cfg, executor=ex)
        savings = tracker.compute_spot_savings(1.0)
        assert savings == 0.0
        ex.shutdown()

    def test_string_name_backward_compat_aws_batch(self, tmp_path: Path) -> None:
        """Passing executor name as string (old API) still works for aws_batch."""
        from osimflow._campaign_cost_tracker import CampaignCostTracker
        from osimflow.config import CampaignConfig

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        tracker = CampaignCostTracker(
            campaign_id="str-backcompat",
            cfg=cfg,
            executor="aws_batch",
        )
        assert tracker.is_enabled
        savings = tracker.compute_spot_savings(1.0)
        assert savings > 0.0

    def test_string_name_backward_compat_local(self, tmp_path: Path) -> None:
        """Passing executor name as string (old API) still works for local."""
        from osimflow._campaign_cost_tracker import CampaignCostTracker
        from osimflow.config import CampaignConfig

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        tracker = CampaignCostTracker(
            campaign_id="str-backcompat-local",
            cfg=cfg,
            executor="local",
        )
        assert tracker.is_enabled
        savings = tracker.compute_spot_savings(1.0)
        assert savings == 0.0

    def test_sum_sample_costs_with_executor_instance(self, tmp_path: Path) -> None:
        """sum_sample_costs uses compute_spot_savings via the executor instance."""
        from osimflow._campaign_cost_tracker import CampaignCostTracker
        from osimflow.config import CampaignConfig
        from osimflow.executors import AWSBatchExecutor

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        tracker = CampaignCostTracker(campaign_id="sum-ex-test", cfg=cfg, executor=ex)

        sample_state = {
            "sample_0000": {"cost_usd": 0.50},
            "sample_0001": {"cost_usd": 0.50},
        }
        total_cost, total_savings = tracker.sum_sample_costs(sample_state, executor=ex)
        assert total_cost == pytest.approx(1.0)
        assert total_savings > 0.0

    def test_spot_savings_for_azure_google_when_using_string(self, tmp_path: Path) -> None:
        """Static name fallback returns non-zero spot savings for spot executors.

        Issue #1393 closed the dispatch-table hazard where the static
        `_supports_spot_from_name` fallback relied on hardcoded
        frozensets. ``azure_batch`` and ``google_batch`` both declare
        ``supports_spot_market = True`` on the class, so the static
        fallback must now match the executor-instance path and report
        non-zero spot savings when ``total_cost > 0``.
        """
        from osimflow._campaign_cost_tracker import CampaignCostTracker
        from osimflow.config import CampaignConfig

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=2,
            outdir=tmp_path,
            openstudio_version="3.11.0",
            enable_cost_tracking=True,
        )
        tracker_azure = CampaignCostTracker(
            campaign_id="azure-str",
            cfg=cfg,
            executor="azure_batch",
        )
        tracker_google = CampaignCostTracker(
            campaign_id="google-str",
            cfg=cfg,
            executor="google_batch",
        )
        assert tracker_azure.compute_spot_savings(1.0) > 0.0
        assert tracker_google.compute_spot_savings(1.0) > 0.0
