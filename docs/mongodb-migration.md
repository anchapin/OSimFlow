# GAP-018: MongoDB to SQLiteDocumentStore Migration Guide

> **Issue**: [#558](https://github.com/anchapin/OSimFlow/issues/558) — GAP-018: Major: MongoDB to SQLiteDocumentStore migration path not available

## Overview

openstudio-server (the predecessor to OSimFlow) stores all persistent state
in MongoDB via the Mongoid ORM.  Collections include:

| Collection | Description |
|---|---|
| `analyses` | Analysis metadata, algorithm type, variable definitions |
| `data_points` | Individual simulation sample results and status |
| `job_states` | Slurm/worker job state transitions |
| `compute_nodes` | HPC compute node registration |

OSimFlow replaces this with `SQLiteDocumentStore`, a SQLite + JSON1 document
store that provides the same MongoDB-like interface without requiring a
separate MongoDB server — critical for HPC environments where MongoDB may
not be available.

This guide explains how to export data from openstudio-server's MongoDB and
migrate it into an OSimFlow `SQLiteDocumentStore`.

---

## Migration Script

The script lives at `scripts/migrate_from_mongodb.py`.

```bash
# Basic usage — auto-detect format
python scripts/migrate_from_mongodb.py \
    --source mongo_export.json \
    --output ./migrated.db

# Explicit JSON format
python scripts/migrate_from_mongodb.py \
    --source mongo_export.json \
    --output ./migrated.db \
    --format json

# Dry run — validate input without writing output
python scripts/migrate_from_mongodb.py \
    --source mongo_export.json \
    --output ./migrated.db \
    --dry-run

# With explicit collection mapping
python scripts/migrate_from_mongodb.py \
    --source mongo_export.json \
    --output ./migrated.db \
    --mapping analyses=analyses,data_points=data_points
```

---

## Step-by-Step Migration

### 1. Export from MongoDB

#### Option A — JSON Array Export (recommended)

```bash
# Using mongoexport (one collection at a time)
mongoexport \
    --db openstudio \
    --collection analyses \
    --out analyses.json \
    --jsonArray

mongoexport \
    --db openstudio \
    --collection data_points \
    --out data_points.json \
    --jsonArray

mongoexport \
    --db openstudio \
    --collection job_states \
    --out job_states.json \
    --jsonArray
```

#### Option B — Combined NDJSON Export

```bash
# Export as newline-delimited JSON (one doc per line)
mongoexport --db openstudio --collection analyses --out combined.jsonl --jsonArray=false
mongoexport --db openstudio --collection data_points --out combined.jsonl --jsonArray=false --append
mongoexport --db openstudio --collection job_states --out combined.jsonl --jsonArray=false --append
```

Each document should include a `collection` or `_collection` field identifying
its source collection.  If missing, the script falls back to a `default`
collection.

#### Option C — BSON Dump (mongodump)

```bash
# Full binary BSON dump
mongodump --db openstudio --archive=mongo_dump.bson
```

> **Note**: BSON format requires the `bson` package from `pymongo`:
> `pip install pymongo`

### 2. Combine into a Single File (optional)

If you exported multiple collections separately and want a single migration
source, combine them:

```python
import json

collections = ["analyses", "data_points", "job_states"]
combined = []

for coll in collections:
    with open(f"{coll}.json") as f:
        docs = json.load(f)
    for doc in docs:
        doc["_collection"] = coll
    combined.extend(docs)

with open("mongo_export.json", "w") as f:
    json.dump(combined, f)
```

### 3. Run the Migration

```bash
python scripts/migrate_from_mongodb.py \
    --source ./mongo_export.json \
    --output ~/.osimflow/migrated.db \
    --log-level DEBUG
```

The script:
1. Reads and parses the MongoDB export (JSON or BSON)
2. Transforms each document (datetime coercion, Mongoid metadata removal)
3. Inserts documents into the SQLiteDocumentStore

### 4. Verify the Migration

```python
from osimflow.document_store import SQLiteDocumentStore
from pathlib import Path

store = SQLiteDocumentStore(db_path=Path("~/.osimflow/migrated.db"))

print("Collections:", store.list_collections())

analyses = store.find_many("analyses")
print(f"Analyses: {len(analyses)}")

data_points = store.find_many("data_points")
print(f"Data points: {len(data_points)}")

store.close()
```

---

## Document Transformation Reference

The migration applies the following transformations automatically:

| Transformation | Details |
|---|---|
| `_id` preservation | The MongoDB ``_id`` is preserved as the document ``_id`` |
| Datetime coercion | BSON ``ISODate`` / ``datetime`` objects → ISO-8601 strings |
| Mongoid metadata removal | Fields like ``_type``, ``_versions``, ``embedding_paths`` are dropped |
| Field name sanitisation | Dots in field names (e.g. ``variables.foo.bar``) → underscores (``variables_foo_bar``) unless ``--preserve-dots`` is set |
| ``None`` normalisation | JSON ``null`` → Python ``None`` |

---

## Collection Mapping

By default, collection names are preserved as-is.  Use `--mapping` to rename:

```bash
python scripts/migrate_from_mongodb.py \
    --source mongo_export.json \
    --output ./migrated.db \
    --mapping analyses=osa_analyses,data_points=osa_data_points
```

This maps the MongoDB `analyses` collection to SQLiteDocumentStore collection
`osa_analyses`, and `data_points` to `osa_data_points`.

---

## Querying Migrated Data

After migration, use the `SQLiteDocumentStore` API:

```python
from osimflow.document_store import SQLiteDocumentStore
from pathlib import Path

store = SQLiteDocumentStore(db_path=Path("~/.osimflow/migrated.db"))

# Find all analyses with a specific algorithm
lhs_analyses = store.find_many(
    "analyses",
    {"algorithm": {"$eq": "lhs"}}
)

# Find data points with EUI > 100
high_eui = store.find_many(
    "data_points",
    {"eui": {"$gt": 100}}
)

# Find a specific analysis by ID
analysis = store.find_one(
    "analyses",
    {"_id": "some-analysis-id"}
)

store.close()
```

### Supported Query Operators

| Operator | Description |
|---|---|
| `$eq` | Equality (default) |
| `$ne` | Not equal |
| `$gt`, `$gte`, `$lt`, `$lte` | Numeric comparison |
| `$in`, `$nin` | Set membership |
| `$exists` | Field existence check |
| `$regex` | SQLite LIKE pattern match |

---

## Limitations

1. **Horizontal scalability**: SQLiteDocumentStore is single-node only.  For
   multi-user or multi-campaign deployments that require horizontal
   scalability, consider a future `MongoDocumentStore` implementation
   (tracked in issue #558).

2. **No transaction support across collections**: Unlike MongoDB's
   multi-document transactions, SQLiteDocumentStore operations are
   per-collection.

3. **Embedded arrays**: Arrays of documents are stored as JSON arrays.
   Querying inside embedded arrays uses SQLite LIKE on the serialised JSON,
   which is less efficient than MongoDB's native nested query.

4. **Binary data (BSON binary)**: BSON `Binary` objects are stored as
   base64-encoded strings.

---

## Future Work

- `MongoDocumentStore` implementation for horizontal scalability
  (tracked in [#558](https://github.com/anchapin/OSimFlow/issues/558))
- Streaming migration for very large exports (>1M documents)
- Incremental / delta migration support

---

## Troubleshooting

### "BSON format requires the 'bson' package"

Install pymongo: `pip install pymongo`

### "Expected a JSON array of documents"

The JSON file must contain a JSON array `[{...}, {...}]` or newline-delimited
JSON.  Check the export format with `mongoexport --jsonArray`.

### Empty collections after migration

Verify the `_collection` field is present in the export, or that the
collection name matches the expected mapping in `DEFAULT_MAPPING`.

### Field names with dots are not queryable

Use `--preserve-dots` with caution.  SQLite JSON1 does not support dots in
key names in `json_extract`.  The migration script replaces dots with
underscores by default.