"""Generational optimization loop for Campaign (issue #1679).

This module extracts the three GA / NSGA-II / DE / SPEA2 generational
optimization methods from ``osimflow.campaign`` (issue #1679,
best-effort line-count reduction following the #1462 / #1542
extraction wave):

- :meth:`CampaignOptimizationMixin._run_one_generation` — the
  per-generation loop body: convergence check, ``algo.observe``,
  sample regeneration, the data-driven dispatcher, Sobol / UQ /
  Pareto post-KPI hooks, and the ``GenerationTrace`` record.  Issue
  #270's feedback loop and the issue #1392 data-driven dispatcher
  live here.
- :meth:`CampaignOptimizationMixin._extract_best_objective` —
  per-generation best-objective value (single-objective only) for
  ``run.json.generations[*].best_objective``.
- :meth:`CampaignOptimizationMixin._persist_pareto_front` —
  per-generation Pareto front persistence for multi-objective
  algorithms (issue #141).

Mixin pattern: :class:`CampaignOptimizationMixin` declares the
attribute surface it relies on (``cfg`` / ``trace`` /
``_latest_samples_file`` / ``_code_hash_with_byos`` / the step
methods it dispatches) as annotation-only class attributes.
``Campaign`` inherits the mixin so the historical method surface —
``campaign._run_one_generation(...)``,
``campaign._persist_pareto_front(...)``,
``campaign._extract_best_objective(...)`` — is unchanged and the
data-driven dispatcher (``getattr(self, step_info.method)``) still
resolves the methods.

Issue #1542 rule: no import or type-reference of ``Campaign``.
"""

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ._campaign_observability import ObservabilityManager
from ._campaign_types import SampleSpec
from .algorithms import BaseAlgorithm
from .config import CampaignConfig
from .monitoring import GenerationTrace, RunTrace
from .pareto import ParetoFront, ParetoSolution

log = logging.getLogger("osimflow.campaign")


# Type alias mirroring the campaign-level SampleDict (issue #1542:
# kept as an alias here so this module's annotations read the same
# as the campaign-level definition, no behavioural dependency).
SampleDict = dict[str, Path]


class CampaignOptimizationMixin:
    """Generational optimization loop body (issues #270, #141 → #1679)."""

    # Annotation-only attribute surface the methods rely on.  Campaign
    # provides all of these at runtime; declaring them here keeps the
    # mixin self-contained for mypy --strict without any Campaign
    # back-reference (issue #1542 rule).
    cfg: CampaignConfig
    trace: RunTrace
    _obs: ObservabilityManager
    _latest_samples_file: Path

    def _check_cancel_requested(self) -> bool:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _maybe_inject_chaos(self, step_name: str, when: str, target_id: str | None = None) -> None:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _verify_step_inputs(self, step_name: str) -> None:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _samples_manifest_path(self) -> Path:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _apply_sharding(self, samples: list[SampleSpec], *, generation: int) -> list[SampleSpec]:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _inject_dp_overrides(self, samples: list[SampleSpec]) -> list[SampleSpec]:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def _code_hash_with_byos(self, byos_key: str) -> str:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def step_generate_samples(
        self,
        algo: BaseAlgorithm,
        generation: int = 0,
    ) -> list[SampleSpec]:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def step_compute_sensitivity_indices(
        self,
        samples: list[SampleSpec],
        kpi_files: list[Path],
        variables: dict[str, Any],
        generation: int = 0,
    ) -> Path | None:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    def step_compute_uq_indices(
        self,
        samples: list[SampleSpec],
        kpi_files: list[Path],
        variables: dict[str, Any],
        generation: int = 0,
    ) -> Path | None:
        """Provided by :class:`osimflow.campaign.Campaign` at runtime."""
        raise NotImplementedError  # pragma: no cover — mixin contract

    # ------------------------------------------------------------------
    # Generational loop body (issue #270 feedback loop).
    # ------------------------------------------------------------------
    def _run_one_generation(  # noqa: PLR0912
        self,
        algo: BaseAlgorithm,
        history: list[dict[str, Any]],
        generation: int,
    ) -> tuple[list[SampleSpec], list[Path], SampleDict] | None:
        """Run one generation of the fan-out DAG.

        Returns ``(samples, kpi_files, simulated_dirs)``, or ``None`` if
        the algorithm has converged and the loop should stop.

        The feedback loop (issue #270):

        1. For generation > 0, check convergence. If converged, stop.
        2. Call ``algo.observe(history)`` — this reads KPI results from
           previous generations and updates the optimizer's internal
           state (best params, proposed samples, etc.).
        3. Call ``step_generate_samples(algo)`` — for iterative
           algorithms, ``algo.generate_samples()`` reads the internal
           state set by ``observe()`` and returns the proposed samples.
           For single-shot algorithms, it always returns LHS samples.
        4. Run the fan-out DAG: apply → simulate → extract KPIs.
        5. Record per-generation monitoring (issue #270).
        """
        gen_t0 = time.time()

        # Convergence check: after the first generation, ask the
        # algorithm whether we should continue.
        if generation > 0:
            if algo.is_converged(history):
                log.info(
                    "algorithm %s converged at generation %d; stopping loop",
                    algo.name(),
                    generation,
                )
                return None
            # observe() reads KPI history and updates optimizer state.
            # The returned samples are also stored in the explicit
            # _pending_proposed_samples slot for verifiable contract
            # (issue #332).
            new_samples = algo.observe(history)
            if new_samples:
                cast_samples(new_samples)
                # Verify observe() return matches the explicit slot
                # (issue #332). This catches bugs where an algorithm
                # sets internal state but fails to return.
                pending = getattr(algo, "_pending_proposed_samples", None)
                if pending is not None and pending != new_samples:
                    log.error(
                        "observe() return value does not match "
                        "_pending_proposed_samples for algorithm %s",
                        algo.name(),
                    )
            else:
                # verify there is actually something to reuse before continuing
                pending = getattr(algo, "_pending_proposed_samples", None)
                if not pending:
                    raise RuntimeError(
                        f"observe() returned empty samples at generation {generation} "
                        f"for algorithm {algo.name()!r} and no previous samples are "
                        "available; cannot continue iterative optimisation"
                    )
                log.warning(
                    "observe() returned empty samples at generation %d; reusing %d previous samples",
                    generation,
                    len(pending),
                )

        samples = self.step_generate_samples(algo, generation=generation)
        samples = self._inject_dp_overrides(samples)
        samples = self._apply_sharding(samples, generation=generation)
        samples_link = self._samples_manifest_path()
        samples_link.parent.mkdir(parents=True, exist_ok=True)
        from .json_utils import safe_json_dumps  # noqa: PLC0415 — circular-safe lazy import

        safe_json_dumps({"samples": samples}, samples_link, indent=2, raise_on_error=True)
        self._latest_samples_file = samples_link

        # Per-generation state namespace (issue #1392).  Each step's
        # ``inputs_signature`` callable reads from this and each step's
        # ``outputs_signature`` callable writes back into it.  ``samples``
        # is seeded here from the pre-loop ``step_generate_samples`` call;
        # ``parameterized``/``simulated``/``kpi_files``/``aggregated`` are
        # populated by their respective ``outputs_signature`` as the
        # dispatcher iterates.
        gen_state = SimpleNamespace(
            samples=samples,
            parameterized=None,
            simulated={},
            kpi_files=[],
            aggregated={},
        )

        # Dispatcher consults ``inputs_signature``/``outputs_signature``
        # instead of a hardcoded if/elif chain (issue #1392).  Each step
        # declares its own arg tuple via ``inputs_signature``; each
        # step captures its return value into ``gen_state`` via
        # ``outputs_signature``.  New steps just register their own
        # callables in ``_STEP_DEPENDENCIES`` — no dispatcher edit.
        #
        # A ``None`` ``inputs_signature`` means the step is configured
        # in the table for monitoring / configuration purposes but is
        # *not* dispatched by this loop (the legacy ``COMPUTE_*``
        # steps are invoked explicitly in the post-loop code below;
        # this preserves their pre-#1392 behaviour where the if/elif
        # chain did not invoke them but still ran the
        # before/after chaos hooks).
        from .campaign import _STEP_DEPENDENCIES  # noqa: PLC0415 — circular-safe lazy import

        for step_name, step_info in _STEP_DEPENDENCIES.items():
            if step_info.condition is not None and not step_info.condition(
                self, algo, generation=generation
            ):
                log.debug("step %s skipped (condition returned False)", step_name)
                continue

            self._verify_step_inputs(step_name)
            step_method = getattr(self, step_info.method, None)
            if step_method is None:
                log.warning(
                    "step method %r for %r not found; skipping", step_info.method, step_name
                )
                continue

            self._maybe_inject_chaos(step_name, "before_step")

            if step_info.inputs_signature is not None:
                args: tuple[Any, ...] = step_info.inputs_signature(
                    gen_state, self, algo, generation
                )
                result = step_method(*args)
                if step_info.outputs_signature is not None:
                    slot = step_info.outputs_signature(result)
                    if slot is not None:
                        slot_name, slot_value = slot
                        setattr(gen_state, slot_name, slot_value)
            else:
                log.debug(
                    "step %s has no inputs_signature; not invoked by dispatcher",
                    step_name,
                )

            self._maybe_inject_chaos(step_name, "after_step")
            log.debug("step %s completed", step_name)

        # Mirror the per-generation state back to local variables for the
        # post-loop code below (Sobol / UQ / Pareto / monitoring).
        samples = gen_state.samples
        simulated = gen_state.simulated
        kpi_files = gen_state.kpi_files

        # Sobol sensitivity indices (issue #346): compute after KPI extraction.
        if self.cfg.algorithm == "sobol":
            import yaml  # noqa: PLC0415 — defer to runtime

            variables: dict[str, Any] = {}
            if self.cfg.input_variables.exists():
                with self.cfg.input_variables.open() as fh:
                    raw = yaml.safe_load(fh)
                    if isinstance(raw, dict):
                        variables = raw
            self.step_compute_sensitivity_indices(
                samples, kpi_files, variables, generation=generation
            )

        # UQ analysis (issue #530): compute POF, CIs, and distribution summaries.
        if self.cfg.algorithm == "uq":
            import yaml  # noqa: PLC0415 — defer to runtime

            uq_variables: dict[str, Any] = {}
            if self.cfg.input_variables.exists():
                with self.cfg.input_variables.open() as fh:
                    raw = yaml.safe_load(fh)
                    if isinstance(raw, dict):
                        uq_variables = raw
            self.step_compute_uq_indices(samples, kpi_files, uq_variables, generation=generation)

        # Per-generation Pareto front persistence for multi-objective
        # algorithms (issue #141).  When the algorithm reports
        # is_multi_objective(), build ParetoSolution objects from the
        # extracted KPIs and persist the front to outdir/pareto/gen_N.json.
        if algo.is_multi_objective() and kpi_files:
            self._persist_pareto_front(algo, samples, kpi_files, generation)

        # Per-generation monitoring (issue #270).
        gen_elapsed = time.time() - gen_t0
        gen_samples = [s for s in self.trace.per_sample if s.generation == generation]
        n_succeeded = sum(1 for s in gen_samples if s.status == "ok")
        n_failed = sum(1 for s in gen_samples if s.status == "failed")
        best_objective = self._extract_best_objective(algo, kpi_files)
        self.trace.generation_done(
            GenerationTrace(
                generation=generation,
                n_samples=len(samples),
                n_succeeded=n_succeeded,
                n_failed=n_failed,
                converged=False,  # updated later if needed
                best_objective=best_objective,
                elapsed_s=round(gen_elapsed, 3),
            )
        )
        log.info(
            "generation %d complete: %d samples (%d ok, %d failed) in %.1fs",
            generation,
            len(samples),
            n_succeeded,
            n_failed,
            gen_elapsed,
        )

        return samples, kpi_files, simulated

    # ------------------------------------------------------------------
    # Best-objective extraction (issue #270).
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_best_objective(
        algo: BaseAlgorithm,
        kpi_files: list[Path],
    ) -> float | None:
        """Extract the best objective value from KPI files.

        For single-objective algorithms (DE, DA, PSO), reads the primary
        KPI. For multi-objective (NSGA-II), returns None (use Pareto front
        instead). The objective name is inferred from the algorithm's
        default (``eui`` for DE/DA/PSO).
        """
        if algo.is_multi_objective():
            return None
        if not kpi_files:
            return None
        best: float | None = None
        for kpi_path in kpi_files:
            try:
                data = json.loads(kpi_path.read_text())
                kpis = data.get("kpis", {})
                # Default objective is "eui" — matches DE/DA/PSO defaults.
                val = kpis.get("eui")
                if (
                    val is not None
                    and isinstance(val, (int, float))
                    and (best is None or float(val) < best)
                ):
                    best = float(val)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
        return best

    # ------------------------------------------------------------------
    # Pareto front persistence (issue #141).
    # ------------------------------------------------------------------
    def _persist_pareto_front(
        self,
        algo: BaseAlgorithm,
        samples: list[SampleSpec],
        kpi_files: list[Path],
        generation: int,
    ) -> None:
        """Build/update the Pareto front and persist per-generation JSON.

        Parameters
        ----------
        algo
            The algorithm instance (must report ``is_multi_objective()``).
        samples
            The sample specs for this generation.
        kpi_files
            Extracted KPI JSON files (one per sample).
        generation
            0-based generation index — used in the output filename.
        """
        # Load existing front (if any) from the previous generation.
        pareto_dir = self.cfg.outdir / "pareto"
        pareto_path = pareto_dir / f"gen_{generation}.json"
        front: ParetoFront | None = None

        # Try to load from previous generation's file to carry forward
        # non-dominated solutions.
        if generation > 0:
            prev_path = pareto_dir / f"gen_{generation - 1}.json"
            if prev_path.exists():
                try:
                    front = ParetoFront.load(prev_path)
                except Exception as exc:
                    log.warning("could not load previous Pareto front: %s", exc, exc_info=True)

        # Determine objective names from the first KPI file that has data.
        objective_names: list[str] = []
        for kpi_path in kpi_files:
            try:
                kpi_data = json.loads(kpi_path.read_text())
                kpis = kpi_data.get("kpis", {})
                objective_names = sorted(k for k, v in kpis.items() if isinstance(v, (int, float)))
                if objective_names:
                    break
            except Exception:
                continue

        if not objective_names:
            log.warning("no objective KPIs found; skipping Pareto front")
            return

        if front is None:
            front = ParetoFront(objective_names=objective_names)

        # Build ParetoSolution objects from samples + KPIs.
        # Match by index (samples[i] -> kpi_files[i]) — this is the
        # same correspondence the Campaign uses throughout.
        new_solutions: list[ParetoSolution] = []
        for i, sample in enumerate(samples):
            if i >= len(kpi_files):
                break
            try:
                kpi_data = json.loads(kpi_files[i].read_text())
                kpis = kpi_data.get("kpis", {})
                objectives = {k: float(v) for k, v in kpis.items() if isinstance(v, (int, float))}
                parameters = {
                    k: float(v) for k, v in sample["values"].items() if isinstance(v, (int, float))
                }
                new_solutions.append(
                    ParetoSolution(
                        sample_id=str(sample["sample_id"]),
                        objectives=objectives,
                        parameters=parameters,
                        generation=generation,
                    )
                )
            except Exception as exc:
                log.warning(
                    "could not build ParetoSolution for sample %s: %s",
                    sample.get("sample_id"),
                    exc,
                    exc_info=True,
                )

        if new_solutions:
            front.add_generation(new_solutions)
            front.save(pareto_path)


# ---------------------------------------------------------------------------
# cast_samples — same canonical narrowing helper as ``osimflow.campaign``.
# Imported lazily above to avoid a circular import; the runtime resolution
# always lands on the canonical version defined in ``osimflow.campaign``.
# ---------------------------------------------------------------------------
def cast_samples(obj: object) -> list[SampleSpec]:
    """Narrow a ``samples`` JSON value to the canonical SampleSpec list.

    Mirrors :func:`osimflow.campaign.cast_samples` exactly.  Defined
    locally here so the optimizer loop is self-contained for tests
    even when imported without ``osimflow.campaign`` resolving first
    (the import at the top of ``_run_one_generation`` is lazy for
    that reason).
    """
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
        out.append(SampleSpec(sample_id=sid, values=values))
    return out
