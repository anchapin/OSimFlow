"""Tests for registry backup/export/import (issue #440).

Covers: CampaignRegistry.export_registry, import_registry, backup,
and the ``osimflow backup`` / ``osimflow restore`` CLI subcommands.
"""

from __future__ import annotations

import sqlite3

import pytest

from osimflow.__main__ import main as cli_main
from osimflow.registry import CampaignRegistry


@pytest.fixture
def registry(tmp_path: pytest.TempPathFactory) -> CampaignRegistry:
    """Provide a CampaignRegistry backed by a temp database."""
    db_path = tmp_path / "test_registry.db"  # type: ignore[operator]
    return CampaignRegistry(db_path=db_path)


@pytest.fixture
def populated_registry(registry: CampaignRegistry) -> CampaignRegistry:
    """Return a registry with a few campaigns for round-trip tests."""
    registry.register(
        "camp-001",
        name="First Campaign",
        project="Project A",
        outdir="/tmp/results-001",
        algorithm="lhs",
        n_samples=10,
        executor="local",
        openstudio_version="3.11.0",
        config_hash="abc123",
        metadata={"building_type": "office"},
    )
    registry.register(
        "camp-002",
        name="Second Campaign",
        project="Project B",
        outdir="/tmp/results-002",
        algorithm="sobol",
        n_samples=50,
        executor="slurm",
        openstudio_version="3.9.0",
        config_hash="def456",
        metadata={"building_type": "warehouse"},
    )
    registry.update_status("camp-002", "success")
    return registry


# ====================================================================== #
# export_registry
# ====================================================================== #


class TestExportRegistry:
    def test_export_creates_valid_sqlite_file(
        self, populated_registry: CampaignRegistry, tmp_path: pytest.TempPathFactory
    ) -> None:
        export_path = tmp_path / "export.db"  # type: ignore[operator]
        populated_registry.export_registry(export_path)
        assert export_path.exists()
        assert export_path.stat().st_size > 0

        # Verify it's a valid SQLite database with correct content
        conn = sqlite3.connect(str(export_path))
        rows = conn.execute("SELECT * FROM campaigns ORDER BY id").fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0][0] == "camp-001"
        assert rows[1][0] == "camp-002"

    def test_export_creates_parent_directories(
        self, registry: CampaignRegistry, tmp_path: pytest.TempPathFactory
    ) -> None:
        export_path = tmp_path / "deep" / "nested" / "dir" / "export.db"  # type: ignore[operator]
        registry.export_registry(export_path)
        assert export_path.exists()

    def test_export_overwrites_existing_file(
        self, populated_registry: CampaignRegistry, tmp_path: pytest.TempPathFactory
    ) -> None:
        export_path = tmp_path / "export.db"  # type: ignore[operator]
        export_path.write_text("placeholder")
        populated_registry.export_registry(export_path)

        conn = sqlite3.connect(str(export_path))
        rows = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()
        conn.close()
        assert rows[0] == 2

    def test_export_empty_registry(
        self, registry: CampaignRegistry, tmp_path: pytest.TempPathFactory
    ) -> None:
        export_path = tmp_path / "empty.db"  # type: ignore[operator]
        registry.export_registry(export_path)
        assert export_path.exists()

        conn = sqlite3.connect(str(export_path))
        rows = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()
        conn.close()
        assert rows[0] == 0


# ====================================================================== #
# import_registry
# ====================================================================== #


class TestImportRegistry:
    def test_import_replace_mode(
        self,
        populated_registry: CampaignRegistry,
        tmp_path: pytest.TempPathFactory,
    ) -> None:
        # Export, then replace with a fresh registry
        backup_path = tmp_path / "backup.db"  # type: ignore[operator]
        populated_registry.export_registry(backup_path)

        # New registry with one different campaign
        new_db = tmp_path / "new.db"  # type: ignore[operator]
        new_reg = CampaignRegistry(db_path=new_db)
        new_reg.register("camp-new", outdir="/tmp/new", n_samples=99)
        assert len(new_reg.list_campaigns()) == 1

        # Import replaces all
        count = new_reg.import_registry(backup_path, merge=False)
        assert count == 2
        campaigns = new_reg.list_campaigns()
        assert len(campaigns) == 2
        ids = {c.id for c in campaigns}
        assert ids == {"camp-001", "camp-002"}
        assert new_reg.get_campaign("camp-new") is None

    def test_import_merge_mode(
        self,
        populated_registry: CampaignRegistry,
        tmp_path: pytest.TempPathFactory,
    ) -> None:
        backup_path = tmp_path / "backup.db"  # type: ignore[operator]
        populated_registry.export_registry(backup_path)

        # New registry with one different campaign
        new_db = tmp_path / "new.db"  # type: ignore[operator]
        new_reg = CampaignRegistry(db_path=new_db)
        new_reg.register("camp-new", outdir="/tmp/new", n_samples=99)

        # Import merges
        count = new_reg.import_registry(backup_path, merge=True)
        assert count == 2
        campaigns = new_reg.list_campaigns()
        assert len(campaigns) == 3
        ids = {c.id for c in campaigns}
        assert ids == {"camp-001", "camp-002", "camp-new"}

    def test_import_merge_overwrites_same_id(
        self,
        populated_registry: CampaignRegistry,
        tmp_path: pytest.TempPathFactory,
    ) -> None:
        """When merging, a record with the same id in the backup
        overwrites the existing one."""
        backup_path = tmp_path / "backup.db"  # type: ignore[operator]
        populated_registry.export_registry(backup_path)

        # New registry with camp-001 but different data
        new_db = tmp_path / "new.db"  # type: ignore[operator]
        new_reg = CampaignRegistry(db_path=new_db)
        new_reg.register("camp-001", outdir="/tmp/overridden", n_samples=999)

        # Merge: camp-001 should be overwritten by backup data
        count = new_reg.import_registry(backup_path, merge=True)
        assert count == 2

        record = new_reg.get_campaign("camp-001")
        assert record is not None
        assert record.outdir == "/tmp/results-001"
        assert record.n_samples == 10

    def test_import_nonexistent_file_raises(
        self, registry: CampaignRegistry, tmp_path: pytest.TempPathFactory
    ) -> None:
        bad_path = tmp_path / "nonexistent.db"  # type: ignore[operator]
        with pytest.raises(FileNotFoundError):
            registry.import_registry(bad_path)

    def test_import_invalid_sqlite_raises(
        self, registry: CampaignRegistry, tmp_path: pytest.TempPathFactory
    ) -> None:
        bad_path = tmp_path / "not_a_db.db"  # type: ignore[operator]
        bad_path.write_text("this is not sqlite")
        with pytest.raises(sqlite3.DatabaseError):
            registry.import_registry(bad_path)

    def test_import_file_without_campaigns_table_raises(
        self, registry: CampaignRegistry, tmp_path: pytest.TempPathFactory
    ) -> None:
        empty_db = tmp_path / "wrong_schema.db"  # type: ignore[operator]
        conn = sqlite3.connect(str(empty_db))
        conn.execute("CREATE TABLE other_table (id TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(ValueError, match="campaigns"):
            registry.import_registry(empty_db)


# ====================================================================== #
# backup
# ====================================================================== #


class TestBackup:
    def test_backup_default_output_dir(self, populated_registry: CampaignRegistry) -> None:
        backup_path = populated_registry.backup()
        assert backup_path.exists()
        assert backup_path.parent == populated_registry.db_path.parent / "backups"
        assert backup_path.name.startswith("registry_backup_")
        assert backup_path.suffix == ".db"

    def test_backup_custom_output_dir(
        self,
        populated_registry: CampaignRegistry,
        tmp_path: pytest.TempPathFactory,
    ) -> None:
        custom_dir = tmp_path / "custom_backups"  # type: ignore[operator]
        backup_path = populated_registry.backup(output_dir=custom_dir)
        assert backup_path.exists()
        assert backup_path.parent == custom_dir

    def test_backup_preserves_data(self, populated_registry: CampaignRegistry) -> None:
        backup_path = populated_registry.backup()

        # Restore into a fresh registry and verify data integrity
        new_db = backup_path.parent / "restored.db"
        new_reg = CampaignRegistry(db_path=new_db)
        new_reg.import_registry(backup_path, merge=False)

        for expected_id in ("camp-001", "camp-002"):
            original = populated_registry.get_campaign(expected_id)
            restored = new_reg.get_campaign(expected_id)
            assert original is not None
            assert restored is not None
            assert original.id == restored.id
            assert original.name == restored.name
            assert original.outdir == restored.outdir
            assert original.algorithm == restored.algorithm
            assert original.n_samples == restored.n_samples
            assert original.metadata == restored.metadata

    def test_backup_filename_has_timestamp(self, populated_registry: CampaignRegistry) -> None:
        """The backup filename must contain a timestamp."""
        backup_path = populated_registry.backup()
        # registry_backup_YYYYMMDD_HHMMSS.db
        name = backup_path.stem  # registry_backup_YYYYMMDD_HHMMSS
        parts = name.split("_")
        assert len(parts) == 4  # registry, backup, date, time
        assert parts[0] == "registry"
        assert parts[1] == "backup"
        # date should be 8 digits, time should be 6 digits
        assert len(parts[2]) == 8
        assert len(parts[3]) == 6


# ====================================================================== #
# Round-trip
# ====================================================================== #


class TestExportImportRoundTrip:
    def test_full_round_trip_preserves_all_fields(
        self,
        populated_registry: CampaignRegistry,
        tmp_path: pytest.TempPathFactory,
    ) -> None:
        """Export -> import into a fresh registry preserves all fields."""
        export_path = tmp_path / "roundtrip.db"  # type: ignore[operator]
        populated_registry.export_registry(export_path)

        new_db = tmp_path / "roundtrip_restored.db"  # type: ignore[operator]
        new_reg = CampaignRegistry(db_path=new_db)
        new_reg.import_registry(export_path)

        for campaign_id in ("camp-001", "camp-002"):
            original = populated_registry.get_campaign(campaign_id)
            restored = new_reg.get_campaign(campaign_id)
            assert original is not None
            assert restored is not None
            # Compare every field
            assert original.id == restored.id
            assert original.name == restored.name
            assert original.project == restored.project
            assert original.outdir == restored.outdir
            assert original.status == restored.status
            assert original.algorithm == restored.algorithm
            assert original.n_samples == restored.n_samples
            assert original.executor == restored.executor
            assert original.openstudio_version == restored.openstudio_version
            assert original.config_hash == restored.config_hash
            assert original.created_at == pytest.approx(restored.created_at)
            assert original.metadata == restored.metadata
            if original.completed_at is not None:
                assert restored.completed_at is not None
                assert restored.completed_at == pytest.approx(original.completed_at)
            else:
                assert restored.completed_at is None


# ====================================================================== #
# CLI subcommands
# ====================================================================== #


class TestCliBackupRestore:
    def test_cli_backup_default_output(
        self,
        populated_registry: CampaignRegistry,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``osimflow backup --registry <path>`` produces a valid backup."""
        rc = cli_main(["backup", "--registry", str(populated_registry.db_path)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Backup created:" in captured.out

        # Find the backup path from output and verify it
        backup_line = captured.out.strip().split("Backup created: ")[-1].strip()
        import pathlib

        backup_path = pathlib.Path(backup_line)
        assert backup_path.exists()

    def test_cli_backup_custom_output(
        self,
        populated_registry: CampaignRegistry,
        tmp_path: pytest.TempPathFactory,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``osimflow backup --registry X --output Y`` writes to Y."""
        output_path = tmp_path / "custom_backup.db"  # type: ignore[operator]
        rc = cli_main(
            [
                "backup",
                "--registry",
                str(populated_registry.db_path),
                "--output",
                str(output_path),
            ]
        )
        assert rc == 0
        assert output_path.exists()

    def test_cli_restore_replace(
        self,
        populated_registry: CampaignRegistry,
        tmp_path: pytest.TempPathFactory,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``osimflow restore <file> --registry <db>`` replaces the registry."""
        # Create backup
        backup_path = tmp_path / "cli_backup.db"  # type: ignore[operator]
        populated_registry.export_registry(backup_path)

        # Fresh registry with different data
        target_db = tmp_path / "target.db"  # type: ignore[operator]
        target_reg = CampaignRegistry(db_path=target_db)
        target_reg.register("local-only", outdir="/tmp/local")
        assert len(target_reg.list_campaigns()) == 1

        # Restore (replace mode)
        rc = cli_main(["restore", str(backup_path), "--registry", str(target_db)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "replaced" in captured.out
        assert "2 campaign(s)" in captured.out

        # Verify: local-only should be gone, camp-001 and camp-002 present
        campaigns = target_reg.list_campaigns()
        assert len(campaigns) == 2
        assert target_reg.get_campaign("local-only") is None
        assert target_reg.get_campaign("camp-001") is not None

    def test_cli_restore_merge(
        self,
        populated_registry: CampaignRegistry,
        tmp_path: pytest.TempPathFactory,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``osimflow restore <file> --registry <db> --merge`` merges."""
        backup_path = tmp_path / "cli_backup.db"  # type: ignore[operator]
        populated_registry.export_registry(backup_path)

        target_db = tmp_path / "target.db"  # type: ignore[operator]
        target_reg = CampaignRegistry(db_path=target_db)
        target_reg.register("local-only", outdir="/tmp/local")

        rc = cli_main(
            [
                "restore",
                str(backup_path),
                "--registry",
                str(target_db),
                "--merge",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "merged" in captured.out

        campaigns = target_reg.list_campaigns()
        assert len(campaigns) == 3
        assert target_reg.get_campaign("local-only") is not None
        assert target_reg.get_campaign("camp-001") is not None

    def test_cli_restore_nonexistent_file(
        self,
        registry: CampaignRegistry,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Restore with a non-existent backup file returns error."""
        rc = cli_main(
            [
                "restore",
                "/nonexistent/backup.db",
                "--registry",
                str(registry.db_path),
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "error" in captured.err.lower()
