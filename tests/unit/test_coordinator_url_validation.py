"""Unit tests for Coordinator URL HTTPS enforcement (issue #1550).

``Campaign._coordinator_url`` accepts ``cfg.coordinator_url`` or the
``OSIMFLOW_COORDINATOR_URL`` env var, and
``osimflow.manifest.report_sample_completion`` PATCHes per-sample results
to it with an ``Authorization: Bearer $OSIMFLOW_API_KEY`` header.  An
``http://`` non-loopback coordinator URL therefore leaks the bearer key
and campaign results in cleartext on every sample completion.

``_validate_coordinator_url`` (in ``osimflow/manifest.py``, next to the
coordinator HTTP client) mirrors ``_validate_storage_endpoint`` (#1386):
fail-closed, loopback-exempt, with the existing
``--allow-insecure-storage-endpoint`` flag reused as the explicit dev
opt-in (no new CLI surface).

Covered here:
* the validator itself (accept / reject matrix),
* the refuse-to-send guard in ``report_sample_completion``,
* the fail-fast gate at ``Campaign.run()`` entry,
* the ``osimflow run --detach`` handoff client gate.
"""

import argparse
import logging
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.__main__ import _perform_detach_handoff
from osimflow.executors import LocalExecutor
from osimflow.manifest import _validate_coordinator_url, report_sample_completion

# ---------------------------------------------------------------------------
# Validator: reject / accept matrix (mirrors _validate_storage_endpoint).
# ---------------------------------------------------------------------------


def test_non_loopback_http_rejected() -> None:
    with pytest.raises(ValueError, match="issue #1550"):
        _validate_coordinator_url("http://10.0.0.5:8080")


def test_error_names_the_override_flag() -> None:
    with pytest.raises(ValueError) as excinfo:
        _validate_coordinator_url("http://coord.example.com")
    assert "--allow-insecure-storage-endpoint" in str(excinfo.value)


def test_non_http_scheme_rejected() -> None:
    with pytest.raises(ValueError, match="expected 'https://'"):
        _validate_coordinator_url("ftp://coord.example.com")


def test_https_accepted() -> None:
    _validate_coordinator_url("https://coord.example.com")  # must not raise


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
        "http://0.0.0.0:8000",
        "http://127.0.0.5:8000",  # 127.0.0.0/8, not just .1
    ],
)
def test_loopback_exempt(url: str) -> None:
    _validate_coordinator_url(url)  # must not raise


def test_non_loopback_http_with_override_accepted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        _validate_coordinator_url("http://10.0.0.5:8080", allow_insecure=True)
    assert "INSECURE" in caplog.text


@pytest.mark.parametrize("url", [None, ""])
def test_unconfigured_coordinator_is_noop(url: str | None) -> None:
    _validate_coordinator_url(url)  # must not raise


# ---------------------------------------------------------------------------
# Client-side guard: report_sample_completion refuses to send over
# plaintext HTTP (defense in depth for workers that bypass Campaign.run).
# ---------------------------------------------------------------------------


def test_report_sample_completion_refuses_insecure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sent: list[str] = []

    def _fail_if_called(url: str, *_args: object, **_kwargs: object) -> None:
        sent.append(url)

    monkeypatch.setattr("osimflow.manifest._do_patch", _fail_if_called)
    with caplog.at_level(logging.ERROR):
        report_sample_completion(
            coordinator_url="http://10.0.0.5:8080",
            campaign_id="c-1550",
            manifest={"status": "completed"},
            api_key="secret-bearer-key",
        )
    assert sent == [], "PATCH must not be issued over insecure coordinator URL"
    assert "insecure coordinator URL" in caplog.text


def test_report_sample_completion_sends_with_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    def _record(url: str, *_args: object, **_kwargs: object) -> None:
        sent.append(url)

    monkeypatch.setattr("osimflow.manifest._do_patch", _record)
    report_sample_completion(
        coordinator_url="http://10.0.0.5:8080",
        campaign_id="c-1550",
        manifest={"status": "completed"},
        allow_insecure=True,
    )
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# Campaign-start wiring: fail fast before any step executes.
# ---------------------------------------------------------------------------


def _make_campaign(
    outdir: Path, campaign_workdir: Path, template_pkg: Path, **extra: object
) -> Campaign:
    cfg = CampaignConfig(
        input_variables=campaign_workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=1,
        outdir=outdir,
        openstudio_version="3.11.0",
        dry_run=True,
        **extra,  # type: ignore[arg-type]
    )
    return Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))


def test_campaign_start_rejects_insecure_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    campaign_workdir: Path,
    template_pkg: Path,
    outdir: Path,
) -> None:
    monkeypatch.setenv("OSIMFLOW_COORDINATOR_URL", "http://10.0.0.5:8080")
    campaign = _make_campaign(outdir, campaign_workdir, template_pkg)

    with pytest.raises(ValueError, match="issue #1550"):
        campaign.run()

    # Fail fast: nothing was executed, no run.json status churn expected
    # beyond the campaign-start gate.
    assert not (outdir / "aggregated_results.csv").exists()


def test_campaign_start_accepts_insecure_coordinator_with_override(
    monkeypatch: pytest.MonkeyPatch,
    campaign_workdir: Path,
    template_pkg: Path,
    outdir: Path,
) -> None:
    monkeypatch.setenv("OSIMFLOW_COORDINATOR_URL", "http://10.0.0.5:8080")
    campaign = _make_campaign(
        outdir,
        campaign_workdir,
        template_pkg,
        allow_insecure_storage_endpoint=True,
    )

    result = campaign.run()  # must not raise

    assert len(result["samples"]) == 1


def test_campaign_start_accepts_loopback_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    campaign_workdir: Path,
    template_pkg: Path,
    outdir: Path,
) -> None:
    monkeypatch.setenv("OSIMFLOW_COORDINATOR_URL", "http://localhost:8000")
    campaign = _make_campaign(outdir, campaign_workdir, template_pkg)

    result = campaign.run()  # must not raise

    assert len(result["samples"]) == 1


# ---------------------------------------------------------------------------
# --detach handoff client wiring (the exact user group the issue calls out).
# Mirrors the stubbed-transport pattern from test_cli_coordinator_reconnect.
# ---------------------------------------------------------------------------


def _detach_args(outdir: Path, coordinator_url: str, **extra: object) -> argparse.Namespace:
    return argparse.Namespace(
        detach=True,
        coordinator_url=coordinator_url,
        executor="aws_batch",
        custom_apply_script=None,
        custom_kpi_extractor=None,
        outdir=outdir,
        **extra,  # type: ignore[arg-type]
    )


def test_detach_handoff_rejects_insecure_coordinator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _detach_args(tmp_path / "out", "http://10.0.0.5:8080")

    assert _perform_detach_handoff(args) == 1

    err = capsys.readouterr().err
    assert "issue #1550" in err
    assert "--allow-insecure-storage-endpoint" in err


def test_detach_handoff_accepts_insecure_with_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The override lets the handoff proceed past the scheme gate."""
    from osimflow import __main__ as cli
    from osimflow.config import CampaignConfig

    outdir = tmp_path / "out"
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _vars: CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("pkg"),
            n_samples=1,
            outdir=outdir,
            openstudio_version="3.11.0",
        ),
    )
    posted: list[str] = []
    monkeypatch.setattr(
        cli.httpx,
        "post",
        lambda url, **kw: (
            posted.append(url)
            or _FakeResponse(202, {"campaign_id": "c-1550", "status_url": "http://x/s"})
        ),
    )
    args = _detach_args(outdir, "http://10.0.0.5:8080", allow_insecure_storage_endpoint=True)

    assert _perform_detach_handoff(args) == 0

    assert len(posted) == 1, "handoff POST must be attempted once the override is set"


class _FakeResponse:
    """Minimal stand-in for the httpx.Response the handoff path expects."""

    def __init__(self, status_code: int, json_data: dict[str, str]) -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = ""

    def json(self) -> dict[str, str]:
        return self._json
