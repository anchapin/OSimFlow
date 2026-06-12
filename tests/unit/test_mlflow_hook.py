"""Unit tests for osimflow.mlflow_hook — optional MLflow integration (issue #220).

Covers:
- Lazy import of mlflow package
- maybe_start_mlflow_run: starts run when URI is set, no-op when None
- maybe_end_mlflow_run: ends active run, no-op when no run active
- log_mlflow_params: logs config fields as parameters
- log_mlflow_metrics: logs campaign summary metrics
- log_mlflow_artifacts: logs output files, skips missing ones
- Graceful degradation when mlflow is not installed
- Module-level state reset between tests
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import osimflow.mlflow_hook as hook_mod


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """Reset module-level globals between tests."""
    hook_mod._ACTIVE_URI = None
    hook_mod._ACTIVE_RUN_NAME = None
    yield
    hook_mod._ACTIVE_URI = None
    hook_mod._ACTIVE_RUN_NAME = None


class TestImportMlflow:
    """Tests for _import_mlflow lazy import helper."""

    def test_returns_mlflow_module_when_installed(self) -> None:
        mlflow_mock = MagicMock()
        with patch.dict("sys.modules", {"mlflow": mlflow_mock}):
            result = hook_mod._import_mlflow()
            assert result is mlflow_mock

    def test_returns_none_when_not_installed(self) -> None:
        with patch.dict("sys.modules", {}, clear=False):
            with patch("builtins.__import__", side_effect=ImportError("no mlflow")):
                result = hook_mod._import_mlflow()
                assert result is None


class TestMaybeStartMlflowRun:
    """Tests for maybe_start_mlflow_run."""

    def test_noop_when_uri_is_none(self) -> None:
        result = hook_mod.maybe_start_mlflow_run(None, "test-campaign")
        assert result is None
        assert hook_mod._ACTIVE_URI is None

    def test_noop_when_uri_is_empty_string(self) -> None:
        result = hook_mod.maybe_start_mlflow_run("", "test-campaign")
        assert result is None

    def test_noop_when_mlflow_not_installed(self) -> None:
        with patch.object(hook_mod, "_import_mlflow", return_value=None):
            result = hook_mod.maybe_start_mlflow_run("http://localhost:5000", "camp-1")
            assert result is None
            assert hook_mod._ACTIVE_URI is None

    def test_starts_run_when_uri_set(self) -> None:
        mlflow_mock = MagicMock()
        with patch.object(hook_mod, "_import_mlflow", return_value=mlflow_mock):
            result = hook_mod.maybe_start_mlflow_run("http://localhost:5000", "camp-1")

        assert result == "camp-1"
        assert hook_mod._ACTIVE_URI == "http://localhost:5000"
        assert hook_mod._ACTIVE_RUN_NAME == "camp-1"
        mlflow_mock.set_tracking_uri.assert_called_once_with("http://localhost:5000")
        mlflow_mock.start_run.assert_called_once_with(run_name="camp-1")

    def test_sets_tracking_uri_before_start_run(self) -> None:
        call_order: list[str] = []
        mlflow_mock = MagicMock()
        mlflow_mock.set_tracking_uri.side_effect = lambda *a, **k: call_order.append(
            "set_tracking_uri"
        )
        mlflow_mock.start_run.side_effect = lambda *a, **k: call_order.append("start_run")

        with patch.object(hook_mod, "_import_mlflow", return_value=mlflow_mock):
            hook_mod.maybe_start_mlflow_run("http://mlflow.server", "camp-2")

        assert call_order == ["set_tracking_uri", "start_run"]


class TestMaybeEndMlflowRun:
    """Tests for maybe_end_mlflow_run."""

    def test_noop_when_no_active_run(self) -> None:
        hook_mod.maybe_end_mlflow_run()
        assert hook_mod._ACTIVE_URI is None

    def test_ends_active_run(self) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        hook_mod._ACTIVE_RUN_NAME = "camp-1"

        mlflow_mock = MagicMock()
        with patch.object(hook_mod, "_import_mlflow", return_value=mlflow_mock):
            hook_mod.maybe_end_mlflow_run()

        assert hook_mod._ACTIVE_URI is None
        assert hook_mod._ACTIVE_RUN_NAME is None
        mlflow_mock.end_run.assert_called_once()

    def test_resets_state_even_when_mlflow_import_fails(self) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        hook_mod._ACTIVE_RUN_NAME = "camp-1"

        with patch.object(hook_mod, "_import_mlflow", return_value=None):
            hook_mod.maybe_end_mlflow_run()

        assert hook_mod._ACTIVE_URI is None
        assert hook_mod._ACTIVE_RUN_NAME is None

    def test_resets_state_even_when_end_run_raises(self) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        hook_mod._ACTIVE_RUN_NAME = "camp-1"

        mlflow_mock = MagicMock()
        mlflow_mock.end_run.side_effect = RuntimeError("connection lost")

        with patch.object(hook_mod, "_import_mlflow", return_value=mlflow_mock):
            hook_mod.maybe_end_mlflow_run()

        assert hook_mod._ACTIVE_URI is None
        assert hook_mod._ACTIVE_RUN_NAME is None


class TestLogMlflowParams:
    """Tests for log_mlflow_params."""

    def test_noop_when_no_active_run(self) -> None:
        cfg = MagicMock()
        hook_mod.log_mlflow_params(cfg)

    def test_noop_when_mlflow_not_installed(self) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        with patch.object(hook_mod, "_import_mlflow", return_value=None):
            cfg = MagicMock()
            hook_mod.log_mlflow_params(cfg)

    def test_logs_config_fields(self) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        mlflow_mock = MagicMock()
        cfg = MagicMock(
            spec=["executor", "openstudio_version", "n_samples", "archive_intermediates"]
        )
        cfg.executor = "local"
        cfg.openstudio_version = "3.11.0"
        cfg.n_samples = 10
        cfg.archive_intermediates = False

        with patch.object(hook_mod, "_import_mlflow", return_value=mlflow_mock):
            hook_mod.log_mlflow_params(cfg)

        logged_params = {
            call.args[0]: call.args[1] for call in mlflow_mock.log_param.call_args_list
        }
        assert logged_params["executor"] == "local"
        assert logged_params["openstudio_version"] == "3.11.0"
        assert logged_params["n_samples"] == 10
        assert logged_params["archive_intermediates"] is False

    def test_handles_missing_config_attrs(self) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        mlflow_mock = MagicMock()
        cfg = MagicMock(spec=[])

        with patch.object(hook_mod, "_import_mlflow", return_value=mlflow_mock):
            hook_mod.log_mlflow_params(cfg)

        logged_params = {
            call.args[0]: call.args[1] for call in mlflow_mock.log_param.call_args_list
        }
        assert logged_params["executor"] is None
        assert logged_params["n_samples"] is None


class TestLogMlflowMetrics:
    """Tests for log_mlflow_metrics."""

    def test_noop_when_no_active_run(self) -> None:
        hook_mod.log_mlflow_metrics(100.0, 8, 2)

    def test_noop_when_mlflow_not_installed(self) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        with patch.object(hook_mod, "_import_mlflow", return_value=None):
            hook_mod.log_mlflow_metrics(100.0, 8, 2)

    def test_logs_metrics(self) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        mlflow_mock = MagicMock()

        with patch.object(hook_mod, "_import_mlflow", return_value=mlflow_mock):
            hook_mod.log_mlflow_metrics(42.5, 8, 2)

        logged = {call.args[0]: call.args[1] for call in mlflow_mock.log_metric.call_args_list}
        assert logged["elapsed_s"] == 42.5
        assert logged["n_succeeded"] == 8
        assert logged["n_failed"] == 2

    def test_casts_values(self) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        mlflow_mock = MagicMock()

        with patch.object(hook_mod, "_import_mlflow", return_value=mlflow_mock):
            hook_mod.log_mlflow_metrics(42, 8, 2)

        mlflow_mock.log_metric.assert_any_call("elapsed_s", 42.0)
        mlflow_mock.log_metric.assert_any_call("n_succeeded", 8)
        mlflow_mock.log_metric.assert_any_call("n_failed", 2)


class TestLogMlflowArtifacts:
    """Tests for log_mlflow_artifacts."""

    def test_noop_when_no_active_run(self) -> None:
        hook_mod.log_mlflow_artifacts(Path("/a.csv"), Path("/b.csv"), Path("/c.json"))

    def test_noop_when_mlflow_not_installed(self) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        with patch.object(hook_mod, "_import_mlflow", return_value=None):
            hook_mod.log_mlflow_artifacts(Path("/a.csv"), Path("/b.csv"), Path("/c.json"))

    def test_logs_existing_files(self, tmp_path: Path) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        mlflow_mock = MagicMock()

        csv = tmp_path / "aggregated.csv"
        csv.write_text("data")
        failed = tmp_path / "failed.csv"
        failed.write_text("errors")
        run_json = tmp_path / "run.json"
        run_json.write_text("{}")

        with patch.object(hook_mod, "_import_mlflow", return_value=mlflow_mock):
            hook_mod.log_mlflow_artifacts(csv, failed, run_json)

        assert mlflow_mock.log_artifact.call_count == 3
        logged_paths = {call.args[0] for call in mlflow_mock.log_artifact.call_args_list}
        assert str(csv) in logged_paths
        assert str(failed) in logged_paths
        assert str(run_json) in logged_paths

    def test_skips_missing_files(self, tmp_path: Path) -> None:
        hook_mod._ACTIVE_URI = "http://localhost:5000"
        mlflow_mock = MagicMock()

        csv = tmp_path / "aggregated.csv"
        csv.write_text("data")
        missing_csv = tmp_path / "nonexistent.csv"
        missing_json = tmp_path / "missing.json"

        with patch.object(hook_mod, "_import_mlflow", return_value=mlflow_mock):
            hook_mod.log_mlflow_artifacts(csv, missing_csv, missing_json)

        assert mlflow_mock.log_artifact.call_count == 1
        mlflow_mock.log_artifact.assert_called_once_with(str(csv))


class TestFullWorkflow:
    """Integration-style test: start → log params → log metrics → log artifacts → end."""

    def test_full_mlflow_lifecycle(self, tmp_path: Path) -> None:
        mlflow_mock = MagicMock()
        csv = tmp_path / "results.csv"
        csv.write_text("eui,cost\n100,50\n")
        failed = tmp_path / "failed.csv"
        failed.write_text("")
        run_json = tmp_path / "run.json"
        run_json.write_text('{"campaign_id": "test"}')

        cfg = MagicMock(
            spec=["executor", "openstudio_version", "n_samples", "archive_intermediates"]
        )
        cfg.executor = "local"
        cfg.openstudio_version = "3.11.0"
        cfg.n_samples = 5
        cfg.archive_intermediates = True

        with patch.object(hook_mod, "_import_mlflow", return_value=mlflow_mock):
            run_name = hook_mod.maybe_start_mlflow_run("http://localhost:5000", "test-camp")
            assert run_name == "test-camp"

            hook_mod.log_mlflow_params(cfg)
            hook_mod.log_mlflow_metrics(120.0, 5, 0)
            hook_mod.log_mlflow_artifacts(csv, failed, run_json)

            hook_mod.maybe_end_mlflow_run()

        mlflow_mock.set_tracking_uri.assert_called_once()
        mlflow_mock.start_run.assert_called_once()
        assert mlflow_mock.log_param.call_count == 4
        assert mlflow_mock.log_metric.call_count == 3
        mlflow_mock.end_run.assert_called_once()
        assert hook_mod._ACTIVE_URI is None
