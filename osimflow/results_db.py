"""SQLite-backed results database for OSimFlow campaign runs.

Phase 1 of GAP-008 / OPS-003: a persistent, queryable store for
per-sample KPI results. The database lives at ``${outdir}/results.db``
and survives across campaign re-runs, enabling historical analysis,
cross-campaign comparison, and filtering by KPI value.

Schema
======
``results`` — one row per (sample_id, kpi_name) pair::

    sample_id      TEXT NOT NULL,
    kpi_name       TEXT NOT NULL,
    kpi_value      REAL NOT NULL,
    unit           TEXT,
    timestamp      REAL NOT NULL,
    campaign_id    TEXT NOT NULL,
    generation     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sample_id, kpi_name, campaign_id, generation)

``campaigns`` — one row per campaign::

    campaign_id    TEXT PRIMARY KEY,
    created_at      REAL NOT NULL,
    n_samples      INTEGER NOT NULL,
    algorithm      TEXT,
    openstudio_version TEXT

The ``campaign_id`` column links results to campaigns, allowing
cross-campaign queries and historical analysis.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ._sqlite_store import connect as _store_connect

log = logging.getLogger("osimflow.results_db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id         TEXT PRIMARY KEY,
    created_at          REAL NOT NULL,
    n_samples           INTEGER NOT NULL,
    algorithm           TEXT,
    openstudio_version  TEXT
);

CREATE TABLE IF NOT EXISTS results (
    sample_id      TEXT NOT NULL,
    kpi_name       TEXT NOT NULL,
    kpi_value      REAL NOT NULL,
    unit           TEXT,
    timestamp      REAL NOT NULL,
    campaign_id    TEXT NOT NULL,
    generation     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sample_id, kpi_name, campaign_id, generation)
);

CREATE INDEX IF NOT EXISTS ix_results_kpi_name ON results(kpi_name);
CREATE INDEX IF NOT EXISTS ix_results_campaign_id ON results(campaign_id);
CREATE INDEX IF NOT EXISTS ix_results_kpi_value ON results(kpi_value);
"""


class ResultsDatabase:
    """SQLite-backed results store with query interface.

    The database uses a single persistent connection in WAL mode for
    the lifetime of the instance, avoiding the race condition where
    concurrent connections fighting over ``.sqlite-wal`` and
    ``.sqlite-shm`` auxiliary files cause ``FileNotFoundError`` during
    pytest-xdist parallel test teardown.

    The database is itself a context manager::

        with ResultsDatabase(db_path) as db:
            db.add_result("0001", "eui", 150.2, "kWh/m²/yr")
            db.add_result("0001", "cost", 1200.0, "USD")
            rows = db.query_results(kpi_name="eui", min_value=100.0)

    Or call ``close()`` explicitly::

        db = ResultsDatabase(db_path)
        try:
            db.add_campaign("camp_001", n_samples=10)
            db.add_result("0001", "eui", 150.2, "kWh/m²/yr")
        finally:
            db.close()
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        log.info("results database opened at %s", db_path)

    def _init_db(self) -> None:
        conn = self._connect()
        conn.executescript(SCHEMA)
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        # Issue #1564: shared SQLite access primitive — WAL, busy_timeout,
        # synchronous=NORMAL, locking_mode=NORMAL, check_same_thread=False,
        # timeout=10.0, and row_factory=sqlite3.Row now live in
        # osimflow._sqlite_store. The schema PRAGMAs are the same as
        # before; the extra ``synchronous=NORMAL`` / ``locking_mode=NORMAL``
        # are SQLite-recommended for a shared database (issues #620/#1340).
        return _store_connect(self.db_path)

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the persistent connection (lazy initialization)."""
        return self._ensure_conn()

    def close(self) -> None:
        """Checkpoint the WAL, run ``PRAGMA optimize``, and close the connection.

        After this method returns, the WAL auxiliary files are removed
        and the database is in a clean state. Safe to call multiple times.
        """
        if self._conn is None:
            return
        try:
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("PRAGMA optimize")
        except Exception:
            pass
        with contextlib.suppress(Exception):
            self._conn.close()
        self._conn = None
        log.debug("results database closed at %s", self.db_path)

    def __enter__(self) -> ResultsDatabase:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Campaign management
    # ------------------------------------------------------------------
    def add_campaign(
        self,
        campaign_id: str,
        n_samples: int,
        algorithm: str | None = None,
        openstudio_version: str | None = None,
    ) -> None:
        """Register a campaign in the database.

        Parameters
        ----------
        campaign_id
            Unique identifier for the campaign.
        n_samples
            Number of samples in the campaign.
        algorithm
            Sampling/optimization algorithm name (e.g. "lhs", "de").
        openstudio_version
            OpenStudio version used for the campaign.
        """
        with self._lock:
            c = self.connection
            c.execute(
                """INSERT OR REPLACE INTO campaigns
                   (campaign_id, created_at, n_samples, algorithm, openstudio_version)
                   VALUES (?, ?, ?, ?, ?)""",
                (campaign_id, time.time(), n_samples, algorithm, openstudio_version),
            )
        log.info("results: registered campaign %s (%d samples)", campaign_id, n_samples)

    # ------------------------------------------------------------------
    # Result ingestion
    # ------------------------------------------------------------------
    def add_result(
        self,
        sample_id: str,
        kpi_name: str,
        kpi_value: float,
        unit: str | None = None,
        campaign_id: str = "default",
        generation: int = 0,
    ) -> None:
        """Store a single KPI result.

        Parameters
        ----------
        sample_id
            The sample's identifier (e.g. "0001").
        kpi_name
            The KPI name (e.g. "eui", "cost", "total_ghg").
        kpi_value
            The numeric KPI value.
        unit
            Optional unit string (e.g. "kWh/m²/yr").
        campaign_id
            Campaign identifier (default "default").
        generation
            Generation index for iterative algorithms (default 0).
        """
        with self._lock:
            c = self.connection
            c.execute(
                """INSERT OR REPLACE INTO results
                   (sample_id, kpi_name, kpi_value, unit, timestamp,
                    campaign_id, generation)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    sample_id,
                    kpi_name,
                    float(kpi_value),
                    unit,
                    time.time(),
                    campaign_id,
                    generation,
                ),
            )
        log.debug(
            "results: added %s=%s (%s) for sample=%s campaign=%s",
            kpi_name,
            kpi_value,
            unit or "no unit",
            sample_id,
            campaign_id,
        )

    def add_results_from_kpi_file(
        self,
        kpi_file: Path,
        campaign_id: str = "default",
        generation: int = 0,
    ) -> int:
        """Ingest all KPIs from a KPI JSON file produced by ``extract_kpis.py``.

        The file must have the shape::

            {
                "sample_id": "0001",
                "kpis": {
                    "eui": 150.2,
                    "total_ghg": 42.5
                }
            }

        Parameters
        ----------
        kpi_file
            Path to the KPI JSON file.
        campaign_id
            Campaign identifier (default "default").
        generation
            Generation index for iterative algorithms (default 0).

        Returns
        -------
        int
            Number of KPI rows inserted.
        """
        try:
            data = json.loads(kpi_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read KPI file %s: %s", kpi_file, exc)
            return 0

        sample_id = str(data.get("sample_id", kpi_file.stem.replace("kpi_", "")))
        kpis = data.get("kpis", {})
        if not isinstance(kpis, dict):
            log.warning("KPI file %s has unexpected kpis shape", kpi_file)
            return 0

        count = 0
        for kpi_name, kpi_value in kpis.items():
            if not isinstance(kpi_value, (int, float)):
                continue
            self.add_result(
                sample_id=sample_id,
                kpi_name=str(kpi_name),
                kpi_value=float(kpi_value),
                campaign_id=campaign_id,
                generation=generation,
            )
            count += 1
        log.info(
            "results: ingested %d KPIs from %s (campaign=%s)",
            count,
            kpi_file.name,
            campaign_id,
        )
        return count

    # ------------------------------------------------------------------
    # Query interface
    # ------------------------------------------------------------------
    def query_results(
        self,
        kpi_name: str | None = None,
        min_value: float | None = None,
        max_value: float | None = None,
        campaign_id: str | None = None,
        generation: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query results with optional filters.

        Parameters
        ----------
        kpi_name
            Filter to a specific KPI name (e.g. "eui"). If None, returns
            all KPI names.
        min_value
            Filter to KPI values >= min_value.
        max_value
            Filter to KPI values <= max_value.
        campaign_id
            Filter to a specific campaign. If None, all campaigns.
        generation
            Filter to a specific generation. If None, all generations.

        Returns
        -------
        list[dict]
            Each dict has keys: sample_id, kpi_name, kpi_value, unit,
            timestamp, campaign_id, generation.
        """
        with self._lock:
            c = self.connection
            conditions: list[str] = []
            params: list[Any] = []

            if kpi_name is not None:
                conditions.append("kpi_name = ?")
                params.append(kpi_name)
            if min_value is not None:
                conditions.append("kpi_value >= ?")
                params.append(float(min_value))
            if max_value is not None:
                conditions.append("kpi_value <= ?")
                params.append(float(max_value))
            if campaign_id is not None:
                conditions.append("campaign_id = ?")
                params.append(campaign_id)
            if generation is not None:
                conditions.append("generation = ?")
                params.append(int(generation))

            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            query = f"SELECT * FROM results {where} ORDER BY campaign_id, sample_id, kpi_name"
            rows = c.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def get_campaign_summary(self, campaign_id: str | None = None) -> dict[str, Any]:
        """Return a summary of results for one or all campaigns.

        Parameters
        ----------
        campaign_id
            If provided, summarize only this campaign. If None, summarize
            all campaigns.

        Returns
        -------
        dict
            Keys: ``campaigns`` (list of campaign summaries), each with
            ``campaign_id``, ``created_at``, ``n_samples``, ``n_results``,
            ``kpis`` (list of KPI names), ``eui_min``, ``eui_max``, ``eui_mean``.
        """
        with self._lock:
            c = self.connection

            if campaign_id is not None:
                camp_rows = c.execute(
                    "SELECT * FROM campaigns WHERE campaign_id = ?",
                    (campaign_id,),
                ).fetchall()
            else:
                camp_rows = c.execute("SELECT * FROM campaigns").fetchall()

            summaries: list[dict[str, Any]] = []
            for camp in camp_rows:
                cid = camp["campaign_id"]
                result_rows = c.execute(
                    "SELECT kpi_name, kpi_value FROM results WHERE campaign_id = ?",
                    (cid,),
                ).fetchall()

                kpi_names = sorted({r["kpi_name"] for r in result_rows})
                by_kpi: dict[str, list[float]] = {k: [] for k in kpi_names}
                for r in result_rows:
                    by_kpi[r["kpi_name"]].append(r["kpi_value"])

                kpi_stats: dict[str, dict[str, float | None]] = {}
                for kpi, values in by_kpi.items():
                    if values:
                        kpi_stats[kpi] = {
                            "min": round(min(values), 4),
                            "max": round(max(values), 4),
                            "mean": round(sum(values) / len(values), 4),
                            "count": len(values),
                        }
                    else:
                        kpi_stats[kpi] = {"min": None, "max": None, "mean": None, "count": 0}

                summaries.append(
                    {
                        "campaign_id": cid,
                        "created_at": camp["created_at"],
                        "n_samples": camp["n_samples"],
                        "algorithm": camp["algorithm"],
                        "openstudio_version": camp["openstudio_version"],
                        "n_results": len(result_rows),
                        "kpis": kpi_stats,
                    }
                )

            return {"campaigns": summaries}

    def list_kpi_names(self, campaign_id: str | None = None) -> list[str]:
        """Return the distinct KPI names in the database.

        Parameters
        ----------
        campaign_id
            If provided, only list KPIs from this campaign.
        """
        with self._lock:
            c = self.connection
            if campaign_id is not None:
                rows = c.execute(
                    "SELECT DISTINCT kpi_name FROM results WHERE campaign_id = ? ORDER BY kpi_name",
                    (campaign_id,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT DISTINCT kpi_name FROM results ORDER BY kpi_name"
                ).fetchall()
            return [r["kpi_name"] for r in rows]

    def n_results(self, campaign_id: str | None = None) -> int:
        """Return the total number of result rows.

        Parameters
        ----------
        campaign_id
            If provided, count only this campaign's results.
        """
        with self._lock:
            c = self.connection
            if campaign_id is not None:
                row = c.execute(
                    "SELECT COUNT(*) FROM results WHERE campaign_id = ?",
                    (campaign_id,),
                ).fetchone()
            else:
                row = c.execute("SELECT COUNT(*) FROM results").fetchone()
            return row[0] if row else 0
