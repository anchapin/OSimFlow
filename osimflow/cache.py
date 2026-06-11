"""SQLite-backed cache for OSimFlow campaign runs.

The cache is an explicit, content-hash-based resume layer: every invalidation
rule must be reviewable, testable, and correct. It is intentionally small
(~120 lines) for that reason.

Cache key shape (PRIMARY KEY):
    (step, sample_id, openstudio_version, inputs_sha256, code_sha256,
     container_digest)

Invalidation rules (per PRD §6 gotcha #3 and the analysis in
`.agents/results/result-architecture.md`):
  * Editing a file in `bin/` invalidates the steps that reference it,
    for ALL samples.
  * Changing `--openstudio_version` invalidates `RUN_OPENSTUDIO_SIM`
    entries only (it does not affect LHS, apply, extract, aggregate).
  * Changing `template_sim_package` content invalidates `APPLY_PARAMETERS`
    and `RUN_OPENSTUDIO_SIM` (the modified sim package is derived from it).
  * Changing `variables.yml` invalidates `GENERATE_LHS_SAMPLES` (and
    therefore every downstream step that depends on the sample set).

The DB schema is small enough to inspect with `sqlite3 cache.db ".schema"`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger("osimflow.cache")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
    step              TEXT NOT NULL,
    sample_id         TEXT NOT NULL,         -- 'ALL' for singleton steps
    openstudio_version TEXT NOT NULL,        -- 'N/A' for non-OS steps
    inputs_sha256     TEXT NOT NULL,
    code_sha256       TEXT NOT NULL,
    container_digest  TEXT NOT NULL,
    generation        INTEGER NOT NULL DEFAULT 0,
    output_path       TEXT NOT NULL,
    started_at        REAL NOT NULL,
    finished_at       REAL NOT NULL,
    exit_code         INTEGER NOT NULL,
    PRIMARY KEY (step, sample_id, openstudio_version, inputs_sha256, code_sha256, container_digest, generation)
);
CREATE INDEX IF NOT EXISTS ix_cache_step ON cache_entries(step);
"""


@dataclasses.dataclass(frozen=True)
class CacheKey:
    """Inputs that, if any one changes, invalidate the cached output."""

    step: str
    sample_id: str
    openstudio_version: str
    inputs_sha256: str
    code_sha256: str
    container_digest: str
    generation: int = 0


def sha256_of_files(paths: Iterable[Path]) -> str:
    """Hash a set of files (path-sorted for determinism)."""
    h = hashlib.sha256()
    for p in sorted(paths, key=str):
        h.update(str(p).encode())
        h.update(b"\0")
        if p.is_file():
            h.update(p.read_bytes())
        else:
            # directory: hash the sorted listing
            for child in sorted(p.rglob("*"), key=str):
                h.update(str(child.relative_to(p)).encode())
                h.update(b"\0")
                if child.is_file():
                    h.update(child.read_bytes())
    return h.hexdigest()


def sha256_of_dict(d: dict[str, object]) -> str:
    """Stable hash of a JSON-serializable dict (sort_keys=True)."""
    blob = json.dumps(d, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


class SQLiteCache:
    """The campaign's resume cache. Append-only on hits, INSERT OR REPLACE on misses."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
        log.info("cache opened at %s", db_path)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def lookup(self, key: CacheKey) -> Path | None:
        """Return the cached output path if this exact key is present and successful."""
        with self._conn() as c:
            row = c.execute(
                """SELECT output_path, exit_code FROM cache_entries
                   WHERE step=? AND sample_id=? AND openstudio_version=?
                     AND inputs_sha256=? AND code_sha256=? AND container_digest=?
                     AND generation=?""",
                (
                    key.step,
                    key.sample_id,
                    key.openstudio_version,
                    key.inputs_sha256,
                    key.code_sha256,
                    key.container_digest,
                    key.generation,
                ),
            ).fetchone()
        if row is None:
            return None
        if row["exit_code"] != 0:
            return None
        out = Path(row["output_path"])
        if not out.exists():
            # Stale cache entry: the output was deleted out from under us.
            log.warning("cache hit but output missing on disk: %s", out)
            return None
        log.info("cache HIT  step=%s sample=%s -> %s", key.step, key.sample_id, out)
        return out

    def store(self, key: CacheKey, output_path: Path, exit_code: int) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO cache_entries
                   (step, sample_id, openstudio_version, inputs_sha256,
                    code_sha256, container_digest, generation, output_path,
                    started_at, finished_at, exit_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key.step,
                    key.sample_id,
                    key.openstudio_version,
                    key.inputs_sha256,
                    key.code_sha256,
                    key.container_digest,
                    key.generation,
                    str(output_path),
                    time.time(),
                    time.time(),
                    exit_code,
                ),
            )
        log.info(
            "cache STORE step=%s sample=%s exit=%d -> %s",
            key.step,
            key.sample_id,
            exit_code,
            output_path,
        )

    def invalidate_step(self, step: str) -> int:
        """Drop every entry for a given step. Used by --openstudio_version bumps."""
        with self._conn() as c:
            cur = c.execute("DELETE FROM cache_entries WHERE step=?", (step,))
            n = cur.rowcount
        log.warning("cache INVALIDATE step=%s (%d rows)", step, n)
        return n

    def invalidate_sample(self, step: str, sample_id: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM cache_entries WHERE step=? AND sample_id=?",
                (step, sample_id),
            )
            n = cur.rowcount
        log.info("cache INVALIDATE step=%s sample=%s (%d rows)", step, sample_id, n)
        return n

    def stats(self) -> dict[str, object]:
        with self._conn() as c:
            n_total = c.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
            by_step = dict(
                c.execute("SELECT step, COUNT(*) FROM cache_entries GROUP BY step").fetchall()
            )
        return {"total": n_total, "by_step": by_step}
