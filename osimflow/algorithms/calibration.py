"""Calibration algorithms for energy model calibration (issue #528).

Implements BM25-based calibration that minimizes error between simulated
and measured energy end-uses using utility bill data. Supports ASHRAE 14
compliance metrics (NMBE, CVRMSE).

Workflow
--------
1. ``generate_samples()`` creates an initial LHS population.
2. ``observe()`` reads KPI files, computes calibration metrics vs measured
   utility data, and proposes new samples that minimize the error.
3. ``is_converged()`` returns True when the calibration metric is below
   the ASHRAE 14 threshold or max_generations is reached.
"""

import csv
import json
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import scipy.stats.qmc
from scipy.optimize import minimize

from osimflow.algorithms import (
    BaseAlgorithm,
    _normalise_var_list,
    _partition_variables,
    _sample_with_engine,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.calibration")

#: ASHRAE 14 Tier 1 thresholds for calibration quality.
ASHRAE14_THRESHOLDS: dict[str, float] = {
    "cvrmse": 30.0,  # CV(RMSE) ≤ 30% for monthly data
    "nmbe": 10.0,  # NMBE ≤ ±10% for monthly data
}


def _load_calibration_data(csv_path: Path) -> dict[str, list[float]]:
    """Load measured utility data from a CSV file.

    Expected CSV format::

        month,electricity,natural_gas
        1,1000.0,500.0
        2,1100.0,450.0
        ...

    Returns
    -------
    dict[str, list[float]]
        Dict mapping end-use names to monthly measured values.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"calibration data not found: {csv_path}")

    data: dict[str, list[float]] = {}
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            for key, val in row.items():
                if key == "month":
                    continue
                if key not in data:
                    data[key] = []
                try:
                    parsed_val = float(val)
                except (ValueError, TypeError):
                    parsed_val = 0.0
                data[key].append(parsed_val)
    return data


def _compute_bm25_score(
    simulated: dict[str, list[float]],
    measured: dict[str, list[float]],
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """Compute BM25-style score between simulated and measured data.

    BM25 is a probabilistic ranking function used in information retrieval.
    Here we adapt it to measure the similarity between simulated and
    measured energy consumption time series.

    Lower scores indicate better match (smaller error).
    """
    if not simulated or not measured:
        return float("inf")

    total_score = 0.0
    n_terms = 0

    for end_use, meas_vals_data in measured.items():
        if end_use not in simulated:
            continue

        sim_vals = np.array(simulated[end_use])
        meas_vals = np.array(meas_vals_data)

        if len(sim_vals) == 0 or len(meas_vals) == 0:
            continue

        # Compute term frequencies (normalized differences)
        # Using (sim - meas) / (meas + epsilon) as the "term frequency"
        epsilon = 1e-6
        tf = np.abs(sim_vals - meas_vals) / (np.abs(meas_vals) + epsilon)

        # Document frequency component (IDF-like)
        # High error = low IDF weight = low contribution to score
        mean_error = np.mean(tf)
        idf = math.log((1.0 + k1) / (1.0 + k1 * mean_error))

        # BM25 term frequency saturation (sublinear TF)
        sat_tf = (k1 + 1) * tf / (k1 * (1 - b + b) + tf)

        # Aggregate per-document scores
        doc_scores = sat_tf * idf
        total_score += np.sum(doc_scores)
        n_terms += len(tf)

    if n_terms == 0:
        return float("inf")

    return total_score / n_terms


def _compute_nmbe(
    simulated: dict[str, list[float]],
    measured: dict[str, list[float]],
) -> float:
    """Compute Normalized Mean Bias Error (NMBE).

    NMBE = (sum(sim - meas) / sum(meas)) * 100

    Expressed as a percentage. Lower absolute value is better.
    Target: |NMBE| ≤ 10% (ASHRAE 14 Tier 1).
    """
    total_numerator = 0.0
    total_denominator = 0.0

    for end_use, meas_vals_data in measured.items():
        if end_use not in simulated:
            continue

        sim_vals = np.array(simulated[end_use])
        meas_vals = np.array(meas_vals_data)

        if len(sim_vals) == 0 or len(meas_vals) == 0:
            continue

        total_numerator += np.sum(sim_vals - meas_vals)
        total_denominator += np.sum(meas_vals)

    if total_denominator == 0:
        return float("inf")

    return (total_numerator / total_denominator) * 100.0


def _compute_cvrmse(
    simulated: dict[str, list[float]],
    measured: dict[str, list[float]],
) -> float:
    """Compute Coefficient of Variation of Root Mean Square Error (CVRMSE).

    CVRMSE = sqrt(mean((sim - meas)^2)) / mean(meas) * 100

    Expressed as a percentage. Lower is better.
    Target: CVRMSE ≤ 30% (ASHRAE 14 Tier 1).
    """
    total_mse = 0.0
    total_count = 0
    total_measured_mean = 0.0
    total_meas_count = 0

    for end_use, meas_vals_data in measured.items():
        if end_use not in simulated:
            continue

        sim_vals = np.array(simulated[end_use])
        meas_vals = np.array(meas_vals_data)

        if len(sim_vals) == 0 or len(meas_vals) == 0:
            continue

        mse = np.mean((sim_vals - meas_vals) ** 2)
        total_mse += mse * len(sim_vals)
        total_count += len(sim_vals)
        total_measured_mean += np.sum(meas_vals)
        total_meas_count += len(meas_vals)

    if total_count == 0 or total_measured_mean == 0:
        return float("inf")

    rmse = math.sqrt(total_mse / total_count)
    cvrmse = (rmse / (total_measured_mean / total_meas_count)) * 100.0
    return cvrmse


def _compute_calibration_metric(
    metric: str,
    simulated: dict[str, list[float]],
    measured: dict[str, list[float]],
) -> float:
    """Compute the specified calibration metric.

    Parameters
    ----------
    metric
        Metric name: "bm25", "nmbe", "cvrmse".
    simulated
        Dict of simulated end-use time series.
    measured
        Dict of measured end-use time series.

    Returns
    -------
    float
        The metric value. Lower is better for all metrics.
    """
    if metric == "bm25":
        return _compute_bm25_score(simulated, measured)
    elif metric == "nmbe":
        return abs(_compute_nmbe(simulated, measured))
    elif metric == "cvrmse":
        return _compute_cvrmse(simulated, measured)
    else:
        log.warning("unknown calibration metric '%s', falling back to bm25", metric)
        return _compute_bm25_score(simulated, measured)


def _extract_bounds(independent_vars: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Extract (min, max) bounds from variable definitions."""
    bounds: list[tuple[float, float]] = []
    for var_def in independent_vars:
        dist = var_def.get("distribution", "uniform")
        if dist == "uniform":
            bounds.append((float(var_def["min"]), float(var_def["max"])))
        elif dist == "normal":
            mu = float(var_def["mean"])
            sigma = float(var_def["sigma"])
            bounds.append((mu - 3 * sigma, mu + 3 * sigma))
        elif dist == "lognormal":
            log_mu = float(var_def["mean"])
            sigma = float(var_def["sigma"])
            bounds.append((max(1e-10, log_mu - 3 * sigma), log_mu + 3 * sigma))
        elif dist == "triangular":
            bounds.append((float(var_def["min"]), float(var_def["max"])))
        else:
            bounds.append((0.0, 1.0))
    return bounds


def _read_kpi_values(
    history: list[dict[str, Any]],
    objective_kpi: str,
) -> list[tuple[list[float], float]]:
    """Read (params, kpi_value) pairs from history entries."""
    results: list[tuple[list[float], float]] = []
    for entry in history:
        samples: list[dict[str, Any]] = entry.get("samples", [])
        kpi_files: list[str] = entry.get("kpi_files", [])
        for i, sample in enumerate(samples):
            if i < len(kpi_files):
                kpi_path = Path(kpi_files[i])
                if kpi_path.exists():
                    try:
                        kpi_data = json.loads(kpi_path.read_text())
                        kpis = kpi_data.get("kpis", {})
                        value = float(kpis.get(objective_kpi, float("inf")))
                        params = list(sample.get("values", {}).values())
                        results.append((params, value))
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
    return results


def _propose_samples_around(
    center: npt.NDArray[np.float64],
    n_new: int,
    bounds: list[tuple[float, float]],
    var_names: list[str],
    width: float = 0.1,
) -> list[dict[str, Any]]:
    """Propose *n_new* sample dicts near *center* with Gaussian perturbation."""
    dim = len(bounds)
    rng = np.random.default_rng(seed=42)
    new_samples: list[dict[str, Any]] = []
    for i in range(n_new):
        perturbation = rng.normal(0, width, size=dim)
        candidate = center + perturbation
        for j, (lo, hi) in enumerate(bounds):
            candidate[j] = np.clip(candidate[j], lo, hi)
        values: dict[str, Any] = {name: float(candidate[j]) for j, name in enumerate(var_names)}
        new_samples.append({"sample_id": f"{i + 1:04d}", "values": values})
    return new_samples


class CalibrationAlgorithm(BaseAlgorithm):
    """Base class for calibration algorithms.

    Calibration algorithms minimize error between simulated outputs
    and measured utility data. Subclasses implement specific optimization
    strategies.

    Parameters
    ----------
    calibration_data
        Path to CSV file containing measured utility data.
    metric
        Calibration metric to use: "bm25" (default), "nmbe", or "cvrmse".
    objective_kpi
        Name of the simulated KPI to compare against measured data.
    tol
        Convergence tolerance for the calibration metric.
    max_generations
        Maximum number of generations to run.
    """

    def __init__(
        self,
        calibration_data: Path | None = None,
        metric: str = "bm25",
        objective_kpi: str = "eui",
        tol: float = 1e-3,
        max_generations: int = 50,
    ) -> None:
        self._calibration_data = calibration_data
        self._metric = metric
        self._objective_kpi = objective_kpi
        self._tol = tol
        self._max_generations = max_generations
        self._measured_data: dict[str, list[float]] = {}
        self._best_params: npt.NDArray[np.float64] = np.array([])
        self._best_value: float = float("inf")
        self._prev_best: float = float("inf")
        self._independent_vars: list[dict[str, Any]] = []
        self._bounds: list[tuple[float, float]] = []
        self._proposed_samples: list[dict[str, Any]] = []
        self._generation_count: int = 0

    def configure(self, config: Any) -> None:
        """Configure the algorithm with campaign-level settings.

        Parameters
        ----------
        config
            The CampaignConfig instance containing algorithm-specific settings.
        """
        if hasattr(config, "calibration_data") and config.calibration_data:
            self._calibration_data = Path(config.calibration_data)
        if hasattr(config, "calibration_metric") and config.calibration_metric:
            self._metric = str(config.calibration_metric)

    def load_calibration_data(self) -> dict[str, list[float]]:
        """Load and return the calibration data from CSV.

        Returns
        -------
        dict[str, list[float]]
            Dict mapping end-use names to monthly measured values.
        """
        if not self._calibration_data:
            raise ValueError("calibration_data not set")
        self._measured_data = _load_calibration_data(self._calibration_data)
        return self._measured_data

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        """Generate initial or subsequent population.

        Generation 0 creates a Latin Hypercube population. Later
        generations propose new points around the current best.
        """
        self._configure_from_variables(variables)
        outdir.mkdir(parents=True, exist_ok=True)
        samples_path = outdir / "samples.json"

        var_list = _normalise_var_list(variables.get("variables", []))
        if not var_list:
            return _write_empty_samples(samples_path)

        independent_vars, conditional_vars = _partition_variables(var_list)
        if not independent_vars:
            return _write_empty_samples(samples_path)

        self._independent_vars = independent_vars
        self._bounds = _extract_bounds(independent_vars)

        # Check explicit feedback-loop slot (issue #332).
        if self._pending_proposed_samples:
            samples = self._pending_proposed_samples
            self._pending_proposed_samples = []
            self._proposed_samples = []
            samples_path.write_text(json.dumps({"samples": samples}, indent=2))
            log.info("Calibration proposed %d samples from explicit slot", len(samples))
            return samples_path

        # Fallback to internal state.
        if self._proposed_samples:
            samples = self._proposed_samples
            self._proposed_samples = []
            samples_path.write_text(json.dumps({"samples": samples}, indent=2))
            log.info("Calibration proposed %d samples from optimizer state", len(samples))
            return samples_path

        # For initial population, use LHS.
        try:
            samples = _sample_with_engine(
                scipy.stats.qmc.LatinHypercube,
                independent_vars,
                n_samples,
                seed,
            )
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_calibration_samples failed: initial LHS population") from exc

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        log.info("Calibration generated %d initial samples", len(samples))
        return samples_path

    def _update_best(self, results: list[tuple[list[float], float]]) -> None:
        """Update ``_best_params`` and ``_best_value`` from observed results."""
        self._prev_best = self._best_value
        for params, value in results:
            if value < self._best_value:
                self._best_value = value
                self._best_params = np.array(params, dtype=np.float64)

    def _optimize_step(
        self,
        results: list[tuple[list[float], float]],
    ) -> npt.NDArray[np.float64] | None:
        """Run one optimization step using scipy minimize.

        Returns the best point found, or None on failure.
        """
        if not results:
            return None

        def _objective(x: npt.NDArray[np.float64]) -> float:
            best_val = float("inf")
            for p, v in results:
                dist = float(np.sum((np.array(p) - x) ** 2))
                if dist < best_val:
                    best_val = v
            return best_val

        try:
            result = minimize(
                _objective,
                self._best_params if len(self._best_params) > 0 else np.array(results[0][0]),
                method="L-BFGS-B",
                bounds=self._bounds,
                options={"maxiter": 1},
            )
        except Exception as exc:
            log.warning("Calibration optimization step failed: %s", exc)
            return None

        return np.asarray(result.x, dtype=np.float64)

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract KPI values from history, update state, propose new samples.

        Reads the KPI files from the last generation, determines the
        best-so-far point, and proposes a new population.
        """
        if not history or not self._independent_vars:
            return []

        self._generation_count += 1

        results = _read_kpi_values(history, self._objective_kpi)
        if not results:
            log.warning("Calibration observe(): no KPI values found in history")
            return []

        self._update_best(results)

        dim = len(self._bounds)
        n_new = len(history[-1].get("samples", []))
        if n_new == 0:
            n_new = max(10, dim * 2)

        # Get a candidate best point from optimization.
        best_point = self._optimize_step(results)
        if best_point is None and len(self._best_params) == 0:
            return []

        # Propose new samples around the best known point.
        center = best_point if best_point is not None else self._best_params
        var_names = [v["name"] for v in self._independent_vars]
        new_samples = _propose_samples_around(center, n_new, self._bounds, var_names, width=0.1)

        self._proposed_samples = new_samples
        self._pending_proposed_samples = list(new_samples)

        log.info(
            "Calibration observe(): best_value=%.4f, proposed %d new samples (gen %d)",
            self._best_value,
            len(new_samples),
            self._generation_count,
        )
        return new_samples

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Check convergence based on metric improvement and max generations.

        Converges when:
        - Relative improvement is below tol, OR
        - Max generations reached
        """
        # Check max generations.
        if self._generation_count >= self._max_generations:
            log.info(
                "Calibration converged: max generations (%d) reached",
                self._max_generations,
            )
            return True

        # Check relative improvement.
        if self._prev_best == float("inf") or self._best_value == float("inf"):
            return False

        if self._prev_best == 0.0:
            return self._best_value == 0.0

        relative_change = abs(self._best_value - self._prev_best) / abs(self._prev_best)
        converged = relative_change < self._tol
        if converged:
            log.info(
                "Calibration converged: relative change %.6f < tol %.6f",
                relative_change,
                self._tol,
            )
        return converged

    def name(self) -> str:
        return "calibration"

    def is_iterative(self) -> bool:
        return True


class BM25CalibrationAlgorithm(CalibrationAlgorithm):
    """BM25-based calibration algorithm (issue #528).

    Uses BM25 scoring to measure similarity between simulated and
    measured energy time series. Lower BM25 score = better match.

    BM25 is particularly suited for calibration because:
    - It handles variable-length time series naturally
    - It applies sublinear term frequency saturation (reduces impact of outliers)
    - It includes document frequency normalization (handles different end-use magnitudes)
    """

    def __init__(
        self,
        calibration_data: Path | None = None,
        k1: float = 1.5,
        b: float = 0.75,
        tol: float = 1e-3,
        max_generations: int = 50,
    ) -> None:
        super().__init__(
            calibration_data=calibration_data,
            metric="bm25",
            tol=tol,
            max_generations=max_generations,
        )
        self._k1 = k1
        self._b = b

    def name(self) -> str:
        return "calibration"


class NMBECalibrationAlgorithm(CalibrationAlgorithm):
    """NMBE-based calibration algorithm (issue #528).

    Uses Normalized Mean Bias Error (NMBE) to measure the average bias
    between simulated and measured energy consumption.

    Target: |NMBE| ≤ 10% for ASHRAE 14 Tier 1 compliance.
    """

    def __init__(
        self,
        calibration_data: Path | None = None,
        tol: float = 1e-3,
        max_generations: int = 50,
    ) -> None:
        super().__init__(
            calibration_data=calibration_data,
            metric="nmbe",
            tol=tol,
            max_generations=max_generations,
        )

    def name(self) -> str:
        return "calibration"


class CVRMSECalibrationAlgorithm(CalibrationAlgorithm):
    """CVRMSE-based calibration algorithm (issue #528).

    Uses Coefficient of Variation of Root Mean Square Error (CVRMSE)
    to measure the precision of simulated vs measured energy consumption.

    Target: CVRMSE ≤ 30% for ASHRAE 14 Tier 1 compliance.
    """

    def __init__(
        self,
        calibration_data: Path | None = None,
        tol: float = 1e-3,
        max_generations: int = 50,
    ) -> None:
        super().__init__(
            calibration_data=calibration_data,
            metric="cvrmse",
            tol=tol,
            max_generations=max_generations,
        )

    def name(self) -> str:
        return "calibration"
