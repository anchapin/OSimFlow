"""Tests for the ``osimflow resume`` cache-replay recovery (issue #1628).

Pre-#1628, ``_cmd_resume`` only unlinked ``outdir/.pause`` and printed
success — but a paused campaign has no live orchestrator (``run()``
returns and the CLI process exits, issue #1537), so the campaign was
stranded with ``run.json`` status ``"paused"`` forever.  The fix:

- ``osimflow run`` records its exact invocation to
  ``<outdir>/campaign_invocation.json`` at start, and
- ``osimflow resume`` removes ``.pause`` and re-launches that recorded
  command (cache replay), falling back to printing the manual recovery
  command (``osimflow run --outdir <same>``) when no record exists.

Run via::

    .venv/bin/pytest tests/unit/test_resume_cli.py -v
"""

import json
from pathlib import Path
from typing import Any

import osimflow.__main__ as cli
from osimflow.__main__ import (
    _build_parser,
    _cmd_resume,
    _persist_run_invocation,
    _replay_paused_campaign,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

PAUSED_RUN_JSON: dict[str, Any] = {
    "schema_version": 1,
    "campaign_id": "camp-resume",
    "status": "paused",
    "finished_at": None,
}


def _write_paused_run_json(outdir: Path, status: str = "paused") -> None:
    payload = dict(PAUSED_RUN_JSON)
    payload["status"] = status
    (outdir / "run.json").write_text(json.dumps(payload))


def _resume(outdir: Path) -> int:
    parser = _build_parser()
    args = parser.parse_args(["resume", str(outdir)])
    return _cmd_resume(args)


# ---------------------------------------------------------------------------
# Invocation record (written by `osimflow run`, read by `osimflow resume`)
# ---------------------------------------------------------------------------


class TestInvocationRecord:
    def test_persist_run_invocation_records_argv_and_cwd(self, tmp_path: Path) -> None:
        od = tmp_path / "nested" / "out"  # not yet created — helper must mkdir
        _persist_run_invocation(["run", "--n_samples", "3"], od)
        record = json.loads((od / "campaign_invocation.json").read_text())
        assert record["argv"] == ["run", "--n_samples", "3"]
        assert Path(record["cwd"]) == Path.cwd()

    def test_replay_returns_none_for_missing_or_unusable_record(self, outdir: Path) -> None:
        # No record at all (programmatic / API / pre-#1628 campaign).
        assert _replay_paused_campaign(outdir) is None
        # Corrupt JSON.
        (outdir / "campaign_invocation.json").write_text("{not json")
        assert _replay_paused_campaign(outdir) is None
        # Non-`run` argv must never be spawned.
        record = {"argv": ["serve"], "cwd": str(outdir)}
        (outdir / "campaign_invocation.json").write_text(json.dumps(record))
        assert _replay_paused_campaign(outdir) is None
        # Recorded cwd no longer exists — relative paths would mis-resolve.
        record = {"argv": ["run", "--outdir", "rel"], "cwd": "/nonexistent/cwd"}
        (outdir / "campaign_invocation.json").write_text(json.dumps(record))
        assert _replay_paused_campaign(outdir) is None


# ---------------------------------------------------------------------------
# `osimflow resume` CLI states (fast, no subprocess)
# ---------------------------------------------------------------------------


class TestResumeCliStates:
    def test_resume_removes_pause_flag_and_drives_replay(
        self, outdir: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        _write_paused_run_json(outdir)
        (outdir / ".pause").write_text("{}")
        _persist_run_invocation(["run", "--outdir", str(outdir)], outdir)

        replayed: list[Path] = []
        monkeypatch.setattr(cli, "_replay_paused_campaign", lambda od: replayed.append(od) or 0)

        assert _resume(outdir) == 0
        assert replayed == [outdir.resolve()]
        assert not (outdir / ".pause").exists()
        out = capsys.readouterr().out
        assert "pause flag removed" in out
        assert "resume requested for campaign 'camp-resume'" in out

    def test_resume_without_pause_flag_states_not_paused_and_still_replays(
        self, outdir: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        """No `.pause` file: exit stays idempotent, message states the
        campaign is not (flag-)paused, and the recovery replay proceeds."""
        _write_paused_run_json(outdir)
        _persist_run_invocation(["run", "--outdir", str(outdir)], outdir)

        replayed: list[Path] = []
        monkeypatch.setattr(cli, "_replay_paused_campaign", lambda od: replayed.append(od) or 7)

        assert _resume(outdir) == 7  # replay exit code is propagated
        assert replayed == [outdir.resolve()]
        err = capsys.readouterr().err
        assert "not flagged for pause" in err

    def test_resume_without_invocation_record_prints_manual_recovery(
        self, outdir: Path, capsys: Any
    ) -> None:
        _write_paused_run_json(outdir)
        (outdir / ".pause").write_text("{}")

        assert _resume(outdir) == 0
        assert not (outdir / ".pause").exists()
        out = capsys.readouterr().out
        assert "osimflow run --outdir" in out

    def test_resume_refuses_campaign_that_is_not_paused(self, outdir: Path, capsys: Any) -> None:
        _write_paused_run_json(outdir, status="success")
        assert _resume(outdir) == 1
        err = capsys.readouterr().err
        assert "is not paused" in err

    def test_resume_requires_run_json(self, tmp_path: Path, capsys: Any) -> None:
        assert _resume(tmp_path / "nope") == 1
        assert "run.json not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# End-to-end: pause → run exits → resume replays to success (stub mode)
# ---------------------------------------------------------------------------


class TestPauseExitResumeEndToEnd:
    def test_cli_pause_exit_then_resume_completes_via_replay(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
        monkeypatch: Any,
        capsys: Any,
    ) -> None:
        """Full issue #1628 flow in stub mode.

        1. ``main(run ...)`` with ``.pause`` pre-created exits with
           run.json status ``"paused"`` and the invocation recorded.
        2. ``main(resume)`` removes the flag, re-launches the recorded
           invocation in a subprocess, and the campaign reaches
           ``"success"`` via cache replay.
        """
        monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
        # The replayed subprocess must import THIS checkout's osimflow
        # (the venv may carry an editable install elsewhere).
        monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
        (outdir / ".pause").touch()

        run_argv = [
            "run",
            "--executor",
            "local",
            "--input_variables",
            str(variables_yml),
            "--template_sim_package",
            str(template_pkg),
            "--n_samples",
            "3",
            "--outdir",
            str(outdir),
            "--openstudio_version",
            "3.11.0",
            "--no-tui",
        ]
        assert cli.main(run_argv) == 0  # paused is a handled exit, not an error

        data = json.loads((outdir / "run.json").read_text())
        assert data["status"] == "paused"
        assert data["finished_at"] is None
        assert (outdir / "campaign_invocation.json").exists()
        assert (outdir / ".pause").exists()  # run() never consumes the flag

        assert cli.main(["resume", str(outdir)]) == 0

        assert not (outdir / ".pause").exists()
        data = json.loads((outdir / "run.json").read_text())
        assert data["status"] == "success"
        assert data["finished_at"] is not None
        out = capsys.readouterr().out
        assert "pause flag removed" in out
        assert "resuming via cache replay" in out
        assert "python" in out and "run" in out
