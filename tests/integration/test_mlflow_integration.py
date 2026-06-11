"""Unit tests for the optional MLflow integration (issue #7).

These tests pin the contract documented in issue #7:

  1. `--mlflow_tracking_uri` CLI flag and `CampaignConfig.mlflow_tracking_uri`
     field both exist and round-trip via `load_config`.
  2. When no tracking URI is set, no `mlflow` import happens (lazy).
  3. When a tracking URI is set, `mlflow.set_tracking_uri`,
     `mlflow.start_run`, `mlflow.end_run` are called with the right args,
     and params / metrics / artifacts are logged in the right order.
  4. `mlflow.end_run()` runs even if the campaign raises (cleanup on
     exception), so the MLflow UI never shows a stuck "RUNNING" entry.

The tests mock `mlflow` via `sys.modules` so the real package is never
imported in CI. This is the same lazy-import pattern the implementation
uses, mirrored in the test to keep the dependency surface zero.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

from osimflow import Campaign, CampaignConfig, load_config
from osimflow.campaign import (
    log_mlflow_artifacts,
    log_mlflow_metrics,
    log_mlflow_params,
    maybe_end_mlflow_run,
    maybe_start_mlflow_run,
)
from osimflow.executors import LocalExecutor


# ---------------------------------------------------------------------------
# Test helpers — fake `mlflow` module injection
# ---------------------------------------------------------------------------
class _FakeMlflowRecorder:
    """Records every mlflow.* call so tests can assert ordering and args."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.active_run = True
        self.tracking_uri: str | None = None
        self.run_name: str | None = None

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def set_tracking_uri(self, uri: str) -> None:
        self._record("set_tracking_uri", uri)
        self.tracking_uri = uri

    def start_run(self, run_name: str | None = None, **kwargs: Any) -> SimpleNamespace:
        self._record("start_run", run_name=run_name, **kwargs)
        self.run_name = run_name
        return SimpleNamespace(info=SimpleNamespace(run_id="fake-run-id"))

    def end_run(self) -> None:
        self._record("end_run")
        self.active_run = False

    def log_param(self, key: str, value: Any) -> None:
        self._record("log_param", key, value)

    def log_params(self, params: dict[str, Any]) -> None:
        self._record("log_params", dict(params))

    def log_metric(self, key: str, value: float, step: int | None = None) -> None:
        self._record("log_metric", key, value, step=step)

    def log_metrics(self, metrics: dict[str, float], step: int | None = None) -> None:
        self._record("log_metrics", dict(metrics), step=step)

    def log_artifact(self, path: str) -> None:
        self._record("log_artifact", path)


@pytest.fixture
def fake_mlflow(monkeypatch: pytest.MonkeyPatch) -> _FakeMlflowRecorder:
    """Inject a fake `mlflow` module into sys.modules for the duration of
    one test. Returns the recorder so the test can assert on call order.
    """
    recorder = _FakeMlflowRecorder()
    fake = ModuleType("mlflow")
    fake.set_tracking_uri = recorder.set_tracking_uri  # type: ignore[attr-defined]
    fake.start_run = recorder.start_run  # type: ignore[attr-defined]
    fake.end_run = recorder.end_run  # type: ignore[attr-defined]
    fake.log_param = recorder.log_param  # type: ignore[attr-defined]
    fake.log_params = recorder.log_params  # type: ignore[attr-defined]
    fake.log_metric = recorder.log_metric  # type: ignore[attr-defined]
    fake.log_metrics = recorder.log_metrics  # type: ignore[attr-defined]
    fake.log_artifact = recorder.log_artifact  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    return recorder


# ---------------------------------------------------------------------------
# CampaignConfig + load_config — new field
# ---------------------------------------------------------------------------
def test_campaign_config_mlflow_tracking_uri_defaults_to_none() -> None:
    cfg = CampaignConfig(
        input_variables=Path("/tmp/v.yml"),
        template_sim_package=Path("/tmp/pkg"),
        n_samples=1,
        outdir=Path("/tmp/o"),
        openstudio_version="3.11.0",
    )
    assert cfg.mlflow_tracking_uri is None


def test_campaign_config_mlflow_tracking_uri_stores_uri() -> None:
    cfg = CampaignConfig(
        input_variables=Path("/tmp/v.yml"),
        template_sim_package=Path("/tmp/pkg"),
        n_samples=1,
        outdir=Path("/tmp/o"),
        openstudio_version="3.11.0",
        mlflow_tracking_uri="http://localhost:5000",
    )
    assert cfg.mlflow_tracking_uri == "http://localhost:5000"


def test_load_config_passes_mlflow_tracking_uri(tmp_path: Path) -> None:
    variables_yml = tmp_path / "variables.yml"
    variables_yml.write_text("variables: []\n")
    template = tmp_path / "template"
    template.mkdir()
    args: dict[str, Any] = {
        "input_variables": str(variables_yml),
        "template_sim_package": str(template),
        "n_samples": "1",
        "outdir": str(tmp_path / "out"),
        "openstudio_version": "3.11.0",
        "mlflow_tracking_uri": "http://mlflow.local:5000",
    }
    cfg = load_config(args)
    assert cfg.mlflow_tracking_uri == "http://mlflow.local:5000"


# ---------------------------------------------------------------------------
# Lazy import — no mlflow import when no tracking URI
# ---------------------------------------------------------------------------
def test_maybe_start_mlflow_run_is_noop_when_uri_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the tracking URI is None, the helper must not import mlflow.

    We assert by blocking any future import of `mlflow`: if the helper
    touches it, this raises AssertionError, failing the test loudly.
    """

    class _RaisingModule(ModuleType):
        def __getattr__(self, name: str) -> Any:  # type: ignore[override]
            raise AssertionError(
                f"mlflow was touched at runtime (attr={name!r}); "
                "the no-tracking-URI path must remain mlflow-free"
            )

    raising = _RaisingModule("mlflow")
    monkeypatch.setitem(sys.modules, "mlflow", raising)
    # If the URI is None, the helper must do nothing — no touch of mlflow.
    assert maybe_start_mlflow_run(None, "campaign-id") is None


# ---------------------------------------------------------------------------
# Tracking URI set — start_run / set_tracking_uri called
# ---------------------------------------------------------------------------
def test_maybe_start_mlflow_run_calls_set_tracking_uri_and_start_run(
    fake_mlflow: _FakeMlflowRecorder,
) -> None:
    """When the URI is set, the helper sets the URI and starts a run
    named after the campaign_id."""
    result = maybe_start_mlflow_run("http://localhost:5000", "2026-06-09T12-00-00")
    assert result is not None
    names = [c[0] for c in fake_mlflow.calls]
    assert names[0] == "set_tracking_uri"
    assert names[1] == "start_run"
    assert fake_mlflow.tracking_uri == "http://localhost:5000"
    assert fake_mlflow.run_name == "2026-06-09T12-00-00"


# ---------------------------------------------------------------------------
# log_params / log_metrics / log_artifacts
# ---------------------------------------------------------------------------
def test_log_mlflow_params_logs_expected_keys(fake_mlflow: _FakeMlflowRecorder) -> None:
    cfg = SimpleNamespace(
        executor="local",
        openstudio_version="3.11.0",
        n_samples=5,
        archive_intermediates=True,
    )
    log_mlflow_params(cfg)
    params_calls = [c for c in fake_mlflow.calls if c[0] == "log_param"]
    keys = {c[1][0] for c in params_calls}
    assert keys == {"executor", "openstudio_version", "n_samples", "archive_intermediates"}


def test_log_mlflow_metrics_logs_expected_keys(fake_mlflow: _FakeMlflowRecorder) -> None:
    log_mlflow_metrics(elapsed_s=12.5, n_succeeded=3, n_failed=1)
    metric_calls = [c for c in fake_mlflow.calls if c[0] == "log_metric"]
    keys = {c[1][0] for c in metric_calls}
    assert keys == {"elapsed_s", "n_succeeded", "n_failed"}
    # The values are the right shape (float / int).
    values_by_key = {c[1][0]: c[1][1] for c in metric_calls}
    assert values_by_key["elapsed_s"] == 12.5
    assert values_by_key["n_succeeded"] == 3
    assert values_by_key["n_failed"] == 1


def test_log_mlflow_artifacts_calls_log_artifact_per_path(
    fake_mlflow: _FakeMlflowRecorder, tmp_path: Path
) -> None:
    csv = tmp_path / "aggregated_results.csv"
    csv.write_text("sample_id,x\n0,1\n")
    failed = tmp_path / "failed_simulations.csv"
    failed.write_text("sample_id,err\n")
    run_json = tmp_path / "run.json"
    run_json.write_text("{}")
    log_mlflow_artifacts(csv, failed, run_json)
    artifact_calls = [c for c in fake_mlflow.calls if c[0] == "log_artifact"]
    paths = [str(c[1][0]) for c in artifact_calls]
    assert len(artifact_calls) == 3
    assert str(csv) in paths
    assert str(failed) in paths
    assert str(run_json) in paths


# ---------------------------------------------------------------------------
# end_run on cleanup
# ---------------------------------------------------------------------------
def test_maybe_end_mlflow_run_calls_end_run(fake_mlflow: _FakeMlflowRecorder) -> None:
    maybe_start_mlflow_run("http://localhost:5000", "test-campaign")
    maybe_end_mlflow_run()
    assert any(c[0] == "end_run" for c in fake_mlflow.calls)


def test_maybe_end_mlflow_run_is_noop_when_mlflow_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `mlflow` is not in sys.modules (the lazy-import never fired),
    the end helper must not import it just to call end_run."""
    monkeypatch.delitem(sys.modules, "mlflow", raising=False)
    # If the helper imports mlflow, this raises ModuleNotFoundError; the
    # test will surface that. The helper should be a no-op.
    assert maybe_end_mlflow_run() is None


# ---------------------------------------------------------------------------
# Campaign integration — end-to-end with mlflow active
# ---------------------------------------------------------------------------
def _make_workdir_with_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Return (variables_yml, template_pkg) wired for a 2-sample stub run."""
    variables_yml = tmp_path / "variables.yml"
    variables_yml.write_text(
        yaml.safe_dump(
            {
                "variables": [
                    {"name": "u1", "distribution": "uniform", "min": 0.0, "max": 1.0},
                ]
            }
        )
    )
    template = tmp_path / "template"
    template.mkdir()
    (template / "model.osm").write_text(json.dumps({"attributes": {"u1": 0.0}}))
    (template / "workflow.osw").write_text(json.dumps({"name": "stub"}))
    return variables_yml, template


def test_campaign_run_logs_to_mlflow_when_tracking_uri_set(
    fake_mlflow: _FakeMlflowRecorder, tmp_path: Path
) -> None:
    variables_yml, template = _make_workdir_with_fixture(tmp_path)
    outdir = tmp_path / "out"
    outdir.mkdir()
    cfg = CampaignConfig(
        input_variables=variables_yml,
        template_sim_package=template,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        mlflow_tracking_uri="http://localhost:5000",
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    campaign.run()

    names = [c[0] for c in fake_mlflow.calls]
    # The lifecycle: set_tracking_uri, start_run, ... params, metrics, artifacts, end_run.
    assert "set_tracking_uri" in names
    assert "start_run" in names
    assert "log_param" in names
    assert "log_metric" in names
    assert "log_artifact" in names
    assert "end_run" in names
    # start_run came before end_run.
    assert names.index("start_run") < names.index("end_run")
    # Artifacts reference the real output files.
    artifact_paths = [str(c[1][0]) for c in fake_mlflow.calls if c[0] == "log_artifact"]
    assert any(p.endswith("aggregated_results.csv") for p in artifact_paths)
    assert any(p.endswith("failed_simulations.csv") for p in artifact_paths)
    assert any(p.endswith("run.json") for p in artifact_paths)


def test_campaign_run_does_not_touch_mlflow_when_no_tracking_uri(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without --mlflow_tracking_uri, the campaign must run unchanged and
    the mlflow module must never be imported (lazy import invariant)."""

    class _RaisingModule(ModuleType):
        def __getattr__(self, name: str) -> Any:  # type: ignore[override]
            raise AssertionError(f"mlflow was touched (attr={name!r}) on the no-tracking-URI path")

    monkeypatch.setitem(sys.modules, "mlflow", _RaisingModule("mlflow"))

    variables_yml, template = _make_workdir_with_fixture(tmp_path)
    outdir = tmp_path / "out"
    outdir.mkdir()
    cfg = CampaignConfig(
        input_variables=variables_yml,
        template_sim_package=template,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        mlflow_tracking_uri=None,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    # If this raises AssertionError, mlflow was touched. If it succeeds
    # cleanly, the no-tracking-URI path is mlflow-free.
    result = campaign.run()
    assert result["samples"]  # type: ignore[index]


def test_mlflow_end_run_called_even_when_step_raises(
    fake_mlflow: _FakeMlflowRecorder, tmp_path: Path
) -> None:
    """If the campaign raises mid-run, end_run must still fire so the
    MLflow UI never shows a stuck RUNNING entry."""
    variables_yml, template = _make_workdir_with_fixture(tmp_path)
    outdir = tmp_path / "out"
    outdir.mkdir()
    cfg = CampaignConfig(
        input_variables=variables_yml,
        template_sim_package=template,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        mlflow_tracking_uri="http://localhost:5000",
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))

    # Patch one of the steps to raise mid-run. Campaign.run() must
    # propagate the exception AND still call end_run (via try/finally).
    # The algorithm framework calls step_generate_samples, not
    # step_generate_lhs, so patch the new method.
    def _boom(*_: object, **__: object) -> None:
        raise RuntimeError("simulated LHS failure")

    monkeypatch_ = pytest.MonkeyPatch()
    monkeypatch_.setattr(campaign, "step_generate_samples", _boom)
    try:
        with pytest.raises(RuntimeError, match="simulated LHS failure"):
            campaign.run()
    finally:
        monkeypatch_.undo()

    assert any(c[0] == "end_run" for c in fake_mlflow.calls)


# ---------------------------------------------------------------------------
# CLI flag
# ---------------------------------------------------------------------------
def test_cli_accepts_mlflow_tracking_uri_flag() -> None:
    """The argparse parser must accept --mlflow_tracking_uri and the value
    must flow into the loaded config."""
    from osimflow.__main__ import _build_parser

    parser = _build_parser()
    argv = [
        "run",
        "--input_variables",
        "v.yml",
        "--template_sim_package",
        "./pkg",
        "--n_samples",
        "1",
        "--outdir",
        "./out",
        "--mlflow_tracking_uri",
        "http://mlflow.example:5000",
    ]
    args = parser.parse_args(argv)
    assert args.mlflow_tracking_uri == "http://mlflow.example:5000"


def test_cli_mlflow_tracking_uri_default_is_none() -> None:
    from osimflow.__main__ import _build_parser

    parser = _build_parser()
    argv = [
        "run",
        "--input_variables",
        "v.yml",
        "--template_sim_package",
        "./pkg",
        "--n_samples",
        "1",
        "--outdir",
        "./out",
    ]
    args = parser.parse_args(argv)
    assert args.mlflow_tracking_uri is None
