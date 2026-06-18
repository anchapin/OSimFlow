"""Cross-run data aggregation for OSimFlow (issue #588).

Provides ``CrossRunAggregator``, a utility class that loads per-campaign
``aggregated_results.csv`` files, merges them into a combined
:class:`pandas.DataFrame` annotated with campaign labels, and exposes
cross-run summary statistics (mean, std, min, max per KPI across runs)
and per-campaign KPI rankings.

CLI entry points
----------------
``osimflow aggregate-runs`` — merge two or more campaign result sets into
a combined CSV and print summary statistics.

``osimflow compare`` — enhanced to accept N campaign outdir paths (not
just two registry IDs) and display aligned KPI statistics across all
campaigns.

API endpoint
-------------
``GET /api/v1/campaigns/compare`` — compare N campaigns by ``outdir``
query parameters and return aligned KPI statistics in the same shape as
the existing POST endpoint.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

if False:
    from pandas import DataFrame

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #


@dataclass
class CampaignRunData:
    """Loaded data from a single campaign run."""

    outdir: Path
    label: str
    df: DataFrame
    n_samples: int = 0
    loaded_at: float = field(default_factory=time.time)

    @property
    def n_rows(self) -> int:
        return len(self.df)


@dataclass
class CrossRunStats:
    """Cross-run statistics for a single KPI metric."""

    kpi: str
    values: dict[str, float]  # campaign_label -> mean value
    overall_mean: float | None = None
    overall_std: float | None = None
    overall_min: float | None = None
    overall_max: float | None = None
    best_campaign: str | None = None
    worst_campaign: str | None = None

    def __post_init__(self) -> None:
        if self.values:
            finite = {k: v for k, v in self.values.items() if v is not None}
            if finite:
                self.overall_mean = sum(finite.values()) / len(finite)
                if len(finite) > 1:
                    vals = list(finite.values())
                    m = self.overall_mean
                    self.overall_std = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5
                self.overall_min = min(finite.values())
                self.overall_max = max(finite.values())
                self.best_campaign = min(finite.keys(), key=lambda k: finite[k])
                self.worst_campaign = max(finite.keys(), key=lambda k: finite[k])


# --------------------------------------------------------------------------- #
# Main aggregator class
# --------------------------------------------------------------------------- #


class CrossRunAggregator:
    """Aggregate and compare KPI data across multiple campaign runs.

    Parameters
    ----------
    campaigns
        Sequence of ``(outdir, label)`` pairs.  ``outdir`` is the path to
        a campaign output directory containing ``aggregated_results.csv``.
        ``label`` is the human-readable name used to identify the campaign
        in the output (e.g. ``"Run A"``, ``"v3.11.0-baseline"``).  If
        ``label`` is ``None`` the ``campaign_id`` from ``run.json`` is used,
        falling back to the outdir stem.
    """

    def __init__(
        self,
        campaigns: list[tuple[Path, str | None]] | None = None,
    ) -> None:
        self._campaigns: list[tuple[Path, str | None]] = campaigns or []
        self._runs: dict[str, CampaignRunData] = {}
        self._combined_df: DataFrame | None = None
        self._cross_run_stats: dict[str, CrossRunStats] = {}

    # ------------------------------------------------------------------ #
    # Campaign management
    # ------------------------------------------------------------------ #

    def add_campaign(self, outdir: Path, label: str | None = None) -> None:
        """Register a campaign directory for aggregation.

        Parameters
        ----------
        outdir
            Campaign output directory (must contain ``aggregated_results.csv``).
        label
            Optional human-readable label.  If omitted the ``campaign_id`` from
            ``run.json`` is used, falling back to the outdir stem.
        """
        self._campaigns.append((outdir, label))
        # Invalidate cached combined dataframe
        self._combined_df = None
        self._cross_run_stats = {}

    def remove_campaign(self, label: str) -> bool:
        """Remove a campaign by its label.

        Returns ``True`` if the label was found and removed.
        """
        original = len(self._campaigns)
        self._campaigns = [(orig, lbl) for orig, lbl in self._campaigns if lbl != label]
        if len(self._campaigns) < original:
            self._combined_df = None
            self._cross_run_stats = {}
            return True
        return False

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    def load(self) -> dict[str, CampaignRunData]:
        """Load ``aggregated_results.csv`` from all registered campaigns.

        Returns a dict mapping label -> ``CampaignRunData``.
        Campaigns whose CSV cannot be read are logged and skipped.
        """
        self._runs.clear()
        for outdir, label_hint in self._campaigns:
            label = label_hint or self._resolve_label(outdir)
            df = self._load_csv(outdir)
            if df is None:
                log.warning("Skipping campaign %s (no aggregated_results.csv)", outdir)
                continue
            self._runs[label] = CampaignRunData(
                outdir=outdir,
                label=label,
                df=df,
                n_samples=len(df),
            )
        return dict(self._runs)

    def _resolve_label(self, outdir: Path) -> str:
        """Resolve a campaign label from run.json or outdir stem."""
        run_json = outdir / "run.json"
        if run_json.exists():
            try:
                data = json.loads(run_json.read_text())
                cid = data.get("campaign_id")
                if cid:
                    return str(cid)
            except Exception:  # noqa: BLE001
                pass
        return outdir.stem

    def _load_csv(self, outdir: Path) -> pd.DataFrame | None:
        """Load and validate aggregated_results.csv from a campaign directory."""
        csv_path = outdir / "aggregated_results.csv"
        if not csv_path.exists():
            return None
        try:
            df = pd.read_csv(csv_path)
            # Ensure sample_id column exists
            if "sample_id" not in df.columns:
                if "Unnamed: 0" in df.columns:
                    df = df.rename(columns={"Unnamed: 0": "sample_id"})
                else:
                    df.insert(0, "sample_id", range(len(df)))
            return df
        except Exception:  # noqa: BLE001
            log.debug("Failed to read %s", csv_path, exc_info=True)
            return None

    # ------------------------------------------------------------------ #
    # Aggregation
    # ------------------------------------------------------------------ #

    def aggregate(self) -> pd.DataFrame:
        """Merge all per-campaign DataFrames into a combined DataFrame.

        Adds a ``campaign`` column to identify the source run.
        Adds a ``global_sample_id`` column of the form ``<label>_<sample_id>``.
        """
        if not self._runs:
            self.load()

        dfs: list[pd.DataFrame] = []
        for label, run in self._runs.items():
            df = run.df.copy()
            df["campaign"] = label
            # Build global sample id
            sid_col = "sample_id" if "sample_id" in df.columns else "Unnamed: 0"
            df["global_sample_id"] = label + "_" + df[sid_col].astype(str)
            dfs.append(df)

        if not dfs:
            self._combined_df = pd.DataFrame()
            return self._combined_df

        self._combined_df = pd.concat(dfs, ignore_index=True)
        return self._combined_df

    def get_combined_dataframe(self) -> pd.DataFrame:
        """Return the combined DataFrame, loading and aggregating if needed."""
        if self._combined_df is None:
            self.aggregate()
        return self._combined_df

    # ------------------------------------------------------------------ #
    # Cross-run statistics
    # ------------------------------------------------------------------ #

    def compute_cross_run_stats(self) -> dict[str, CrossRunStats]:
        """Compute cross-run statistics for all numeric KPIs.

        For each KPI metric, computes:
        - Per-campaign mean value
        - Overall mean / std / min / max across all campaigns
        - Best and worst performing campaign (by mean)
        """
        if not self._runs:
            self.load()

        if self._combined_df is None:
            self.aggregate()

        df = self._combined_df
        if df is None or df.empty:
            return {}

        kpi_cols = [
            c
            for c in df.columns
            if c
            not in (
                "sample_id",
                "Unnamed: 0",
                "campaign",
                "global_sample_id",
            )
        ]

        # Filter to numeric columns only
        assert df is not None
        numeric_cols = [c for c in kpi_cols if pd.api.types.is_numeric_dtype(df[c])]

        self._cross_run_stats.clear()
        for kpi in numeric_cols:
            values: dict[str, float | None] = {}
            for label, run in self._runs.items():
                if kpi in run.df.columns:
                    series = run.df[kpi].dropna()
                    if not series.empty:
                        values[label] = float(series.mean())
                    else:
                        values[label] = None
                else:
                    values[label] = None

            self._cross_run_stats[kpi] = CrossRunStats(kpi=kpi, values=values)  # type: ignore[arg-type]

        return dict(self._cross_run_stats)

    def get_cross_run_stats(self) -> dict[str, CrossRunStats]:
        """Return cross-run statistics, computing if needed."""
        if not self._cross_run_stats:
            self.compute_cross_run_stats()
        return self._cross_run_stats

    # ------------------------------------------------------------------ #
    # Ranking
    # ------------------------------------------------------------------ #

    def get_kpi_rankings(self, kpi: str) -> list[tuple[str, float]]:
        """Return campaigns ranked by their mean value for ``kpi`` (ascending).

        Returns a list of ``(campaign_label, mean_value)`` pairs sorted
        from lowest to highest mean.
        """
        stats = self.get_cross_run_stats()
        if kpi not in stats:
            return []
        s = stats[kpi]
        return sorted(
            [(label, v) for label, v in s.values.items() if v is not None],
            key=lambda x: x[1],
        )

    def get_best_campaigns(
        self,
        kpi: str,
        n: int = 3,
        ascending: bool = True,
    ) -> list[tuple[str, float]]:
        """Return the top ``n`` campaigns for ``kpi``.

        Parameters
        ----------
        kpi
            KPI metric name.
        n
            Number of top campaigns to return.
        ascending
            If True (default), return lowest-first (e.g. for energy use).
            If False, return highest-first (e.g. for comfort hours).
        """
        rankings = self.get_kpi_rankings(kpi)
        if ascending:
            return rankings[:n]
        return list(reversed(rankings))[:n]

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def export_combined_csv(self, output_path: Path) -> None:
        """Write the combined DataFrame to a CSV file.

        Parameters
        ----------
        output_path
            Destination CSV path.  Parent directories are created.
        """
        df = self.get_combined_dataframe()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False)
        log.info("Exported combined results to %s (%d rows)", output_path, len(df))

    # ------------------------------------------------------------------ #
    # CLI helpers
    # ------------------------------------------------------------------ #

    def summary(self) -> dict[str, object]:
        """Return a human-readable summary dict for CLI output."""
        runs = self._runs if self._runs else self.load()
        combined = self.get_combined_dataframe()
        stats = self.get_cross_run_stats()

        kpi_summary: dict[str, dict[str, str | float | None]] = {}
        for kpi, s in stats.items():
            kpi_summary[kpi] = {
                "best_campaign": s.best_campaign,
                "best_value": (s.values.get(s.best_campaign) if s.best_campaign else None),
                "overall_mean": s.overall_mean,
                "overall_std": s.overall_std,
            }

        return {
            "n_campaigns": len(runs),
            "campaigns": list(runs.keys()),
            "total_samples": sum(r.n_samples for r in runs.values()),
            "combined_rows": len(combined),
            "kpis": list(stats.keys()),
            "kpi_summary": kpi_summary,
        }
