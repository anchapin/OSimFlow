"""Moved from ``osimflow.work`` (issue #1890); re-exported there."""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("osimflow.work")

# Default conditioned floor area used by the stub eplusout.sql writer
# (issue #1419).  Picked so a single-zone model has a plausible 100 m²
# floor — large enough that EUI values come out in the realistic
# 10-1000 kWh/m²/yr window the validator enforces
# (see ``osimflow._work_scripts.extract_kpis.DEFAULT_QUALITY_THRESHOLDS``).
_STUB_FLOOR_AREA_M2: float = 100.0


def _stub_eui_for_sample(sample_id: str) -> float:
    """Return a deterministic per-sample site EUI in MJ/m² for the stub.

    Issue #1419 — the stub must produce values that satisfy the
    validator's critical-KPI check (``eui_kwh_m2_yr`` and
    ``total_site_energy_kwh``).  We cycle through five plausible MJ/m²
    values keyed off the trailing decimal digit of the zero-padded
    sample id (``0001`` → 1, ``0002`` → 2, …).  Range chosen so the
    final kWh/m²/yr EUI lands between ~28 and ~64, comfortably inside
    the 10-1000 kWh/m²/yr validator band and leaving room for
    per-sample variance that the basic plots can visualise.

    The function is deterministic — same input always returns the same
    value — so tests are reproducible.
    """
    try:
        n = int(sample_id)
    except (TypeError, ValueError):
        n = 0
    cycle = (100.0, 130.0, 160.0, 190.0, 220.0)
    return cycle[n % len(cycle)]


def _write_stub_eplusout_sql(sim_out: Path, sample_id: str) -> Path:
    """Write a valid stub ``eplusout.sql`` containing the EnergyPlus
    schema that ``osimflow._work_scripts.extract_kpis`` actually reads.

    Issue #1419 — the previous stub wrote the literal string
    ``"-- placeholder sql"`` to ``eplusout.sql``, which is not a valid
    SQLite database.  ``extract_kpis`` then raised
    ``sqlite3.DatabaseError: file is not a database``, logged
    "Corrupt eplusout.sql", and emitted a KPI JSON without the critical
    KPIs (``eui_kwh_m2_yr`` and ``total_site_energy_kwh``).  Every
    sample failed the validator, ``AGGREGATE_RESULTS`` ran into
    downstream ``_verify_step_inputs`` failures, and the campaign died.

    This helper writes a real SQLite database containing the minimal
    rows for ``TabularDataWithStrings`` (the EnergyPlus denormalised
    tabular output) and ``Zones`` (per-zone floor area) that satisfy
    the validator's critical-KPI check.  Values are deterministic
    functions of *sample_id* so KPI variance still exists for the
    aggregation and basic-plot steps while staying reproducible across
    test runs.

    The schema mirrors only the columns ``extract_kpis`` reads — see
    :func:`osimflow._work_scripts.extract_kpis.extract_kpis_from_sql`
    and ``_fetch_floor_area``.  End-use, peak-demand, and unmet-hours
    tables are intentionally omitted; missing tables cause
    ``extract_kpis`` to silently skip those non-critical KPIs rather
    than fail.

    Returns:
        The path to the written ``eplusout.sql`` file.
    """
    sql_path = sim_out / "eplusout.sql"
    site_energy_mj_per_m2 = _stub_eui_for_sample(sample_id)
    site_energy_mj_total = site_energy_mj_per_m2 * _STUB_FLOOR_AREA_M2

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(sql_path))
        cur = conn.cursor()

        # Denormalised tabular data — what ``_fetch_scalar`` reads.
        # Column order matches the EnergyPlus SQL output convention so
        # the schema can later be enriched without rewriting the writer.
        cur.execute(
            """
            CREATE TABLE TabularDataWithStrings (
                ReportName TEXT,
                ReportForString TEXT,
                TableName TEXT,
                RowName TEXT,
                ColumnName TEXT,
                Units TEXT,
                Value TEXT
            )
            """
        )

        # Minimum rows for the two critical KPIs the validator requires
        # (``eui_kwh_m2_yr`` and ``total_site_energy_kwh``).  The EUI
        # table is the one referenced by ``_extract_eui`` in
        # ``extract_kpis.py``.  Values are stored in MJ — the extractor
        # multiplies by ``_MJ_TO_KWH = 1/3.6`` to convert to kWh.
        tabular_rows = [
            (
                "AnnualBuildingUtilityPerformanceSummary",
                "Entire Facility",
                "Site and Source Energy",
                "Total Site Energy",
                "Energy Per Total Building Area",
                "MJ/m2",
                f"{site_energy_mj_per_m2:.3f}",
            ),
            (
                "AnnualBuildingUtilityPerformanceSummary",
                "Entire Facility",
                "Site and Source Energy",
                "Total Site Energy",
                "Total Energy",
                "MJ",
                f"{site_energy_mj_total:.3f}",
            ),
            # Net Site Energy per area — populates ``net_eui_kwh_m2_yr``
            # when extract_kpis runs the optional branch.
            (
                "AnnualBuildingUtilityPerformanceSummary",
                "Entire Facility",
                "Site and Source Energy",
                "Net Site Energy",
                "Energy Per Total Building Area",
                "MJ/m2",
                f"{site_energy_mj_per_m2 * 0.9:.3f}",
            ),
        ]
        cur.executemany(
            "INSERT INTO TabularDataWithStrings VALUES (?, ?, ?, ?, ?, ?, ?)",
            tabular_rows,
        )

        # Per-zone floor area — what ``_fetch_floor_area`` reads first.
        cur.execute(
            """
            CREATE TABLE Zones (
                ZoneIndex INTEGER,
                ZoneName TEXT,
                Floor_Area REAL
            )
            """
        )
        cur.execute(
            "INSERT INTO Zones VALUES (?, ?, ?)",
            (1, "Zone 1", _STUB_FLOOR_AREA_M2),
        )

        conn.commit()
    except sqlite3.DatabaseError as exc:
        # Never let the stub-SQL writer itself fail the campaign —
        # fall back to an empty database so downstream code sees the
        # same "no tabular schema" warning path it would have seen
        # before this fix (the issue #1419 root-cause path is closed
        # by the helper itself; this except only protects against
        # disk-full / permission errors on the stub write).
        log.warning(
            "_write_stub_eplusout_sql: failed to write %s for sample=%s: %s",
            sql_path,
            sample_id,
            exc,
        )
        sql_path.write_text("")
        raise
    finally:
        if conn is not None:
            conn.close()

    return sql_path
