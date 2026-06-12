"""Time-series query API for per-sample eplusout.sql files (issue #274).

This module provides a FastAPI router that queries the raw EnergyPlus
SQL output for a given campaign/sample, returning hourly, daily, or
monthly aggregated time-series data for specified output variables.

Storage implications
--------------------
Per-sample ``eplusout.sql`` files contain the full 8760-hour raw
time-series for every output variable reported by EnergyPlus. A single
file typically ranges from 5 MB (small model, few variables) to
200 MB+ (complex model, hundreds of reporting variables). For a campaign
with 1 000 samples, raw SQL storage can reach **200 GB** before any
aggregation.

OSimFlow now preserves these files unconditionally (issue #274):
they are stored at::

    {outdir}/work/sim/{sample_id}/eplusout.sql

and are **never deleted** after a successful simulation. Users who need
to reclaim disk space should set up an external archival policy (e.g.,
compress to ``.sql.gz`` or migrate to object storage) rather than
relying on the framework to clean them up.

API usage
--------
::

    # Hourly data for a single variable
    GET /api/v1/campaigns/{cid}/samples/{sid}/timeseries?variable=Zone Air Temperature&freq=hourly

    # Daily aggregates
    GET /api/v1/campaigns/{cid}/samples/{sid}/timeseries?variable=Zone Air Temperature&freq=daily

    # Monthly aggregates
    GET /api/v1/campaigns/{cid}/samples/{sid}/timeseries?variable=Zone Air Temperature&freq=monthly

EnergyPlus SQL schema
---------------------
The relevant tables are:

``reportmetadata``
  One row per (VariableName, KeyName, ReportingFrequency) triplet.
  Columns: VariableName, KeyName, Units, ReportingFrequency, ScheduleName,
  ConformReportFreq, MinValue, MaxValue, AvgValue.

``reportdata``
  The actual time-series values.  Columns: TimeIndex, ReportDataRunIndex,
  VariableName, KeyName, Units, Value, ReportingFrequency.

``timedata``
  Maps TimeIndex to calendar fields (Month, Day, Hour, Minute, DST).
  Columns: TimeIndex, Month, Day, Hour, Minute, DST.

For aggregation we join ``reportdata`` → ``timedata`` and group by
strftime-based date truncations.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from osimflow.api.campaigns import _campaign_dir_from_id, _load_campaign_json

log = logging.getLogger("osimflow.api.timeseries")

timeseries_router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sim_dir_from_sample(campaign_dir: Path, sample_id: str) -> Path:
    """Return the simulation output directory for a sample.

    The path is ``{campaign_dir}/work/sim/{sample_id}/``.
    """
    # Basic sanity check to avoid path traversal
    if "/" in sample_id or "\\" in sample_id or ".." in sample_id:
        raise HTTPException(status_code=400, detail="Invalid sample_id")
    return campaign_dir / "work" / "sim" / sample_id


def _open_sql(sim_dir: Path) -> sqlite3.Connection:
    """Open the eplusout.sql file in a given directory.

    Raises HTTPException 404 if the file does not exist.
    """
    sql_path = sim_dir / "eplusout.sql"
    if not sql_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"eplusout.sql not found for sample in {sim_dir}. "
            "The simulation may have failed or not completed.",
        )
    conn = sqlite3.connect(str(sql_path))
    conn.row_factory = sqlite3.Row
    return conn


def _available_variables(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Return the list of available (VariableName, KeyName, Units) triplets."""
    rows = conn.execute(
        """
        SELECT DISTINCT VariableName, KeyName, Units
        FROM reportmetadata
        ORDER BY VariableName, KeyName
        """,
    ).fetchall()
    return [dict(row) for row in rows]


def _query_timeseries(
    conn: sqlite3.Connection,
    variable: str,
    freq: str,
) -> list[dict[str, Any]]:
    """Query time-series data for *variable* at aggregation *freq*.

    Parameters
    ----------
    conn
        SQLite connection to the eplusout.sql file.
    variable
        Variable name to query (e.g. ``"Zone Air Temperature"``).
    freq
        One of ``hourly``, ``daily``, ``monthly``.

    Returns
    -------
    list[dict]
        Each row has ``timestamp`` (ISO 8601), ``value`` (float),
        ``units`` (str), and ``key`` (str).

    Raises
    ------
    HTTPException 404 if the variable is not found in the SQL file.
    """
    # Determine the strftime format for grouping
    if freq == "hourly":
        group_fmt = "%Y-%m-%d %H:00"
        select_fmt = "%Y-%m-%d %H:00:00"
    elif freq == "daily":
        group_fmt = "%Y-%m-%d"
        select_fmt = "%Y-%m-%d"
    elif freq == "monthly":
        group_fmt = "%Y-%m"
        select_fmt = "%Y-%m"
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid freq={freq!r}. Must be hourly, daily, or monthly.",
        )

    # Check that the variable exists and get its KeyName / Units
    meta_rows = conn.execute(
        """
        SELECT VariableName, KeyName, Units, ReportingFrequency
        FROM reportmetadata
        WHERE VariableName = ?
        ORDER BY KeyName
        """,
        (variable,),
    ).fetchall()

    if not meta_rows:
        raise HTTPException(
            status_code=404,
            detail=f"Variable {variable!r} not found in this simulation. "
            "Check the available variables endpoint first.",
        )

    # Build the aggregation query.
    # We join reportdata → timedata to get actual timestamps, then
    # group by the strftime truncation.
    #
    # EnergyPlus stores Hour as 1-24, so we need to subtract 1 to get
    # 0-based hours for proper daily grouping (hour 1 = 00:00-01:00).
    query = f"""
        SELECT
            strftime('{select_fmt}', td.Month, td.Day,
                    CASE WHEN td.Hour = 24 THEN 0 ELSE td.Hour END,
                    '00', '00') AS timestamp,
            AVG(rd.Value) AS value,
            rd.Units AS units,
            rd.KeyName AS key
        FROM reportdata rd
        JOIN timedata td ON rd.TimeIndex = td.TimeIndex
        WHERE rd.VariableName = ?
        GROUP BY strftime('{group_fmt}',
                         td.Month, td.Day,
                         CASE WHEN td.Hour = 24 THEN 0 ELSE td.Hour END,
                         '00', '00'),
                 rd.Units, rd.KeyName
        ORDER BY timestamp, rd.KeyName
        """
    rows = conn.execute(query, (variable,)).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@timeseries_router.get(
    "/api/v1/campaigns/{campaign_id}/samples/{sample_id}/timeseries",
)
async def get_timeseries(
    campaign_id: str,
    sample_id: str,
    request: Request,
    variable: str = Query(..., description="Variable name to retrieve (e.g. 'Zone Air Temperature')"),
    freq: str = Query("hourly", description="Aggregation frequency: hourly (default), daily, or monthly"),
) -> dict[str, Any]:
    """Return time-series data for a variable from a sample's eplusout.sql.

    The response is a JSON object with::

        {
          "variable": "Zone Air Temperature",
          "frequency": "hourly",
          "units": "C",
          "n_points": 8760,
          "data": [
            {"timestamp": "2024-01-01 01:00:00", "value": 20.5, "units": "C", "key": "Zone 1"},
            ...
          ]
        }

    Timestamps are ISO 8601 strings.  Values are averaged over the
    aggregation period for ``daily`` and ``monthly`` frequencies.

    **Storage note**: each per-sample ``eplusout.sql`` can be 5–200+ MB.
    Retrieve only the variables you need to avoid transferring large files.
    """
    from osimflow.api.campaigns import _campaigns_base_dir  # noqa: PLC0415

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    _load_campaign_json(campaign_dir)  # validate campaign exists

    sim_dir = _sim_dir_from_sample(campaign_dir, sample_id)
    conn = _open_sql(sim_dir)

    try:
        rows = _query_timeseries(conn, variable, freq)
    finally:
        conn.close()

    if not rows:
        return {
            "variable": variable,
            "frequency": freq,
            "units": "",
            "n_points": 0,
            "data": [],
        }

    # Extract units from the first row (consistent across the variable)
    units = rows[0].get("units") or ""
    return {
        "variable": variable,
        "frequency": freq,
        "units": units,
        "n_points": len(rows),
        "data": rows,
    }


@timeseries_router.get(
    "/api/v1/campaigns/{campaign_id}/samples/{sample_id}/timeseries/variables",
)
async def list_timeseries_variables(
    campaign_id: str,
    sample_id: str,
    request: Request,
) -> dict[str, Any]:
    """List all available time-series variables for a sample's eplusout.sql.

    Returns a JSON object with::

        {
          "variables": [
            {"VariableName": "Zone Air Temperature", "KeyName": "Zone 1", "Units": "C"},
            ...
          ],
          "total": 42
        }

    Use this endpoint to discover valid ``variable`` values before calling
    ``GET .../timeseries``.
    """
    from osimflow.api.campaigns import _campaigns_base_dir  # noqa: PLC0415

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    _load_campaign_json(campaign_dir)

    sim_dir = _sim_dir_from_sample(campaign_dir, sample_id)
    conn = _open_sql(sim_dir)

    try:
        variables = _available_variables(conn)
    finally:
        conn.close()

    return {
        "variables": variables,
        "total": len(variables),
    }
