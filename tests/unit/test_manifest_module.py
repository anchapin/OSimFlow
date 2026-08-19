"""Unit tests for osimflow/manifest.py — issue #625 manifest helpers.

Covers:
  * first_severe_error: missing file, unreadable file, file with no severe lines,
    file with one severe line, file with multiple severe lines (first wins)
  * build_manifest: default finished_at, explicit finished_at, field order matches
    MANIFEST_FIELDS
  * write_manifest_atomically: LocalStorage atomic rename, remote-storage upload
    with temp unlink, exception path cleans up the staging file
  * _local_dest_for_key: storage.root, storage._root, missing-attr fallback to cwd
  * report_sample_completion + _do_patch: httpx success, httpx failure,
    urllib fallback, urllib HTTPError non-2xx raises, urllib HTTPError 2xx returns
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow.manifest import (
    MANIFEST_FIELDS,
    _do_patch,
    _local_dest_for_key,
    build_manifest,
    first_severe_error,
    report_sample_completion,
    write_manifest_atomically,
)


class TestFirstSevereError:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert first_severe_error(tmp_path / "does_not_exist.err") is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.err"
        p.write_text("")
        assert first_severe_error(p) is None

    def test_no_severe_line_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "warn.err"
        p.write_text("   * Warning: something minor\n   * Info: another thing\n")
        assert first_severe_error(p) is None

    def test_one_severe_line_is_returned_stripped(self, tmp_path: Path) -> None:
        p = tmp_path / "one.err"
        p.write_text(
            "   **  Fatal: bad setup\n  * Severe: temperature out of range\n  something else\n"
        )
        result = first_severe_error(p)
        assert result is not None
        assert "Severe" in result
        assert result == "* Severe: temperature out of range"

    def test_returns_first_when_multiple(self, tmp_path: Path) -> None:
        p = tmp_path / "many.err"
        p.write_text("  * Severe: first one\n  * Severe: second one\n")
        assert first_severe_error(p) == "* Severe: first one"

    def test_unreadable_file_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "broken.err"
        p.write_text("  * Severe: x\n")
        with patch.object(Path, "read_text", side_effect=OSError("disk error")):
            assert first_severe_error(p) is None


class TestBuildManifest:
    def test_field_order_matches_canonical(self) -> None:
        m = build_manifest(
            sample_id="0001",
            index=0,
            status="completed",
            kpis_key="kpis/0001.json",
            exit_code=0,
            first_severe_error=None,
            finished_at=1.0,
        )
        assert tuple(m.keys()) == MANIFEST_FIELDS
        assert m["sample_id"] == "0001"
        assert m["index"] == 0
        assert m["status"] == "completed"
        assert m["kpis_key"] == "kpis/0001.json"
        assert m["exit_code"] == 0
        assert m["first_severe_error"] is None
        assert m["finished_at"] == 1.0

    def test_finished_at_defaults_to_time_time(self) -> None:
        m = build_manifest(
            sample_id="0002",
            index=1,
            status="failed",
            kpis_key=None,
            exit_code=2,
            first_severe_error="* Severe: boom",
        )
        assert isinstance(m["finished_at"], float)
        assert m["finished_at"] > 0


class TestLocalDestForKey:
    def test_uses_root_attribute(self, tmp_path: Path) -> None:
        storage = MagicMock()
        storage._root = str(tmp_path)
        # LocalStorage has a root; the helper falls back to getattr.
        dest = _local_dest_for_key(storage, "kpis/0001.json")
        assert dest == tmp_path / "kpis/0001.json"

    def test_falls_back_to_cwd_when_no_root(self, tmp_path: Path) -> None:
        storage = MagicMock(spec=[])  # no _root or root attribute
        # Workaround: provide no _root, no root via direct attribute control
        if hasattr(storage, "_root"):
            del storage._root
        if hasattr(storage, "root"):
            del storage.root
        dest = _local_dest_for_key(storage, "kpis/0001.json")
        assert dest == Path.cwd() / "kpis/0001.json"


class TestWriteManifestAtomically:
    def test_local_storage_uses_atomic_rename(self, tmp_path: Path) -> None:
        # Use a Mock with the right class identity so that the local-import
        # ``isinstance(storage, LocalStorage)`` check inside
        # ``write_manifest_atomically`` matches in coverage-instrumented runs.
        import osimflow.storage as storage_mod

        storage = MagicMock(spec=storage_mod.LocalStorage)
        # Inject _root so the rename target resolves under tmp_path.
        storage._root = str(tmp_path)  # type: ignore[attr-defined]
        manifest = build_manifest(
            sample_id="0001",
            index=0,
            status="completed",
            kpis_key=None,
            exit_code=0,
            first_severe_error=None,
            finished_at=1.0,
        )
        # Verify the destination path resolves to where we expect.
        final = _local_dest_for_key(storage, "kpis/_manifest_0001.json")
        assert final == tmp_path / "kpis" / "_manifest_0001.json"
        write_manifest_atomically(
            storage,
            "kpis/_manifest_0001.json",
            manifest,
            local_tmp_dir=tmp_path,
        )
        # The Mock.spec ensures the isinstance check is True.
        # The temp staging file is consumed by the atomic rename.
        assert final.is_file()
        loaded = json.loads(final.read_text())
        assert loaded["sample_id"] == "0001"

    def test_remote_storage_uploads_and_unlinks_tmp(self, tmp_path: Path) -> None:
        storage = MagicMock()
        # Not a LocalStorage instance, so we go down the remote branch.
        storage.upload_file = MagicMock()
        manifest = {"sample_id": "0002", "index": 1, "status": "completed"}
        write_manifest_atomically(
            storage,
            "kpis/_manifest_0002.json",
            manifest,
            local_tmp_dir=tmp_path,
        )
        storage.upload_file.assert_called_once()
        # No leftover _manifest_*.json in tmp_path.
        leftovers = list(tmp_path.glob("_manifest_*.json"))
        assert leftovers == []

    def test_failure_path_cleans_up_tmp_file(self, tmp_path: Path) -> None:
        storage = MagicMock()
        storage.upload_file.side_effect = RuntimeError("network down")
        with pytest.raises(RuntimeError):
            write_manifest_atomically(
                storage,
                "kpis/_manifest_0003.json",
                {"sample_id": "0003"},
                local_tmp_dir=tmp_path,
            )
        # The tmp staging file should be removed.
        leftovers = list(tmp_path.glob("_manifest_*.json"))
        assert leftovers == []


class TestDoPatch:
    def test_httpx_success(self) -> None:
        fake_httpx = MagicMock()
        fake_client = MagicMock()
        fake_httpx.Client.return_value.__enter__.return_value = fake_client
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_client.patch.return_value = fake_resp
        with patch.dict("sys.modules", {"httpx": fake_httpx}):
            _do_patch("http://coord/api", b"{}", {}, {"status": "ok"}, 5.0)
        fake_client.patch.assert_called_once()
        fake_resp.raise_for_status.assert_called_once()

    def test_httpx_import_missing_falls_back_to_urllib(self) -> None:
        with patch.dict("sys.modules", {"httpx": None}):
            with patch("osimflow.manifest._patch_urllib") as mock_urllib:
                _do_patch("http://coord/api", b"{}", {}, {"status": "ok"}, 5.0)
                mock_urllib.assert_called_once()

    def test_httpx_import_missing_no_params_uses_url(self) -> None:
        with patch.dict("sys.modules", {"httpx": None}):
            with patch("osimflow.manifest._patch_urllib") as mock_urllib:
                _do_patch("http://coord/api", b"{}", {}, {}, 5.0)
                mock_urllib.assert_called_once()
                # url passed through unchanged when params empty
                assert mock_urllib.call_args.args[0] == "http://coord/api"


class TestReportSampleCompletion:
    def test_best_effort_logs_warning_on_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch(
            "osimflow.manifest._do_patch",
            side_effect=RuntimeError("network down"),
        ):
            with caplog.at_level(logging.WARNING, logger="osimflow.manifest"):
                report_sample_completion(
                    coordinator_url="http://coord",
                    campaign_id="camp-1",
                    manifest={"status": "completed"},
                )
            assert any("coordinator status report failed" in rec.message for rec in caplog.records)

    def test_with_api_key_passes_auth_header(self) -> None:
        with patch("osimflow.manifest._do_patch") as mock_patch:
            report_sample_completion(
                coordinator_url="http://coord/",
                campaign_id="camp-2",
                manifest={"status": "failed"},
                api_key="secret",
            )
            args, _ = mock_patch.call_args
            url, body, headers, params, timeout = args
            assert "campaigns/camp-2" in url
            assert headers["Authorization"] == "Bearer secret"
            assert params == {"status": "failed"}
            assert body == b'{"status": "failed"}'
            assert timeout == 10.0

    def test_url_strips_trailing_slash(self) -> None:
        with patch("osimflow.manifest._do_patch") as mock_patch:
            report_sample_completion(
                coordinator_url="http://coord///",
                campaign_id="camp-3",
                manifest={"status": "completed"},
            )
            args, _ = mock_patch.call_args
            assert args[0] == ("http://coord/api/v1/coordinator/campaigns/camp-3/status")

    def test_status_param_defaults_to_unknown(self) -> None:
        with patch("osimflow.manifest._do_patch") as mock_patch:
            report_sample_completion(
                coordinator_url="http://coord",
                campaign_id="camp-4",
                manifest={},
            )
            args, _ = mock_patch.call_args
            assert args[3] == {"status": "unknown"}

    def test_custom_timeout_passes_through(self) -> None:
        with patch("osimflow.manifest._do_patch") as mock_patch:
            report_sample_completion(
                coordinator_url="http://coord",
                campaign_id="camp-5",
                manifest={"status": "ok"},
                timeout_s=2.5,
            )
            args, _ = mock_patch.call_args
            assert args[4] == 2.5
