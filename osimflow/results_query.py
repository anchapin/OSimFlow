"""Cross-campaign results query and export logic (issue #1699).

This module is the canonical home for the pure Python (no FastAPI)
logic that powers both the ``osimflow query-results`` /
``osimflow export-results`` CLI subcommands and the corresponding
HTTP endpoints exposed by :mod:`osimflow.api.results_query`.

The previous home for these helpers was
``osimflow.api.results_query``, but that module imports
:mod:`fastapi` at module scope — meaning the CLI subcommands crashed
with ``ModuleNotFoundError`` on any install without the optional
``[api]`` extra. This module exists so the core CLI surface has no
``fastapi`` dependency, while the API routes continue to delegate to
the shared helpers via thin FastAPI wrappers.

Public surface (importable without the ``[api]`` extra)
-------------------------------------------------------
- :func:`query_results_cli` — ``osimflow query-results`` helper.
- :func:`export_results_cli` — ``osimflow export-results`` helper.
- :func:`apply_filter` — MongoDB-style filter operator on a
  :class:`pandas.DataFrame` (re-exported as ``_apply_filter`` from
  the API module for backward compat).

These helpers are also re-exported from
``osimflow.api.results_query`` for backward compatibility with code
that imports them from the FastAPI module; that re-export emits a
:class:`DeprecationWarning` (issue #1699).
"""

from __future__ import annotations

__all__ = [
    "apply_filter",
    "export_results_cli",
    "load_aggregated_results",
    "query_results_cli",
]

import io
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger("osimflow.results_query")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def load_aggregated_results(campaign_dir: Path) -> pd.DataFrame:
    """Load ``aggregated_results.csv`` from a campaign directory.

    Returns an empty :class:`pandas.DataFrame` if the file does not
    exist or cannot be parsed. This is the canonical helper used by
    both the CLI and the API endpoints; the API module re-exports it
    as ``_load_aggregated_results``.
    """
    csv_path = campaign_dir / "aggregated_results.csv"
    if not csv_path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(csv_path)
    except Exception:  # noqa: BLE001
        log.warning("failed to read aggregated_results.csv in %s", campaign_dir)
        return pd.DataFrame()


def apply_filter(df: pd.DataFrame, filter_spec: dict[str, Any]) -> pd.DataFrame:  # noqa: PLR0912
    """Apply a MongoDB-style filter spec to a DataFrame.

    Supports:
      - Top-level equality: ``{"status": "ok"}``
      - Comparison operators: ``{"kpi.eui": {"$gt": 100}}``
      - ``$in``, ``$nin`` for array membership
      - ``$exists`` for field presence

    This is a pure-pandas operation with no FastAPI dependency — it is
    re-exported as ``_apply_filter`` from the API module.
    """
    if not filter_spec:
        return df

    for key, value in filter_spec.items():
        if key.startswith("$"):
            continue

        if isinstance(value, dict):
            for op, op_val in value.items():
                if op == "$eq":  # noqa: PLR0912
                    df = df[df[key] == op_val]
                elif op == "$ne":  # noqa: PLR0912
                    df = df[df[key] != op_val]
                elif op == "$gt":  # noqa: PLR0912
                    df = df[df[key] > op_val]
                elif op == "$gte":  # noqa: PLR0912
                    df = df[df[key] >= op_val]
                elif op == "$lt":  # noqa: PLR0912
                    df = df[df[key] < op_val]
                elif op == "$lte":  # noqa: PLR0912
                    df = df[df[key] <= op_val]
                elif op == "$in":  # noqa: PLR0912
                    df = df[df[key].isin(op_val)]
                elif op == "$nin":  # noqa: PLR0912
                    df = df[~df[key].isin(op_val)]
                elif op == "$exists":  # noqa: PLR0912
                    df = df[df[key].notna()] if op_val else df[df[key].isna()]
                else:
                    log.warning("unknown filter operator: %s", op)
        else:
            df = df[df[key] == value]

    return df


# ---------------------------------------------------------------------------
# CLI helpers (called from osimflow.__main__ via ``query-results`` /
# ``export-results`` subcommands).  These functions have no FastAPI
# dependency and are safe to import without the ``[api]`` extra.
# ---------------------------------------------------------------------------


def _resolve_paths(  # noqa: PLR0912
    campaign_ids: list[str] | None,
    outdirs: list[str] | None,
) -> list[tuple[Path, str]]:
    """Resolve CLI-supplied identifiers into (path, label) pairs.

    ``outdirs`` are taken as absolute-or-cwd-relative paths; missing
    directories are logged and skipped. ``campaign_ids`` are resolved
    relative to ``Path.cwd()`` (matching the historical CLI semantics
    where campaigns live next to the invocation directory).
    """
    paths: list[tuple[Path, str]] = []
    if outdirs:
        for outdir in outdirs:
            p = Path(outdir)
            if p.is_dir():
                paths.append((p, p.name))
            else:
                log.warning("Outdir not found, skipping: %s", outdir)
    if campaign_ids:
        base = Path.cwd()
        for cid in campaign_ids:
            campaign_dir = base / cid
            if campaign_dir.is_dir():
                paths.append((campaign_dir, cid))
    return paths


def query_results_cli(  # noqa: PLR0912
    campaign_ids: list[str] | None = None,
    outdirs: list[str] | None = None,
    filter_expr: str | None = None,
    page: int = 1,
    per_page: int = 50,
    format: str = "table",
) -> dict[str, Any]:
    """CLI helper for ``osimflow query-results``.

    Parameters
    ----------
    campaign_ids
        List of campaign IDs to query (resolved relative to ``Path.cwd()``).
    outdirs
        List of explicit output directory paths to query.
    filter_expr
        JSON filter expression as a string.
    page
        Page number (1-indexed).
    per_page
        Items per page.
    format
        Output format: ``table`` or ``json``.

    Returns
    -------
    dict
        Keys: ``rows``, ``total``, ``columns``, ``campaigns_queried``.
    """
    if not campaign_ids and not outdirs:
        return {"rows": [], "total": 0, "columns": [], "campaigns_queried": 0}

    all_rows: list[dict[str, Any]] = []
    all_columns: set[str] = set()
    campaigns_queried = 0

    filter_spec: dict[str, Any] = {}
    if filter_expr:
        try:
            filter_spec = json.loads(filter_expr)
        except json.JSONDecodeError as exc:
            log.error("Invalid filter expression: %s", exc)
            return {"rows": [], "total": 0, "columns": [], "campaigns_queried": 0}

    paths_to_query = _resolve_paths(campaign_ids, outdirs)

    for campaign_path, label in paths_to_query:
        df = load_aggregated_results(campaign_path)
        if df.empty:
            continue

        if filter_spec:
            df = apply_filter(df, filter_spec)

        if df.empty:
            continue

        campaigns_queried += 1

        for col in df.columns:
            if col not in ("sample_id",):
                all_columns.add(col)

        page_df = df.iloc[(page - 1) * per_page : page * per_page]
        rows = json.loads(page_df.to_json(orient="records"))
        for row in rows:
            row["_campaign"] = label
        all_rows.extend(rows)

    columns = sorted(all_columns)
    if not all_rows:
        return {"rows": [], "total": 0, "columns": columns, "campaigns_queried": campaigns_queried}

    return {
        "rows": all_rows,
        "total": len(all_rows),
        "columns": columns,
        "campaigns_queried": campaigns_queried,
    }


def export_results_cli(  # noqa: PLR0912
    campaign_ids: list[str] | None = None,
    outdirs: list[str] | None = None,
    filter_expr: str | None = None,
    format: str = "csv",
    output_path: str | None = None,
    include_failed: bool = True,
) -> int:
    """CLI helper for ``osimflow export-results``.

    Parameters
    ----------
    campaign_ids
        List of campaign IDs to export.
    outdirs
        List of explicit output directory paths to export.
    filter_expr
        JSON filter expression as a string.
    format
        Export format: ``csv`` or ``json``.
    output_path
        Output file path. If None, prints to stdout.
    include_failed
        Include failed simulations in export.

    Returns
    -------
    int
        Exit code (0 = success, 1 = error).
    """
    filter_spec: dict[str, Any] = {}
    if filter_expr:
        try:
            filter_spec = json.loads(filter_expr)
        except json.JSONDecodeError as exc:
            log.error("Invalid filter expression: %s", exc)
            return 1

    paths_to_query = _resolve_paths(campaign_ids, outdirs)
    if not paths_to_query:
        log.error("No valid campaign directories found")
        return 1

    all_dfs: list[pd.DataFrame] = []

    for campaign_path, label in paths_to_query:
        df = load_aggregated_results(campaign_path)
        if df.empty:
            log.warning("No aggregated_results.csv found in %s", campaign_path)
            continue

        if not include_failed and "status" in df.columns:
            df = df[df["status"] != "failed"]

        if filter_spec:
            df = apply_filter(df, filter_spec)

        if not df.empty:
            df["_campaign"] = label
            all_dfs.append(df)

    if not all_dfs:
        log.error("No results to export")
        return 1

    combined = pd.concat(all_dfs, ignore_index=True)

    if format == "json":
        records = json.loads(combined.to_json(orient="records"))
        content = json.dumps(
            {"campaigns": [label for _, label in paths_to_query], "rows": records},
            indent=2,
            default=str,
        )
    else:
        output = io.StringIO()
        combined.to_csv(output, index=False)
        content = output.getvalue()

    if output_path:
        Path(output_path).write_text(content)
        print(f"Exported {len(combined)} rows to {output_path}")
    else:
        print(content)

    return 0
