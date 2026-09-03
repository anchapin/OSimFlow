"""Baseline KPI comparison for Campaign (issue #1462 extraction).

Extracted from ``osimflow.campaign``: the baseline-sample comparison
introduced by issue #64.  Reads the baseline sample's KPIs and the
parametric samples' KPIs and computes per-KPI improvement ranges,
stored on the :class:`~osimflow.monitoring.RunTrace`.
"""

import json
import logging
from pathlib import Path

from .config import CampaignConfig
from .monitoring import RunTrace

log = logging.getLogger("osimflow.campaign")


def baseline_sample_id(cfg: CampaignConfig) -> str | None:
    """Return the baseline sample_id from config, or None."""
    if cfg.baseline is None:
        return None
    return str(cfg.baseline.get("sample_id", "baseline"))


def read_all_kpis(kpi_files: list[Path]) -> dict[str, dict[str, float]]:
    """Read KPI files into a {sample_id: {kpi_name: value}} mapping."""
    all_kpis: dict[str, dict[str, float]] = {}
    for kpi_path in kpi_files:
        try:
            data = json.loads(kpi_path.read_text())
            sid = str(data.get("sample_id", kpi_path.stem.replace("kpi_", "")))
            kpis = data.get("kpis", {})
            numeric_kpis = {k: float(v) for k, v in kpis.items() if isinstance(v, (int, float))}
            all_kpis[sid] = numeric_kpis
        except Exception as exc:
            log.warning(
                "could not read KPI file %s for baseline comparison: %s",
                kpi_path,
                exc,
                exc_info=True,
            )
    return all_kpis


def compute_improvement_range(
    baseline_sid: str,
    baseline_kpis: dict[str, float],
    all_kpis: dict[str, dict[str, float]],
) -> dict[str, object]:
    """Compute pct improvement range for each KPI relative to baseline."""
    comparison: dict[str, object] = {}
    for kpi_name, baseline_val in baseline_kpis.items():
        if baseline_val == 0:
            continue
        parametric_values = [
            kpis[kpi_name]
            for sid, kpis in all_kpis.items()
            if sid != baseline_sid and kpi_name in kpis
        ]
        if not parametric_values:
            continue
        improvements = [(baseline_val - v) / baseline_val * 100.0 for v in parametric_values]
        comparison[f"baseline_{kpi_name}"] = round(baseline_val, 2)
        comparison[f"min_{kpi_name}_improvement_pct"] = round(min(improvements), 2)
        comparison[f"max_{kpi_name}_improvement_pct"] = round(max(improvements), 2)
    return comparison


def compute_baseline_comparison(
    cfg: CampaignConfig,
    trace: RunTrace,
    kpi_files: list[Path],
) -> None:
    """Compute baseline comparison metrics and store on the run trace.

    Reads the baseline sample's KPIs and computes improvement statistics
    across all parametric samples. Populates ``trace.baseline_comparison``
    (issue #64).

    When no baseline is configured, this is a no-op.
    """
    baseline_sid = baseline_sample_id(cfg)
    if baseline_sid is None:
        return

    all_kpis = read_all_kpis(kpi_files)
    if baseline_sid not in all_kpis:
        log.warning(
            "baseline sample_id=%s not found in KPI files; skipping baseline comparison",
            baseline_sid,
        )
        return

    baseline_kpis = all_kpis[baseline_sid]
    comparison = compute_improvement_range(baseline_sid, baseline_kpis, all_kpis)
    if comparison:
        trace.baseline_comparison = comparison
        log.info("baseline comparison: %s", comparison)
