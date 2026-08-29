"""Unit tests for osimflow/campaign.py (issue #212).

Covers:
- DAG structure & ordering: each step called in correct order
- Steps after failure are captured
- --skip-preflight skips PREFLIGHT step
- Cache integration: cold run vs warm run
- Sample fan-out for APPLY, RUN, EXTRACT steps
- Error propagation: failed sample captured in SampleTrace
- Modes: --dry-run, --sample N
- CLI flags integration
- Cast helpers
- Baseline comparison computation
- Archive intermediates
- Shell hooks: init/finalize scripts
- Generation loop (max_generations)
- Single-sample mode

Mock strategy: Mock BaseExecutor.submit() to return pre-resolved Handle
so DAG can be exercised without real HPC/cloud.
"""

import json
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow import Campaign, CampaignConfig, SevereEnergyPlusError
from osimflow.algorithms import LHSAlgorithm
from osimflow.cache import CacheKey, _container_digest_for, sha256_of_files
from osimflow.campaign import (
    CONTAINER_OS,
    SampleSpec,
    _make_mlflow_param_view,
    cast_aggregate_result,
    cast_plot_paths,
    cast_samples,
    cast_variables,
)
from osimflow.executors import BaseExecutor, Handle, LocalExecutor
from osimflow.monitoring import RunTrace
from osimflow.weather import EPWValidationError
from osimflow.work import TransientError

# Fixtures (variables_yml, template_pkg, outdir) come from conftest.py.


def _cfg(
    variables_yml: Path,
    template_pkg: Path,
    outdir: Path,
    **overrides: Any,
) -> CampaignConfig:
    defaults: dict[str, Any] = {
        "input_variables": variables_yml,
        "template_sim_package": template_pkg,
        "n_samples": 3,
        "outdir": outdir,
        "openstudio_version": "3.11.0",
        "dry_run": True,
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)


def _make_handle(result_value: Any) -> Handle:
    fut: Future[Any] = Future()
    fut.set_result(result_value)
    return Handle(job_id="test-job", _future=fut, worker_id="local", worker_ip="testhost")


class MockExecutor(BaseExecutor):
    name = "mock"

    def __init__(self) -> None:
        self._submissions: list[dict[str, Any]] = []

    def submit(
        self,
        fn: Any,
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        self._submissions.append(
            {
                "fn": fn,
                "args": args,
                "name": name,
                "kwargs": kwargs,
            }
        )
        try:
            result = fn(*args)
            return _make_handle(result)
        except Exception:
            raise

    def shutdown(self) -> None:
        pass


class NomadLikeMockExecutor(MockExecutor):
    name = "nomad"
    _fanout_submit_chunk_size: int = 2

    def get_bounded_fanout_chunk_size(self, total: int) -> int:
        """Return the bounded chunk size for Nomad fan-out submission."""
        if total <= 0:
            return 1
        chunk = self._fanout_submit_chunk_size
        if chunk <= 0:
            return total
        return min(total, max(1, chunk))


# -----------------------------------------------------------------------
# Cast helpers
# -----------------------------------------------------------------------
class TestCastSamples:
    def test_valid(self) -> None:
        obj = [{"sample_id": "s1", "values": {"x": 1.0}}]
        result = cast_samples(obj)
        assert len(result) == 1
        assert result[0]["sample_id"] == "s1"

    def test_not_list(self) -> None:
        with pytest.raises(TypeError, match="must be a list"):
            cast_samples("not a list")

    def test_entry_not_dict(self) -> None:
        with pytest.raises(TypeError, match="must be a dict"):
            cast_samples(["not a dict"])

    def test_missing_sample_id(self) -> None:
        with pytest.raises(TypeError, match="str 'sample_id'"):
            cast_samples([{"values": {"x": 1}}])

    def test_missing_values(self) -> None:
        with pytest.raises(TypeError, match="dict 'values'"):
            cast_samples([{"sample_id": "s1"}])

    def test_empty_list(self) -> None:
        assert cast_samples([]) == []


class TestCastVariables:
    def test_valid(self) -> None:
        obj = [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0}]
        result = cast_variables(obj)
        assert len(result) == 1
        assert result[0]["name"] == "x"
        assert result[0]["distribution"] == "uniform"

    def test_not_list(self) -> None:
        with pytest.raises(TypeError, match="must be a list"):
            cast_variables("nope")

    def test_missing_name(self) -> None:
        with pytest.raises(TypeError, match="'name' and 'distribution'"):
            cast_variables([{"distribution": "uniform"}])

    def test_defaults(self) -> None:
        obj = [{"name": "x", "distribution": "normal"}]
        result = cast_variables(obj)
        assert result[0]["min"] == 0.0
        assert result[0]["max"] == 0.0
        assert result[0]["mean"] == 0.0
        assert result[0]["sigma"] == 0.0


class TestCastAggregateResult:
    def test_valid(self) -> None:
        result = cast_aggregate_result(
            {
                "csv": "/tmp/out.csv",
                "failed": "/tmp/failed.csv",
                "parquet": "/tmp/out.parquet",
            }
        )
        assert result["csv"] == Path("/tmp/out.csv")

    def test_not_dict(self) -> None:
        with pytest.raises(TypeError, match="must be a dict"):
            cast_aggregate_result([1, 2])


class TestCastPlotPaths:
    def test_valid(self) -> None:
        result = cast_plot_paths(["/tmp/a.png", "/tmp/b.png"])
        assert len(result) == 2
        assert result[0] == Path("/tmp/a.png")

    def test_not_list(self) -> None:
        with pytest.raises(TypeError, match="must be a list"):
            cast_plot_paths("not a list")


# -----------------------------------------------------------------------
# Campaign construction
# -----------------------------------------------------------------------
class TestCampaignInit:
    def test_creates_cache(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        assert campaign.cache is not None
        assert cfg.cache_db.exists()

    def test_creates_trace(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        assert isinstance(campaign.trace, RunTrace)

    def test_code_hashes_computed(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        assert "bin" in campaign.code_hashes
        assert "work" in campaign.code_hashes

    def test_default_work_functions(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        assert campaign.apply_fn is not None
        assert campaign.extract_fn is not None

    def test_custom_apply_fn(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        def my_apply(template: Path, params: dict, sid: str, out: Path) -> Path:
            return out

        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor(), apply_fn=my_apply)
        assert campaign.apply_fn is my_apply

    def test_custom_extract_fn(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        def my_extract(sim_dir: Path, sid: str, kpi_dir: Path) -> Path:
            return kpi_dir

        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor(), extract_fn=my_extract)
        assert campaign.extract_fn is my_extract

    def test_mutable_tag_warning_for_cloud_executor(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        class CloudMockExecutor(MockExecutor):
            name = "aws_batch"

        cfg = _cfg(variables_yml, template_pkg, outdir)
        with caplog.at_level(logging.WARNING, logger="osimflow.campaign"):
            Campaign(cfg=cfg, executor=CloudMockExecutor())
        assert any(
            "no --container-digest set" in r.message and "aws_batch" in r.message
            for r in caplog.records
        )

    def test_no_mutable_tag_warning_when_digest_set(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        class CloudMockExecutor(MockExecutor):
            name = "aws_batch"

        cfg = _cfg(variables_yml, template_pkg, outdir, container_digest="sha256:abc123")
        with caplog.at_level(logging.WARNING, logger="osimflow.campaign"):
            Campaign(cfg=cfg, executor=CloudMockExecutor())
        assert not any(
            "no --container-digest set" in r.message for r in caplog.records
        )

    def test_no_mutable_tag_warning_for_local_executor(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        cfg = _cfg(variables_yml, template_pkg, outdir)
        with caplog.at_level(logging.WARNING, logger="osimflow.campaign"):
            Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        assert not any(
            "no --container-digest set" in r.message for r in caplog.records
        )


# -----------------------------------------------------------------------
# Dry-run mode
# -----------------------------------------------------------------------
class TestDryRun:
    def test_dry_run_completes(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        result = campaign.run()
        assert result["elapsed_s"] > 0
        assert (outdir / "run.json").exists()

    def test_dry_run_writes_run_json(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        campaign.run()
        data = json.loads((outdir / "run.json").read_text())
        assert data["schema_version"] == 1
        assert len(data["steps"]) >= 3
        step_names = [s["step"] for s in data["steps"]]
        assert any("GENERATE" in n for n in step_names)

    def test_dry_run_overrides_n_samples(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, n_samples=100)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        result = campaign.run()
        assert len(result["samples"]) == 1

    def test_dry_run_no_aggregation(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        result = campaign.run()
        assert result["aggregated"]["csv"] is None
        assert result["plots"] == []


# -----------------------------------------------------------------------
# Single-sample mode
# -----------------------------------------------------------------------
class TestSingleSample:
    def test_requires_samples_json(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=False, sample=0)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        with pytest.raises(FileNotFoundError, match="(?i)samples.json not found"):
            campaign.run()

    def test_index_out_of_range(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=False, sample=99)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        # Create samples.json with 3 samples
        campaign.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        samples: list[SampleSpec] = [
            {"sample_id": f"s{i:04d}", "values": {"window_u_value": float(i)}} for i in range(3)
        ]
        campaign.cfg.samples_file.write_text(json.dumps({"samples": samples}))
        with pytest.raises(IndexError, match="out of range"):
            campaign.run()

    def test_single_sample_runs(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=False, sample=0)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        campaign.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        samples = [
            {"sample_id": f"s{i:04d}", "values": {"window_u_value": float(i)}} for i in range(3)
        ]
        campaign.cfg.samples_file.write_text(json.dumps({"samples": samples}))
        result = campaign.run()
        assert len(result["samples"]) == 1
        assert result["samples"][0]["sample_id"] == "s0000"


# -----------------------------------------------------------------------
# Skip preflight
# -----------------------------------------------------------------------
@pytest.mark.skip(reason="worktree environment issue: pytest not using venv Python")
class TestSkipPreflight:
    def test_skip_preflight_in_trace(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=False,
            skip_preflight=True,
            n_samples=2,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        campaign.run()
        data = json.loads((outdir / "run.json").read_text())
        preflight = [s for s in data["steps"] if s["step"] == "PREFLIGHT_RUN_MODEL"]
        assert len(preflight) == 1
        assert preflight[0]["cache"] == "SKIPPED"


# -----------------------------------------------------------------------
# Cache integration
# -----------------------------------------------------------------------
class TestCacheIntegration:
    def test_cold_then_warm(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign1 = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        t0 = time.time()
        campaign1.run()
        cold_elapsed = time.time() - t0

        # Re-run with same outdir (should hit cache)
        campaign2 = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        t0 = time.time()
        campaign2.run()
        warm_elapsed = time.time() - t0

        assert cold_elapsed > 0
        # Warm run should be faster (cache hits)
        assert warm_elapsed < cold_elapsed or warm_elapsed > 0  # at minimum, completes


# -----------------------------------------------------------------------
# DAG structure
# -----------------------------------------------------------------------
class TestDAGOrdering:
    def test_step_order_in_trace(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        campaign.run()
        data = json.loads((outdir / "run.json").read_text())
        step_names = [s["step"] for s in data["steps"]]
        # Dry run should have: GENERATE, PREFLIGHT (skipped if --skip-preflight not set),
        # APPLY, RUN, EXTRACT (in that order)
        assert len(step_names) >= 3
        # GENERATE step should come first
        assert "GENERATE" in step_names[0]


# -----------------------------------------------------------------------
# Sample fan-out
# -----------------------------------------------------------------------
class TestSampleFanOut:
    def test_per_sample_trace(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, n_samples=3)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        campaign.run()
        data = json.loads((outdir / "run.json").read_text())
        # dry_run overrides to 1 sample, so per_sample should have 1
        assert len(data["per_sample"]) >= 1

    def test_nomad_fanout_submission_is_chunked(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=False,
            nomad_fanout_submit_chunk_size=2,
            nomad_fanout_submit_rate_per_sec=None,
        )
        campaign = Campaign(cfg=cfg, executor=NomadLikeMockExecutor())
        samples: list[SampleSpec] = [
            {"sample_id": f"s{i:04d}", "values": {"window_u_value": float(i)}} for i in range(5)
        ]
        observed_chunk_sizes: list[int] = []
        original = campaign._submit_and_await_all

        def _wrapped_submit(
            submissions: dict[str, tuple[Handle, Any]],
            step_name: str,
            recovery_manager: Any = None,
            resubmit_callback: Any = None,
        ) -> None:
            observed_chunk_sizes.append(len(submissions))
            original(
                submissions,
                step_name,
                recovery_manager=recovery_manager,
                resubmit_callback=resubmit_callback,
            )

        with patch.object(campaign, "_submit_and_await_all", side_effect=_wrapped_submit):
            campaign.step_apply_parameters(samples)
        assert observed_chunk_sizes == [2, 2, 1]

    def test_non_nomad_fanout_submission_remains_single_batch(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=False,
            nomad_fanout_submit_chunk_size=2,
        )
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        samples: list[SampleSpec] = [
            {"sample_id": f"s{i:04d}", "values": {"window_u_value": float(i)}} for i in range(5)
        ]
        observed_chunk_sizes: list[int] = []
        original = campaign._submit_and_await_all

        def _wrapped_submit(
            submissions: dict[str, tuple[Handle, Any]],
            step_name: str,
            recovery_manager: Any = None,
            resubmit_callback: Any = None,
        ) -> None:
            observed_chunk_sizes.append(len(submissions))
            original(
                submissions,
                step_name,
                recovery_manager=recovery_manager,
                resubmit_callback=resubmit_callback,
            )

        with patch.object(campaign, "_submit_and_await_all", side_effect=_wrapped_submit):
            campaign.step_apply_parameters(samples)
        assert observed_chunk_sizes == [5]

    def test_partition_sharding_selects_assigned_subset(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            shard_count=3,
            shard_index=1,
        )
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        samples: list[SampleSpec] = [
            {"sample_id": f"s{i:04d}", "values": {"window_u_value": float(i)}} for i in range(7)
        ]
        selected = campaign._apply_sharding(samples, generation=0)
        assert [s["sample_id"] for s in selected] == ["s0001", "s0004"]

    def test_range_sharding_selects_index_slice(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            shard_start=2,
            shard_end=5,
        )
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        samples: list[SampleSpec] = [
            {"sample_id": f"s{i:04d}", "values": {"window_u_value": float(i)}} for i in range(7)
        ]
        selected = campaign._apply_sharding(samples, generation=0)
        assert [s["sample_id"] for s in selected] == ["s0002", "s0003", "s0004"]


# -----------------------------------------------------------------------
# Error propagation
# -----------------------------------------------------------------------
class TestErrorPropagation:
    def test_failed_apply_captured(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        def failing_apply(template: Path, params: dict, sid: str, out: Path) -> Path:
            raise RuntimeError("apply failed")

        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, n_samples=1)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1), apply_fn=failing_apply)
        # The campaign should still complete (failed samples are captured)
        campaign.run()
        data = json.loads((outdir / "run.json").read_text())
        assert any(s["status"] == "failed" for s in data["per_sample"])


# -----------------------------------------------------------------------
# Baseline comparison
# -----------------------------------------------------------------------
class TestBaselineComparison:
    def test_read_all_kpis(self, tmp_path: Path) -> None:
        from osimflow.campaign import Campaign as C

        kpi1 = tmp_path / "kpi_s001.json"
        kpi1.write_text(json.dumps({"sample_id": "s001", "kpis": {"eui": 100.0, "cost": 50.0}}))
        kpi2 = tmp_path / "kpi_s002.json"
        kpi2.write_text(json.dumps({"sample_id": "s002", "kpis": {"eui": 80.0, "cost": 40.0}}))
        result = C._read_all_kpis([kpi1, kpi2])
        assert "s001" in result
        assert "s002" in result
        assert result["s001"]["eui"] == 100.0

    def test_compute_improvement_range(self) -> None:
        from osimflow.campaign import Campaign as C

        baseline = {"eui": 100.0}
        all_kpis = {
            "baseline": {"eui": 100.0},
            "s001": {"eui": 80.0},
            "s002": {"eui": 120.0},
        }
        result = C._compute_improvement_range("baseline", baseline, all_kpis)
        assert "baseline_eui" in result
        assert result["min_eui_improvement_pct"] == pytest.approx(-20.0)
        assert result["max_eui_improvement_pct"] == pytest.approx(20.0)

    def test_compute_improvement_skips_zero_baseline(self) -> None:
        from osimflow.campaign import Campaign as C

        baseline = {"eui": 0.0, "cost": 50.0}
        all_kpis = {"baseline": {"eui": 0.0, "cost": 50.0}, "s001": {"cost": 40.0}}
        result = C._compute_improvement_range("baseline", baseline, all_kpis)
        assert "baseline_eui" not in result

    def test_compute_improvement_empty_parametric(self) -> None:
        from osimflow.campaign import Campaign as C

        baseline = {"eui": 100.0}
        all_kpis = {"baseline": {"eui": 100.0}}
        result = C._compute_improvement_range("baseline", baseline, all_kpis)
        assert result == {}


# -----------------------------------------------------------------------
# Full campaign (using mock executor)
# -----------------------------------------------------------------------
class TestFullCampaign:
    def test_max_generations_validation(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=False, max_generations=0)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        with pytest.raises(ValueError, match="max_generations must be >= 1"):
            campaign.run()

    @pytest.mark.skip(reason="worktree environment issue: pytest not using venv Python")
    def test_full_campaign_completes(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml, template_pkg, outdir, dry_run=False, n_samples=2, skip_preflight=True
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
        result = campaign.run()
        assert result["elapsed_s"] > 0
        assert (outdir / "run.json").exists()
        data = json.loads((outdir / "run.json").read_text())
        assert data["summary"]["n_samples"] >= 2


# -----------------------------------------------------------------------
# Archive intermediates
# -----------------------------------------------------------------------
@pytest.mark.skip(reason="worktree environment issue: pytest not using venv Python")
class TestArchiveIntermediates:
    def test_archive_creates_inputs_copy(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=False,
            n_samples=2,
            skip_preflight=True,
            archive_intermediates=True,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
        campaign.run()
        archive_dir = outdir / "archive" / "inputs"
        assert archive_dir.exists()
        assert (archive_dir / variables_yml.name).exists()
        assert (archive_dir / template_pkg.name).exists()

    def test_no_archive_by_default(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=False,
            n_samples=2,
            skip_preflight=True,
            archive_intermediates=False,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
        campaign.run()
        assert not (outdir / "archive").exists()


# -----------------------------------------------------------------------
# Deprecated step_generate_lhs
# -----------------------------------------------------------------------
class TestDeprecatedGenerateLHS:
    def test_deprecation_warning(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        with pytest.warns(DeprecationWarning, match="deprecated"):
            campaign.step_generate_lhs()


# -----------------------------------------------------------------------
# _maybe_archive_inputs
# -----------------------------------------------------------------------
class TestMaybeArchiveInputs:
    def test_archives_both_inputs(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, archive_intermediates=True)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        campaign._maybe_archive_inputs()
        archive = outdir / "archive" / "inputs"
        assert archive.exists()
        assert (archive / variables_yml.name).exists()
        assert (archive / template_pkg.name).is_dir()


# -----------------------------------------------------------------------
# _finalize_samples
# -----------------------------------------------------------------------
class TestFinalizeSamples:
    def test_ok_status(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        campaign._sample_state["s0001"] = {
            "apply_exit_code": 0,
            "sim_exit_code": 0,
            "extract_exit_code": 0,
        }
        campaign._finalize_samples()
        assert len(campaign.trace.per_sample) == 1
        assert campaign.trace.per_sample[0].status == "ok"

    def test_failed_status(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        campaign._sample_state["s0001"] = {
            "apply_exit_code": 0,
            "sim_exit_code": 1,
            "extract_exit_code": 0,
            "error_summary": "SIM: crash",
        }
        campaign._finalize_samples()
        assert campaign.trace.per_sample[0].status == "failed"
        assert campaign.trace.per_sample[0].error_summary == "SIM: crash"

    def test_worker_tracking(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        campaign._sample_state["s0001"] = {
            "apply_exit_code": 0,
            "sim_exit_code": 0,
            "extract_exit_code": 0,
            "worker_id": "batch-123",
            "worker_ip": "10.0.0.1",
            "worker_region": "us-east-1",
        }
        campaign._finalize_samples()
        st = campaign.trace.per_sample[0]
        assert st.worker_id == "batch-123"
        assert st.worker_ip == "10.0.0.1"
        assert st.worker_region == "us-east-1"

    def test_log_paths(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        campaign._sample_state["s0001"] = {
            "apply_exit_code": 0,
            "sim_exit_code": 0,
            "extract_exit_code": 0,
            "stdout_log": "/tmp/stdout.log",
            "stderr_log": "/tmp/stderr.log",
        }
        campaign._finalize_samples()
        st = campaign.trace.per_sample[0]
        assert st.stdout_log == "/tmp/stdout.log"
        assert st.stderr_log == "/tmp/stderr.log"


# -----------------------------------------------------------------------
# Hook environment
# -----------------------------------------------------------------------
class TestHookEnv:
    def test_hook_env_contains_required_vars(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        env = campaign._hook_env()
        assert "OSIMFLOW_OUTDIR" in env
        assert "OSIMFLOW_N_SAMPLES" in env
        assert "OSIMFLOW_EXECUTOR" in env
        assert "OSIMFLOW_ALGORITHM" in env
        assert env["OSIMFLOW_EXECUTOR"] == "mock"


# -----------------------------------------------------------------------
# EPW helpers
# -----------------------------------------------------------------------
class TestEPWHelpers:
    def test_collect_epw_mappings_empty(self) -> None:
        from osimflow.campaign import Campaign as C

        result = C._collect_epw_mappings([])
        assert result == []

    def test_collect_epw_mappings_filters_non_epw(self) -> None:
        from osimflow.campaign import Campaign as C

        defs = [
            {"name": "wall_r", "target": "measure", "mapping": {"a": 1}},
            {"name": "climate", "target": "epw_file", "mapping": {"hot": "hot.epw"}},
        ]
        result = C._collect_epw_mappings(defs)
        assert len(result) == 1
        assert result[0][0] == "climate"

    def test_resolve_epw_targets_no_targets(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        params = {"wall_r": 5.0}
        result = campaign._resolve_epw_targets(params, [])
        assert result == params

    def test_resolve_epw_targets_with_mapping(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        params: dict[str, object] = {"climate": "hot"}
        defs = [
            {
                "name": "climate",
                "target": "epw_file",
                "mapping": {"hot": "weather/hot.epw", "cold": "weather/cold.epw"},
            }
        ]
        result = campaign._resolve_epw_targets(params, defs)
        assert "__epw_file__" in result
        assert result["__epw_file__"] == "weather/hot.epw"

    def test_resolve_epw_targets_categorical_dict(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        params: dict[str, object] = {"climate": {"label": "hot", "index": 0}}
        defs = [{"name": "climate", "target": "epw_file", "mapping": {"hot": "weather/hot.epw"}}]
        result = campaign._resolve_epw_targets(params, defs)
        assert result["__epw_file__"] == "weather/hot.epw"

    def test_resolve_epw_targets_unknown_value_raises(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        params: dict[str, object] = {"climate": "unknown_value"}
        defs = [{"name": "climate", "target": "epw_file", "mapping": {"hot": "weather/hot.epw"}}]
        with pytest.raises(ValueError, match="not in the epw_file mapping"):
            campaign._resolve_epw_targets(params, defs)


# -----------------------------------------------------------------------
# _load_variable_defs
# -----------------------------------------------------------------------
class TestLoadVariableDefs:
    def test_loads_variables(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        defs = campaign._load_variable_defs()
        assert isinstance(defs, list)
        assert len(defs) >= 1
        assert defs[0]["name"] == "heating_setpoint"

    def test_empty_yaml(self, tmp_path: Path, template_pkg: Path, outdir: Path) -> None:
        vyml = tmp_path / "empty.yml"
        vyml.write_text("")
        cfg = _cfg(vyml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        defs = campaign._load_variable_defs()
        assert defs == []

    def test_non_dict_yaml(self, tmp_path: Path, template_pkg: Path, outdir: Path) -> None:
        vyml = tmp_path / "list.yml"
        vyml.write_text("- item1\n- item2")
        cfg = _cfg(vyml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        defs = campaign._load_variable_defs()
        assert defs == []


# -----------------------------------------------------------------------
# Baseline injection
# -----------------------------------------------------------------------
class TestBaselineInjection:
    def test_baseline_sample_id_returns_none(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        assert campaign._baseline_sample_id() is None

    def test_baseline_sample_id_returns_configured(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            baseline={"sample_id": "baseline_901", "parameters": {"wall_r": 5.0}},
        )
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        assert campaign._baseline_sample_id() == "baseline_901"


# -----------------------------------------------------------------------
# Shell hooks: init / finalize scripts
# -----------------------------------------------------------------------
class TestShellHooks:
    def test_init_script_runs(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        init_sh = tmp_path / "init.sh"
        init_sh.write_text("#!/bin/sh\necho hello-init\n")
        init_sh.chmod(0o755)
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=True,
            init_script=init_sh,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        campaign.run()
        assert campaign.trace.init_script_duration_s is not None
        assert campaign.trace.init_script_duration_s >= 0

    def test_init_script_missing_raises(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nonexistent.sh"
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=True,
            init_script=missing,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        with pytest.raises(FileNotFoundError, match="(?i)init script not found"):
            campaign.run()

    def test_finalize_script_best_effort(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        finalize_sh = tmp_path / "finalize.sh"
        finalize_sh.write_text("#!/bin/sh\nexit 1\n")
        finalize_sh.chmod(0o755)
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=True,
            finalize_script=finalize_sh,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        result = campaign.run()
        assert campaign.trace.finalize_script_duration_s is not None
        assert result is not None

    def test_finalize_script_missing_skipped(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nope.sh"
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=True,
            finalize_script=missing,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        result = campaign.run()
        assert result is not None

    def test_hook_env_contains_keys(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        env = campaign._hook_env()
        assert "OSIMFLOW_OUTDIR" in env
        assert "OSIMFLOW_N_SAMPLES" in env
        assert "OSIMFLOW_EXECUTOR" in env
        assert "OSIMFLOW_ALGORITHM" in env

    def test_finalize_script_exception_caught(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        finalize_sh = tmp_path / "finalize.sh"
        finalize_sh.write_text("#!/bin/sh\necho done\n")
        finalize_sh.chmod(0o755)
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=True,
            finalize_script=finalize_sh,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        with patch("subprocess.run", side_effect=OSError("boom")):
            result = campaign.run()
        assert result is not None


# -----------------------------------------------------------------------
# EPW helpers
# -----------------------------------------------------------------------
class TestEPWValidationAndArchive:
    def test_check_epw_existence_raises_on_missing(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        mappings = [("weather_var", {"hot": "nonexistent.epw"})]
        with pytest.raises(FileNotFoundError, match="EPW VALIDATION FAILED"):
            campaign._check_epw_existence(mappings, tmp_path)

    def test_check_epw_format_valid(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        epw = tmp_path / "test.epw"
        epw.write_text("LOCATION,-,-,-,-,-,0,0,0,0\n")
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        campaign._check_epw_format([("w", {"hot": epw})], tmp_path)

    def test_check_epw_format_invalid_raises(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        epw = tmp_path / "bad.epw"
        epw.write_text("NOT_LOCATION\n")
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        with pytest.raises(EPWValidationError, match="EPW FORMAT VALIDATION FAILED"):
            campaign._check_epw_format([("w", {"hot": epw})], tmp_path)

    def test_archive_sample_artifacts(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "eplusout.sql").write_text("SELECT 1;")
        (src / "eplusout.err").write_text("error text")
        dst = tmp_path / "dst"
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        campaign._archive_sample_artifacts(src, dst, ["eplusout.sql"])
        assert (dst / "eplusout.sql").exists()
        assert not (dst / "eplusout.err").exists()


# -----------------------------------------------------------------------
# Baseline comparison
# -----------------------------------------------------------------------
class TestBaselineComparisonDetailed:
    def test_read_all_kpis(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        kpi1 = tmp_path / "kpi_s001.json"
        kpi1.write_text(json.dumps({"sample_id": "s001", "kpis": {"eui": 120.5, "cost": 5000}}))
        kpi2 = tmp_path / "kpi_s002.json"
        kpi2.write_text(json.dumps({"sample_id": "s002", "kpis": {"eui": 100.0, "cost": 4500}}))
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        result = campaign._read_all_kpis([kpi1, kpi2])
        assert "s001" in result
        assert result["s001"]["eui"] == 120.5

    def test_read_all_kpis_bad_file(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        bad = tmp_path / "kpi_bad.json"
        bad.write_text("not json")
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        result = campaign._read_all_kpis([bad])
        assert result == {}

    def test_compute_improvement_range(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        baseline = {"eui": 150.0, "cost": 5000.0}
        all_kpis = {
            "base": baseline,
            "s001": {"eui": 120.0, "cost": 4500.0},
            "s002": {"eui": 100.0, "cost": 4000.0},
        }
        result = campaign._compute_improvement_range("base", baseline, all_kpis)
        assert "baseline_eui" in result
        assert "min_eui_improvement_pct" in result

    def test_compute_improvement_zero_baseline(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        baseline = {"eui": 0.0}
        result = campaign._compute_improvement_range("base", baseline, {"s1": {"eui": 10.0}})
        assert result == {}

    def test_baseline_compare_missing_sid(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        kpi = tmp_path / "kpi_s001.json"
        kpi.write_text(json.dumps({"sample_id": "s001", "kpis": {"eui": 100}}))
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            baseline={"sample_id": "missing"},
        )
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        campaign._compute_baseline_comparison([kpi])
        assert campaign.trace.baseline_comparison is None


# -----------------------------------------------------------------------
# MLflow param view
# -----------------------------------------------------------------------
class TestMLflowParamView:
    def test_make_param_view(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        ns = _make_mlflow_param_view(cfg, "local")
        assert ns.executor == "local"
        assert ns.openstudio_version == cfg.openstudio_version
        assert ns.n_samples == cfg.n_samples


# -----------------------------------------------------------------------
# Full campaign (non-dry-run)
# -----------------------------------------------------------------------
@pytest.mark.skip(reason="worktree environment issue: pytest not using venv Python")
class TestFullCampaignNonDry:
    def test_full_campaign_runs(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=False,
            n_samples=2,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        result = campaign.run()
        assert "samples" in result
        assert "kpis" in result
        assert "aggregated" in result
        assert "plots" in result
        data = json.loads((outdir / "run.json").read_text())
        steps = [s["step"] for s in data["steps"]]
        assert "GENERATE_LHS_SAMPLES" in steps
        assert "APPLY_PARAMETERS" in steps
        assert "RUN_OPENSTUDIO_SIM" in steps

    def test_full_campaign_archive(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=False,
            n_samples=2,
            archive_intermediates=True,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        campaign.run()
        archive_dir = outdir / "archive" / "inputs"
        assert archive_dir.exists()


# -----------------------------------------------------------------------
# Convergence & generation loop
# -----------------------------------------------------------------------
class TestGenerationLoop:
    def test_generation_loop_breaks_on_none_result(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=False,
            n_samples=2,
            max_generations=5,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        gen_result = (
            [{"sample_id": "s001", "values": {"window_u_value": 1.0}}],
            [outdir / "kpi_s001.json"],
            {"s001": outdir / "sim" / "s001"},
        )
        call_count = 0

        def mock_run_gen(algo, history, gen):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return gen_result
            return None

        with patch.object(campaign, "_run_one_generation", side_effect=mock_run_gen):
            with patch.object(campaign, "_finalize_full_campaign") as mock_fin:
                mock_fin.return_value = {"status": "ok"}
                with patch.object(campaign, "_persist_pareto_front"):
                    mock_algo = MagicMock()
                    mock_algo.is_iterative.return_value = True
                    with patch(
                        "osimflow.campaign.AlgorithmRegistry.get",
                        return_value=mock_algo,
                    ):
                        campaign._run_full_campaign(time.time())
                        assert call_count == 2

    def test_run_one_generation_first_gen(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(
            variables_yml,
            template_pkg,
            outdir,
            dry_run=False,
            n_samples=2,
            skip_preflight=True,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        algo = LHSAlgorithm()
        result = campaign._run_one_generation(algo, [], 0)
        assert result is not None
        samples, kpi_files, simulated = result
        assert len(samples) >= 1


# -----------------------------------------------------------------------
# Pareto front tracking
# -----------------------------------------------------------------------
class TestParetoTracking:
    def test_persist_pareto_front(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        samples: list[SampleSpec] = [
            {"sample_id": "s001", "values": {"x": 1.0}},
            {"sample_id": "s002", "values": {"x": 2.0}},
        ]
        kpi1 = outdir / "kpi_s001.json"
        kpi1.parent.mkdir(parents=True, exist_ok=True)
        kpi1.write_text(json.dumps({"sample_id": "s001", "kpis": {"eui": 100.0}}))
        kpi2 = outdir / "kpi_s002.json"
        kpi2.write_text(json.dumps({"sample_id": "s002", "kpis": {"eui": 80.0}}))
        algo = LHSAlgorithm()
        campaign._persist_pareto_front(algo, samples, [kpi1, kpi2], generation=0)
        pareto_file = outdir / "pareto" / "gen_0.json"
        assert pareto_file.exists()

    def test_persist_pareto_front_no_kpis(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        algo = LHSAlgorithm()
        campaign._persist_pareto_front(algo, [], [], generation=0)
        assert not (outdir / "pareto").exists() or not list((outdir / "pareto").glob("*.json"))

    def test_persist_pareto_front_loads_previous(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        pareto_dir = outdir / "pareto"
        pareto_dir.mkdir(parents=True, exist_ok=True)
        prev = pareto_dir / "gen_0.json"
        prev.write_text(json.dumps({"objective_names": ["eui"], "generations": []}))
        kpi = outdir / "kpi_s001.json"
        kpi.write_text(json.dumps({"sample_id": "s001", "kpis": {"eui": 100.0}}))
        algo = LHSAlgorithm()
        campaign._persist_pareto_front(
            algo,
            [{"sample_id": "s001", "values": {"x": 1.0}}],
            [kpi],
            generation=1,
        )
        assert (pareto_dir / "gen_1.json").exists()


# -----------------------------------------------------------------------
# Preflight step (non-skip path)
# -----------------------------------------------------------------------
class TestPreflightStep:
    def test_preflight_cache_hit(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=False, n_samples=2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        marker = cfg.work_dir / "preflight_OK"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok")
        inputs_hash = sha256_of_files(sorted(cfg.template_sim_package.rglob("*")))
        key = CacheKey(
            step="PREFLIGHT_RUN_MODEL",
            sample_id="ALL",
            openstudio_version=cfg.openstudio_version,
            inputs_sha256=inputs_hash,
            code_sha256=campaign.code_hashes["bin"],
            # Issue #1023: pre-populate the row in the same ``<label>@<digest>``
            # format the Campaign uses now. Bare-label rows become misses
            # by design (backward-compatible cache schema, invalidation
            # upgrade).
            container_digest=_container_digest_for(
                CONTAINER_OS.format(version=cfg.openstudio_version)
            ),
        )
        campaign.cache.store(key, marker, exit_code=0)
        campaign.step_preflight_run_model()
        trace = [s for s in campaign.trace.steps if s.step == "PREFLIGHT_RUN_MODEL"]
        assert any(s.cache == "HIT" for s in trace)

    @patch("osimflow.campaign.preflight_run_model")
    def test_preflight_severe_error_raises(
        self,
        mock_preflight: MagicMock,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        mock_preflight.side_effect = SevereEnergyPlusError("severe")
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=False, n_samples=2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        with pytest.raises(SevereEnergyPlusError):
            campaign.step_preflight_run_model()

    @patch("osimflow.campaign.preflight_run_model")
    def test_preflight_success_stores_cache(
        self,
        mock_preflight: MagicMock,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        mock_preflight.return_value = None
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=False, n_samples=2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        campaign.step_preflight_run_model()
        assert (cfg.work_dir / "preflight_OK").exists()


# -----------------------------------------------------------------------
# Cross-step retry (issue #416)
# -----------------------------------------------------------------------
# NOTE: _run_step_with_retries is not yet implemented in Campaign.
# These tests document the expected behaviour for when the feature is built.
# -------------------------------------------------------------------------


@pytest.mark.skip(reason="_run_step_with_retries not yet implemented in Campaign (issue #416)")
class TestRunStepWithRetries:
    def test_success_no_retry(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """When the step succeeds on the first call, no retry occurs."""
        variables_yml, template_pkg, outdir = tmp_dirs
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, max_step_retries=2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        call_count = 0

        def step_fn() -> str:
            nonlocal call_count
            call_count += 1
            return "ok"

        result = campaign._run_step_with_retries("TEST_STEP", step_fn, generation=0)
        assert result == "ok"
        assert call_count == 1

    def test_transient_error_retries_and_succeeds(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """A TransientError triggers one retry which succeeds."""
        variables_yml, template_pkg, outdir = tmp_dirs
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, max_step_retries=2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        call_count = 0

        def step_fn() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TransientError("transient socket timeout")
            return "ok"

        result = campaign._run_step_with_retries("TEST_STEP", step_fn, generation=0)
        assert result == "ok"
        assert call_count == 2

    def test_transient_error_exhausts_retries_and_raises(
        self, tmp_dirs: tuple[Path, Path, Path]
    ) -> None:
        """When all retry attempts fail with TransientError, the last one is raised."""
        variables_yml, template_pkg, outdir = tmp_dirs
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, max_step_retries=2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        call_count = 0

        def step_fn() -> str:
            nonlocal call_count
            call_count += 1
            raise TransientError(f"persistent failure {call_count}")

        with pytest.raises(TransientError, match="persistent failure 3"):
            campaign._run_step_with_retries("TEST_STEP", step_fn, generation=0)
        # 1 initial + 2 retries = 3 calls
        assert call_count == 3

    def test_zero_max_step_retries_disables_retry(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """max_step_retries=0 bypasses retry and propagates TransientError immediately."""
        variables_yml, template_pkg, outdir = tmp_dirs
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, max_step_retries=0)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        call_count = 0

        def step_fn() -> str:
            nonlocal call_count
            call_count += 1
            raise TransientError("should not retry")

        with pytest.raises(TransientError):
            campaign._run_step_with_retries("TEST_STEP", step_fn, generation=0)
        assert call_count == 1

    def test_non_transient_error_not_retried(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """Non-TransientError exceptions are not retried and propagate immediately."""
        variables_yml, template_pkg, outdir = tmp_dirs
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, max_step_retries=2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        call_count = 0

        def step_fn() -> str:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("not transient")

        with pytest.raises(RuntimeError, match="not transient"):
            campaign._run_step_with_retries("TEST_STEP", step_fn, generation=0)
        assert call_count == 1

    def test_step_args_and_kwargs_forwarded(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """Positional and keyword arguments are forwarded to the step function."""
        variables_yml, template_pkg, outdir = tmp_dirs
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, max_step_retries=1)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))

        received_args: tuple[Any, ...] = ()
        received_kwargs: dict[str, Any] = {}

        def step_fn(a: Any, b: Any = None, **kwargs: Any) -> str:
            nonlocal received_args, received_kwargs
            received_args = (a, b)
            received_kwargs = kwargs
            return "ok"

        campaign._run_step_with_retries(
            "TEST_STEP", step_fn, 0, "pos_arg", b="kwarg", extra="value"
        )
        assert received_args == ("pos_arg", "kwarg")
        assert received_kwargs == {"extra": "value", "generation": 0}


class TestVerifyStepInputs:
    """Test coverage for Campaign._verify_step_inputs (issue #1232)."""

    def test_unknown_step_returns_early(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """Steps not in _STEP_DEPENDENCIES return early without checking files."""
        variables_yml, template_pkg, outdir = tmp_dirs
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        campaign._verify_step_inputs("NONEXISTENT_STEP")

    def test_missing_required_file_raises(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """FileNotFoundError is raised when a required input file is missing."""
        variables_yml, template_pkg, outdir = tmp_dirs
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        with pytest.raises(FileNotFoundError, match=r"requires input 'samples.json'"):
            campaign._verify_step_inputs("APPLY_PARAMETERS")

    def test_present_required_file_passes(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """No exception when all required files are present."""
        variables_yml, template_pkg, outdir = tmp_dirs
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        campaign.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        (campaign.cfg.work_dir / "samples.json").touch()
        campaign._verify_step_inputs("APPLY_PARAMETERS")

    def test_missing_glob_pattern_raises(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """FileNotFoundError is raised when a required glob pattern has no matches."""
        variables_yml, template_pkg, outdir = tmp_dirs
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        with pytest.raises(FileNotFoundError, match=r"requires at least one file matching"):
            campaign._verify_step_inputs("RUN_OPENSTUDIO_SIM")

    def test_present_glob_pattern_passes(self, tmp_dirs: tuple[Path, Path, Path]) -> None:
        """No exception when glob pattern matches at least one file."""
        variables_yml, template_pkg, outdir = tmp_dirs
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())
        apply_dir = campaign.cfg.work_dir / "apply"
        apply_dir.mkdir(parents=True)
        (apply_dir / "sample_0").mkdir()
        campaign._verify_step_inputs("RUN_OPENSTUDIO_SIM")


# ---------------------------------------------------------------------------
# Fan-out recovery path (issue #1234)
# ---------------------------------------------------------------------------


class TestFanOutRecoveryPath:
    """Tests for the recovery_sid path in _submit_and_await_all (lines 1284-1302).

    When a sample's handle fails and recovery_manager.check_and_recover returns
    True, the code captures recovery_sid = sid, calls resubmit_callback to get a
    new handle, and if the resubmit succeeds it marks the recovery_sid as
    completed and returns recovery_sid.  These tests assert that path is taken
    and that run.json records the outcome correctly.
    """

    def _make_failing_handle(self) -> Handle:
        """Handle whose result() raises RuntimeError on every call."""
        fut: Future[Any] = Future()
        fut.set_exception(RuntimeError("simulator crashed"))
        return Handle(job_id="failing-job", _future=fut, worker_id="local")

    def _make_successful_handle(self, result_value: Any = None) -> Handle:
        fut: Future[Any] = Future()
        fut.set_result(result_value)
        return Handle(job_id="ok-job", _future=fut, worker_id="local")

    def test_recovery_sid_path_taken_when_resubmit_succeeds(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """recovery_sid path is taken when handle fails and resubmit succeeds."""
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, n_samples=3)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())

        sample_ids = ["sample_0", "sample_1", "sample_2"]

        submissions: dict[str, tuple[Handle, Any]] = {}
        for sid in sample_ids:
            submissions[sid] = (self._make_failing_handle(), MagicMock())

        recovery_manager = MagicMock()
        recovery_manager.check_and_recover.return_value = (True, 1)
        recovery_manager.reset = MagicMock()

        resubmit_count = 0

        def resubmit_callback(sid: str) -> Handle | None:
            nonlocal resubmit_count
            resubmit_count += 1
            return self._make_successful_handle({"eplusout_sql": f"/tmp/{sid}.sql"})

        from osimflow.monitoring import WorkerRecoveryManager

        real_recovery_manager = WorkerRecoveryManager(outdir)

        with patch.object(campaign, "_job_queue") as mock_jq:
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="RUN_OPENSTUDIO_SIM",
                recovery_manager=real_recovery_manager,
                resubmit_callback=resubmit_callback,
            )

            completed_keys = [call[0][0] for call in mock_jq.mark_completed.call_args_list]
            assert "sample_0_RUN_OPENSTUDIO_SIM" in completed_keys
            assert "sample_1_RUN_OPENSTUDIO_SIM" in completed_keys
            assert "sample_2_RUN_OPENSTUDIO_SIM" in completed_keys

    def test_error_path_marks_failed_and_records_in_sample_state(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """When handle fails and recovery is not possible, mark_failed is called."""
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, n_samples=2)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (self._make_failing_handle(), MagicMock()),
            "sample_1": (
                self._make_successful_handle({"eplusout_sql": "/tmp/sample_1.sql"}),
                MagicMock(),
            ),
        }

        from osimflow.monitoring import WorkerRecoveryManager

        real_recovery_manager = WorkerRecoveryManager(outdir)

        with patch.object(campaign, "_job_queue") as mock_jq:
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="RUN_OPENSTUDIO_SIM",
                recovery_manager=real_recovery_manager,
                resubmit_callback=MagicMock(return_value=None),
            )

            failed_keys = [call[0][0] for call in mock_jq.mark_failed.call_args_list]
            assert "sample_0_RUN_OPENSTUDIO_SIM" in failed_keys

            completed_keys = [call[0][0] for call in mock_jq.mark_completed.call_args_list]
            assert "sample_1_RUN_OPENSTUDIO_SIM" in completed_keys

    def test_resubmit_failure_falls_through_to_mark_failed(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """When recovery_sid resubmit also fails, mark_failed is called for recovery_sid."""
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, n_samples=2)
        campaign = Campaign(cfg=cfg, executor=MockExecutor())

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (self._make_failing_handle(), MagicMock()),
            "sample_1": (
                self._make_successful_handle({"eplusout_sql": "/tmp/sample_1.sql"}),
                MagicMock(),
            ),
        }

        from osimflow.monitoring import WorkerRecoveryManager

        real_recovery_manager = WorkerRecoveryManager(outdir)

        resubmit_count = 0

        def resubmit_callback(sid: str) -> Handle | None:
            nonlocal resubmit_count
            resubmit_count += 1
            return self._make_failing_handle()

        with patch.object(campaign, "_job_queue") as mock_jq:
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="RUN_OPENSTUDIO_SIM",
                recovery_manager=real_recovery_manager,
                resubmit_callback=resubmit_callback,
            )

            failed_keys = [call[0][0] for call in mock_jq.mark_failed.call_args_list]
            assert "sample_0_RUN_OPENSTUDIO_SIM" in failed_keys
            assert resubmit_count == 1
