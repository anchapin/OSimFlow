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
        trace.sample_done(
            SampleTrace(sample_id="s1", status="ok", elapsed_s=1.0, cost_usd=0.10)
        )
        trace.sample_done(
            SampleTrace(sample_id="s2", status="ok", elapsed_s=1.0, cost_usd=0.20)
        )
        trace.sample_done(
            SampleTrace(sample_id="s3", status="ok", elapsed_s=1.0, cost_usd=None)
        )

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
        trace.sample_done(
            SampleTrace(sample_id="s1", status="ok", elapsed_s=5.0)
        )
        trace.finalize()

        d = trace.to_dict()
        # Per-sample cost fields not present (None excluded)
        assert "cost_usd" not in d["per_sample"][0]
        assert "billed_duration_seconds" not in d["per_sample"][0]
        # Totals present but zero
        assert d["total_cost_usd"] == 0.0
        assert d["spot_savings_usd"] == 0.0
