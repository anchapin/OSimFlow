"""Regression tests for the result-transport contract (issue #1333).

Every remote handle that consumes a ``result_hint`` must behave
identically under ``result_transport_mode="object_storage"``: resolve
the hint through ``resolve_result_for_callback`` and then download
artifacts via ``materialize_object_storage_result`` so Campaign
callbacks receive **local paths**, never object-storage keys.

Before #1333 only the Nomad and Kubernetes handles materialized; the
AWS Batch, Azure Batch, Google Batch, and PBS handles returned raw
object-storage keys to the callback. These tests pin the contract for
all six transport-participating executors by asserting that a stubbed
``materialize_object_storage_result`` is invoked with the handle's
transport configuration on every success path (including the
spot-retry and on-demand-fallback paths).

The submitit-style executors (``slurm``, ``docker_swarm``,
``dask_jobqueue``) and ``local`` are exempt: their work function's
return value arrives via a local Future and no result hint is
consumed. See the participation matrix in
``osimflow/executors/transport.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_RESULT_TRANSPORT_KWARGS: dict[str, Any] = {
    "result_transport_mode": "object_storage",
    "result_storage_backend": "s3",
    "result_storage_bucket": "bucket-a",
    "result_storage_prefix": "campaigns/c1",
    "result_storage_endpoint": "https://s3.example.test",
}

_RESULT_HINT = {"__osimflow_type__": "path", "value": "s3://bucket-a/campaigns/c1/out.json"}


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, module: str, calls: list[dict[str, Any]]
) -> None:
    """Stub ``materialize_object_storage_result`` in *module*, recording calls."""

    def _fake_materialize(callback_result: Any, **kwargs: Any) -> Any:
        calls.append({"callback_result": callback_result, **kwargs})
        return {"materialized": True}

    monkeypatch.setattr(f"{module}.materialize_object_storage_result", _fake_materialize)


def _aws_handle() -> Any:
    from osimflow.testing.patch_targets import _AWSBatchHandle

    executor = SimpleNamespace(
        max_retries=0,
        fallback_to_on_demand=False,
        _calculate_job_cost=lambda job: (0.0, 0.0),
    )
    executor._wait_for_terminal = lambda job_id, timeout=None: {  # noqa: SLF001
        "status": "SUCCEEDED",
        "statusReason": "",
    }
    handle = _AWSBatchHandle(
        job_id="job-aws",
        executor=executor,
        submit_params={},
        result_hint=_RESULT_HINT,
        **_RESULT_TRANSPORT_KWARGS,
    )
    return handle


def _azure_handle() -> Any:
    from osimflow.executors.azure_batch_executor import AzureBatchExecutor, _AzureBatchHandle

    # azure-batch 15.x shape (issue #1582): execution_info lives directly
    # on the task; the real None-safe accessor (``AzureBatchExecutor``)
    # bridges the mock executor onto the new model.
    succeeded_task = SimpleNamespace(execution_info=SimpleNamespace(exit_code=0))
    executor = SimpleNamespace(max_retries=0, fallback_to_on_demand=False, location="eastus")
    executor._wait_for_terminal = lambda job_id, timeout=None: succeeded_task  # noqa: SLF001
    executor._execution_info = AzureBatchExecutor._execution_info  # noqa: SLF001
    return _AzureBatchHandle(
        job_id="job-azure",
        executor=executor,
        submit_params={},
        result_hint=_RESULT_HINT,
        **_RESULT_TRANSPORT_KWARGS,
    )


def _google_handle() -> Any:
    from osimflow.executors.google_batch_executor import _GoogleBatchHandle

    state = SimpleNamespace()
    state.JobStatus = SimpleNamespace(
        State=SimpleNamespace(SUCCEEDED="SUCCEEDED", FAILED="FAILED", QUEUED="QUEUED")
    )
    succeeded_job = SimpleNamespace(status=SimpleNamespace(state="SUCCEEDED"))
    executor = SimpleNamespace(max_retries=0, fallback_to_on_demand=False, region="us-central1")
    executor._batch_v1 = state  # noqa: SLF001
    executor._wait_for_terminal = lambda job_name, timeout=None: succeeded_job  # noqa: SLF001
    return _GoogleBatchHandle(
        job_name="job-google",
        executor=executor,
        submit_params={},
        result_hint=_RESULT_HINT,
        **_RESULT_TRANSPORT_KWARGS,
    )


def _pbs_handle() -> Any:
    from osimflow.executors.pbs_executor import _PBSHandle

    executor = SimpleNamespace(max_retries=0)
    executor._wait_for_terminal = lambda job_id, timeout=None: ("R", 0)  # noqa: SLF001
    return _PBSHandle(
        job_id="1[hostname]",
        executor=executor,
        result_hint=_RESULT_HINT,
        **_RESULT_TRANSPORT_KWARGS,
    )


def _nomad_handle() -> Any:
    from osimflow.testing.patch_targets import _NomadHandle

    executor = SimpleNamespace(
        max_retries=0,
        fallback_to_on_demand=False,
        datacentre="dc1",
        allocation_resolution_timeout_s=1.0,
    )
    executor._wait_for_terminal = lambda alloc_id, timeout=None: {  # noqa: SLF001
        "ClientStatus": "complete",
        "TaskStates": {},
    }
    executor._calculate_job_cost = lambda job: (0.0, 0.0)  # noqa: SLF001
    executor._client = SimpleNamespace(  # noqa: SLF001
        resolve_allocation=lambda **_: "alloc-1"
    )
    return _NomadHandle(
        job_id="job-nomad",
        eval_id="eval-1",
        executor=executor,
        result_hint=_RESULT_HINT,
        **_RESULT_TRANSPORT_KWARGS,
    )


@pytest.mark.parametrize(
    ("handle_factory", "materialize_module"),
    [
        (_aws_handle, "osimflow.executors"),
        (_azure_handle, "osimflow.executors.azure_batch_executor"),
        (_google_handle, "osimflow.executors.google_batch_executor"),
        (_pbs_handle, "osimflow.executors.pbs_executor"),
        (_nomad_handle, "osimflow.executors"),
    ],
    ids=["aws_batch", "azure_batch", "google_batch", "pbs", "nomad"],
)
def test_remote_handle_materializes_object_storage_result(
    handle_factory: Any, materialize_module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every transport-participating handle downloads artifacts on success."""
    calls: list[dict[str, Any]] = []
    _patch_transport(monkeypatch, materialize_module, calls)
    handle = handle_factory()

    resolved = handle.result(timeout=10.0)

    assert resolved == {"materialized": True}
    assert len(calls) == 1
    call = calls[0]
    assert call["transport_mode"] == "object_storage"
    assert call["result_storage_backend"] == "s3"
    assert call["result_storage_bucket"] == "bucket-a"
    assert call["result_storage_prefix"] == "campaigns/c1"
    assert call["result_storage_endpoint"] == "https://s3.example.test"


def test_aws_handle_materializes_on_fallback_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The on-demand fallback success path also materializes (issue #1333)."""
    from osimflow.testing.patch_targets import _AWSBatchHandle

    calls: list[dict[str, Any]] = []
    _patch_transport(monkeypatch, "osimflow.executors", calls)

    # First poll: spot interruption; second poll (on-demand): success.
    polls = iter(
        [
            {"status": "FAILED", "statusReason": "SpotInterruption"},
            {"status": "SUCCEEDED", "statusReason": ""},
        ]
    )
    resubmits: list[dict[str, Any]] = []

    def _fake_submit(**params: Any) -> str:
        resubmits.append(params)
        return "job-aws-ondemand"

    executor = SimpleNamespace(
        max_retries=1,
        fallback_to_on_demand=True,
        _calculate_job_cost=lambda job: (0.0, 0.0),
        _submit_job=_fake_submit,
        _is_spot_interruption=lambda reason: reason == "SpotInterruption",
    )
    executor._wait_for_terminal = lambda job_id, timeout=None: next(polls)  # noqa: SLF001
    handle = _AWSBatchHandle(
        job_id="job-aws",
        executor=executor,
        submit_params={},
        result_hint=_RESULT_HINT,
        **_RESULT_TRANSPORT_KWARGS,
    )

    resolved = handle.result(timeout=10.0)

    assert resolved == {"materialized": True}
    assert len(resubmits) == 1
    assert len(calls) == 1
    assert calls[0]["transport_mode"] == "object_storage"


def test_azure_handle_materializes_on_fallback_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Azure on-demand fallback success path also materializes."""
    from osimflow.executors.azure_batch_executor import AzureBatchExecutor, _AzureBatchHandle

    calls: list[dict[str, Any]] = []
    _patch_transport(monkeypatch, "osimflow.executors.azure_batch_executor", calls)

    # azure-batch 15.x shape (issue #1582): failure text lives on
    # ``execution_info.failure_info.message`` (not ``.failure_reason``).
    ok = lambda exit_code, reason=None: SimpleNamespace(  # noqa: E731
        execution_info=SimpleNamespace(
            exit_code=exit_code,
            failure_info=SimpleNamespace(message=reason) if reason else None,
        )
    )
    polls = iter([ok(1, "SpotInterruption"), ok(0)])  # spot-failure exit code, then success
    resubmits: list[dict[str, Any]] = []

    def _fake_submit(**params: Any) -> str:
        resubmits.append(params)
        return "job-azure-ondemand"

    executor = SimpleNamespace(
        max_retries=1,
        fallback_to_on_demand=True,
        location="eastus",
        _submit_job=_fake_submit,
        _is_spot_interruption=lambda reason: reason == "SpotInterruption",
    )
    executor._wait_for_terminal = lambda job_id, timeout=None: next(polls)  # noqa: SLF001
    executor._execution_info = AzureBatchExecutor._execution_info  # noqa: SLF001
    handle = _AzureBatchHandle(
        job_id="job-azure",
        executor=executor,
        submit_params={},
        result_hint=_RESULT_HINT,
        **_RESULT_TRANSPORT_KWARGS,
    )

    resolved = handle.result(timeout=10.0)

    assert resolved == {"materialized": True}
    assert len(resubmits) == 1
    assert len(calls) == 1
    assert calls[0]["transport_mode"] == "object_storage"


def test_google_handle_materializes_on_fallback_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Google on-demand fallback success path also materializes."""
    from osimflow.executors.google_batch_executor import _GoogleBatchHandle

    calls: list[dict[str, Any]] = []
    _patch_transport(monkeypatch, "osimflow.executors.google_batch_executor", calls)

    state = SimpleNamespace()
    state.JobStatus = SimpleNamespace(
        State=SimpleNamespace(SUCCEEDED="SUCCEEDED", FAILED="FAILED", QUEUED="QUEUED")
    )
    polls = iter(
        [
            SimpleNamespace(status=SimpleNamespace(state="FAILED", status_details=None)),
            SimpleNamespace(status=SimpleNamespace(state="SUCCEEDED")),
        ]
    )
    resubmits: list[dict[str, Any]] = []

    def _fake_submit(**params: Any) -> str:
        resubmits.append(params)
        return "job-google-ondemand"

    executor = SimpleNamespace(
        max_retries=1,
        fallback_to_on_demand=True,
        region="us-central1",
        _submit_job=_fake_submit,
        _is_spot_interruption=lambda details: True,
    )
    executor._batch_v1 = state  # noqa: SLF001
    executor._wait_for_terminal = lambda job_name, timeout=None: next(polls)  # noqa: SLF001
    handle = _GoogleBatchHandle(
        job_name="job-google",
        executor=executor,
        submit_params={},
        result_hint=_RESULT_HINT,
        **_RESULT_TRANSPORT_KWARGS,
    )

    resolved = handle.result(timeout=10.0)

    assert resolved == {"materialized": True}
    assert len(resubmits) == 1
    assert len(calls) == 1
    assert calls[0]["transport_mode"] == "object_storage"


def test_handles_default_to_auto_transport_without_storage_config() -> None:
    """Backward compat: handles constructed without transport params behave as before."""
    from osimflow.executors.pbs_executor import _PBSHandle

    executor = SimpleNamespace(max_retries=0)
    executor._wait_for_terminal = lambda job_id, timeout=None: ("R", 0)  # noqa: SLF001
    hint: dict[str, str] = {"__osimflow_type__": "path", "value": "/tmp/work/out.json"}
    handle = _PBSHandle(job_id="1[hostname]", executor=executor, result_hint=hint)

    resolved = handle.result()

    # shared_fs/auto mode: resolve returns the decoded path, no materialization.
    assert resolved == Path("/tmp/work/out.json")
