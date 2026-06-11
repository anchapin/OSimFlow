"""Tests for PREFLIGHT_RUN_MODEL step (issue #107).

Covers:
  - Preflight with stub simulation passes
  - Preflight with simulated failure raises SevereEnergyPlusError
  - --skip-preflight skips the step
  - Cache key includes preflight result
  - Integration: 3-sample campaign with preflight produces all artifacts
"""

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from osimflow import Campaign, CampaignConfig, LocalExecutor, SevereEnergyPlusError
from osimflow.work import preflight_run_model


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def template_package(tmp_path: Path) -> Path:
    """Create a minimal template simulation package."""
    pkg = tmp_path / "template"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text('{"steps": []}')
    # Use the JSON-mode .osm convention so apply_params works without
    # real OpenStudio bindings (see osimflow/apply_params.py).
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"u1": 0.0}}))
    return pkg


@pytest.fixture()
def variables_yml(tmp_path: Path) -> Path:
    """Create a minimal variables.yml."""
    v = tmp_path / "variables.yml"
    v.write_text(
        "variables:\n  - name: u1\n    distribution: uniform\n    min: 0.0\n    max: 1.0\n"
    )
    return v


def _make_cfg(
    tmp_path: Path,
    template: Path,
    variables: Path,
    n_samples: int = 3,
    skip_preflight: bool = False,
) -> CampaignConfig:
    return CampaignConfig(
        input_variables=variables,
        template_sim_package=template,
        n_samples=n_samples,
        outdir=tmp_path / "results",
        openstudio_version="3.4.0",
        skip_preflight=skip_preflight,
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------
class TestPreflightStubPasses:
    """Preflight with stub simulation passes."""

    def test_stub_mode_passes(self, template_package: Path) -> None:
        """In stub mode (no openstudio.cli), preflight should succeed."""
        with mock.patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            # Should not raise
            preflight_run_model(template_package, "3.4.0")

    def test_stub_creates_no_leftover_files(self, template_package: Path) -> None:
        """The temp directory should be cleaned up after success."""
        with mock.patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            preflight_run_model(template_package, "3.4.0")
        # No preflight_package left in the template dir
        assert not (template_package / "preflight_package").exists()


class TestPreflightFailure:
    """Preflight with simulated failure raises SevereEnergyPlusError."""

    def test_real_cli_failure_raises(self, template_package: Path) -> None:
        """When openstudio.cli is available but fails, raise SevereEnergyPlusError."""
        with (
            mock.patch("osimflow.work._is_openstudio_available", return_value=True),
            mock.patch("osimflow.work._is_stub_mode", return_value=False),
            mock.patch(
                "osimflow.work.subprocess.run",
                return_value=mock.Mock(
                    returncode=1,
                    stderr="   1 * Severe  Plant loop has no components",
                    stdout="",
                ),
            ),
        ):
            with pytest.raises(SevereEnergyPlusError, match="Preflight simulation FAILED"):
                preflight_run_model(template_package, "3.4.0")

    def test_error_message_contains_severe_line(self, template_package: Path) -> None:
        """The exception message includes the first severe error line."""
        severe_msg = "   1 * Severe  ** Missing weather file"
        with (
            mock.patch("osimflow.work._is_openstudio_available", return_value=True),
            mock.patch("osimflow.work._is_stub_mode", return_value=False),
            mock.patch(
                "osimflow.work.subprocess.run",
                return_value=mock.Mock(returncode=1, stderr=severe_msg, stdout=""),
            ),
        ):
            with pytest.raises(SevereEnergyPlusError, match="Missing weather file"):
                preflight_run_model(template_package, "3.4.0")

    def test_real_cli_no_workflow_raises_runtime_error(self, template_package: Path) -> None:
        """When no workflow.osw is found, raise RuntimeError."""
        # Remove the workflow.osw from the template
        empty_pkg = template_package.parent / "empty_pkg"
        empty_pkg.mkdir()
        (empty_pkg / "model.osm").write_text("// no osw")
        with (
            mock.patch("osimflow.work._is_openstudio_available", return_value=True),
            mock.patch("osimflow.work._is_stub_mode", return_value=False),
        ):
            with pytest.raises(RuntimeError, match="No workflow.osw found"):
                preflight_run_model(empty_pkg, "3.4.0")


class TestSkipPreflight:
    """--skip-preflight skips the step."""

    def test_skip_preflight_flag(
        self,
        tmp_path: Path,
        template_package: Path,
        variables_yml: Path,
    ) -> None:
        """Campaign with skip_preflight=True should not run preflight."""
        cfg = _make_cfg(tmp_path, template_package, variables_yml, skip_preflight=True)
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(cfg, executor)

        # The step should complete without calling preflight_run_model
        # (no subprocess, no temp dir) — just a SKIPPED trace.
        campaign.step_preflight_run_model()

        # Check the trace has a SKIPPED entry
        steps = [s.step for s in campaign.trace.steps]
        assert "PREFLIGHT_RUN_MODEL" in steps
        preflight_step = next(s for s in campaign.trace.steps if s.step == "PREFLIGHT_RUN_MODEL")
        assert preflight_step.cache == "SKIPPED"


class TestPreflightCacheKey:
    """Cache key includes preflight result."""

    def test_second_run_is_cache_hit(
        self,
        tmp_path: Path,
        template_package: Path,
        variables_yml: Path,
    ) -> None:
        """Running preflight twice should cache-hit on the second call."""
        cfg = _make_cfg(tmp_path, template_package, variables_yml)
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(cfg, executor)

        with mock.patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            campaign.step_preflight_run_model()

        # First call: MISS
        steps = [s for s in campaign.trace.steps if s.step == "PREFLIGHT_RUN_MODEL"]
        assert len(steps) == 1
        assert steps[0].cache == "MISS"

        # Second call: HIT
        with mock.patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            campaign.step_preflight_run_model()

        steps = [s for s in campaign.trace.steps if s.step == "PREFLIGHT_RUN_MODEL"]
        assert len(steps) == 2
        assert steps[1].cache == "HIT"


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------
class TestPreflightIntegration:
    """Integration: 3-sample campaign with preflight produces all artifacts."""

    def test_full_campaign_with_preflight(
        self,
        tmp_path: Path,
        template_package: Path,
        variables_yml: Path,
    ) -> None:
        """A 3-sample campaign with preflight should produce all output artifacts."""
        cfg = _make_cfg(tmp_path, template_package, variables_yml, n_samples=3)
        executor = LocalExecutor(max_workers=2)
        campaign = Campaign(cfg, executor)

        with mock.patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            result = campaign.run()

        # Verify all expected artifacts
        assert "samples" in result
        assert "kpis" in result
        assert "aggregated" in result
        assert "plots" in result
        assert "run_json" in result

        # Verify run.json was written
        run_json_path = cfg.outdir / "run.json"
        assert run_json_path.exists()
        run_data = json.loads(run_json_path.read_text())
        step_names = [s["step"] for s in run_data["steps"]]
        assert "PREFLIGHT_RUN_MODEL" in step_names
        assert "GENERATE_LHS_SAMPLES" in step_names
        assert "APPLY_PARAMETERS" in step_names
        assert "RUN_OPENSTUDIO_SIM" in step_names
        assert "EXTRACT_KPIS" in step_names
        assert "AGGREGATE_RESULTS" in step_names
        assert "GENERATE_BASIC_PLOTS" in step_names

        # Verify preflight step precedes APPLY_PARAMETERS
        preflight_idx = step_names.index("PREFLIGHT_RUN_MODEL")
        apply_idx = step_names.index("APPLY_PARAMETERS")
        assert preflight_idx < apply_idx

        # Verify all 3 samples succeeded
        n_ok = sum(1 for s in run_data["per_sample"] if s["status"] == "ok")
        assert n_ok == 3

        # Verify aggregated results exist
        aggregated = result["aggregated"]
        assert isinstance(aggregated, dict)
        assert aggregated["csv"] is not None
        assert aggregated["failed"] is not None


class TestExtractSevereError:
    """Test the _extract_severe_error helper."""

    def test_extracts_severe_line(self) -> None:
        from osimflow.work import _extract_severe_error

        output = (
            "EnergyPlus Completed Successfully\n"
            "   1 * Severe  Plant loop has no components\n"
            "   2 * Warning  Something minor\n"
        )
        result = _extract_severe_error(output)
        assert "Severe" in result
        assert "Plant loop" in result

    def test_no_severe_returns_empty(self) -> None:
        from osimflow.work import _extract_severe_error

        output = "EnergyPlus Completed Successfully\nNo errors."
        result = _extract_severe_error(output)
        assert result == ""

    def test_extracts_first_severe_only(self) -> None:
        from osimflow.work import _extract_severe_error

        output = "   1 * Severe  First error\n   2 * Severe  Second error\n"
        result = _extract_severe_error(output)
        assert "First error" in result
        assert "Second error" not in result
