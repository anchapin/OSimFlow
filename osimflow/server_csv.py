"""openstudio-server ``download_data.csv?export=true`` compatible export (issue #1872).

Builds, from a campaign output directory, a CSV with the column layout the
openstudio-bem-to-surrogate gem consumes from the EKS / OpenStack server path
(``rake download_results``). Layout (see ``docs/migration-openstudio-server.md``):

1. ``name``, ``_id``, ``status``, ``status_message`` — datapoint identity.
2. Variable columns ``<measure>.<argument>`` in ``samples.json`` order.
3. Output columns (KPI / ``reporting_179_d.*`` values) in aggregated order.
4. ``reporting_179_d.simulation_failed_message`` — always last, empty on success.

Failed samples (``failed_simulations.csv``) are included with
``status_message`` ``datapoint failure`` and their error text in the failure
column, matching what the gem's failure enrichment expects.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

SIMULATION_FAILED_COL = "reporting_179_d.simulation_failed_message"
IDENTITY_COLUMNS = ("name", "_id", "status", "status_message")
STATUS_COMPLETED = "completed"
MESSAGE_NORMAL = "completed normal"
MESSAGE_FAILURE = "datapoint failure"

# Columns OSimFlow adds that have no server equivalent.
_DROPPED_COLUMNS = frozenset(
    {"sample_id", "status", "_campaign", "error_summary", "exit_code", "log_path"}
)


def _find_samples_json(campaign_dir: Path) -> Path | None:
    for cand in (campaign_dir / "samples.json", campaign_dir / "work" / "samples.json"):
        if cand.is_file():
            return cand
    found = sorted(campaign_dir.rglob("samples.json"))
    return found[0] if found else None


def _load_samples(campaign_dir: Path) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Variable names (samples.json order) and per-sample values."""
    path = _find_samples_json(campaign_dir)
    if path is None:
        return [], {}
    try:
        data: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s: %s", path, exc, exc_info=True)
        return [], {}
    names: list[str] = []
    values: dict[str, dict[str, Any]] = {}
    for sample in data.get("samples", []):
        flat: dict[str, Any] = {}
        for key, val in sample.get("values", {}).items():
            if key not in names:
                names.append(key)
            flat[key] = val["label"] if isinstance(val, dict) and "label" in val else val
        values[str(sample.get("sample_id", ""))] = flat
    return names, values


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype={"sample_id": str})
    except Exception:
        log.warning("Could not read %s", path, exc_info=True)
        return pd.DataFrame()


def build_server_csv_frame(campaign_dir: Path) -> pd.DataFrame:
    """Return the server-layout DataFrame for one campaign directory."""
    ok = _read_csv(campaign_dir / "aggregated_results.csv")
    failed = _read_csv(campaign_dir / "failed_simulations.csv")
    if ok.empty and failed.empty:
        return pd.DataFrame()

    variables, sample_values = _load_samples(campaign_dir)
    outputs = [
        c
        for c in ok.columns
        if c not in _DROPPED_COLUMNS and c not in variables and c != SIMULATION_FAILED_COL
    ]

    rows: list[dict[str, Any]] = []
    for rec in ok.to_dict(orient="records"):
        sid = str(rec["sample_id"])
        row: dict[str, Any] = {
            "name": sid,
            "_id": sid,
            "status": STATUS_COMPLETED,
            "status_message": MESSAGE_NORMAL,
        }
        for col in variables:
            row[col] = sample_values.get(sid, {}).get(col, rec.get(col))
        for col in outputs:
            row[col] = rec.get(col)
        row[SIMULATION_FAILED_COL] = rec.get(SIMULATION_FAILED_COL, "")
        rows.append(row)
    for rec in failed.to_dict(orient="records"):
        sid = str(rec["sample_id"])
        row = {
            "name": sid,
            "_id": sid,
            "status": STATUS_COMPLETED,
            "status_message": MESSAGE_FAILURE,
            SIMULATION_FAILED_COL: rec.get("error_summary", ""),
        }
        for col in variables:
            row[col] = sample_values.get(sid, {}).get(col)
        rows.append(row)

    columns = [*IDENTITY_COLUMNS, *variables, *outputs, SIMULATION_FAILED_COL]
    df = pd.DataFrame(rows, columns=columns)
    df[SIMULATION_FAILED_COL] = df[SIMULATION_FAILED_COL].fillna("")
    df.attrs["variables"] = list(variables)
    return df


def combine_server_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate per-campaign frames keeping identity, variables, outputs, failure order."""
    variables: list[str] = []
    outputs: list[str] = []
    for df in frames:
        var_names = list(df.attrs.get("variables", []))
        for col in var_names:
            if col not in variables:
                variables.append(col)
        for col in df.columns:
            if (
                col not in IDENTITY_COLUMNS
                and col != SIMULATION_FAILED_COL
                and col not in var_names
                and col not in outputs
            ):
                outputs.append(col)
    outputs = [c for c in outputs if c not in variables]
    columns = [*IDENTITY_COLUMNS, *variables, *outputs, SIMULATION_FAILED_COL]
    return pd.concat([f.reindex(columns=columns) for f in frames], ignore_index=True)
