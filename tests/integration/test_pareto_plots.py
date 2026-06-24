"""Integration tests for Pareto front plot generation (issue #124)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osimflow.work import generate_plots


@pytest.fixture
def _campaign_csvs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create minimal CSVs and return (csv, failed_csv, outdir)."""
    csv_path = tmp_path / "aggregated_results.csv"
    csv_path.write_text("sample_id,eui_kwh_m2_yr\ns001,100.0\ns002,120.0\n")
    failed_path = tmp_path / "failed_simulations.csv"
    failed_path.write_text("sample_id,error_summary\n")
    outdir = tmp_path / "plots"
    return csv_path, failed_path, outdir


def _write_pareto_gen(pareto_dir: Path, gen: int, solutions: list[dict]) -> None:
    """Write a gen_N.json file."""
    pareto_dir.mkdir(parents=True, exist_ok=True)
    (pareto_dir / f"gen_{gen}.json").write_text(
        json.dumps({"generation": gen, "solutions": solutions})
    )


class TestParetoFrontPlots:
    """Tests for Pareto front scatter plot generation."""

    def test_multi_obj_produces_pareto_front_png(
        self, tmp_path: Path, _campaign_csvs: tuple[Path, Path, Path]
    ) -> None:
        csv_path, failed_path, outdir = _campaign_csvs
        pareto_dir = tmp_path / "pareto"
        _write_pareto_gen(
            pareto_dir,
            0,
            [
                {"sample_id": "s001", "objectives": {"eui": 100, "cost": 50}, "parameters": {}},
                {"sample_id": "s002", "objectives": {"eui": 120, "cost": 30}, "parameters": {}},
            ],
        )
        _write_pareto_gen(
            pareto_dir,
            1,
            [
                {"sample_id": "s003", "objectives": {"eui": 95, "cost": 45}, "parameters": {}},
            ],
        )
        plots = generate_plots(csv_path, failed_path, outdir, pareto_dir=pareto_dir)
        assert (outdir / "pareto_front.png").exists()
        assert (outdir / "pareto_front.png") in plots

    def test_multi_obj_multiple_gens_produces_convergence_png(
        self, tmp_path: Path, _campaign_csvs: tuple[Path, Path, Path]
    ) -> None:
        csv_path, failed_path, outdir = _campaign_csvs
        pareto_dir = tmp_path / "pareto"
        for gen in range(3):
            _write_pareto_gen(
                pareto_dir,
                gen,
                [
                    {
                        "sample_id": f"s{gen}",
                        "objectives": {"eui": 100 - gen * 5, "cost": 50 - gen},
                        "parameters": {},
                    },
                ],
            )
        generate_plots(csv_path, failed_path, outdir, pareto_dir=pareto_dir)
        assert (outdir / "pareto_convergence.png").exists()

    def test_single_gen_no_convergence_plot(
        self, tmp_path: Path, _campaign_csvs: tuple[Path, Path, Path]
    ) -> None:
        csv_path, failed_path, outdir = _campaign_csvs
        pareto_dir = tmp_path / "pareto"
        _write_pareto_gen(
            pareto_dir,
            0,
            [
                {"sample_id": "s001", "objectives": {"eui": 100, "cost": 50}, "parameters": {}},
            ],
        )
        generate_plots(csv_path, failed_path, outdir, pareto_dir=pareto_dir)
        assert (outdir / "pareto_front.png").exists()
        assert not (outdir / "pareto_convergence.png").exists()

    def test_single_obj_no_pareto_plots(
        self, tmp_path: Path, _campaign_csvs: tuple[Path, Path, Path]
    ) -> None:
        """Single-objective campaign produces no Pareto plots."""
        csv_path, failed_path, outdir = _campaign_csvs
        pareto_dir = tmp_path / "pareto"
        _write_pareto_gen(
            pareto_dir,
            0,
            [
                {"sample_id": "s001", "objectives": {"eui": 100}, "parameters": {}},
            ],
        )
        generate_plots(csv_path, failed_path, outdir, pareto_dir=pareto_dir)
        assert not (outdir / "pareto_front.png").exists()
        assert not (outdir / "pareto_convergence.png").exists()

    def test_no_pareto_dir_no_pareto_plots(
        self, tmp_path: Path, _campaign_csvs: tuple[Path, Path, Path]
    ) -> None:
        """Without pareto_dir, no Pareto plots are generated."""
        csv_path, failed_path, outdir = _campaign_csvs
        generate_plots(csv_path, failed_path, outdir)
        assert not (outdir / "pareto_front.png").exists()

    def test_empty_pareto_dir_no_plots(
        self, tmp_path: Path, _campaign_csvs: tuple[Path, Path, Path]
    ) -> None:
        """Empty pareto_dir produces no Pareto plots."""
        csv_path, failed_path, outdir = _campaign_csvs
        pareto_dir = tmp_path / "pareto"
        pareto_dir.mkdir()
        generate_plots(csv_path, failed_path, outdir, pareto_dir=pareto_dir)
        assert not (outdir / "pareto_front.png").exists()
