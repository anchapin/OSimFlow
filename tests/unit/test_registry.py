"""Tests for the campaign registry (issue #266).

Covers: CampaignRegistry CRUD, CampaignRecord serialization, CLI
subcommands, and auto-registration in Campaign.run().
"""

import time

import pytest

from osimflow.campaign import Campaign
from osimflow.config import CampaignConfig
from osimflow.executors import LocalExecutor
from osimflow.registry import CampaignRecord, CampaignRegistry


@pytest.fixture
def registry(tmp_path):
    """Provide a CampaignRegistry backed by a temp database."""
    db_path = tmp_path / "test_registry.db"
    return CampaignRegistry(db_path=db_path)


class TestCampaignRegistry:
    """Core registry CRUD operations."""

    def test_register_and_get(self, registry: CampaignRegistry) -> None:
        registry.register(
            "test-campaign-001",
            name="Test Campaign",
            outdir="/tmp/results",
            algorithm="lhs",
            n_samples=10,
            executor="local",
            openstudio_version="3.11.0",
        )
        record = registry.get_campaign("test-campaign-001")
        assert record is not None
        assert record.id == "test-campaign-001"
        assert record.name == "Test Campaign"
        assert record.outdir == "/tmp/results"
        assert record.status == "running"
        assert record.algorithm == "lhs"
        assert record.n_samples == 10
        assert record.executor == "local"
        assert record.openstudio_version == "3.11.0"
        assert record.completed_at is None

    def test_get_nonexistent(self, registry: CampaignRegistry) -> None:
        assert registry.get_campaign("does-not-exist") is None

    def test_list_campaigns_empty(self, registry: CampaignRegistry) -> None:
        campaigns = registry.list_campaigns()
        assert campaigns == []

    def test_list_campaigns_ordered_newest_first(self, registry: CampaignRegistry) -> None:
        registry.register("campaign-old", outdir="/tmp/old")
        time.sleep(0.01)
        registry.register("campaign-new", outdir="/tmp/new")
        campaigns = registry.list_campaigns()
        assert len(campaigns) == 2
        assert campaigns[0].id == "campaign-new"
        assert campaigns[1].id == "campaign-old"

    def test_list_campaigns_filter_by_status(self, registry: CampaignRegistry) -> None:
        registry.register("c1", outdir="/tmp/1", status="success")
        registry.register("c2", outdir="/tmp/2", status="running")
        registry.register("c3", outdir="/tmp/3", status="failure")

        running = registry.list_campaigns(status="running")
        assert len(running) == 1
        assert running[0].id == "c2"

        successes = registry.list_campaigns(status="success")
        assert len(successes) == 1
        assert successes[0].id == "c1"

    def test_update_status_to_success(self, registry: CampaignRegistry) -> None:
        registry.register("c1", outdir="/tmp/1", status="running")
        registry.update_status("c1", "success")
        record = registry.get_campaign("c1")
        assert record is not None
        assert record.status == "success"
        assert record.completed_at is not None
        assert record.completed_at > 0

    def test_update_status_to_failure(self, registry: CampaignRegistry) -> None:
        registry.register("c1", outdir="/tmp/1", status="running")
        registry.update_status("c1", "failure")
        record = registry.get_campaign("c1")
        assert record is not None
        assert record.status == "failure"
        assert record.completed_at is not None

    def test_update_status_running_no_completed_at(self, registry: CampaignRegistry) -> None:
        registry.register("c1", outdir="/tmp/1", status="running")
        # Update to running (non-terminal) should not set completed_at
        registry.update_status("c1", "running")
        record = registry.get_campaign("c1")
        assert record is not None
        assert record.status == "running"
        assert record.completed_at is None

    def test_delete_campaign(self, registry: CampaignRegistry) -> None:
        registry.register("c1", outdir="/tmp/1")
        assert registry.delete_campaign("c1") is True
        assert registry.get_campaign("c1") is None
        assert registry.delete_campaign("c1") is False

    def test_register_replaces_existing(self, registry: CampaignRegistry) -> None:
        registry.register("c1", outdir="/tmp/v1", n_samples=10)
        registry.register("c1", outdir="/tmp/v2", n_samples=20)
        record = registry.get_campaign("c1")
        assert record is not None
        assert record.outdir == "/tmp/v2"
        assert record.n_samples == 20

    def test_compare_both_found(self, registry: CampaignRegistry) -> None:
        registry.register("c1", outdir="/tmp/1", algorithm="lhs", n_samples=10)
        registry.register("c2", outdir="/tmp/2", algorithm="sobol", n_samples=20)
        result = registry.compare("c1", "c2")
        assert result["left"] is not None
        assert result["right"] is not None
        assert result["left"].algorithm == "lhs"
        assert result["right"].algorithm == "sobol"

    def test_compare_one_missing(self, registry: CampaignRegistry) -> None:
        registry.register("c1", outdir="/tmp/1")
        result = registry.compare("c1", "missing")
        assert result["left"] is not None
        assert result["right"] is None

    def test_metadata_stored_as_json(self, registry: CampaignRegistry) -> None:
        meta = {"key": "value", "nested": {"a": 1}}
        registry.register("c1", outdir="/tmp/1", metadata=meta)
        record = registry.get_campaign("c1")
        assert record is not None
        assert record.metadata == meta

    def test_metadata_default_empty_dict(self, registry: CampaignRegistry) -> None:
        registry.register("c1", outdir="/tmp/1")
        record = registry.get_campaign("c1")
        assert record is not None
        assert record.metadata == {}


class TestCampaignRecord:
    """CampaignRecord serialization."""

    def test_to_dict(self) -> None:
        record = CampaignRecord(
            id="test",
            name="Test",
            outdir="/tmp",
            status="success",
            algorithm="lhs",
            n_samples=10,
            executor="local",
            openstudio_version="3.11.0",
            config_hash="abc123",
            created_at=1000.0,
            completed_at=2000.0,
            metadata={"key": "val"},
        )
        d = record.to_dict()
        assert d["id"] == "test"
        assert d["status"] == "success"
        assert d["metadata"] == {"key": "val"}

    def test_from_row_round_trip(self, registry: CampaignRegistry) -> None:
        meta = {"dry_run": True, "generations": 3}
        registry.register(
            "round-trip",
            name="Round Trip",
            outdir="/tmp/rt",
            status="success",
            algorithm="nsga2",
            n_samples=50,
            executor="slurm",
            openstudio_version="3.9.0",
            config_hash="deadbeef",
            metadata=meta,
        )
        registry.update_status("round-trip", "success")
        record = registry.get_campaign("round-trip")
        assert record is not None
        assert record.name == "Round Trip"
        assert record.algorithm == "nsga2"
        assert record.n_samples == 50
        assert record.metadata == meta
        assert record.config_hash == "deadbeef"


class TestAutoRegistration:
    """Auto-registration when Campaign.run() is called."""

    def test_campaign_auto_registers_on_init(self, tmp_path) -> None:
        """Campaign.__init__ should create a registry reference."""
        reg_path = tmp_path / "auto_reg.db"
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            "variables:\n  - name: test_var\n    distribution: uniform\n    min: 0\n    max: 1\n"
        )
        template_dir = tmp_path / "template"
        template_dir.mkdir()
        (template_dir / "workflow.osw").write_text('{"seed_file": "model.osm"}')
        outdir = tmp_path / "results"

        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_dir,
            n_samples=2,
            outdir=outdir,
            openstudio_version="3.11.0",
            algorithm="lhs",
            registry_path=reg_path,
        )
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(cfg, executor, max_workers=1)

        # Verify registry was initialized
        assert campaign._registry is not None
        assert reg_path.exists()

    def test_register_and_update_helpers(self, tmp_path) -> None:
        """Test _register_campaign and _update_registry_status directly."""
        reg_path = tmp_path / "auto_reg2.db"
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            "variables:\n  - name: test_var\n    distribution: uniform\n    min: 0\n    max: 1\n"
        )
        template_dir = tmp_path / "template"
        template_dir.mkdir()
        (template_dir / "workflow.osw").write_text('{"seed_file": "model.osm"}')
        outdir = tmp_path / "results"

        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_dir,
            n_samples=5,
            outdir=outdir,
            openstudio_version="3.11.0",
            algorithm="sobol",
            registry_path=reg_path,
        )
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(cfg, executor, max_workers=1)

        # Call the register helper directly
        campaign._register_campaign()

        reg = CampaignRegistry(db_path=reg_path)
        record = reg.get_campaign(campaign.trace.campaign_id)
        assert record is not None
        assert record.status == "running"
        assert record.algorithm == "sobol"
        assert record.n_samples == 5
        assert record.completed_at is None

        # Call the update helper
        campaign._update_registry_status("success")
        record = reg.get_campaign(campaign.trace.campaign_id)
        assert record is not None
        assert record.status == "success"
        assert record.completed_at is not None

    def test_registry_handles_none_gracefully(self, tmp_path) -> None:
        """When registry_path is None, Campaign should still work."""
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            "variables:\n  - name: test_var\n    distribution: uniform\n    min: 0\n    max: 1\n"
        )
        template_dir = tmp_path / "template"
        template_dir.mkdir()
        (template_dir / "workflow.osw").write_text('{"seed_file": "model.osm"}')
        outdir = tmp_path / "results"

        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_dir,
            n_samples=2,
            outdir=outdir,
            openstudio_version="3.11.0",
            registry_path=None,  # default path
        )
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(cfg, executor, max_workers=1)

        # Registry should be initialized (uses default path)
        assert campaign._registry is not None


class TestDefaultRegistryPath:
    """Test default_registry_path resolution."""

    def test_default_path(self, monkeypatch) -> None:
        from osimflow.registry import default_registry_path

        monkeypatch.delenv("OSIMFLOW_REGISTRY", raising=False)
        path = default_registry_path()
        assert path == path.home() / ".osimflow" / "registry.db"

    def test_env_override(self, monkeypatch) -> None:
        from osimflow.registry import default_registry_path

        monkeypatch.setenv("OSIMFLOW_REGISTRY", "/custom/path/registry.db")
        path = default_registry_path()
        assert str(path) == "/custom/path/registry.db"
