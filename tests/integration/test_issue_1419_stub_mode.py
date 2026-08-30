"""Regression tests for GitHub issue #1419.

Before this fix, stub-mode campaigns (``OSIMFLOW_STUB_SIM=1`` or no
``openstudio.cli`` on PATH) wrote ``eplusout.sql`` as the literal
string ``"-- placeholder sql"`` — which is not a valid SQLite
database.  ``osimflow._work_scripts.extract_kpis`` then logged
``Corrupt eplusout.sql: file is not a database`` and emitted a KPI
JSON missing the critical KPIs ``eui_kwh_m2_yr`` and
``total_site_energy_kwh``.  Every sample failed the validator,
``AGGREGATE_RESULTS`` had no usable inputs, and the per-generation
loop's ``_verify_step_inputs("GENERATE_BASIC_PLOTS")`` raised
``FileNotFoundError: Step 'GENERATE_BASIC_PLOTS' requires input
'../aggregated_results.csv'``.

These tests assert:

1. The stub ``eplusout.sql`` is a readable SQLite database (the root
   cause is closed).
2. ``extract_kpis`` populated the critical KPIs from the stub.
3. A 3-sample stub-mode campaign produces all four canonical output
   artifacts (``aggregated_results.csv``, ``failed_simulations.csv``,
   KPI JSONs, plot files) without hitting the
   ``GENERATE_BASIC_PLOTS`` ``FileNotFoundError``.
4. The Part-2 defensive fix: an all-samples-failed campaign still
   writes a header-only ``aggregated_results.csv`` so the downstream
   verification does not mask the real per-sample errors.

The fixture layout mirrors ``test_local_executor.py`` so the two test
files exercise the same Campaign code path through different
assertions.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor
from osimflow.work import _write_stub_eplusout_sql

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: wwr\n"
        "    distribution: uniform\n"
        "    min: 0.2\n"
        "    max: 0.6\n"
        "    measure_argument: SetEnvelopePerformance.wwr\n"
    )
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


def _make_cfg(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    *,
    n_samples: int = 3,
) -> CampaignConfig:
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=n_samples,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        skip_preflight=True,
    )


def test_stub_eplusout_sql_is_valid_sqlite(workdir: Path) -> None:
    """The helper that backs the stub writes a parseable SQLite file.

    This is the root-cause assertion (issue #1419 part 1): the file
    must open as a database and contain ``TabularDataWithStrings`` plus
    ``Zones`` with values that satisfy ``extract_kpis`` queries.
    """
    sim_out = workdir / "stub_sim"
    sim_out.mkdir()

    sql_path = _write_stub_eplusout_sql(sim_out, sample_id="0001")
    assert sql_path.is_file()

    conn = sqlite3.connect(str(sql_path))
    try:
        cur = conn.cursor()
        # Verify the schema ``extract_kpis`` reads.
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='TabularDataWithStrings'"
        )
        assert cur.fetchone() is not None, "TabularDataWithStrings table missing"

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Zones'")
        assert cur.fetchone() is not None, "Zones table missing"

        # The Total Site Energy / Energy Per Total Building Area cell
        # is the one ``_extract_eui`` reads for ``eui_kwh_m2_yr``.
        cur.execute(
            """
            SELECT Value FROM TabularDataWithStrings
            WHERE TableName = 'Site and Source Energy'
              AND RowName = 'Total Site Energy'
              AND ColumnName = 'Energy Per Total Building Area'
            """
        )
        row = cur.fetchone()
        assert row is not None, "Total Site Energy per-area row missing"
        mj_per_m2 = float(row[0])
        assert mj_per_m2 > 0.0

        # The Total Energy cell is the one for ``total_site_energy_kwh``.
        cur.execute(
            """
            SELECT Value FROM TabularDataWithStrings
            WHERE TableName = 'Site and Source Energy'
              AND RowName = 'Total Site Energy'
              AND ColumnName = 'Total Energy'
            """
        )
        row = cur.fetchone()
        assert row is not None, "Total Site Energy total row missing"
        total_mj = float(row[0])
        assert total_mj > 0.0

        # Floor area must come back from the Zones table so
        # ``_extract_eui`` can divide.
        cur.execute("SELECT SUM(Floor_Area) FROM Zones")
        row = cur.fetchone()
        assert row is not None and row[0] is not None
        assert float(row[0]) > 0.0
    finally:
        conn.close()


def test_stub_eplusout_sql_values_are_deterministic_per_sample(workdir: Path) -> None:
    """Same sample_id → same MJ/m² (tests are reproducible).

    KPI variance across samples comes from the trailing digit of the
    sample id (1..5 cycle); this test pins that contract so a future
    refactor does not silently break test reproducibility.
    """
    sim_a = workdir / "sim_a"
    sim_a.mkdir()
    sim_b = workdir / "sim_b"
    sim_b.mkdir()

    sql_a = _write_stub_eplusout_sql(sim_a, sample_id="0001")
    sql_b = _write_stub_eplusout_sql(sim_b, sample_id="0001")

    def _per_area_value(p: Path) -> float:
        conn = sqlite3.connect(str(p))
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT Value FROM TabularDataWithStrings
                WHERE TableName = 'Site and Source Energy'
                  AND RowName = 'Total Site Energy'
                  AND ColumnName = 'Energy Per Total Building Area'
                """
            )
            row = cur.fetchone()
            assert row is not None
            return float(row[0])
        finally:
            conn.close()

    assert _per_area_value(sql_a) == _per_area_value(sql_b)

    # And different sample ids → different values (variance for plots).
    sim_c = workdir / "sim_c"
    sim_c.mkdir()
    sql_c = _write_stub_eplusout_sql(sim_c, sample_id="0002")
    assert _per_area_value(sql_a) != _per_area_value(sql_c)


def test_stub_mode_campaign_produces_critical_kpis(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """A 3-sample stub-mode campaign produces non-null critical KPIs.

    Issue #1419 acceptance criterion (a)+(b): ``eplusout.sql`` is a
    readable SQLite database; extracted KPIs contain
    ``eui_kwh_m2_yr`` and ``total_site_energy_kwh`` with non-null
    values.
    """
    cfg = _make_cfg(workdir, template_pkg, outdir)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    campaign.run()

    # Per-sample stub eplusout.sql files must be valid SQLite.
    sim_dirs = sorted((outdir / "work" / "sim").iterdir())
    assert len(sim_dirs) == 3, f"expected 3 sim dirs, got {len(sim_dirs)}"
    for sim_dir in sim_dirs:
        sql_path = sim_dir / "eplusout.sql"
        assert sql_path.is_file(), f"missing {sql_path}"
        conn = sqlite3.connect(str(sql_path))
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}
            assert "TabularDataWithStrings" in tables
            assert "Zones" in tables
        finally:
            conn.close()

    # KPI JSONs must contain the critical KPIs (Part 1 acceptance).
    kpi_paths = sorted((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_paths) == 3
    for kpi_path in kpi_paths:
        data = json.loads(kpi_path.read_text())
        kpis = data["kpis"]
        assert kpis.get("eui_kwh_m2_yr") is not None, f"eui_kwh_m2_yr missing in {kpi_path}"
        assert kpis.get("total_site_energy_kwh") is not None, (
            f"total_site_energy_kwh missing in {kpi_path}"
        )
        # Validator must must be happy too — quality.valid drives downstream
        # success counts and the per-sample status in run.json.
        assert data["quality"]["valid"] is True, (
            f"quality validation failed for {kpi_path}: {data['quality']}"
        )


def test_stub_mode_campaign_completes_full_dag(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """The campaign completes all 7 DAG steps without FileNotFoundError.

    Issue #1419 acceptance criterion (c)+(d): ``aggregated_results.csv``
    exists with 3 data rows and ``GENERATE_BASIC_PLOTS`` completes.
    This is the full-DAG equivalent of the prior CI failure: the
    per-generation loop's ``_verify_step_inputs("GENERATE_BASIC_PLOTS")``
    used to raise ``FileNotFoundError`` before AGGREGATE_RESULTS could
    write the CSV.  Now AGGREGATE_RESULTS runs first inside the loop
    and the verification passes.
    """
    cfg = _make_cfg(workdir, template_pkg, outdir)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    result = campaign.run()

    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    lines = csv_path.read_text().strip().splitlines()
    assert lines[0].startswith("sample_id"), (
        f"aggregated_results.csv missing header; got first line: {lines[0]!r}"
    )
    assert len(lines) == 3 + 1, f"expected 3 data rows + header; got {len(lines)} lines"

    parquet_path = outdir / "aggregated_results.parquet"
    assert parquet_path.is_file(), f"missing artifact: {parquet_path}"

    failed_path = outdir / "failed_simulations.csv"
    assert failed_path.is_file(), f"missing artifact: {failed_path}"

    plots_dir = outdir / "plots"
    assert plots_dir.is_dir(), f"missing plots dir: {plots_dir}"
    assert any(plots_dir.glob("*.png")) or any(plots_dir.glob("*.pdf")), (
        f"plots directory is empty: {plots_dir}"
    )

    # run.json records every step's completion.
    run_json = outdir / "run.json"
    trace = json.loads(run_json.read_text())
    step_names = {s["step"] for s in trace["steps"]}
    for required in (
        "GENERATE_LHS_SAMPLES",
        "APPLY_PARAMETERS",
        "RUN_OPENSTUDIO_SIM",
        "EXTRACT_KPIS",
        "AGGREGATE_RESULTS",
        "GENERATE_BASIC_PLOTS",
    ):
        assert required in step_names, (
            f"step {required} missing from run.json steps (got {step_names})"
        )

    assert trace["summary"]["n_samples"] == 3
    assert trace["summary"]["n_succeeded"] == 3
    assert trace["summary"]["n_failed"] == 0

    # Returned handles expose the four artifacts.
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s"}
    assert len(result["kpis"]) == 3


def test_all_failed_campaign_still_writes_csv(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """Issue #1419 Part 2: an all-failed campaign must still write the CSV.

    We force every per-sample extraction to fail by replacing the stub
    eplusout.sql writer with a broken database (``eplusout.sql`` left
    empty).  ``extract_kpis`` then logs "No supported tabular schema
    found" and emits a KPI JSON missing the critical KPIs.  The
    campaign reaches the end (the per-sample ``run.json`` records the
    quality failures) and AGGREGATE_RESULTS must still produce
    ``aggregated_results.csv`` so the downstream
    ``_verify_step_inputs("GENERATE_BASIC_PLOTS")`` does not raise
    ``FileNotFoundError`` and mask the real per-sample errors.
    """
    import osimflow.work as work_mod

    original = work_mod._write_stub_eplusout_sql

    def _broken_stub_sql(sim_out: Path, sample_id: str) -> Path:
        """Drop an empty file in place of a valid SQLite DB."""
        sql_path = sim_out / "eplusout.sql"
        sql_path.write_text("")
        return sql_path

    work_mod._write_stub_eplusout_sql = _broken_stub_sql
    try:
        cfg = _make_cfg(workdir, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
        # Campaign completes (status: success in trace) but every sample
        # is quality-invalidated — that is the contract Part-2 closes:
        # the campaign must finish without the
        # ``_verify_step_inputs("GENERATE_BASIC_PLOTS")`` FileNotFoundError
        # that used to mask the per-sample quality failures.
        campaign.run()
    finally:
        work_mod._write_stub_eplusout_sql = original

    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), (
        f"all-quality-failed campaign did not write {csv_path} "
        "(issue #1419 Part 2 — defensive resilience)."
    )
    csv_text = csv_path.read_text()
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("sample_id")
    # The KPI JSONs still contain ``sample_id`` (from the broken-stub
    # branch), so the aggregator emits one row per sample.
    assert len(lines) >= 2, (
        f"expected at least 1 data row + header in all-quality-failed CSV, "
        f"got {len(lines)} lines:\n{csv_text!r}"
    )

    parquet_path = outdir / "aggregated_results.parquet"
    assert parquet_path.is_file(), f"missing parquet twin: {parquet_path}"

    failed_path = outdir / "failed_simulations.csv"
    assert failed_path.is_file(), f"missing failed CSV: {failed_path}"

    # Per-sample KPI JSONs carry the validator's ``quality.failures``
    # list (this is the user-facing surface that exposes why each
    # sample was rejected — the Part-2 contract is that this signal
    # survives the campaign completing).
    kpi_paths = sorted((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_paths) == 3, f"expected 3 KPI JSONs, got {len(kpi_paths)}"
    for kpi_path in kpi_paths:
        data = json.loads(kpi_path.read_text())
        quality_failures = data["quality"]["failures"]
        assert any("eui_kwh_m2_yr" in f for f in quality_failures), (
            f"expected eui_kwh_m2_yr critical-KPI failure in {kpi_path}, got {quality_failures}"
        )
        assert any("total_site_energy_kwh" in f for f in quality_failures), (
            f"expected total_site_energy_kwh critical-KPI failure in "
            f"{kpi_path}, got {quality_failures}"
        )


def test_aggregate_results_script_writes_header_only_when_no_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct unit test for the Part-2 aggregate_results defensive fix.

    Drives ``osimflow._work_scripts.aggregate_results.main`` with no
    ``--kpis`` files at all — equivalent to the "all samples failed"
    branch — and asserts the header-only CSV (and parquet) is still
    written.
    """
    import sys

    from osimflow._work_scripts import aggregate_results as agg_mod

    out_csv = tmp_path / "aggregated_results.csv"
    out_parquet = tmp_path / "aggregated_results.parquet"
    out_failed = tmp_path / "failed_simulations.csv"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "aggregate_results.py",
            "--kpis",
            "--out_csv",
            str(out_csv),
            "--out_parquet",
            str(out_parquet),
            "--out_failed",
            str(out_failed),
            "--ts_resolution",
            "monthly",
        ],
    )
    rc = agg_mod.main()
    assert rc == 0

    assert out_csv.is_file()
    assert out_csv.read_text() == "sample_id\n"

    assert out_failed.is_file()
    assert out_failed.read_text().startswith(
        "sample_id,failure_category,root_cause_line,total_severe_errors,"
    )


def test_aggregate_results_script_handles_corrupt_kpi_jsons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KPI JSONs that parse but contain only ``error`` keys (issue #1419
    legacy path) must not crash aggregation — the CSV still gets written.
    """
    import sys

    from osimflow._work_scripts import aggregate_results as agg_mod

    kpi_dir = tmp_path / "kpis"
    kpi_dir.mkdir()
    # Mimic the legacy broken-stub KPI JSON shape (issue #1419 root
    # cause before the fix).
    for sid in ("0001", "0002"):
        (kpi_dir / f"kpi_{sid}.json").write_text(
            json.dumps(
                {
                    "sample_id": sid,
                    "openstudio_version": None,
                    "kpis": {"error": "corrupt_database", "raw_error": "x"},
                    "quality": {
                        "valid": False,
                        "warnings": [],
                        "failures": [
                            "Missing critical KPI: eui_kwh_m2_yr",
                            "Missing critical KPI: total_site_energy_kwh",
                        ],
                    },
                }
            )
        )
    # Empty eplusout.sql mirrors — extract_kpis already wrote them.
    sim_dir = tmp_path / "sim"
    sim_dir.mkdir()
    for sid in ("0001", "0002"):
        (sim_dir / sid).mkdir()
        (sim_dir / sid / "eplusout.sql").write_text("")

    out_csv = tmp_path / "aggregated_results.csv"
    out_parquet = tmp_path / "aggregated_results.parquet"
    out_failed = tmp_path / "failed_simulations.csv"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "aggregate_results.py",
            "--kpis",
            *(str(p) for p in sorted(kpi_dir.glob("kpi_*.json"))),
            "--simulation_dirs",
            *(str(sim_dir / sid) for sid in ("0001", "0002")),
            "--out_csv",
            str(out_csv),
            "--out_parquet",
            str(out_parquet),
            "--out_failed",
            str(out_failed),
            "--ts_resolution",
            "monthly",
        ],
    )
    rc = agg_mod.main()
    assert rc == 0

    assert out_csv.is_file()
    csv_text = out_csv.read_text()
    lines = csv_text.strip().splitlines()
    # Two KPI rows + header (each row carries the legacy error/raw_error
    # columns — that is fine, the assertion is just that the CSV is
    # written with the right shape).
    assert len(lines) == 3, f"expected 2 data rows + header, got:\n{csv_text!r}"
    assert lines[0].startswith("sample_id")
