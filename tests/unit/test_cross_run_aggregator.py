"""Tests for CrossRunAggregator (issue #588).

Covers:
- CampaignRunData dataclass
- CrossRunStats dataclass (overall_mean, std, best/worst campaign)
- CrossRunAggregator: add/remove campaigns, load CSVs, aggregate
- Combined DataFrame with campaign + global_sample_id columns
- Cross-run statistics computation
- KPI rankings and best campaigns
- export_combined_csv and summary
- Edge cases: missing CSV, empty campaigns, run.json label resolution
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from osimflow.cross_run_aggregator import (
    CampaignRunData,
    CrossRunAggregator,
    CrossRunStats,
)

# ======================================================================
# Fixtures
# ======================================================================


def _make_campaign(
    outdir: Path,
    rows: list[dict[str, float | str]],
    campaign_id: str | None = None,
) -> Path:
    """Create a minimal campaign outdir with aggregated_results.csv + run.json."""
    outdir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(outdir / "aggregated_results.csv", index=False)
    if campaign_id:
        run = {"campaign_id": campaign_id}
        (outdir / "run.json").write_text(json.dumps(run))
    return outdir


@pytest.fixture
def campaign_a(tmp_path: Path) -> Path:
    return _make_campaign(
        tmp_path / "run_a",
        [
            {"sample_id": "0001", "eui": 100.0, "cost": 50.0},
            {"sample_id": "0002", "eui": 110.0, "cost": 45.0},
            {"sample_id": "0003", "eui": 95.0, "cost": 55.0},
        ],
        campaign_id="campaign-alpha",
    )


@pytest.fixture
def campaign_b(tmp_path: Path) -> Path:
    return _make_campaign(
        tmp_path / "run_b",
        [
            {"sample_id": "0001", "eui": 80.0, "cost": 60.0},
            {"sample_id": "0002", "eui": 85.0, "cost": 58.0},
        ],
    )


@pytest.fixture
def two_campaigns(campaign_a: Path, campaign_b: Path) -> list[tuple[Path, str | None]]:
    return [(campaign_a, "Run A"), (campaign_b, "Run B")]


# ======================================================================
# CampaignRunData
# ======================================================================


class TestCampaignRunData:
    def test_basic_properties(self) -> None:
        df = pd.DataFrame({"eui": [100.0, 110.0]})
        data = CampaignRunData(outdir=Path("/tmp/x"), label="test", df=df, n_samples=2)
        assert data.label == "test"
        assert data.n_samples == 2
        assert data.n_rows == 2

    def test_n_rows_empty(self) -> None:
        data = CampaignRunData(outdir=Path("/tmp/x"), label="empty", df=pd.DataFrame())
        assert data.n_rows == 0


# ======================================================================
# CrossRunStats
# ======================================================================


class TestCrossRunStats:
    def test_with_values(self) -> None:
        s = CrossRunStats(kpi="eui", values={"A": 100.0, "B": 80.0})
        assert s.overall_mean == 90.0
        assert s.overall_min == 80.0
        assert s.overall_max == 100.0
        assert s.best_campaign == "B"
        assert s.worst_campaign == "A"
        assert s.overall_std is not None

    def test_single_value(self) -> None:
        s = CrossRunStats(kpi="eui", values={"A": 100.0})
        assert s.overall_mean == 100.0
        assert s.overall_std is None  # only one value → no std
        assert s.best_campaign == "A"

    def test_empty_values(self) -> None:
        s = CrossRunStats(kpi="eui", values={})
        assert s.overall_mean is None
        assert s.best_campaign is None

    def test_none_values_excluded(self) -> None:
        s = CrossRunStats(kpi="eui", values={"A": 100.0, "B": None})
        assert s.overall_mean == 100.0
        assert s.best_campaign == "A"


# ======================================================================
# CrossRunAggregator — Campaign management
# ======================================================================


class TestCampaignManagement:
    def test_add_campaign(self) -> None:
        agg = CrossRunAggregator()
        agg.add_campaign(Path("/tmp/a"), "A")
        assert len(agg._campaigns) == 1

    def test_add_campaign_invalidates_cache(
        self, two_campaigns: list[tuple[Path, str | None]]
    ) -> None:
        agg = CrossRunAggregator(two_campaigns)
        agg.load()
        agg.aggregate()
        assert agg._combined_df is not None
        agg.add_campaign(Path("/tmp/c"), "C")
        assert agg._combined_df is None

    def test_remove_campaign_found(self, two_campaigns: list[tuple[Path, str | None]]) -> None:
        agg = CrossRunAggregator(two_campaigns)
        assert agg.remove_campaign("Run A") is True
        assert len(agg._campaigns) == 1

    def test_remove_campaign_not_found(self) -> None:
        agg = CrossRunAggregator()
        assert agg.remove_campaign("nonexistent") is False

    def test_init_with_campaigns(self, two_campaigns: list[tuple[Path, str | None]]) -> None:
        agg = CrossRunAggregator(two_campaigns)
        assert len(agg._campaigns) == 2


# ======================================================================
# CrossRunAggregator — Loading
# ======================================================================


class TestLoading:
    def test_load_two_campaigns(self, two_campaigns: list[tuple[Path, str | None]]) -> None:
        agg = CrossRunAggregator(two_campaigns)
        runs = agg.load()
        assert len(runs) == 2
        assert "Run A" in runs
        assert "Run B" in runs
        assert runs["Run A"].n_samples == 3
        assert runs["Run B"].n_samples == 2

    def test_load_missing_csv_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        agg = CrossRunAggregator([(tmp_path / "empty", "Empty")])
        runs = agg.load()
        assert len(runs) == 0

    def test_resolve_label_from_run_json(self, campaign_a: Path) -> None:
        agg = CrossRunAggregator()
        label = agg._resolve_label(campaign_a)
        assert label == "campaign-alpha"

    def test_resolve_label_fallback_to_stem(self, campaign_b: Path) -> None:
        agg = CrossRunAggregator()
        label = agg._resolve_label(campaign_b)
        assert label == "run_b"

    def test_load_csv_missing_sample_id(self, tmp_path: Path) -> None:
        outdir = tmp_path / "campaign"
        outdir.mkdir()
        pd.DataFrame({"eui": [1.0, 2.0]}).to_csv(outdir / "aggregated_results.csv", index=False)
        agg = CrossRunAggregator()
        df = agg._load_csv(outdir)
        assert df is not None
        assert "sample_id" in df.columns

    def test_load_csv_unnamed_index(self, tmp_path: Path) -> None:
        outdir = tmp_path / "campaign"
        outdir.mkdir()
        pd.DataFrame({"eui": [1.0, 2.0]}).to_csv(
            outdir / "aggregated_results.csv", index_label="Unnamed: 0"
        )
        agg = CrossRunAggregator()
        df = agg._load_csv(outdir)
        assert df is not None
        assert "sample_id" in df.columns

    def test_load_csv_returns_none_for_missing(self, tmp_path: Path) -> None:
        agg = CrossRunAggregator()
        assert agg._load_csv(tmp_path / "nonexistent") is None


# ======================================================================
# CrossRunAggregator — Aggregation
# ======================================================================


class TestAggregation:
    def test_aggregate_adds_campaign_column(
        self, two_campaigns: list[tuple[Path, str | None]]
    ) -> None:
        agg = CrossRunAggregator(two_campaigns)
        df = agg.aggregate()
        assert "campaign" in df.columns
        assert set(df["campaign"]) == {"Run A", "Run B"}

    def test_aggregate_adds_global_sample_id(
        self, two_campaigns: list[tuple[Path, str | None]]
    ) -> None:
        agg = CrossRunAggregator(two_campaigns)
        df = agg.aggregate()
        assert "global_sample_id" in df.columns
        assert "Run A_1" in df["global_sample_id"].values

    def test_aggregate_total_rows(self, two_campaigns: list[tuple[Path, str | None]]) -> None:
        agg = CrossRunAggregator(two_campaigns)
        df = agg.aggregate()
        assert len(df) == 5  # 3 + 2

    def test_aggregate_empty_returns_empty_df(self) -> None:
        agg = CrossRunAggregator()
        df = agg.aggregate()
        assert df.empty

    def test_get_combined_dataframe_cached(
        self, two_campaigns: list[tuple[Path, str | None]]
    ) -> None:
        agg = CrossRunAggregator(two_campaigns)
        df1 = agg.get_combined_dataframe()
        df2 = agg.get_combined_dataframe()
        assert df1 is df2  # same cached object


# ======================================================================
# CrossRunAggregator — Cross-run statistics
# ======================================================================


class TestCrossRunStatsComputation:
    def test_compute_stats_has_kpis(self, two_campaigns: list[tuple[Path, str | None]]) -> None:
        agg = CrossRunAggregator(two_campaigns)
        stats = agg.compute_cross_run_stats()
        assert "eui" in stats
        assert "cost" in stats

    def test_eui_stats_values(self, two_campaigns: list[tuple[Path, str | None]]) -> None:
        agg = CrossRunAggregator(two_campaigns)
        stats = agg.compute_cross_run_stats()
        eui = stats["eui"]
        # Run A mean = (100+110+95)/3 = 101.67; Run B mean = (80+85)/2 = 82.5
        assert abs(eui.values["Run A"] - 101.667) < 0.1
        assert abs(eui.values["Run B"] - 82.5) < 0.1
        assert eui.best_campaign == "Run B"  # lower EUI is better

    def test_compute_stats_empty(self) -> None:
        agg = CrossRunAggregator()
        assert agg.compute_cross_run_stats() == {}

    def test_get_cross_run_stats_cached(self, two_campaigns: list[tuple[Path, str | None]]) -> None:
        agg = CrossRunAggregator(two_campaigns)
        s1 = agg.get_cross_run_stats()
        s2 = agg.get_cross_run_stats()
        assert s1 is s2


# ======================================================================
# CrossRunAggregator — Rankings
# ======================================================================


class TestRankings:
    @pytest.fixture
    def agg_with_stats(self, two_campaigns: list[tuple[Path, str | None]]) -> CrossRunAggregator:
        a = CrossRunAggregator(two_campaigns)
        a.compute_cross_run_stats()
        return a

    def test_kpi_rankings(self, agg_with_stats: CrossRunAggregator) -> None:
        rankings = agg_with_stats.get_kpi_rankings("eui")
        assert len(rankings) == 2
        assert rankings[0][0] == "Run B"  # lowest EUI first

    def test_kpi_rankings_unknown_kpi(self, agg_with_stats: CrossRunAggregator) -> None:
        assert agg_with_stats.get_kpi_rankings("nonexistent") == []

    def test_best_campaigns_ascending(self, agg_with_stats: CrossRunAggregator) -> None:
        best = agg_with_stats.get_best_campaigns("eui", ascending=True)
        assert best[0][0] == "Run B"

    def test_best_campaigns_descending(self, agg_with_stats: CrossRunAggregator) -> None:
        best = agg_with_stats.get_best_campaigns("eui", ascending=False)
        assert best[0][0] == "Run A"


# ======================================================================
# CrossRunAggregator — Persistence
# ======================================================================


class TestExport:
    def test_export_combined_csv(
        self,
        two_campaigns: list[tuple[Path, str | None]],
        tmp_path: Path,
    ) -> None:
        agg = CrossRunAggregator(two_campaigns)
        out = tmp_path / "output" / "combined.csv"
        agg.export_combined_csv(out)
        assert out.is_file()
        df = pd.read_csv(out)
        assert len(df) == 5
        assert "campaign" in df.columns


# ======================================================================
# CrossRunAggregator — Summary
# ======================================================================


class TestSummary:
    def test_summary_structure(self, two_campaigns: list[tuple[Path, str | None]]) -> None:
        agg = CrossRunAggregator(two_campaigns)
        s = agg.summary()
        assert s["n_campaigns"] == 2
        assert s["total_samples"] == 5
        assert s["combined_rows"] == 5
        assert "eui" in s["kpis"]
        assert "eui" in s["kpi_summary"]

    def test_summary_kpi_best(self, two_campaigns: list[tuple[Path, str | None]]) -> None:
        agg = CrossRunAggregator(two_campaigns)
        s = agg.summary()
        assert s["kpi_summary"]["eui"]["best_campaign"] == "Run B"
