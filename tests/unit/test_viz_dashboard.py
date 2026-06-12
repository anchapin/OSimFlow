"""Tests for the ``osimflow dashboard`` CLI subcommand and viz package (issue #199)."""

from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from osimflow.__main__ import _build_parser, _cmd_dashboard, main

# ---------------------------------------------------------------------------
# DashboardData (unit tests, no streamlit required)
# ---------------------------------------------------------------------------


class TestDashboardData:
    """Tests for the DashboardData data loader."""

    def test_load_with_results_csv(self, tmp_path: Path) -> None:
        """DashboardData loads aggregated_results.csv when present."""
        csv = tmp_path / "aggregated_results.csv"
        df = pd.DataFrame({"sample_id": [0, 1], "eui_kwh_m2_yr": [120.5, 98.3]})
        df.to_csv(csv, index=False)

        from osimflow.viz.dashboard import DashboardData

        data = DashboardData(tmp_path)
        assert data.has_results
        assert data.sample_count == 2
        assert data.eui_column() == "eui_kwh_m2_yr"

    def test_load_with_run_json(self, tmp_path: Path) -> None:
        """DashboardData loads run.json when present."""
        run_json = tmp_path / "run.json"
        run_json.write_text(json.dumps({
            "schema_version": 1,
            "per_sample": [
                {"sample_id": "0", "status": "ok", "elapsed_s": 5.0},
                {"sample_id": "1", "status": "failed", "elapsed_s": 2.0},
            ],
            "steps": [],
        }))

        from osimflow.viz.dashboard import DashboardData

        data = DashboardData(tmp_path)
        assert data.has_run_trace
        assert data.sample_count == 2
        assert data.failure_count == 1
        assert data.success_count == 1

    def test_empty_directory(self, tmp_path: Path) -> None:
        """DashboardData handles empty directory gracefully."""
        from osimflow.viz.dashboard import DashboardData

        data = DashboardData(tmp_path)
        assert not data.has_results
        assert not data.has_run_trace
        assert data.sample_count == 0
        assert data.failure_count == 0

    def test_eui_column_heuristic(self, tmp_path: Path) -> None:
        """eui_column() finds EUI column by various naming conventions."""
        csv = tmp_path / "aggregated_results.csv"
        df = pd.DataFrame({"sample_id": [0], "total_eui": [100.0]})
        df.to_csv(csv, index=False)

        from osimflow.viz.dashboard import DashboardData

        data = DashboardData(tmp_path)
        # "total_eui" contains "eui" so it should be found
        assert data.eui_column() == "total_eui"

    def test_numeric_lhs_columns(self, tmp_path: Path) -> None:
        """numeric_lhs_columns() returns numeric non-KPI columns."""
        csv = tmp_path / "aggregated_results.csv"
        df = pd.DataFrame({
            "sample_id": [0],
            "insulation_thickness": [0.1],
            "window_ratio": [0.4],
            "eui_kwh_m2_yr": [120.5],
            "name": ["model_a"],
        })
        df.to_csv(csv, index=False)

        from osimflow.viz.dashboard import DashboardData

        data = DashboardData(tmp_path)
        lhs = data.numeric_lhs_columns()
        assert "insulation_thickness" in lhs
        assert "window_ratio" in lhs
        assert "eui_kwh_m2_yr" not in lhs
        assert "name" not in lhs

    def test_failure_count_from_failed_csv(self, tmp_path: Path) -> None:
        """failure_count reads from failed_simulations.csv when available."""
        csv = tmp_path / "aggregated_results.csv"
        pd.DataFrame({"sample_id": [0, 1, 2], "eui": [100, 110, 120]}).to_csv(csv, index=False)

        failed = tmp_path / "failed_simulations.csv"
        pd.DataFrame({"sample_id": [1], "error": ["Severe Error"]}).to_csv(failed, index=False)

        from osimflow.viz.dashboard import DashboardData

        data = DashboardData(tmp_path)
        assert data.failure_count == 1


# ---------------------------------------------------------------------------
# create_dashboard_app function exists
# ---------------------------------------------------------------------------


class TestCreateDashboardApp:
    """Tests for create_dashboard_app."""

    def test_function_exists_and_callable(self) -> None:
        """create_dashboard_app is importable and callable."""
        from osimflow.viz.dashboard import create_dashboard_app

        assert callable(create_dashboard_app)

    def test_returns_none_signature(self) -> None:
        """create_dashboard_app has correct signature."""
        import inspect

        from osimflow.viz.dashboard import create_dashboard_app

        sig = inspect.signature(create_dashboard_app)
        params = list(sig.parameters.keys())
        assert "outdir" in params
        assert "port" in params
        assert sig.parameters["port"].default == 8501


# ---------------------------------------------------------------------------
# CLI parser — dashboard subcommand
# ---------------------------------------------------------------------------


class TestDashboardCLI:
    """Tests for the dashboard CLI subcommand wiring."""

    def test_parser_accepts_dashboard(self) -> None:
        """Parser recognises the 'dashboard' subcommand."""
        parser = _build_parser()
        args = parser.parse_args(["dashboard", "./results"])
        assert args.command == "dashboard"
        assert args.outdir == Path("./results")

    def test_dashboard_default_port(self) -> None:
        """Dashboard defaults to port 8501."""
        parser = _build_parser()
        args = parser.parse_args(["dashboard", "./results"])
        assert args.port == 8501

    def test_dashboard_custom_port(self) -> None:
        """Dashboard accepts --port flag."""
        parser = _build_parser()
        args = parser.parse_args(["dashboard", "./results", "--port", "9999"])
        assert args.port == 9999

    def test_dashboard_requires_outdir(self) -> None:
        """Dashboard subcommand requires outdir positional arg."""
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["dashboard"])

    def test_cmd_dashboard_missing_dir(self, tmp_path: Path) -> None:
        """_cmd_dashboard returns 1 when outdir does not exist."""
        parser = _build_parser()
        args = parser.parse_args(["dashboard", str(tmp_path / "nonexistent")])
        result = _cmd_dashboard(args)
        assert result == 1

    def test_cmd_dashboard_streamlit_not_installed(self, tmp_path: Path) -> None:
        """_cmd_dashboard returns 1 when the viz module import fails (no streamlit)."""
        parser = _build_parser()
        args = parser.parse_args(["dashboard", str(tmp_path)])

        # Remove viz modules from sys.modules cache so __import__ is invoked,
        # then block the import to simulate streamlit not installed.
        real_import = builtins.__import__
        saved_modules: dict[str, object] = {}
        for key in list(sys.modules.keys()):
            if key.startswith("osimflow.viz"):
                saved_modules[key] = sys.modules.pop(key)

        def block_viz(name: str, *a: object, **kw: object) -> object:
            if name == "osimflow.viz.dashboard":
                raise ImportError("no streamlit")
            return real_import(name, *a, **kw)

        try:
            with patch.object(builtins, "__import__", side_effect=block_viz):
                result = _cmd_dashboard(args)
        finally:
            sys.modules.update(saved_modules)

        assert result == 1

    def test_cmd_dashboard_launches_streamlit(self, tmp_path: Path) -> None:
        """_cmd_dashboard calls create_dashboard_app when streamlit is available."""
        parser = _build_parser()
        args = parser.parse_args(["dashboard", str(tmp_path)])

        with patch("osimflow.viz.dashboard.create_dashboard_app") as mock_create:
            result = _cmd_dashboard(args)

        assert result == 0
        mock_create.assert_called_once_with(
            outdir=tmp_path.resolve(), port=8501,
        )

    def test_main_routes_dashboard(self, tmp_path: Path) -> None:
        """main() routes 'dashboard' command to _cmd_dashboard."""
        with patch("osimflow.__main__._cmd_dashboard", return_value=0) as mock:
            result = main(["dashboard", str(tmp_path)])
        assert result == 0
        mock.assert_called_once()


# ---------------------------------------------------------------------------
# create_dashboard_app graceful fallback
# ---------------------------------------------------------------------------


class TestDashboardFallback:
    """Tests for graceful fallback when streamlit is not installed."""

    def test_create_dashboard_app_raises_without_streamlit(self, tmp_path: Path) -> None:
        """create_dashboard_app raises SystemExit when streamlit is not installed."""
        real_import = builtins.__import__

        def block_streamlit(name: str, *a: object, **kw: object) -> object:
            if name == "streamlit" or name.startswith("streamlit."):
                raise ImportError("no streamlit")
            return real_import(name, *a, **kw)

        from osimflow.viz.dashboard import create_dashboard_app

        with (
            patch.object(builtins, "__import__", side_effect=block_streamlit),
            pytest.raises(SystemExit) as exc_info,
        ):
            create_dashboard_app(outdir=tmp_path)

        assert exc_info.value.code == 1
