"""BYOS example: extract end-use energy breakdown from eplusout.sql.

This example queries the ``End Uses`` table from the EnergyPlus
SQL output to extract energy consumption broken down by end-use
category (heating, cooling, fans, lighting, interior equipment)
and fuel type (electricity, natural gas, etc.).

The result is a flat dict where each key is a normalised
``"<end_use>_<fuel_type>_GJ"`` string.  This format integrates
cleanly with the default aggregator (``bin/aggregate_results.py``)
which stacks all KPI dict keys into columns.

Usage::

    osimflow run \\
        --executor local \\
        --custom_kpi_extractor user_scripts/examples/custom_kpi_enduses.py \\
        --input_variables variables.yml \\
        --template_sim_package ./example_package \\
        --n_samples 10 \\
        --outdir ./results

See user_scripts/README.md for the full BYOS contract reference.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger("custom_kpi_enduses")


def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    """Extract end-use breakdown from a single sample's simulation output.

    Args:
        simulation_dir: directory containing eplusout.sql.
        sample_id: the sample identifier (e.g. ``"0001"``).
        out: output directory for the KPI JSON file.

    Returns:
        Path to the written KPI JSON file.
    """
    out.mkdir(parents=True, exist_ok=True)
    kpi_path = out / f"kpi_{sample_id}.json"

    sql_path = simulation_dir / "eplusout.sql"
    kpis: dict[str, object] = {}

    if sql_path.exists():
        kpis = _query_end_uses(sql_path)
    else:
        log.warning(
            "eplusout.sql not found in %s for sample %s",
            simulation_dir,
            sample_id,
        )

    kpi_path.write_text(
        json.dumps(
            {"sample_id": sample_id, "kpis": kpis},
            indent=2,
        )
    )
    log.info("wrote end-use KPIs for sample %s -> %s", sample_id, kpi_path)
    return kpi_path


def _normalise_key(name: str) -> str:
    """Convert an EnergyPlus table name to a flat dict key.

    ``"Heating"`` -> ``"heating"``, ``"Interior Equipment"`` ->
    ``"interior_equipment"``, etc.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _query_end_uses(sql_path: Path) -> dict[str, object]:
    """Query the End Uses table from eplusout.sql.

    The ``End Uses`` table in EnergyPlus has end-use categories as
    rows (Heating, Cooling, Interior Lighting, Fans, etc.) and fuel
    types as columns (Electricity, Natural Gas, Additional Fuel, etc.).

    Each cell is in GJ.  We flatten to ``<end_use>_<fuel_type>_GJ``
    so the aggregator can treat each as a separate column.
    """
    result: dict[str, object] = {}
    try:
        conn = sqlite3.connect(sql_path)
        cur = conn.cursor()

        cur.execute(
            """
            SELECT RowName, ColumnName, Value, Units
            FROM TabularDataWithStrings
            WHERE TableName = 'End Uses'
              AND ReportForString = 'Entire Facility'
              AND Value IS NOT NULL
              AND Value != ''
              AND Value != '0.00'
            """
        )
        for row_name, col_name, value, _units in cur.fetchall():
            try:
                val = float(value)
            except (ValueError, TypeError):
                continue
            key = f"{_normalise_key(row_name)}_{_normalise_key(col_name)}_GJ"
            result[key] = round(val, 4)

        conn.close()
    except Exception as e:
        log.error("failed to query end uses from %s: %s", sql_path, e)
    return result
