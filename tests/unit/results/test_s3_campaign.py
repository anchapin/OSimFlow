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


@pytest.fixture(autouse=True)
def _stub_default_batch_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent unmocked unit tests from making live AWS Batch calls."""
    import boto3

    real_client = boto3.client

    def _fake_client(service_name: str, *args: Any, **kwargs: Any) -> Any:
        if service_name == "batch":
            return mock.MagicMock(describe_jobs=mock.MagicMock(return_value={"jobs": []}))
        return real_client(service_name, *args, **kwargs)

    monkeypatch.setattr(boto3, "client", _fake_client)


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
    assert st["succeeded"] == 2 and st["failed"] == [] and st["failed_count"] == 0
    assert [r.campaign_id for r in store.list_campaigns("camp")] == ["camp1"]
    assert len(store.list_campaigns()) == 2


def _fail(store: S3CampaignStore, cid: str, sid: str) -> None:
    from osimflow.s3_campaign import FAILED_MARKER

    d = store.storage.root / cid / "work" / "sim" / sid  # type: ignore[attr-defined]
    d.mkdir(parents=True, exist_ok=True)
    (d / FAILED_MARKER).write_text("boom")


def test_status_partial_failure(store: S3CampaignStore, tmp_path: Path) -> None:
    _submit(store, tmp_path, "camp1")
    _complete(store, "camp1", "0000")
    _fail(store, "camp1", "0001")
    st = store.status("camp1")
    assert st["state"] == "completed_with_failures"
    assert (st["completed"], st["succeeded"], st["failed"], st["failed_count"]) == (
        2,
        1,
        ["0001"],
        1,
    )


def test_status_all_failed(store: S3CampaignStore, tmp_path: Path) -> None:
    _submit(store, tmp_path, "camp1")
    _fail(store, "camp1", "0000")
    st = store.status("camp1")
    assert st["state"] == "running" and st["pending"] == ["0001"]
    assert (st["completed"], st["succeeded"], st["failed_count"]) == (1, 0, 1)
    _fail(store, "camp1", "0001")
    st = store.status("camp1")
    assert st["state"] == "failed" and st["succeeded"] == 0 and st["completed"] == 2


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
                        "measure_dir_name": "openstudio_results",
                        "result": {
                            "step_result": "Success",
                            "step_values": [
                                {"name": "out_total_electricity_gj", "value": 12.5},
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
                        "measure_dir_name": "openstudio_results",
                        "result": {
                            "step_result": "Success",
                            "step_values": [
                                {"name": "out_total_electricity_gj", "value": 15.0},
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
    assert "openstudio_results.out_total_electricity_gj" in agg_df.columns
    assert agg_df["openstudio_results.out_total_electricity_gj"].tolist() == [12.5, 15.0]

    server_df = pd.read_csv(
        out / "download_data.csv", dtype={"name": str, "_id": str}, keep_default_na=False
    )
    cols = list(server_df.columns)
    assert cols[:4] == ["name", "_id", "status", "status_message"]
    assert cols[4:6] == ["hvac.cop", "envelope.wall_r"]
    assert "openstudio_results.out_total_electricity_gj" in cols
    assert "openstudio_results.building_area_m2" in cols
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


def _failed_sample_remote(store: S3CampaignStore, cid: str, sid: str, err_msg: str) -> None:
    from osimflow.s3_campaign import FAILED_MARKER

    root: Path = store.storage.root  # type: ignore[attr-defined]
    d = root / cid / "work" / "sim" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "eplusout.err").write_text(f"Program Version,EnergyPlus\n   ** Severe  ** {err_msg}\n")
    (d / "out.osw").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "measure_dir_name": "hvac",
                        "result": {"step_result": "Fail", "step_errors": [err_msg]},
                    }
                ]
            }
        )
    )
    (d / FAILED_MARKER).write_text("RuntimeError: simulation failed\n")


def test_download_mixed_incremental_and_stale_kpi_for_failed_sample(
    store: S3CampaignStore, tmp_path: Path
) -> None:
    """Issue #2041: mixed success/failure + incremental download + stale kpi_<failed>.json ignored."""
    samples = tmp_path / "samples.json"
    samples.write_text(
        json.dumps(
            {
                "samples": [
                    {"sample_id": "0000", "values": {"hvac.cop": 3.5, "envelope.wall_r": 12.0}},
                    {"sample_id": "0001", "values": {"hvac.cop": 2.0, "envelope.wall_r": 9.0}},
                ]
            }
        )
    )
    rec = new_handoff(
        "camp_mix",
        ["0000", "0001"],
        openstudio_version="3.10.0",
        kpis=None,
        job_ids={"0000": "j0", "0001": "j1"},
    )
    store.write_handoff(rec, samples)

    out = tmp_path / "out_mix"

    # 1. Empty (0 completed) download with --allow-partial writes download_data.csv with canonical header
    res0 = store.download("camp_mix", out, allow_partial=True)
    assert res0["completed"] == 0
    assert res0["server_csv"] == out / "download_data.csv"
    df0 = pd.read_csv(out / "download_data.csv", dtype=str, keep_default_na=False)
    assert df0.empty
    assert list(df0.columns) == [
        "name",
        "_id",
        "status",
        "status_message",
        "hvac.cop",
        "envelope.wall_r",
        SIMULATION_FAILED_COL,
    ]

    # 2. Sample 0001 fails first; partial download records it once as datapoint failure
    _failed_sample_remote(store, "camp_mix", "0001", "Plant loop convergence failure")
    # Pre-seed a stale kpi_0001.json on disk to ensure incremental download ignores it
    kpi_dir = out / "kpis"
    kpi_dir.mkdir(parents=True, exist_ok=True)
    (kpi_dir / "kpi_0001.json").write_text(
        json.dumps({"sample_id": "0001", "error": "eplusout.sql_missing"})
    )

    res1 = store.download("camp_mix", out, allow_partial=True)
    assert res1["failed"] == ["0001"]
    assert res1["server_csv"] == out / "download_data.csv"
    df1 = pd.read_csv(out / "download_data.csv", dtype=str, keep_default_na=False)
    assert len(df1) == 1
    assert df1["name"].tolist() == ["0001"]
    assert df1["status"].tolist() == ["completed"]
    assert df1["status_message"].tolist() == ["datapoint failure"]
    assert "error" not in df1.columns
    assert "Plant loop convergence failure" in df1[SIMULATION_FAILED_COL].iloc[0]

    # 3. Sample 0000 succeeds; incremental download produces 1 normal row + 1 failure row (no duplicates)
    root: Path = store.storage.root  # type: ignore[attr-defined]
    s0 = root / "camp_mix" / "work" / "sim" / "0000"
    s0.mkdir(parents=True, exist_ok=True)
    _write_sql(s0 / "eplusout.sql")
    (s0 / COMPLETE_MARKER).write_text("")

    res2 = store.download("camp_mix", out)
    assert res2["fetched"] == ["0000"]
    df2 = pd.read_csv(out / "download_data.csv", dtype=str, keep_default_na=False)
    assert len(df2) == 2
    assert df2["name"].tolist() == ["0000", "0001"]
    assert df2["status_message"].tolist() == ["completed normal", "datapoint failure"]
    assert "error" not in df2.columns
    assert df2[SIMULATION_FAILED_COL].iloc[0] == ""
    assert "Plant loop convergence failure" in df2[SIMULATION_FAILED_COL].iloc[1]

    agg_df = pd.read_csv(out / "aggregated_results.csv", dtype={"sample_id": str})
    assert agg_df["sample_id"].tolist() == ["0000"]


def test_download_all_failed_campaign_writes_canonical_download_data_csv(
    store: S3CampaignStore, tmp_path: Path
) -> None:
    """Issue #2041: all-failed campaign writes download_data.csv with canonical columns and no error KPI column."""
    samples = tmp_path / "samples.json"
    samples.write_text(
        json.dumps(
            {
                "samples": [
                    {"sample_id": "0000", "values": {"hvac.cop": 3.5}},
                    {"sample_id": "0001", "values": {"hvac.cop": 2.0}},
                ]
            }
        )
    )
    rec = new_handoff(
        "camp_fail",
        ["0000", "0001"],
        openstudio_version="3.10.0",
        kpis=None,
        job_ids={"0000": "j0", "0001": "j1"},
    )
    store.write_handoff(rec, samples)
    _failed_sample_remote(store, "camp_fail", "0000", "Severe failure 0")
    _failed_sample_remote(store, "camp_fail", "0001", "Severe failure 1")

    out = tmp_path / "out_fail"
    res = store.download("camp_fail", out)
    assert res["failed"] == ["0000", "0001"]
    assert res["server_csv"] == out / "download_data.csv"

    df = pd.read_csv(out / "download_data.csv", dtype=str, keep_default_na=False)
    assert list(df.columns) == [
        "name",
        "_id",
        "status",
        "status_message",
        "hvac.cop",
        SIMULATION_FAILED_COL,
    ]
    assert df["name"].tolist() == ["0000", "0001"]
    assert df["status_message"].tolist() == ["datapoint failure", "datapoint failure"]
    assert "Severe failure 0" in df[SIMULATION_FAILED_COL].iloc[0]
    assert "Severe failure 1" in df[SIMULATION_FAILED_COL].iloc[1]


def test_cli_run_detach_s3_disables_rich_tui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #2044: osimflow run --detach-s3 never starts RichTUI even when is_tui_available() is True."""
    import sys

    from osimflow.__main__ import main

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
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"lighting_power_density": 10.0}}))

    vars_yml = tmp_path / "variables.yml"
    vars_yml.write_text(
        "variables:\n"
        "  - name: SetEnvelopePerformance.wwr\n"
        "    distribution: uniform\n"
        "    min: 0.3\n"
        "    max: 0.5\n"
    )

    bucket_root = tmp_path / "bucket"
    fake_storage = FakeStorage(bucket_root)
    fake_batch = _DetachedBatchClient(image="docker.io/nrel/openstudio@sha256:" + "d" * 64)

    from tests.integration._aws_patch import patch_boto3_client

    with (
        patch_boto3_client(fake_batch),
        mock.patch("osimflow.campaign.build_result_storage", return_value=fake_storage),
        mock.patch("osimflow.storage.build_result_storage", return_value=fake_storage),
        mock.patch("osimflow.tui.is_tui_available", return_value=True),
        mock.patch("osimflow.tui.RichTUI") as mock_tui_cls,
    ):
        rc = main(
            [
                "run",
                "--executor",
                "aws_batch",
                "--aws-batch-queue",
                "osimflow-q",
                "--aws-batch-job-definition",
                "osimflow-openstudio-job-def",
                "--input_variables",
                str(vars_yml),
                "--template_sim_package",
                str(pkg),
                "--n_samples",
                "1",
                "--outdir",
                str(tmp_path / "camp_tui"),
                "--openstudio_version",
                "3.11.0",
                "--container-digest",
                "sha256:" + "d" * 64,
                "--detach-s3",
                "--result-storage-backend",
                "s3",
                "--result-storage-bucket",
                "test-bucket",
            ]
        )

    assert rc == 0
    mock_tui_cls.assert_not_called()
    mock_tui_cls.return_value.start.assert_not_called()


def test_cli_list_and_status_from_s3_json_stdout_purity(
    store: S3CampaignStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #2044: list/status --from-s3 emit strictly valid JSON on stdout and stderr-only errors."""
    from osimflow.__main__ import main

    with mock.patch("osimflow.storage.build_result_storage", return_value=store.storage):
        # 1. list --from-s3 when 0 campaigns exist -> [] on stdout, empty stderr
        rc = main(["list", "--from-s3", "--result-storage-bucket", "test-bucket"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.err == ""
        assert json.loads(captured.out) == []

        # 2. Submit a campaign, verify list --from-s3 and status --from-s3 JSON purity
        _submit(store, tmp_path, "camp_json")
        _complete(store, "camp_json", "0000")

        rc = main(["list", "--from-s3", "--result-storage-bucket", "test-bucket"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.err == ""
        listed = json.loads(captured.out)
        assert isinstance(listed, list) and len(listed) == 1
        assert listed[0]["campaign_id"] == "camp_json"
        assert listed[0]["samples"] == 2

        rc = main(
            [
                "status",
                str(tmp_path / "camp_json"),
                "--from-s3",
                "--result-storage-bucket",
                "test-bucket",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.err == ""
        st = json.loads(captured.out)
        assert st["campaign_id"] == "camp_json"
        assert st["state"] == "running"
        assert st["completed"] == 1
        assert st["total"] == 2

        # 3. Failed status --from-s3 on non-existent campaign -> empty stdout, error on stderr, rc != 0
        rc = main(
            [
                "status",
                str(tmp_path / "does_not_exist"),
                "--from-s3",
                "--result-storage-bucket",
                "test-bucket",
            ]
        )
        captured = capsys.readouterr()
        assert rc != 0
        assert captured.out == ""
        assert "error:" in captured.err

        # 4. Operational S3 error (OSError) during list --from-s3 and status --from-s3
        with mock.patch.object(
            store.storage, "list_results", side_effect=OSError("S3 AccessDenied")
        ):
            rc_list = main(["list", "--from-s3", "--result-storage-bucket", "test-bucket"])
            cap_list = capsys.readouterr()
            assert rc_list == 1
            assert cap_list.out == ""
            assert "error: S3 AccessDenied" in cap_list.err

            rc_st = main(
                [
                    "status",
                    str(tmp_path / "camp_json"),
                    "--from-s3",
                    "--result-storage-bucket",
                    "test-bucket",
                ]
            )
            cap_st = capsys.readouterr()
            assert rc_st == 1
            assert cap_st.out == ""
            assert "error: S3 AccessDenied" in cap_st.err


def test_download_out_osw_only_completed_sample_without_sql(
    store: S3CampaignStore, tmp_path: Path
) -> None:
    """Issue #2051: out.osw-only completed sample (ServerDirectoryCleanup deleted eplusout.sql) succeeds end-to-end."""
    from osimflow._work_scripts.aggregate_results import extract_failure
    from osimflow._work_scripts.extract_kpis import run_extract_kpis

    samples = tmp_path / "samples.json"
    samples.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "sample_id": "0000",
                        "values": {"SetEnvelopePerformance.wwr": 0.35},
                    }
                ]
            }
        )
    )
    rec = new_handoff(
        "camp_osw_only",
        ["0000"],
        openstudio_version="3.11.0",
        kpis=None,
        job_ids={"0000": "j0"},
    )
    store.write_handoff(rec, samples)

    root: Path = store.storage.root  # type: ignore[attr-defined]
    s0 = root / "camp_osw_only" / "work" / "sim" / "0000"
    s0.mkdir(parents=True, exist_ok=True)
    (s0 / "out.osw").write_text(
        json.dumps(
            {
                "completed_status": "Success",
                "steps": [
                    {
                        "measure_dir_name": "openstudio_results",
                        "result": {
                            "step_result": "Success",
                            "step_values": [
                                {"name": "total_site_energy_kbtu", "value": 45000.0},
                                {"name": "building_area_sqft", "value": 10000.0},
                            ],
                        },
                    },
                    {
                        "measure_dir_name": "ServerDirectoryCleanup",
                        "result": {"step_result": "Success", "step_values": []},
                    },
                ],
            }
        )
    )
    (s0 / COMPLETE_MARKER).write_text("")

    # Direct check on run_extract_kpis and extract_failure
    direct_kpi_path = tmp_path / "direct_kpi_0000.json"
    run_extract_kpis(s0, "0000", direct_kpi_path, openstudio_version="3.11.0")
    kpi_doc = json.loads(direct_kpi_path.read_text())
    assert "error" not in kpi_doc["kpis"]
    assert kpi_doc["kpis"]["openstudio_results.total_site_energy_kbtu"] == 45000.0
    assert kpi_doc["kpis"]["openstudio_results.building_area_sqft"] == 10000.0
    assert kpi_doc["quality"]["valid"] is True
    assert any("SQL-derived quality checks skipped" in w for w in kpi_doc["quality"]["warnings"])
    assert extract_failure(s0) is None

    # End-to-end S3CampaignStore.download() verification
    out = tmp_path / "out_osw_only"
    res = store.download("camp_osw_only", out)
    assert res["completed"] == 1
    assert res["failed"] == []
    assert res["server_csv"] == out / "download_data.csv"

    failed_df = pd.read_csv(out / "failed_simulations.csv")
    assert failed_df.empty

    agg_df = pd.read_csv(out / "aggregated_results.csv", dtype={"sample_id": str})
    assert agg_df["sample_id"].tolist() == ["0000"]
    assert agg_df["openstudio_results.total_site_energy_kbtu"].iloc[0] == pytest.approx(45000.0)
    assert agg_df["openstudio_results.building_area_sqft"].iloc[0] == pytest.approx(10000.0)

    dl_df = pd.read_csv(out / "download_data.csv", dtype=str, keep_default_na=False)
    assert len(dl_df) == 1
    assert dl_df["name"].tolist() == ["0000"]
    assert dl_df["status"].tolist() == ["completed"]
    assert dl_df["status_message"].tolist() == ["completed normal"]
    assert float(dl_df["openstudio_results.total_site_energy_kbtu"].iloc[0]) == pytest.approx(
        45000.0
    )
    assert float(dl_df["openstudio_results.building_area_sqft"].iloc[0]) == pytest.approx(10000.0)


def test_handoff_from_json_normalizes_non_dict_job_ids() -> None:
    """Issue #2058: S3Handoff.from_json coerces non-dict job_ids to {} and normalizes keys/values."""
    base = {
        "campaign_id": "camp_norm",
        "submitted_at": 1234.5,
        "sample_ids": ["0000", "0001"],
    }
    for bad_job_ids in (None, ["j0", "j1"], "not-a-dict", 42):
        raw = json.dumps({**base, "job_ids": bad_job_ids})
        rec = S3Handoff.from_json(raw)
        assert rec.job_ids == {}

    rec_typed = S3Handoff.from_json(json.dumps({**base, "job_ids": {"0000": 999, "0001": None}}))
    assert rec_typed.job_ids == {"0000": "999", "0001": None}


def test_reconcile_terminal_failed_batch_jobs_oom_and_cannot_pull(tmp_path: Path) -> None:
    """Issue #2058: terminal FAILED Batch jobs (OOM / CannotPullContainerError) write _OSIMFLOW_FAILED and transition to failed."""
    from osimflow.s3_campaign import FAILED_MARKER

    batch_client = mock.MagicMock()
    batch_client.describe_jobs.return_value = {
        "jobs": [
            {
                "jobId": "job-oom",
                "status": "FAILED",
                "statusReason": "Essential container in task exited",
                "container": {
                    "exitCode": 137,
                    "reason": "OutOfMemoryError: Container killed due to memory usage",
                },
            },
            {
                "jobId": "job-pull",
                "status": "FAILED",
                "statusReason": "Task failed to start",
                "container": {
                    "reason": "CannotPullContainerError: pull image manifest has been retried 5 time(s)",
                },
            },
        ]
    }
    storage = FakeStorage(tmp_path / "bucket")
    store = S3CampaignStore(storage, tmp_path / "scratch", batch_client=batch_client)

    samples = tmp_path / "samples.json"
    samples.write_text(json.dumps({"samples": []}))
    # 0002 is missing in record.job_ids so reconciliation falls back to recorded_jobs (_jobs/0002.json)
    rec = new_handoff(
        "camp_batch_fail",
        ["0000", "0001", "0002"],
        openstudio_version="3.11.0",
        kpis=None,
        job_ids={"0000": "job-ok", "0001": "job-oom", "0002": None},
    )
    store.write_handoff(rec, samples)
    store.record_job("camp_batch_fail", "0002", "job-pull", "osimflow-camp_batch_fail-0002")
    _complete(store, "camp_batch_fail", "0000")

    st = store.status("camp_batch_fail")
    assert st["state"] == "completed_with_failures"
    assert st["total"] == 3
    assert st["completed"] == 3
    assert st["succeeded"] == 1
    assert st["failed_count"] == 2
    assert st["failed"] == ["0001", "0002"]
    assert st["pending"] == []
    assert batch_client.describe_jobs.call_count == 1
    assert set(batch_client.describe_jobs.call_args.kwargs["jobs"]) == {"job-oom", "job-pull"}

    # _OSIMFLOW_FAILED markers persisted to S3 with formatted Batch diagnostic details
    m1 = (storage.root / "camp_batch_fail" / "work" / "sim" / "0001" / FAILED_MARKER).read_text()
    assert "AWS Batch job job-oom FAILED (exit_code=137)" in m1
    assert "OutOfMemoryError: Container killed due to memory usage" in m1
    assert "Essential container in task exited" in m1

    m2 = (storage.root / "camp_batch_fail" / "work" / "sim" / "0002" / FAILED_MARKER).read_text()
    assert "AWS Batch job job-pull FAILED" in m2
    assert "CannotPullContainerError" in m2

    # Subsequent status call sees persisted S3 markers and does not re-query describe_jobs
    batch_client.describe_jobs.reset_mock()
    st2 = store.status("camp_batch_fail")
    assert st2 == st
    batch_client.describe_jobs.assert_not_called()


def test_reconcile_non_terminal_batch_states_remain_pending(tmp_path: Path) -> None:
    """Issue #2058: RUNNING / RUNNABLE (on retry) / SUBMITTED / STARTING jobs remain pending."""
    from osimflow.s3_campaign import FAILED_MARKER, RETRYING_STATE

    batch_client = mock.MagicMock()
    batch_client.describe_jobs.return_value = {
        "jobs": [
            {"jobId": "j0", "status": "RUNNING"},
            {"jobId": "j1", "status": "RUNNABLE", "statusReason": "Retrying attempt 2"},
            {"jobId": "j2", "status": "SUBMITTED"},
        ]
    }
    storage = FakeStorage(tmp_path / "bucket")
    store = S3CampaignStore(storage, tmp_path / "scratch", batch_client=batch_client)
    rec = new_handoff(
        "camp_running",
        ["0000", "0001", "0002"],
        openstudio_version="3.11.0",
        kpis=None,
        job_ids={"0000": "j0", "0001": "j1", "0002": "j2"},
    )
    store.write_handoff(rec)
    # 0001 has a retrying _OSIMFLOW_FAILED marker from attempt 2 startup
    retry_dir = storage.root / "camp_running" / "work" / "sim" / "0001"
    retry_dir.mkdir(parents=True, exist_ok=True)
    (retry_dir / FAILED_MARKER).write_text(RETRYING_STATE)

    st = store.status("camp_running")
    assert st["state"] == "running"
    assert st["completed"] == 0
    assert st["failed"] == []
    assert st["pending"] == ["0000", "0001", "0002"]
    assert not (storage.root / "camp_running" / "work" / "sim" / "0000" / FAILED_MARKER).exists()
    assert (retry_dir / FAILED_MARKER).read_text() == RETRYING_STATE


def test_reconcile_chunks_over_100_pending_jobs(tmp_path: Path) -> None:
    """Issue #2058: >100 pending job IDs are queried in chunks of <= 100."""
    sample_ids = [f"{i:04d}" for i in range(205)]
    job_ids = {sid: f"job-{sid}" for sid in sample_ids}
    chunk_lengths: list[int] = []

    def _describe_jobs(*, jobs: list[str]) -> dict[str, Any]:
        chunk_lengths.append(len(jobs))
        return {
            "jobs": [
                {
                    "jobId": jid,
                    "status": "FAILED"
                    if jid in {"job-0000", "job-0150", "job-0204"}
                    else "RUNNING",
                    "attempts": [
                        {
                            "container": {
                                "exitCode": 137,
                                "reason": "OutOfMemoryError: Container killed",
                            }
                        }
                    ],
                }
                for jid in jobs
            ]
        }

    batch_client = mock.MagicMock()
    batch_client.describe_jobs.side_effect = _describe_jobs
    storage = FakeStorage(tmp_path / "bucket")
    store = S3CampaignStore(storage, tmp_path / "scratch", batch_client=batch_client)
    rec = new_handoff(
        "camp_205",
        sample_ids,
        openstudio_version="3.11.0",
        kpis=None,
        job_ids=job_ids,
    )
    store.write_handoff(rec)

    states = store.sample_states("camp_205")
    assert chunk_lengths == [100, 100, 5]
    assert states == {"0000": "failed", "0150": "failed", "0204": "failed"}


def test_reconcile_race_guard_when_complete_marker_appears_before_failed_write(
    tmp_path: Path,
) -> None:
    """Issue #2058: if _OSIMFLOW_COMPLETE appears in S3 before _OSIMFLOW_FAILED is written, sample is marked complete."""
    from osimflow.s3_campaign import FAILED_MARKER

    storage = FakeStorage(tmp_path / "bucket")

    def _describe_jobs_with_late_complete(*, jobs: list[str]) -> dict[str, Any]:
        # Worker finishes uploading _OSIMFLOW_COMPLETE between initial S3 scan and describe_jobs response
        _complete(store, "camp_race", "0000")
        return {
            "jobs": [
                {
                    "jobId": jobs[0],
                    "status": "FAILED",
                    "statusReason": "Essential container in task exited",
                    "container": {"exitCode": 1, "reason": "Late exit after upload"},
                }
            ]
        }

    batch_client = mock.MagicMock()
    batch_client.describe_jobs.side_effect = _describe_jobs_with_late_complete
    store = S3CampaignStore(storage, tmp_path / "scratch", batch_client=batch_client)
    rec = new_handoff(
        "camp_race",
        ["0000"],
        openstudio_version="3.11.0",
        kpis=None,
        job_ids={"0000": "job-race"},
    )
    store.write_handoff(rec)

    states = store.sample_states(campaign_id="camp_race")
    assert states == {"0000": "complete"}
    assert not (storage.root / "camp_race" / "work" / "sim" / "0000" / FAILED_MARKER).exists()


def test_reconcile_graceful_fallback_on_describe_jobs_or_boto3_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #2058: describe_jobs / boto3 initialization failures log a warning and fall back to S3-marker-only status."""
    import boto3
    from botocore.exceptions import NoCredentialsError, NoRegionError

    storage = FakeStorage(tmp_path / "bucket")
    rec = new_handoff(
        "camp_err",
        ["0000", "0001"],
        openstudio_version="3.11.0",
        kpis=None,
        job_ids={"0000": "j0", "0001": "j1"},
    )

    # 1. describe_jobs raises RuntimeError / ClientError -> falls back gracefully
    failing_client = mock.MagicMock()
    failing_client.describe_jobs.side_effect = RuntimeError("Batch API throttled")
    store_err = S3CampaignStore(storage, tmp_path / "scratch1", batch_client=failing_client)
    store_err.write_handoff(rec)
    _complete(store_err, "camp_err", "0000")

    st = store_err.status("camp_err")
    assert st["state"] == "running"
    assert st["completed"] == 1
    assert st["failed"] == []
    assert st["pending"] == ["0001"]

    # 2. Lazy boto3.client("batch") raises NoRegionError / NoCredentialsError -> returns None and falls back
    def _raise_no_region(*_a: Any, **_kw: Any) -> Any:
        raise NoRegionError()

    monkeypatch.setattr(boto3, "client", _raise_no_region)
    store_lazy = S3CampaignStore(storage, tmp_path / "scratch2")
    st_lazy = store_lazy.status("camp_err")
    assert st_lazy["pending"] == ["0001"]

    def _raise_no_creds(*_a: Any, **_kw: Any) -> Any:
        raise NoCredentialsError()

    monkeypatch.setattr(boto3, "client", _raise_no_creds)
    store_creds = S3CampaignStore(storage, tmp_path / "scratch3")
    assert store_creds.sample_states(rec) == {"0000": "complete"}

    # 3. Non-aws_batch executor skips Batch reconciliation entirely
    local_rec = S3Handoff(
        campaign_id="camp_local",
        submitted_at=1.0,
        sample_ids=["0000"],
        executor="local",
        job_ids={"0000": "j0"},
    )
    store_err.write_handoff(local_rec)
    failing_client.describe_jobs.reset_mock()
    assert store_err.sample_states(local_rec) == {}
    failing_client.describe_jobs.assert_not_called()

    # 4. Calling sample_states(None) without campaign_id raises ValueError
    with pytest.raises(ValueError, match="requires either record or campaign_id"):
        store_err.sample_states()


def test_download_propagates_batch_failure_reason_to_failed_simulations_and_download_data_csv(
    tmp_path: Path,
) -> None:
    """Issue #2058: Batch OOM failure reason is preserved through download() into failed_simulations.csv and download_data.csv."""
    from osimflow.s3_campaign import FAILED_MARKER

    batch_client = mock.MagicMock()
    batch_client.describe_jobs.return_value = {
        "jobs": [
            {
                "jobId": "job-oom-0001",
                "status": "FAILED",
                "container": {
                    "exitCode": 137,
                    "reason": "OutOfMemoryError: Container killed due to memory usage",
                },
            }
        ]
    }
    storage = FakeStorage(tmp_path / "bucket")
    store = S3CampaignStore(storage, tmp_path / "scratch", batch_client=batch_client)

    samples = tmp_path / "samples.json"
    samples.write_text(
        json.dumps(
            {
                "samples": [
                    {"sample_id": "0000", "values": {"hvac.cop": 3.5}},
                    {"sample_id": "0001", "values": {"hvac.cop": 2.0}},
                ]
            }
        )
    )
    rec = new_handoff(
        "camp_oom_dl",
        ["0000", "0001"],
        openstudio_version="3.11.0",
        kpis=None,
        job_ids={"0000": "job-ok-0000", "0001": "job-oom-0001"},
    )
    store.write_handoff(rec, samples)

    # Sample 0000 succeeds; sample 0001 died from OOM before uploading anything to S3
    s0 = storage.root / "camp_oom_dl" / "work" / "sim" / "0000"
    s0.mkdir(parents=True, exist_ok=True)
    _write_sql(s0 / "eplusout.sql")
    (s0 / COMPLETE_MARKER).write_text("")

    out = tmp_path / "out_oom_dl"
    res = store.download("camp_oom_dl", out)
    assert res["completed"] == 2
    assert res["failed"] == ["0001"]
    assert sorted(res["fetched"]) == ["0000", "0001"]

    expected_msg = (
        "AWS Batch job job-oom-0001 FAILED (exit_code=137): "
        "OutOfMemoryError: Container killed due to memory usage"
    )
    local_failed_marker = out / "work" / "sim" / "0001" / FAILED_MARKER
    assert local_failed_marker.is_file()
    assert local_failed_marker.read_text().strip() == expected_msg

    failed_df = pd.read_csv(out / "failed_simulations.csv", dtype=str, keep_default_na=False)
    assert len(failed_df) == 1
    assert failed_df["sample_id"].iloc[0] == "0001"
    assert failed_df["error_summary"].iloc[0] == expected_msg

    dl_df = pd.read_csv(out / "download_data.csv", dtype=str, keep_default_na=False)
    assert len(dl_df) == 2
    assert dl_df["name"].tolist() == ["0000", "0001"]
    assert dl_df["status"].tolist() == ["completed", "completed"]
    assert dl_df["status_message"].tolist() == ["completed normal", "datapoint failure"]
    assert dl_df[SIMULATION_FAILED_COL].iloc[0] == ""
    assert dl_df[SIMULATION_FAILED_COL].iloc[1] == expected_msg


def test_download_detects_post_energyplus_reporting_measure_and_severe_failures_when_sql_exists(
    store: S3CampaignStore, tmp_path: Path
) -> None:
    """Issue #2059: when eplusout.sql exists alongside out.osw Fail, _OSIMFLOW_FAILED, or eplusout.err Severe,
    samples are not duplicated across aggregated_results.csv, failed_simulations.csv, or download_data.csv."""
    from osimflow.s3_campaign import FAILED_MARKER

    samples = tmp_path / "samples.json"
    samples.write_text(
        json.dumps(
            {
                "samples": [
                    {"sample_id": "0000", "values": {"hvac.cop": 3.5}},
                    {"sample_id": "0001", "values": {"hvac.cop": 3.0}},
                    {"sample_id": "0002", "values": {"hvac.cop": 2.5}},
                    {"sample_id": "0003", "values": {"hvac.cop": 2.0}},
                ]
            }
        )
    )
    rec = new_handoff(
        "camp_post_ep",
        ["0000", "0001", "0002", "0003"],
        openstudio_version="3.11.0",
        kpis=None,
        job_ids={"0000": "j0", "0001": "j1", "0002": "j2", "0003": "j3"},
    )
    store.write_handoff(rec, samples)

    root: Path = store.storage.root  # type: ignore[attr-defined]

    # 0000: clean success (eplusout.sql + out.osw Success)
    s0 = root / "camp_post_ep" / "work" / "sim" / "0000"
    s0.mkdir(parents=True, exist_ok=True)
    _write_sql(s0 / "eplusout.sql")
    (s0 / "out.osw").write_text(
        json.dumps(
            {
                "completed_status": "Success",
                "steps": [
                    {
                        "measure_dir_name": "openstudio_results",
                        "result": {
                            "step_result": "Success",
                            "step_values": [{"name": "building_area_sqft", "value": 10000.0}],
                        },
                    }
                ],
            }
        )
    )
    (s0 / COMPLETE_MARKER).write_text("")

    # 0001: EnergyPlus succeeded (eplusout.sql exists), but openstudio_results failed in out.osw
    s1 = root / "camp_post_ep" / "work" / "sim" / "0001"
    s1.mkdir(parents=True, exist_ok=True)
    _write_sql(s1 / "eplusout.sql")
    (s1 / "out.osw").write_text(
        json.dumps(
            {
                "completed_status": "Fail",
                "steps": [
                    {
                        "measure_dir_name": "openstudio_results",
                        "result": {
                            "step_result": "Fail",
                            "step_errors": ["Reporting measure SQL query returned nil"],
                        },
                    }
                ],
            }
        )
    )
    (s1 / COMPLETE_MARKER).write_text("")

    # 0002: EnergyPlus wrote eplusout.sql AND a Severe error in eplusout.err
    s2 = root / "camp_post_ep" / "work" / "sim" / "0002"
    s2.mkdir(parents=True, exist_ok=True)
    _write_sql(s2 / "eplusout.sql")
    (s2 / "eplusout.err").write_text(
        "Program Version,EnergyPlus\n   ** Severe  ** Plant loop did not converge\n"
    )
    (s2 / COMPLETE_MARKER).write_text("")

    # 0003: eplusout.sql exists alongside _OSIMFLOW_FAILED marker
    s3 = root / "camp_post_ep" / "work" / "sim" / "0003"
    s3.mkdir(parents=True, exist_ok=True)
    _write_sql(s3 / "eplusout.sql")
    (s3 / FAILED_MARKER).write_text("Worker died after EnergyPlus finished\n")

    out = tmp_path / "out_post_ep"
    store.download("camp_post_ep", out)

    # Verify KPI files were NOT generated for 0001, 0002, or 0003
    assert (out / "kpis" / "kpi_0000.json").is_file()
    assert not (out / "kpis" / "kpi_0001.json").exists()
    assert not (out / "kpis" / "kpi_0002.json").exists()
    assert not (out / "kpis" / "kpi_0003.json").exists()

    agg_df = pd.read_csv(out / "aggregated_results.csv", dtype={"sample_id": str})
    fail_df = pd.read_csv(out / "failed_simulations.csv", dtype={"sample_id": str})
    assert agg_df["sample_id"].tolist() == ["0000"]
    assert fail_df["sample_id"].tolist() == ["0001", "0002", "0003"]
    assert set(agg_df["sample_id"]).isdisjoint(set(fail_df["sample_id"]))

    dl_df = pd.read_csv(out / "download_data.csv", dtype=str, keep_default_na=False)
    assert len(dl_df) == 4
    assert dl_df["name"].tolist() == ["0000", "0001", "0002", "0003"]
    assert dl_df["status_message"].tolist() == [
        "completed normal",
        "datapoint failure",
        "datapoint failure",
        "datapoint failure",
    ]
    assert dl_df[SIMULATION_FAILED_COL].iloc[0] == ""
    assert (
        dl_df[SIMULATION_FAILED_COL].iloc[1]
        == "openstudio_results: Reporting measure SQL query returned nil"
    )
    assert "Plant loop did not converge" in dl_df[SIMULATION_FAILED_COL].iloc[2]
    assert dl_df[SIMULATION_FAILED_COL].iloc[3] == "Worker died after EnergyPlus finished"
