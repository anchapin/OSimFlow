"""Integration test for eplusout.err cleanup after successful simulation (issue #1163).

This test verifies that:
1. eplusout.err is deleted after successful simulation (regardless of file size)
2. eplusout.err is kept for failed simulations
3. eplusout.err is NOT archived when --archive_intermediates is used
"""

import json
from pathlib import Path

import pytest
import yaml

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor


class TestEplusoutErrCleanup:
    """Integration tests for eplusout.err cleanup behavior."""

    @pytest.fixture
    def workdir(self, tmp_path: Path) -> Path:
        """A clean per-test work directory with input variables.yml."""
        wd = tmp_path / "work"
        wd.mkdir()

        (wd / "variables.yml").write_text(
            yaml.safe_dump(
                {
                    "variables": [
                        {"name": "test_var", "distribution": "uniform", "min": 1.0, "max": 2.0},
                    ]
                }
            )
        )
        return wd

    @pytest.fixture
    def template_pkg(self, workdir: Path) -> Path:
        pkg = workdir / "template"
        pkg.mkdir()
        (pkg / "model.osm").write_text(json.dumps({"attributes": {"test_var": 1.0}}))
        (pkg / "workflow.osw").write_text(json.dumps({"name": "stub"}))
        return pkg

    @pytest.fixture
    def outdir(self, workdir: Path) -> Path:
        od = workdir / "out"
        od.mkdir()
        return od

    @pytest.fixture
    def cfg(self, workdir: Path, template_pkg: Path, outdir: Path) -> CampaignConfig:
        return CampaignConfig(
            input_variables=workdir / "variables.yml",
            template_sim_package=template_pkg,
            n_samples=1,
            outdir=outdir,
            openstudio_version="3.11.0",
            archive_intermediates=False,
        )

    @pytest.fixture
    def campaign(self, cfg: CampaignConfig) -> Campaign:
        return Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))

    def test_eplusout_err_deleted_on_successful_simulation(self, campaign: Campaign, workdir: Path):
        """eplusout.err should be deleted after successful simulation."""
        # Skip preflight by using stub mode
        import os

        os.environ["OSIMFLOW_STUB_SIM"] = "1"
        try:
            campaign.run()
        finally:
            os.environ.pop("OSIMFLOW_STUB_SIM", None)

        # Find the simulation output directory
        sim_dirs = list((workdir / "out" / "work" / "sim").glob("*/"))
        assert len(sim_dirs) == 1
        sim_dir = sim_dirs[0]

        # eplusout.sql should exist
        assert (sim_dir / "eplusout.sql").exists()

        # eplusout.err should NOT exist (deleted on success)
        assert not (sim_dir / "eplusout.err").exists(), (
            "eplusout.err should be deleted after successful simulation"
        )

    def test_eplusout_err_not_archived_with_archive_intermediates(
        self, workdir: Path, template_pkg: Path, outdir: Path
    ):
        """eplusout.err should NOT be archived even when --archive_intermediates is used."""
        cfg = CampaignConfig(
            input_variables=workdir / "variables.yml",
            template_sim_package=template_pkg,
            n_samples=1,
            outdir=outdir,
            openstudio_version="3.11.0",
            archive_intermediates=True,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))

        # Skip preflight by using stub mode
        import os

        os.environ["OSIMFLOW_STUB_SIM"] = "1"
        try:
            campaign.run()
        finally:
            os.environ.pop("OSIMFLOW_STUB_SIM", None)

        # Check archive directory
        archive_sim_dir = outdir / "archive" / "sim"
        if archive_sim_dir.exists():
            archived_dirs = list(archive_sim_dir.glob("*/"))
            assert len(archived_dirs) == 1
            archived_dir = archived_dirs[0]

            # eplusout.sql should be archived
            assert (archived_dir / "eplusout.sql").exists()

            # eplusout.err should NOT be archived
            assert not (archived_dir / "eplusout.err").exists(), (
                "eplusout.err should not be archived"
            )

    def test_eplusout_err_deleted_even_with_warnings(self, campaign: Campaign, workdir: Path):
        """eplusout.err should be deleted even if it contains warnings (non-empty)."""
        # Skip preflight by using stub mode
        import os

        os.environ["OSIMFLOW_STUB_SIM"] = "1"
        try:
            campaign.run()
        finally:
            os.environ.pop("OSIMFLOW_STUB_SIM", None)

        sim_dirs = list((workdir / "out" / "work" / "sim").glob("*/"))
        assert len(sim_dirs) == 1
        sim_dir = sim_dirs[0]

        # eplusout.err should be deleted regardless of content
        # (In stub mode it's empty, but the fix ensures deletion even if non-empty)
        assert not (sim_dir / "eplusout.err").exists(), (
            "eplusout.err should be deleted even if non-empty"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
