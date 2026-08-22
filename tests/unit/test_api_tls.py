"""Tests for TLS enforcement on the REST API server (issue #333, SEC-004)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from osimflow.__main__ import _build_parser, _cmd_serve


class TestTLSServeCLI:
    """Tests for the TLS CLI flags on the serve subcommand."""

    def test_tls_cert_flag_accepted(self, tmp_path: Path) -> None:
        """Serve subcommand accepts --tls-cert."""
        parser = _build_parser()
        cert = tmp_path / "cert.pem"
        cert.touch()
        args = parser.parse_args(["serve", "--outdir", str(tmp_path), "--tls-cert", str(cert)])
        assert args.tls_cert == cert

    def test_tls_key_flag_accepted(self, tmp_path: Path) -> None:
        """Serve subcommand accepts --tls-key."""
        parser = _build_parser()
        key = tmp_path / "key.pem"
        key.touch()
        args = parser.parse_args(["serve", "--outdir", str(tmp_path), "--tls-key", str(key)])
        assert args.tls_key == key

    def test_tls_both_flags_accepted(self, tmp_path: Path) -> None:
        """Serve subcommand accepts both --tls-cert and --tls-key."""
        parser = _build_parser()
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.touch()
        key.touch()
        args = parser.parse_args(
            [
                "serve",
                "--outdir",
                str(tmp_path),
                "--tls-cert",
                str(cert),
                "--tls-key",
                str(key),
            ]
        )
        assert args.tls_cert == cert
        assert args.tls_key == key

    def test_tls_cert_only_returns_error(self, tmp_path: Path) -> None:
        """Providing --tls-cert without --tls-key returns exit code 1."""
        parser = _build_parser()
        cert = tmp_path / "cert.pem"
        cert.touch()
        args = parser.parse_args(["serve", "--outdir", str(tmp_path), "--tls-cert", str(cert)])

        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app),
        ):
            result = _cmd_serve(args)

        assert result == 1
        mock_uvicorn.run.assert_not_called()

    def test_tls_key_only_returns_error(self, tmp_path: Path) -> None:
        """Providing --tls-key without --tls-cert returns exit code 1."""
        parser = _build_parser()
        key = tmp_path / "key.pem"
        key.touch()
        args = parser.parse_args(["serve", "--outdir", str(tmp_path), "--tls-key", str(key)])

        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app),
        ):
            result = _cmd_serve(args)

        assert result == 1
        mock_uvicorn.run.assert_not_called()

    def test_tls_both_passed_to_uvicorn(self, tmp_path: Path) -> None:
        """When both --tls-cert and --tls-key are provided, uvicorn.run is called with ssl parameters."""
        parser = _build_parser()
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.touch()
        key.touch()
        args = parser.parse_args(
            [
                "serve",
                "--outdir",
                str(tmp_path),
                "--tls-cert",
                str(cert),
                "--tls-key",
                str(key),
            ]
        )

        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app),
        ):
            result = _cmd_serve(args)

        assert result == 0
        mock_uvicorn.run.assert_called_once_with(
            mock_app,
            host="127.0.0.1",
            port=8000,
            ssl_certfile=cert,
            ssl_keyfile=key,
        )

    def test_no_tls_calls_uvicorn_without_ssl(self, tmp_path: Path) -> None:
        """When no TLS flags are provided, uvicorn.run is called without ssl parameters."""
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

    def test_tls_default_is_none(self, tmp_path: Path) -> None:
        """TLS flags default to None when not provided."""
        parser = _build_parser()
        args = parser.parse_args(["serve", "--outdir", str(tmp_path)])
        assert args.tls_cert is None
        assert args.tls_key is None

    def test_tls_with_custom_host_port(self, tmp_path: Path) -> None:
        """TLS flags work with custom --host and --port."""
        parser = _build_parser()
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.touch()
        key.touch()
        args = parser.parse_args(
            [
                "serve",
                "--outdir",
                str(tmp_path),
                "--tls-cert",
                str(cert),
                "--tls-key",
                str(key),
                "--host",
                "0.0.0.0",
                "--port",
                "443",
            ]
        )

        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app),
        ):
            result = _cmd_serve(args)

        assert result == 0
        mock_uvicorn.run.assert_called_once_with(
            mock_app,
            host="0.0.0.0",
            port=443,
            ssl_certfile=cert,
            ssl_keyfile=key,
        )


class TestAutoGeneratedApiKeyDisplay:
    """The auto-generated API key must be printed to stdout (issue #1117)."""

    def test_generated_key_printed_to_stdout(self, tmp_path: Path, capsys) -> None:
        """--enable-writes without --api-key prints the actual key value."""
        parser = _build_parser()
        args = parser.parse_args(["serve", "--outdir", str(tmp_path), "--enable-writes"])

        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app) as mock_create,
        ):
            result = _cmd_serve(args)

        assert result == 0
        captured = capsys.readouterr().out
        # The key passed to create_app must appear verbatim in stdout.
        assert mock_create.call_args is not None
        api_key = mock_create.call_args[1]["api_key"]
        assert api_key, "create_app must receive a non-None api_key"
        assert api_key in captured, "the generated key value must be printed to stdout"
        assert "won't be shown again" in captured

    def test_explicit_api_key_not_regenerated(self, tmp_path: Path, capsys) -> None:
        """When --api-key is provided, no auto-generation message is printed."""
        parser = _build_parser()
        args = parser.parse_args(
            ["serve", "--outdir", str(tmp_path), "--enable-writes", "--api-key", "my-secret"]
        )

        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app),
        ):
            result = _cmd_serve(args)

        assert result == 0
        captured = capsys.readouterr().out
        assert "Auto-generated" not in captured


class TestNonLocalTLSWarning:
    """Loud warning when TLS is off on a network-accessible bind (issue #1113)."""

    def test_warns_for_nonlocal_bind_without_tls(self, tmp_path: Path) -> None:
        import pytest

        parser = _build_parser()
        args = parser.parse_args(
            ["serve", "--outdir", str(tmp_path), "--host", "0.0.0.0", "--port", "8000"]
        )
        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app),
            pytest.warns(UserWarning, match="SEC-004"),
        ):
            result = _cmd_serve(args)

        assert result == 0
        mock_uvicorn.run.assert_called_once()

    def test_no_warning_for_localhost_bind_without_tls(self, tmp_path: Path) -> None:
        import warnings as warnings_mod

        parser = _build_parser()
        args = parser.parse_args(["serve", "--outdir", str(tmp_path), "--host", "127.0.0.1"])
        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app),
            warnings_mod.catch_warnings(),
        ):
            warnings_mod.simplefilter("error", UserWarning)
            result = _cmd_serve(args)

        assert result == 0

    def test_no_warning_when_tls_enabled_on_nonlocal_bind(self, tmp_path: Path) -> None:
        import warnings as warnings_mod

        parser = _build_parser()
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.touch()
        key.touch()
        args = parser.parse_args(
            [
                "serve",
                "--outdir",
                str(tmp_path),
                "--host",
                "0.0.0.0",
                "--api-key",
                "test-key-1095",
                "--tls-cert",
                str(cert),
                "--tls-key",
                str(key),
            ]
        )
        mock_app = MagicMock()
        mock_uvicorn = MagicMock()

        with (
            patch.dict(sys.modules, {"uvicorn": mock_uvicorn}),
            patch("osimflow.api.create_app", return_value=mock_app),
            warnings_mod.catch_warnings(),
        ):
            warnings_mod.simplefilter("error", UserWarning)
            result = _cmd_serve(args)

        assert result == 0
        assert mock_uvicorn.run.call_args[1]["ssl_certfile"] == cert


class TestIsLocalHost:
    def test_loopback_variants(self) -> None:
        from osimflow.__main__ import _is_local_host

        assert _is_local_host("127.0.0.1") is True
        assert _is_local_host("localhost") is True
        assert _is_local_host("LOCALHOST") is True
        assert _is_local_host("::1") is True
        assert _is_local_host("[::1]") is True
        assert _is_local_host("127.5.5.5") is True

    def test_non_local_variants(self) -> None:
        from osimflow.__main__ import _is_local_host

        assert _is_local_host("0.0.0.0") is False
        assert _is_local_host("*") is False
        assert _is_local_host("") is False
        assert _is_local_host("10.0.0.5") is False
        assert _is_local_host("api.example.com") is False
