"""BYOS template: custom KPI extractor.

Copy this file into ``user_scripts/`` and customise the ``_extract``
function to compute your KPIs from the simulation output.  The
framework calls this function once per sample.

Usage::

    cp user_scripts/templates/kpi_extractor_template.py \\
       user_scripts/my_kpis.py

    # Edit my_kpis.py, then run:
    osimflow run \\
        --custom_kpi_extractor user_scripts/my_kpis.py \\
        ...

Required function signature::

    def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path

The function must return a ``Path`` to a JSON file containing::

    {
        "sample_id": "0001",
        "kpis": {
            "your_kpi_name": 123.45,
            ...
        }
    }

See user_scripts/README.md for the full BYOS contract.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("my_kpi_extractor")


def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    kpi_path = out / f"kpi_{sample_id}.json"

    kpis = _extract(simulation_dir)

    kpi_path.write_text(json.dumps({"sample_id": sample_id, "kpis": kpis}, indent=2))
    log.info("wrote KPIs for sample %s -> %s", sample_id, kpi_path)
    return kpi_path


def _extract(simulation_dir: Path) -> dict[str, object]:
    """TODO: Replace this body with your KPI extraction logic.

    ``simulation_dir`` contains the EnergyPlus output files for one
    sample:

    - ``eplusout.sql`` -- SQLite database with tabular reports.
      Use ``sqlite3.connect(simulation_dir / "eplusout.sql")``.
    - ``eplusout.err`` -- error log (useful for diagnostics).
    - Other files depending on the ``output_style`` in the OSW.

    Return a flat dict mapping KPI names to numeric values.
    The aggregator (``bin/aggregate_results.py``) will merge all
    per-sample dicts into a single ``aggregated_results.csv``.

    Example::

        import sqlite3
        conn = sqlite3.connect(simulation_dir / "eplusout.sql")
        cur = conn.cursor()
        cur.execute("SELECT Value FROM TabularDataWithStrings WHERE ...")
        row = cur.fetchone()
        conn.close()
        return {"my_custom_kpi": float(row[0]) if row else None}
    """
    sql_path = simulation_dir / "eplusout.sql"
    if not sql_path.exists():
        log.warning("eplusout.sql not found in %s", simulation_dir)
        return {}

    # TODO: Add your SQL queries here.
    return {}
