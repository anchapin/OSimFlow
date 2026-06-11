"""Tests for osimflow.pareto — Pareto front model (issue #141, G5a).

Covers:
- Non-dominated identification (2D and 3D objectives)
- Maximization objective handling
- to_dict / from_dict round-trip
- save / load round-trip with temp file
- Per-generation persistence
- Empty front validity
- Single solution is non-dominated
"""

import json
from pathlib import Path

import pytest

from osimflow.pareto import ParetoFront, ParetoSolution  # noqa: E402

# ======================================================================
# Helpers
# ======================================================================


def _make_solution(
    sid: str,
    objectives: dict[str, float],
    params: dict[str, float] | None = None,
    generation: int = 0,
) -> ParetoSolution:
    return ParetoSolution(
        sample_id=sid,
        objectives=objectives,
        parameters=params or {"x": float(sid.strip("s"))},
        generation=generation,
    )


# ======================================================================
# Tests
# ======================================================================


class TestNonDominated2D:
    """Correctly identifies non-dominated points in 2D."""

    def test_simple_front(self) -> None:
        """Three points: two on the front, one dominated."""
        front = ParetoFront(objective_names=["eui", "cost"])
        solutions = [
            _make_solution("s1", {"eui": 100.0, "cost": 50.0}),
            _make_solution("s2", {"eui": 120.0, "cost": 30.0}),
            _make_solution("s3", {"eui": 110.0, "cost": 60.0}),  # dominated by s1
        ]
        front.add_generation(solutions)
        nd = front.get_nondominated()
        ids = {s.sample_id for s in nd}
        assert ids == {"s1", "s2"}

    def test_all_nondominated(self) -> None:
        """Points that trade off in different directions."""
        front = ParetoFront(objective_names=["a", "b"])
        solutions = [
            _make_solution("s1", {"a": 1.0, "b": 10.0}),
            _make_solution("s2", {"a": 2.0, "b": 5.0}),
            _make_solution("s3", {"a": 5.0, "b": 2.0}),
            _make_solution("s4", {"a": 10.0, "b": 1.0}),
        ]
        front.add_generation(solutions)
        assert len(front) == 4

    def test_all_dominated_except_one(self) -> None:
        """Single best point dominates all others."""
        front = ParetoFront(objective_names=["x", "y"])
        solutions = [
            _make_solution("s1", {"x": 1.0, "y": 1.0}),
            _make_solution("s2", {"x": 2.0, "y": 2.0}),
            _make_solution("s3", {"x": 3.0, "y": 3.0}),
        ]
        front.add_generation(solutions)
        ids = {s.sample_id for s in front.get_nondominated()}
        assert ids == {"s1"}


class TestNonDominated3D:
    """3D objectives work."""

    def test_3d_front(self) -> None:
        front = ParetoFront(objective_names=["eui", "cost", "carbon"])
        solutions = [
            _make_solution("s1", {"eui": 80.0, "cost": 60.0, "carbon": 50.0}),
            _make_solution("s2", {"eui": 90.0, "cost": 40.0, "carbon": 45.0}),
            _make_solution("s3", {"eui": 100.0, "cost": 30.0, "carbon": 40.0}),
            _make_solution("s4", {"eui": 95.0, "cost": 55.0, "carbon": 60.0}),  # dominated
        ]
        front.add_generation(solutions)
        ids = {s.sample_id for s in front.get_nondominated()}
        assert "s4" not in ids
        assert "s1" in ids
        assert "s2" in ids
        assert "s3" in ids

    def test_3d_all_nondominated(self) -> None:
        """Points that each dominate on one axis."""
        front = ParetoFront(objective_names=["a", "b", "c"])
        solutions = [
            _make_solution("s1", {"a": 1.0, "b": 10.0, "c": 10.0}),
            _make_solution("s2", {"a": 10.0, "b": 1.0, "c": 10.0}),
            _make_solution("s3", {"a": 10.0, "b": 10.0, "c": 1.0}),
        ]
        front.add_generation(solutions)
        assert len(front) == 3


class TestMaximization:
    """Maximization objectives handled correctly."""

    def test_maximize_both(self) -> None:
        """Higher is better for both objectives."""
        front = ParetoFront(objective_names=["efficiency", "comfort"], maximize=[True, True])
        solutions = [
            _make_solution("s1", {"efficiency": 0.9, "comfort": 0.8}),
            _make_solution("s2", {"efficiency": 0.7, "comfort": 0.95}),
            _make_solution("s3", {"efficiency": 0.5, "comfort": 0.5}),  # dominated
        ]
        front.add_generation(solutions)
        ids = {s.sample_id for s in front.get_nondominated()}
        assert ids == {"s1", "s2"}

    def test_mixed_min_max(self) -> None:
        """Minimise EUI, maximise comfort."""
        front = ParetoFront(objective_names=["eui", "comfort"], maximize=[False, True])
        solutions = [
            _make_solution("s1", {"eui": 80.0, "comfort": 0.7}),
            _make_solution("s2", {"eui": 100.0, "comfort": 0.9}),
            _make_solution("s3", {"eui": 90.0, "comfort": 0.6}),  # dominated by s1
        ]
        front.add_generation(solutions)
        ids = {s.sample_id for s in front.get_nondominated()}
        assert ids == {"s1", "s2"}

    def test_maximize_single_dominates(self) -> None:
        """Single best point when maximising."""
        front = ParetoFront(objective_names=["score"], maximize=[True])
        solutions = [
            _make_solution("s1", {"score": 95.0}),
            _make_solution("s2", {"score": 80.0}),
            _make_solution("s3", {"score": 70.0}),
        ]
        front.add_generation(solutions)
        assert len(front) == 1
        assert front.get_nondominated()[0].sample_id == "s1"


class TestRoundTrip:
    """to_dict / from_dict round-trip."""

    def test_dict_roundtrip(self) -> None:
        front = ParetoFront(objective_names=["eui", "cost"], maximize=[False, True])
        solutions = [
            _make_solution("s1", {"eui": 100.0, "cost": 50.0}),
            _make_solution("s2", {"eui": 120.0, "cost": 30.0}),
        ]
        front.add_generation(solutions)

        d = front.to_dict()
        restored = ParetoFront.from_dict(d)

        assert restored.objective_names == ["eui", "cost"]
        assert restored.maximize == [False, True]
        assert len(restored) == len(front)
        for orig, rest in zip(front.get_nondominated(), restored.get_nondominated(), strict=True):
            assert orig.sample_id == rest.sample_id
            assert orig.objectives == rest.objectives
            assert orig.parameters == rest.parameters
            assert orig.generation == rest.generation

    def test_empty_dict_roundtrip(self) -> None:
        front = ParetoFront(objective_names=["a", "b"])
        d = front.to_dict()
        restored = ParetoFront.from_dict(d)
        assert len(restored) == 0
        assert restored.objective_names == ["a", "b"]


class TestSaveLoad:
    """save / load round-trip with temp file."""

    def test_file_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "pareto" / "gen_0.json"
        front = ParetoFront(objective_names=["eui", "cost"])
        solutions = [
            _make_solution("s1", {"eui": 100.0, "cost": 50.0}, {"x": 1.0}),
            _make_solution("s2", {"eui": 120.0, "cost": 30.0}, {"x": 2.0}),
        ]
        front.add_generation(solutions)
        front.save(path)

        # Verify the file is valid JSON.
        data = json.loads(path.read_text())
        assert "objective_names" in data
        assert "solutions" in data

        # Load and compare.
        loaded = ParetoFront.load(path)
        assert len(loaded) == len(front)
        assert loaded.objective_names == ["eui", "cost"]
        ids = {s.sample_id for s in loaded.get_nondominated()}
        assert ids == {"s1", "s2"}

    def test_load_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "pareto.json"
        front = ParetoFront(objective_names=["a"])
        front.save(path)
        loaded = ParetoFront.load(path)
        assert len(loaded) == 0


class TestPerGeneration:
    """Per-generation persistence."""

    def test_generation_carries_forward(self, tmp_path: Path) -> None:
        """Solutions from gen 0 that survive should still be present in gen 1."""
        front = ParetoFront(objective_names=["eui", "cost"])

        # Gen 0: two solutions on the front.
        gen0 = [
            _make_solution("s1", {"eui": 100.0, "cost": 50.0}, generation=0),
            _make_solution("s2", {"eui": 120.0, "cost": 30.0}, generation=0),
        ]
        front.add_generation(gen0)
        gen0_path = tmp_path / "gen_0.json"
        front.save(gen0_path)

        # Gen 1: load from gen 0, add new solutions.
        front1 = ParetoFront.load(gen0_path)
        gen1 = [
            _make_solution("s3", {"eui": 110.0, "cost": 55.0}, generation=1),  # dominated by s1
            _make_solution("s4", {"eui": 130.0, "cost": 20.0}, generation=1),  # new front member
        ]
        front1.add_generation(gen1)
        gen1_path = tmp_path / "gen_1.json"
        front1.save(gen1_path)

        loaded = ParetoFront.load(gen1_path)
        ids = {s.sample_id for s in loaded.get_nondominated()}
        assert "s1" in ids  # survived from gen 0
        assert "s4" in ids  # new from gen 1
        assert "s3" not in ids  # dominated


class TestEdgeCases:
    """Edge cases."""

    def test_empty_front_is_valid(self) -> None:
        front = ParetoFront(objective_names=["eui", "cost"])
        assert len(front) == 0
        assert front.get_nondominated() == []
        d = front.to_dict()
        assert d["solutions"] == []

    def test_single_solution_is_nondominated(self) -> None:
        front = ParetoFront(objective_names=["eui"])
        front.add_generation([_make_solution("s1", {"eui": 100.0})])
        assert len(front) == 1
        assert front.get_nondominated()[0].sample_id == "s1"

    def test_add_empty_generation_preserves_existing(self) -> None:
        front = ParetoFront(objective_names=["eui"])
        front.add_generation([_make_solution("s1", {"eui": 100.0})])
        front.add_generation([])  # no new solutions
        assert len(front) == 1

    def test_equal_objectives_are_nondominated(self) -> None:
        """Two solutions with identical objectives — neither dominates."""
        front = ParetoFront(objective_names=["eui", "cost"])
        solutions = [
            _make_solution("s1", {"eui": 100.0, "cost": 50.0}),
            _make_solution("s2", {"eui": 100.0, "cost": 50.0}),
        ]
        front.add_generation(solutions)
        assert len(front) == 2

    def test_mismatched_maximize_raises(self) -> None:
        with pytest.raises(ValueError, match="maximize length"):
            ParetoFront(objective_names=["a", "b", "c"], maximize=[True, False])

    def test_missing_objective_means_no_domination(self) -> None:
        """Solution missing an objective cannot dominate or be dominated."""
        front = ParetoFront(objective_names=["eui", "cost"])
        solutions = [
            ParetoSolution("s1", {"eui": 100.0}, {"x": 1.0}, 0),  # missing cost
            ParetoSolution("s2", {"eui": 80.0, "cost": 30.0}, {"x": 2.0}, 0),
        ]
        front.add_generation(solutions)
        # s1 is not dominated (s2 can't dominate due to missing objective)
        # and s2 is not dominated either.
        assert len(front) == 2
