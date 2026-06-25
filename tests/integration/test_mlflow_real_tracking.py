"""Real MLflow file-tracking-URI smoke test (issue #948).

Unlike the unit tests in ``tests/unit/test_mlflow_hook.py`` (which inject a
fake ``mlflow`` module into ``sys.modules``), this test exercises the real
``mlflow`` package against a hermetic ``file://`` tracking store — **no server
required**. It catches API regressions (e.g. ``log_metric`` rejecting a
``numpy.float64``, a renamed kwarg, or a changed ``MlflowClient`` contract)
that the mock-based tests cannot see.

Gate
----
The test is skipped unless **both**:

  1. ``OSIMFLOW_MLFLOW_E2E=1`` is set, and
  2. the real ``mlflow`` package is importable (checked via
     ``importlib.util.find_spec``, NOT a ``sys.modules`` fake).

Because the store is a local ``file://`` directory and the campaign uses
stub simulation (``openstudio.cli`` is not required), the test is fast and
hermetic — it can be a *genuinely-passing* smoke test rather than inert
scaffolding. Install the optional extra and run locally::

    pip install -e ".[mlflow]"
    OSIMFLOW_MLFLOW_E2E=1 .venv/bin/pytest tests/integration/test_mlflow_real_tracking.py -v --timeout=300

The ``mlflow-real`` job in ``.github/workflows/ci.yml`` exercises this path
on every PR.

.. note:: **MLflow 3.x file-store deprecation.** Starting with MLflow 3.x the
    filesystem (``file://``) tracking backend is in maintenance mode and raises
    ``MlflowException`` by default unless ``MLFLOW_ALLOW_FILE_STORE=true`` is
    set. This test sets that opt-in env var via ``monkeypatch`` so the hermetic
    serverless ``file://`` store (the substrate the issue explicitly requests)
    remains usable. This is precisely the kind of version-skew the mock-based
    unit tests cannot catch — if MLflow ever removes the file store entirely,
    this smoke test will fail loudly and the docs/CI path can be migrated.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"


def _mlflow_importable() -> bool:
    """Return True iff the *real* ``mlflow`` package is importable.

    Uses ``importlib.util.find_spec`` (not a ``sys.modules`` lookup) so a
    fake module injected by another test cannot satisfy the gate.
    """
    return importlib.util.find_spec("mlflow") is not None


pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_MLFLOW_E2E") != "1" or not _mlflow_importable(),
    reason=(
        "Set OSIMFLOW_MLFLOW_E2E=1 and install the [mlflow] extra "
        "(pip install osimflow[mlflow]) to run the real MLflow file-tracking "
        "smoke test"
    ),
)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: wwr\n"
        "    distribution: uniform\n"
        "    min: 0.2\n"
        "    max: 0.6\n"
        "    measure_argument: SetEnvelopePerformance.wwr\n"
    )
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
    return pkg


def test_real_mlflow_file_tracking_logs_run_params_metrics_artifacts(
    workdir: Path, template_pkg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run a 2-sample stub campaign with a ``file://`` MLflow tracking URI.

    Validates, via the *real* ``mlflow.tracking.MlflowClient``, that the
    ``osimflow.mlflow_hook`` helpers actually land data in a real MLflow
    tracking store:

      (a) an experiment + run was created,
      (b) the four campaign params were logged (executor, openstudio_version,
          n_samples, archive_intermediates),
      (c) the three summary metrics were logged (elapsed_s, n_succeeded,
          n_failed),
      (d) the run's ``artifact_uri`` points into the file store and contains
          at least one artifact file.

    The active MLflow run is ended in a ``finally`` so a campaign failure
    cannot leak an open run into subsequent tests.
    """
    import mlflow
    from mlflow.tracking import MlflowClient

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import LocalExecutor
    from osimflow.mlflow_hook import maybe_end_mlflow_run

    # MLflow 3.x: the file:// backend is opt-in (maintenance mode). Set the
    # documented opt-in env var so the hermetic serverless store is usable.
    # See the module docstring note on the file-store deprecation.
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")

    # Hermetic file:// tracking store — no server, no network.
    mlruns_dir = tmp_path / "mlruns"
    mlruns_dir.mkdir()
    tracking_uri = f"file://{mlruns_dir}"

    outdir = tmp_path / "out"
    outdir.mkdir()

    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        skip_preflight=True,
        mlflow_tracking_uri=tracking_uri,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))

    try:
        campaign.run()
    finally:
        # Defensive: the Campaign wraps run() in try/finally and calls
        # maybe_end_mlflow_run() itself, but we belt-and-braces here so a
        # hook regression cannot leak an open run into other tests.
        maybe_end_mlflow_run()
        mlflow.end_run()

    # ------------------------------------------------------------------
    # Read back via the REAL mlflow client (acceptance criteria #948).
    # ------------------------------------------------------------------
    client = MlflowClient(tracking_uri=tracking_uri)

    # (a) An experiment exists and contains at least one run.
    experiments = client.search_experiments()
    assert experiments, "no MLflow experiments were created"
    all_runs = []
    for exp in experiments:
        all_runs.extend(client.search_runs(experiment_ids=[exp.experiment_id]))
    assert all_runs, "no MLflow runs were created in the file store"

    # Pick the run whose run_name matches the campaign id. mlflow stores
    # run_name inside the run's tags ("mlflow.runName").
    trace_campaign_id = campaign.trace.campaign_id
    matching = [r for r in all_runs if r.data.tags.get("mlflow.runName") == trace_campaign_id]
    assert matching, (
        f"no MLflow run with run_name={trace_campaign_id!r}; "
        f"found run names: "
        f"{[r.data.tags.get('mlflow.runName') for r in all_runs]}"
    )
    run = matching[0]

    # (b) All four campaign params were logged.
    params = run.data.params
    for key, expected in (
        ("executor", "local"),
        ("openstudio_version", "3.11.0"),
        ("n_samples", "2"),
        ("archive_intermediates", "False"),
    ):
        assert key in params, f"missing MLflow param {key!r}; got {params}"
        assert params[key] == expected, (
            f"MLflow param {key!r} = {params[key]!r}, expected {expected!r}"
        )

    # (c) All three summary metrics were logged.
    metrics = run.data.metrics
    for key in ("elapsed_s", "n_succeeded", "n_failed"):
        assert key in metrics, f"missing MLflow metric {key!r}; got {metrics}"
    assert metrics["n_succeeded"] == 2, metrics
    assert metrics["n_failed"] == 0, metrics

    # (d) artifact_uri points into the file store and holds an artifact.
    artifact_uri = run.info.artifact_uri
    assert artifact_uri.startswith("file://"), f"artifact_uri not a file:// URI: {artifact_uri!r}"
    artifact_root = Path(artifact_uri[len("file://") :])
    assert mlruns_dir in artifact_root.parents, (
        f"artifact_uri {artifact_root} not under the file store {mlruns_dir}"
    )
    listed = client.list_artifacts(run.info.run_id)
    assert listed, f"no artifacts listed for run {run.info.run_id}"
    artifact_names = {a.path for a in listed}
    # The hook logs aggregated_results.csv, failed_simulations.csv, run.json.
    assert "aggregated_results.csv" in artifact_names, artifact_names
    assert "run.json" in artifact_names, artifact_names
