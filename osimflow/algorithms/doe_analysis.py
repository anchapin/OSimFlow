"""DOE analysis module for OSimFlow campaigns.

Provides Design of Experiments analysis capabilities:
- Main effects analysis (factor vs. response)
- Interaction effects analysis (2-way interactions)
- Factor sensitivity / importance ranking (Pareto chart)
- ANOVA-based decomposition of variance

Intended to complement openstudio-server's DOE analysis features
while staying within the OSimFlow Python-native stack.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

try:
    from statsmodels.formula.api import ols

    _STATSMOELS_AVAILABLE = True
except ImportError:
    _STATSMOELS_AVAILABLE = False

log = logging.getLogger("osimflow.algorithms.doe")


@dataclass
class MainEffect:
    """Main effect of a single factor on the response."""

    factor: str
    effect_size: float
    std_error: float
    t_statistic: float
    p_value: float
    levels: list[float]
    means: list[float]
    std_devs: list[float]
    counts: list[int]


@dataclass
class InteractionEffect:
    """Two-way interaction effect between two factors."""

    factor_a: str
    factor_b: str
    sse_interaction: float
    df_interaction: int
    ms_interaction: float
    f_statistic: float
    p_value: float


@dataclass
class FactorSensitivity:
    """Factor importance ranking from DOE analysis."""

    factor: str
    main_effect: float
    interaction_effect: float
    total_effect: float
    percent_contribution: float


class DOEAnalysis:
    """Design of Experiments analysis for simulation campaign results.

    Takes aggregated results (with input parameters and KPIs) and computes:
    - Main effects per factor
    - Two-way interaction effects
    - Factor sensitivity / importance ranking

    Suitable for LHS, Sobol, Halton, full-factorial, and other sampling designs.
    """

    def __init__(
        self,
        results_csv: Path,
        response_col: str = "eui_kwh_m2_yr",
        min_samples_per_level: int = 3,
    ) -> None:
        self.results_csv = Path(results_csv)
        self.response_col = response_col
        self.min_samples_per_level = min_samples_per_level
        self._df: pd.DataFrame | None = None
        self._factors: list[str] = []
        self._main_effects: list[MainEffect] = []
        self._interaction_effects: list[InteractionEffect] = []
        self._factor_sensitivity: list[FactorSensitivity] = []

    def load(self) -> None:
        """Load results CSV and identify factor columns."""
        self._df = pd.read_csv(self.results_csv)

        numeric_cols = self._df.select_dtypes(include="number").columns
        exclude = {"sample_id", self.response_col}
        self._factors = [c for c in numeric_cols if c not in exclude]

        log.info(
            "DOE analysis loaded: %d samples, %d factors, response=%s",
            len(self._df),
            len(self._factors),
            self.response_col,
        )

    def compute_main_effects(self) -> list[MainEffect]:
        """Compute main effect of each factor on the response.

        Uses simple linear regression (one-way ANOVA equivalent) to estimate
        the effect size and statistical significance of each factor.
        """
        if self._df is None:
            self.load()
        assert self._df is not None

        effects: list[MainEffect] = []
        for factor in self._factors:
            me = self._compute_single_main_effect(factor)
            if me is not None:
                effects.append(me)
        self._main_effects = effects
        return effects

    def _compute_single_main_effect(self, factor: str) -> MainEffect | None:
        """Compute main effect for a single factor."""
        if self._df is None:
            return None
        df = self._df.dropna(subset=[factor, self.response_col])
        if len(df) < self.min_samples_per_level:
            return None

        groups: list[np.ndarray[Any, np.dtype[Any]]] = []
        levels: list[float] = []
        for level, group in df.groupby(factor, sort=True):
            if len(group) >= self.min_samples_per_level:
                groups.append(group[self.response_col].values)
                levels.append(float(level))

        if len(groups) < 2:
            return None

        counts = [len(g) for g in groups]
        means = [float(np.mean(g)) for g in groups]
        std_devs = [float(np.std(g, ddof=1)) if len(g) > 1 else 0.0 for g in groups]

        grand_mean = df[self.response_col].mean()
        effect_size = float(np.mean(means) - grand_mean) if grand_mean else 0.0

        if len(groups) == 2 and all(len(g) >= 2 for g in groups):
            t_stat, p_value = stats.ttest_ind(groups[0], groups[1])
        else:
            f_stat, p_value = stats.f_oneway(*groups)
            t_stat = float(f_stat**0.5) if f_stat > 0 else 0.0

        pooled_std = float(
            np.sqrt(
                sum((n - 1) * sd**2 for n, sd in zip(counts, std_devs, strict=True)) / sum(counts)
            )
        )
        std_error = pooled_std * np.sqrt(sum(1 / n for n in counts if n > 0))

        return MainEffect(
            factor=factor,
            effect_size=effect_size,
            std_error=std_error,
            t_statistic=float(t_stat),
            p_value=float(p_value),
            levels=levels,
            means=means,
            std_devs=std_devs,
            counts=counts,
        )

    def compute_interaction_effects(self) -> list[InteractionEffect]:
        """Compute all 2-way interaction effects using ANOVA.

        Runs a 2-way ANOVA for each pair of factors and returns the
        interaction term's F-test p-value and sum of squares.
        """
        if self._df is None:
            self.load()
        assert self._df is not None

        interactions: list[InteractionEffect] = []
        n = len(self._factors)
        for i in range(n):
            for j in range(i + 1, n):
                fa = self._factors[i]
                fb = self._factors[j]
                ie = self._compute_2way_interaction(fa, fb)
                if ie is not None:
                    interactions.append(ie)
        self._interaction_effects = interactions
        return interactions

    def _compute_2way_interaction(self, factor_a: str, factor_b: str) -> InteractionEffect | None:
        """Compute 2-way interaction effect for a factor pair."""
        if self._df is None:
            return None
        df = self._df.dropna(subset=[factor_a, factor_b, self.response_col])
        if len(df) < self.min_samples_per_level * 2:
            return None

        try:
            if not _STATSMOELS_AVAILABLE:
                log.warning(
                    "statsmodels not available, skipping 2-way interaction for %s x %s",
                    factor_a,
                    factor_b,
                )
                return None
            formula = f"{self.response_col} ~ C({factor_a}) * C({factor_b})"
            model = ols(formula, data=df).fit()
            anova_table = stats.anova_lm(model, typ=2)

            if f"C({factor_a}):C({factor_b})" in anova_table.index:
                interaction_row = anova_table.loc[f"C({factor_a}):C({factor_b})"]
                sse_interaction = float(interaction_row["sum_sq"])
                df_interaction = int(interaction_row["df"])
                ms_interaction = float(interaction_row["mean_sq"])
                f_statistic = float(interaction_row["F"])
                p_value = float(interaction_row["PR(>F)"])
            else:
                sse_interaction = 0.0
                df_interaction = 0
                ms_interaction = 0.0
                f_statistic = 0.0
                p_value = 1.0

            return InteractionEffect(
                factor_a=factor_a,
                factor_b=factor_b,
                sse_interaction=sse_interaction,
                df_interaction=df_interaction,
                ms_interaction=ms_interaction,
                f_statistic=f_statistic,
                p_value=p_value,
            )
        except Exception as exc:
            log.warning("2-way ANOVA failed for %s x %s: %s", factor_a, factor_b, exc)
            return None

    def compute_factor_sensitivity(self) -> list[FactorSensitivity]:
        """Compute factor sensitivity / importance ranking.

        Combines main effects and total interaction effects into a
        single importance metric (percent contribution to total variance).
        """
        if not self._main_effects:
            self.compute_main_effects()
        if not self._interaction_effects:
            self.compute_interaction_effects()

        total_sse = sum(me.effect_size**2 for me in self._main_effects)
        for ie in self._interaction_effects:
            total_sse += ie.sse_interaction
        if total_sse == 0:
            total_sse = 1.0

        sensitivity: list[FactorSensitivity] = []
        for me in self._main_effects:
            interaction_sse = sum(
                ie.sse_interaction
                for ie in self._interaction_effects
                if me.factor in {ie.factor_a, ie.factor_b}
            )
            total_effect = me.effect_size**2 + interaction_sse
            sensitivity.append(
                FactorSensitivity(
                    factor=me.factor,
                    main_effect=me.effect_size,
                    interaction_effect=interaction_sse**0.5,
                    total_effect=total_effect**0.5,
                    percent_contribution=100.0 * total_effect / total_sse,
                )
            )

        sensitivity.sort(key=lambda x: x.percent_contribution, reverse=True)
        self._factor_sensitivity = sensitivity
        return sensitivity

    def to_dict(self) -> dict[str, Any]:
        """Serialize full DOE analysis to a dict for JSON output."""
        return {
            "response_column": self.response_col,
            "n_samples": len(self._df) if self._df is not None else 0,
            "n_factors": len(self._factors),
            "factors": self._factors,
            "main_effects": [
                {
                    "factor": me.factor,
                    "effect_size": me.effect_size,
                    "std_error": me.std_error,
                    "t_statistic": me.t_statistic,
                    "p_value": me.p_value,
                    "significant": me.p_value < 0.05,
                    "levels": me.levels,
                    "means": me.means,
                    "std_devs": me.std_devs,
                    "counts": me.counts,
                }
                for me in self._main_effects
            ],
            "interaction_effects": [
                {
                    "factor_a": ie.factor_a,
                    "factor_b": ie.factor_b,
                    "sse_interaction": ie.sse_interaction,
                    "df_interaction": ie.df_interaction,
                    "ms_interaction": ie.ms_interaction,
                    "f_statistic": ie.f_statistic,
                    "p_value": ie.p_value,
                    "significant": ie.p_value < 0.05,
                }
                for ie in self._interaction_effects
            ],
            "factor_sensitivity": [
                {
                    "factor": fs.factor,
                    "main_effect": fs.main_effect,
                    "interaction_effect": fs.interaction_effect,
                    "total_effect": fs.total_effect,
                    "percent_contribution": fs.percent_contribution,
                }
                for fs in self._factor_sensitivity
            ],
        }

    def write_json(self, outdir: Path) -> Path:
        """Write DOE analysis results to ``doe_analysis.json``."""
        outdir.mkdir(parents=True, exist_ok=True)
        path = outdir / "doe_analysis.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        log.info("Wrote DOE analysis to %s", path)
        return path


def run_doe_analysis(
    results_csv: Path,
    outdir: Path,
    response_col: str = "eui_kwh_m2_yr",
) -> Path:
    """Run full DOE analysis on *results_csv* and write results to *outdir*.

    Convenience function that instantiates ``DOEAnalysis``, runs all
    computations, and writes the JSON output.
    """
    analyzer = DOEAnalysis(results_csv, response_col=response_col)
    analyzer.compute_main_effects()
    analyzer.compute_interaction_effects()
    analyzer.compute_factor_sensitivity()
    return analyzer.write_json(outdir)
