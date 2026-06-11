"""Pareto front model for multi-objective optimisation (issue #141, G5a).

Multi-objective algorithms (e.g. NSGA-II) produce a Pareto front — the
set of *non-dominated* solutions where no single solution is strictly
better than another across all objectives.  This module provides:

- :class:`ParetoSolution` — a single point on the front.
- :class:`ParetoFront` — tracks non-dominated solutions across
  generations, with JSON persistence.

Non-dominated sorting uses pairwise comparison (O(n²) in the number of
solutions) which is sufficient for the typical front size of
energy-model campaigns (tens to low hundreds of points).

The Campaign wires into :class:`ParetoFront` after each generation's
KPI extraction when the algorithm reports ``is_multi_objective()`` as
``True``, persisting to ``outdir/pareto/gen_N.json``.
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.pareto")


@dataclass
class ParetoSolution:
    """A single point on the Pareto front."""

    sample_id: str
    objectives: dict[str, float]  # kpi_name -> value
    parameters: dict[str, float]  # variable_name -> value
    generation: int


class ParetoFront:
    """Tracks non-dominated solutions across generations.

    Parameters
    ----------
    objective_names
        Ordered list of KPI names used as objectives.
    maximize
        Per-objective flag: ``True`` means higher is better,
        ``False`` (default) means lower is better.  Length must match
        *objective_names*.
    """

    def __init__(
        self,
        objective_names: list[str],
        maximize: list[bool] | None = None,
    ) -> None:
        self._objective_names = objective_names
        self._maximize = maximize or [False] * len(objective_names)
        if len(self._maximize) != len(self._objective_names):
            raise ValueError(
                f"maximize length ({len(self._maximize)}) must match "
                f"objective_names length ({len(self._objective_names)})"
            )
        self._solutions: list[ParetoSolution] = []

    # ------------------------------------------------------------------
    # Core non-dominated sorting
    # ------------------------------------------------------------------

    def _dominates(self, a: ParetoSolution, b: ParetoSolution) -> bool:
        """Return ``True`` if *a* dominates *b*.

        *a* dominates *b* when *a* is at least as good as *b* on every
        objective and strictly better on at least one.  The sense
        (minimise / maximise) is determined by ``self._maximize``.
        """
        at_least_as_good = True
        strictly_better = False
        for name, should_max in zip(self._objective_names, self._maximize, strict=True):
            a_val = a.objectives.get(name)
            b_val = b.objectives.get(name)
            # Missing objectives break domination.
            if a_val is None or b_val is None:
                return False
            if should_max:
                if a_val < b_val:
                    at_least_as_good = False
                    break
                if a_val > b_val:
                    strictly_better = True
            else:
                if a_val > b_val:
                    at_least_as_good = False
                    break
                if a_val < b_val:
                    strictly_better = True
        return at_least_as_good and strictly_better

    def _get_nondominated(self) -> list[ParetoSolution]:
        """Return non-dominated solutions using pairwise comparison.

        A solution survives if no other solution dominates it.
        """
        n = len(self._solutions)
        if n <= 1:
            return list(self._solutions)

        dominated: set[int] = set()
        for i in range(n):
            if i in dominated:
                continue
            for j in range(n):
                if i == j or j in dominated:
                    continue
                if self._dominates(self._solutions[j], self._solutions[i]):
                    dominated.add(i)
                    break
        return [s for idx, s in enumerate(self._solutions) if idx not in dominated]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_generation(self, solutions: list[ParetoSolution]) -> None:
        """Add solutions from a generation and recompute non-dominated set."""
        self._solutions.extend(solutions)
        self._solutions = self._get_nondominated()

    def get_nondominated(self) -> list[ParetoSolution]:
        """Return the current non-dominated solution set."""
        return list(self._solutions)

    @property
    def objective_names(self) -> list[str]:
        return list(self._objective_names)

    @property
    def maximize(self) -> list[bool]:
        return list(self._maximize)

    def __len__(self) -> int:
        return len(self._solutions)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON persistence."""
        return {
            "objective_names": self._objective_names,
            "maximize": self._maximize,
            "solutions": [asdict(s) for s in self._solutions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParetoFront":
        """Deserialize from dict."""
        obj_names: list[str] = data.get("objective_names", [])
        max_flags: list[bool] = data.get("maximize", [False] * len(obj_names))
        front = cls(objective_names=obj_names, maximize=max_flags)
        raw_solutions: list[dict[str, Any]] = data.get("solutions", [])
        front._solutions = [
            ParetoSolution(
                sample_id=s["sample_id"],
                objectives=s["objectives"],
                parameters=s["parameters"],
                generation=s["generation"],
            )
            for s in raw_solutions
        ]
        return front

    def save(self, path: Path) -> None:
        """Save to JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        log.info("Pareto front saved to %s (%d solutions)", path, len(self._solutions))

    @classmethod
    def load(cls, path: Path) -> "ParetoFront":
        """Load from JSON file."""
        data = json.loads(path.read_text())
        front = cls.from_dict(data)
        log.info("Pareto front loaded from %s (%d solutions)", path, len(front))
        return front
