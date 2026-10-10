"""Crash-safe / concurrency-safe detached submit (issue #1881)."""

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from tests.unit.test_s3_campaign import FakeStorage

from osimflow.executors.aws_batch_executor import AWSBatchExecutor
from osimflow.s3_campaign import (
    CLAIM_ACQUIRED,
    CLAIM_ALREADY_SUBMITTED,
    CLAIM_IN_PROGRESS,
    S3CampaignError,
    S3CampaignStore,
    batch_job_name,
    new_handoff,
)
from osimflow.storage import LocalStorage, S3Storage


@pytest.fixture
def store(tmp_path: Path) -> S3CampaignStore:
    return S3CampaignStore(FakeStorage(tmp_path / "bucket"), tmp_path / "scratch")


def test_concurrent_claims_have_single_winner(store: S3CampaignStore) -> None:
    outcomes: list[str] = []
    barrier = threading.Barrier(6)

    def go() -> None:
        barrier.wait()
        outcomes.append(store.claim_submission("c1")[0])

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count(CLAIM_ACQUIRED) == 1
    assert outcomes.count(CLAIM_IN_PROGRESS) == 5


def test_live_claim_blocks_and_expired_lease_is_taken_over(store: S3CampaignStore) -> None:
    outcome, first = store.claim_submission("c1", lease_s=600)
    assert outcome == CLAIM_ACQUIRED and first is not None and not first.resumed
    assert store.claim_submission("c1")[0] == CLAIM_IN_PROGRESS

    store.release_claim(first)
    outcome, second = store.claim_submission("c1")
    assert outcome == CLAIM_ACQUIRED and second is not None
    assert second.generation == 2 and second.resumed


def test_superseded_owner_must_stop(store: S3CampaignStore) -> None:
    _, first = store.claim_submission("c1")
    assert first is not None
    store.release_claim(first)
    _, second = store.claim_submission("c1")
    assert second is not None
    with pytest.raises(S3CampaignError, match="taken over"):
        store.renew_claim(first)
    store.renew_claim(second)


def test_submitted_claim_and_handoff_report_already_submitted(
    store: S3CampaignStore, tmp_path: Path
) -> None:
    _, claim = store.claim_submission("c1")
    assert claim is not None
    store.finish_claim(claim)
    assert store.claim_submission("c1")[0] == CLAIM_ALREADY_SUBMITTED
    store.release_claim(claim)  # no-op once submitted
    assert store.claim_submission("c1")[0] == CLAIM_ALREADY_SUBMITTED

    store.write_handoff(
        new_handoff("c2", ["s1"], openstudio_version="3.10.0", kpis=None, job_ids={"s1": "j1"})
    )
    assert store.claim_submission("c2")[0] == CLAIM_ALREADY_SUBMITTED


def test_recorded_jobs_first_write_wins(store: S3CampaignStore) -> None:
    store.record_job("c1", "s1", "job-a", "n1")
    store.record_job("c1", "s1", "job-b", "n1")
    store.record_job("c1", "s2", "job-c", "n2")
    assert store.recorded_jobs("c1") == {"s1": "job-a", "s2": "job-c"}
    assert store.recorded_jobs("other") == {}


def test_batch_job_name_deterministic_and_valid() -> None:
    assert batch_job_name("camp", "s1") == "osimflow-camp-s1"
    messy = batch_job_name("my camp/1", "s 1")
    assert messy == batch_job_name("my camp/1", "s 1")
    assert all(c.isalnum() or c in "-_" for c in messy)
    long_a = batch_job_name("c" * 200, "a")
    long_b = batch_job_name("c" * 200, "b")
    assert len(long_a) <= 128 and long_a != long_b


def test_local_storage_fails_closed() -> None:
    with pytest.raises(NotImplementedError):
        LocalStorage().put_if_absent("k", b"x")


def _s3(client: Any) -> S3Storage:
    st = S3Storage.__new__(S3Storage)
    st.bucket = "b"
    st.prefix = ""
    st._client = client  # noqa: SLF001
    return st


def test_s3_put_if_absent_maps_precondition_failed() -> None:
    from botocore.exceptions import ClientError

    client = MagicMock()
    st = _s3(client)
    assert st.put_if_absent("k", b"x") is True
    assert client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    client.put_object.side_effect = ClientError({"Error": {"Code": "PreconditionFailed"}}, "Put")
    assert st.put_if_absent("k", b"x") is False
    client.put_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "Put")
    with pytest.raises(OSError):
        st.put_if_absent("k", b"x")


def _executor() -> tuple[AWSBatchExecutor, MagicMock]:
    ex = AWSBatchExecutor(job_queue="q", job_definition="jd")
    client = MagicMock()
    ex._client = client  # noqa: SLF001
    return ex, client


def test_find_existing_job_skips_failed_and_reuses_live() -> None:
    ex, client = _executor()
    client.list_jobs.return_value = {
        "jobSummaryList": [
            {"jobId": "old", "jobName": "n1", "jobStatus": "FAILED"},
            {"jobId": "live", "jobName": "n1", "jobStatus": "RUNNING"},
        ]
    }
    assert ex._find_existing_job({"name": "n1"}) == "live"  # noqa: SLF001
    assert client.list_jobs.call_args.kwargs["filters"] == [{"name": "JOB_NAME", "values": ["n1"]}]


def test_find_existing_job_none_when_only_failed_and_paginates() -> None:
    ex, client = _executor()
    client.list_jobs.side_effect = [
        {"jobSummaryList": [], "nextToken": "t"},
        {"jobSummaryList": [{"jobId": "x", "jobName": "n1", "jobStatus": "FAILED"}]},
    ]
    assert ex._find_existing_job({"name": "n1"}) is None  # noqa: SLF001
    assert client.list_jobs.call_count == 2


def _stub_campaign(store: S3CampaignStore, tmp_path: Path) -> Any:
    from types import SimpleNamespace

    from osimflow.campaign import Campaign

    camp = object.__new__(Campaign)
    camp.cfg = SimpleNamespace(  # type: ignore[assignment]
        outdir=tmp_path / "camp1",
        result_storage_backend="s3",
        result_storage_bucket="b",
    )
    camp._detach_store_obj = store  # noqa: SLF001
    return camp


def test_campaign_claim_wiring(store: S3CampaignStore, tmp_path: Path) -> None:
    from osimflow.campaign import CampaignError
    from osimflow.s3_campaign import CampaignDetached

    first = _stub_campaign(store, tmp_path)
    claim = first._acquire_detach_claim()  # noqa: SLF001
    assert first._detach_submit_kwargs("s1", claim) == {  # noqa: SLF001
        "job_name": "osimflow-camp1-s1",
        "reuse_existing_job": False,
    }
    assert first._detach_submit_kwargs("s1", None) == {}  # noqa: SLF001
    first._record_detached_job(claim, "s1", "job-1")  # noqa: SLF001
    assert store.recorded_jobs("camp1") == {"s1": "job-1"}

    with pytest.raises(CampaignError, match="another process"):
        _stub_campaign(store, tmp_path)._acquire_detach_claim()  # noqa: SLF001

    first._release_detach_claim()  # noqa: SLF001
    retry = _stub_campaign(store, tmp_path)
    resumed = retry._acquire_detach_claim()  # noqa: SLF001
    assert retry._detach_submit_kwargs("s2", resumed)["reuse_existing_job"] is True  # noqa: SLF001

    store.finish_claim(resumed)
    with pytest.raises(CampaignDetached):
        _stub_campaign(store, tmp_path)._acquire_detach_claim()  # noqa: SLF001
