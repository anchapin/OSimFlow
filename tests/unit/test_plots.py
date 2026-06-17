"""Tests for new GAP-012 plots: radar, EuiDistribution, and density heatmap."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from osimflow._work_scripts import generate_plots as gp


# ---------------------------------------------------------------------------
# _generate_eui_distribution_plot
# ---------------------------------------------------------------------------


class TestEuiDistributionPlot:
    """Tests for _generate_eui_distribution_plot (histogram + CDF)."""

    def test_produces_file(self, tmp_path: Path) -> None:
        df = pd.DataFrame({"eui_kwh_m2_yr": np.random.normal(120, 15, 100)})
        path = gp._generate_eui_distribution_plot(df, tmp_path, baseline_eui=None)
        assert path is not None
        assert path.exists()
        assert path.name == "eui_distribution.png"

    def test_with_baseline(self, tmp_path: Path) -> None:
        df = pd.DataFrame({"eui_kwh_m2_yr": np.random.normal(120, 15, 100)})
        path = gp._generate_eui_distribution_plot(df, tmp_path, baseline_eui=120.0)
        assert path is not None
        assert path.exists()

    def test_returns_none_when_no_eui_column(self, tmp_path: Path) -> None:
        df = pd.DataFrame({"param1": [1, 2, 3]})
        assert gp._generate_eui_distribution_plot(df, tmp_path, baseline_eui=None) is None

    def test_returns_none_when_empty(self, tmp_path: Path) -> None:
        df = pd.DataFrame({"eui_kwh_m2_yr": pd.Series(dtype=float)})
        assert gp._generate_eui_distribution_plot(df, tmp_path, baseline_eui=None) is None

    def test_returns_none_when_too_few_values(self, tmp_path: Path) -> None:
        df = pd.DataFrame({"eui_kwh_m2_yr": [100.0, 110.0]})
        assert gp._generate_eui_distribution_plot(df, tmp_path, baseline_eui=None) is None


# ---------------------------------------------------------------------------
# _generate_radar_plot
# ---------------------------------------------------------------------------


class TestRadarPlot:
    """Tests for _generate_radar_plot (spider/radar chart)."""

    def test_produces_file(self, tmp_path: Path) -> None:
        df = pd.DataFrame(
            {
                "eui_kwh_m2_yr": np.random.normal(120, 15, 50),
                "cost_per_m2": np.random.normal(200, 30, 50),
                "comfort_hours": np.random.normal(7000, 500, 50),
                "sample_id": list(range(50)),
            }
        )
        path = gp._generate_radar_plot(df, tmp_path)
        assert path is not None
        assert path.exists()
        assert path.name == "radar_plot.png"

    def test_skips_when_fewer_than_3_kpis(self, tmp_path: Path) -> None:
        df = pd.DataFrame(
            {
                "eui_kwh_m2_yr": np.random.normal(120, 15, 10),
                "cost_per_m2": np.random.normal(200, 30, 10),
                "sample_id": list(range(10)),
            }
        )
        assert gp._generate_radar_plot(df, tmp_path) is None

    def test_skips_constant_columns(self, tmp_path: Path) -> None:
        df = pd.DataFrame(
            {
                "eui_kwh_m2_yr": [120.0] * 20,
                "cost_per_m2": [200.0] * 20,
                "comfort_hours": list(range(20)),
                "sample_id": list(range(20)),
            }
        )
        # only comfort_hours varies, so < 3 varying columns
        assert gp._generate_radar_plot(df, tmp_path) is None

    def test_returns_none_when_empty(self, tmp_path: Path) -> None:
        assert gp._generate_radar_plot(pd.DataFrame(), tmp_path) is None


# ---------------------------------------------------------------------------
# _generate_density_heatmap
# ---------------------------------------------------------------------------


class TestDensityHeatmap:
    """Tests for _generate_density_heatmap (2-D KDE heatmap)."""

    def test_produces_file(self, tmp_path: Path) -> None:
        df = pd.DataFrame(
            {
                "insulation_r": np.random.normal(30, 5, 100),
                "window_u": np.random.normal(1.5, 0.3, 100),
                "eui_kwh_m2_yr": np.random.normal(120, 15, 100),
                "sample_id": list(range(100)),
            }
        )
        path = gp._generate_density_heatmap(df, tmp_path)
        assert path is not None
        assert path.exists()
        assert path.name == "density_heatmap.png"

    def test_skips_when_fewer_than_2_design_vars(self, tmp_path: Path) -> None:
        df = pd.DataFrame(
            {
                "insulation_r": np.random.normal(30, 5, 50),
                "eui_kwh_m2_yr": np.random.normal(120, 15, 50),
                "sample_id": list(range(50)),
            }
        )
        assert gp._generate_density_heatmap(df, tmp_path) is None

    def test_skips_constant_columns(self, tmp_path: Path) -> None:
        df = pd.DataFrame(
            {
                "insulation_r": [30.0] * 20,
                "window_u": list(range(20)),
                "eui_kwh_m2_yr": np.random.normal(120, 15, 20),
                "sample_id": list(range(20)),
            }
        )
        # only window_u varies among non-KPI columns
        assert gp._generate_density_heatmap(df, tmp_path) is None

    def test_returns_none_when_too_few_samples(self, tmp_path: Path) -> None:
        df = pd.DataFrame(
            {
                "insulation_r": [30.0, 31.0, 32.0],
                "window_u": [1.5, 1.6, 1.7],
                "sample_id": [0, 1, 2],
            }
        )
        assert gp._generate_density_heatmap(df, tmp_path) is None

    def test_returns_none_when_empty(self, tmp_path: Path) -> None:
        assert gp._generate_density_heatmap(pd.DataFrame(), tmp_path) is None


# ---------------------------------------------------------------------------
# Integration: main() produces the new files
# ---------------------------------------------------------------------------


class TestNewPlotsIntegration:
    """End-to-end test that main() generates radar, eui_distribution, density_heatmap."""

    def test_main_produces_new_plot_files(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        PROJECT_ROOT = Path(__file__).resolve().parents[2]
        results_csv = tmp_path / "results.csv"
        failed_csv = tmp_path / "failed.csv"
        outdir = tmp_path / "plots"
        outdir.mkdir()

        # Create a result CSV with enough samples and varying numeric columns
        np.random.seed(42)
        n = 80
        results_csv.write_text(
            "sample_id,eui_kwh_m2_yr,insulation_r,window_u\n"
            + "\n".join(
                f"{i},{np.random.normal(120,15):.2f},{np.random.normal(30,5):.2f},{np.random.normal(1.5,0.3):.2f}"
                for i in range(n)
            )
        )
        failed_csv.write_text("sample_id,error_summary,exit_code\n")

        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "bin" / "generate_plots.py"),
                "--results_csv",
                str(results_csv),
                "--failed_csv",
                str(failed_csv),
                "--outdir",
                str(outdir),
            ],
            capture_output=True,
        )
        assert result.returncode == 0, f"STDERR: {result.stderr.decode()}"
        assert (outdir / "radar_plot.png").exists()
        assert (outdir / "eui_distribution.png").exists()
        assert (outdir / "density_heatmap.png").exists()