"""Tests for S3-backed detached campaigns (issue #1873)."""

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

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


class _DetachedBatchClient:
    """Fake boto3 Batch client whose jobs remain SUBMITTED (never SUCCEEDED).

    Also exposes a realistic ``nrel/openstudio:3.11.0`` job definition image so
    ``AWSBatchExecutor._validate_job_definition_image`` runs against every
    submitted job. Any synchronous ``apply_<sid>`` submission with
    ``ghcr.io/anchapin/scientific_python_image:latest`` will fail image
    validation immediately, and any ``handle.result()`` wait on a Batch handle
    will time out because jobs never transition to ``SUCCEEDED``.
    """

    def __init__(self, image: str = "docker.io/nrel/openstudio:3.11.0") -> None:
        self.image = image
        self.submit_job_calls: list[dict[str, Any]] = []
        self._counter = 0

    def submit_job(self, **kwargs: Any) -> dict[str, str]:
        self._counter += 1
        job_id = f"batch-job-{self._counter:04d}"
        self.submit_job_calls.append({"jobId": job_id, **kwargs})
        return {"jobId": job_id, "jobName": str(kwargs.get("jobName", ""))}

    def describe_job_definitions(self, **kwargs: Any) -> dict[str, Any]:
        job_defs = kwargs.get("jobDefinitions") or ["osimflow-openstudio-job-def"]
        return {
            "jobDefinitions": [
                {
                    "jobDefinitionName": str(job_defs[0]),
                    "revision": 1,
                    "status": "ACTIVE",
                    "containerProperties": {"image": self.image},
                }
            ]
        }

    def describe_jobs(self, *, jobs: list[str]) -> dict[str, Any]:
        return {
            "jobs": [
                {
                    "jobId": jid,
                    "status": "SUBMITTED",
                    "statusReason": "detached test — remains in queue",
                }
                for jid in jobs
            ]
        }


def test_detach_s3_aws_batch_osw_measure_mutation_submits_only_sim_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #2024: AWSBatchExecutor + detach_s3=True mutates OSW locally without apply_<sid> Batch jobs or duplicate S3 staging."""
    import sys

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import AWSBatchExecutor

    monkeypatch.delenv("OSIMFLOW_STUB_SIM", raising=False)
    monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", "test-signing-secret-0123456789abcdef")
    monkeypatch.setitem(sys.modules, "openstudio", None)

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "seed_file": "model.osm",
                "steps": [
                    {
                        "measure_dir_name": "SetEnvelopePerformance",
                        "arguments": {"wwr": 0.4},
                    }
                ],
            }
        )
    )
    (pkg / "model.osm").write_text(
        "OS:Version,\n  {00000000-0000-0000-0000-000000000000},\n  3.11.0;\n"
    )

    vars_yml = tmp_path / "variables.yml"
    vars_yml.write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: SetEnvelopePerformance.wwr\n"
        "    distribution: uniform\n"
        "    min: 0.25\n"
        "    max: 0.55\n"
    )

    bucket_root = tmp_path / "bucket"
    fake_storage = FakeStorage(bucket_root)
    store = S3CampaignStore(fake_storage, tmp_path / "scratch")
    fake_batch = _DetachedBatchClient(image="docker.io/nrel/openstudio@sha256:" + "a" * 64)

    cfg = CampaignConfig(
        input_variables=vars_yml,
        template_sim_package=pkg,
        n_samples=2,
        outdir=tmp_path / "camp_osw",
        openstudio_version="3.11.0",
        detach_s3=True,
        result_storage_backend="s3",
        result_storage_bucket="test-bucket",
        container_digest="sha256:" + "a" * 64,
    )

    from tests.integration._aws_patch import patch_boto3_client

    with (
        patch_boto3_client(fake_batch),
        mock.patch("osimflow.campaign.build_result_storage", return_value=fake_storage),
        mock.patch("osimflow.storage.build_result_storage", return_value=fake_storage),
    ):
        executor = AWSBatchExecutor(
            job_queue="osimflow-q",
            job_definition="osimflow-openstudio-job-def",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )
        campaign = Campaign(cfg, executor)
        campaign._detach_store_obj = store
        result = campaign.run()

    assert result["status"] == "detached"
    job_names = [str(c["jobName"]) for c in fake_batch.submit_job_calls]
    assert job_names == ["osimflow-camp_osw-0001", "osimflow-camp_osw-0002"]
    assert not any("apply" in name for name in job_names)

    # Verify task payloads submitted to Batch are exclusively "sim" steps (no "apply" step).
    for call in fake_batch.submit_job_calls:
        env_list = call["containerOverrides"]["environment"]
        env_map = {item["name"]: item["value"] for item in env_list}
        payload = json.loads(env_map["OSIMFLOW_TASK_PAYLOAD"])
        assert payload["step"] == "sim"

    # No premature/duplicate apply_<sid> manifests in S3: sample packages are staged once (for sim_<sid>).
    manifests = [
        json.loads(p.read_text()) for p in (bucket_root / "_inputs" / "manifests").glob("*.json")
    ]
    sample_manifests = [m for m in manifests if m["name"] in {"0001", "0002"}]
    assert len(sample_manifests) == 2
    assert not any("apply" in str(m.get("name", "")) for m in manifests)

    for sid in ("0001", "0002"):
        staged_osw = json.loads((cfg.work_dir / "apply" / sid / "workflow.osw").read_text())
        wwr_val = staged_osw["steps"][0]["arguments"]["wwr"]
        assert 0.25 <= float(wwr_val) <= 0.55
        assert float(wwr_val) != 0.4


def test_detach_s3_aws_batch_osm_attribute_variables_deferred_to_sim_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #2024: .osm attribute variables work under --detach-s3 without local bindings and execute once in the sim worker."""
    import sys
    from types import SimpleNamespace

    from osimflow import Campaign, CampaignConfig
    from osimflow.campaign.helpers import _package_content_digest
    from osimflow.campaign.steps import _parameterized_package_content_digest
    from osimflow.executors import AWSBatchExecutor
    from osimflow.work import _PENDING_OSM_PARAMS_FILE, run_openstudio_sim

    monkeypatch.delenv("OSIMFLOW_STUB_SIM", raising=False)
    monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", "test-signing-secret-0123456789abcdef")
    monkeypatch.setitem(sys.modules, "openstudio", None)

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "seed_file": "model.osm",
                "steps": [
                    {
                        "measure_dir_name": "SetEnvelopePerformance",
                        "arguments": {"wwr": 0.4},
                    }
                ],
            }
        )
    )
    (pkg / "model.osm").write_text(
        "OS:Version,\n  {00000000-0000-0000-0000-000000000000},\n  3.11.0;\n"
    )

    vars_yml = tmp_path / "variables.yml"
    vars_yml.write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: SetEnvelopePerformance.wwr\n"
        "    distribution: uniform\n"
        "    min: 0.3\n"
        "    max: 0.5\n"
        "  - name: lighting_power_density\n"
        "    distribution: uniform\n"
        "    min: 8.0\n"
        "    max: 12.0\n"
    )

    bucket_root = tmp_path / "bucket"
    fake_storage = FakeStorage(bucket_root)
    store = S3CampaignStore(fake_storage, tmp_path / "scratch")
    fake_batch = _DetachedBatchClient(image="docker.io/nrel/openstudio@sha256:" + "b" * 64)

    cfg = CampaignConfig(
        input_variables=vars_yml,
        template_sim_package=pkg,
        n_samples=1,
        outdir=tmp_path / "camp_osm",
        openstudio_version="3.11.0",
        detach_s3=True,
        result_storage_backend="s3",
        result_storage_bucket="test-bucket",
        container_digest="sha256:" + "b" * 64,
    )

    from tests.integration._aws_patch import patch_boto3_client

    with (
        patch_boto3_client(fake_batch),
        mock.patch("osimflow.campaign.build_result_storage", return_value=fake_storage),
        mock.patch("osimflow.storage.build_result_storage", return_value=fake_storage),
    ):
        executor = AWSBatchExecutor(
            job_queue="osimflow-q",
            job_definition="osimflow-openstudio-job-def",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )
        campaign = Campaign(cfg, executor)
        campaign._detach_store_obj = store
        result = campaign.run()

    assert result["status"] == "detached"
    assert [str(c["jobName"]) for c in fake_batch.submit_job_calls] == ["osimflow-camp_osm-0001"]

    # Sample package 0001 staged once in S3 (for sim_0001; no premature apply_0001 manifest).
    manifests = [
        json.loads(p.read_text()) for p in (bucket_root / "_inputs" / "manifests").glob("*.json")
    ]
    sample_manifests = [m for m in manifests if m["name"] == "0001"]
    assert len(sample_manifests) == 1
    manifest_data = sample_manifests[0]
    staged_rel_paths = {entry["path"] for entry in manifest_data["files"]}
    assert _PENDING_OSM_PARAMS_FILE in staged_rel_paths

    staged_pkg = cfg.work_dir / "apply" / "0001"
    pending_file = staged_pkg / _PENDING_OSM_PARAMS_FILE
    assert pending_file.is_file()
    pending_params = json.loads(pending_file.read_text())
    assert set(pending_params.keys()) == {"lighting_power_density"}
    assert 8.0 <= float(pending_params["lighting_power_density"]) <= 12.0

    # Package content digest changes when deferred .osm attribute values change.
    digest_before = _parameterized_package_content_digest(staged_pkg, _package_content_digest)
    pending_file.write_text(json.dumps({"lighting_power_density": 99.0}))
    digest_after = _parameterized_package_content_digest(staged_pkg, _package_content_digest)
    assert digest_before != digest_after
    pending_file.write_text(json.dumps(pending_params))

    # Simulate the nrel/openstudio worker container executing run_openstudio_sim:
    # pending .osm attribute mutations must run once before simulation and remove the pending file.
    applied_lpd: list[float] = []

    class _FakeModel:
        def setString(self, attr: str, val: str) -> bool:
            assert attr == "lighting_power_density"
            applied_lpd.append(float(val))
            return True

        def save(self, path: str, overwrite: bool = True) -> bool:
            Path(path).write_text(f"SAVED_LPD={applied_lpd[-1]}\n")
            return True

    fake_openstudio = SimpleNamespace(
        openstudiomodelcore=SimpleNamespace(
            Model=SimpleNamespace(
                load=lambda _p: SimpleNamespace(
                    is_initialized=lambda: True,
                    get=lambda: _FakeModel(),
                )
            )
        )
    )
    monkeypatch.setitem(sys.modules, "openstudio", fake_openstudio)
    monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
    with mock.patch("osimflow.work._is_stub_mode", side_effect=[False, False, True]):
        sim_out = run_openstudio_sim(
            staged_pkg,
            "0001",
            "3.11.0",
            tmp_path / "worker_sim_out",
            simulate_work_s=0.0,
            health_check_interval=0.0,
        )

    assert (sim_out / "eplusout.sql").is_file()
    assert len(applied_lpd) == 1
    assert applied_lpd[0] == pytest.approx(float(pending_params["lighting_power_density"]))
    assert not pending_file.exists()


def test_detach_s3_aws_batch_stages_external_epw_and_hits_apply_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #2024: _stage_external_epw, APPLY_PARAMETERS cache hits, and _verify_step_inputs are preserved under --detach-s3."""
    import sys

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import AWSBatchExecutor

    monkeypatch.delenv("OSIMFLOW_STUB_SIM", raising=False)
    monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", "test-signing-secret-0123456789abcdef")
    monkeypatch.setitem(sys.modules, "openstudio", None)

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "seed_file": "model.osm",
                "weather_file": "",
                "steps": [
                    {
                        "measure_dir_name": "SetEnvelopePerformance",
                        "arguments": {"wwr": 0.4},
                    }
                ],
            }
        )
    )
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"lighting_power_density": 10.0}}))

    ext_epw = tmp_path / "external_weather" / "Chicago.epw"
    ext_epw.parent.mkdir(parents=True)
    ext_epw.write_text(
        "LOCATION,Chicago,IL,USA,725300,41.78,-87.75,-6.0,190.0\n"
        "DATA PERIODS,1,1,Data,Sunday, 1/ 1,12/31,8760,\n"
    )

    csv_path = tmp_path / "samples.csv"
    csv_path.write_text(
        f"SetEnvelopePerformance.wwr,weather_file\n0.33,{ext_epw}\n0.47,{ext_epw}\n"
    )
    vars_yml = tmp_path / "variables.yml"
    vars_yml.write_text(
        "algorithm:\n"
        "  type: custom\n"
        f"  samples_file: {csv_path}\n"
        "variables:\n"
        "  - name: SetEnvelopePerformance.wwr\n"
        "    distribution: uniform\n"
        "    min: 0.3\n"
        "    max: 0.5\n"
    )

    bucket_root = tmp_path / "bucket"
    fake_storage = FakeStorage(bucket_root)
    store = S3CampaignStore(fake_storage, tmp_path / "scratch")
    fake_batch = _DetachedBatchClient(image="docker.io/nrel/openstudio@sha256:" + "c" * 64)

    cfg = CampaignConfig(
        input_variables=vars_yml,
        template_sim_package=pkg,
        n_samples=2,
        outdir=tmp_path / "camp_epw",
        openstudio_version="3.11.0",
        algorithm="custom",
        detach_s3=True,
        result_storage_backend="s3",
        result_storage_bucket="test-bucket",
        container_digest="sha256:" + "c" * 64,
    )

    from tests.integration._aws_patch import patch_boto3_client

    with (
        patch_boto3_client(fake_batch),
        mock.patch("osimflow.campaign.build_result_storage", return_value=fake_storage),
        mock.patch("osimflow.storage.build_result_storage", return_value=fake_storage),
    ):
        executor = AWSBatchExecutor(
            job_queue="osimflow-q",
            job_definition="osimflow-openstudio-job-def",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )
        campaign = Campaign(cfg, executor)
        campaign._detach_store_obj = store
        result = campaign.run()

        # Verify _stage_external_epw copied external EPW into weather/ and rewrote relative path.
        for sid in ("0001", "0002"):
            apply_dir = cfg.work_dir / "apply" / sid
            assert (apply_dir / "weather" / "Chicago.epw").is_file()
            osw_data = json.loads((apply_dir / "workflow.osw").read_text())
            assert osw_data["weather_file"] == "weather/Chicago.epw"
        manifests = [
            json.loads(p.read_text())
            for p in (bucket_root / "_inputs" / "manifests").glob("*.json")
        ]
        sample_manifests = [m for m in manifests if m["name"] in {"0001", "0002"}]
        assert len(sample_manifests) == 2
        for mdata in sample_manifests:
            assert "weather/Chicago.epw" in {entry["path"] for entry in mdata["files"]}

        # Verify _verify_step_inputs("RUN_OPENSTUDIO_SIM") passes.
        campaign._verify_step_inputs("RUN_OPENSTUDIO_SIM")

        # Re-run step_apply_parameters with the same samples and verify cache hit behavior.
        samples_data = json.loads(cfg.samples_file.read_text())["samples"]
        with mock.patch.object(campaign, "_run_local_apply_handle") as mock_local_apply:
            warm_out = campaign.step_apply_parameters(samples_data)
            mock_local_apply.assert_not_called()
        assert set(warm_out.keys()) == {"0001", "0002"}
        for sid in ("0001", "0002"):
            assert campaign._sample_state[sid]["apply_status"] == "cached"

    assert result["status"] == "detached"
