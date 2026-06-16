"""Uncertainty Quantification (UQ) framework (issue #530).

Provides Monte Carlo propagation, probability of failure (POF) analysis,
and confidence interval (CI) computation for campaign outputs.

The UQ algorithm is a single-shot sampler (like LHS/Sobol) that runs after
KPI extraction. It reads per-sample KPI values and produces a
``uq_results.json`` containing POF, CIs, and distribution summaries.

Usage::

    osimflow run \\
        --algorithm uq \\
        --uq-method monte_carlo \\
        --uq-n-samples 10000 \\
        --uq-failure-threshold eui=150 \\
        --input_variables variables.yml \\
        --template_sim_package ./model \\
        --n_samples 500

The UQ framework uses the same Latin Hypercube sampling infrastructure
as ``LHSAlgorithm`` but adds post-simulation UQ analysis via
``compute_uq_indices()``.
"""

import json
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import scipy.stats

from osimflow.algorithms import (
    BaseAlgorithm,
    _normalise_var_list,
    _partition_variables,
    _resolve_conditional,
    _sample_independent,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.uq")

SUPPORTED_UQ_METHODS = {"monte_carlo", "latin_hypercube"}


def _parse_failure_threshold(raw: str) -> tuple[str, float]:
    """Parse a failure threshold string like 'eui=150' or 'cooling=5000'.

    Returns (kpi_name, threshold_value).
    """
    if "=" not in raw:
        raise ValueError(f"failure threshold must be 'kpi_name=value', got {raw!r}")
    kpi_name, value_str = raw.split("=", 1)
    kpi_name = kpi_name.strip()
    try:
        value = float(value_str.strip())
    except ValueError:
        raise ValueError(f"failure threshold value must be numeric, got {value_str!r}") from None
    return kpi_name, value


def _compute_confidence_interval(
    values: np.ndarray[Any, Any],
    confidence: float = 0.95,
) -> dict[str, float]:
    """Compute mean and confidence interval for an array of KPI values.

    Uses the t-distribution for CI computation.
    """
    n = len(values)
    if n < 2:
        return {
            "mean": float(np.mean(values)),
            "ci_lower": float(values[0]),
            "ci_upper": float(values[0]),
        }
    mean = float(np.mean(values))
    se = float(scipy.stats.sem(values))
    df = n - 1
    t_crit = scipy.stats.t.ppf((1 + confidence) / 2, df=df)
    ci_lower = mean - t_crit * se
    ci_upper = mean + t_crit * se
    return {
        "mean": mean,
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
        "std": float(np.std(values, ddof=1)),
        "n": n,
    }


def _compute_pof(
    kpi_values: dict[str, float],
    kpi_name: str,
    threshold: float,
    direction: str = "greater",
) -> dict[str, Any]:
    """Compute probability of failure for a KPI exceeding a threshold.

    Parameters
    ----------
    kpi_values
        Mapping of sample_id -> KPI value.
    kpi_name
        Name of the KPI to evaluate.
    threshold
        Failure threshold value.
    direction
        'greater' means failure when KPI > threshold (e.g., EUI > target).
        'less' means failure when KPI < threshold (e.g., comfort < target).

    Returns
    -------
    dict with pof, threshold, direction, n_failed, n_total.
    """
    values = np.array(list(kpi_values.values()))
    n_total = len(values)

    if direction == "greater":
        n_failed = int(np.sum(values > threshold))
    else:
        n_failed = int(np.sum(values < threshold))

    pof = float(n_failed) / float(n_total)

    return {
        "pof": pof,
        "threshold": threshold,
        "direction": direction,
        "kpi_name": kpi_name,
        "n_failed": n_failed,
        "n_total": n_total,
    }


def _compute_distribution_summary(
    values: np.ndarray[Any, Any],
) -> dict[str, Any]:
    """Compute summary statistics and histogram-ready bin edges for a KPI."""
    n = len(values)
    mean = float(np.mean(values))
    std = float(np.std(values, ddof=1))
    median = float(np.median(values))
    min_val = float(np.min(values))
    max_val = float(np.max(values))

    percentiles = [5, 10, 25, 50, 75, 90, 95]
    pct_values = {f"p{p}": float(np.percentile(values, p)) for p in percentiles}

    nbins = min(20, max(5, int(math.sqrt(n))))
    hist_counts, bin_edges = np.histogram(values, bins=nbins)
    hist_data = [
        {"bin_start": float(bin_edges[i]), "bin_end": float(bin_edges[i + 1]), "count": int(c)}
        for i, c in enumerate(hist_counts)
    ]

    return {
        "mean": mean,
        "std": std,
        "median": median,
        "min": min_val,
        "max": max_val,
        "n": n,
        "percentiles": pct_values,
        "histogram": hist_data,
    }


class UncertaintyQuantification(BaseAlgorithm):
    """Uncertainty Quantification (UQ) single-shot algorithm.

    Uses Latin Hypercube sampling (Monte Carlo propagation) to propagate
    input parameter distributions through the simulation and compute:

    - **Probability of Failure (POF)**: fraction of samples where a KPI
      exceeds a user-specified threshold.
    - **Confidence Intervals (CI)**: t-distribution-based confidence
      intervals for each KPI at a given confidence level.
    - **Distribution Summaries**: mean, median, std, min, max, percentiles,
      and histogram data for each KPI.

    ``is_iterative()`` returns ``False`` — UQ is single-shot.
    ``is_converged()`` always returns ``True``.
    """

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        """Generate LHS samples for UQ analysis.

        Delegates to ``_sample_independent`` which uses
        ``scipy.stats.qmc.LatinHypercube`` — the same engine as
        ``LHSAlgorithm`` but exposed here for clarity.
        """
        outdir.mkdir(parents=True, exist_ok=True)
        samples_path = outdir / "samples.json"

        var_list = _normalise_var_list(variables.get("variables", []))
        if not var_list:
            return _write_empty_samples(samples_path)

        independent_vars, conditional_vars = _partition_variables(var_list)
        if not independent_vars:
            return _write_empty_samples(samples_path)

        try:
            samples = _sample_independent(independent_vars, n_samples, seed)
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_uq_samples failed") from exc

        if conditional_vars:
            _resolve_conditional(samples, conditional_vars, n_samples)

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        return samples_path

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Single-shot: return the samples from the last iteration."""
        if not history:
            return []
        last = history[-1].get("samples", [])
        last_samples: list[dict[str, Any]] = list(last)
        return last_samples

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Single-shot algorithms are always converged."""
        return True

    def name(self) -> str:
        return "uq"

    def is_iterative(self) -> bool:
        return False

    def compute_uq_indices(  # noqa: PLR0912, PLR0913
        self,
        variables: dict[str, Any],
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        outdir: Path,
        failure_thresholds: dict[str, tuple[float, str]] | None = None,
        confidence: float = 0.95,
    ) -> Path:
        """Compute UQ indices from KPI values.

        Parameters
        ----------
        variables
            Parsed ``variables.yml`` dict (same as ``generate_samples``).
        samples
            List of sample dicts produced by ``generate_samples()``.
        kpi_values
            Dict mapping ``sample_id`` -> dict of KPI name -> value.
        outdir
            Directory to write the UQ results JSON.
        failure_thresholds
            Optional dict mapping KPI name -> (threshold, direction).
            direction is 'greater' (failure if KPI > threshold) or
            'less' (failure if KPI < threshold).
            Example: {"eui": (150.0, "greater")}
        confidence
            Confidence level for CI computation (default: 0.95 for 95% CI).

        Returns
        -------
        Path
            Path to the written ``uq_results.json`` file.

        Raises
        ------
        RuntimeError
            When no KPI values are available.
        """
        outdir.mkdir(parents=True, exist_ok=True)
        uq_results_path = outdir / "uq_results.json"

        if not kpi_values:
            raise RuntimeError("compute_uq_indices: no KPI values provided")

        failure_thresholds = failure_thresholds or {}

        all_kpi_names: set[str] = set()
        for kpis in kpi_values.values():
            all_kpi_names.update(kpis.keys())

        if not all_kpi_names:
            raise RuntimeError("compute_uq_indices: no numeric KPIs found")

        distributions: dict[str, dict[str, Any]] = {}
        pof_results: dict[str, dict[str, Any]] = {}
        ci_results: dict[str, dict[str, Any]] = {}

        for kpi_name in sorted(all_kpi_names):
            values_list: list[float] = []
            for sample in samples:
                sid = str(sample["sample_id"])
                kpis = kpi_values.get(sid, {})
                if kpi_name in kpis and isinstance(kpis[kpi_name], (int, float)):
                    values_list.append(float(kpis[kpi_name]))

            if not values_list:
                continue

            values = np.array(values_list)

            distributions[kpi_name] = _compute_distribution_summary(values)

            ci_results[kpi_name] = _compute_confidence_interval(values, confidence=confidence)

            if kpi_name in failure_thresholds:
                threshold, direction = failure_thresholds[kpi_name]
                pof_results[kpi_name] = _compute_pof(
                    {
                        sid: kpi_values[sid][kpi_name]
                        for sid in kpi_values
                        if kpi_name in kpi_values[sid]
                    },
                    kpi_name=kpi_name,
                    threshold=threshold,
                    direction=direction,
                )

        output: dict[str, Any] = {
            "algorithm": "uq",
            "confidence_level": confidence,
            "n_samples": len(samples),
            "distributions": distributions,
            "confidence_intervals": ci_results,
        }

        if pof_results:
            output["probability_of_failure"] = pof_results

        uq_results_path.write_text(json.dumps(output, indent=2))
        log.info(
            "UQ results computed for %d KPIs, %d samples, POF computed for %d KPIs",
            len(distributions),
            len(samples),
            len(pof_results),
        )
        return uq_results_path
