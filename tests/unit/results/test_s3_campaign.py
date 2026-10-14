"""Tests for S3-backed detached campaigns (issue #1873)."""

import json
import os
import sqlite3
import uuid
from pathlib import Path

import pandas as pd
import pytest

from osimflow.s3_campaign import (
    COMPLETE_MARKER,
    S3CampaignError,
    S3CampaignStore,
    S3Handoff,
    new_handoff,
)
from osimflow.server_csv import SIMULATION_FAILED_COL
from osimflow.storage import ResultStorage


class FakeStorage(ResultStorage):
    """Filesystem-backed stand-in for a bucket rooted at an empty prefix."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        dest = self.root / remote_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(local_path.read_bytes())

    async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
        self.upload_file(local_path, remote_path)

    def download_file(self, remote_path: str, local_path: Path) -> None:
        src = self.root / remote_path
        if not src.is_file():
            raise FileNotFoundError(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(src.read_bytes())

    async def download_file_async(self, remote_path: str, local_path: Path) -> None:
        self.download_file(remote_path, local_path)

    def list_results(self, prefix: str = "") -> list[str]:
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file() and p.relative_to(self.root).as_posix().startswith(prefix)
        )

    async def list_results_async(self, prefix: str = "") -> list[str]:
        return self.list_results(prefix)

    def put_if_absent(self, remote_path: str, data: bytes) -> bool:
        dest = self.root / remote_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = self.root.parent / f"{self.root.name}.staging"
        staging.mkdir(exist_ok=True)
        tmp = staging / uuid.uuid4().hex
        tmp.write_bytes(data)
        try:
            os.link(tmp, dest)  # atomic create-if-absent, like S3 If-None-Match
        except FileExistsError:
            return False
        finally:
            tmp.unlink()
        return True


@pytest.fixture
def store(tmp_path: Path) -> S3CampaignStore:
    return S3CampaignStore(FakeStorage(tmp_path / "bucket"), tmp_path / "scratch")


def _submit(store: S3CampaignStore, tmp_path: Path, cid: str = "camp1") -> S3Handoff:
    samples = tmp_path / "samples.json"
    samples.write_text(json.dumps({"samples": []}))
    rec = new_handoff(
        cid,
        ["0000", "0001"],
        openstudio_version="3.10.0",
        kpis=None,
        job_ids={"0000": "j0", "0001": "j1"},
    )
    store.write_handoff(rec, samples)
    return rec


def _complete(store: S3CampaignStore, cid: str, sid: str) -> None:
    root = store.storage.root  # type: ignore[attr-defined]
    d = root / cid / "work" / "sim" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.log").write_text("ok")
    (d / COMPLETE_MARKER).write_text("")


def test_handoff_roundtrip_and_exists(store: S3CampaignStore, tmp_path: Path) -> None:
    assert not store.handoff_exists("camp1")
    rec = _submit(store, tmp_path)
    assert store.handoff_exists("camp1")
    back = store.read_handoff("camp1")
    assert back.sample_ids == rec.sample_ids
    assert back.job_ids == {"0000": "j0", "0001": "j1"}


def test_missing_handoff_raises(store: S3CampaignStore) -> None:
    with pytest.raises(S3CampaignError):
        store.read_handoff("nope")


def test_status_and_list(store: S3CampaignStore, tmp_path: Path) -> None:
    _submit(store, tmp_path, "camp1")
    _submit(store, tmp_path, "other")
    assert store.status("camp1")["state"] == "running"
    _complete(store, "camp1", "0000")
    assert store.status("camp1")["completed"] == 1
    _complete(store, "camp1", "0001")
    st = store.status("camp1")
    assert st["state"] == "completed" and st["pending"] == []
    assert [r.campaign_id for r in store.list_campaigns("camp")] == ["camp1"]
    assert len(store.list_campaigns()) == 2


def test_download_requires_complete_unless_partial(store: S3CampaignStore, tmp_path: Path) -> None:
    _submit(store, tmp_path)
    _complete(store, "camp1", "0000")
    out = tmp_path / "out"
    with pytest.raises(S3CampaignError):
        store.download("camp1", out)
    res = store.download("camp1", out, allow_partial=True)
    assert res["fetched"] == ["0000"]
    assert (out / "aggregated_results.csv").is_file()
    assert (out / "failed_simulations.csv").is_file()


def test_download_skips_existing(store: S3CampaignStore, tmp_path: Path) -> None:
    _submit(store, tmp_path)
    _complete(store, "camp1", "0000")
    _complete(store, "camp1", "0001")
    out = tmp_path / "out"
    first = store.download("camp1", out)
    assert sorted(first["fetched"]) == ["0000", "0001"]
    second = store.download("camp1", out)
    assert second["fetched"] == []
    assert (out / "work" / "sim" / "0000" / "run.log").is_file()


def test_download_rejects_other_campaign_dir(store: S3CampaignStore, tmp_path: Path) -> None:
    _submit(store, tmp_path, "camp1")
    _submit(store, tmp_path, "camp2")
    for sid in ("0000", "0001"):
        _complete(store, "camp1", sid)
        _complete(store, "camp2", sid)
    out = tmp_path / "out"
    store.download("camp1", out)
    with pytest.raises(S3CampaignError):
        store.download("camp2", out)


def _write_sql(sql_path: Path) -> None:
    conn = sqlite3.connect(str(sql_path))
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


def test_download_s3_campaign_includes_out_osw_and_sql_kpis(
    store: S3CampaignStore, tmp_path: Path
) -> None:
    samples = tmp_path / "samples.json"
    samples.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "sample_id": "0000",
                        "values": {"hvac.cop": 3.5, "envelope.wall_r": 12.0},
                    },
                    {
                        "sample_id": "0001",
                        "values": {"hvac.cop": 2.0, "envelope.wall_r": 9.0},
                    },
                ]
            }
        )
    )
    rec = new_handoff(
        "camp1",
        ["0000", "0001"],
        openstudio_version="3.10.0",
        kpis=None,
        job_ids={"0000": "j0", "0001": "j1"},
    )
    store.write_handoff(rec, samples)

    root: Path = store.storage.root  # type: ignore[attr-defined]
    s0 = root / "camp1" / "work" / "sim" / "0000"
    s0.mkdir(parents=True, exist_ok=True)
    _write_sql(s0 / "eplusout.sql")
    (s0 / "out.osw").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "measure_dir_name": "reporting_179_d",
                        "result": {
                            "step_result": "Success",
                            "step_values": [
                                {"name": "out_total_electricity_179_d_gj", "value": 12.5},
                                {"name": "building_area_m2", "value": 100.0},
                            ],
                        },
                    }
                ]
            }
        )
    )
    (s0 / COMPLETE_MARKER).write_text("")

    s1 = root / "camp1" / "work" / "sim" / "0001"
    s1.mkdir(parents=True, exist_ok=True)
    _write_sql(s1 / "eplusout.sql")
    (s1 / "out.osw").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "measure_dir_name": "reporting_179_d",
                        "result": {
                            "step_result": "Success",
                            "step_values": [
                                {"name": "out_total_electricity_179_d_gj", "value": 15.0},
                                {"name": "building_area_m2", "value": 100.0},
                            ],
                        },
                    }
                ]
            }
        )
    )
    (s1 / COMPLETE_MARKER).write_text("")

    out = tmp_path / "out"
    res = store.download("camp1", out)
    assert res["server_csv"] == out / "download_data.csv"

    agg_df = pd.read_csv(out / "aggregated_results.csv", dtype={"sample_id": str})
    assert "eui_kwh_m2_yr" in agg_df.columns
    assert "reporting_179_d.out_total_electricity_179_d_gj" in agg_df.columns
    assert agg_df["reporting_179_d.out_total_electricity_179_d_gj"].tolist() == [12.5, 15.0]

    server_df = pd.read_csv(
        out / "download_data.csv", dtype={"name": str, "_id": str}, keep_default_na=False
    )
    cols = list(server_df.columns)
    assert cols[:4] == ["name", "_id", "status", "status_message"]
    assert cols[4:6] == ["hvac.cop", "envelope.wall_r"]
    assert "reporting_179_d.out_total_electricity_179_d_gj" in cols
    assert "reporting_179_d.building_area_m2" in cols
    assert "eui_kwh_m2_yr" in cols
    assert cols[-1] == SIMULATION_FAILED_COL
    assert server_df[SIMULATION_FAILED_COL].tolist() == ["", ""]
