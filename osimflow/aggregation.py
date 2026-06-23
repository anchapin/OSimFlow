"""Terminal aggregation: compile per-sample results into campaign CSVs (issue #627).

This module is the pure-Python, storage-agnostic core of the Coordinator's
**S3 Aggregator Task** (Epic #624).  On array-job completion, the Coordinator
lists every ``_manifest.json`` produced by the workers
(see :mod:`osimflow.manifest`), reads each referenced ``kpis.json``, and
compiles two CSVs that match the **column contract of the local
``bin/aggregate_results.py`` path**):

* ``aggregated_results.csv`` — one row per *successful* sample, ``sample_id``
  column first followed by every KPI spread from the sample's ``kpis.json``.
  Failed samples are intentionally excluded (matching the local path, where a
  missing ``kpis.json`` is silently skipped) — they surface in
  ``failed_simulations.csv`` instead.

* ``failed_simulations.csv`` — one row per *failed* sample, with the columns::

      sample_id, failure_category, root_cause_line, total_severe_errors,
      error_summary, exit_code, log_path, diagnosis_suggestion

  ``error_summary`` carries exactly the first ``  * Severe`` line from
  ``eplusout.err`` (the ``grep -m 1 "  * Severe"`` pattern).  The
  manifest already captured this line (``first_severe_error`` field) at worker
  time, so the aggregator does **not** download ``.err`` files.

When ``--algorithm`` is multi-objective (:data:`_MULTI_OBJECTIVE_ALGORITHMS`),
a Pareto front JSON is also produced by reusing :class:`osimflow.pareto.ParetoFront`.

Robustness contract (issue #627 criterion #5): a manifest that claims
``status="ok"`` but whose ``kpis.json`` cannot be fetched is **logged with
``exc_info=True`` and counted as failed** — it never crashes the aggregation.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from osimflow._work_scripts.aggregate_results import (
    CATEGORY_SUGGESTIONS,
    _classify_line,
)
from osimflow.pareto import ParetoFront, ParetoSolution

log = logging.getLogger("osimflow.aggregation")

#: Algorithm names that require multi-objective Pareto-front output
#: (criterion #4).  Lower-cased for case-insensitive comparison.
_MULTI_OBJECTIVE_ALGORITHMS: frozenset[str] = frozenset({"nsga2", "pso"})

#: Manifest ``status`` values that mean "the sample believes it succeeded".
#: The worker (:func:`osimflow.work.publish_kpi_results`) emits ``"completed"``;
#: the issue spec uses ``"ok"``.  Both are accepted so the aggregator is robust
#: to either vocabulary.
_OK_STATUSES: frozenset[str] = frozenset({"ok", "completed", "success", "succeeded"})

#: The canonical column order of ``failed_simulations.csv`` — identical to the
#: local ``bin/aggregate_results.py`` output (``extract_failure`` + the
#: ``cols`` list in ``main``).  Exported so tests can assert against it.
FAILED_SIMULATIONS_COLUMNS: tuple[str, ...] = (
    "sample_id",
    "failure_category",
    "root_cause_line",
    "total_severe_errors",
    "error_summary",
    "exit_code",
    "log_path",
    "diagnosis_suggestion",
)

#: Sentinel error summary for the criterion-#5 robustness path: a manifest that
#: claimed success but whose ``kpis.json`` is unrecoverable.  Surfaced verbatim
#: in ``failed_simulations.csv``'s ``error_summary`` column so an operator can
#: grep for the exact inconsistency.
_MISSING_KPIS_ERROR_SUMMARY = (
    "kpis.json missing (manifest claimed status=ok but no KPIs retrievable)"
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AggregatedManifest:
    """A parsed ``_manifest.json`` entry ready for aggregation.

    Mirrors the §3.1 schema emitted by :func:`osimflow.manifest.build_manifest`.
    Kept as a small dedicated type (rather than reusing the raw dict) so the
    aggregation core has typed fields and is independent of the manifest
    module's storage/atomic-write concerns.
    """

    sample_id: str
    index: int
    status: str
    kpis_key: str | None
    exit_code: int
    first_severe_error: str | None
    finished_at: float | None


@dataclass(frozen=True)
class AggregationResult:
    """Output of :func:`compile_aggregation`.

    The two CSV payloads are returned as *strings* (not file paths) so the
    caller — the Coordinator endpoint — can stream them to any
    :class:`~osimflow.storage.ResultStorage` backend via its path-based
    ``upload_file``.  ``pareto_json`` is ``None`` unless the algorithm is
    multi-objective.
    """

    aggregated_results_csv: str
    failed_simulations_csv: str
    pareto_json: str | None
    ok_count: int
    failed_count: int
    total_count: int
    #: Sample IDs whose ``status="ok"`` claim could not be honoured because the
    #: referenced ``kpis.json`` was missing/unreadable (criterion #5).  Surfaced
    #: so the endpoint can log them explicitly without re-deriving the set.
    degraded_ok_samples: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def parse_manifest(raw: dict[str, Any]) -> AggregatedManifest:
    """Parse a raw manifest dict into :class:`AggregatedManifest`.

    Tolerant of the schema drift between the issue spec (``status="ok"``) and
    the worker (``status="completed"``): both round-trip into ``status``.  A
    missing ``index``/``exit_code`` defaults to ``0``; a non-numeric
    ``finished_at`` (e.g. an ISO-8601 string) is coerced to ``None`` rather
    than crashing — the field is metadata only and does not feed the CSVs.
    """
    finished = raw.get("finished_at")
    finished_f = float(finished) if isinstance(finished, (int, float)) else None

    return AggregatedManifest(
        sample_id=str(raw.get("sample_id", "")),
        index=int(raw.get("index", 0) or 0),
        status=str(raw.get("status", "failed")),
        kpis_key=raw.get("kpis_key"),
        exit_code=int(raw.get("exit_code", 0) or 0),
        first_severe_error=raw.get("first_severe_error"),
        finished_at=finished_f,
    )


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ClassifiedSample:
    """Per-manifest classification: either an OK row or a failure row.

    Exactly one of ``ok_row``/``failure_row`` is non-None.  ``degraded`` is
    ``True`` only for the criterion-#5 path (ok manifest + unrecoverable
    kpis.json).  ``pareto`` carries the Pareto solution when the algorithm is
    multi-objective and the sample has usable numeric objectives.
    """

    ok_row: dict[str, Any] | None
    failure_row: dict[str, Any] | None
    degraded: bool
    pareto: ParetoSolution | None


def _classify_sample(
    m: AggregatedManifest,
    kpi_fetcher: Callable[[str], dict[str, Any] | None],
    *,
    is_multi_objective: bool,
    kpi_objectives: dict[str, str] | None,
    objective_names: list[str] | None,
) -> _ClassifiedSample:
    """Classify one manifest into an OK or failure contribution.

    Encapsulates the criterion-#5 robustness contract: an ok manifest whose
    ``kpis.json`` is absent/unreadable is downgraded to a failure with a clear
    error summary (never raised).
    """
    claimed_ok = m.status.lower() in _OK_STATUSES
    kpis: dict[str, Any] | None = None
    if claimed_ok and m.kpis_key:
        try:
            payload = kpi_fetcher(m.kpis_key)
        except Exception:
            log.warning(
                "compile_aggregation: kpi_fetcher raised for sample %s "
                "(key=%s) — counting as failed",
                m.sample_id,
                m.kpis_key,
                exc_info=True,
            )
            payload = None
        if payload is not None:
            kpis = _extract_kpis_dict(payload)

    if not (claimed_ok and kpis):
        return _ClassifiedSample(
            ok_row=None,
            failure_row=_build_failure_row(m, claimed_ok, kpis is None),
            degraded=bool(claimed_ok and kpis is None),
            pareto=None,
        )

    row: dict[str, Any] = {"sample_id": m.sample_id}
    row.update(kpis)
    pareto: ParetoSolution | None = None
    if is_multi_objective:
        pareto = _to_pareto_solution(m.sample_id, kpis, kpi_objectives, objective_names)
    return _ClassifiedSample(ok_row=row, failure_row=None, degraded=False, pareto=pareto)


def compile_aggregation(
    manifests: list[AggregatedManifest],
    kpi_fetcher: Callable[[str], dict[str, Any] | None],
    *,
    algorithm: str | None = None,
    kpi_objectives: dict[str, str] | None = None,
) -> AggregationResult:
    """Compile the terminal campaign artifacts from parsed manifests.

    This is the storage-agnostic core of the S3 Aggregator Task.  Given the
    parsed manifests and a callable that fetches a ``kpis.json`` payload by its
    remote key (returning ``None`` when the object is absent/unreadable), it
    produces the two campaign CSVs and an optional Pareto front.

    Column contract
    ---------------
    ``aggregated_results.csv`` matches the local
    ``bin/aggregate_results.py`` output: a ``sample_id`` column followed by
    every KPI key spread from the sample's ``kpis.json`` (the ``kpis`` sub-dict).
    Rows are ordered by sample ``index`` then ``sample_id``.  Failed samples
    are excluded (mirroring the local path where a missing ``kpis.json`` is
    silently dropped from the aggregate).

    ``failed_simulations.csv`` carries :data:`FAILED_SIMULATIONS_COLUMNS`,
    with ``error_summary`` = the manifest's ``first_severe_error`` (the first
    ``  * Severe`` line).  ``failure_category`` /
    ``diagnosis_suggestion`` reuse the local classifier so the two paths
    classify identically.

    Parameters
    ----------
    manifests
        Parsed ``_manifest.json`` entries (any order; they are sorted here).
    kpi_fetcher
        Callable returning the parsed ``kpis.json`` dict for a given
        ``kpis_key`` (or ``None`` when the object cannot be fetched/parsed).
        The Coordinator endpoint wires this to
        :meth:`~osimflow.storage.ResultStorage.download_file` + ``json.loads``.
    algorithm
        Sampling/optimisation algorithm name.  When lower-cased into
        :data:`_MULTI_OBJECTIVE_ALGORITHMS` a Pareto front JSON is produced.
    kpi_objectives
        Optional ``{kpi_name: "minimize" | "maximize"}`` map for the Pareto
        front.  When ``None`` with a multi-objective algorithm, every KPI
        present in the first successful sample is treated as a minimization
        objective.

    Returns
    -------
    AggregationResult
        The two CSV payloads (+ optional Pareto JSON) and success/failure
        counts.  Never raises on missing ``kpis.json`` (criterion #5).
    """
    # Sort for deterministic output: index, then sample_id (matches the spirit
    # of the local path which preserves kpi-file argument order; sorting makes
    # the distributed result reproducible regardless of list_objects ordering).
    ordered = sorted(manifests, key=lambda m: (m.index, m.sample_id))
    is_multi_objective = algorithm is not None and algorithm.lower() in _MULTI_OBJECTIVE_ALGORITHMS

    ok_rows: list[dict[str, Any]] = []
    ok_solutions: list[ParetoSolution] = []
    failure_rows: list[dict[str, Any]] = []
    degraded_ok: list[str] = []
    # Objective-name set is derived lazily from the first OK sample when
    # kpi_objectives is None, so every subsequent sample is scored against the
    # same axis set.
    objective_names: list[str] | None = list(kpi_objectives.keys()) if kpi_objectives else None

    for m in ordered:
        classified = _classify_sample(
            m,
            kpi_fetcher,
            is_multi_objective=is_multi_objective,
            kpi_objectives=kpi_objectives,
            objective_names=objective_names,
        )
        if classified.degraded:
            degraded_ok.append(m.sample_id)
        if classified.ok_row is not None:
            ok_rows.append(classified.ok_row)
            if objective_names is None and is_multi_objective:
                # First successful sample seeds the objective axis set.
                objective_names = [
                    k
                    for k, v in classified.ok_row.items()
                    if k != "sample_id" and isinstance(v, (int, float))
                ]
        elif classified.failure_row is not None:
            failure_rows.append(classified.failure_row)
        if classified.pareto is not None:
            ok_solutions.append(classified.pareto)

    aggregated_results_csv = _render_ok_csv(ok_rows)
    failed_simulations_csv = _render_failure_csv(failure_rows)
    pareto_json = (
        _compute_pareto_front(ok_solutions, kpi_objectives) if is_multi_objective else None
    )

    return AggregationResult(
        aggregated_results_csv=aggregated_results_csv,
        failed_simulations_csv=failed_simulations_csv,
        pareto_json=pareto_json,
        ok_count=len(ok_rows),
        failed_count=len(failure_rows),
        total_count=len(ordered),
        degraded_ok_samples=degraded_ok,
    )


def _render_ok_csv(ok_rows: list[dict[str, Any]]) -> str:
    """Render ``aggregated_results.csv`` with ``sample_id`` as the lead column."""
    if not ok_rows:
        # Match the local path: header-only file with just sample_id.
        return "sample_id\n"
    agg_df = pd.DataFrame(ok_rows)
    cols = ["sample_id"] + [c for c in agg_df.columns if c != "sample_id"]
    agg_df = agg_df[cols]
    return str(agg_df.to_csv(index=False))


def _render_failure_csv(failure_rows: list[dict[str, Any]]) -> str:
    """Render ``failed_simulations.csv`` with the canonical column order."""
    if not failure_rows:
        # Header-only CSV — identical to the local path's empty-failures branch.
        return ",".join(FAILED_SIMULATIONS_COLUMNS) + "\n"
    fail_df = pd.DataFrame(failure_rows)
    for c in FAILED_SIMULATIONS_COLUMNS:
        if c not in fail_df.columns:
            fail_df[c] = None
    fail_df = fail_df[list(FAILED_SIMULATIONS_COLUMNS)]
    return str(fail_df.to_csv(index=False))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_kpis_dict(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the KPI sub-dict out of a parsed ``kpis.json`` payload.

    The canonical worker format is ``{"sample_id": ..., "kpis": {...}}``.  If
    the ``kpis`` key is absent or empty, fall back to the top-level dict minus
    a small set of reserved metadata keys (so a flat ``{"eui": 1.0}`` payload
    still aggregates).  Returns ``None`` when no usable KPIs remain.
    """
    kpis = payload.get("kpis")
    if isinstance(kpis, dict) and kpis:
        return dict(kpis)
    reserved = {"sample_id", "openstudio_version", "quality", "index", "campaign_id"}
    flat = {k: v for k, v in payload.items() if k not in reserved}
    return flat or None


def _build_failure_row(
    m: AggregatedManifest, claimed_ok: bool, kpis_missing: bool
) -> dict[str, Any]:
    """Build a single ``failed_simulations.csv`` row from a failed manifest.

    Reuses the local classifier (:func:`._classify_line` +
    :data:`CATEGORY_SUGGESTIONS`) so failure categories are consistent between
    the local and Coordinator paths.
    """
    if claimed_ok and kpis_missing:
        # Criterion #5: ok manifest whose kpis.json is unrecoverable.
        error_summary = _MISSING_KPIS_ERROR_SUMMARY
        category = "generic_severe"
    else:
        error_summary = m.first_severe_error or "no severe error recorded"
        category = _classify_line(error_summary)
    suggestion = CATEGORY_SUGGESTIONS.get(category, CATEGORY_SUGGESTIONS["generic_severe"])
    # The manifest captures only the *first* Severe line, so the best available
    # count is 1 when one exists and 0 otherwise.  We deliberately do not
    # download ``.err`` files (criterion #3); ``total_severe_errors`` therefore
    # reflects the manifest, not the full error log.
    total_severe = 1 if m.first_severe_error else 0
    return {
        "sample_id": m.sample_id,
        "failure_category": category,
        "root_cause_line": error_summary,
        "total_severe_errors": total_severe,
        "error_summary": error_summary,
        "exit_code": m.exit_code,
        "log_path": "",
        "diagnosis_suggestion": suggestion,
    }


def _to_pareto_solution(
    sample_id: str,
    kpis: dict[str, Any],
    kpi_objectives: dict[str, str] | None,
    objective_names: list[str] | None,
) -> ParetoSolution | None:
    """Best-effort conversion of a sample's KPIs into a :class:`ParetoSolution`.

    Objective names come from *kpi_objectives* when set, otherwise from the
    caller-derived *objective_names* (the first OK sample's numeric keys).
    When both are ``None`` the sample's own numeric keys are used.  Non-numeric
    objective values cause the sample to be skipped (logged at debug) — the
    Pareto front still computes over the rest.
    """
    if kpi_objectives:
        names = list(kpi_objectives.keys())
    elif objective_names is not None:
        names = objective_names
    else:
        names = [k for k, v in kpis.items() if isinstance(v, (int, float))]
    objectives: dict[str, float] = {}
    for name in names:
        val = kpis.get(name)
        if isinstance(val, (int, float)):
            objectives[name] = float(val)
    if not objectives:
        log.debug("Pareto: sample %s has no numeric objectives — skipping", sample_id)
        return None
    return ParetoSolution(
        sample_id=sample_id,
        objectives=objectives,
        parameters={},
        generation=0,
    )


def _compute_pareto_front(
    solutions: list[ParetoSolution],
    kpi_objectives: dict[str, str] | None,
) -> str | None:
    """Compute + serialize the non-dominated front, or ``None`` if empty.

    Objective direction comes from ``kpi_objectives`` (``"maximize"`` →
    maximize, anything else → minimize).  When ``kpi_objectives`` is ``None``
    every objective is minimized (the :class:`ParetoFront` default).
    """
    if not solutions:
        return None
    # Objective set = the union of objective names present across solutions,
    # ordered by first appearance so the JSON is stable.
    seen: dict[str, None] = {}
    for sol in solutions:
        for name in sol.objectives:
            seen.setdefault(name, None)
    names = list(seen.keys())
    maximize = [
        (kpi_objectives.get(name, "minimize").lower() == "maximize") if kpi_objectives else False
        for name in names
    ]
    front = ParetoFront(objective_names=names, maximize=maximize)
    front.add_generation(solutions)
    if len(front) == 0:
        return None
    return json.dumps(front.to_dict(), indent=2)


__all__ = [
    "FAILED_SIMULATIONS_COLUMNS",
    "AggregatedManifest",
    "AggregationResult",
    "compile_aggregation",
    "parse_manifest",
]
