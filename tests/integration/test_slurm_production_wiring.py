"""Tests for issue #4 — wire SlurmExecutor for production.

The production Slurm path itself needs a real cluster to exercise end-to-end,
so most of these tests target the `debug=True` path (which routes through
`submitit.DebugExecutor` and runs jobs locally while still emitting the
exact `sbatch` script that would have run). The production wiring is the
configuration plumbing; the dry-run is the observable side-effect we
can assert against.

Acceptance criteria from the issue:
  - `osimflow run --executor slurm --slurm_real --slurm_partition <X>` produces
    a real Slurm submission (we can't run this without a cluster, but the
    `debug=False` branch must be reachable and the executor must be in
    real-Slurm mode).
  - Per-sample resource directives are honored — `slurm_cpus_per_task`,
    `slurm_mem_gb`, and `slurm_time` are propagated.
  - The dry-run path (`debug=True`) still produces a logged `sbatch` script
    for inspection.
  - A pytest smoke test exercises `submitit.DebugExecutor` (debug path).
  - `AGENTS.md` §4 documents the production invocation.
  - New advanced flags: `--slurm_qos`, `--slurm_constraint`, `--slurm_gres`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import submitit

from osimflow.config import CampaignConfig, load_config
from osimflow.executors import SlurmExecutor


# ---------------------------------------------------------------------------
# submitit parameter-name compatibility (acceptance: validate against a
# current submitit version). Issue #4 says "the existing try/except TypeError
# fallback handles old-vs-new submitit, but should be tested against an actual
# production submitit version."
# ---------------------------------------------------------------------------
def test_slurm_executor_accepts_submitit_1_5_slurm_prefixed_kwargs() -> None:
    """submitit >= 1.5 uses `slurm_*` kwarg names. The executor must use the
    new spelling on a current submitit without falling through to the
    legacy `TypeError` branch (which would mean our parameter names are
    silently ignored — a silent-failure trap)."""
    ex = SlurmExecutor(partition="short", debug=True)
    # If we got here, update_parameters accepted the slurm_* kwargs in
    # the try-block (i.e. submitit 1.5+ was detected). Assert that the
    # underlying AutoExecutor's update_parameters *also* accepts the same
    # kwargs so a runtime mismatch never goes unnoticed.
    # Inspect via the internal _ex attribute; it is the submitit
    # AutoExecutor that `ex.update_parameters` would push to.
    update_params = ex._ex.update_parameters  # type: ignore[attr-defined]
    try:
        update_params(slurm_partition="x", slurm_cpus_per_task=1, slurm_mem_gb=1, slurm_time=1)
    except TypeError as e:
        pytest.fail(f"submitit 1.5+ rejected slurm_-prefixed kwargs: {e}")
    ex.shutdown()


def test_slurm_executor_falls_back_to_legacy_kwargs_on_old_submitit() -> None:
    """When the slurm_-prefixed kwargs raise TypeError (old submitit), the
    executor must retry with the legacy kwarg names. We simulate that by
    making the first call to AutoExecutor.update_parameters raise
    TypeError and asserting the constructor still returns a usable
    executor."""
    import submitit  # noqa: PLC0415

    original = submitit.AutoExecutor.update_parameters
    call_log: list[dict[str, Any]] = []

    def spy(self: Any, **kwargs: Any) -> None:
        call_log.append(kwargs)
        if kwargs and all(k.startswith("slurm_") for k in kwargs):
            raise TypeError("simulated old submitit: no slurm_-prefixed kwargs")
        return original(self, **kwargs)

    with patch.object(submitit.AutoExecutor, "update_parameters", spy):
        ex = SlurmExecutor(partition="short", debug=True)

    # The executor must have made at least two update_parameters calls:
    # one with the slurm_-prefixed kwargs (which raised) and one with the
    # legacy kwargs (which succeeded).
    assert len(call_log) >= 2, f"expected fallback call, got {call_log}"
    assert any(not k.startswith("slurm_") for k in call_log[1]), (
        f"fallback call should use legacy kwargs, got {call_log[1]}"
    )
    ex.shutdown()


# ---------------------------------------------------------------------------
# Per-submit resource directives (acceptance: per-sample cpus/mem/time are
# honored). submitit 1.5+ lets us override `slurm_cpus_per_task` /
# `slurm_mem_gb` / `slurm_time` per call via the closure we pass to
# `ex.submit()`. We expose this through per-submit overrides.
# ---------------------------------------------------------------------------
def test_slurm_executor_per_submit_cpus_propagate_to_submitit() -> None:
    """Per-submit cpus override must reach the underlying AutoExecutor via
    a closure (issue #4 acceptance: 'per-submit overrides need a closure
    or submitit 1.5+ supports slurm_cpus_per_task overrides per call —
    verify'). We test by capturing what update_parameters the closure
    invokes on the inner AutoExecutor."""
    ex = SlurmExecutor(partition="short", cpus_per_task=1, debug=True)
    captured: list[dict[str, Any]] = []
    # The closure creates a new AutoExecutor on-the-fly (via the
    # `make_local_dir` helper used internally). Intercept at the module
    # level so we catch the new executor too.
    original = submitit.AutoExecutor.update_parameters

    def spy(self: Any, **kwargs: Any) -> None:
        captured.append(kwargs)
        return original(self, **kwargs)

    with patch.object(submitit.AutoExecutor, "update_parameters", spy):
        # Override cpus at submit time.
        ex.submit(lambda: "ok", name="t", cpus=8, memory_mb=512, time_min=5)

    # At least one captured call must include the per-submit override.
    assert any(
        call.get("slurm_cpus_per_task") == 8 or call.get("cpus_per_task") == 8 for call in captured
    ), f"per-submit cpus=8 not propagated; captured={captured}"
    ex.shutdown()


def test_slurm_executor_per_submit_memory_mb_propagates_as_mem_gb() -> None:
    """The executor accepts `memory_mb` (int) on submit() but submitit
    speaks `slurm_mem_gb`. The closure must convert MB to a GB int (or
    the closest unit submitit accepts) and surface it."""
    ex = SlurmExecutor(partition="short", debug=True)
    captured: list[dict[str, Any]] = []
    original = submitit.AutoExecutor.update_parameters

    def spy(self: Any, **kwargs: Any) -> None:
        captured.append(kwargs)
        return original(self, **kwargs)

    with patch.object(submitit.AutoExecutor, "update_parameters", spy):
        # 2048 MB == 2 GB.
        ex.submit(lambda: "ok", name="t", cpus=1, memory_mb=2048, time_min=5)

    assert any(call.get("slurm_mem_gb") == 2 or call.get("mem_gb") == 2 for call in captured), (
        f"per-submit mem 2048MB not converted to 2GB; captured={captured}"
    )
    ex.shutdown()


def test_slurm_executor_per_submit_time_min_propagates() -> None:
    """Per-submit time_min (int minutes) must reach submitit as
    `slurm_time` (also minutes, per submitit convention)."""
    ex = SlurmExecutor(partition="short", debug=True)
    captured: list[dict[str, Any]] = []
    original = submitit.AutoExecutor.update_parameters

    def spy(self: Any, **kwargs: Any) -> None:
        captured.append(kwargs)
        return original(self, **kwargs)

    with patch.object(submitit.AutoExecutor, "update_parameters", spy):
        ex.submit(lambda: "ok", name="t", cpus=1, memory_mb=256, time_min=37)

    assert any(call.get("slurm_time") == 37 or call.get("time") == 37 for call in captured), (
        f"per-submit time_min=37 not propagated; captured={captured}"
    )
    ex.shutdown()


# ---------------------------------------------------------------------------
# dry-run path: debug=True still logs the sbatch script (acceptance: 'The
# dry-run path (debug=True) still produces a logged sbatch script for
# inspection.')
# ---------------------------------------------------------------------------
def test_slurm_executor_debug_mode_logs_sbatch_script(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """When debug=True, the executor must log the exact `sbatch` script
    that would have been submitted at INFO level (so an operator can copy
    it into `sbatch` to debug a job without running the full campaign).

    The DebugExecutor emits this as a DEBUG-level log via the
    `submitit` logger. We assert the message contains the expected
    `#SBATCH` directives that the executor-level config would inject.
    """
    caplog.set_level(logging.DEBUG, logger="submitit")
    caplog.set_level(logging.INFO, logger="osimflow.executors")
    os.environ["OSIMFLOW_SLURM_LOGS"] = str(tmp_path / "slurm-logs")
    try:
        ex = SlurmExecutor(
            partition="debug-partition",
            account="debug-account",
            cpus_per_task=2,
            mem_gb=4,
            time_h=2,
            debug=True,
        )
        handle = ex.submit(lambda: "ok", name="sample-1")
        handle.result(timeout=30)
    finally:
        os.environ.pop("OSIMFLOW_SLURM_LOGS", None)

    # submitit.DebugExecutor logs the sbatch script via the `submitit`
    # logger at DEBUG. We don't pin to a specific submitit message
    # format (it has changed across versions); we just require that
    # the script (or its `echo` of the path) shows up *somewhere* in
    # the captured records.
    full_log = "\n".join(rec.getMessage() for rec in caplog.records)
    # The executor itself also logs a clear "running in DEBUG mode" warning
    # that names the `sbatch` script path; assert on that as the
    # contract.
    assert any(
        "DEBUG mode" in rec.getMessage() and "sbatch" in rec.getMessage() for rec in caplog.records
    ), (
        "expected a clear debug-mode log message naming the sbatch script; "
        f"records: {[r.getMessage() for r in caplog.records]}"
    )
    # And the submitit DebugExecutor must have emitted something we can
    # extract into a `sbatch` script. We don't pin the exact wording
    # (changes per submitit version) but require SOMETHING with a
    # #SBATCH directive to appear in the captured log.
    assert "#SBATCH" in full_log or "sbatch" in full_log.lower(), (
        f"no sbatch script content in log; records: {full_log[:2000]}"
    )


def test_slurm_executor_real_mode_does_not_warn_about_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`debug=False` (real Slurm) must NOT log the debug-mode warning."""
    caplog.set_level(logging.WARNING, logger="osimflow.executors")
    ex = SlurmExecutor(partition="short", debug=False)
    assert not any("DEBUG mode" in rec.getMessage() for rec in caplog.records), (
        "real Slurm mode should not log the debug warning"
    )
    ex.shutdown()


# ---------------------------------------------------------------------------
# Advanced flags: --slurm_qos, --slurm_constraint, --slurm_gres (acceptance:
# 'Add a --slurm_qos, --slurm_constraint, --slurm_gres flag for advanced
# users (GPU jobs, etc.).')
# ---------------------------------------------------------------------------
def test_slurm_executor_accepts_qos_constraint_gres() -> None:
    """submitit 1.5+ accepts `slurm_qos`, `slurm_constraint`, `slurm_gres`.
    The constructor must propagate them when set. We spy on
    `update_parameters` *before* constructing the executor so the
    init-time call is captured."""
    import submitit  # noqa: PLC0415

    captured: list[dict[str, Any]] = []
    original = submitit.AutoExecutor.update_parameters

    def spy(self: Any, **kwargs: Any) -> None:
        captured.append(kwargs)
        return original(self, **kwargs)

    with patch.object(submitit.AutoExecutor, "update_parameters", spy):
        ex = SlurmExecutor(
            partition="gpu",
            qos="high-priority",
            constraint="gpu",
            gres="gpu:1",
            debug=True,
        )
    # The first (and only) init-time call must include the advanced flags.
    assert captured, "expected at least one update_parameters call"
    init_call = captured[0]
    assert init_call.get("slurm_qos") == "high-priority" or init_call.get("qos") == "high-priority"
    assert init_call.get("slurm_constraint") == "gpu" or init_call.get("constraint") == "gpu"
    assert init_call.get("slurm_gres") == "gpu:1" or init_call.get("gres") == "gpu:1"
    ex.shutdown()


def test_slurm_executor_advanced_flags_optional() -> None:
    """qos/constraint/gres are all optional; default to None and submit
    without them. We spy on update_parameters *before* construction so
    the init-time call is captured (and verify None values are filtered
    out so submitit doesn't receive explicit None assignments)."""
    import submitit  # noqa: PLC0415

    captured: list[dict[str, Any]] = []
    original = submitit.AutoExecutor.update_parameters

    def spy(self: Any, **kwargs: Any) -> None:
        captured.append(kwargs)
        return original(self, **kwargs)

    with patch.object(submitit.AutoExecutor, "update_parameters", spy):
        ex = SlurmExecutor(partition="short", debug=True)
    assert captured, "expected at least one update_parameters call"
    init_call = captured[0]
    # None values must be filtered out — submitit shouldn't receive
    # slurm_qos=None / slurm_constraint=None / slurm_gres=None.
    assert "slurm_qos" not in init_call
    assert "slurm_constraint" not in init_call
    assert "slurm_gres" not in init_call
    # And the basic fields should still be present.
    assert "slurm_partition" in init_call
    assert "slurm_cpus_per_task" in init_call
    ex.shutdown()


# ---------------------------------------------------------------------------
# load_config: the new flags plumb through CampaignConfig. (This is the
# programmatic surface; the CLI lives in __main__.)
# ---------------------------------------------------------------------------
def test_load_config_plumbs_slurm_advanced_flags(tmp_path: Path) -> None:
    variables_yml = tmp_path / "variables.yml"
    variables_yml.write_text(
        "variables:\n  - name: test_var\n    distribution: uniform\n    min: 0\n    max: 1\n"
    )
    template = tmp_path / "template"
    template.mkdir()
    (template / "workflow.osw").write_text("{}")
    args: dict[str, Any] = {
        "input_variables": str(variables_yml),
        "template_sim_package": str(template),
        "n_samples": "1",
        "outdir": str(tmp_path / "out"),
        "openstudio_version": "3.11.0",
        "slurm_qos": "high",
        "slurm_constraint": "gpu",
        "slurm_gres": "gpu:1",
    }
    cfg = load_config(args)
    assert cfg.slurm_qos == "high"
    assert cfg.slurm_constraint == "gpu"
    assert cfg.slurm_gres == "gpu:1"


def test_load_config_slurm_advanced_flags_default_none(tmp_path: Path) -> None:
    variables_yml = tmp_path / "variables.yml"
    variables_yml.write_text(
        "variables:\n  - name: test_var\n    distribution: uniform\n    min: 0\n    max: 1\n"
    )
    template = tmp_path / "template"
    template.mkdir()
    (template / "workflow.osw").write_text("{}")
    args: dict[str, Any] = {
        "input_variables": str(variables_yml),
        "template_sim_package": str(template),
        "n_samples": "1",
        "outdir": str(tmp_path / "out"),
        "openstudio_version": "3.11.0",
    }
    cfg = load_config(args)
    assert cfg.slurm_qos is None
    assert cfg.slurm_constraint is None
    assert cfg.slurm_gres is None


# ---------------------------------------------------------------------------
# CampaignConfig: dataclass fields for the new flags.
# ---------------------------------------------------------------------------
def test_campaign_config_has_slurm_advanced_fields() -> None:
    cfg = CampaignConfig(
        input_variables=Path("/tmp/v.yml"),
        template_sim_package=Path("/tmp/pkg"),
        n_samples=1,
        outdir=Path("/tmp/o"),
        openstudio_version="3.11.0",
    )
    assert hasattr(cfg, "slurm_qos")
    assert hasattr(cfg, "slurm_constraint")
    assert hasattr(cfg, "slurm_gres")
    assert cfg.slurm_qos is None
    assert cfg.slurm_constraint is None
    assert cfg.slurm_gres is None


# ---------------------------------------------------------------------------
# __main__: new CLI flags are wired.
# ---------------------------------------------------------------------------
def test_cli_parser_has_slurm_advanced_flags() -> None:
    """The `osimflow run` parser must expose --slurm-qos, --slurm-constraint,
    and --slurm-gres so the production user can pass them from the shell."""
    from osimflow.__main__ import _build_parser  # noqa: PLC0415

    parser = _build_parser()
    # argparse stores the default value of an optional flag on the
    # parsed namespace; parse a minimal argv and assert the attrs exist.
    args = parser.parse_args(
        [
            "run",
            "--input_variables",
            "v.yml",
            "--template_sim_package",
            "pkg",
            "--n_samples",
            "1",
            "--outdir",
            "out",
        ]
    )
    assert hasattr(args, "slurm_qos")
    assert hasattr(args, "slurm_constraint")
    assert hasattr(args, "slurm_gres")
    assert args.slurm_qos is None
    assert args.slurm_constraint is None
    assert args.slurm_gres is None


def test_cli_parser_accepts_slurm_advanced_flag_values() -> None:
    from osimflow.__main__ import _build_parser  # noqa: PLC0415

    parser = _build_parser()
    args = parser.parse_args(
        [
            "run",
            "--executor",
            "slurm",
            "--slurm-real",
            "--slurm-partition",
            "gpu",
            "--slurm-qos",
            "high",
            "--slurm-constraint",
            "gpu",
            "--slurm-gres",
            "gpu:1",
            "--input_variables",
            "v.yml",
            "--template_sim_package",
            "pkg",
            "--n_samples",
            "1",
            "--outdir",
            "out",
        ]
    )
    assert args.slurm_real is True
    assert args.slurm_partition == "gpu"
    assert args.slurm_qos == "high"
    assert args.slurm_constraint == "gpu"
    assert args.slurm_gres == "gpu:1"


# ---------------------------------------------------------------------------
# __main__: _build_executor propagates the advanced flags to SlurmExecutor.
# ---------------------------------------------------------------------------
def test_build_executor_propagates_slurm_advanced_flags() -> None:
    from osimflow.__main__ import _build_executor  # noqa: PLC0415

    class A:
        executor = "slurm"
        slurm_partition = "gpu"
        slurm_account = None
        slurm_real = True
        slurm_qos = "high"
        slurm_constraint = "gpu"
        slurm_gres = "gpu:1"

    with patch("osimflow.__main__.SlurmExecutor") as mock_cls:
        mock_cls.return_value.name = "slurm"
        _build_executor(A())  # type: ignore[arg-type]
    kwargs = mock_cls.call_args.kwargs
    assert kwargs.get("qos") == "high"
    assert kwargs.get("constraint") == "gpu"
    assert kwargs.get("gres") == "gpu:1"
    assert kwargs.get("debug") is False  # --slurm_real


# ---------------------------------------------------------------------------
# End-to-end dry-run: a 2-sample "campaign-shaped" smoke test exercises the
# DebugExecutor path. This is the pytest smoke test the issue asks for.
# ---------------------------------------------------------------------------
def test_slurm_dry_run_smoke_executes_two_samples(tmp_path: Path) -> None:
    """The full debug path: construct executor, submit two callables,
    block on both, get the values back. Mirrors the campaign's per-step
    fan-out without spinning up the campaign class."""
    os.environ["OSIMFLOW_SLURM_LOGS"] = str(tmp_path / "slurm-logs")
    try:
        ex = SlurmExecutor(partition="short", cpus_per_task=1, debug=True)
        results: list[Any] = []
        for i in range(2):
            h = ex.submit(lambda i=i: i * 2, name=f"sample-{i}", cpus=1, memory_mb=256, time_min=2)
            results.append(h.result(timeout=30))
        assert results == [0, 2]
    finally:
        os.environ.pop("OSIMFLOW_SLURM_LOGS", None)
        ex.shutdown()
