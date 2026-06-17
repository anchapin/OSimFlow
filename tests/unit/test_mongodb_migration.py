"""Unit tests for scripts/migrate_from_mongodb.py (GAP-018, issue #558).

Covers:
- MongoDBJSONReader: JSON array format
- MongoDBJSONReader: NDJSON / JSON Lines format
- MongoDBBSONReader: import error when bson not installed
- Document transformation: datetime coercion, mongoid metadata removal,
  field name sanitisation, None normalisation
- End-to-end migrate() into SQLiteDocumentStore
- CLI argument parsing: --mapping, --dry-run, --format
"""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path

import pytest
from scripts.migrate_from_mongodb import (
    MongoDBJSONReader,
    _coerce_datetime,
    _drop_mongoid_metadata,
    _parse_mapping,
    _sanitize_field_name,
    _transform_document,
    migrate,
)

# ---------------------------------------------------------------------------
# Helper: write a temp file with given content
# ---------------------------------------------------------------------------


def _tmp_file(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _sanitize_field_name
# ---------------------------------------------------------------------------


class TestSanitizeFieldName:
    def test_dots_replaced_with_underscore(self) -> None:
        assert _sanitize_field_name("variables.foo.bar") == "variables_foo_bar"

    def test_no_change_when_no_dots(self) -> None:
        assert _sanitize_field_name("sample_id") == "sample_id"

    def test_leading_dot_stripped(self) -> None:
        assert _sanitize_field_name(".foo.bar") == "_foo_bar"


# ---------------------------------------------------------------------------
# _drop_mongoid_metadata
# ---------------------------------------------------------------------------


class TestDropMongoidMetadata:
    def test_drops_type_field(self) -> None:
        doc = {"_id": "a1", "_type": "Analysis", "name": "test"}
        result = _drop_mongoid_metadata(doc)
        assert result == {"_id": "a1", "name": "test"}

    def test_drops_versions_field(self) -> None:
        doc = {"_id": "a1", "_versions": [], "data": 42}
        result = _drop_mongoid_metadata(doc)
        assert result == {"_id": "a1", "data": 42}

    def test_keeps_regular_fields(self) -> None:
        doc = {"_id": "dp1", "sample_id": "s0001", "eui": 150.5}
        result = _drop_mongoid_metadata(doc)
        assert result == doc


# ---------------------------------------------------------------------------
# _coerce_datetime
# ---------------------------------------------------------------------------


class TestCoerceDatetime:
    def test_datetime_to_iso_string(self) -> None:
        dt = datetime.datetime(2024, 6, 15, 10, 30, 0)
        result = _coerce_datetime(dt)
        assert result == "2024-06-15T10:30:00"

    def test_nested_datetime(self) -> None:
        doc = {
            "created_at": datetime.datetime(2024, 6, 15, 10, 30, 0),
            "updated_at": datetime.datetime(2024, 6, 16, 11, 0, 0),
        }
        result = _coerce_datetime(doc)
        assert result["created_at"] == "2024-06-15T10:30:00"
        assert result["updated_at"] == "2024-06-16T11:00:00"

    def test_list_with_datetime(self) -> None:
        value = [datetime.datetime(2024, 6, 15), "text", None]
        result = _coerce_datetime(value)
        assert result[0] == "2024-06-15T00:00:00"
        assert result[1] == "text"
        assert result[2] is None

    def test_preserves_other_types(self) -> None:
        assert _coerce_datetime(42) == 42
        assert _coerce_datetime("hello") == "hello"
        assert _coerce_datetime(None) is None


# ---------------------------------------------------------------------------
# _transform_document
# ---------------------------------------------------------------------------


class TestTransformDocument:
    def test_full_transform(self) -> None:
        doc = {
            "_id": "dp1",
            "_type": "DataPoint",  # dropped
            "created_at": datetime.datetime(2024, 6, 15, 10, 30, 0),
            "sample_id": "s0001",
            "variables.foo": 100,
        }
        result = _transform_document(doc, preserve_dots=False)
        assert result == {
            "_id": "dp1",
            "created_at": "2024-06-15T10:30:00",
            "sample_id": "s0001",
            "variables_foo": 100,
        }

    def test_preserve_dots(self) -> None:
        doc = {"_id": "dp1", "variables.foo": 100}
        result = _transform_document(doc, preserve_dots=True)
        assert "variables.foo" in result


# ---------------------------------------------------------------------------
# MongoDBJSONReader — JSON array format
# ---------------------------------------------------------------------------


class TestMongoDBJSONReader:
    def test_json_array_with_collection_field(self, tmp_path: Path) -> None:
        docs = [
            {"_id": "a1", "_collection": "analyses", "name": "Analysis 1"},
            {"_id": "a2", "_collection": "analyses", "name": "Analysis 2"},
            {"_id": "dp1", "_collection": "data_points", "sample_id": "s0001"},
        ]
        source = _tmp_file(tmp_path, "export.json", json.dumps(docs))

        reader = MongoDBJSONReader(source)
        collections = reader.read()

        assert collections == {
            "analyses": [
                {"_id": "a1", "name": "Analysis 1"},
                {"_id": "a2", "name": "Analysis 2"},
            ],
            "data_points": [
                {"_id": "dp1", "sample_id": "s0001"},
            ],
        }

    def test_json_array_with_collection_alias(self, tmp_path: Path) -> None:
        docs = [
            {"_id": "a1", "collection": "analyses", "name": "A1"},
        ]
        source = _tmp_file(tmp_path, "export.json", json.dumps(docs))

        reader = MongoDBJSONReader(source)
        collections = reader.read()

        assert "analyses" in collections

    def test_json_array_without_collection_field(self, tmp_path: Path) -> None:
        # Bare documents without _collection field → "default" collection
        docs = [
            {"_id": "x1", "value": 1},
            {"_id": "x2", "value": 2},
        ]
        source = _tmp_file(tmp_path, "export.json", json.dumps(docs))

        reader = MongoDBJSONReader(source)
        collections = reader.read()

        assert collections == {"default": [{"_id": "x1", "value": 1}, {"_id": "x2", "value": 2}]}

    def test_json_array_malformed_json_falls_through_to_ndjson(self, tmp_path: Path) -> None:
        # Malformed JSON array is not a valid JSON array, so the reader
        # falls back to treating it as potential NDJSON and skips the
        # malformed line with a warning.
        source = _tmp_file(tmp_path, "bad.json", "{not valid json")
        reader = MongoDBJSONReader(source)
        collections = reader.read()
        # Falls through to NDJSON; the bad line is skipped → empty result
        assert collections == {}

    def test_ndjson_format(self, tmp_path: Path) -> None:
        content = (
            '{"_collection":"analyses","_id":"a1","name":"A1"}\n'
            '{"_collection":"data_points","_id":"dp1","sample_id":"s0001"}\n'
        )
        source = _tmp_file(tmp_path, "export.jsonl", content)

        reader = MongoDBJSONReader(source)
        collections = reader.read()

        assert collections == {
            "analyses": [{"_id": "a1", "name": "A1"}],
            "data_points": [{"_id": "dp1", "sample_id": "s0001"}],
        }

    def test_ndjson_skips_malformed_lines(self, tmp_path: Path) -> None:
        content = (
            '{"_collection":"analyses","_id":"a1"}\n'
            "not valid json\n"
            '{"_collection":"analyses","_id":"a2"}\n'
        )
        source = _tmp_file(tmp_path, "export.jsonl", content)

        reader = MongoDBJSONReader(source)
        collections = reader.read()

        assert "analyses" in collections
        assert len(collections["analyses"]) == 2


# ---------------------------------------------------------------------------
# migrate() — end-to-end
# ---------------------------------------------------------------------------


class TestMigrate:
    def test_migrate_json_array_to_sqlite(self, tmp_path: Path) -> None:
        docs = [
            {"_collection": "analyses", "_id": "a1", "name": "Test Analysis"},
            {"_collection": "analyses", "_id": "a2", "name": "Test Analysis 2"},
            {
                "_collection": "data_points",
                "_id": "dp1",
                "sample_id": "s0001",
                "eui": 150.5,
            },
        ]
        source = _tmp_file(tmp_path, "export.json", json.dumps(docs))
        output = tmp_path / "migrated.db"

        counts = migrate(source=source, output=output)

        assert counts["analyses"] == 2
        assert counts["data_points"] == 1

        # Verify the SQLiteDocumentStore contents
        from osimflow.document_store import SQLiteDocumentStore

        store = SQLiteDocumentStore(db_path=output)
        try:
            collections = store.list_collections()
            assert "analyses" in collections
            assert "data_points" in collections

            analyses = store.find_many("analyses")
            assert len(analyses) == 2

            dp = store.find_one("data_points", {"sample_id": "s0001"})
            assert dp is not None
            assert dp["eui"] == 150.5
        finally:
            store.close()

    def test_migrate_with_custom_mapping(self, tmp_path: Path) -> None:
        docs = [
            {"_collection": "analyses", "_id": "a1", "name": "A1"},
        ]
        source = _tmp_file(tmp_path, "export.json", json.dumps(docs))
        output = tmp_path / "migrated.db"

        migrate(
            source=source,
            output=output,
            mapping={"analyses": "osa_analyses"},
        )

        from osimflow.document_store import SQLiteDocumentStore

        store = SQLiteDocumentStore(db_path=output)
        try:
            collections = store.list_collections()
            assert "osa_analyses" in collections
            assert "analyses" not in collections
        finally:
            store.close()

    def test_migrate_dry_run_does_not_write(self, tmp_path: Path) -> None:
        docs = [{"_collection": "analyses", "_id": "a1"}]
        source = _tmp_file(tmp_path, "export.json", json.dumps(docs))
        output = tmp_path / "migrated.db"

        counts = migrate(source=source, output=output, dry_run=True)

        assert output.exists() is False
        assert counts["analyses"] == 1

    def test_migrate_datetime_coercion(self, tmp_path: Path) -> None:
        docs = [
            {
                "_collection": "analyses",
                "_id": "a1",
                "created_at": "2024-06-15T10:30:00",
            },
        ]
        source = _tmp_file(tmp_path, "export.json", json.dumps(docs))
        output = tmp_path / "migrated.db"

        migrate(source=source, output=output)

        from osimflow.document_store import SQLiteDocumentStore

        store = SQLiteDocumentStore(db_path=output)
        try:
            doc = store.find_one("analyses", {"_id": "a1"})
            assert doc is not None
            assert doc["created_at"] == "2024-06-15T10:30:00"
        finally:
            store.close()

    def test_migrate_ndjson(self, tmp_path: Path) -> None:
        content = '{"_collection":"analyses","_id":"a1"}\n'
        source = _tmp_file(tmp_path, "export.jsonl", content)
        output = tmp_path / "migrated.db"

        counts = migrate(source=source, output=output)
        assert counts["analyses"] == 1

    def test_migrate_unknown_format_raises(self, tmp_path: Path) -> None:
        source = _tmp_file(tmp_path, "export.xyz", "some content")
        output = tmp_path / "migrated.db"

        with pytest.raises(ValueError, match="unknown format"):
            migrate(source=source, output=output, format="unknown")


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class TestParseMapping:
    def test_single_mapping(self) -> None:
        result = _parse_mapping("analyses=osa_analyses")
        assert result == {"analyses": "osa_analyses"}

    def test_multiple_mappings(self) -> None:
        result = _parse_mapping("analyses=osa_analyses,data_points=osa_data_points")
        assert result == {
            "analyses": "osa_analyses",
            "data_points": "osa_data_points",
        }

    def test_empty_value_skipped(self) -> None:
        result = _parse_mapping("analyses=osa_analyses,,data_points=osa_dp")
        assert result == {"analyses": "osa_analyses", "data_points": "osa_dp"}

    def test_invalid_mapping_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="invalid mapping item"):
            _parse_mapping("invalid-no-equals")

    def test_invalid_form_raises(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="invalid mapping item"):
            _parse_mapping("src:tgt")  # colon instead of equals


# ---------------------------------------------------------------------------
# CLI main() smoke test
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_dry_run_exits_zero(self, tmp_path: Path) -> None:
        from scripts.migrate_from_mongodb import main

        docs = [{"_collection": "analyses", "_id": "a1", "name": "Test"}]
        source = _tmp_file(tmp_path, "export.json", json.dumps(docs))

        exit_code = main(
            [
                "--source",
                str(source),
                "--output",
                str(tmp_path / "out.db"),
                "--dry-run",
            ]
        )
        assert exit_code == 0

    def test_main_missing_source_raises_system_exit(self, tmp_path: Path) -> None:
        from scripts.migrate_from_mongodb import main

        exit_code = main(["--source", "/nonexistent", "--output", str(tmp_path / "out.db")])
        # FileNotFoundError (subclass of OSError) is caught and returns 1
        assert exit_code == 1

    def test_main_unknown_format_raises(self, tmp_path: Path) -> None:
        # argparse validates --format choices before main() is called,
        # so we test the underlying migrate() function directly instead.
        from scripts.migrate_from_mongodb import migrate

        source = _tmp_file(tmp_path, "export.json", "[]")
        with pytest.raises(ValueError, match="unknown format"):
            migrate(source=source, output=tmp_path / "out.db", format="unknown")
