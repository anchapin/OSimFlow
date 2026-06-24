"""Optional MLflow integration for OSimFlow (issue #7).

Mirrors the lazy-import pattern used for `submitit` and `boto3` so users
who do not pass `--mlflow_tracking_uri` pay zero import cost. The five
public helpers are the only surface the Campaign should call:

  - `maybe_start_mlflow_run(uri, campaign_id)` -> str | None
  - `maybe_end_mlflow_run()` -> None
  - `log_mlflow_params(cfg)` -> None
  - `log_mlflow_metrics(elapsed_s, n_succeeded, n_failed)` -> None
  - `log_mlflow_artifacts(csv, failed, run_json)` -> None

The contract:

  * If `uri` is falsy, `maybe_start_mlflow_run` is a no-op (no `mlflow`
    import, no module side effects). The end / log helpers short-circuit
    when no run is active.

  * If `uri` is set, the helpers import `mlflow` lazily. The first
    `set_tracking_uri` / `start_run` happens inside `maybe_start_mlflow_run`;
    subsequent calls reuse the same `mlflow` import via the module
    attribute lookup.

  * The `mlflow` package is intentionally not declared as a runtime
    dependency. Users who want it `pip install osimflow[mlflow]`. The
    import path degrades to a logged warning if the package is missing
    rather than crashing the campaign.

The Campaign wraps its `run()` body in a `try/finally` so the cleanup
helper is always called, even if a step raises.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.mlflow")

# Module-level state: the active MLflow run (truthy when a run is open).
# Kept private to the module so the public helpers are the only API.
_ACTIVE_URI: str | None = None
_ACTIVE_RUN_NAME: str | None = None


def _import_mlflow() -> Any | None:
    """Lazy import of the `mlflow` package.

    Returns the module if importable, otherwise None. The caller is
    expected to short-circuit when the module is unavailable so the
    rest of the campaign runs unchanged.
    """
    try:
        import mlflow  # noqa: PLC0415
    except ImportError:
        log.warning(
            "mlflow_tracking_uri is set but the `mlflow` package is not "
            "installed. Install with `pip install osimflow[mlflow]` to enable "
            "MLflow logging. Continuing without MLflow tracking."
        )
        return None
    return mlflow


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def maybe_start_mlflow_run(tracking_uri: str | None, campaign_id: str) -> str | None:
    """Begin an MLflow run if a tracking URI is configured.

    Returns the run name on success, None when no tracking is configured
    or the `mlflow` package is unavailable. The return value is advisory
    (callers can use it for logging) — the module-level state is the
    authoritative source for "is a run active".
    """
    global _ACTIVE_URI, _ACTIVE_RUN_NAME  # noqa: PLW0603
    if not tracking_uri:
        # Lazy invariant: do NOT touch the `mlflow` module when the URI
        # is absent. The no-tracking-URI path is mlflow-free.
        return None
    mlflow = _import_mlflow()
    if mlflow is None:
        return None
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.start_run(run_name=campaign_id)
    _ACTIVE_URI = tracking_uri
    _ACTIVE_RUN_NAME = campaign_id
    log.info("MLflow run started: %s @ %s", campaign_id, tracking_uri)
    return campaign_id


def maybe_end_mlflow_run() -> None:
    """Close the active MLflow run, if any.

    Safe to call when no run is active (no-op). Safe to call from a
    `finally` block — does not re-raise even if `mlflow.end_run()` itself
    errors out, so a transient MLflow connectivity issue cannot mask the
    original campaign failure.
    """
    global _ACTIVE_URI, _ACTIVE_RUN_NAME  # noqa: PLW0603
    if _ACTIVE_URI is None:
        return
    try:
        mlflow = _import_mlflow()
        if mlflow is not None:
            mlflow.end_run()
    except Exception:  # noqa: BLE001
        # Cleanup must not mask the original exception. Log and move on.
        log.exception("MLflow end_run failed; suppressing to preserve original error")
    _ACTIVE_URI = None
    _ACTIVE_RUN_NAME = None


def log_mlflow_params(cfg: Any) -> None:
    """Log the static config knobs to the active MLflow run.

    `cfg` is duck-typed (CampaignConfig-shaped) so this helper does not
    create a circular import on `osimflow.config`. The accepted keys
    are: `executor`, `openstudio_version`, `n_samples`,
    `archive_intermediates`. Missing keys are logged as None.
    """
    if _ACTIVE_URI is None:
        return
    mlflow = _import_mlflow()
    if mlflow is None:
        return
    params = {
        "executor": getattr(cfg, "executor", None),
        "openstudio_version": getattr(cfg, "openstudio_version", None),
        "n_samples": getattr(cfg, "n_samples", None),
        "archive_intermediates": getattr(cfg, "archive_intermediates", None),
    }
    # log_param keeps the per-key type contract (str/int/bool).
    for k, v in params.items():
        mlflow.log_param(k, v)


def log_mlflow_metrics(elapsed_s: float, n_succeeded: int, n_failed: int) -> None:
    """Log the campaign-level summary metrics to the active MLflow run.

    `elapsed_s` is a float, `n_succeeded` and `n_failed` are integers.
    """
    if _ACTIVE_URI is None:
        return
    mlflow = _import_mlflow()
    if mlflow is None:
        return
    mlflow.log_metric("elapsed_s", float(elapsed_s))
    mlflow.log_metric("n_succeeded", int(n_succeeded))
    mlflow.log_metric("n_failed", int(n_failed))


def log_mlflow_artifacts(aggregated_csv: Path, failed_csv: Path, run_json: Path) -> None:
    """Log the three primary output artifacts to the active MLflow run.

    `mlflow.log_artifact` uploads the file (or its directory contents)
    to the tracking server, so users can browse the artifacts from the
    MLflow UI. Missing files are logged as a warning and skipped —
    logging should never fail the campaign.
    """
    if _ACTIVE_URI is None:
        return
    mlflow = _import_mlflow()
    if mlflow is None:
        return
    for path in (aggregated_csv, failed_csv, run_json):
        if path.is_file():
            mlflow.log_artifact(str(path))
        else:
            log.warning("MLflow artifact missing, skipping: %s", path)
