"""Unit tests for osimflow/monitoring.py (issue #213).

Covers:
- StepTrace: construction, to_dict
- SampleTrace: construction, to_dict with None filtering
- RunTrace: lifecycle, step hooks, serialization, write
- sample_log_paths: directory creation, path structure
- Quality summary computation
- Baseline comparison data
"""

import json
from pathlib import Path

from osimflow.monitoring import RunTrace, SampleTrace, StepTrace, sample_log_paths


class TestStepTrace:
    def test_construction(self) -> None:
        st = StepTrace(step="GENERATE_LHS_SAMPLES", cache="MISS", elapsed_s=1.5, exit_code=0)
        assert st.step == "GENERATE_LHS_SAMPLES"
        assert st.cache == "MISS"
        assert st.elapsed_s == 1.5
        assert st.exit_code == 0

    def test_to_dict(self) -> None:
        st = StepTrace(step="RUN_OPENSTUDIO_SIM", cache="HIT", elapsed_s=3.0, exit_code=0)
        d = st.to_dict()
        assert d == {"step": "RUN_OPENSTUDIO_SIM", "cache": "HIT", "elapsed_s": 3.0, "exit_code": 0}

    def test_cache_hit_label(self) -> None:
        st = StepTrace(step="S", cache="HIT", elapsed_s=0.1, exit_code=0)
        assert st.cache == "HIT"

    def test_cache_miss_label(self) -> None:
        st = StepTrace(step="S", cache="MISS", elapsed_s=5.0, exit_code=0)
        assert st.cache == "MISS"

    def test_cache_skipped_label(self) -> None:
        st = StepTrace(step="S", cache="SKIPPED", elapsed_s=0.0, exit_code=0)
        assert st.cache == "SKIPPED"

    def test_failed_exit_code(self) -> None:
        st = StepTrace(step="S", cache="MISS", elapsed_s=2.0, exit_code=1)
        assert st.exit_code == 1


class TestSampleTrace:
    def test_minimal(self) -> None:
        st = SampleTrace(sample_id="s0001", status="ok", elapsed_s=1.0)
        assert st.sample_id == "s0001"
        assert st.status == "ok"
        assert st.apply_exit_code == 0
        assert st.sim_exit_code == 0
        assert st.extract_exit_code == 0
        assert st.error_summary is None

    def test_to_dict_drops_none(self) -> None:
        st = SampleTrace(sample_id="s0001", status="ok", elapsed_s=1.0)
        d = st.to_dict()
        assert "error_summary" not in d
        assert "eplusout_sql" not in d
        assert "quality_valid" not in d
        assert "generation" not in d
        assert "worker_id" not in d
        assert "sample_id" in d
        assert "status" in d

    def test_to_dict_includes_non_none(self) -> None:
        st = SampleTrace(
            sample_id="s0001",
            status="failed",
            elapsed_s=5.0,
            sim_exit_code=1,
            error_summary="SIM: RuntimeError",
            eplusout_sql="/path/to/eplusout.sql",
            generation=2,
            worker_id="local-123",
            worker_ip="localhost",
            worker_region="us-east-1",
        )
        d = st.to_dict()
        assert d["error_summary"] == "SIM: RuntimeError"
        assert d["eplusout_sql"] == "/path/to/eplusout.sql"
        assert d["generation"] == 2
        assert d["worker_id"] == "local-123"
        assert d["worker_ip"] == "localhost"
        assert d["worker_region"] == "us-east-1"

    def test_status_cached(self) -> None:
        st = SampleTrace(sample_id="s0001", status="cached", elapsed_s=0.01)
        assert st.status == "cached"

    def test_quality_fields(self) -> None:
        st = SampleTrace(
            sample_id="s0001",
            status="ok",
            elapsed_s=1.0,
            quality_valid=False,
            quality_warnings=2,
            quality_failures=1,
        )
        d = st.to_dict()
        assert d["quality_valid"] is False
        assert d["quality_warnings"] == 2
        assert d["quality_failures"] == 1


class TestRunTrace:
    def test_lifecycle(self) -> None:
        trace = RunTrace(campaign_id="test-001", config_summary={"executor": "local"})
        assert trace.started_at > 0
        assert trace.finished_at is None
        assert trace.steps == []
        assert trace.per_sample == []

    def test_step_finished_appends(self) -> None:
        trace = RunTrace(campaign_id="test-001", config_summary={})
        trace.step_finished("GENERATE_LHS_SAMPLES", cache="MISS", elapsed_s=1.0, exit_code=0)
        assert len(trace.steps) == 1
        assert trace.steps[0].step == "GENERATE_LHS_SAMPLES"
        assert trace.steps[0].cache == "MISS"

    def test_step_finished_with_cache_hit(self) -> None:
        trace = RunTrace(campaign_id="test-001", config_summary={})
        trace.step_finished("RUN_OPENSTUDIO_SIM", cache="HIT", elapsed_s=0.05, exit_code=0)
        assert trace.steps[0].cache == "HIT"

    def test_multiple_steps(self) -> None:
        trace = RunTrace(campaign_id="test-001", config_summary={})
        trace.step_finished("GENERATE_LHS_SAMPLES", "MISS", 1.0, 0)
        trace.step_finished("PREFLIGHT_RUN_MODEL", "MISS", 2.0, 0)
        trace.step_finished("APPLY_PARAMETERS", "MISS", 3.0, 0)
        assert len(trace.steps) == 3
        assert [s.step for s in trace.steps] == [
            "GENERATE_LHS_SAMPLES",
            "PREFLIGHT_RUN_MODEL",
            "APPLY_PARAMETERS",
        ]

    def test_sample_done(self) -> None:
        trace = RunTrace(campaign_id="test-001", config_summary={})
        trace.sample_done(SampleTrace(sample_id="s0001", status="ok", elapsed_s=1.0))
        trace.sample_done(SampleTrace(sample_id="s0002", status="failed", elapsed_s=2.0))
        assert len(trace.per_sample) == 2
        assert trace.per_sample[0].sample_id == "s0001"
        assert trace.per_sample[1].status == "failed"

    def test_finalize_sets_finished_at(self) -> None:
        trace = RunTrace(campaign_id="test-001", config_summary={})
        assert trace.finished_at is None
        trace.finalize()
        assert trace.finished_at is not None
        assert trace.finished_at >= trace.started_at


class TestRunTraceSerialization:
    def test_to_dict_schema(self) -> None:
        trace = RunTrace(
            campaign_id="test-001", config_summary={"executor": "local", "n_samples": 5}
        )
        trace.step_finished("GENERATE_LHS_SAMPLES", "MISS", 1.0, 0)
        trace.sample_done(SampleTrace(sample_id="s0001", status="ok", elapsed_s=1.0))
        trace.sample_done(SampleTrace(sample_id="s0002", status="failed", elapsed_s=2.0))
        trace.finalize()

        d = trace.to_dict()
        assert d["schema_version"] == 1
        assert d["campaign_id"] == "test-001"
        assert "started_at" in d
        assert "finished_at" in d
        assert "elapsed_s" in d
        assert d["config"]["executor"] == "local"
        assert d["summary"]["n_samples"] == 2
        assert d["summary"]["n_succeeded"] == 1
        assert d["summary"]["n_failed"] == 1
        assert len(d["steps"]) == 1
        assert len(d["per_sample"]) == 2

    def test_to_dict_empty(self) -> None:
        trace = RunTrace(campaign_id="empty", config_summary={})
        trace.finalize()
        d = trace.to_dict()
        assert d["summary"]["n_samples"] == 0
        assert d["summary"]["n_succeeded"] == 0
        assert d["summary"]["n_failed"] == 0
        assert d["steps"] == []
        assert d["per_sample"] == []

    def test_quality_summary(self) -> None:
        trace = RunTrace(campaign_id="q-test", config_summary={})
        trace.sample_done(
            SampleTrace(sample_id="s1", status="ok", elapsed_s=1.0, quality_valid=True)
        )
        trace.sample_done(
            SampleTrace(sample_id="s2", status="ok", elapsed_s=1.0, quality_valid=False)
        )
        trace.sample_done(
            SampleTrace(sample_id="s3", status="ok", elapsed_s=1.0, quality_warnings=3)
        )
        trace.finalize()
        d = trace.to_dict()
        qs = d["quality_summary"]
        assert qs["n_quality_failures"] == 1
        assert qs["n_quality_warnings"] == 1
        assert qs["n_quality_ok"] == 1

    def test_baseline_comparison_included(self) -> None:
        trace = RunTrace(campaign_id="bl-test", config_summary={})
        trace.baseline_comparison = {"baseline_eui": 100.0, "min_improvement_pct": 5.0}
        trace.finalize()
        d = trace.to_dict()
        assert "baseline_comparison" in d
        assert d["baseline_comparison"]["baseline_eui"] == 100.0

    def test_baseline_comparison_absent_when_none(self) -> None:
        trace = RunTrace(campaign_id="no-bl", config_summary={})
        trace.finalize()
        d = trace.to_dict()
        assert "baseline_comparison" not in d

    def test_hook_timing_included(self) -> None:
        trace = RunTrace(campaign_id="hook-test", config_summary={})
        trace.init_script_duration_s = 1.5
        trace.finalize_script_duration_s = 2.5
        trace.finalize()
        d = trace.to_dict()
        assert d["init_script_duration_s"] == 1.5
        assert d["finalize_script_duration_s"] == 2.5

    def test_hook_timing_absent_when_none(self) -> None:
        trace = RunTrace(campaign_id="no-hook", config_summary={})
        trace.finalize()
        d = trace.to_dict()
        assert "init_script_duration_s" not in d
        assert "finalize_script_duration_s" not in d


class TestRunTraceWrite:
    def test_writes_json(self, tmp_path: Path) -> None:
        trace = RunTrace(campaign_id="write-test", config_summary={"executor": "local"})
        trace.step_finished("STEP_A", "MISS", 1.0, 0)
        trace.finalize()
        out_file = tmp_path / "run.json"
        trace.write(out_file)

        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert data["campaign_id"] == "write-test"
        assert len(data["steps"]) == 1

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        trace = RunTrace(campaign_id="dir-test", config_summary={})
        trace.finalize()
        nested = tmp_path / "a" / "b" / "run.json"
        trace.write(nested)
        assert nested.exists()

    def test_overwrites_on_rerun(self, tmp_path: Path) -> None:
        out_file = tmp_path / "run.json"
        trace1 = RunTrace(campaign_id="first", config_summary={})
        trace1.step_finished("STEP_A", "MISS", 1.0, 0)
        trace1.finalize()
        trace1.write(out_file)

        trace2 = RunTrace(campaign_id="second", config_summary={})
        trace2.step_finished("STEP_B", "HIT", 0.1, 0)
        trace2.finalize()
        trace2.write(out_file)

        data = json.loads(out_file.read_text())
        assert data["campaign_id"] == "second"
        assert len(data["steps"]) == 1
        assert data["steps"][0]["step"] == "STEP_B"

    def test_elapsed_s_computed(self, tmp_path: Path) -> None:
        trace = RunTrace(campaign_id="elapsed-test", config_summary={})
        trace.finalize()
        d = trace.to_dict()
        assert d["elapsed_s"] >= 0


class TestSampleLogPaths:
    def test_creates_directories(self, tmp_path: Path) -> None:
        stdout, stderr = sample_log_paths(tmp_path, "s0001")
        assert stdout.parent.exists()
        assert stderr.parent.exists()

    def test_correct_paths(self, tmp_path: Path) -> None:
        stdout, stderr = sample_log_paths(tmp_path, "s0001")
        assert stdout == tmp_path / "work" / "sim" / "s0001" / "stdout.log"
        assert stderr == tmp_path / "work" / "sim" / "s0001" / "stderr.log"

    def test_idempotent(self, tmp_path: Path) -> None:
        s1, e1 = sample_log_paths(tmp_path, "s0001")
        s2, e2 = sample_log_paths(tmp_path, "s0001")
        assert s1 == s2
        assert e1 == e2
