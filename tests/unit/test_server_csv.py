import json
from pathlib import Path

import pandas as pd

from osimflow.results_query import export_results_cli
from osimflow.server_csv import SIMULATION_FAILED_COL, build_server_csv_frame

REFERENCE = Path(__file__).parent.parent / "fixtures" / "server_download_data_reference.csv"


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
                "reporting_179_d.out_eui": 55.2,
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
    assert build_server_csv_frame(tmp_path).empty


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
