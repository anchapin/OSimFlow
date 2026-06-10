"""Regression tests for the campaign performance benchmark (issue #10).

These tests assert the `bench_campaign.run_benchmark()` entry point
behaves correctly:

  * Writes a `benchmarks.json` artifact with the documented schema.
  * The cold-cache wall-clock stays under a configurable threshold
    (default 30s, override via the `OSIMFLOW_BENCH_THRESHOLD_S` env var).
  * The warm-cache (re-run) wall-clock is strictly smaller than the
    cold-cache wall-clock — i.e. caching is actually working. This is
    the regression test for orchestrator overhead: if someone removes
    or breaks the cache, the warm run will be as slow as the cold one
    and the test fails loudly.

The benchmark fixture uses a per-test `tmp_path` so the cache is
genuinely cold for the first run and warm for the second, mirroring
the `tests/integration/test_cache_invalidation.py` pattern. A
module-scoped fixture runs the benchmark once and shares the metrics
across the read-only assertions to keep the suite well under the
60-second acceptance criterion from issue #10 (otherwise each test
would re-run cold+warm = 2 campaigns).

Acceptance criteria from issue #10:
  - [x] `pytest tests/benchmarks/` runs in <60s locally.
  - [x] The benchmark fails if the cold-cache wall-clock exceeds the
        threshold.
  - [x] The CI workflow uploads the `benchmarks.json` as an artifact
        on every PR.
  - [x] `docs/benchmarks.md` exists with a one-paragraph explanation
        of how to interpret the results.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from osimflow import CampaignConfig
from osimflow.executors import LocalExecutor

# Import the benchmark under test. We import it lazily at test-time so
# the import cost (and any side effects) only hit the benchmark suite,
# not every test in the project.
from tests.benchmarks import bench_campaign


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_workspace(tmp_path: Path) -> Path:
    """A clean per-test workspace with `variables.yml` and a minimal
    template_sim_package that satisfies the apply-step pre-flight check."""
    wd = tmp_path / "bench"
    wd.mkdir()

    # 2 uniform + 1 lognormal — minimal but exercises the LHS sampler
    # and the apply-step pre-flight check. We deliberately keep the
    # distribution count small so the bench stays under 60s.
    (wd / "variables.yml").write_text(
        yaml.safe_dump(
            {
                "variables": [
                    {"name": "u1", "distribution": "uniform", "min": 0.0, "max": 1.0},
                    {"name": "u2", "distribution": "uniform", "min": 10.0, "max": 20.0},
                    {"name": "ln1", "distribution": "lognormal", "mean": 0.0, "sigma": 0.5},
                ]
            }
        )
    )

    template = wd / "template"
    template.mkdir()
    # The stub apply step expects a JSON .osm that declares each
    # variable name as an attribute. The fixture mirrors
    # `tests/integration/test_campaign.py::template_pkg` so the apply
    # step passes the pre-flight check.
    (template / "model.osm").write_text(
        json.dumps({"attributes": {"u1": 0.0, "u2": 10.0, "ln1": 1.0}})
    )
    (template / "workflow.osw").write_text(json.dumps({"name": "stub"}))
    return wd


@pytest.fixture(scope="module")
def shared_metrics(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run the benchmark once per module and share the result across
    the read-only assertions. Running it 7 times would re-run cold+warm
    = 14 campaigns and push us past the 60s acceptance criterion; one
    canonical run is enough to validate the artifact shape and the
    cold-vs-warm relationship.
    """
    wd = tmp_path_factory.mktemp("bench_module")
    workspace = _make_workspace(wd)
    cfg = CampaignConfig(
        input_variables=workspace / "variables.yml",
        template_sim_package=workspace / "template",
        n_samples=3,
        outdir=workspace / "out",
        openstudio_version="3.4.0",
        archive_intermediates=False,
    )
    return bench_campaign.run_benchmark(cfg, executor=LocalExecutor(max_workers=2))


@pytest.fixture
def bench_workspace(tmp_path: Path) -> Path:
    """A clean per-test workspace for the per-test re-runs (threshold
    override + executor cleanup)."""
    return _make_workspace(tmp_path)


# ---------------------------------------------------------------------------
# Test 1 — bench script writes a benchmarks.json with the documented shape
# ---------------------------------------------------------------------------
def test_bench_writes_benchmarks_json(shared_metrics: dict[str, Any]) -> None:
    """`run_benchmark()` writes `${outdir}/benchmarks.json` whose shape
    matches the schema documented in `docs/benchmarks.md`."""
    assert isinstance(shared_metrics, dict)
    assert "cold_wall_s" in shared_metrics
    assert "warm_wall_s" in shared_metrics
    assert "cold_per_step_s" in shared_metrics
    assert "warm_per_step_s" in shared_metrics
    assert "n_samples" in shared_metrics
    assert "executor" in shared_metrics
    assert "openstudio_version" in shared_metrics
    assert "threshold_cold_s" in shared_metrics
    assert "passed" in shared_metrics

    # The module-scoped run wrote benchmarks.json to a tmp dir we no
    # longer have a direct handle on. Re-run with a fresh workspace
    # to assert the on-disk artifact shape (covers the file-write
    # branch in isolation).
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        workspace = _make_workspace(Path(td))
        cfg = CampaignConfig(
            input_variables=workspace / "variables.yml",
            template_sim_package=workspace / "template",
            n_samples=3,
            outdir=workspace / "out",
            openstudio_version="3.4.0",
            archive_intermediates=False,
        )
        bench_campaign.run_benchmark(cfg, executor=LocalExecutor(max_workers=2))

        bench_json = cfg.outdir / "benchmarks.json"
        assert bench_json.is_file(), f"expected {bench_json} to exist"
        data = json.loads(bench_json.read_text())
        assert data["schema_version"] == 1
        assert data["n_samples"] == 3
        assert data["executor"] == "local"
        assert data["openstudio_version"] == "3.4.0"
        # Per-step dicts map step name -> elapsed_s (float).
        for step_name in (
            "GENERATE_LHS_SAMPLES",
            "APPLY_PARAMETERS",
            "RUN_OPENSTUDIO_SIM",
            "EXTRACT_KPIS",
            "AGGREGATE_RESULTS",
        ):
            assert step_name in data["cold_per_step_s"]
            assert isinstance(data["cold_per_step_s"][step_name], (int, float))


# ---------------------------------------------------------------------------
# Test 2 — cold-cache wall-clock stays under the threshold
# ---------------------------------------------------------------------------
def test_bench_cold_wall_under_threshold(shared_metrics: dict[str, Any]) -> None:
    """The cold-cache wall-clock must stay under the configured threshold.

    The default threshold is 30s (generous for the stub work, tight
    enough to catch orchestrator regressions). The test reads
    `OSIMFLOW_BENCH_THRESHOLD_S` from the environment so CI runners
    or local devs can override it without editing source.
    """
    threshold = float(os.environ.get("OSIMFLOW_BENCH_THRESHOLD_S", "30.0"))
    assert shared_metrics["cold_wall_s"] < threshold, (
        f"cold-cache wall-clock {shared_metrics['cold_wall_s']:.2f}s "
        f"exceeds threshold {threshold:.2f}s — possible orchestrator "
        f"regression"
    )


# ---------------------------------------------------------------------------
# Test 3 — warm-cache wall-clock is strictly smaller (cache regression guard)
# ---------------------------------------------------------------------------
def test_bench_warm_is_faster_than_cold(shared_metrics: dict[str, Any]) -> None:
    """A second run on the same outdir is a full cache hit and must be
    faster than the cold run. If this fails, the cache is broken and
    the orchestrator is redoing work on every resume (the 288x
    speedup documented in `.agents/results/decision-verdict.md` §1
    would have evaporated)."""
    assert shared_metrics["warm_wall_s"] < shared_metrics["cold_wall_s"], (
        f"warm run ({shared_metrics['warm_wall_s']:.3f}s) is not faster "
        f"than cold run ({shared_metrics['cold_wall_s']:.3f}s) — cache "
        f"is broken"
    )


# ---------------------------------------------------------------------------
# Test 4 — `passed` flag reflects the threshold check
# ---------------------------------------------------------------------------
def test_bench_passed_flag(shared_metrics: dict[str, Any]) -> None:
    """The `passed` flag is `True` iff cold_wall_s < threshold.

    This is the gate the CI workflow surfaces in its summary line —
    keeps a single source of truth (the metrics dict) that the
    benchmark artifact itself records the verdict.
    """
    expected = shared_metrics["cold_wall_s"] < shared_metrics["threshold_cold_s"]
    assert shared_metrics["passed"] is expected


# ---------------------------------------------------------------------------
# Test 5 — peak RSS is recorded when `resource.getrusage` is available
# ---------------------------------------------------------------------------
def test_bench_records_peak_rss_on_posix(shared_metrics: dict[str, Any]) -> None:
    """On POSIX systems, the benchmark records the calling process's
    peak RSS via `resource.getrusage`. The field is optional — on
    Windows it stays `None` — but on Linux/macOS it must be a
    positive integer (KB on Linux, bytes on macOS; we just check
    it's > 0).
    """
    if not bench_campaign._HAS_RUSAGE:  # noqa: SLF001
        pytest.skip("resource.getrusage unavailable on this platform")

    assert "peak_rss" in shared_metrics
    rss = shared_metrics["peak_rss"]
    assert rss is not None
    assert rss > 0


# ---------------------------------------------------------------------------
# Test 6 — `run_benchmark()` shuts down the executor it was given
# ---------------------------------------------------------------------------
def test_bench_cleans_up_executor(bench_workspace: Path) -> None:
    """`run_benchmark()` must call `.shutdown()` on the executor it
    was given. A leaked thread pool leaks file handles across tests
    and is a CI flake risk; assert it's gone after the call.
    """
    cfg = CampaignConfig(
        input_variables=bench_workspace / "variables.yml",
        template_sim_package=bench_workspace / "template",
        n_samples=3,
        outdir=bench_workspace / "out",
        openstudio_version="3.4.0",
        archive_intermediates=False,
    )
    executor = LocalExecutor(max_workers=2)
    bench_campaign.run_benchmark(cfg, executor=executor)
    # `ThreadPoolExecutor` flips `_shutdown` to True after `.shutdown()`.
    # Accessing private state here is justified by the test's purpose.
    assert executor._pool._shutdown is True  # noqa: SLF001


# ---------------------------------------------------------------------------
# Test 7 — the benchmark module exposes a CLI entry point
# ---------------------------------------------------------------------------
def test_bench_module_exposes_main() -> None:
    """`bench_campaign` must expose a `main()` function so the CI
    workflow can call `python -m tests.benchmarks.bench_campaign`
    and the GitHub Actions `bench` job can upload the resulting
    `benchmarks.json` artifact."""
    assert callable(getattr(bench_campaign, "main", None))
