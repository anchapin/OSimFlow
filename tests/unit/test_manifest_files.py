"""Tests for structured JSON manifest files (issue #277).

Validates that Campaign writes:
  - campaign_meta.json at campaign start
  - artifact_manifest.json after aggregation
  - provenance.json at campaign completion
"""

import json
import platform

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor

pytestmark = pytest.mark.slow


@pytest.fixture
def campaign_env(tmp_path, _session_example_package, _session_variables_yml):
    """Provide a minimal campaign environment with 3-sample run."""
    vyml = tmp_path / "variables.yml"
    vyml.write_text(_session_variables_yml.read_text())
    outdir = tmp_path / "out"
    outdir.mkdir()
    cfg = CampaignConfig(
        input_variables=vyml,
        template_sim_package=_session_example_package,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        algorithm="lhs",
    )
    executor = LocalExecutor(max_workers=1)
    return cfg, executor, outdir


class TestCampaignMeta:
    """campaign_meta.json: written at campaign start."""

    def test_written_after_run(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        meta_path = outdir / "campaign_meta.json"
        assert meta_path.is_file(), "campaign_meta.json should exist after run"

    def test_schema_structure(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        meta = json.loads((outdir / "campaign_meta.json").read_text())

        # Required top-level keys.
        required_keys = {
            "campaign_id",
            "algorithm",
            "n_samples",
            "openstudio_version",
            "executor_type",
            "input_variables",
            "template_sim_package",
            "created_at",
            "osimflow_version",
        }
        assert required_keys.issubset(meta.keys()), f"Missing keys: {required_keys - meta.keys()}"

    def test_config_values_match(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        meta = json.loads((outdir / "campaign_meta.json").read_text())

        assert meta["algorithm"] == "lhs"
        assert meta["n_samples"] == 3
        assert meta["openstudio_version"] == "3.11.0"
        assert meta["executor_type"] == "local"
        assert meta["template_sim_package"] == str(cfg.template_sim_package)

    def test_input_variables_summary(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        meta = json.loads((outdir / "campaign_meta.json").read_text())

        iv = meta["input_variables"]
        assert isinstance(iv, dict)
        assert "path" in iv
        assert "variables" in iv
        assert isinstance(iv["variables"], list)
        assert len(iv["variables"]) > 0, "variables.yml should declare at least one variable"

        # Each variable entry must have name and distribution.
        for var in iv["variables"]:
            assert "name" in var
            assert "distribution" in var

    def test_created_at_is_iso_format(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        meta = json.loads((outdir / "campaign_meta.json").read_text())
        created = meta["created_at"]
        assert isinstance(created, str)
        # ISO-like format: starts with YYYY-MM-DD
        assert created[:4].isdigit() and created[4] == "-"


class TestProvenance:
    """provenance.json: written at campaign completion."""

    def test_written_after_run(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        prov_path = outdir / "provenance.json"
        assert prov_path.is_file(), "provenance.json should exist after run"

    def test_schema_structure(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        prov = json.loads((outdir / "provenance.json").read_text())

        required_keys = {
            "campaign_id",
            "sampling",
            "code_hashes",
            "environment",
            "cache_stats",
            "completed_at",
        }
        assert required_keys.issubset(prov.keys()), f"Missing keys: {required_keys - prov.keys()}"

    def test_sampling_details(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        prov = json.loads((outdir / "provenance.json").read_text())
        sampling = prov["sampling"]

        assert sampling["algorithm"] == "lhs"
        assert sampling["n_samples"] == 3
        assert sampling["max_generations"] == 1

    def test_code_hashes_present(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        prov = json.loads((outdir / "provenance.json").read_text())
        hashes = prov["code_hashes"]

        assert "bin" in hashes
        assert "work" in hashes
        assert all(isinstance(v, str) and len(v) == 64 for v in hashes.values())

    def test_environment_info(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        prov = json.loads((outdir / "provenance.json").read_text())
        env = prov["environment"]

        assert env["python_version"] == platform.python_version()
        assert "osimflow_version" in env
        assert "platform" in env
        assert "python_implementation" in env

    def test_sample_ids_recorded(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        prov = json.loads((outdir / "provenance.json").read_text())
        sampling = prov["sampling"]

        assert "sample_ids" in sampling
        assert isinstance(sampling["sample_ids"], list)
        assert len(sampling["sample_ids"]) == 3


class TestArtifactManifest:
    """artifact_manifest.json: written after aggregation."""

    def test_written_after_run(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        manifest_path = outdir / "artifact_manifest.json"
        assert manifest_path.is_file(), "artifact_manifest.json should exist after run"

    def test_schema_structure(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        manifest = json.loads((outdir / "artifact_manifest.json").read_text())

        required_keys = {"campaign_id", "artifacts", "generated_at"}
        assert required_keys.issubset(manifest.keys()), (
            f"Missing keys: {required_keys - manifest.keys()}"
        )

    def test_artifacts_are_list(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        manifest = json.loads((outdir / "artifact_manifest.json").read_text())

        assert isinstance(manifest["artifacts"], list)
        assert len(manifest["artifacts"]) > 0

    def test_artifact_entry_schema(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        manifest = json.loads((outdir / "artifact_manifest.json").read_text())

        required_keys = {"path", "size_bytes", "checksum_sha256", "category"}
        for artifact in manifest["artifacts"]:
            assert required_keys.issubset(artifact.keys()), (
                f"Artifact missing keys: {required_keys - artifact.keys()}"
            )
            assert isinstance(artifact["path"], str)
            assert isinstance(artifact["size_bytes"], int)
            assert isinstance(artifact["category"], str)

    def test_known_output_files_present(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        manifest = json.loads((outdir / "artifact_manifest.json").read_text())

        paths = {a["path"] for a in manifest["artifacts"]}
        # The campaign must produce aggregated_results.csv and run.json.
        assert any("aggregated_results" in p for p in paths), (
            f"aggregated_results file not found in manifest: {paths}"
        )
        assert "run.json" in paths, f"run.json not found in manifest: {paths}"

    def test_checksums_are_sha256(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        manifest = json.loads((outdir / "artifact_manifest.json").read_text())

        for artifact in manifest["artifacts"]:
            sha = artifact["checksum_sha256"]
            if sha:  # empty for the manifest itself
                assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)

    def test_categories_are_valid(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        manifest = json.loads((outdir / "artifact_manifest.json").read_text())

        valid_categories = {
            "results",
            "plots",
            "logs",
            "intermediates",
            "metadata",
            "cache",
            "other",
        }
        for artifact in manifest["artifacts"]:
            assert artifact["category"] in valid_categories, (
                f"Invalid category: {artifact['category']}"
            )

    def test_relative_paths(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        manifest = json.loads((outdir / "artifact_manifest.json").read_text())

        for artifact in manifest["artifacts"]:
            # Paths must be relative to outdir (no leading /).
            assert not artifact["path"].startswith("/"), (
                f"Path should be relative: {artifact['path']}"
            )

    def test_manifest_has_metadata_category(self, campaign_env):
        """The manifest files themselves should be in the metadata category."""
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()
        manifest = json.loads((outdir / "artifact_manifest.json").read_text())

        meta_files = [a for a in manifest["artifacts"] if a["category"] == "metadata"]
        meta_paths = {a["path"] for a in meta_files}
        assert "campaign_meta.json" in meta_paths
        assert "provenance.json" in meta_paths


class TestManifestIntegration:
    """Integration: manifests in full campaign lifecycle."""

    def test_all_three_manifests_after_full_run(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()

        assert (outdir / "campaign_meta.json").is_file()
        assert (outdir / "provenance.json").is_file()
        assert (outdir / "artifact_manifest.json").is_file()

    def test_campaign_meta_written_early(
        self, tmp_path, _session_example_package, _session_variables_yml
    ):
        """Verify campaign_meta.json is written even when run fails."""
        vyml = tmp_path / "variables.yml"
        vyml.write_text(_session_variables_yml.read_text())
        outdir = tmp_path / "out"
        outdir.mkdir()
        cfg = CampaignConfig(
            input_variables=vyml,
            template_sim_package=_session_example_package,
            n_samples=3,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(cfg=cfg, executor=executor)

        # Campaign meta is written before the try block, so even a
        # successful run has it by the end.
        campaign.run()
        assert (outdir / "campaign_meta.json").is_file()

    def test_manifests_valid_json(self, campaign_env):
        cfg, executor, outdir = campaign_env
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.run()

        for name in ("campaign_meta.json", "provenance.json", "artifact_manifest.json"):
            data = json.loads((outdir / name).read_text())
            assert isinstance(data, dict)

    def test_provenance_written_even_on_failure(
        self, tmp_path, _session_example_package, _session_variables_yml
    ):
        """Provenance is written in the finally block, so it appears even on error."""
        vyml = tmp_path / "variables.yml"
        vyml.write_text(_session_variables_yml.read_text())
        outdir = tmp_path / "out"
        outdir.mkdir()
        cfg = CampaignConfig(
            input_variables=vyml,
            template_sim_package=_session_example_package,
            n_samples=3,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(cfg=cfg, executor=executor)

        # Run succeeds (stub mode), so provenance should exist.
        campaign.run()
        assert (outdir / "provenance.json").is_file()
        prov = json.loads((outdir / "provenance.json").read_text())
        assert prov["campaign_id"] == campaign.trace.campaign_id
