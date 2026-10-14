import json
import sqlite3
from pathlib import Path

import pandas as pd

from osimflow._work_scripts.extract_kpis import run_extract_kpis
from osimflow.results_query import export_results_cli
from osimflow.server_csv import SIMULATION_FAILED_COL, build_server_csv_frame
from osimflow.work import aggregate_results

REFERENCE = Path(__file__).parent.parent.parent / "fixtures" / "server_download_data_reference.csv"


def _campaign(tmp_path: Path) -> Path:
    d = tmp_path / "camp"
    d.mkdir()
    (d / "samples.json").write_text(
        json.dumps(
            {
                "samples": [
                    {"sample_id": "s000", "values": {"hvac.cop": 3.5, "envelope.wall_r": 12.0}},
                    {"sample_id": "s001", "values": {"hvac.cop": 2.0, "envelope.wall_r": 9.0}},
                ]
            }
        )
    )
    pd.DataFrame(
        [
            {
                "sample_id": "s000",
                "envelope.wall_r": 12.0,
                "hvac.cop": 3.5,
                "openstudio_results.out_eui": 55.2,
            }
        ]
    ).to_csv(d / "aggregated_results.csv", index=False)
    pd.DataFrame(
        [{"sample_id": "s001", "error_summary": "Measure hvac failed: bad argument"}]
    ).to_csv(d / "failed_simulations.csv", index=False)
    return d


def test_column_parity_with_reference(tmp_path: Path) -> None:
    df = build_server_csv_frame(_campaign(tmp_path))
    ref = pd.read_csv(REFERENCE)
    assert list(df.columns) == list(ref.columns)
    assert df[SIMULATION_FAILED_COL].tolist() == ["", "Measure hvac failed: bad argument"]
    assert df["status_message"].tolist() == ["completed normal", "datapoint failure"]
    assert df["name"].tolist() == ref["name"].tolist()


def test_failed_column_present_without_failures(tmp_path: Path) -> None:
    d = _campaign(tmp_path)
    (d / "failed_simulations.csv").unlink()
    df = build_server_csv_frame(d)
    assert list(df.columns)[-1] == SIMULATION_FAILED_COL
    assert len(df) == 1


def test_empty_campaign(tmp_path: Path) -> None:
    df = build_server_csv_frame(tmp_path)
    assert df.empty
    assert list(df.columns) == ["name", "_id", "status", "status_message", SIMULATION_FAILED_COL]


def test_empty_campaign_preserves_variable_columns_from_samples_json(tmp_path: Path) -> None:
    """Issue #2041: empty campaign with samples.json preserves canonical variable columns."""
    (tmp_path / "samples.json").write_text(
        json.dumps(
            {
                "samples": [
                    {"sample_id": "0000", "values": {"hvac.cop": 3.5, "envelope.wall_r": 12.0}},
                ]
            }
        )
    )
    df = build_server_csv_frame(tmp_path)
    assert df.empty
    assert list(df.columns) == [
        "name",
        "_id",
        "status",
        "status_message",
        "hvac.cop",
        "envelope.wall_r",
        SIMULATION_FAILED_COL,
    ]


def test_deduplicates_failed_sample_also_present_in_aggregated_results(tmp_path: Path) -> None:
    """Issue #2041: defense-in-depth — failed sample in both CSVs appears once as datapoint failure."""
    d = _campaign(tmp_path)
    pd.DataFrame(
        [
            {
                "sample_id": "s000",
                "envelope.wall_r": 12.0,
                "hvac.cop": 3.5,
                "openstudio_results.out_eui": 55.2,
            },
            {
                "sample_id": "s001",
                "envelope.wall_r": 9.0,
                "hvac.cop": 2.0,
                "error": "eplusout.sql_missing",
            },
        ]
    ).to_csv(d / "aggregated_results.csv", index=False)
    df = build_server_csv_frame(d)
    assert df["name"].tolist() == ["s000", "s001"]
    assert df["status_message"].tolist() == ["completed normal", "datapoint failure"]
    assert df[SIMULATION_FAILED_COL].tolist() == ["", "Measure hvac failed: bad argument"]


def test_cli_export_and_no_include_failed(tmp_path: Path) -> None:
    d = _campaign(tmp_path)
    out = tmp_path / "out.csv"
    rc = export_results_cli(outdirs=[str(d)], format="openstudio-server-csv", output_path=str(out))
    assert rc == 0
    assert len(pd.read_csv(out)) == 2
    rc = export_results_cli(
        outdirs=[str(d)],
        format="openstudio-server-csv",
        output_path=str(out),
        include_failed=False,
    )
    assert rc == 0
    assert len(pd.read_csv(out)) == 1


def test_leading_zero_ids_and_kpi_only_aggregate(tmp_path: Path) -> None:
    d = tmp_path / "c"
    d.mkdir()
    (d / "samples.json").write_text(
        json.dumps({"samples": [{"sample_id": "0001", "values": {"a.x": 1.5}}]})
    )
    pd.DataFrame([{"sample_id": "0001", "out": 2.0}]).to_csv(
        d / "aggregated_results.csv", index=False
    )
    df = build_server_csv_frame(d)
    assert df["name"].tolist() == ["0001"]
    assert df["_id"].tolist() == ["0001"]
    assert df["a.x"].tolist() == [1.5]


def test_multi_campaign_column_order(tmp_path: Path) -> None:
    first = _campaign(tmp_path)
    second = tmp_path / "camp2"
    second.mkdir()
    (second / "samples.json").write_text(
        json.dumps({"samples": [{"sample_id": "t0", "values": {"new.var": 7}}]})
    )
    pd.DataFrame([{"sample_id": "t0", "other_out": 1.0}]).to_csv(
        second / "aggregated_results.csv", index=False
    )
    out = tmp_path / "o.csv"
    rc = export_results_cli(
        outdirs=[str(first), str(second)], format="openstudio-server-csv", output_path=str(out)
    )
    assert rc == 0
    cols = list(pd.read_csv(out).columns)
    assert cols.index("new.var") < cols.index("openstudio_results.out_eui")
    assert cols[-1] == SIMULATION_FAILED_COL


def test_end_to_end_extract_kpis_to_server_csv(tmp_path: Path) -> None:
    camp = tmp_path / "camp_e2e"
    sim0 = camp / "work" / "sim" / "s000"
    sim1 = camp / "work" / "sim" / "s001"
    kpi_dir = camp / "kpis"
    sim0.mkdir(parents=True)
    sim1.mkdir(parents=True)

    samples_json = camp / "samples.json"
    samples_json.write_text(
        json.dumps(
            {
                "samples": [
                    {"sample_id": "s000", "values": {"hvac.cop": 3.5, "envelope.wall_r": 12.0}},
                    {"sample_id": "s001", "values": {"hvac.cop": 2.0, "envelope.wall_r": 9.0}},
                ]
            }
        )
    )

    conn = sqlite3.connect(str(sim0 / "eplusout.sql"))
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE TabularDataWithStrings (
            ReportName TEXT,
            ReportForString TEXT,
            TableName TEXT,
            RowName TEXT,
            ColumnName TEXT,
            Units TEXT,
            Value TEXT
        )
        """
    )
    cur.executemany(
        "INSERT INTO TabularDataWithStrings VALUES (?,?,?,?,?,?,?)",
        [
            (
                "AnnualBuildingUtilityPerformanceSummary",
                "Entire Facility",
                "Site and Source Energy",
                "Total Site Energy",
                "Energy Per Total Building Area",
                "MJ/m2",
                "360.0",
            ),
            (
                "AnnualBuildingUtilityPerformanceSummary",
                "Entire Facility",
                "Site and Source Energy",
                "Total Site Energy",
                "Total Energy",
                "MJ",
                "36000.0",
            ),
        ],
    )
    cur.execute("CREATE TABLE Zones (ZoneIndex INTEGER, Floor_Area REAL)")
    cur.execute("INSERT INTO Zones VALUES (0, 100.0)")
    conn.commit()
    conn.close()

    (sim0 / "out.osw").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "measure_dir_name": "openstudio_results",
                        "result": {
                            "step_result": "Success",
                            "step_values": [
                                {"name": "out_eui", "value": 55.2},
                                {"name": "out_total_electricity_gj", "value": 12.5},
                            ],
                        },
                    }
                ]
            }
        )
    )
    (sim1 / "out.osw").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "measure_dir_name": "hvac",
                        "result": {
                            "step_result": "Fail",
                            "step_errors": ["bad argument"],
                        },
                    }
                ]
            }
        )
    )

    kpi_file = run_extract_kpis(sim0, "s000", kpi_dir / "kpi_s000.json")
    aggregate_results([kpi_file], [sim0, sim1], camp, samples_json=samples_json)
    df = build_server_csv_frame(camp)

    cols = list(df.columns)
    assert cols[:4] == ["name", "_id", "status", "status_message"]
    assert cols[4:6] == ["hvac.cop", "envelope.wall_r"]
    assert "openstudio_results.out_eui" in cols
    assert "openstudio_results.out_total_electricity_gj" in cols
    assert "eui_kwh_m2_yr" in cols
    assert cols.index("envelope.wall_r") < cols.index("openstudio_results.out_eui")
    assert cols[-1] == SIMULATION_FAILED_COL
    assert df[SIMULATION_FAILED_COL].tolist() == ["", "hvac: bad argument"]
