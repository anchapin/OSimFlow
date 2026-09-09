"""Sensitivity / UQ analysis steps for Campaign (issue #1542).

This module extracts the two post-KPI analysis step methods from
``osimflow.campaign`` (issue #1542, best-effort line-count
reduction): ``step_compute_sensitivity_indices`` (Sobol, issue
#346) and ``step_compute_uq_indices`` (issue #530).

Mixin pattern: :class:`CampaignAnalysisMixin` declares the narrow
attribute surface it needs (``cfg`` / ``trace`` / ``_obs`` /
``_check_cancel_requested``) as annotation-only class attributes;
``Campaign`` inherits the mixin so the historical method surface —
``campaign.step_compute_sensitivity_indices(...)`` etc. — is
unchanged and the data-driven dispatcher
(``getattr(self, step_info.method)``) still resolves the methods.

Issue #1542 rule: no import or type-reference of ``Campaign``.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

from ._campaign_observability import ObservabilityManager
from ._campaign_types import SampleSpec
from .algorithms import AlgorithmRegistry, parse_failure_threshold
from .config import CampaignConfig
from .monitoring import RunTrace

log = logging.getLogger("osimflow.campaign")


class CampaignAnalysisMixin:
    """Sobol / UQ analysis steps (issues #346, #530 → #1542 extraction)."""

    # Annotation-only attribute surface the step methods rely on.
    # Campaign provides all of these at runtime; declaring them here
    # keeps the mixin self-contained for mypy --strict without any
    # Campaign back-reference.
    cfg: CampaignConfig
    trace: RunTrace
    _obs: ObservabilityManager

    def _check_cancel_requested(self) -> bool:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def step_compute_sensitivity_indices(
        self,
        samples: list[SampleSpec],
        kpi_files: list[Path],
        variables: dict[str, Any],
        generation: int = 0,
    ) -> Path | None:
        """Compute Sobol sensitivity indices after KPI extraction.

        This step runs only when ``cfg.algorithm == "sobol"``. It reads
        the per-sample KPI values, passes them to
        :meth:`SobolAlgorithm.compute_sensitivity_indices`, and stores
        the resulting ``sensitivity_indices.json`` in the campaign
        output directory.

        The step is not cached — sensitivity index computation is cheap
        relative to simulation and the user may want to re-run with
        different KPI selections.
        """
        if self.cfg.algorithm != "sobol":
            return None

        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before COMPUTE_SENSITIVITY_INDICES")

        if not kpi_files:
            log.warning("COMPUTE_SENSITIVITY_INDICES: no KPI files — skipping")
            self.trace.step_finished(
                "COMPUTE_SENSITIVITY_INDICES",
                cache="SKIPPED",
                elapsed_s=0.0,
                exit_code=0,
            )
            return None

        # Build {sample_id: {kpi_name: value}} mapping from KPI files.
        kpi_values: dict[str, dict[str, float]] = {}
        for kpi_path in kpi_files:
            try:
                data = json.loads(kpi_path.read_text())
                sid = str(data.get("sample_id", kpi_path.stem.replace("kpi_", "")))
                kpis = data.get("kpis", {})
                numeric_kpis = {k: float(v) for k, v in kpis.items() if isinstance(v, (int, float))}
                kpi_values[sid] = numeric_kpis
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                log.warning("could not read KPI file %s: %s", kpi_path, exc, exc_info=True)

        algo = AlgorithmRegistry.get("sobol")
        indices_dir = self.cfg.outdir / "sensitivity"
        indices_dir.mkdir(parents=True, exist_ok=True)

        try:
            indices_path = algo.compute_sensitivity_indices(
                variables=variables,
                samples=samples,  # type: ignore[arg-type]
                kpi_values=kpi_values,
                outdir=indices_dir,
            )
        except Exception as exc:
            log.error(
                "COMPUTE_SENSITIVITY_INDICES failed: %s",
                exc,
                exc_info=True,
            )
            self.trace.step_finished(
                "COMPUTE_SENSITIVITY_INDICES",
                cache="MISS",
                elapsed_s=time.time() - t0,
                exit_code=1,
            )
            raise RuntimeError("compute_sensitivity_indices failed") from exc

        elapsed = time.time() - t0
        self.trace.step_finished(
            "COMPUTE_SENSITIVITY_INDICES",
            cache="MISS",
            elapsed_s=elapsed,
            exit_code=0,
        )
        self._obs.record_step_duration(
            "COMPUTE_SENSITIVITY_INDICES", elapsed, generation=generation
        )
        log.info("COMPUTE_SENSITIVITY_INDICES: wrote %s", indices_path)
        return indices_path

    def step_compute_uq_indices(
        self,
        samples: list[SampleSpec],
        kpi_files: list[Path],
        variables: dict[str, Any],
        generation: int = 0,
    ) -> Path | None:
        """Compute UQ indices (POF, CIs, distribution summaries) after KPI extraction.

        This step runs only when ``cfg.algorithm == "uq"``. It reads the
        per-sample KPI values, passes them to
        :meth:`UncertaintyQuantification.compute_uq_indices`, and stores
        the resulting ``uq_results.json`` in the campaign output directory.

        The step is not cached — UQ computation is cheap relative to
        simulation and the user may want to re-run with different thresholds.
        """
        if self.cfg.algorithm != "uq":
            return None

        t0 = time.time()

        if self._check_cancel_requested():
            raise KeyboardInterrupt("cancellation requested before COMPUTE_UQ_INDICES")

        if not kpi_files:
            log.warning("COMPUTE_UQ_INDICES: no KPI files — skipping")
            self.trace.step_finished(
                "COMPUTE_UQ_INDICES",
                cache="SKIPPED",
                elapsed_s=0.0,
                exit_code=0,
            )
            return None

        kpi_values: dict[str, dict[str, float]] = {}
        for kpi_path in kpi_files:
            try:
                data = json.loads(kpi_path.read_text())
                sid = str(data.get("sample_id", kpi_path.stem.replace("kpi_", "")))
                kpis = data.get("kpis", {})
                numeric_kpis = {k: float(v) for k, v in kpis.items() if isinstance(v, (int, float))}
                kpi_values[sid] = numeric_kpis
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                log.warning("could not read KPI file %s: %s", kpi_path, exc, exc_info=True)

        failure_thresholds: dict[str, tuple[float, str]] | None = None
        if self.cfg.uq_failure_thresholds:
            failure_thresholds = {}
            for raw in self.cfg.uq_failure_thresholds:
                try:
                    kpi_name, threshold = parse_failure_threshold(raw)
                    failure_thresholds[kpi_name] = (threshold, "greater")
                except ValueError as exc:
                    log.warning("invalid failure threshold %r: %s", raw, exc, exc_info=True)

        algo = AlgorithmRegistry.get("uq")
        uq_dir = self.cfg.outdir / "uq"
        uq_dir.mkdir(parents=True, exist_ok=True)

        try:
            uq_path = algo.compute_uq_indices(
                variables=variables,
                samples=samples,  # type: ignore[arg-type]
                kpi_values=kpi_values,
                outdir=uq_dir,
                failure_thresholds=failure_thresholds,
            )
        except Exception as exc:
            log.error(
                "COMPUTE_UQ_INDICES failed: %s",
                exc,
                exc_info=True,
            )
            self.trace.step_finished(
                "COMPUTE_UQ_INDICES",
                cache="MISS",
                elapsed_s=time.time() - t0,
                exit_code=1,
            )
            raise RuntimeError("compute_uq_indices failed") from exc

        elapsed = time.time() - t0
        self.trace.step_finished(
            "COMPUTE_UQ_INDICES",
            cache="MISS",
            elapsed_s=elapsed,
            exit_code=0,
        )
        self._obs.record_step_duration("COMPUTE_UQ_INDICES", elapsed, generation=generation)
        log.info("COMPUTE_UQ_INDICES: wrote %s", uq_path)
        return uq_path
