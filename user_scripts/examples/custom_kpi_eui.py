"""BYOS example: extract EUI (kWh/m2/yr) from eplusout.sql.

This is the most common KPI extraction pattern. It queries the
EnergyPlus ``TabularDataWithStrings`` table for the
``Annual Building Utility Performance Summary`` to retrieve site
energy per total building area, then converts from MJ/m2 to kWh/m2.

Usage::

    osimflow run \\
        --executor local \\
        --custom_kpi_extractor user_scripts/examples/custom_kpi_eui.py \\
        --input_variables variables.yml \\
        --template_sim_package ./example_package \\
        --n_samples 10 \\
        --outdir ./results

The BYOS contract requires a function named ``extract_kpis`` with
the signature::

    def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path

The Campaign loader (``osimflow.byos.load_user_function``) discovers
the function by name and passes it directly to the executor.  There
is no separate CLI surface for BYOS scripts -- the Python function
*is* the contract.

See user_scripts/README.md for the full BYOS contract reference.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("custom_kpi_eui")


def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    """Extract EUI from a single sample's simulation output.

    Args:
        simulation_dir: directory containing eplusout.sql and other
            EnergyPlus output files for this sample.
        sample_id: the sample identifier (e.g. ``"0001"``).
        out: output directory where the KPI JSON file should be
            written.

    Returns:
        Path to the written KPI JSON file (must match what the
        Campaign expects so aggregation can proceed).
    """
    out.mkdir(parents=True, exist_ok=True)
    kpi_path = out / f"kpi_{sample_id}.json"

    sql_path = simulation_dir / "eplusout.sql"
    kpis: dict[str, object] = {}

    if sql_path.exists():
        kpis = _query_eui(sql_path)
    else:
        log.warning(
            "eplusout.sql not found in %s -- producing empty KPIs for sample %s",
            simulation_dir,
            sample_id,
        )

    kpi_path.write_text(
        json.dumps(
            {"sample_id": sample_id, "kpis": kpis},
            indent=2,
        )
    )
    log.info("wrote KPIs for sample %s -> %s", sample_id, kpi_path)
    return kpi_path


def _query_eui(sql_path: Path) -> dict[str, object]:
    """Query EUI from the EnergyPlus SQL output.

    The canonical query targets the ``TabularDataWithStrings``
    table.  EnergyPlus writes site energy in MJ/m2; we convert
    to kWh/m2 using the 1 MJ = 1/3.6 kWh conversion factor.

    Returns a dict with keys like ``eui_kwh_m2_yr``,
    ``total_site_energy_MJ_m2_yr``, ``net_site_energy_MJ_m2_yr``.
    """
    result: dict[str, object] = {}
    try:
        conn = sqlite3.connect(sql_path)
        cur = conn.cursor()

        # --- Total Site Energy EUI ---
        cur.execute(
            """
            SELECT Value
            FROM TabularDataWithStrings
            WHERE ReportName = 'InitializationSummary'
              AND ReportForString = 'Entire Facility'
              AND TableName = 'Site and Source Energy'
              AND RowName = 'Total Site Energy'
              AND ColumnName = 'Energy Per Total Building Area'
              AND Units = 'MJ/m2'
            """
        )
        row = cur.fetchone()
        if row is not None:
            mj_m2 = float(row[0])
            result["total_site_energy_MJ_m2_yr"] = round(mj_m2, 3)
            result["eui_kwh_m2_yr"] = round(mj_m2 / 3.6, 3)

        # --- Net Site Energy EUI (may differ if on-site generation) ---
        cur.execute(
            """
            SELECT Value
            FROM TabularDataWithStrings
            WHERE ReportName = 'InitializationSummary'
              AND ReportForString = 'Entire Facility'
              AND TableName = 'Site and Source Energy'
              AND RowName = 'Net Site Energy'
              AND ColumnName = 'Energy Per Total Building Area'
              AND Units = 'MJ/m2'
            """
        )
        row = cur.fetchone()
        if row is not None:
            result["net_site_energy_MJ_m2_yr"] = round(float(row[0]), 3)

        conn.close()
    except Exception as e:
        log.error("failed to query eplusout.sql at %s: %s", sql_path, e)
    return result
