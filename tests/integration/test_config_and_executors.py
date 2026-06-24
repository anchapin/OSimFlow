"""Unit tests for the osimflow.config and osimflow.executors modules.

Covers behavior at the public API surface: config validation errors,
executor lifecycle (handle.result, shutdown), and the
`handle.done()` ergonomics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from osimflow.config import CampaignConfig, load_config
from osimflow.executors import AWSBatchExecutor, LocalExecutor, NomadExecutor, SlurmExecutor


# ---------------------------------------------------------------------------
# CampaignConfig
# ---------------------------------------------------------------------------
def test_work_dir_is_outdir_work() -> None:
    cfg = CampaignConfig(
        input_variables=Path("/tmp/v.yml"),
        template_sim_package=Path("/tmp/pkg"),
        n_samples=1,
        outdir=Path("/tmp/o"),
        openstudio_version="3.11.0",
    )
    assert cfg.work_dir == Path("/tmp/o/work")


def test_samples_file_and_cache_db_paths() -> None:
    cfg = CampaignConfig(
        input_variables=Path("/tmp/v.yml"),
        template_sim_package=Path("/tmp/pkg"),
        n_samples=1,
        outdir=Path("/tmp/o"),
        openstudio_version="3.11.0",
    )
    assert cfg.samples_file == Path("/tmp/o/work/samples.json")
    assert cfg.cache_db == Path("/tmp/o/work/cache.sqlite")


def test_archive_and_custom_script_defaults() -> None:
    cfg = CampaignConfig(
        input_variables=Path("/tmp/v.yml"),
        template_sim_package=Path("/tmp/pkg"),
        n_samples=1,
        outdir=Path("/tmp/o"),
        openstudio_version="3.11.0",
    )
    assert cfg.archive_intermediates is False
    assert cfg.custom_apply_script is None
    assert cfg.custom_kpi_extractor is None


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------
def test_load_config_happy_path(tmp_path: Path) -> None:
    variables_yml = tmp_path / "variables.yml"
    variables_yml.write_text(
        "variables:\n  - name: test\n    distribution: uniform\n    min: 0\n    max: 1\n"
    )
    template = tmp_path / "template"
    template.mkdir()
    (template / "workflow.osw").write_text("{}")
    args: dict[str, Any] = {
        "input_variables": str(variables_yml),
        "template_sim_package": str(template),
        "n_samples": "5",
        "outdir": str(tmp_path / "out"),
        "openstudio_version": "3.11.0",
        "archive_intermediates": True,
    }
    cfg = load_config(args)
    assert cfg.n_samples == 5
    assert cfg.openstudio_version == "3.11.0"
    assert cfg.archive_intermediates is True
    assert cfg.input_variables == variables_yml.resolve()
    assert cfg.template_sim_package == template.resolve()
    assert cfg.outdir.is_dir()


def test_load_config_raises_on_missing_variables_yml(tmp_path: Path) -> None:
    template = tmp_path / "template"
    template.mkdir()
    args: dict[str, Any] = {
        "input_variables": str(tmp_path / "missing.yml"),
        "template_sim_package": str(template),
        "n_samples": "1",
        "outdir": str(tmp_path / "out"),
        "openstudio_version": "3.11.0",
    }
    with pytest.raises(FileNotFoundError, match="variables_yml not found"):
        load_config(args)


def test_load_config_raises_on_missing_template(tmp_path: Path) -> None:
    variables_yml = tmp_path / "variables.yml"
    variables_yml.write_text(
        "variables:\n  - name: test\n    distribution: uniform\n    min: 0\n    max: 1\n"
    )
    args: dict[str, Any] = {
        "input_variables": str(variables_yml),
        "template_sim_package": str(tmp_path / "no-template"),
        "n_samples": "1",
        "outdir": str(tmp_path / "out"),
        "openstudio_version": "3.11.0",
    }
    with pytest.raises(FileNotFoundError, match="template_sim_package not found"):
        load_config(args)


def test_load_config_resolves_custom_scripts(tmp_path: Path) -> None:
    variables_yml = tmp_path / "variables.yml"
    variables_yml.write_text(
        "variables:\n  - name: test\n    distribution: uniform\n    min: 0\n    max: 1\n"
    )
    template = tmp_path / "template"
    template.mkdir()
    (template / "workflow.osw").write_text("{}")
    apply_script = tmp_path / "apply.py"
    apply_script.write_text("def apply_parameters(*a, **k): pass\n")
    kpi_script = tmp_path / "kpi.py"
    kpi_script.write_text("def extract_kpis(*a, **k): pass\n")
    args: dict[str, Any] = {
        "input_variables": str(variables_yml),
        "template_sim_package": str(template),
        "n_samples": "1",
        "outdir": str(tmp_path / "out"),
        "openstudio_version": "3.11.0",
        "custom_apply_script": str(apply_script),
        "custom_kpi_extractor": str(kpi_script),
    }
    cfg = load_config(args)
    assert cfg.custom_apply_script == apply_script.resolve()
    assert cfg.custom_kpi_extractor == kpi_script.resolve()


# ---------------------------------------------------------------------------
# LocalExecutor
# ---------------------------------------------------------------------------
def test_local_executor_name() -> None:
    assert LocalExecutor().name == "local"


def test_local_executor_submit_and_result() -> None:
    ex = LocalExecutor(max_workers=1)
    handle = ex.submit(lambda: 42)
    assert handle.result(timeout=5) == 42
    assert handle.done() is True
    ex.shutdown()


def test_local_executor_submits_with_kwargs() -> None:
    """The `name` / `cpus` / etc. kwargs are advisory on the local executor."""
    ex = LocalExecutor(max_workers=1)
    handle = ex.submit(lambda a, b: a + b, 2, 3, name="add", cpus=2)
    assert handle.result(timeout=5) == 5
    ex.shutdown()


def test_local_executor_submit_no_duplicate_positional_kwarg() -> None:
    """Regression for #622.

    The campaign submits ``run_openstudio_sim`` with ``openstudio_version`` as
    the 3rd POSITIONAL argument. It must NOT also be passed as a keyword, or
    Python raises ``TypeError: got multiple values for argument
    'openstudio_version'``. This sentinel mirrors ``run_openstudio_sim``'s
    signature (3rd positional = ``openstudio_version``, keyword-only after
    ``*``) to guard the binding contract at the executor level.
    """
    captured: dict[str, Any] = {}

    def _sim_like(
        mod_pkg: Path,
        sample_id: str,
        openstudio_version: str,
        out: Path,
        simulate_work_s: float = 2.0,
        *,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        max_retries: int = 3,
        worker_id: str = "local",
    ) -> Path:
        captured["openstudio_version"] = openstudio_version
        captured["sample_id"] = sample_id
        return out

    ex = LocalExecutor(max_workers=1)
    try:
        handle = ex.submit(
            _sim_like,
            Path("/pkg"),
            "0001",
            "3.11.0",  # 3rd positional -> binds openstudio_version
            Path("/out"),
            name="sim_0001",
            cpus=1,
            memory_mb=512,
            time_min=1,
            container="nrel/openstudio:3.11.0",
            stdout_path=None,
            stderr_path=None,
            max_retries=0,
            worker_id="local",
        )
        assert handle.result(timeout=5) == Path("/out")
        assert captured["openstudio_version"] == "3.11.0"
        assert captured["sample_id"] == "0001"
    finally:
        ex.shutdown()


# ---------------------------------------------------------------------------
# SlurmExecutor (debug mode — no real Slurm)
# ---------------------------------------------------------------------------
def test_slurm_executor_debug_submits_locally() -> None:
    ex = SlurmExecutor(partition="short", debug=True)
    handle = ex.submit(lambda: "ok", name="t", cpus=1, memory_mb=128)
    assert handle.result(timeout=30) == "ok"
    ex.shutdown()


# ---------------------------------------------------------------------------
# AWSBatchExecutor (no real AWS — boto3.client is patched)
# ---------------------------------------------------------------------------
def test_aws_batch_executor_submits() -> None:
    """Smoke test: the executor accepts a boto3.client-patched environment
    and returns a Handle. The real polling behavior is covered in
    `test_awsbatch_boto3_wiring.py`."""
    from unittest.mock import MagicMock, patch

    fake_client = MagicMock()
    fake_client.submit_job.return_value = {"jobId": "stub-job"}
    fake_client.describe_jobs.return_value = {
        "jobs": [{"jobId": "stub-job", "status": "SUCCEEDED", "statusReason": "OK"}]
    }
    with patch("boto3.client", return_value=fake_client):
        ex = AWSBatchExecutor()
        handle = ex.submit(lambda: None, name="t", cpus=1)
        assert handle.result(timeout=5) is None
    ex.shutdown()


# ---------------------------------------------------------------------------
# NomadExecutor (no real Nomad — urllib.request.urlopen is patched)
# ---------------------------------------------------------------------------
def test_nomad_executor_submits() -> None:
    """Smoke test: the executor accepts a urlopen-patched environment
    and returns a Handle. The real polling behavior is covered in
    `test_nomad_http_wiring.py`."""
    import json
    from unittest.mock import MagicMock, patch

    def _mock_response(data: dict[str, Any]) -> MagicMock:
        resp = MagicMock()
        resp.read.return_value = json.dumps(data).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    # The NomadExecutor.submit() -> handle.result() call chain is:
    #   1. submit_job()         -> POST /v1/jobs
    #   2. resolve_allocation() -> GET /v1/evaluation/{eval}/allocations
    #      (returns list with alloc stub → extracts ID)
    #   3. _wait_for_terminal() -> GET /v1/allocation/{alloc_id}
    # Each needs a separate mock response.
    submit_resp = _mock_response({"JobID": "stub-job", "EvalID": "eval-1", "Index": 0})
    eval_allocs_resp = _mock_response(
        [{"ID": "alloc-1", "ClientStatus": "running", "JobID": "stub-job"}]
    )
    terminal_alloc_resp = _mock_response(
        {"ID": "alloc-1", "ClientStatus": "complete", "JobID": "stub-job"}
    )

    with patch(
        "urllib.request.urlopen",
        side_effect=[submit_resp, eval_allocs_resp, terminal_alloc_resp],
    ):
        ex = NomadExecutor()
        handle = ex.submit(lambda: None, name="t", cpus=1)
        assert handle.result(timeout=5) is None
    ex.shutdown()
