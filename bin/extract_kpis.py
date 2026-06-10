#!/usr/bin/env python3
"""extract_kpis.py — Parse one sample's simulation outputs into a KPI JSON.

Extracts Key Performance Indicators from the EnergyPlus SQLite output
(``eplusout.sql``).  See docs/OSimFlow.md §4.2 (PROCESS_EXTRACT_KPIS)
for the contract and the PRD issue #52 for the full requirements.

KPIs extracted
--------------
1. **EUI** (Energy Use Intensity) — total site energy / building area
   in both kWh/m²/yr and kBtu/ft²/yr.
2. **End-use breakdown** — heating, cooling, interior lighting,
   interior equipment, fans, pumps, water systems (kWh/m²/yr).
3. **Peak demand** — total electricity demand (W/m²).
4. **Unmet hours** — heating and cooling setpoint-not-met hours.
5. **Simulation summary** — number of warnings / severe errors from
   ``eplusout.err``.

The EnergyPlus SQL schema stores tabular data in
``TabularDataWithStrings``.  The queries below target the canonical
table names produced by EnergyPlus 9.x–24.x.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("extract_kpis")

# ---------------------------------------------------------------------------
# Unit conversion constants
# ---------------------------------------------------------------------------
_MJ_TO_KWH = 1.0 / 3.6
_MJ_M2_TO_KBTU_FT2 = 0.0888  # 1 MJ/m² ≈ 0.0888 kBtu/ft²


# ---------------------------------------------------------------------------
# Low-level query helpers
# ---------------------------------------------------------------------------


def _fetch_scalar(
    cur: sqlite3.Cursor,
    table_name: str,
    row_name: str,
    column_name: str,
    report_name: str = "AnnualBuildingUtilityPerformanceSummary",
    report_for_string: str = "Entire Facility",
) -> str | None:
    """Return the ``Value`` cell matching the given keys, or ``None``."""
    cur.execute(
        """
        SELECT Value
        FROM TabularDataWithStrings
        WHERE ReportName = ?
          AND ReportForString = ?
          AND TableName = ?
          AND RowName = ?
          AND ColumnName = ?
        """,
        (report_name, report_for_string, table_name, row_name, column_name),
    )
    row = cur.fetchone()
    return row[0] if row is not None else None


def _fetch_floor_area(cur: sqlite3.Cursor) -> float | None:
    """Return total conditioned floor area in m² from the ``Zones`` table."""
    try:
        cur.execute("SELECT SUM(Floor_Area) FROM Zones")
        row = cur.fetchone()
        if row is not None and row[0] is not None:
            return float(row[0])
    except sqlite3.OperationalError:
        pass

    # Fallback: try TabularDataWithStrings
    val = _fetch_scalar(
        cur,
        table_name="Building Area",
        row_name="Total Area",
        column_name="Area",
        report_name="InputVerificationandResultsSummary",
    )
    if val is not None:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass

    return None


def _safe_float(value: str | None) -> float | None:
    """Parse *value* to float, returning ``None`` on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# KPI extraction functions
# ---------------------------------------------------------------------------


def _extract_eui(cur: sqlite3.Cursor, floor_area_m2: float | None) -> dict[str, Any]:
    """Extract Energy Use Intensity (EUI) from the Site and Source Energy table."""
    kpis: dict[str, Any] = {}

    site_energy_mj_per_m2 = _safe_float(
        _fetch_scalar(
            cur,
            table_name="Site and Source Energy",
            row_name="Total Site Energy",
            column_name="Energy Per Total Building Area",
        )
    )

    if site_energy_mj_per_m2 is not None:
        kpis["eui_kwh_per_m2"] = round(site_energy_mj_per_m2 * _MJ_TO_KWH, 3)
        kpis["eui_kbtu_per_ft2"] = round(site_energy_mj_per_m2 * _MJ_M2_TO_KBTU_FT2, 3)

    total_site_energy_mj = _safe_float(
        _fetch_scalar(
            cur,
            table_name="Site and Source Energy",
            row_name="Total Site Energy",
            column_name="Total Energy",
        )
    )
    if total_site_energy_mj is not None:
        kpis["total_site_energy_kwh"] = round(total_site_energy_mj * _MJ_TO_KWH, 3)

    net_site_energy_mj = _safe_float(
        _fetch_scalar(
            cur,
            table_name="Site and Source Energy",
            row_name="Net Site Energy",
            column_name="Energy Per Total Building Area",
        )
    )
    if net_site_energy_mj is not None:
        kpis["net_eui_kwh_per_m2"] = round(net_site_energy_mj * _MJ_TO_KWH, 3)

    return kpis


# Canonical end-use row names and the output key prefix.
_END_USE_ROWS: list[tuple[str, str]] = [
    ("Heating", "heating"),
    ("Cooling", "cooling"),
    ("Interior Lighting", "interior_lighting"),
    ("Exterior Lighting", "exterior_lighting"),
    ("Interior Equipment", "interior_equipment"),
    ("Exterior Equipment", "exterior_equipment"),
    ("Fans", "fans"),
    ("Pumps", "pumps"),
    ("Water Systems", "water_systems"),
]

# Fuel types (columns) to extract per end use.
_END_USE_FUELS: list[tuple[str, str]] = [
    ("Electricity", "electricity"),
    ("Natural Gas", "natural_gas"),
    ("Additional Fuel", "additional_fuel"),
    ("District Heating", "district_heating"),
    ("District Cooling", "district_cooling"),
]


def _extract_end_uses(cur: sqlite3.Cursor) -> dict[str, Any]:
    """Extract end-use energy breakdown from the ``End Uses`` table.

    EnergyPlus reports end uses in GJ.  We convert to kWh for the output.
    """
    end_uses: dict[str, Any] = {}
    gj_to_kwh = 1.0 / 3.6e-3  # 1 GJ = 277.778 kWh

    for row_name, prefix in _END_USE_ROWS:
        for col_name, fuel_suffix in _END_USE_FUELS:
            val_str = _fetch_scalar(
                cur,
                table_name="End Uses",
                row_name=row_name,
                column_name=col_name,
            )
            val = _safe_float(val_str)
            if val is not None and val != 0.0:
                key = f"{prefix}_{fuel_suffix}_kwh"
                end_uses[key] = round(val * gj_to_kwh, 3)

    # Total end-use electricity
    total_elec = _safe_float(
        _fetch_scalar(
            cur,
            table_name="End Uses",
            row_name="Total End Uses",
            column_name="Electricity",
        )
    )
    if total_elec is not None:
        end_uses["total_electricity_kwh"] = round(total_elec * gj_to_kwh, 3)

    return end_uses


def _extract_peak_demand(
    cur: sqlite3.Cursor,
    floor_area_m2: float | None,
) -> dict[str, Any]:
    """Extract peak demand from ``Demand End Use Components Summary``."""
    kpis: dict[str, Any] = {}

    # Try the "Demand End Use Components Summary" table.
    # EnergyPlus reports peak demand in Watts.
    total_demand_w = _safe_float(
        _fetch_scalar(
            cur,
            table_name="Demand End Use Components Summary",
            row_name="Total End Uses",
            column_name="Electricity",
        )
    )
    if total_demand_w is not None:
        kpis["peak_demand_w"] = round(total_demand_w, 3)
        if floor_area_m2 and floor_area_m2 > 0:
            kpis["peak_demand_w_per_m2"] = round(total_demand_w / floor_area_m2, 3)
        kpis["peak_demand_kw"] = round(total_demand_w / 1000.0, 3)

    return kpis


def _extract_unmet_hours(cur: sqlite3.Cursor) -> dict[str, Any]:
    """Extract unmet heating/cooling hours from ``Comfort and Setpoint Not Met Summary``."""
    kpis: dict[str, Any] = {}

    heating_hours = _safe_float(
        _fetch_scalar(
            cur,
            table_name="Comfort and Setpoint Not Met Summary",
            row_name="Facility",
            column_name="During Heating",
        )
    )
    if heating_hours is not None:
        kpis["unmet_hours_heating"] = round(heating_hours, 1)

    cooling_hours = _safe_float(
        _fetch_scalar(
            cur,
            table_name="Comfort and Setpoint Not Met Summary",
            row_name="Facility",
            column_name="During Cooling",
        )
    )
    if cooling_hours is not None:
        kpis["unmet_hours_cooling"] = round(cooling_hours, 1)

    # Try "During Occupied Heating" / "During Occupied Cooling" as well
    occ_heating = _safe_float(
        _fetch_scalar(
            cur,
            table_name="Comfort and Setpoint Not Met Summary",
            row_name="Facility",
            column_name="During Occupied Heating",
        )
    )
    if occ_heating is not None:
        kpis["unmet_hours_heating_occupied"] = round(occ_heating, 1)

    occ_cooling = _safe_float(
        _fetch_scalar(
            cur,
            table_name="Comfort and Setpoint Not Met Summary",
            row_name="Facility",
            column_name="During Occupied Cooling",
        )
    )
    if occ_cooling is not None:
        kpis["unmet_hours_cooling_occupied"] = round(occ_cooling, 1)

    return kpis


def _extract_simulation_summary(simulation_dir: Path) -> dict[str, Any]:
    """Parse ``eplusout.err`` for warning / error counts."""
    summary: dict[str, Any] = {}
    err_path = simulation_dir / "eplusout.err"
    if not err_path.exists():
        return summary

    warnings = 0
    severe = 0
    try:
        text = err_path.read_text(errors="replace")
        warnings = len(re.findall(r"^\s*\*\*\s*Warning", text, re.MULTILINE))
        severe = len(re.findall(r"^\s*\*\*\s*Severe", text, re.MULTILINE))
        # EnergyPlus also uses "  * Severe" (two spaces + asterisk)
        severe += len(re.findall(r"^\s{2}\*\s+Severe", text, re.MULTILINE))
    except OSError as exc:
        log.warning("Could not read eplusout.err: %s", exc)

    summary["n_warnings"] = warnings
    summary["n_severe_errors"] = severe
    return summary


# ---------------------------------------------------------------------------
# Main extraction entry point
# ---------------------------------------------------------------------------


def extract_kpis_from_sql(sql_path: Path) -> dict[str, Any]:
    """Connect to *sql_path* and extract all KPIs.

    Returns a flat dict suitable for JSON serialisation.  Missing or
    unreadable data yields an empty subset of keys rather than an
    exception.
    """
    if not sql_path.exists():
        log.warning("eplusout.sql not found at %s", sql_path)
        return {"error": "eplusout.sql_missing"}

    kpis: dict[str, Any] = {}

    try:
        conn = sqlite3.connect(str(sql_path))
        cur = conn.cursor()

        # Verify the database has the expected table structure
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='TabularDataWithStrings'"
        )
        if cur.fetchone() is None:
            log.warning("TabularDataWithStrings table missing in %s", sql_path)
            return {"error": "missing_TabularDataWithStrings"}

        floor_area = _fetch_floor_area(cur)
        if floor_area is not None:
            kpis["floor_area_m2"] = round(floor_area, 3)

        kpis.update(_extract_eui(cur, floor_area))
        end_uses = _extract_end_uses(cur)
        if end_uses:
            kpis["end_uses"] = end_uses
        kpis.update(_extract_peak_demand(cur, floor_area))
        kpis.update(_extract_unmet_hours(cur))

        conn.close()
    except sqlite3.DatabaseError as exc:
        log.error("Corrupt eplusout.sql at %s: %s", sql_path, exc)
        return {"error": "corrupt_database", "raw_error": str(exc)}
    except Exception as exc:
        log.error("Unexpected error reading %s: %s", sql_path, exc, exc_info=True)
        return {"error": "extraction_failed", "raw_error": str(exc)}

    return kpis


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulation_dir", required=True, type=Path)
    parser.add_argument("--sample_id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--custom_kpi_extractor", type=Path, default=None)
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    sql_path = args.simulation_dir / "eplusout.sql"

    kpis: dict[str, Any] = {}

    if args.custom_kpi_extractor:
        if not args.custom_kpi_extractor.exists():
            log.error("Custom KPI extractor %s does not exist.", args.custom_kpi_extractor)
            return 1
        spec = importlib.util.spec_from_file_location("custom_extractor", args.custom_kpi_extractor)
        if spec is None or spec.loader is None:
            log.error("Failed to load custom extractor from %s.", args.custom_kpi_extractor)
            return 1

        custom_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(custom_mod)

        if not hasattr(custom_mod, "extract_kpis"):
            log.error(
                "Custom extractor %s must define `extract_kpis(ctx)`.", args.custom_kpi_extractor
            )
            return 1

        ctx = {
            "simulation_dir": args.simulation_dir,
            "sample_id": args.sample_id,
        }

        try:
            custom_kpis = custom_mod.extract_kpis(ctx)
            if isinstance(custom_kpis, dict):
                kpis.update(custom_kpis)
            else:
                log.error("Custom extractor returned %s, expected dict.", type(custom_kpis))
                return 1
        except Exception as e:
            log.error("Custom extractor failed: %s", e, exc_info=True)
            return 1
    else:
        kpis = extract_kpis_from_sql(sql_path)
        kpis.update(_extract_simulation_summary(args.simulation_dir))

    output = {
        "sample_id": args.sample_id,
        "openstudio_version": None,
        "kpis": kpis,
    }

    args.out.write_text(json.dumps(output, indent=2))
    log.info("KPI extraction complete for sample=%s", args.sample_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
