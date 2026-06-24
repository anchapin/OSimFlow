"""Unit tests for the CLI ``--detach`` handoff + ``status``/``download`` reconnect
UX (issue #630, Epic #624).

These exercise the orchestration in ``osimflow.__main__`` (the helpers
``_perform_detach_handoff``, ``_print_coordinator_status``,
``_download_coordinator_results``, and the ``_cmd_status`` / ``_cmd_download``
dispatch) with a stubbed HTTP transport, so they need no real Coordinator.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from osimflow import __main__ as cli
from osimflow.config import CampaignConfig
from osimflow.handoff_record import (
    HANDOFF_RECORD_NAME,
    HandoffRecord,
    write_handoff_record,
)

COORD_URL = "https://coord.example.com"
CAMP_ID = "camp-xyz-123"
STATUS_URL = f"{COORD_URL}/api/v1/coordinator/campaigns/{CAMP_ID}"


# ---------------------------------------------------------------------------
# Stubs / helpers
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for an ``httpx.Response``."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: dict[str, Any] | None = None,
        text: str = "",
        content: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text or json.dumps(self._json)
        self.content = content

    def json(self) -> dict[str, Any]:
        return self._json


def _record(outdir: Path, **overrides: object) -> HandoffRecord:
    base = dict(
        campaign_id=CAMP_ID,
        coordinator_url=COORD_URL,
        submitted_at=1700000000.0,
        status_url=STATUS_URL,
        idempotency_key="osimflow-test-key",
    )
    base.update(overrides)
    rec = HandoffRecord(**base)  # type: ignore[arg-type]
    write_handoff_record(outdir, rec)
    return rec


def _minimal_cfg(outdir: Path) -> CampaignConfig:
    """A CampaignConfig with just the required fields populated."""
    return CampaignConfig(
        input_variables=Path("variables.yml"),
        template_sim_package=Path("pkg"),
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
    )


def _detach_args(outdir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        detach=True,
        coordinator_url=COORD_URL,
        executor="aws_batch",
        custom_apply_script=None,
        custom_kpi_extractor=None,
        outdir=outdir,
    )


# ---------------------------------------------------------------------------
# _perform_detach_handoff
# ---------------------------------------------------------------------------


class TestDetachHandoff:
    def test_writes_record_and_prints_status_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        outdir = tmp_path / "results"
        monkeypatch.setattr(cli, "load_config", lambda _vars: _minimal_cfg(outdir))

        captured: dict[str, Any] = {}

        def fake_post(url, **kw):
            captured["url"] = url
            captured["headers"] = kw.get("headers", {})
            captured["json"] = kw.get("json")
            return FakeResponse(
                status_code=202,
                json_data={"campaign_id": CAMP_ID, "status": "pending", "status_url": STATUS_URL},
            )

        monkeypatch.setattr(cli.httpx, "post", fake_post)

        rc = cli._perform_detach_handoff(_detach_args(outdir))

        assert rc == 0
        # Posted to the handoff endpoint with an Idempotency-Key header.
        assert captured["url"] == f"{COORD_URL}/api/v1/coordinator/handoff"
        assert "Idempotency-Key" in captured["headers"]
        assert captured["headers"]["Idempotency-Key"].startswith("osimflow-")
        # Record written under outdir.
        record = json.loads((outdir / HANDOFF_RECORD_NAME).read_text())
        assert record["campaign_id"] == CAMP_ID
        assert record["coordinator_url"] == COORD_URL
        assert record["status_url"] == STATUS_URL
        # Output prints campaign_id + status_url and the reconnect hint.
        out = capsys.readouterr().out
        assert CAMP_ID in out
        assert STATUS_URL in out
        assert "osimflow status" in out

    def test_idempotent_when_record_already_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        outdir = tmp_path / "results"
        _record(outdir)  # pre-existing handoff record

        def fail_if_called(*a, **kw):  # noqa: ANN002
            raise AssertionError("httpx.post must not be called when a record exists")

        monkeypatch.setattr(cli.httpx, "post", fail_if_called)

        rc = cli._perform_detach_handoff(_detach_args(outdir))
        assert rc == 0
        out = capsys.readouterr().out
        assert "already handed off" in out
        assert CAMP_ID in out

    def test_unreachable_coordinator_returns_1_no_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        outdir = tmp_path / "results"
        monkeypatch.setattr(cli, "load_config", lambda _vars: _minimal_cfg(outdir))
        monkeypatch.setattr(
            cli.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("boom"))
        )

        rc = cli._perform_detach_handoff(_detach_args(outdir))
        assert rc == 1
        assert not (outdir / HANDOFF_RECORD_NAME).exists()  # no partial state
        err = capsys.readouterr().err
        assert "could not reach" in err
        assert "idempotent" in err

    def test_4xx_rejection_creates_no_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        outdir = tmp_path / "results"
        monkeypatch.setattr(cli, "load_config", lambda _vars: _minimal_cfg(outdir))
        monkeypatch.setattr(
            cli.httpx,
            "post",
            lambda *a, **k: FakeResponse(status_code=400, text="bad config"),
        )

        rc = cli._perform_detach_handoff(_detach_args(outdir))
        assert rc == 1
        assert not (outdir / HANDOFF_RECORD_NAME).exists()
        err = capsys.readouterr().err
        assert "HTTP 400" in err
        assert "No campaign was created" in err

    def test_5xx_documents_idempotent_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        outdir = tmp_path / "results"
        monkeypatch.setattr(cli, "load_config", lambda _vars: _minimal_cfg(outdir))
        monkeypatch.setattr(
            cli.httpx,
            "post",
            lambda *a, **k: FakeResponse(status_code=500, text="internal"),
        )

        rc = cli._perform_detach_handoff(_detach_args(outdir))
        assert rc == 1
        assert not (outdir / HANDOFF_RECORD_NAME).exists()
        err = capsys.readouterr().err
        assert "HTTP 500" in err
        assert "Idempotency-Key" in err  # recovery path documented

    def test_missing_coordinator_url_returns_1(self, tmp_path: Path, capsys) -> None:
        args = _detach_args(tmp_path)
        args.coordinator_url = ""
        rc = cli._perform_detach_handoff(args)
        assert rc == 1
        assert "--detach requires --coordinator-url" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _cmd_status (Coordinator reconnect)
# ---------------------------------------------------------------------------


class TestStatusReconnect:
    def test_status_polls_coordinator_for_live_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        outdir = tmp_path / "results"
        _record(outdir)
        monkeypatch.setattr(
            cli.httpx,
            "get",
            lambda url, **kw: FakeResponse(
                status_code=200,
                json_data={
                    "campaign_id": CAMP_ID,
                    "name": "my-campaign",
                    "status": "running",
                    "executor": "aws_batch",
                    "openstudio_version": "3.11.0",
                    "n_samples": 3,
                    "created_at": 1700000000.0,
                },
            ),
        )
        args = argparse.Namespace(outdir=outdir, log_level="INFO")
        rc = cli._cmd_status(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Coordinator-backed campaign" in out
        assert "running" in out
        assert CAMP_ID in out

    def test_status_no_record_no_runjson_errors_with_detach_hint(
        self, tmp_path: Path, capsys
    ) -> None:
        outdir = tmp_path / "empty"
        outdir.mkdir()
        args = argparse.Namespace(outdir=outdir, log_level="INFO")
        rc = cli._cmd_status(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "run.json not found" in err
        assert "--detach" in err

    def test_status_coordinator_404_reports_missing_campaign(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        outdir = tmp_path / "results"
        _record(outdir)
        monkeypatch.setattr(
            cli.httpx, "get", lambda url, **kw: FakeResponse(status_code=404, text="nope")
        )
        args = argparse.Namespace(outdir=outdir, log_level="INFO")
        rc = cli._cmd_status(args)
        assert rc == 1
        assert "404" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _download_coordinator_results (aggregated-only via presigned URL)
# ---------------------------------------------------------------------------


class TestDownloadAggregatedOnly:
    def _fetch_dispatcher(self, presigned_url: str, csv_bytes: bytes, results_body: dict[str, Any]):
        def fetch(url, **kw):
            if url.endswith("/results"):
                return FakeResponse(status_code=200, json_data=results_body)
            if url == presigned_url:
                return FakeResponse(status_code=200, content=csv_bytes)
            raise AssertionError(f"unexpected fetch URL: {url}")

        return fetch

    def test_downloads_only_aggregated_csv(self, tmp_path: Path, capsys) -> None:
        outdir = tmp_path / "results"
        rec = _record(outdir)
        output_dir = tmp_path / "out"
        presigned = "https://s3.example.com/presigned-aggregated"
        csv_bytes = b"sample_id,eui\n0,120.5\n1,118.2\n"
        results_body = {
            "campaign_id": CAMP_ID,
            "status": "complete",
            "result_bucket": "bkt",
            "aggregated_results_key": "results/aggregated_results.csv",
            "aggregated_results_url": presigned,
            "kpi_files": [{"sample_index": 0, "file_key": "results/kpi_0.json"}],
            "message": "ok",
        }

        rc = cli._download_coordinator_results(
            rec, output_dir, http_get=self._fetch_dispatcher(presigned, csv_bytes, results_body)
        )
        assert rc == 0
        dest = output_dir / "aggregated_results.csv"
        assert dest.exists()
        assert dest.read_bytes() == csv_bytes
        # Per-sample bytes are NOT downloaded — only aggregated_results.csv exists.
        assert sorted(p.name for p in output_dir.iterdir()) == ["aggregated_results.csv"]
        assert "not downloaded" in capsys.readouterr().out.lower()

    def test_no_aggregated_yet_returns_1_with_hint(self, tmp_path: Path, capsys) -> None:
        outdir = tmp_path / "results"
        rec = _record(outdir)
        results_body = {
            "campaign_id": CAMP_ID,
            "status": "running",
            "result_bucket": "bkt",
            "aggregated_results_key": None,
            "aggregated_results_url": None,
            "kpi_files": [],
            "message": "still running",
        }
        rc = cli._download_coordinator_results(
            rec, tmp_path / "out", http_get=self._fetch_dispatcher("", b"", results_body)
        )
        assert rc == 1
        out = capsys.readouterr().out.lower()
        assert "no aggregated results" in out
        assert "not finished" in out

    def test_results_endpoint_error_returns_1(self, tmp_path: Path, capsys) -> None:
        outdir = tmp_path / "results"
        rec = _record(outdir)
        rc = cli._download_coordinator_results(
            rec,
            tmp_path / "out",
            http_get=lambda url, **kw: FakeResponse(status_code=500, text="boom"),
        )
        assert rc == 1
        assert "HTTP 500" in capsys.readouterr().err
