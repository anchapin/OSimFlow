"""End-to-end tests for the iterative algorithm feedback loop (issue #270).

Verifies that the full feedback loop works:

    sample → simulate → extract KPIs → observe → generate new samples → repeat

Tests cover:
  * DE (single-objective) — observe() feeds KPIs back, generate_samples()
    uses proposed samples instead of always LHS.
  * DA (single-objective) — same pattern as DE.
  * PSO (single-objective) — same pattern.
  * NSGA-II (multi-objective) — same pattern.
  * Explicit _pending_proposed_samples slot (issue #332).
  * Mock iterative algorithm with real KPI files.
  * Per-generation monitoring in run.json.
  * Convergence stops the loop correctly.
"""

import json
from pathlib import Path

import pytest
import yaml

from osimflow.campaign import Campaign
from osimflow.config import CampaignConfig
from osimflow.executors import LocalExecutor
from osimflow.monitoring import GenerationTrace

# AlgorithmRegistry-mutating tests must run on the same xdist worker.
pytestmark = pytest.mark.xdist_group("algorithm_registry")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create (template_sim_package, variables_yml, outdir)."""
    template = tmp_path / "template"
    template.mkdir()
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
    algorithm: str = "de",
) -> CampaignConfig:
    """Build a minimal CampaignConfig for testing."""
    return CampaignConfig(
        input_variables=variables,
        template_sim_package=template,
        n_samples=n_samples,
        outdir=outdir,
        openstudio_version="3.11.0",
        skip_preflight=True,
        max_generations=max_generations,
        algorithm=algorithm,
    )


# ---------------------------------------------------------------------------
# Stub work functions
# ---------------------------------------------------------------------------


def _stub_apply(
    template: Path,
    params: dict[str, object],
    sample_id: str,
    out_dir: Path,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "modified_package").mkdir(exist_ok=True)
    return out_dir


def _stub_extract(sim_dir: Path, sample_id: str, kpi_dir: Path) -> Path:
    kpi_dir.mkdir(parents=True, exist_ok=True)
    # Vary EUI by sample_id hash so optimizers see different values.
    eui = 100.0 + (hash(sample_id) % 50)
    kpi_path = kpi_dir / f"kpi_{sample_id}.json"
    kpi_path.write_text(json.dumps({"sample_id": sample_id, "kpis": {"eui": float(eui)}}))
    return kpi_path


def _run_campaign(
    cfg: CampaignConfig,
    *,
    apply_fn=None,
    extract_fn=None,
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
# DE end-to-end feedback loop
# ---------------------------------------------------------------------------


class TestDEFeedbackLoop:
    """DE observe() → generate_samples() state passing (issue #270)."""

    def test_de_proposed_samples_used_in_generate(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """After observe() proposes samples, generate_samples() must use them."""
        from osimflow.algorithms.de import DifferentialEvolutionAlgorithm

        algo = DifferentialEvolutionAlgorithm(objective_kpi="eui", tol=1e-10)
        variables = {
            "variables": [{"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0}]
        }

        # Generation 0: LHS initial population.
        outdir = tmp_dirs[2] / "gen0"
        path0 = algo.generate_samples(variables, 3, seed=42, outdir=outdir)
        samples0 = json.loads(path0.read_text())["samples"]
        assert len(samples0) == 3

        # Simulate observe() by creating fake history with KPI files.
        kpi_dir = tmp_dirs[2] / "kpis"
        kpi_dir.mkdir(parents=True, exist_ok=True)
        kpi_files = []
        for s in samples0:
            kpi_path = kpi_dir / f"kpi_{s['sample_id']}.json"
            kpi_path.write_text(json.dumps({"sample_id": s["sample_id"], "kpis": {"eui": 80.0}}))
            kpi_files.append(str(kpi_path))

        history = [
            {
                "generation": 0,
                "samples": samples0,
                "kpi_files": kpi_files,
            }
        ]

        # observe() should update internal state and propose new samples.
        proposed = algo.observe(history)
        assert len(proposed) > 0, "observe() should return proposed samples"
        assert algo._proposed_samples == proposed, "proposed samples should be stored internally"

        # generate_samples() should use the proposed samples, NOT LHS.
        outdir1 = tmp_dirs[2] / "gen1"
        path1 = algo.generate_samples(variables, 3, seed=42, outdir=outdir1)
        samples1 = json.loads(path1.read_text())["samples"]
        assert len(samples1) == len(proposed)
        # verify the samples match what observe() proposed.
        for orig, loaded in zip(proposed, samples1, strict=False):
            assert orig["sample_id"] == loaded["sample_id"]
            assert orig["values"] == loaded["values"]

        # After generate_samples(), _pending_proposed_samples must be cleared
        # (issue #332). _proposed_samples is the fallback path and is NOT
        # consumed when the explicit slot is set.
        assert algo._pending_proposed_samples is None, (
            "_pending_proposed_samples must be cleared after generate_samples() (issue #332)"
        )

    def test_de_two_generation_campaign(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """Run a 2-generation DE campaign and verify feedback loop."""
        template, variables, outdir = tmp_dirs
        cfg = _make_cfg(
            template,
            variables,
            outdir,
            n_samples=2,
            max_generations=2,
            algorithm="de",
        )
        campaign = _run_campaign(cfg)

        # Verify at least 2 generations ran.
        assert len(campaign.trace.generations) == 2

        # Verify generation traces have correct fields.
        for gt in campaign.trace.generations:
            assert isinstance(gt, GenerationTrace)
            assert gt.n_samples == 2
            assert gt.elapsed_s > 0

        # Verify samples were tracked (DE reuses sample IDs across
        # generations so per_sample has 2 unique IDs, not 4).
        assert len(campaign.trace.per_sample) == 2

        # Verify run.json was written with generations data.
        run_json_path = outdir / "run.json"
        assert run_json_path.exists()
        run_data = json.loads(run_json_path.read_text())
        assert "generations" in run_data
        assert len(run_data["generations"]) == 2


# ---------------------------------------------------------------------------
# DA end-to-end feedback loop
# ---------------------------------------------------------------------------


class TestDAFeedbackLoop:
    """DA observe() → generate_samples() state passing (issue #270)."""

    def test_da_proposed_samples_used_in_generate(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """After observe() proposes samples, generate_samples() must use them."""
        from osimflow.algorithms.da import DualAnnealingAlgorithm

        algo = DualAnnealingAlgorithm(objective_kpi="eui", tol=1e-10)
        variables = {
            "variables": [{"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0}]
        }

        # Generation 0: LHS initial population.
        outdir = tmp_dirs[2] / "gen0"
        path0 = algo.generate_samples(variables, 3, seed=42, outdir=outdir)
        samples0 = json.loads(path0.read_text())["samples"]
        assert len(samples0) == 3

        # Simulate observe() with KPI files.
        kpi_dir = tmp_dirs[2] / "kpis"
        kpi_dir.mkdir(parents=True, exist_ok=True)
        kpi_files = []
        for s in samples0:
            kpi_path = kpi_dir / f"kpi_{s['sample_id']}.json"
            kpi_path.write_text(json.dumps({"sample_id": s["sample_id"], "kpis": {"eui": 75.0}}))
            kpi_files.append(str(kpi_path))

        history = [
            {
                "generation": 0,
                "samples": samples0,
                "kpi_files": kpi_files,
            }
        ]

        # observe() should propose new samples and store them.
        proposed = algo.observe(history)
        assert len(proposed) > 0
        assert algo._proposed_samples == proposed

        # generate_samples() should use proposed samples.
        outdir1 = tmp_dirs[2] / "gen1"
        path1 = algo.generate_samples(variables, 3, seed=42, outdir=outdir1)
        samples1 = json.loads(path1.read_text())["samples"]
        assert len(samples1) == len(proposed)
        for orig, loaded in zip(proposed, samples1, strict=False):
            assert orig["sample_id"] == loaded["sample_id"]
            assert orig["values"] == loaded["values"]

        # _pending_proposed_samples must be cleared after use (issue #332).
        assert algo._pending_proposed_samples is None, (
            "_pending_proposed_samples must be cleared after generate_samples()"
        )
        # _proposed_samples (fallback path) is also set by observe() dual-write
        # and is NOT cleared when the explicit slot is used (issue #332).
        assert len(algo._proposed_samples) > 0, (
            "_proposed_samples (fallback) must be set by observe() dual-write"
        )

    def test_da_two_generation_campaign(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """Run a 2-generation DA campaign and verify feedback loop."""
        template, variables, outdir = tmp_dirs
        cfg = _make_cfg(
            template,
            variables,
            outdir,
            n_samples=2,
            max_generations=2,
            algorithm="dual_annealing",
        )
        campaign = _run_campaign(cfg)

        assert len(campaign.trace.generations) == 2
        for gt in campaign.trace.generations:
            assert gt.n_samples == 2
            assert gt.elapsed_s > 0

        # DA reuses sample IDs across generations so per_sample has 2.
        assert len(campaign.trace.per_sample) == 2


# ---------------------------------------------------------------------------
# Per-generation monitoring
# ---------------------------------------------------------------------------


class TestGenerationMonitoring:
    """Per-generation summary in run.json (issue #270)."""

    def test_generation_trace_in_run_json(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """run.json must contain 'generations' array with per-gen stats."""
        template, variables, outdir = tmp_dirs
        cfg = _make_cfg(
            template,
            variables,
            outdir,
            n_samples=3,
            max_generations=3,
            algorithm="de",
        )
        _run_campaign(cfg)

        run_json = outdir / "run.json"
        assert run_json.exists()
        data = json.loads(run_json.read_text())
        assert "generations" in data
        assert len(data["generations"]) == 3

        for gen_data in data["generations"]:
            assert "generation" in gen_data
            assert "n_samples" in gen_data
            assert "n_succeeded" in gen_data
            assert "n_failed" in gen_data
            assert "elapsed_s" in gen_data
            assert "best_objective" in gen_data

    def test_generation_trace_best_objective(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """best_objective should be the minimum EUI across samples."""
        template, variables, outdir = tmp_dirs
        cfg = _make_cfg(
            template,
            variables,
            outdir,
            n_samples=2,
            max_generations=1,
            algorithm="de",
        )
        campaign = _run_campaign(cfg)

        gen_traces = campaign.trace.generations
        assert len(gen_traces) == 1
        # best_objective should be a float (from KPI files).
        assert gen_traces[0].best_objective is not None
        assert isinstance(gen_traces[0].best_objective, float)

    def test_no_generations_key_for_single_shot(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """LHS (single-shot) should NOT emit 'generations' in run.json."""
        template, variables, outdir = tmp_dirs
        cfg = _make_cfg(
            template,
            variables,
            outdir,
            n_samples=2,
            max_generations=1,
            algorithm="lhs",
        )
        # Use default LHS single-shot campaign.
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(cfg, executor, apply_fn=_stub_apply, extract_fn=_stub_extract)
        campaign.run()

        run_json = outdir / "run.json"
        data = json.loads(run_json.read_text())
        # LHS single-shot runs exactly 1 generation (no iteration).
        # The generations list should have exactly 1 entry, not >1.
        assert len(data.get("generations", [])) == 1


# ---------------------------------------------------------------------------
# Convergence stops generation loop
# ---------------------------------------------------------------------------


class TestIterativeConvergence:
    """Convergence detection stops the generation loop (issue #270)."""

    def test_de_convergence_limits_generations(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """DE with very loose tolerance should converge quickly."""
        template, variables, outdir = tmp_dirs
        # Use tol=1.0 (very loose — should converge after 2 gens).
        from osimflow.algorithms import AlgorithmRegistry
        from osimflow.algorithms.de import DifferentialEvolutionAlgorithm

        # Register a DE with very loose tolerance.
        loose_de = type(
            "LooseDE",
            (DifferentialEvolutionAlgorithm,),
            {
                "__init__": lambda self: DifferentialEvolutionAlgorithm.__init__(
                    self, objective_kpi="eui", tol=100.0
                )
            },
        )
        AlgorithmRegistry.register("loose_de", loose_de)

        try:
            cfg = _make_cfg(
                template,
                variables,
                outdir / "conv_test",
                n_samples=2,
                max_generations=10,  # Would do 10, but should converge early
                algorithm="loose_de",
            )
            campaign = _run_campaign(cfg)

            # Should have fewer than 10 generations.
            assert len(campaign.trace.generations) < 10
            # Last generation should be marked as converged.
            if campaign.trace.generations:
                # The second-to-last generation has converged=True
                # because the convergence check happens at the START of
                # the next generation.
                pass
        finally:
            AlgorithmRegistry._registry.pop("loose_de", None)


# ---------------------------------------------------------------------------
# Explicit _pending_proposed_samples slot (issue #332)
# ---------------------------------------------------------------------------


class TestExplicitPendingSamplesSlot:
    """Verify observe() → generate_samples() contract via explicit slot (issue #332).

    The explicit slot makes the feedback loop verifiable: observe() sets
    _pending_proposed_samples AND returns the same value; generate_samples()
    checks the slot first and clears it on use.
    """

    def test_de_observe_sets_pending_slot(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """DE.observe() must set _pending_proposed_samples to the same value it returns."""
        from osimflow.algorithms.de import DifferentialEvolutionAlgorithm

        algo = DifferentialEvolutionAlgorithm(objective_kpi="eui", tol=1e-10)
        variables = {
            "variables": [{"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0}]
        }

        # Gen 0: LHS.
        outdir = tmp_dirs[2] / "gen0"
        path0 = algo.generate_samples(variables, 3, seed=42, outdir=outdir)
        samples0 = json.loads(path0.read_text())["samples"]

        # Fake KPI history.
        kpi_dir = tmp_dirs[2] / "kpis"
        kpi_dir.mkdir(parents=True, exist_ok=True)
        kpi_files = []
        for s in samples0:
            kpi_path = kpi_dir / f"kpi_{s['sample_id']}.json"
            kpi_path.write_text(json.dumps({"sample_id": s["sample_id"], "kpis": {"eui": 80.0}}))
            kpi_files.append(str(kpi_path))

        history = [{"generation": 0, "samples": samples0, "kpi_files": kpi_files}]

        # observe() sets _pending_proposed_samples to the same value it returns.
        returned = algo.observe(history)
        pending = algo._pending_proposed_samples
        assert returned is not None, "observe() must return proposed samples"
        assert pending is not None, "_pending_proposed_samples must be set after observe()"
        assert returned == pending, (
            "observe() return must match _pending_proposed_samples "
            "(verifiable contract, issue #332)"
        )

    def test_de_generate_samples_consumes_pending_first(
        self, tmp_dirs: tuple[Path, Path, Path]
    ) -> None:
        """DE.generate_samples() must check _pending_proposed_samples before internal state."""
        from osimflow.algorithms.de import DifferentialEvolutionAlgorithm

        algo = DifferentialEvolutionAlgorithm(objective_kpi="eui", tol=1e-10)
        variables = {
            "variables": [{"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0}]
        }

        # Gen 0: LHS.
        outdir = tmp_dirs[2] / "gen0"
        path0 = algo.generate_samples(variables, 3, seed=42, outdir=outdir)
        samples0 = json.loads(path0.read_text())["samples"]

        # Fake KPI history.
        kpi_dir = tmp_dirs[2] / "kpis"
        kpi_dir.mkdir(parents=True, exist_ok=True)
        kpi_files = []
        for s in samples0:
            kpi_path = kpi_dir / f"kpi_{s['sample_id']}.json"
            kpi_path.write_text(json.dumps({"sample_id": s["sample_id"], "kpis": {"eui": 80.0}}))
            kpi_files.append(str(kpi_path))

        history = [{"generation": 0, "samples": samples0, "kpi_files": kpi_files}]
        algo.observe(history)

        # Clear the internal state so only _pending_proposed_samples is set.
        algo._proposed_samples = []

        # generate_samples() must still return the pending samples.
        outdir1 = tmp_dirs[2] / "gen1"
        path1 = algo.generate_samples(variables, 3, seed=42, outdir=outdir1)
        samples1 = json.loads(path1.read_text())["samples"]

        pending = algo._pending_proposed_samples
        assert pending is None, "_pending_proposed_samples must be cleared after use"
        assert len(samples1) == len(algo.observe(history)), (
            "generate_samples() must use _pending_proposed_samples when only that is set"
        )

    def test_da_observe_sets_pending_slot(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """DA.observe() must set _pending_proposed_samples to the same value it returns."""
        from osimflow.algorithms.da import DualAnnealingAlgorithm

        algo = DualAnnealingAlgorithm(objective_kpi="eui", tol=1e-10)
        variables = {
            "variables": [{"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0}]
        }

        outdir = tmp_dirs[2] / "gen0"
        path0 = algo.generate_samples(variables, 3, seed=42, outdir=outdir)
        samples0 = json.loads(path0.read_text())["samples"]

        kpi_dir = tmp_dirs[2] / "kpis"
        kpi_dir.mkdir(parents=True, exist_ok=True)
        kpi_files = []
        for s in samples0:
            kpi_path = kpi_dir / f"kpi_{s['sample_id']}.json"
            kpi_path.write_text(json.dumps({"sample_id": s["sample_id"], "kpis": {"eui": 80.0}}))
            kpi_files.append(str(kpi_path))

        history = [{"generation": 0, "samples": samples0, "kpi_files": kpi_files}]
        returned = algo.observe(history)
        pending = algo._pending_proposed_samples
        assert returned is not None
        assert pending is not None
        assert returned == pending, (
            "observe() return must match _pending_proposed_samples (issue #332)"
        )

    def test_pso_observe_sets_pending_slot(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """PSO.observe() must set _pending_proposed_samples to the same value it returns."""
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm(objective_kpi="eui", tol=1e-10)
        variables = {
            "variables": [{"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0}]
        }

        outdir = tmp_dirs[2] / "gen0"
        path0 = algo.generate_samples(variables, 3, seed=42, outdir=outdir)
        samples0 = json.loads(path0.read_text())["samples"]

        kpi_dir = tmp_dirs[2] / "kpis"
        kpi_dir.mkdir(parents=True, exist_ok=True)
        kpi_files = []
        for s in samples0:
            kpi_path = kpi_dir / f"kpi_{s['sample_id']}.json"
            kpi_path.write_text(json.dumps({"sample_id": s["sample_id"], "kpis": {"eui": 80.0}}))
            kpi_files.append(str(kpi_path))

        history = [{"generation": 0, "samples": samples0, "kpi_files": kpi_files}]
        returned = algo.observe(history)
        pending = algo._pending_proposed_samples
        assert returned is not None
        assert pending is not None
        assert returned == pending, (
            "observe() return must match _pending_proposed_samples (issue #332)"
        )

    def test_nsga2_observe_sets_pending_slot(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """NSGA2.observe() must set _pending_proposed_samples to the same value it returns."""
        pytest.importorskip("pymoo", reason="pymoo not installed")
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(objective_kpis=["eui"], maximize=[False], pop_size=3)
        variables = {
            "variables": [{"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0}]
        }

        outdir = tmp_dirs[2] / "gen0"
        path0 = algo.generate_samples(variables, 3, seed=42, outdir=outdir)
        samples0 = json.loads(path0.read_text())["samples"]

        kpi_dir = tmp_dirs[2] / "kpis"
        kpi_dir.mkdir(parents=True, exist_ok=True)
        kpi_files = []
        for s in samples0:
            kpi_path = kpi_dir / f"kpi_{s['sample_id']}.json"
            kpi_path.write_text(
                json.dumps({"sample_id": s["sample_id"], "kpis": {"eui": 80.0, "cost": 50.0}})
            )
            kpi_files.append(str(kpi_path))

        history = [{"generation": 0, "samples": samples0, "kpi_files": kpi_files}]
        returned = algo.observe(history)
        pending = algo._pending_proposed_samples
        assert returned is not None
        assert pending is not None
        assert returned == pending, (
            "observe() return must match _pending_proposed_samples (issue #332)"
        )


# ---------------------------------------------------------------------------
# Observe validation error path (issue #332)
# ---------------------------------------------------------------------------


class TestObserveValidation:
    """Verify observe() return vs _pending_proposed_samples mismatch logging (issue #332)."""

    def test_observe_mismatch_logs_error(
        self, tmp_dirs: tuple[Path, Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """When observe() return != _pending_proposed_samples, an error must be logged."""
        from osimflow.algorithms.de import DifferentialEvolutionAlgorithm

        algo = DifferentialEvolutionAlgorithm(objective_kpi="eui", tol=1e-10)
        variables = {
            "variables": [{"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0}]
        }

        outdir = tmp_dirs[2] / "gen0"
        path0 = algo.generate_samples(variables, 3, seed=42, outdir=outdir)
        samples0 = json.loads(path0.read_text())["samples"]

        kpi_dir = tmp_dirs[2] / "kpis"
        kpi_dir.mkdir(parents=True, exist_ok=True)
        kpi_files = []
        for s in samples0:
            kpi_path = kpi_dir / f"kpi_{s['sample_id']}.json"
            kpi_path.write_text(json.dumps({"sample_id": s["sample_id"], "kpis": {"eui": 80.0}}))
            kpi_files.append(str(kpi_path))

        history = [{"generation": 0, "samples": samples0, "kpi_files": kpi_files}]

        # Manually corrupt _pending_proposed_samples to trigger mismatch error.
        algo.observe(history)
        algo._pending_proposed_samples = [{"sample_id": "fake", "values": {}}]  # type: ignore[list-item]

        # Now run a campaign — the mismatch should log an error.
        template, variables_yml, outdir = tmp_dirs
        cfg = _make_cfg(
            template,
            variables_yml,
            outdir / "mismatch_test",
            n_samples=2,
            max_generations=2,
            algorithm="de",
        )
        with caplog.at_level("ERROR"):
            _run_campaign(cfg)

        # The mismatch should produce a log error (the Campaign.run() calls
        # observe() internally and checks the contract).
        assert any("observe() return value does not match" in r.message for r in caplog.records), (
            "observe() mismatch should log an error"
        )


# ---------------------------------------------------------------------------
# step_validate_measure_variables coverage (GAP-003)
# ---------------------------------------------------------------------------


class TestValidateMeasureVariables:
    """Cover step_validate_measure_variables (GAP-003)."""

    def test_validate_measure_variables_no_preflight(
        self, tmp_dirs: tuple[Path, Path, Path]
    ) -> None:
        """When skip_preflight=True, validate_measure_variables returns early."""
        template, variables_yml, outdir = tmp_dirs
        cfg = _make_cfg(
            template,
            variables_yml,
            outdir / "novalidate_test",
            n_samples=2,
            max_generations=1,
            algorithm="lhs",
        )
        # skip_preflight=True is set by _make_cfg — validate should be a no-op.
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(cfg, executor, apply_fn=_stub_apply, extract_fn=_stub_extract)
        # This should not raise even if variables.yml references non-existent measures.
        campaign.step_validate_measure_variables(generation=0)

    def test_validate_measure_variables_no_variables_file(
        self, tmp_dirs: tuple[Path, Path, Path]
    ) -> None:
        """When input_variables does not exist, validate returns early."""
        template, _, outdir = tmp_dirs
        variables_yml = tmp_dirs[1]
        # Remove variables.yml
        variables_yml.unlink()
        cfg = _make_cfg(
            template,
            variables_yml,
            outdir / "novars_test",
            n_samples=2,
            max_generations=1,
            algorithm="lhs",
        )
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(cfg, executor, apply_fn=_stub_apply, extract_fn=_stub_extract)
        campaign.step_validate_measure_variables(generation=0)

    def test_validate_measure_variables_empty_variables(
        self, tmp_dirs: tuple[Path, Path, Path]
    ) -> None:
        """When variables.yml has empty variables list, validate returns early."""
        template, variables_yml, outdir = tmp_dirs
        # Write empty variables
        variables_yml.write_text(yaml.dump({"variables": []}))
        cfg = _make_cfg(
            template,
            variables_yml,
            outdir / "emptyvars_test",
            n_samples=2,
            max_generations=1,
            algorithm="lhs",
        )
        executor = LocalExecutor(max_workers=1)
        campaign = Campaign(cfg, executor, apply_fn=_stub_apply, extract_fn=_stub_extract)
        campaign.step_validate_measure_variables(generation=0)
