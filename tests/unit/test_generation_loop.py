"""Tests for the generation loop (issue #122).

Verifies:
  * LHS with max_generations=1 produces identical results to before.
  * A mock 3-generation algorithm produces 3×N cache entries.
  * Cache keys differ across generations for the same sample_id.
  * Algorithm convergence stops the loop early.
  * max_generations < 1 raises ValueError.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from osimflow.algorithms import BaseAlgorithm
from osimflow.cache import CacheKey, SQLiteCache
from osimflow.campaign import Campaign
from osimflow.config import CampaignConfig
from osimflow.executors import LocalExecutor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create (template_sim_package, variables_yml, outdir)."""
    template = tmp_path / "template"
    template.mkdir()
    # Minimal OSW so the campaign doesn't crash during preflight.
    (template / "workflow.osw").write_text(json.dumps({"steps": [], "seed_model": "model.osm"}))
    (template / "model.osm").write_text(json.dumps({"attributes": {"wall_r": 5.0}}))

    variables = tmp_path / "variables.yml"
    variables.write_text(
        yaml.dump(
            {
                "variables": [
                    {
                        "name": "wall_r",
                        "distribution": "uniform",
                        "min": 1.0,
                        "max": 10.0,
                    }
                ]
            }
        )
    )

    outdir = tmp_path / "results"
    outdir.mkdir()

    return template, variables, outdir


def _make_cfg(
    template: Path,
    variables: Path,
    outdir: Path,
    *,
    n_samples: int = 3,
    max_generations: int = 1,
    algorithm: str = "lhs",
) -> CampaignConfig:
    """Build a minimal CampaignConfig for testing."""
    return CampaignConfig(
        input_variables=variables,
        template_sim_package=template,
        n_samples=n_samples,
        outdir=outdir,
        openstudio_version="3.4.0",
        skip_preflight=True,
        max_generations=max_generations,
        algorithm=algorithm,
    )


# ---------------------------------------------------------------------------
# Mock iterative algorithm
# ---------------------------------------------------------------------------


class MockIterativeAlgorithm(BaseAlgorithm):
    """A mock iterative algorithm that runs for exactly *max_gens* generations.

    Produces a distinct sample set each generation by varying the seed
    suffix.  Converges once *max_gens* generations have been observed.
    """

    def __init__(self, max_gens: int = 3) -> None:
        self._max_gens = max_gens
        self._call_count = 0

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        outdir.mkdir(parents=True, exist_ok=True)
        self._call_count += 1
        suffix = f"_gen{self._call_count}"
        samples = [
            {
                "sample_id": f"{i + 1:04d}{suffix}",
                "values": {"wall_r": float(i + 1)},
            }
            for i in range(n_samples)
        ]
        path = outdir / "samples.json"
        path.write_text(json.dumps({"samples": samples}, indent=2))
        return path

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return new samples from the last generation's results."""
        if not history:
            return []
        n = len(history[-1].get("samples", []))
        suffix = f"_gen{len(history) + 1}"
        return [
            {
                "sample_id": f"{i + 1:04d}{suffix}",
                "values": {"wall_r": float(i + 1) * 0.5},
            }
            for i in range(n)
        ]

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        return len(history) >= self._max_gens

    def name(self) -> str:
        return "mock_iterative"

    def is_iterative(self) -> bool:
        return True


class EarlyConvergingAlgorithm(MockIterativeAlgorithm):
    """Converges after 2 generations regardless of max_generations."""

    def __init__(self) -> None:
        super().__init__(max_gens=2)

    def name(self) -> str:
        return "early_converge"


# ---------------------------------------------------------------------------
# Helper: run a campaign with stub work functions
# ---------------------------------------------------------------------------


def _stub_apply(
    template: Path,
    params: dict[str, object],
    sample_id: str,
    out_dir: Path,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "modified_package").mkdir()
    return out_dir


def _stub_extract(sim_dir: Path, sample_id: str, kpi_dir: Path) -> Path:
    kpi_dir.mkdir(parents=True, exist_ok=True)
    kpi_path = kpi_dir / f"kpi_{sample_id}.json"
    kpi_path.write_text(json.dumps({"sample_id": sample_id, "kpis": {"eui": 100.0}}))
    return kpi_path


def _run_campaign(
    cfg: CampaignConfig,
    *,
    apply_fn: Callable[..., Path] | None = None,
    extract_fn: Callable[..., Path] | None = None,
) -> Campaign:
    executor = LocalExecutor(max_workers=1)
    campaign = Campaign(
        cfg,
        executor,
        apply_fn=apply_fn or _stub_apply,
        extract_fn=extract_fn or _stub_extract,
    )
    campaign.run()
    return campaign


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLHSBackwardCompatible:
    """max_generations=1 (default) must produce identical results."""

    def test_single_generation_lhs(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        template, variables, outdir = tmp_dirs
        cfg = _make_cfg(template, variables, outdir, n_samples=3)
        campaign = _run_campaign(cfg)

        # Exactly 3 samples generated (LHS is single-shot).
        assert len(campaign.trace.per_sample) == 3
        # All steps completed successfully.
        step_names = {s.step for s in campaign.trace.steps}
        assert "GENERATE_LHS_SAMPLES" in step_names
        assert "APPLY_PARAMETERS" in step_names
        # Cache stats should show entries.
        stats = campaign.cache.stats()
        assert stats["total"] > 0

    def test_cache_key_default_generation_zero(self) -> None:
        key = CacheKey(
            step="TEST",
            sample_id="0001",
            openstudio_version="N/A",
            inputs_sha256="abc",
            code_sha256="def",
            container_digest="img",
        )
        assert key.generation == 0


class TestGenerationCacheKeyIsolation:
    """Cache keys must differ across generations for the same sample."""

    def test_keys_differ_across_generations(self) -> None:
        base = CacheKey(
            step="APPLY_PARAMETERS",
            sample_id="0001",
            openstudio_version="N/A",
            inputs_sha256="hash1",
            code_sha256="hash2",
            container_digest="img",
            generation=0,
        )
        gen1 = CacheKey(
            step="APPLY_PARAMETERS",
            sample_id="0001",
            openstudio_version="N/A",
            inputs_sha256="hash1",
            code_sha256="hash2",
            container_digest="img",
            generation=1,
        )
        assert base != gen1
        assert hash(base) != hash(gen1)

    def test_cache_stores_and_looks_up_by_generation(self, tmp_path: Path) -> None:
        db = tmp_path / "test_cache.sqlite"
        cache = SQLiteCache(db)
        marker = tmp_path / "out_gen0"
        marker.write_text("gen0")
        key0 = CacheKey(
            step="TEST",
            sample_id="0001",
            openstudio_version="N/A",
            inputs_sha256="h",
            code_sha256="h",
            container_digest="img",
            generation=0,
        )
        cache.store(key0, marker, exit_code=0)

        # Same key fields but generation=1 should NOT hit.
        key1 = CacheKey(
            step="TEST",
            sample_id="0001",
            openstudio_version="N/A",
            inputs_sha256="h",
            code_sha256="h",
            container_digest="img",
            generation=1,
        )
        assert cache.lookup(key0) is not None
        assert cache.lookup(key1) is None

        # Store gen1 and verify independent retrieval.
        marker1 = tmp_path / "out_gen1"
        marker1.write_text("gen1")
        cache.store(key1, marker1, exit_code=0)
        assert cache.lookup(key1) == marker1
        assert cache.lookup(key0) == marker  # gen0 still intact


class TestMaxGenerationsValidation:
    """max_generations < 1 must raise."""

    def test_zero_raises(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        template, variables, outdir = tmp_dirs
        cfg = _make_cfg(template, variables, outdir, max_generations=0)
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(
            cfg,
            executor,
            apply_fn=_stub_apply,
            extract_fn=_stub_extract,
        )
        with pytest.raises(ValueError, match="max_generations must be >= 1"):
            campaign.run()

    def test_negative_raises(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        template, variables, outdir = tmp_dirs
        cfg = _make_cfg(template, variables, outdir, max_generations=-1)
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(
            cfg,
            executor,
            apply_fn=_stub_apply,
            extract_fn=_stub_extract,
        )
        with pytest.raises(ValueError, match="max_generations must be >= 1"):
            campaign.run()


class TestIterativeAlgorithmLoop:
    """A mock 3-generation iterative algorithm must produce 3×N entries."""

    def test_three_generation_produces_correct_cache_entries(
        self, tmp_dirs: tuple[Path, Path, Path]
    ) -> None:
        template, variables, outdir = tmp_dirs
        cfg = _make_cfg(
            template,
            variables,
            outdir,
            n_samples=2,
            max_generations=3,
            algorithm="mock_iterative",
        )

        # Register the mock algorithm.
        from osimflow.algorithms import AlgorithmRegistry

        AlgorithmRegistry.register("mock_iterative", MockIterativeAlgorithm)

        try:
            campaign = _run_campaign(cfg)

            stats = campaign.cache.stats()
            # We expect entries for:
            #   - 3× GENERATE (one per generation)
            #   - 3×2 APPLY (3 gens × 2 samples)
            #   - 3×2 SIM
            #   - 3×2 EXTRACT
            #   - 1× AGGREGATE (single-shot)
            #   = 3 + 6 + 6 + 6 + 1 = 22
            by_step = stats.get("by_step", {})
            assert by_step.get("GENERATE_MOCK_ITERATIVE_SAMPLES", 0) == 3
            assert by_step.get("APPLY_PARAMETERS", 0) == 6  # 3 gens × 2 samples
            assert by_step.get("RUN_OPENSTUDIO_SIM", 0) == 6
            assert by_step.get("EXTRACT_KPIS", 0) == 6
            assert by_step.get("AGGREGATE_RESULTS", 0) == 1
        finally:
            # Clean up the registry so other tests aren't affected.
            AlgorithmRegistry._registry.pop("mock_iterative", None)


class TestConvergenceStopsLoop:
    """Convergence must stop the generation loop early."""

    def test_early_convergence(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        template, variables, outdir = tmp_dirs
        cfg = _make_cfg(
            template,
            variables,
            outdir,
            n_samples=2,
            max_generations=10,  # Would do 10, but converges at 2
            algorithm="early_converge",
        )

        from osimflow.algorithms import AlgorithmRegistry

        AlgorithmRegistry.register("early_converge", EarlyConvergingAlgorithm)

        try:
            campaign = _run_campaign(cfg)

            stats = campaign.cache.stats()
            by_step = stats.get("by_step", {})
            # Should stop at 2 generations, not 10.
            gen_entries = by_step.get("GENERATE_EARLY_CONVERGE_SAMPLES", 0)
            assert gen_entries == 2, f"Expected 2 generation entries, got {gen_entries}"
            # Apply entries should be 2 gens × 2 samples = 4.
            apply_entries = by_step.get("APPLY_PARAMETERS", 0)
            assert apply_entries == 4, f"Expected 4 apply entries, got {apply_entries}"
        finally:
            AlgorithmRegistry._registry.pop("early_converge", None)


class TestLHSSingleShotBreaksLoop:
    """LHS (is_iterative=False) must break after 1 generation even if
    max_generations > 1."""

    def test_lhs_ignores_max_generations(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        template, variables, outdir = tmp_dirs
        cfg = _make_cfg(
            template,
            variables,
            outdir,
            n_samples=2,
            max_generations=5,  # Would do 5, but LHS is single-shot
            algorithm="lhs",
        )

        campaign = _run_campaign(cfg)
        stats = campaign.cache.stats()
        by_step = stats.get("by_step", {})
        # Only 1 generation's worth of entries.
        gen_entries = by_step.get("GENERATE_LHS_SAMPLES", 0)
        assert gen_entries == 1, f"Expected 1 LHS generation entry, got {gen_entries}"
        apply_entries = by_step.get("APPLY_PARAMETERS", 0)
        assert apply_entries == 2, (
            f"Expected 2 apply entries (1 gen × 2 samples), got {apply_entries}"
        )
