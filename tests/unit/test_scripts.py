import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BIN = PROJECT_ROOT / "bin"


def test_generate_lhs(tmp_path):
    var_yml = tmp_path / "variables.yml"
    var_yml.write_text("""
variables:
  - name: param1
    distribution: uniform
    min: 1.0
    max: 5.0
""")
    out_dir = tmp_path / "out"
    out_json = out_dir / "samples.json"

    subprocess.run(
        [
            sys.executable,
            str(BIN / "generate_lhs.py"),
            "--variables_yml",
            str(var_yml),
            "--n_samples",
            "2",
            "--out",
            str(out_json),
        ],
        check=True,
    )

    assert out_json.exists()
    data = json.loads(out_json.read_text())
    assert data["n_samples"] == 2
    assert len(data["samples"]) == 2
    assert "param1" in data["samples"][0]["values"]
    assert (out_dir / "0001.params.json").exists()


def test_extract_kpis(tmp_path):
    sim_dir = tmp_path / "sim"
    sim_dir.mkdir()

    # We won't test sqlite here because we can't easily mock the DB.
    # We will just ensure the stub behavior works (graceful fallback).
    out_kpi = tmp_path / "kpi.json"

    subprocess.run(
        [
            sys.executable,
            str(BIN / "extract_kpis.py"),
            "--simulation_dir",
            str(sim_dir),
            "--sample_id",
            "0001",
            "--out",
            str(out_kpi),
        ],
        check=True,
    )

    assert out_kpi.exists()
    data = json.loads(out_kpi.read_text())
    assert data["sample_id"] == "0001"
    assert "kpis" in data


def test_aggregate_results(tmp_path):
    sim_dir = tmp_path / "sim" / "0001"
    sim_dir.mkdir(parents=True)

    kpi_file = tmp_path / "kpi_0001.json"
    kpi_file.write_text(json.dumps({"sample_id": "0001", "kpis": {"eui": 100.0}}))

    out_csv = tmp_path / "agg.csv"
    out_fail = tmp_path / "fail.csv"

    subprocess.run(
        [
            sys.executable,
            str(BIN / "aggregate_results.py"),
            "--kpis",
            str(kpi_file),
            "--simulation_dirs",
            str(sim_dir),
            "--out_csv",
            str(out_csv),
            "--out_failed",
            str(out_fail),
        ],
        check=True,
    )

    assert out_csv.exists()
    assert out_fail.exists()

    df = pd.read_csv(out_csv)
    assert len(df) == 1
    assert "eui" in df.columns


def test_generate_plots(tmp_path):
    out_csv = tmp_path / "agg.csv"
    out_fail = tmp_path / "fail.csv"

    out_csv.write_text("sample_id,eui_kwh_m2_yr,var1\n0001,100,1\n0002,110,2")
    out_fail.write_text("sample_id,error_summary,exit_code,log_path\n0003,Error,1,log")

    out_plots = tmp_path / "plots"

    subprocess.run(
        [
            sys.executable,
            str(BIN / "generate_plots.py"),
            "--results_csv",
            str(out_csv),
            "--failed_csv",
            str(out_fail),
            "--outdir",
            str(out_plots),
        ],
        check=True,
    )

    assert (out_plots / "eui_histogram.png").exists()
    assert (out_plots / "failure_summary.png").exists()
    assert (out_plots / "top_var_vs_eui.png").exists()


def test_excel_to_variables(tmp_path):
    pytest.importorskip("openpyxl", reason="openpyxl required for Excel conversion")
    import openpyxl

    xlsx_path = tmp_path / "variables.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Variables"
    ws.append(["var_name", "lower_bound", "upper_bound", "distribution", "display_name"])
    ws.append(["wall_r", 2.0, 5.0, "uniform", "Wall R-value"])
    ws.append(["light_lmp", 0.05, 0.15, "normal", None])
    wb.save(xlsx_path)

    out_yml = tmp_path / "variables.yml"

    subprocess.run(
        [
            sys.executable,
            str(BIN / "excel_to_variables.py"),
            "--input",
            str(xlsx_path),
            "--output",
            str(out_yml),
        ],
        check=True,
    )

    assert out_yml.exists()
    data = yaml.safe_load(out_yml.read_text())
    assert data["algorithm"] == "lhs"
    assert len(data["variables"]) == 2
    assert data["variables"][0]["name"] == "wall_r"
    assert data["variables"][0]["distribution"] == "uniform"
    assert data["variables"][0]["min"] == 2.0
    assert data["variables"][0]["max"] == 5.0
    assert data["variables"][1]["name"] == "light_lmp"
    assert data["variables"][1]["distribution"] == "normal"
