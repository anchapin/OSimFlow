"""Unit tests for bin/extract_kpis.py — KPI extraction from eplusout.sql.

Uses an in-memory SQLite database populated with synthetic
TabularDataWithStrings rows that mimic the EnergyPlus schema.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# Module under test — import the bin script's functions
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bin"))
import extract_kpis as ek

# ---------------------------------------------------------------------------
# Fixtures: synthetic eplusout.sql databases
# ---------------------------------------------------------------------------


def _make_eplusout_sql(
    path: Path,
    *,
    site_energy_mj_per_m2: float = 433.8,
    total_site_energy_mj: float = 43380.0,
    net_site_energy_mj_per_m2: float = 400.0,
    floor_area_m2: float = 100.0,
    end_uses: dict[tuple[str, str], float] | None = None,
    peak_demand_w: float | None = 45000.0,
    unmet_heating: float | None = 0.0,
    unmet_cooling: float | None = 12.0,
    include_zones: bool = True,
) -> Path:
    """Create a minimal ``eplusout.sql`` at *path* for testing."""
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE TabularDataWithStrings (
            ReportName TEXT,
            ReportForString TEXT,
            TableName TEXT,
            RowName TEXT,
            ColumnName TEXT,
            Units TEXT,
            Value TEXT
        )
    """)

    def _insert(
        report: str,
        table: str,
        row: str,
        col: str,
        value: str,
        units: str = "",
        report_for: str = "Entire Facility",
    ) -> None:
        cur.execute(
            "INSERT INTO TabularDataWithStrings VALUES (?,?,?,?,?,?,?)",
            (report, report_for, table, row, col, units, value),
        )

    # EUI
    _insert(
        "AnnualBuildingUtilityPerformanceSummary",
        "Site and Source Energy",
        "Total Site Energy",
        "Energy Per Total Building Area",
        str(site_energy_mj_per_m2),
        "MJ/m2",
    )
    _insert(
        "AnnualBuildingUtilityPerformanceSummary",
        "Site and Source Energy",
        "Total Site Energy",
        "Total Energy",
        str(total_site_energy_mj),
        "MJ",
    )
    _insert(
        "AnnualBuildingUtilityPerformanceSummary",
        "Site and Source Energy",
        "Net Site Energy",
        "Energy Per Total Building Area",
        str(net_site_energy_mj_per_m2),
        "MJ/m2",
    )

    # Floor area via Zones table
    if include_zones:
        cur.execute("CREATE TABLE Zones (ZoneIndex INTEGER, Floor_Area REAL)")
        cur.execute("INSERT INTO Zones VALUES (0, ?)", (floor_area_m2,))

    # End uses (EnergyPlus reports in GJ)
    if end_uses:
        for (row_name, col_name), val in end_uses.items():
            _insert(
                "AnnualBuildingUtilityPerformanceSummary",
                "End Uses",
                row_name,
                col_name,
                str(val),
                "GJ",
            )
        # Total End Uses
        total_elec = sum(v for (r, c), v in end_uses.items() if c == "Electricity")
        _insert(
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Total End Uses",
            "Electricity",
            str(total_elec),
            "GJ",
        )

    # Peak demand
    if peak_demand_w is not None:
        _insert(
            "AnnualBuildingUtilityPerformanceSummary",
            "Demand End Use Components Summary",
            "Total End Uses",
            "Electricity",
            str(peak_demand_w),
            "W",
        )

    # Unmet hours
    if unmet_heating is not None:
        _insert(
            "AnnualBuildingUtilityPerformanceSummary",
            "Comfort and Setpoint Not Met Summary",
            "Facility",
            "During Heating",
            str(unmet_heating),
            "hr",
        )
    if unmet_cooling is not None:
        _insert(
            "AnnualBuildingUtilityPerformanceSummary",
            "Comfort and Setpoint Not Met Summary",
            "Facility",
            "During Cooling",
            str(unmet_cooling),
            "hr",
        )

    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def full_sql(tmp_path: Path) -> Path:
    """A synthetic eplusout.sql with all KPI categories populated."""
    return _make_eplusout_sql(
        tmp_path / "eplusout.sql",
        site_energy_mj_per_m2=433.8,
        floor_area_m2=100.0,
        end_uses={
            ("Heating", "Electricity"): 0.018,
            ("Cooling", "Electricity"): 0.0432,
            ("Interior Lighting", "Electricity"): 0.0288,
            ("Interior Equipment", "Electricity"): 0.0360,
            ("Fans", "Electricity"): 0.0144,
        },
        peak_demand_w=45000.0,
        unmet_heating=0.0,
        unmet_cooling=12.0,
    )


@pytest.fixture()
def minimal_sql(tmp_path: Path) -> Path:
    """An eplusout.sql with only the EUI row — no end uses, no peak demand."""
    return _make_eplusout_sql(
        tmp_path / "eplusout.sql",
        site_energy_mj_per_m2=200.0,
        end_uses=None,
        peak_demand_w=None,
        unmet_heating=None,
        unmet_cooling=None,
    )


# ---------------------------------------------------------------------------
# Tests: extract_kpis_from_sql
# ---------------------------------------------------------------------------


class TestExtractKpisFromSql:
    def test_full_extraction(self, full_sql: Path) -> None:
        result = ek.extract_kpis_from_sql(full_sql)

        # EUI
        assert "eui_kwh_m2_yr" in result
        assert result["eui_kwh_m2_yr"] == pytest.approx(433.8 / 3.6, abs=0.01)
        assert "eui_kbtu_per_ft2" in result

        # Floor area
        assert result["floor_area_m2"] == 100.0

        # End uses
        assert "end_uses" in result
        eu = result["end_uses"]
        assert "cooling_electricity_kwh" in eu
        assert eu["cooling_electricity_kwh"] > 0
        assert "interior_lighting_electricity_kwh" in eu
        assert "total_electricity_kwh" in eu

        # Peak demand
        assert result["peak_demand_kw"] == pytest.approx(45.0, abs=0.01)
        assert result["peak_demand_w_per_m2"] == pytest.approx(450.0, abs=0.1)

        # Unmet hours
        assert result["unmet_hours_heating"] == 0.0
        assert result["unmet_hours_cooling"] == 12.0

    def test_minimal_extraction(self, minimal_sql: Path) -> None:
        result = ek.extract_kpis_from_sql(minimal_sql)
        assert result["eui_kwh_m2_yr"] == pytest.approx(200.0 / 3.6, abs=0.01)
        assert "end_uses" not in result
        assert "peak_demand_kw" not in result

    def test_missing_sql_file(self, tmp_path: Path) -> None:
        result = ek.extract_kpis_from_sql(tmp_path / "nonexistent.sql")
        assert result.get("error") == "eplusout.sql_missing"

    def test_missing_tabular_data(self, tmp_path: Path) -> None:
        sql_path = tmp_path / "eplusout.sql"
        conn = sqlite3.connect(str(sql_path))
        conn.execute("CREATE TABLE dummy (id INTEGER)")
        conn.commit()
        conn.close()

        result = ek.extract_kpis_from_sql(sql_path)
        assert result.get("error") == "missing_TabularDataWithStrings"

    def test_corrupt_database(self, tmp_path: Path) -> None:
        sql_path = tmp_path / "eplusout.sql"
        sql_path.write_text("this is not a sqlite database")

        result = ek.extract_kpis_from_sql(sql_path)
        assert "error" in result

    def test_no_floor_area(self, tmp_path: Path) -> None:
        sql_path = _make_eplusout_sql(
            tmp_path / "eplusout.sql",
            include_zones=False,
            peak_demand_w=45000.0,
        )
        result = ek.extract_kpis_from_sql(sql_path)
        assert "floor_area_m2" not in result
        # peak_demand_w should be present but not per-m2
        assert "peak_demand_w" in result
        assert "peak_demand_w_per_m2" not in result


class TestExtractEui:
    def test_eui_conversion(self, tmp_path: Path) -> None:
        sql_path = _make_eplusout_sql(tmp_path / "eplusout.sql", site_energy_mj_per_m2=360.0)
        conn = sqlite3.connect(str(sql_path))
        cur = conn.cursor()
        result = ek._extract_eui(cur, 100.0)
        conn.close()

        assert result["eui_kwh_m2_yr"] == pytest.approx(100.0, abs=0.01)
        assert result["total_site_energy_kwh"] == pytest.approx(43380.0 / 3.6, abs=1.0)
        assert "net_eui_kwh_m2_yr" in result


class TestExtractEndUses:
    def test_end_use_breakdown(self, tmp_path: Path) -> None:
        sql_path = _make_eplusout_sql(
            tmp_path / "eplusout.sql",
            end_uses={
                ("Heating", "Electricity"): 0.018,
                ("Cooling", "Electricity"): 0.0432,
                ("Interior Lighting", "Electricity"): 0.0288,
                ("Heating", "Natural Gas"): 0.050,
            },
        )
        conn = sqlite3.connect(str(sql_path))
        cur = conn.cursor()
        result = ek._extract_end_uses(cur)
        conn.close()

        gj_to_kwh = 1.0 / 3.6e-3
        assert "heating_electricity_kwh" in result
        assert result["heating_electricity_kwh"] == pytest.approx(0.018 * gj_to_kwh, abs=0.1)
        assert "cooling_electricity_kwh" in result
        assert "heating_natural_gas_kwh" in result
        assert "total_electricity_kwh" in result

    def test_empty_end_uses(self, tmp_path: Path) -> None:
        sql_path = _make_eplusout_sql(tmp_path / "eplusout.sql", end_uses=None)
        conn = sqlite3.connect(str(sql_path))
        cur = conn.cursor()
        result = ek._extract_end_uses(cur)
        conn.close()

        assert result == {}


class TestExtractPeakDemand:
    def test_peak_demand_with_area(self, tmp_path: Path) -> None:
        sql_path = _make_eplusout_sql(
            tmp_path / "eplusout.sql",
            floor_area_m2=200.0,
            peak_demand_w=60000.0,
        )
        conn = sqlite3.connect(str(sql_path))
        cur = conn.cursor()
        result = ek._extract_peak_demand(cur, 200.0)
        conn.close()

        assert result["peak_demand_kw"] == pytest.approx(60.0, abs=0.01)
        assert result["peak_demand_w_per_m2"] == pytest.approx(300.0, abs=0.1)


class TestExtractUnmetHours:
    def test_unmet_hours(self, tmp_path: Path) -> None:
        sql_path = _make_eplusout_sql(
            tmp_path / "eplusout.sql",
            unmet_heating=5.0,
            unmet_cooling=20.0,
        )
        conn = sqlite3.connect(str(sql_path))
        cur = conn.cursor()
        result = ek._extract_unmet_hours(cur)
        conn.close()

        assert result["unmet_hours_heating"] == 5.0
        assert result["unmet_hours_cooling"] == 20.0

    def test_no_unmet_hours_table(self, tmp_path: Path) -> None:
        sql_path = _make_eplusout_sql(
            tmp_path / "eplusout.sql",
            unmet_heating=None,
            unmet_cooling=None,
        )
        conn = sqlite3.connect(str(sql_path))
        cur = conn.cursor()
        result = ek._extract_unmet_hours(cur)
        conn.close()

        assert "unmet_hours_heating" not in result
        assert "unmet_hours_cooling" not in result


class TestSimulationSummary:
    def test_warning_and_error_count(self, tmp_path: Path) -> None:
        err_path = tmp_path / "eplusout.err"
        err_path.write_text(
            "** Warning: Some mild issue\n"
            "** Warning: Another issue\n"
            "** Severe: Something bad happened\n"
        )
        result = ek._extract_simulation_summary(tmp_path)
        assert result["n_warnings"] == 2
        assert result["n_severe_errors"] == 1

    def test_no_err_file(self, tmp_path: Path) -> None:
        result = ek._extract_simulation_summary(tmp_path)
        assert result == {}


class TestValidateKpis:
    """Tests for validate_kpis() — simulation convergence and quality checks."""

    def _normal_kpis(self) -> dict:
        return {
            "eui_kwh_m2_yr": 120.0,
            "eui_kbtu_per_ft2": 38.0,
            "total_site_energy_kwh": 12000.0,
            "net_eui_kwh_m2_yr": 110.0,
            "floor_area_m2": 100.0,
            "end_uses": {
                "heating_electricity_kwh": 3000.0,
                "cooling_electricity_kwh": 4000.0,
                "interior_lighting_electricity_kwh": 2500.0,
                "interior_equipment_electricity_kwh": 2500.0,
                "total_electricity_kwh": 12000.0,
            },
            "unmet_hours_heating": 50.0,
            "unmet_hours_cooling": 80.0,
        }

    def test_valid_kpis(self) -> None:
        result = ek.validate_kpis(self._normal_kpis())
        assert result["valid"] is True
        assert result["failures"] == []
        assert result["warnings"] == []

    def test_eui_below_minimum(self) -> None:
        kpis = self._normal_kpis()
        kpis["eui_kwh_m2_yr"] = 5.0
        result = ek.validate_kpis(kpis)
        assert result["valid"] is False
        assert any("below minimum" in f for f in result["failures"])

    def test_eui_above_maximum(self) -> None:
        kpis = self._normal_kpis()
        kpis["eui_kwh_m2_yr"] = 1500.0
        result = ek.validate_kpis(kpis)
        assert result["valid"] is False
        assert any("exceeds maximum" in f for f in result["failures"])

    def test_zero_energy_total(self) -> None:
        kpis = self._normal_kpis()
        kpis["total_site_energy_kwh"] = 0.0
        result = ek.validate_kpis(kpis)
        assert result["valid"] is False
        assert any("zero or near-zero" in f for f in result["failures"])

    def test_zero_energy_end_uses(self) -> None:
        kpis = self._normal_kpis()
        kpis["end_uses"] = {
            "heating_electricity_kwh": 0.0,
            "cooling_electricity_kwh": 0.0,
            "total_electricity_kwh": 0.0,
        }
        result = ek.validate_kpis(kpis)
        assert result["valid"] is False
        assert any("All end-use" in f for f in result["failures"])

    def test_extreme_value_detection(self) -> None:
        kpis = self._normal_kpis()
        kpis["end_uses"]["heating_electricity_kwh"] = 50000.0
        result = ek.validate_kpis(kpis)
        assert any("exceeds" in w and "median" in w for w in result["warnings"])

    def test_unmet_hours_exceed_threshold(self) -> None:
        kpis = self._normal_kpis()
        kpis["unmet_hours_heating"] = 350.0
        result = ek.validate_kpis(kpis)
        assert any("heating hours" in w and "ASHRAE" in w for w in result["warnings"])

    def test_unmet_hours_exceed_8760(self) -> None:
        kpis = self._normal_kpis()
        kpis["unmet_hours_heating"] = 5000.0
        kpis["unmet_hours_cooling"] = 5000.0
        result = ek.validate_kpis(kpis)
        assert result["valid"] is False
        assert any("exceed 8760" in f for f in result["failures"])

    def test_negative_total_energy(self) -> None:
        kpis = self._normal_kpis()
        kpis["total_site_energy_kwh"] = -500.0
        result = ek.validate_kpis(kpis)
        assert any("negative" in w for w in result["warnings"])

    def test_negative_end_use(self) -> None:
        kpis = self._normal_kpis()
        kpis["end_uses"]["cooling_electricity_kwh"] = -100.0
        result = ek.validate_kpis(kpis)
        assert any("negative" in w and "cooling" in w for w in result["warnings"])

    def test_missing_critical_kpis(self) -> None:
        result = ek.validate_kpis({})
        assert result["valid"] is False
        assert any("eui_kwh_m2_yr" in f for f in result["failures"])
        assert any("total_site_energy_kwh" in f for f in result["failures"])

    def test_custom_thresholds_override(self) -> None:
        kpis = self._normal_kpis()
        kpis["eui_kwh_m2_yr"] = 50.0
        result_default = ek.validate_kpis(kpis)
        assert result_default["valid"] is True

        result_strict = ek.validate_kpis(kpis, thresholds={"eui_min_kwh_m2_yr": 100.0})
        assert result_strict["valid"] is False
        assert any("below minimum" in f for f in result_strict["failures"])

    def test_custom_unmet_hours_threshold(self) -> None:
        kpis = self._normal_kpis()
        kpis["unmet_hours_cooling"] = 200.0
        result = ek.validate_kpis(kpis, thresholds={"unmet_hours_max": 150.0})
        assert any("cooling hours" in w for w in result["warnings"])

    def test_none_thresholds_uses_defaults(self) -> None:
        result = ek.validate_kpis(self._normal_kpis(), thresholds=None)
        assert result["valid"] is True

    def test_warnings_do_not_affect_validity(self) -> None:
        kpis = self._normal_kpis()
        kpis["total_site_energy_kwh"] = -10.0
        result = ek.validate_kpis(kpis)
        assert len(result["warnings"]) > 0
        assert result["valid"] is True

    def test_empty_end_uses_no_crash(self) -> None:
        kpis = self._normal_kpis()
        kpis["end_uses"] = {}
        result = ek.validate_kpis(kpis)
        assert result["valid"] is True

    def test_no_end_uses_key_no_crash(self) -> None:
        kpis = self._normal_kpis()
        del kpis["end_uses"]
        result = ek.validate_kpis(kpis)
        assert result["valid"] is True


class TestCliMain:
    def test_cli_writes_json(self, full_sql: Path, tmp_path: Path) -> None:
        sim_dir = full_sql.parent
        out_path = tmp_path / "kpi_test.json"

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent.parent.parent / "bin" / "extract_kpis.py"),
                "--simulation_dir",
                str(sim_dir),
                "--sample_id",
                "test",
                "--out",
                str(out_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        data = json.loads(out_path.read_text())
        assert data["sample_id"] == "test"
        assert "kpis" in data
        kpis = data["kpis"]
        assert "eui_kwh_m2_yr" in kpis
        assert "quality" in data
        assert "valid" in data["quality"]
        assert "warnings" in data["quality"]
        assert "failures" in data["quality"]

    def test_cli_missing_sql(self, tmp_path: Path) -> None:
        sim_dir = tmp_path / "empty_sim"
        sim_dir.mkdir()
        out_path = tmp_path / "kpi_missing.json"

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent.parent.parent / "bin" / "extract_kpis.py"),
                "--simulation_dir",
                str(sim_dir),
                "--sample_id",
                "missing",
                "--out",
                str(out_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(out_path.read_text())
        assert data["kpis"].get("error") == "eplusout.sql_missing"
