"""Evidence verifiers for the AWS Batch MVP live acceptance gate (issue #1815).

Pure, offline helpers: every function inspects files already retrieved to the
controller outdir, so the verifiers themselves are unit-tested in normal CI
(``tests/unit/test_aws_batch_acceptance_evidence.py``) while the live test
(``tests/integration/test_aws_batch_acceptance.py``) is skip-gated.

Why a stricter check than "SQLite with tables": ``osimflow.work`` stub mode
writes a structurally valid SQLite file with plausible tabular values. Real
EnergyPlus output uniquely carries a populated ``Simulations`` row (version +
``CompletedSuccessfully``) and a ``ReportDataDictionary`` table, which the stub
never creates. Worker logs are additionally scanned for the stub banner.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

STUB_LOG_MARKERS = ("openstudio CLI stub", "-- eplusout.sql stub --")
_SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"\b\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com\b"),
    re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:\d{12}:"),
)
_IMAGE_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


class AcceptanceError(AssertionError):
    """Raised when live-run evidence does not prove real execution."""


def real_energyplus_sql_problems(sql_path: Path) -> list[str]:
    """Return reasons *sql_path* is not a genuine, completed EnergyPlus SQL."""
    if not sql_path.is_file() or sql_path.stat().st_size == 0:
        return [f"{sql_path} missing or empty"]
    try:
        conn = sqlite3.connect(f"file:{sql_path}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        return [f"{sql_path} not SQLite: {exc}"]
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        problems: list[str] = []
        for needed in ("Simulations", "ReportDataDictionary"):
            if needed not in tables:
                problems.append(f"missing EnergyPlus-only table {needed}")
        if "Simulations" in tables:
            row = conn.execute(
                "SELECT EnergyPlusVersion, CompletedSuccessfully FROM Simulations "
                "ORDER BY SimulationIndex DESC LIMIT 1"
            ).fetchone()
            if row is None:
                problems.append("Simulations table has no rows")
            else:
                if not row[0]:
                    problems.append("Simulations.EnergyPlusVersion empty")
                if not row[1]:
                    problems.append("Simulations.CompletedSuccessfully is false")
        return problems
    except sqlite3.DatabaseError as exc:
        return [f"{sql_path} unreadable: {exc}"]
    finally:
        conn.close()


def stub_markers_in_logs(sample_dir: Path) -> list[str]:
    """Return log files under *sample_dir* that carry a stub-mode banner."""
    hits: list[str] = []
    for log in sorted(sample_dir.glob("*.log")):
        text = log.read_text(encoding="utf-8", errors="replace")
        if any(marker in text for marker in STUB_LOG_MARKERS):
            hits.append(str(log))
    return hits


def energyplus_completion_evidence(sample_dir: Path) -> bool:
    """True iff a sample log/err (or eplusout.end) shows EnergyPlus completing successfully."""
    for pattern in ("*.log", "*.err", "*.end"):
        for f in sample_dir.rglob(pattern):
            text = f.read_text(encoding="utf-8", errors="replace")
            if "EnergyPlus Completed Successfully" in text or "EnergyPlus Run Time" in text:
                return True
    return False


def kpis_problems(kpi_json: Path) -> list[str]:
    """Check one KPI JSON holds finite, positive, model-appropriate KPIs."""
    try:
        data = json.loads(kpi_json.read_text())
    except (OSError, ValueError) as exc:
        return [f"{kpi_json}: unreadable ({exc})"]
    kpis = data.get("kpis") or {}
    problems: list[str] = []
    for key in ("eui_kwh_m2_yr", "total_site_energy_kwh"):
        val = kpis.get(key)
        if not isinstance(val, (int, float)) or isinstance(val, bool) or not val > 0:
            problems.append(f"{kpi_json.name}: KPI {key}={val!r} not a positive number")
    return problems


@dataclass
class SampleEvidence:
    sample_id: str
    status: str
    problems: list[str] = field(default_factory=list)


def verify_successful_sample(outdir: Path, sample_id: str) -> SampleEvidence:
    """Verify one expected-success sample proves real worker execution."""
    problems: list[str] = []
    sim_dir = outdir / "work" / "sim" / sample_id
    problems += real_energyplus_sql_problems(sim_dir / "eplusout.sql")
    for hit in stub_markers_in_logs(sim_dir):
        problems.append(f"worker stub-mode banner in {hit}")
    if not energyplus_completion_evidence(sim_dir):
        problems.append("no EnergyPlus completion evidence in sample logs")
    problems += kpis_problems(outdir / "work" / "kpis" / f"kpi_{sample_id}.json")
    return SampleEvidence(sample_id, "ok" if not problems else "unproven", problems)


def verify_acceptance(
    outdir: Path,
    *,
    expected_success: list[str],
    expected_failed: list[str] | None = None,
    min_successes: int = 1,
) -> list[SampleEvidence]:
    """Strict acceptance: raise ``AcceptanceError`` unless real evidence exists.

    Fails on zero successful models, any unproven expected success, an expected
    failure recorded as ``ok`` (a failure masquerading as a simulation), or a
    failure with no retained status reason.
    """
    trace = json.loads((outdir / "run.json").read_text())
    per_sample = {row["sample_id"]: row for row in trace.get("per_sample", [])}
    errors: list[str] = []
    evidence = [verify_successful_sample(outdir, sid) for sid in expected_success]
    ok = [e for e in evidence if not e.problems]
    if len(ok) < min_successes:
        errors.append(f"only {len(ok)} proven successes; need >= {min_successes}")
    for ev in evidence:
        row = per_sample.get(ev.sample_id)
        if row is None or row.get("status") not in ("ok", "cached"):
            errors.append(f"{ev.sample_id}: run.json status {row and row.get('status')!r}")
        errors += [f"{ev.sample_id}: {p}" for p in ev.problems]
    for sid in expected_failed or []:
        row = per_sample.get(sid)
        if row is None or row.get("status") != "failed":
            errors.append(f"{sid}: expected failed, run.json has {row and row.get('status')!r}")
        elif not (row.get("error_summary") or row.get("stderr_log")):
            errors.append(f"{sid}: failure has no retained status reason/log")
    if errors:
        raise AcceptanceError("; ".join(errors))
    return evidence


def redact(text: str) -> str:
    """Strip account IDs / ARNs / access keys so evidence can be public."""
    for pat in _SECRET_PATTERNS:
        text = pat.sub("<redacted>", text)
    return text


def build_evidence_record(
    *,
    job_definition: str,
    image: str,
    job_ids: list[str],
    input_manifest: dict[str, object],
    output_manifest: dict[str, object],
    wall_time_s: float,
    per_sample: dict[str, str],
) -> dict[str, object]:
    """Assemble the publishable evidence record (job-def, digest, job IDs...)."""
    if not _IMAGE_DIGEST_RE.search(image):
        raise AcceptanceError(f"worker image must be digest-pinned (@sha256:...), got {image!r}")
    record: dict[str, object] = {
        "job_definition": job_definition,
        "image": image,
        "batch_job_ids": job_ids,
        "input_manifest": input_manifest,
        "output_manifest": output_manifest,
        "wall_time_s": wall_time_s,
        "per_sample_status": per_sample,
    }
    return json.loads(redact(json.dumps(record)))
