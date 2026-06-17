#!/usr/bin/env python3
"""MongoDB to SQLiteDocumentStore migration script (GAP-018, issue #558).

Converts an openstudio-server MongoDB export (JSON array or BSON dump) into
an SQLiteDocumentStore-compatible database, enabling migration from the
legacy openstudio-server/Mongoid stack to OSimFlow's SQLite-based document
store.

Usage
-----
    # Convert a MongoDB JSON export to a new SQLite document store
    python scripts/migrate_from_mongodb.py \
        --source mongo_export.json \
        --output ./migrated.db \
        --format json

    # Convert with explicit collection mappings
    python scripts/migrate_from_mongodb.py \
        --source mongo_export.json \
        --output ./migrated.db \
        --mapping analyses=osa_analyses data_points=osa_data_points

    # Dry run — validate input without writing output
    python scripts/migrate_from_mongodb.py \
        --source mongo_export.json \
        --dry-run

    # Convert from BSON (requires bson pip package)
    python scripts/migrate_from_mongodb.py \
        --source mongo_export.bson \
        --output ./migrated.db \
        --format bson

Input format
------------
The script accepts two input formats:

  1. JSON array export — one JSON document per line or a JSON array
     containing MongoDB-style documents with ``_id``, ``created_at``,
     ``updated_at`` and domain fields.

  2. BSON dump — raw BSON bytes as produced by ``mongodump --archive``.

Collection mapping
------------------
MongoDB collections in the export are mapped to SQLiteDocumentStore
collections via the ``--mapping`` argument (or auto-detected from the
export metadata).  The default mapping is:

  ========================  ==============================
  MongoDB collection        SQLiteDocumentStore collection
  ========================  ==============================
  analyses                  analyses
  data_points               data_points
  job_states                job_states
  compute_nodes             compute_nodes
  ========================  ==============================

Document transformation
-----------------------
The migration applies the following transformations automatically:

1. ``_id`` → kept as ``_id`` (SQLiteDocumentStore uses it as the primary
   document identifier).

2. ``created_at`` / ``updated_at`` (BSON datetime) → ISO-8601 string.

3. ``_type`` / ``type`` (Mongoid discrimination field) → dropped unless
   ``--keep-type`` is set (stored in ``metadata._type`` instead).

4. Embedded documents (e.g. ``data_point.variables``) → stored as-is
   (SQLiteDocumentStore handles JSON natively).

5. ``nil`` values in JSON → Python ``None`` (JSON-compatible).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("migrate_from_mongodb")


# ---------------------------------------------------------------------------
# Default collection mapping
# ---------------------------------------------------------------------------

DEFAULT_MAPPING: dict[str, str] = {
    "analyses": "analyses",
    "data_points": "data_points",
    "job_states": "job_states",
    "compute_nodes": "compute_nodes",
}


# ---------------------------------------------------------------------------
# Transformation helpers
# ---------------------------------------------------------------------------

#: Fields that are always dropped during migration (Mongoid internals).
_DROP_FIELDS: frozenset[str] = frozenset(
    {
        "$__.向西",
        "$__向西",
        "_type",
        "type",
        "_versions",
        "embedding_paths",
    }
)


def _drop_mongoid_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """Remove Mongoid-internal keys that have no meaning in SQLiteDocumentStore."""
    return {k: v for k, v in doc.items() if k not in _DROP_FIELDS}


def _coerce_datetime(value: Any) -> Any:
    """Recursively convert BSON datetime objects to ISO-8601 strings."""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _coerce_datetime(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_datetime(item) for item in value]
    return value


def _sanitize_field_name(name: str) -> str:
    """Make a field name safe for SQLiteDocumentStore JSON extraction.

    SQLite JSON1 ``json_extract`` does not support dots in key names,
    so we replace ``.`` with ``_`` in field paths.  Callers can still
    query using dot notation by passing ``--preserve-dots``.
    """
    return name.replace(".", "_")


def _transform_document(doc: dict[str, Any], *, preserve_dots: bool = False) -> dict[str, Any]:
    """Apply all migration transformations to a single document."""
    doc = _drop_mongoid_metadata(doc)
    doc = _coerce_datetime(doc)

    if not preserve_dots:
        doc = {_sanitize_field_name(k): v for k, v in doc.items()}

    # Normalise None / null
    doc = {k: (None if v is None else v) for k, v in doc.items()}

    return doc


# ---------------------------------------------------------------------------
# Format readers
# ---------------------------------------------------------------------------


class MongoDBJSONReader:
    """Read a JSON array or newline-delimited JSON MongoDB export."""

    def __init__(self, source: Path) -> None:
        self.source = source

    def read(self) -> dict[str, list[dict[str, Any]]]:
        """Parse the file and return ``{collection_name: [documents]}``."""
        raw = self.source.read_text(encoding="utf-8").strip()

        # Detect JSON Lines (NDJSON) vs JSON array
        if raw.startswith("["):
            # JSON array — parse as a list of documents with ``_type`` or
            # ``collection`` metadata, OR as a bare list of documents.
            try:
                documents = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"failed to parse JSON array in {self.source}: {exc}") from exc

            if not isinstance(documents, list):
                raise ValueError(
                    f"expected a JSON array of documents in {self.source}, "
                    f"got {type(documents).__name__}"
                )

            # Group by collection using ``collection`` or ``_collection`` field
            return self._group_by_collection(documents)
        else:
            # Newline-delimited JSON — one document per line
            collections: dict[str, list[dict[str, Any]]] = {}
            for line in raw.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    doc = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    log.warning("skipping malformed line: %s", exc)
                    continue
                coll = doc.pop("_collection", doc.pop("collection", "default"))
                collections.setdefault(coll, []).append(doc)
            return collections

    def _group_by_collection(self, documents: list[Any]) -> dict[str, list[dict[str, Any]]]:
        """Infer collection name from each document and group accordingly.

        Uses the ``collection`` / ``_collection`` field if present,
        otherwise falls back to the top-level key structure in the export.
        """
        collections: dict[str, list[dict[str, Any]]] = {}
        for doc in documents:
            if not isinstance(doc, dict):
                continue
            coll = doc.pop("_collection", doc.pop("collection", "default"))
            collections.setdefault(coll, []).append(doc)
        return collections


class MongoDBBSONReader:
    """Read a BSON dump (requires the ``bson`` package from ``pymongo``)."""

    def __init__(self, source: Path) -> None:
        self.source: Path = source

    def read(self) -> dict[str, list[dict[str, Any]]]:
        try:
            import bson
        except ImportError:
            raise ImportError(
                "BSON format requires the 'bson' package (from pymongo). "
                "Install it with: pip install pymongo"
            ) from None

        collections: dict[str, list[dict[str, Any]]] = {}
        with open(self.source, "rb") as fh:
            for doc in bson.decode_file_iter(fh):
                coll = doc.pop("_collection", doc.pop("collection", "default"))
                collections.setdefault(coll, []).append(doc)
        return collections


def _detect_format(source: Path) -> str:
    """Heuristically detect whether the export is JSON or BSON."""
    raw = source.read_bytes()[:8]
    # BSON documents start with a length (4 bytes, little-endian int32).
    # JSON starts with '[' or '{'.
    if raw[0] == 0x5C or raw[0:2] == b"\x00\x00":
        # Looks like BSON (length-prefixed)
        return "bson"
    return "json"


# ---------------------------------------------------------------------------
# Core migration logic
# ---------------------------------------------------------------------------


def migrate(
    source: Path,
    output: Path,
    *,
    mapping: dict[str, str] | None = None,
    format: str | None = None,
    preserve_dots: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """Migrate a MongoDB export to a SQLiteDocumentStore database.

    Parameters
    ----------
    source
        Path to the MongoDB JSON or BSON export file.
    output
        Path to the output SQLite database (created or overwritten).
    mapping
        Optional dict mapping MongoDB collection names to
        SQLiteDocumentStore collection names.
    format
        One of ``"json"`` or ``"bson"``.  If ``None`` the format is
        auto-detected from the file header.
    preserve_dots
        If ``True``, field names with dots are preserved as-is instead
        of being sanitised to underscores.
    dry_run
        If ``True``, validate the input and report what would be migrated
        without writing the output file.

    Returns
    -------
    dict[str, int]
        Per-collection document counts that would be/were migrated.
    """
    mapping = mapping or DEFAULT_MAPPING

    # Detect or validate format
    if format is None:
        format = _detect_format(source)
    if format not in ("json", "bson"):
        raise ValueError(f"unknown format {format!r}; expected 'json' or 'bson'")

    # Read source
    if format == "bson":
        reader: Any = MongoDBBSONReader(source)
    else:
        reader = MongoDBJSONReader(source)

    log.info("reading %s format from %s", format, source)
    collections = reader.read()

    # Apply transformations and count
    counts: dict[str, int] = {}
    transformed: dict[str, list[dict[str, Any]]] = {}

    for coll, docs in collections.items():
        target_coll = mapping.get(coll, coll)
        transformed_docs = [_transform_document(doc, preserve_dots=preserve_dots) for doc in docs]
        transformed[target_coll] = transformed_docs
        counts[coll] = len(transformed_docs)

    # Summary
    total = sum(counts.values())
    log.info(
        "would migrate %d documents across %d collection(s): %s",
        total,
        len(counts),
        counts,
    )

    if dry_run:
        return counts

    # Write to SQLiteDocumentStore
    from osimflow.document_store import SQLiteDocumentStore

    output.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteDocumentStore(db_path=output)

    try:
        for coll, docs in transformed.items():
            log.info("inserting %d documents into collection %r", len(docs), coll)
            for doc in docs:
                store.insert_one(coll, doc)
    finally:
        store.close()

    log.info("migration complete → %s", output)
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_COLLECTION_MAPPING_RE = re.compile(r"^(?P<src>[a-zA-Z0-9_]+)=(?P<tgt>[a-zA-Z0-9_]+)$")


def _parse_mapping(value: str) -> dict[str, str]:
    """Parse a ``--mapping`` argument value into a dict."""
    mapping: dict[str, str] = {}
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        m = _COLLECTION_MAPPING_RE.match(part)
        if not m:
            raise argparse.ArgumentTypeError(
                f"invalid mapping item {part!r}; expected form: src_col=tgt_col"
            )
        mapping[m.group("src")] = m.group("tgt")
    return mapping


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_from_mongodb",
        description="Migrate a MongoDB export to OSimFlow's SQLiteDocumentStore.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to the MongoDB JSON or BSON export file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help="Path to the output SQLite database (created or overwritten).",
    )
    parser.add_argument(
        "--format",
        choices=["json", "bson"],
        default=None,
        help=(
            "Input format: 'json' for JSON array / NDJSON, 'bson' for BSON dump. "
            "If omitted the format is auto-detected from the file header."
        ),
    )
    parser.add_argument(
        "--mapping",
        type=_parse_mapping,
        default=None,
        metavar="SRC=TGT[,SRC2=TGT2]",
        help=(
            "Comma-separated collection mapping. "
            "Example: analyses=osa_analyses,data_points=osa_data_points. "
            "Collections not listed use their original name."
        ),
    )
    parser.add_argument(
        "--preserve-dots",
        action="store_true",
        help=(
            "Preserve dots in field names instead of replacing with underscores. "
            "SQLite JSON1 extraction with dots in key names can behave unexpectedly; "
            "enable this only if your queries use dot-notation key names."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate input and report what would be migrated, without writing output.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s: %(message)s",
    )

    try:
        counts = migrate(
            source=args.source,
            output=args.output,
            mapping=args.mapping,
            format=args.format,
            preserve_dots=args.preserve_dots,
            dry_run=args.dry_run,
        )
        print(f"Migration summary: {counts}")
        return 0
    except (ValueError, ImportError, OSError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
