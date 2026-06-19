"""Tests for the ``osimflow serve`` CLI subcommand (issue #143)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow.__main__ import _build_parser, _cmd_serve


class TestServeCLI:
    """Tests for the serve subcommand wiring."""

    def test_serve_requires_outdir(self) -> None:
        """Serve subcommand requires --outdir."""
        from osimflow.__main__ import main

        with pytest.raises(SystemExit) as exc_info:
            main(["serve"])
        assert exc_info.value.code == 2

    def test_serve_creates_app_with_correct_args(self, tmp_path: Path) -> None:
        """Serve subcommand passes outdir and read_only to create_app."""
        parser = _build_parser()
        args = parser.parse_args(["serve", "--outdir", str(tmp_path)])

        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app),
        ):
            result = _cmd_serve(args)

        assert result == 0
        mock_uvicorn.run.assert_called_once_with(mock_app, host="127.0.0.1", port=8000)

    def test_serve_custom_host_port(self, tmp_path: Path) -> None:
        """Serve subcommand passes --host and --port to uvicorn."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "serve",
                "--outdir",
                str(tmp_path),
                "--host",
                "127.0.0.1",
                "--port",
                "9000",
            ]
        )

        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app),
        ):
            _cmd_serve(args)

        mock_uvicorn.run.assert_called_once_with(mock_app, host="127.0.0.1", port=9000)

    def test_serve_read_write_flag(self, tmp_path: Path) -> None:
        """Serve with --read-write passes read_only=False."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "serve",
                "--outdir",
                str(tmp_path),
                "--read-write",
            ]
        )

        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app) as mock_create_app,
        ):
            _cmd_serve(args)

        mock_create_app.assert_called_once_with(
            outdir=tmp_path,
            read_only=False,
            api_key=None,
            api_keys_file=None,
            cors_origins=None,
            rate_limit="60/minute",
            rate_limit_key="ip",
            ui_enabled=False,
            variable_editor=False,
            results_viewer=False,
            dashboard=False,
            registry_path=None,
        )

    def test_serve_read_only_default(self, tmp_path: Path) -> None:
        """Serve defaults to read_only=True."""
        parser = _build_parser()
        args = parser.parse_args(
            [
                "serve",
                "--outdir",
                str(tmp_path),
            ]
        )

        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app) as mock_create_app,
        ):
            _cmd_serve(args)

        mock_create_app.assert_called_once_with(
            outdir=tmp_path,
            read_only=True,
            api_key=None,
            api_keys_file=None,
            cors_origins=None,
            rate_limit="60/minute",
            rate_limit_key="ip",
            ui_enabled=False,
            variable_editor=False,
            results_viewer=False,
            dashboard=False,
            registry_path=None,
        )

    def test_serve_import_error_returns_1(self, tmp_path: Path) -> None:
        """Serve returns 1 when uvicorn is not installed."""
        parser = _build_parser()
        args = parser.parse_args(["serve", "--outdir", str(tmp_path)])

        # Patch the import inside _cmd_serve by making uvicorn unimportable
        import builtins

        real_import = builtins.__import__

        def block_uvicorn(name: str, *args: object, **kwargs: object) -> object:
            if name == "uvicorn":
                raise ImportError("no uvicorn")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=block_uvicorn):
            result = _cmd_serve(args)

        assert result == 1

    def test_serve_log_level(self, tmp_path: Path) -> None:
        """Serve subcommand accepts --log_level."""
        from osimflow.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            [
                "serve",
                "--outdir",
                str(tmp_path),
                "--log_level",
                "DEBUG",
            ]
        )
        assert args.log_level == "DEBUG"

    def test_serve_default_log_level(self, tmp_path: Path) -> None:
        """Serve subcommand defaults log_level to INFO."""
        from osimflow.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            [
                "serve",
                "--outdir",
                str(tmp_path),
            ]
        )
        assert args.log_level == "INFO"
