"""Tests for offline-bundle manifest verification (issue #1640).

``scripts/bundle_offline.py`` writes ``bundle_manifest.json`` with a
SHA-256 digest per bundled asset. These tests pin the fail-closed
verification contract: every listed asset is verified before the CLI
consumes the bundle, and any mismatch / missing manifest / missing
asset aborts the run naming the offending asset.
"""

import hashlib
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from osimflow.__main__ import main
from osimflow.offline_bundle import (
    MANIFEST_FILENAME,
    OfflineBundleError,
    verify_offline_bundle,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_WHEEL_NAME = "osimflow-1.0.0-py3-none-any.whl"
_TAR_NAME = "nrel_openstudio_3.11.0.tar"
_EPW_NAME = "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_bundle(root: Path) -> Path:
    """Build a tiny fake offline bundle with a correct manifest."""
    bundle = root / "offline"
    (bundle / "pip").mkdir(parents=True)
    (bundle / "docker").mkdir()
    (bundle / "weather").mkdir()
    wheel = bundle / "pip" / _WHEEL_NAME
    tar = bundle / "docker" / _TAR_NAME
    epw = bundle / "weather" / _EPW_NAME
    wheel.write_bytes(b"fake wheel payload")
    tar.write_bytes(b"fake docker tar payload")
    epw.write_bytes(b"fake epw payload")
    manifest = {
        "created_at": "2026-01-01T00:00:00+00:00",
        "openstudio_version": "3.11.0",
        "pip_extras": "dev",
        "pip_wheels": [{"name": _WHEEL_NAME, "sha256": _sha256(wheel)}],
        "docker_images": [{"name": _TAR_NAME, "sha256": _sha256(tar)}],
        "weather_files": [{"name": _EPW_NAME, "sha256": _sha256(epw)}],
    }
    (bundle / MANIFEST_FILENAME).write_text(json.dumps(manifest))
    return bundle


class TestVerifyOfflineBundle:
    """Unit tests for :func:`osimflow.offline_bundle.verify_offline_bundle`."""

    def test_valid_bundle_passes_and_returns_manifest(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path)
        manifest = verify_offline_bundle(bundle)
        assert manifest["openstudio_version"] == "3.11.0"
        assert [e["name"] for e in manifest["pip_wheels"]] == [_WHEEL_NAME]

    def test_single_mutated_byte_raises_naming_asset(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path)
        wheel = bundle / "pip" / _WHEEL_NAME
        data = bytearray(wheel.read_bytes())
        data[0] ^= 0xFF  # flip exactly one byte of the bundled wheel
        wheel.write_bytes(bytes(data))
        with pytest.raises(OfflineBundleError, match=_WHEEL_NAME):
            verify_offline_bundle(bundle)

    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path)
        (bundle / MANIFEST_FILENAME).unlink()
        with pytest.raises(OfflineBundleError, match=MANIFEST_FILENAME):
            verify_offline_bundle(bundle)

    def test_missing_asset_raises(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path)
        (bundle / "docker" / _TAR_NAME).unlink()
        with pytest.raises(OfflineBundleError, match=_TAR_NAME):
            verify_offline_bundle(bundle)

    def test_extra_unlisted_file_is_ok(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path)
        (bundle / "pip" / "notes.txt").write_text("unlisted extra file")
        manifest = verify_offline_bundle(bundle)
        assert manifest["pip_extras"] == "dev"

    def test_missing_bundle_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(OfflineBundleError, match="not found"):
            verify_offline_bundle(tmp_path / "no-such-bundle")

    def test_invalid_manifest_json_raises(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path)
        (bundle / MANIFEST_FILENAME).write_text("{not json")
        with pytest.raises(OfflineBundleError, match="not valid JSON"):
            verify_offline_bundle(bundle)

    def test_malformed_entry_raises(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path)
        manifest = json.loads((bundle / MANIFEST_FILENAME).read_text())
        manifest["pip_wheels"][0].pop("sha256")
        (bundle / MANIFEST_FILENAME).write_text(json.dumps(manifest))
        with pytest.raises(OfflineBundleError, match="malformed entry"):
            verify_offline_bundle(bundle)

    def test_path_traversal_asset_name_rejected(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path)
        manifest = json.loads((bundle / MANIFEST_FILENAME).read_text())
        manifest["weather_files"][0]["name"] = "../secrets.epw"
        (bundle / MANIFEST_FILENAME).write_text(json.dumps(manifest))
        with pytest.raises(OfflineBundleError, match="bare"):
            verify_offline_bundle(bundle)

    def test_error_is_osimflow_and_runtime_error(self, tmp_path: Path) -> None:
        """OfflineBundleError keeps the package-root + RuntimeError contract."""
        from osimflow.errors import OSimFlowError

        bundle = _make_bundle(tmp_path)
        (bundle / MANIFEST_FILENAME).unlink()
        with pytest.raises(OfflineBundleError) as exc_info:
            verify_offline_bundle(bundle)
        assert isinstance(exc_info.value, OSimFlowError)
        assert isinstance(exc_info.value, RuntimeError)


def _cli_run_args(bundle: Path, tmp_path: Path) -> list[str]:
    pkg = tmp_path / "pkg"
    if not pkg.exists():
        shutil.copytree(REPO_ROOT / "example_package", pkg)
    return [
        "run",
        "--executor",
        "local",
        "--input_variables",
        str(pkg / "variables.yml"),
        "--template_sim_package",
        str(pkg),
        "--n_samples",
        "1",
        "--outdir",
        str(tmp_path / "out"),
        "--offline-bundle",
        str(bundle),
        "--no-tui",
    ]


class TestCliOfflineBundleGate:
    """The `osimflow run` CLI verifies the bundle before the campaign starts."""

    def test_tampered_bundle_exits_1_before_campaign(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bundle = _make_bundle(tmp_path)
        wheel = bundle / "pip" / _WHEEL_NAME
        data = bytearray(wheel.read_bytes())
        data[3] ^= 0x01
        wheel.write_bytes(bytes(data))

        campaign_mock = MagicMock(name="Campaign")
        monkeypatch.setattr("osimflow.__main__.Campaign", campaign_mock)

        with pytest.raises(SystemExit) as exc_info:
            main(_cli_run_args(bundle, tmp_path))

        assert exc_info.value.code == 1
        campaign_mock.assert_not_called()
        stderr = capsys.readouterr().err
        assert _WHEEL_NAME in stderr
        assert "SHA-256" in stderr

    def test_valid_bundle_proceeds_past_gate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bundle = _make_bundle(tmp_path)

        campaign_mock = MagicMock(name="Campaign")
        campaign_mock.return_value.run.return_value = {
            "elapsed_s": 0.01,
            "kpis": [],
            "aggregated": {},
            "plots": [],
            "run_json": str(tmp_path / "out" / "run.json"),
        }
        monkeypatch.setattr("osimflow.__main__.Campaign", campaign_mock)

        args = _cli_run_args(bundle, tmp_path) + ["--dry-run"]
        rc = main(args)

        assert rc == 0
        campaign_mock.assert_called_once()
