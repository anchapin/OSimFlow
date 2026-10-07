"""CI coverage for the AWS Batch acceptance evidence verifiers (issue #1815)."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
import _aws_batch_acceptance as acc  # noqa: E402

from osimflow.work import _write_stub_eplusout_sql  # noqa: E402

DIGEST = "nrel/openstudio@sha256:" + "a" * 64


def _real_sql(path: Path, completed: int = 1) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE Simulations (SimulationIndex INTEGER, EnergyPlusVersion TEXT, "
        "CompletedSuccessfully INTEGER)"
    )
    conn.execute("INSERT INTO Simulations VALUES (1, 'EnergyPlus 24.2', ?)", (completed,))
    conn.execute("CREATE TABLE ReportDataDictionary (ReportDataDictionaryIndex INTEGER)")
    conn.commit()
    conn.close()


def _sample(outdir: Path, sid: str, *, real: bool = True, log: str = "") -> None:
    sim = outdir / "work" / "sim" / sid
    sim.mkdir(parents=True, exist_ok=True)
    if real:
        _real_sql(sim / "eplusout.sql")
    else:
        _write_stub_eplusout_sql(sim, sid)
    (sim / "stdout.log").write_text(log or "EnergyPlus Completed Successfully\n")
    kpis = outdir / "work" / "kpis"
    kpis.mkdir(parents=True, exist_ok=True)
    (kpis / f"kpi_{sid}.json").write_text(
        json.dumps(
            {"sample_id": sid, "kpis": {"eui_kwh_m2_yr": 90.0, "total_site_energy_kwh": 5e4}}
        )
    )


def _run_json(outdir: Path, rows: list[dict[str, object]]) -> None:
    (outdir / "run.json").write_text(json.dumps({"per_sample": rows}))


def test_real_sql_accepted(tmp_path: Path) -> None:
    _real_sql(tmp_path / "e.sql")
    assert acc.real_energyplus_sql_problems(tmp_path / "e.sql") == []


def test_stub_sql_rejected(tmp_path: Path) -> None:
    _write_stub_eplusout_sql(tmp_path, "s0")
    assert acc.real_energyplus_sql_problems(tmp_path / "eplusout.sql")


def test_incomplete_simulation_rejected(tmp_path: Path) -> None:
    _real_sql(tmp_path / "e.sql", completed=0)
    assert any(
        "CompletedSuccessfully" in p for p in acc.real_energyplus_sql_problems(tmp_path / "e.sql")
    )


def test_acceptance_passes_for_mixed_outcome(tmp_path: Path) -> None:
    _sample(tmp_path, "s0")
    _run_json(
        tmp_path,
        [
            {"sample_id": "s0", "status": "ok"},
            {"sample_id": "s1", "status": "failed", "error_summary": "invalid workflow"},
        ],
    )
    ev = acc.verify_acceptance(tmp_path, expected_success=["s0"], expected_failed=["s1"])
    assert ev[0].status == "ok"


def test_acceptance_fails_on_stub_sample(tmp_path: Path) -> None:
    _sample(tmp_path, "s0", real=False)
    _run_json(tmp_path, [{"sample_id": "s0", "status": "ok"}])
    with pytest.raises(acc.AcceptanceError):
        acc.verify_acceptance(tmp_path, expected_success=["s0"])


def test_acceptance_fails_on_stub_banner(tmp_path: Path) -> None:
    _sample(
        tmp_path, "s0", log="openstudio CLI stub v3 sample=s0\nEnergyPlus Completed Successfully"
    )
    _run_json(tmp_path, [{"sample_id": "s0", "status": "ok"}])
    with pytest.raises(acc.AcceptanceError, match="stub-mode banner"):
        acc.verify_acceptance(tmp_path, expected_success=["s0"])


def test_acceptance_fails_on_zero_successes(tmp_path: Path) -> None:
    _run_json(tmp_path, [])
    with pytest.raises(acc.AcceptanceError, match="proven successes"):
        acc.verify_acceptance(tmp_path, expected_success=[])


def test_failure_masquerading_as_ok_is_rejected(tmp_path: Path) -> None:
    _sample(tmp_path, "s0")
    _run_json(
        tmp_path,
        [{"sample_id": "s0", "status": "ok"}, {"sample_id": "s1", "status": "ok"}],
    )
    with pytest.raises(acc.AcceptanceError, match="expected failed"):
        acc.verify_acceptance(tmp_path, expected_success=["s0"], expected_failed=["s1"])


def test_failure_without_reason_is_rejected(tmp_path: Path) -> None:
    _sample(tmp_path, "s0")
    _run_json(
        tmp_path,
        [{"sample_id": "s0", "status": "ok"}, {"sample_id": "s1", "status": "failed"}],
    )
    with pytest.raises(acc.AcceptanceError, match="no retained status reason"):
        acc.verify_acceptance(tmp_path, expected_success=["s0"], expected_failed=["s1"])


def test_evidence_record_requires_digest_and_redacts() -> None:
    with pytest.raises(acc.AcceptanceError):
        acc.build_evidence_record(
            job_definition="jd:1",
            image="nrel/openstudio:3.11.0",
            job_ids=[],
            input_manifest={},
            output_manifest={},
            wall_time_s=1.0,
            per_sample={},
        )
    rec = acc.build_evidence_record(
        job_definition="jd:1",
        image=DIGEST,
        job_ids=["j-1"],
        input_manifest={"uri": "arn:aws:s3:us-east-1:123456789012:x"},
        output_manifest={},
        wall_time_s=1.0,
        per_sample={"s0": "ok"},
    )
    assert "123456789012" not in json.dumps(rec)
