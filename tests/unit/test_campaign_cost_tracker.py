"""Regression tests for the static spot-savings fallback (issue #1393).

`CampaignCostTracker._supports_spot_from_name` previously relied on two
hardcoded frozensets (`_EXECUTORS_WITH_SPOT` / `_EXECUTORS_WITH_FLAT_RATE`)
that fell out of sync with the executor classes' `supports_spot_market`
attributes. Azure Batch and Google Batch both set `supports_spot_market
= True` on the class but were missing from the dispatch table, so any
caller that only had an executor *name* string (e.g. `--cost-tracking-only`
workflows, external clients without an executor instance) silently
received a zero spot-savings estimate.

The fix routes `_supports_spot_from_name` through `ExecutorRegistry.get`
so the static fallback and the executor-instance path read from the same
source of truth. These tests assert that:

- All ten built-in executor names resolve correctly via the fallback.
- Spot-capable executors (`aws_batch`, `azure_batch`, `google_batch`)
  return non-zero spot savings when `total_cost > 0`.
- Flat-rate executors return zero spot savings.
- Unknown executor names degrade gracefully (no exception, no savings).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from osimflow._campaign_cost_tracker import (
    CampaignCostTracker,
    _supports_spot_from_name,
)
from osimflow.cost_tracking import (
    DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR,
    DEFAULT_SPOT_PRICE_PER_VCPU_HOUR,
)
from osimflow.executors import ExecutorRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SPOT_CAPABLE_EXECUTORS: tuple[str, ...] = ("aws_batch", "azure_batch", "google_batch")
FLAT_RATE_EXECUTORS: tuple[str, ...] = (
    "local",
    "slurm",
    "pbs",
    "nomad",
    "kubernetes",
    "docker_swarm",
    "dask_jobqueue",
)
ALL_EXECUTOR_NAMES: tuple[str, ...] = SPOT_CAPABLE_EXECUTORS + FLAT_RATE_EXECUTORS

EXPECTED_SAVINGS_RATIO: float = (
    DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR - DEFAULT_SPOT_PRICE_PER_VCPU_HOUR
) / DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR


@pytest.fixture
def campaign_cfg(tmp_path: Path) -> object:
    """Minimal CampaignConfig with cost tracking enabled."""
    from osimflow.config import CampaignConfig

    return CampaignConfig(
        input_variables=tmp_path / "variables.yml",
        template_sim_package=tmp_path / "template",
        n_samples=3,
        outdir=tmp_path / "out",
        openstudio_version="3.11.0",
        enable_cost_tracking=True,
    )


# ---------------------------------------------------------------------------
# Class-attribute parity (issue #1393 root cause)
# ---------------------------------------------------------------------------


class TestClassAttributeParity:
    """Every executor registered in `ExecutorRegistry` declares its spot
    capability via the class attribute `supports_spot_market`. The static
    fallback must read that same attribute, not a stale frozenset.
    """

    @pytest.mark.parametrize("executor_name", ALL_EXECUTOR_NAMES)
    def test_executor_class_declares_supports_spot_market(self, executor_name: str) -> None:
        cls = ExecutorRegistry.get(executor_name)
        assert hasattr(cls, "supports_spot_market"), (
            f"{executor_name} class is missing supports_spot_market attribute"
        )

    @pytest.mark.parametrize("executor_name", SPOT_CAPABLE_EXECUTORS)
    def test_spot_capable_classes_report_true(self, executor_name: str) -> None:
        cls = ExecutorRegistry.get(executor_name)
        assert cls.supports_spot_market is True, (
            f"{executor_name} should advertise spot support"
        )

    @pytest.mark.parametrize("executor_name", FLAT_RATE_EXECUTORS)
    def test_flat_rate_classes_report_false(self, executor_name: str) -> None:
        cls = ExecutorRegistry.get(executor_name)
        raw = inspect.getattr_static(cls, "supports_spot_market", False)
        # Flat-rate executors only inherit the BaseExecutor @property that
        # returns False — they do NOT declare their own class attribute
        # shadowing the property. ``inspect.getattr_static`` returns the
        # property descriptor object in that case.
        assert isinstance(raw, property), (
            f"{executor_name} should inherit BaseExecutor.supports_spot_market "
            f"without overriding it (got {raw!r})"
        )


# ---------------------------------------------------------------------------
# `_supports_spot_from_name` — direct fallback tests
# ---------------------------------------------------------------------------


class TestSupportsSpotFromName:
    """The static name-based fallback must match the class attribute."""

    @pytest.mark.parametrize("executor_name", SPOT_CAPABLE_EXECUTORS)
    def test_returns_true_for_spot_capable(self, executor_name: str) -> None:
        assert _supports_spot_from_name(executor_name) is True

    @pytest.mark.parametrize("executor_name", FLAT_RATE_EXECUTORS)
    def test_returns_false_for_flat_rate(self, executor_name: str) -> None:
        assert _supports_spot_from_name(executor_name) is False

    def test_unknown_executor_returns_false(self) -> None:
        """Best-effort fallback: unknown names must not raise."""
        assert _supports_spot_from_name("not_a_real_executor") is False

    def test_unknown_executor_does_not_raise(self) -> None:
        """A misnamed executor must degrade gracefully (issue #1393 contract)."""
        try:
            result = _supports_spot_from_name("definitely_not_registered")
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"fallback must absorb unknown names, got: {exc!r}")
        assert result is False


# ---------------------------------------------------------------------------
# CampaignCostTracker spot savings — end-to-end via CampaignConfig
# ---------------------------------------------------------------------------


class TestCampaignCostTrackerAllExecutors:
    """Issue #1393 acceptance criterion:

    > Constructs CostTracker with each of the 10 executor names and asserts
    > that azure_batch/google_batch spot savings are non-zero when total_cost > 0.
    """

    @pytest.mark.parametrize("executor_name", SPOT_CAPABLE_EXECUTORS)
    def test_spot_capable_executor_returns_nonzero_savings(
        self, campaign_cfg: object, executor_name: str
    ) -> None:
        tracker = CampaignCostTracker(
            campaign_id="issue-1393-spot",
            cfg=campaign_cfg,
            executor=executor_name,
        )
        assert tracker.is_enabled

        total_cost = 10.0
        savings = tracker.compute_spot_savings(total_cost)
        expected_savings = round(total_cost * EXPECTED_SAVINGS_RATIO, 6)
        assert savings > 0.0, (
            f"{executor_name} spot savings should be > 0 when total_cost > 0 "
            f"(got {savings})"
        )
        assert savings == pytest.approx(expected_savings), (
            f"{executor_name} savings mismatch: got {savings}, expected {expected_savings}"
        )

    @pytest.mark.parametrize("executor_name", FLAT_RATE_EXECUTORS)
    def test_flat_rate_executor_returns_zero_savings(
        self, campaign_cfg: object, executor_name: str
    ) -> None:
        tracker = CampaignCostTracker(
            campaign_id="issue-1393-flat",
            cfg=campaign_cfg,
            executor=executor_name,
        )
        assert tracker.is_enabled

        savings = tracker.compute_spot_savings(10.0)
        assert savings == 0.0, (
            f"{executor_name} is flat-rate; spot savings must be 0.0 (got {savings})"
        )

    @pytest.mark.parametrize("executor_name", SPOT_CAPABLE_EXECUTORS)
    def test_zero_total_cost_yields_zero_savings_even_for_spot(
        self, campaign_cfg: object, executor_name: str
    ) -> None:
        """Even for spot executors, zero total cost means zero savings."""
        tracker = CampaignCostTracker(
            campaign_id="issue-1393-zero",
            cfg=campaign_cfg,
            executor=executor_name,
        )
        assert tracker.compute_spot_savings(0.0) == 0.0
        assert tracker.compute_spot_savings(-1.0) == 0.0

    @pytest.mark.parametrize("executor_name", FLAT_RATE_EXECUTORS)
    def test_zero_total_cost_yields_zero_savings_for_flat_rate(
        self, campaign_cfg: object, executor_name: str
    ) -> None:
        tracker = CampaignCostTracker(
            campaign_id="issue-1393-flat-zero",
            cfg=campaign_cfg,
            executor=executor_name,
        )
        assert tracker.compute_spot_savings(0.0) == 0.0


# ---------------------------------------------------------------------------
# `sum_sample_costs` — same fallback path via the static helper
# ---------------------------------------------------------------------------


class TestSumSampleCostsAllExecutors:
    """The module-level `sum_sample_costs` helper also goes through the
    fallback for string-name executors. Regression coverage here matches
    `compute_spot_savings` to lock in the contract on both surfaces.
    """

    @pytest.mark.parametrize("executor_name", SPOT_CAPABLE_EXECUTORS)
    def test_spot_capable_via_sum_sample_costs(self, executor_name: str) -> None:
        sample_state = {
            "s1": {"cost_usd": "4.0"},
            "s2": {"cost_usd": "6.0"},
        }
        total_cost, savings = CampaignCostTracker.sum_sample_costs(
            sample_state, executor_name
        )
        assert total_cost == pytest.approx(10.0)
        assert savings > 0.0
        assert savings == pytest.approx(round(10.0 * EXPECTED_SAVINGS_RATIO, 6))

    @pytest.mark.parametrize("executor_name", FLAT_RATE_EXECUTORS)
    def test_flat_rate_via_sum_sample_costs(self, executor_name: str) -> None:
        sample_state = {
            "s1": {"cost_usd": "4.0"},
            "s2": {"cost_usd": "6.0"},
        }
        total_cost, savings = CampaignCostTracker.sum_sample_costs(
            sample_state, executor_name
        )
        assert total_cost == pytest.approx(10.0)
        assert savings == 0.0


# ---------------------------------------------------------------------------
# Instance-vs-name parity (the exact hazard #1393 closed)
# ---------------------------------------------------------------------------


class TestInstanceAndNameParity:
    """The instance path (`executor.supports_spot_market`) and the
    name fallback (`_supports_spot_from_name(name)`) must agree for
    every spot-capable executor. Before #1393, the static fallback
    returned False for `azure_batch` / `google_batch` while the
    instance path correctly returned True — a silent divergence.
    """

    @pytest.mark.parametrize("executor_name", SPOT_CAPABLE_EXECUTORS)
    def test_instance_and_name_paths_agree(self, executor_name: str) -> None:
        cls = ExecutorRegistry.get(executor_name)
        instance = cls.__new__(cls)  # bypass __init__; only the class attr matters
        assert _supports_spot_from_name(executor_name) == instance.supports_spot_market

    @pytest.mark.parametrize("executor_name", FLAT_RATE_EXECUTORS)
    def test_instance_and_name_paths_agree_for_flat_rate(
        self, executor_name: str
    ) -> None:
        cls = ExecutorRegistry.get(executor_name)
        instance = cls.__new__(cls)
        assert _supports_spot_from_name(executor_name) == instance.supports_spot_market
