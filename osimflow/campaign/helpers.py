"""Small campaign helpers split out of ``campaign.py`` (issue #1890).

Dry-run env scoping, the MLflow param view, the package content digest and
the ``cast_*`` narrowing helpers.
"""

import contextlib
import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

from osimflow.campaign.types import SampleSpec, VariableSpec
from osimflow.config import CampaignConfig


@contextlib.contextmanager
def _scoped_dry_run_env() -> Iterator[None]:
    """Set ``OSIMFLOW_DOCKER_SWARM_DRY_RUN=1`` for the duration of a block.

    The ``DockerSwarmExecutor`` reads this env var in
    ``_is_dev_fallback_enabled()`` to decide whether to fall back to
    ``LocalExecutor`` during ``--dry-run`` campaigns (issue #944).

    Captures and restores the prior value so the var never leaks across
    campaigns in a long-lived process (e.g. ``osimflow serve``) or across
    tests sharing a worker process under ``pytest-xdist`` (issue #976).
    Restore runs even when the wrapped block raises.
    """
    prev = os.environ.get("OSIMFLOW_DOCKER_SWARM_DRY_RUN")
    os.environ["OSIMFLOW_DOCKER_SWARM_DRY_RUN"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("OSIMFLOW_DOCKER_SWARM_DRY_RUN", None)
        else:
            os.environ["OSIMFLOW_DOCKER_SWARM_DRY_RUN"] = prev


# ---------------------------------------------------------------------------
# MLflow param view (issue #7)
def _make_mlflow_param_view(cfg: CampaignConfig, executor_name: str) -> SimpleNamespace:
    """Build a small adapter for `log_mlflow_params`.

    The hook reads attributes via `getattr`, so a `SimpleNamespace` is
    sufficient. The adapter is built from a `CampaignConfig` + the
    executor's `name` so the hook does not import `osimflow.config`
    (avoiding a circular import).
    """
    return SimpleNamespace(
        executor=executor_name,
        openstudio_version=cfg.openstudio_version,
        n_samples=cfg.n_samples,
        archive_intermediates=cfg.archive_intermediates,
    )


# ---------------------------------------------------------------------------
# Cast helpers — narrow YAML/external `object` types to the strict shapes
# the rest of the code relies on. The cast is a single audit point.
# ---------------------------------------------------------------------------
def _package_content_digest(pkg: Path) -> str:
    """SHA-256 over the parameterized ``workflow.osw`` / ``model.osm`` bytes."""
    h = hashlib.sha256()
    for name in ("workflow.osw", "model.osm"):
        f = pkg / name
        if f.is_file():
            h.update(name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def cast_samples(obj: object) -> list[SampleSpec]:
    """Narrow a `samples` JSON value to the canonical SampleSpec list."""
    if not isinstance(obj, list):
        raise TypeError(f"samples must be a list, got {type(obj).__name__}")
    out: list[SampleSpec] = []
    for item in obj:
        if not isinstance(item, dict):
            raise TypeError("sample entry must be a dict")
        sid = item.get("sample_id")
        values = item.get("values")
        if not isinstance(sid, str) or not isinstance(values, dict):
            raise TypeError("sample entry must have str 'sample_id' and dict 'values'")
        spec = SampleSpec(sample_id=sid, values=values)
        # Per-sample overrides (GAP-009 / issue #1869) must survive the round-trip.
        for override in ("seed_model", "weather_file"):
            override_value = item.get(override)
            if isinstance(override_value, str) and override_value:
                spec[override] = override_value
        out.append(spec)
    return out


def cast_variables(obj: object) -> list[VariableSpec]:
    """Narrow the `variables` list from variables.yml.

    Each entry is a dict with at least `name` and `distribution` keys,
    plus distribution-specific numeric fields.
    """
    if not isinstance(obj, list):
        raise TypeError(f"variables must be a list, got {type(obj).__name__}")
    out: list[VariableSpec] = []
    for item in obj:
        if not isinstance(item, dict) or "name" not in item or "distribution" not in item:
            raise TypeError("variable entry must have 'name' and 'distribution'")
        v: VariableSpec = {
            "name": str(item["name"]),
            "distribution": str(item["distribution"]),
            "min": float(item.get("min", 0.0)),
            "max": float(item.get("max", 0.0)),
            "mean": float(item.get("mean", 0.0)),
            "sigma": float(item.get("sigma", 0.0)),
        }
        out.append(v)
    return out


def cast_aggregate_result(obj: object) -> dict[str, Path]:
    """Narrow aggregate_results() return to a Path-keyed dict."""
    if not isinstance(obj, dict):
        raise TypeError(f"aggregate result must be a dict, got {type(obj).__name__}")
    out: dict[str, Path] = {}
    for k, v in obj.items():
        out[str(k)] = Path(str(v))
    return out


def cast_plot_paths(obj: object) -> list[Path]:
    """Narrow generate_plots() return to a list[Path]."""
    if not isinstance(obj, list):
        raise TypeError(f"plot list must be a list, got {type(obj).__name__}")
    out: list[Path] = []
    for item in obj:
        out.append(Path(str(item)))
    return out
