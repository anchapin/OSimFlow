"""Tests for ``osimflow.results_db``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osimflow.results_db import ResultsDatabase


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "results.db"


@pytest.fixture
def db(db_path: Path) -> ResultsDatabase:
    return ResultsDatabase(db_path)


class TestResultsDatabase:
    def test_add_campaign(self, db: ResultsDatabase) -> None:
        db.add_campaign("camp_001", n_samples=10, algorithm="lhs", openstudio_version="3.11.0")
        summary = db.get_campaign_summary("camp_001")
        assert len(summary["campaigns"]) == 1
        camp = summary["campaigns"][0]
        assert camp["campaign_id"] == "camp_001"
        assert camp["n_samples"] == 10
        assert camp["algorithm"] == "lhs"
        assert camp["openstudio_version"] == "3.11.0"

    def test_add_result(self, db: ResultsDatabase) -> None:
        db.add_result("0001", "eui", 150.2, "kWh/m²/yr", campaign_id="camp_001")
        db.add_result("0001", "cost", 1200.0, "USD", campaign_id="camp_001")
        db.add_result("0002", "eui", 160.0, "kWh/m²/yr", campaign_id="camp_001")

        rows = db.query_results(campaign_id="camp_001")
        assert len(rows) == 3

        eui_rows = db.query_results(kpi_name="eui", campaign_id="camp_001")
        assert len(eui_rows) == 2
        assert all(r["kpi_name"] == "eui" for r in eui_rows)

    def test_query_results_min_max_value(self, db: ResultsDatabase) -> None:
        db.add_campaign("camp_001", n_samples=5)
        db.add_result("0001", "eui", 100.0, unit=None, campaign_id="camp_001")
        db.add_result("0002", "eui", 200.0, unit=None, campaign_id="camp_001")
        db.add_result("0003", "eui", 150.0, unit=None, campaign_id="camp_001")

        rows = db.query_results(kpi_name="eui", min_value=120.0, max_value=180.0)
        assert len(rows) == 1
        assert rows[0]["sample_id"] == "0003"
        assert rows[0]["kpi_value"] == 150.0

    def test_query_results_no_filter(self, db: ResultsDatabase) -> None:
        db.add_campaign("camp_001", n_samples=3)
        db.add_result("0001", "eui", 100.0, campaign_id="camp_001")
        db.add_result("0001", "cost", 500.0, campaign_id="camp_001")
        db.add_result("0002", "eui", 200.0, campaign_id="camp_001")

        rows = db.query_results()
        assert len(rows) == 3

    def test_get_campaign_summary(self, db: ResultsDatabase) -> None:
        db.add_campaign("camp_001", n_samples=2, algorithm="lhs")
        db.add_result("0001", "eui", 100.0, campaign_id="camp_001")
        db.add_result("0001", "cost", 500.0, campaign_id="camp_001")
        db.add_result("0002", "eui", 200.0, campaign_id="camp_001")

        summary = db.get_campaign_summary("camp_001")
        camps = summary["campaigns"]
        assert len(camps) == 1
        camp = camps[0]
        assert camp["n_results"] == 3
        assert "eui" in camp["kpis"]
        assert camp["kpis"]["eui"]["min"] == 100.0
        assert camp["kpis"]["eui"]["max"] == 200.0
        assert camp["kpis"]["eui"]["mean"] == 150.0

    def test_get_campaign_summary_all_campaigns(self, db: ResultsDatabase) -> None:
        db.add_campaign("camp_001", n_samples=2)
        db.add_campaign("camp_002", n_samples=3)
        db.add_result("0001", "eui", 100.0, campaign_id="camp_001")
        db.add_result("0002", "eui", 200.0, campaign_id="camp_002")

        summary = db.get_campaign_summary()
        camps = summary["campaigns"]
        assert len(camps) == 2
        camp_ids = {c["campaign_id"] for c in camps}
        assert camp_ids == {"camp_001", "camp_002"}

    def test_add_results_from_kpi_file(self, db: ResultsDatabase, tmp_path: Path) -> None:
        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(
            json.dumps(
                {
                    "sample_id": "0001",
                    "kpis": {
                        "eui": 150.2,
                        "total_ghg": 42.5,
                        "cost": 1200.0,
                    },
                }
            )
        )

        count = db.add_results_from_kpi_file(kpi_file, campaign_id="camp_001")
        assert count == 3

        rows = db.query_results(kpi_name="eui", campaign_id="camp_001")
        assert len(rows) == 1
        assert rows[0]["kpi_value"] == 150.2

    def test_add_results_from_kpi_file_missing_file(
        self, db: ResultsDatabase, tmp_path: Path
    ) -> None:
        count = db.add_results_from_kpi_file(tmp_path / "nonexistent.json")
        assert count == 0

    def test_add_results_from_kpi_file_invalid_json(
        self, db: ResultsDatabase, tmp_path: Path
    ) -> None:
        kpi_file = tmp_path / "bad.json"
        kpi_file.write_text("not json")

        count = db.add_results_from_kpi_file(kpi_file)
        assert count == 0

    def test_list_kpi_names(self, db: ResultsDatabase) -> None:
        db.add_campaign("camp_001", n_samples=2)
        db.add_result("0001", "eui", 100.0, campaign_id="camp_001")
        db.add_result("0001", "cost", 500.0, campaign_id="camp_001")
        db.add_result("0002", "eui", 200.0, campaign_id="camp_001")

        names = db.list_kpi_names()
        assert set(names) == {"eui", "cost"}

        names = db.list_kpi_names(campaign_id="camp_001")
        assert set(names) == {"eui", "cost"}

    def test_n_results(self, db: ResultsDatabase) -> None:
        db.add_campaign("camp_001", n_samples=2)
        db.add_result("0001", "eui", 100.0, campaign_id="camp_001")
        db.add_result("0001", "cost", 500.0, campaign_id="camp_001")
        db.add_result("0002", "eui", 200.0, campaign_id="camp_001")

        assert db.n_results() == 3
        assert db.n_results(campaign_id="camp_001") == 3

    def test_generation_filter(self, db: ResultsDatabase) -> None:
        db.add_campaign("camp_001", n_samples=2)
        db.add_result("0001", "eui", 100.0, campaign_id="camp_001", generation=0)
        db.add_result("0001", "eui", 110.0, campaign_id="camp_001", generation=1)

        rows_gen0 = db.query_results(generation=0)
        assert len(rows_gen0) == 1
        assert rows_gen0[0]["kpi_value"] == 100.0

        rows_gen1 = db.query_results(generation=1)
        assert len(rows_gen1) == 1
        assert rows_gen1[0]["kpi_value"] == 110.0

    def test_context_manager(self, db_path: Path) -> None:
        with ResultsDatabase(db_path) as db:
            db.add_campaign("camp_001", n_samples=5)
            db.add_result("0001", "eui", 100.0, campaign_id="camp_001")

        summary = db.get_campaign_summary("camp_001")
        assert len(summary["campaigns"]) == 1

    def test_close_idempotent(self, db: ResultsDatabase) -> None:
        db.add_campaign("camp_001", n_samples=1)
        db.close()
        db.close()
        db.close()

        summary = db.get_campaign_summary("camp_001")
        assert len(summary["campaigns"]) == 1

    def test_replace_on_duplicate(self, db: ResultsDatabase) -> None:
        db.add_campaign("camp_001", n_samples=5)
        db.add_result("0001", "eui", 100.0, campaign_id="camp_001")
        db.add_result("0001", "eui", 150.0, campaign_id="camp_001")

        rows = db.query_results(kpi_name="eui", campaign_id="camp_001")
        assert len(rows) == 1
        assert rows[0]["kpi_value"] == 150.0
