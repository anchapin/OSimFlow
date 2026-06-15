"""Campaign registry for multi-campaign management (issue #266).

Stores campaign metadata in a SQLite database so users can list, query,
and compare past campaigns without manually searching the filesystem.

Registry file location:
    Default: ``~/.osimflow/registry.db``
    Override: ``--registry`` CLI flag or ``OSIMFLOW_REGISTRY`` env var.

The schema is intentionally small — one row per campaign with enough
metadata to support ``osimflow list``, ``osimflow show``, and
``osimflow compare`` without touching the per-campaign ``run.json``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.registry")

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    project         TEXT NOT NULL DEFAULT '',
    outdir          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    algorithm       TEXT NOT NULL DEFAULT 'lhs',
    n_samples       INTEGER NOT NULL DEFAULT 0,
    executor        TEXT NOT NULL DEFAULT 'local',
    openstudio_version TEXT NOT NULL DEFAULT '',
    config_hash     TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    completed_at    REAL,
    metadata        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_campaigns_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS ix_campaigns_created ON campaigns(created_at);
CREATE INDEX IF NOT EXISTS ix_campaigns_project ON campaigns(project);
"""


@dataclasses.dataclass
class CampaignRecord:
    """One row in the campaign registry."""

    id: str
    name: str
    project: str
    outdir: str
    status: str
    algorithm: str
    n_samples: int
    executor: str
    openstudio_version: str
    config_hash: str
    created_at: float
    completed_at: float | None
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> CampaignRecord:
        metadata: dict[str, Any] = {}
        raw_meta = row["metadata"]
        if raw_meta:
            try:
                metadata = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        return cls(
            id=row["id"],
            name=row["name"],
            project=row["project"],
            outdir=row["outdir"],
            status=row["status"],
            algorithm=row["algorithm"],
            n_samples=row["n_samples"],
            executor=row["executor"],
            openstudio_version=row["openstudio_version"],
            config_hash=row["config_hash"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            metadata=metadata,
        )


def default_registry_path() -> Path:
    """Return the default registry path: ``~/.osimflow/registry.db``."""
    env = os.environ.get("OSIMFLOW_REGISTRY")
    if env:
        return Path(env)
    return Path.home() / ".osimflow" / "registry.db"


class CampaignRegistry:
    """SQLite-backed campaign metadata store.

    Usage::

        reg = CampaignRegistry()            # default path
        reg.register("2026-01-01T12-00-00", metadata)
        campaigns = reg.list_campaigns()
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_registry_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            # Migration: add project column if it doesn't exist (issue #390)
            try:
                c.execute("SELECT project FROM campaigns LIMIT 1")
            except sqlite3.OperationalError:
                c.execute("ALTER TABLE campaigns ADD COLUMN project TEXT NOT NULL DEFAULT ''")
                c.execute("CREATE INDEX IF NOT EXISTS ix_campaigns_project ON campaigns(project)")
        log.debug("registry opened at %s", self.db_path)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def register(
        self,
        campaign_id: str,
        *,
        name: str = "",
        project: str = "",
        outdir: str = "",
        status: str = "running",
        algorithm: str = "lhs",
        n_samples: int = 0,
        executor: str = "local",
        openstudio_version: str = "",
        config_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert a new campaign into the registry.

        If a campaign with the same id already exists, it is replaced
        (``INSERT OR REPLACE``).
        """
        meta_json = json.dumps(metadata or {}, sort_keys=True, default=str)
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO campaigns
                   (id, name, project, outdir, status, algorithm, n_samples,
                    executor, openstudio_version, config_hash,
                    created_at, completed_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    campaign_id,
                    name,
                    project,
                    outdir,
                    status,
                    algorithm,
                    n_samples,
                    executor,
                    openstudio_version,
                    config_hash,
                    time.time(),
                    None,
                    meta_json,
                ),
            )
        log.info("registered campaign %s (status=%s)", campaign_id, status)

    def list_campaigns(
        self,
        *,
        status: str | None = None,
        project: str | None = None,
        limit: int = 100,
    ) -> list[CampaignRecord]:
        """Return all registered campaigns, newest first.

        Parameters
        ----------
        status
            Filter by status (e.g. ``"success"``, ``"failure"``,
            ``"running"``).  ``None`` returns all.
        project
            Filter by project name. ``None`` returns all projects.
        limit
            Maximum number of records to return.
        """
        with self._conn() as c:
            conditions: list[str] = []
            params: list[str | int] = []
            if status is not None:
                conditions.append("status=?")
                params.append(status)
            if project is not None:
                conditions.append("project=?")
                params.append(project)
            if conditions:
                where_clause = " WHERE " + " AND ".join(conditions)
                rows = c.execute(
                    f"SELECT * FROM campaigns{where_clause} ORDER BY created_at DESC LIMIT ?",
                    [*params, limit],
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM campaigns ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [CampaignRecord.from_row(r) for r in rows]

    def get_campaign(self, campaign_id: str) -> CampaignRecord | None:
        """Return a single campaign by id, or ``None`` if not found."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM campaigns WHERE id=?",
                (campaign_id,),
            ).fetchone()
        if row is None:
            return None
        return CampaignRecord.from_row(row)

    def update_status(self, campaign_id: str, status: str) -> None:
        """Update the status of a campaign and set completed_at if terminal."""
        completed_at: float | None = None
        if status in ("success", "failure"):
            completed_at = time.time()
        with self._conn() as c:
            c.execute(
                "UPDATE campaigns SET status=?, completed_at=? WHERE id=?",
                (status, completed_at, campaign_id),
            )
        log.info("campaign %s status updated to %s", campaign_id, status)

    def delete_campaign(self, campaign_id: str) -> bool:
        """Remove a campaign from the registry. Returns True if deleted."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM campaigns WHERE id=?",
                (campaign_id,),
            )
        deleted = cur.rowcount > 0
        if deleted:
            log.info("deleted campaign %s from registry", campaign_id)
        return deleted

    def compare(
        self,
        id1: str,
        id2: str,
    ) -> dict[str, CampaignRecord | None]:
        """Return both campaign records for side-by-side comparison.

        Returns ``{"left": record_or_none, "right": record_or_none}``.
        """
        return {
            "left": self.get_campaign(id1),
            "right": self.get_campaign(id2),
        }
