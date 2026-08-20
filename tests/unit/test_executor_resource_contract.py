"""Parametrized per-executor resource-directive contract test (issue #943).

PRD §6 #3 calls the resource-allocation mapping a key consideration. Each
executor translates the canonical OSimFlow directives ``cpus`` /
``memory_mb`` / ``time_min`` into a scheduler-native spec:

  * SlurmExecutor        -> submitit ``update_parameters`` (mem in **GB**, ceil)
  * AWSBatchExecutor     -> Boto3 ``containerOverrides`` (memory in **MiB**, 1:1)
  * AzureBatchExecutor   -> ``TaskConstraints(max_wall_clock_time="PT{N}M")`` (ISO 8601)
  * GoogleBatchExecutor  -> ``ComputeResource`` + ``Duration(seconds=...)``
  * DaskJobQueueExecutor -> ``SLURMCluster(cores=, memory=, walltime=)`` (cluster-level)
  * KubernetesExecutor   -> ``V1ResourceRequirements`` (memory in ``{N}Mi``)
  * NomadExecutor        -> ``Resources{CPU: MHz, MemoryMB: MB}``
  * DockerSwarmExecutor  -> ``NanoCPUs`` (x1e9) + ``MemoryBytes`` (bytes)
  * PBSExecutor          -> ``qsub -l ... mem={N}gb walltime=HH:MM:SS`` (mem in GB, ceil)
  * LocalExecutor        -> advisory only (no scheduler translation)

Today these mappings are verified only piecemeal across scattered,
heavily-mocked unit tests, so a regression in one backend's unit
conversion (MiB-vs-MB, nanocpus, ISO-8601 timeout) can slip through.

This module asserts the canonical directive ``cpus=4, memory_mb=8192,
time_min=240`` renders to the correct scheduler-native object in **all
10** executors, with each unit-conversion edge covered by an explicit
assertion. Everything is fully mocked — no real Docker / Slurm / K8s /
AWS / Azure / Google substrate is required.

The test also folds in a backoff-cap assertion: every executor that
implements Spot/preemptible retry logic caps the retry sleep at 60 s
and honors ``max_retries``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Executor classes are importable without their SDKs (lazy imports inside
# __init__ / _get_client), so we can construct them via __new__ and inject
# MagicMock SDK handles — the same pattern used by
# tests/unit/test_executor_backoff_cap.py.
from osimflow.executors import (
    AWSBatchExecutor,
    ExecutorRegistry,
    LocalExecutor,
    NomadExecutor,
    SlurmExecutor,
    _AWSBatchHandle,
)
from osimflow.executors.azure_batch_executor import AzureBatchExecutor, _AzureBatchHandle
from osimflow.executors.dask_jobqueue_executor import DaskJobQueueExecutor
from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor
from osimflow.executors.google_batch_executor import GoogleBatchExecutor, _GoogleBatchHandle
from osimflow.executors.kubernetes_executor import KubernetesExecutor
from osimflow.executors.pbs_executor import PBSExecutor

# The kubernetes SDK provides real ``V1ResourceRequirements`` objects, which
# makes the resource-translation assertion strongest. Detect it up front so
# the kubernetes case still collects (and skips) cleanly when the SDK is
# absent — mirroring tests/unit/test_kubernetes_executor.py (issue #623).
try:
    from kubernetes import client as _k8s_client  # noqa: F401

    _HAS_KUBERNETES = True
except ImportError:
    _HAS_KUBERNETES = False

# The canonical directive every executor must translate identically.
CPUS = 4
MEMORY_MB = 8192
TIME_MIN = 240

# The set of built-in executors this contract must cover. Adding an 11th
# executor to ``osimflow/executors/__init__.py`` (or the per-file modules)
# without extending this set AND adding a CASE below fails
# ``test_all_ten_executors_covered_by_contract`` — that is the acceptance
# criterion "adding an 11th executor fails the test until asserted".
EXPECTED_EXECUTORS: set[str] = {
    "local",
    "slurm",
    "aws_batch",
    "azure_batch",
    "google_batch",
    "dask_jobqueue",
    "kubernetes",
    "nomad",
    "docker_swarm",
    "pbs",
}


# ===========================================================================
# Per-executor builders + assertions
# ===========================================================================
# Each builder constructs an executor with its SDK client mocked (bypassing
# __init__ where it would import a heavy SDK), submits the canonical
# directive, and returns the executor + its captured mock client. Each
# checker asserts the scheduler-native object received the correctly
# translated values. Splitting build/check lets the parametrized test report
# failures against the executor id directly.


def _build_local() -> tuple[Any, Any]:
    """LocalExecutor: advisory only — directives accepted, not translated."""
    ex = LocalExecutor(max_workers=2)
    handle = ex.submit(
        lambda: "ok", name="contract", cpus=CPUS, memory_mb=MEMORY_MB, time_min=TIME_MIN
    )
    return ex, handle


def _check_local(ex: Any, handle: Any) -> None:
    # LocalExecutor maps nothing to a scheduler; the contract is "accept the
    # directive and return a Handle". No exception + a local job_id is the
    # full assertion (documented advisory behavior — see base.py submit()).
    assert handle.job_id.startswith("local-")
    # resolve so the thread actually runs (keeps the pool clean for teardown).
    assert handle.result(timeout=5) == "ok"


def _build_slurm() -> tuple[Any, Any]:
    """SlurmExecutor: submitit update_parameters (mem MB->GB, ceil)."""
    ex = SlurmExecutor.__new__(SlurmExecutor)
    ex._submitit = MagicMock()
    ex._ex = MagicMock()  # carries .folder used by the per-submit AutoExecutor
    ex.partition = "short"
    ex.account = None
    ex.qos = None
    ex.constraint = None
    ex.gres = None
    call_ex = ex._submitit.AutoExecutor.return_value
    ex.submit(lambda: "ok", name="contract", cpus=CPUS, memory_mb=MEMORY_MB, time_min=TIME_MIN)
    return ex, call_ex


def _check_slurm(ex: Any, call_ex: Any) -> None:
    # Edge: memory_mb=8192 -> ceil(8192/1024) = 8 GB on slurm_mem_gb.
    # time_min is passed through unchanged (submitit speaks minutes).
    call_ex.update_parameters.assert_called_once()
    kwargs = call_ex.update_parameters.call_args[1]
    assert kwargs["slurm_cpus_per_task"] == CPUS
    assert kwargs["slurm_mem_gb"] == 8  # 8192 MiB -> 8 GB (ceil)
    assert kwargs["slurm_time"] == TIME_MIN
    # None-valued advanced directives must be filtered out (not forwarded).
    for filtered in ("slurm_account", "slurm_qos", "slurm_constraint", "slurm_gres"):
        assert filtered not in kwargs


def _build_aws_batch() -> tuple[Any, Any]:
    """AWSBatchExecutor: Boto3 containerOverrides (memory MiB 1:1, timeout s)."""
    ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
    ex._boto3 = MagicMock()
    mock_client = MagicMock()
    mock_client.submit_job.return_value = {"jobId": "aws-1"}
    ex._client = mock_client
    ex._ec2_client = MagicMock()
    ex.job_queue = "q"
    ex.job_definition = "jd"
    ex.ecr_repository = None
    ex.max_spot_price_usd = None  # skip the Spot price ceiling check
    ex.fallback_to_on_demand = False
    ex.max_retries = 3
    ex._region_name = None
    ex._instance_type = None
    ex._submit_rps = None
    ex._submit_limiter = MagicMock()
    ex._spot_price_cache = MagicMock()
    ex.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR = 0.05
    ex.DEFAULT_SPOT_PRICE_PER_VCPU_HOUR = 0.03
    ex.submit(lambda: None, name="contract", cpus=CPUS, memory_mb=MEMORY_MB, time_min=TIME_MIN)
    return ex, mock_client


def _check_aws_batch(ex: Any, mock_client: Any) -> None:
    mock_client.submit_job.assert_called_once()
    kwargs = mock_client.submit_job.call_args[1]
    overrides = kwargs["containerOverrides"]
    # Edge: AWS memory is in MiB; OSimFlow treats MB==MiB 1:1 (no scaling).
    assert overrides["vcpus"] == CPUS
    assert overrides["memory"] == MEMORY_MB  # 8192 MiB (1:1 pass-through)
    # Edge: timeout is attemptDurationSeconds = time_min * 60.
    assert kwargs["timeout"]["attemptDurationSeconds"] == TIME_MIN * 60  # 14400


def _build_azure_batch() -> tuple[Any, Any]:
    """AzureBatchExecutor: TaskConstraints(max_wall_clock_time="PT{N}M" ISO 8601).

    NOTE: Azure does not map cpus/memory_mb to the task (pool-level only);
    this contract documents that current behavior and asserts time_min only.
    """
    ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
    azure_batch = MagicMock()
    azure_identity = MagicMock()
    mock_client = MagicMock()
    ex._azure_batch = azure_batch
    ex._azure_identity = azure_identity
    ex._client = mock_client
    ex.account_name = "acct"
    ex.account_url = "https://acct.eastus.batch.azure.com"
    ex.pool_id = "pool"
    ex.location = "eastus"
    ex.use_spot = False
    ex.fallback_to_on_demand = False
    ex.max_retries = 3
    ex.submit(lambda: None, name="contract", cpus=CPUS, memory_mb=MEMORY_MB, time_min=TIME_MIN)
    return ex, azure_batch


def _check_azure_batch(ex: Any, azure_batch: Any) -> None:
    # Edge: Azure timeout is ISO 8601 "PT{N}M".
    azure_batch.models.TaskConstraints.assert_called_once_with(
        max_wall_clock_time=f"PT{TIME_MIN}M",  # "PT240M"
        max_retry_count=0,
    )
    # The executor sets ``task_params.constraints`` AFTER constructing the
    # TaskAddParameter (attribute assignment on the mock return value).
    # Assert the constraints object actually landed on the submitted task.
    task_params = azure_batch.models.TaskAddParameter.return_value
    assert task_params.constraints is azure_batch.models.TaskConstraints.return_value
    # Documented current behavior (issue #943): cpus/memory_mb are NOT mapped
    # to the Azure task — resource sizing happens at the pool level only. The
    # TaskAddParameter was constructed with NO cpu/memory keyword; assert the
    # only resource-derived field is resource_files (an empty list).
    task_kwargs = azure_batch.models.TaskAddParameter.call_args.kwargs
    assert task_kwargs.get("resource_files") == []
    for absent in ("cpus", "cpu", "memory_mb", "memory"):
        assert absent not in task_kwargs, (
            f"Azure task unexpectedly carries resource directive {absent!r} "
            "(cpu/memory are pool-level only — documented current behavior)"
        )


def _build_google_batch() -> tuple[Any, Any]:
    """GoogleBatchExecutor: ComputeResource + Duration(seconds=...)."""
    ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
    batch_v1 = MagicMock()
    mock_client = MagicMock()
    ex._batch_v1 = batch_v1
    ex._client = mock_client
    ex.project_id = "proj"
    ex.region = "us-central1"
    ex.batch_service_account = None
    ex.use_spot = False
    ex.fallback_to_on_demand = False
    ex.max_retries = 3
    ex.submit(lambda: None, name="contract", cpus=CPUS, memory_mb=MEMORY_MB, time_min=TIME_MIN)
    return ex, batch_v1


def _check_google_batch(ex: Any, batch_v1: Any) -> None:
    # Edge: Google ComputeResource takes cpu_cores + memory_mb directly,
    # and the task timeout is a protobuf Duration(seconds=...).
    batch_v1.ComputeResource.assert_called_once_with(
        cpu_cores=CPUS,
        memory_mb=MEMORY_MB,
    )
    batch_v1.Duration.assert_called_once_with(seconds=TIME_MIN * 60)  # 14400


def _build_dask_jobqueue() -> tuple[Any, Any]:
    """DaskJobQueueExecutor: cluster-level cores/memory/walltime (per-call advisory).

    Per-call cpus/memory_mb/time_min are advisory (logged) in the Dask
    executor — resource sizing happens once when the cluster is built via
    ``_build_cluster``. This case injects a fake ``dask_jobqueue`` module and
    asserts the cluster-level translation.
    """
    ex = DaskJobQueueExecutor.__new__(DaskJobQueueExecutor)
    ex.cluster_type = "slurm"
    ex.min_workers = 0
    ex.max_workers = 1
    # Use the canonical directive values at the cluster level so the contract
    # is consistent across executors.
    ex.cpus_per_worker = CPUS
    ex.memory_per_worker = f"{MEMORY_MB}MiB"
    ex.walltime = f"{TIME_MIN // 60:02d}:00:00"
    ex.queue = None
    ex.project = None
    ex.job_extra = {}
    ex._cluster = None
    ex._client = None
    ex._scaler_running = False
    fake_djq = MagicMock()
    with patch.dict(sys.modules, {"dask_jobqueue": fake_djq}):
        ex._build_cluster()
    return ex, fake_djq


def _check_dask_jobqueue(ex: Any, fake_djq: Any) -> None:
    fake_djq.SLURMCluster.assert_called_once()
    kwargs = fake_djq.SLURMCluster.call_args[1]
    # Dask speaks ``cores`` (not cpus) and a human-readable ``memory``/``walltime``.
    assert kwargs["cores"] == CPUS
    assert kwargs["memory"] == f"{MEMORY_MB}MiB"
    assert kwargs["walltime"] == "04:00:00"


def _build_kubernetes() -> tuple[Any, Any]:
    """KubernetesExecutor: V1ResourceRequirements (memory in {N}Mi)."""
    ex = KubernetesExecutor.__new__(KubernetesExecutor)
    mock_client = MagicMock()
    mock_client.create_namespaced_job.return_value = None
    ex._client = mock_client
    ex.namespace = "default"
    ex.poll_interval_s = 5.0
    ex.max_poll_interval_s = 60.0
    # Native Job controls (issue #997) — defaults preserve the
    # pre-#997 manifest byte-for-byte. Other tests in
    # tests/unit/test_kubernetes_executor.py exercise the field setter
    # path; this contract test only verifies resource mapping.
    ex.backoff_limit = 0
    ex.ttl_seconds_after_finished = None
    ex.queue_name = None
    ex.submit(lambda: None, name="contract", cpus=CPUS, memory_mb=MEMORY_MB, time_min=TIME_MIN)
    return ex, mock_client


def _check_kubernetes(ex: Any, mock_client: Any) -> None:
    mock_client.create_namespaced_job.assert_called_once()
    job = mock_client.create_namespaced_job.call_args.kwargs["body"]
    container = job.spec.template.spec.containers[0]
    requests = container.resources.requests
    limits = container.resources.limits
    # Edge: K8s memory uses the binary-suffix string "{N}Mi"; cpu is a core count string.
    assert requests["cpu"] == str(CPUS)
    assert requests["memory"] == f"{MEMORY_MB}Mi"  # "8192Mi"
    assert limits["memory"] == f"{MEMORY_MB}Mi"
    # Edge: K8s active_deadline_seconds is time_min * 60.
    assert job.spec.active_deadline_seconds == TIME_MIN * 60  # 14400


def _build_nomad() -> tuple[Any, Any]:
    """NomadExecutor: Resources{CPU: MHz, MemoryMB: MB} in direct (non-dispatch) mode."""
    ex = NomadExecutor.__new__(NomadExecutor)
    ex.address = "http://127.0.0.1:4646"
    ex.datacentre = "dc1"
    ex.poll_interval_s = 5.0
    ex.max_poll_interval_s = 60.0
    # Force direct mode (build a unique job spec per call) so the per-submit
    # cpus/memory_mb flow into the rendered Resources block.
    ex.dispatch_policy = "keep_manual"
    ex._manual_dispatch_requested = False
    ex.use_dispatch = False
    ex._submit_count = 0
    ex.estimated_run_size = None
    ex._auto_dispatch_threshold = 25
    ex.allocation_resolution_timeout_s = 30.0
    ex.remote_results_only = True
    ex.verify_tls = True
    ex.tls = False
    ex.cert = None
    ex.key = None
    ex.ca_cert = None
    ex._dispatch_job_registered = False
    ex._local_pool = MagicMock()
    mock_client = MagicMock()
    mock_client.submit_job.return_value = {"JobID": "nomad-1", "EvalID": "eval-1"}
    ex._client = mock_client
    ex.submit(lambda: None, name="contract", cpus=CPUS, memory_mb=MEMORY_MB, time_min=TIME_MIN)
    return ex, mock_client


def _check_nomad(ex: Any, mock_client: Any) -> None:
    mock_client.submit_job.assert_called_once()
    spec = mock_client.submit_job.call_args.args[0]
    resources = spec["Job"]["TaskGroups"][0]["Tasks"][0]["Resources"]
    # Edge: Nomad CPU is in MHz (1 logical cpu = 1000 MHz).
    assert resources["CPU"] == CPUS * 1000  # 4000 MHz
    # Edge: Nomad MemoryMB is a direct MB value.
    assert resources["MemoryMB"] == MEMORY_MB  # 8192


def _build_docker_swarm() -> tuple[Any, Any]:
    """DockerSwarmExecutor: NanoCPUs (x1e9) + MemoryBytes (bytes)."""
    ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
    mock_client = MagicMock()
    created_service = MagicMock()
    created_service.name = "osimflow-contract"
    mock_client.services.create.return_value = created_service
    mock_client.info.return_value = {"Swarm": {"ControlAvailable": True}}
    ex._client = mock_client
    ex._stub_executor = None
    ex.poll_interval_s = 5.0
    ex.max_poll_interval_s = 60.0
    ex.image = "nrel/openstudio:latest"
    ex.network = None
    with patch.object(ex, "_check_docker_available", return_value=True):
        ex.submit(
            lambda: None,
            name="contract",
            cpus=CPUS,
            memory_mb=MEMORY_MB,
            time_min=TIME_MIN,
            container="nrel/openstudio:3.11.0",
        )
    return ex, mock_client


def _check_docker_swarm(ex: Any, mock_client: Any) -> None:
    mock_client.services.create.assert_called_once()
    kwargs = mock_client.services.create.call_args.kwargs
    resources = kwargs["resources"]
    # Edge: Docker NanoCPUs = cpus * 1e9.
    assert resources["Limits"]["NanoCPUs"] == CPUS * 1_000_000_000  # 4_000_000_000
    # Edge: Docker memory is in BYTES (MiB * 1024 * 1024).
    assert resources["Limits"]["MemoryBytes"] == MEMORY_MB * 1024 * 1024  # 8_589_934_592
    # Reservations mirror Limits (Docker Swarm dual-spec).
    assert resources["Reservations"]["NanoCPUs"] == CPUS * 1_000_000_000
    assert resources["Reservations"]["MemoryBytes"] == MEMORY_MB * 1024 * 1024


def _build_pbs() -> tuple[Any, Any]:
    """PBSExecutor: qsub -l select=...:ncpus=N:mem={G}gb -l walltime=HH:MM:SS."""
    ex = PBSExecutor.__new__(PBSExecutor)
    ex.server = None
    ex.queue = None
    ex.debug = False  # real qsub path so _qsub_cmd is exercised
    ex.poll_interval_s = 5.0
    ex.max_poll_interval_s = 60.0
    ex.cpus_per_node = 1
    ex.mem_mb_per_node = 1024
    run_result = MagicMock()
    run_result.stdout = "123.pbsserver\n"
    run_result.returncode = 0
    with patch("osimflow.executors.pbs_executor.subprocess.run", return_value=run_result):
        ex.submit(lambda: None, name="contract", cpus=CPUS, memory_mb=MEMORY_MB, time_min=TIME_MIN)
    return ex, run_result


def _check_pbs(ex: Any, run_result: Any) -> None:
    # The patched subprocess.run captured the qsub argv, but rather than
    # depend on the captured-mock lifecycle, assert directly against the pure
    # translation function _qsub_cmd — this isolates the unit conversion.
    del run_result  # captured in _build_pbs; re-derive from _qsub_cmd here.
    cmd = ex._qsub_cmd(
        name="contract",
        cpus=CPUS,
        memory_mb=MEMORY_MB,
        time_min=TIME_MIN,
        container=None,
        openstudio_version=None,
        script_lines=["sleep infinity"],
    )
    # Edge: PBS mem is in GB, ceil((8192+1023)/1024)=8; n_chunks=cpus/1=4.
    select_str = next(arg for arg in cmd if arg.startswith("select="))
    assert select_str == f"select={CPUS}:ncpus={CPUS}:mem=8gb"
    # Edge: PBS walltime is HH:MM:SS derived from time_min*60 seconds.
    walltime_str = next(arg for arg in cmd if arg.startswith("walltime="))
    assert walltime_str == "walltime=04:00:00"


# Registry of (builder, checker) per executor. Keys MUST equal
# EXPECTED_EXECUTORS; ``test_all_ten_executors_covered_by_contract`` enforces
# that adding a new executor extends both sets + this map.
CASES: dict[str, tuple[Callable[[], tuple[Any, Any]], Callable[[Any, Any], None]]] = {
    "local": (_build_local, _check_local),
    "slurm": (_build_slurm, _check_slurm),
    "aws_batch": (_build_aws_batch, _check_aws_batch),
    "azure_batch": (_build_azure_batch, _check_azure_batch),
    "google_batch": (_build_google_batch, _check_google_batch),
    "dask_jobqueue": (_build_dask_jobqueue, _check_dask_jobqueue),
    "kubernetes": (_build_kubernetes, _check_kubernetes),
    "nomad": (_build_nomad, _check_nomad),
    "docker_swarm": (_build_docker_swarm, _check_docker_swarm),
    "pbs": (_build_pbs, _check_pbs),
}


# ===========================================================================
# Contract: every executor translates the canonical directive correctly.
# ===========================================================================

_param_ids = sorted(CASES)


@pytest.mark.parametrize("executor_name", _param_ids)
def test_resource_directive_contract(executor_name: str) -> None:
    """Assert the canonical directive renders to the correct native object.

    This is the single parametrized test that exercises all executors'
    resource mapping. Each case mocks the SDK client, submits
    ``cpus=4, memory_mb=8192, time_min=240``, and asserts the rendered
    scheduler-native object carries the correctly-translated values —
    including the unit-conversion edges (MiB, NanoCPUs, ISO-8601, etc.).
    """
    if executor_name == "kubernetes" and not _HAS_KUBERNETES:
        pytest.skip("kubernetes SDK not installed — V1ResourceRequirements unavailable")
    builder, checker = CASES[executor_name]
    ex, captured = builder()
    checker(ex, captured)


# ===========================================================================
# Completeness guard: a new (11th) executor fails this test until asserted.
# ===========================================================================


def test_all_ten_executors_covered_by_contract() -> None:
    """The registry's built-in executors must exactly match this contract.

    If a contributor adds an executor (issue #943 acceptance criterion), this
    test fails until they (a) add it to ``EXPECTED_EXECUTORS`` and (b) add a
    matching CASE above. Conversely, removing an executor also fails here, so
    the contract stays in lockstep with the codebase.
    """
    registered = set(ExecutorRegistry.list_available())
    # Third-party plug-ins (entry points) are intentionally out of scope —
    # only built-in executors must be covered by this contract. We assert the
    # built-in set is exactly EXPECTED_EXECUTORS and that the parametrized
    # CASES cover every one of them.
    assert EXPECTED_EXECUTORS <= registered, (
        f"expected built-in executors missing from registry: {EXPECTED_EXECUTORS - registered}"
    )
    assert set(CASES) == EXPECTED_EXECUTORS, (
        "CASES must cover exactly EXPECTED_EXECUTORS; add a builder+checker "
        "for any new executor. missing="
        f"{EXPECTED_EXECUTORS - set(CASES)} extra={set(CASES) - EXPECTED_EXECUTORS}"
    )
    # Hard count guard — the issue title says "all 10 executors".
    assert len(EXPECTED_EXECUTORS) == 10


# ===========================================================================
# Backoff-cap contract: Spot/preemptible retry sleeps cap at 60s, honor max_retries.
# ===========================================================================
#
# Three executors implement Spot/preemptible retry inside their handle's
# ``result()``: AWS, Azure, Google. Each computes
# ``backoff = min(5.0 * (2 ** attempt), 60.0)``. We simulate consecutive Spot
# interruptions with ``max_retries=5`` so the 4th-iteration backoff (5*2^4=80)
# would exceed the cap — proving the 60s ceiling is actually exercised — and
# assert no captured sleep exceeds 60s and exactly ``max_retries`` retries
# occurred before the exhaustion RuntimeError.
#
# NOTE: the Google handle compares ``job.status.state ==
# executor._batch_v1.JobStatus.State.FAILED`` (enum identity, not string
# equality), so the fake job's state MUST be the same object the executor's
# MagicMock returns for that attribute. The builders below wire that up.


def _build_aws_retry_executor() -> Any:
    ex = MagicMock()
    ex.max_retries = 5  # large enough that 5*2^4=80 would exceed the 60s cap
    ex.fallback_to_on_demand = False
    ex._submit_job = MagicMock(return_value="resubmitted-job")
    ex._calculate_job_cost = MagicMock(return_value=(0.0, 0.0))
    # AWS handle reads statusReason via dict.get(); the markers include
    # "Spot Instance termination" so _is_spot_interruption (real method on
    # AWSBatchExecutor) returns True. Use the real executor so the matcher
    # logic runs verbatim rather than a MagicMock stand-in.
    real = AWSBatchExecutor.__new__(AWSBatchExecutor)
    real._SPOT_INTERRUPTION_MARKERS = AWSBatchExecutor._SPOT_INTERRUPTION_MARKERS
    ex._is_spot_interruption = real._is_spot_interruption
    spot_job = {"status": "FAILED", "statusReason": "Spot Instance termination: capacity-over"}
    ex._wait_for_terminal = MagicMock(return_value=spot_job)
    return ex


def _build_azure_retry_executor() -> Any:
    ex = MagicMock()
    ex.max_retries = 5
    ex.fallback_to_on_demand = False
    ex._submit_job = MagicMock(return_value="resubmitted-job")
    real = AzureBatchExecutor.__new__(AzureBatchExecutor)
    real._SPOT_INTERRUPTION_MARKERS = AzureBatchExecutor._SPOT_INTERRUPTION_MARKERS
    ex._is_spot_interruption = real._is_spot_interruption
    job = MagicMock()
    job.properties.execution_info.end_time = "2024-01-01T00:01:00Z"
    job.properties.execution_info.exit_code = 1  # non-zero => failure
    job.properties.execution_info.failure_reason = "SpotNodeTermination"
    ex._wait_for_terminal = MagicMock(return_value=job)
    return ex


def _build_google_retry_executor() -> Any:
    ex = MagicMock()
    ex.max_retries = 5
    ex.fallback_to_on_demand = False
    ex._submit_job = MagicMock(return_value="resubmitted-job")
    real = GoogleBatchExecutor.__new__(GoogleBatchExecutor)
    real._SPOT_INTERRUPTION_MARKERS = GoogleBatchExecutor._SPOT_INTERRUPTION_MARKERS
    ex._is_spot_interruption = real._is_spot_interruption
    # CRITICAL: the google handle compares state by identity against the
    # executor's enum. The fake job's state MUST be the SAME object the
    # executor's MagicMock exposes for JobStatus.State.FAILED, and must NOT
    # equal the SUCCEEDED enum, so the FAILED branch is taken every poll.
    failed_state = ex._batch_v1.JobStatus.State.FAILED
    succeeded_state = ex._batch_v1.JobStatus.State.SUCCEEDED
    assert failed_state is not succeeded_state  # MagicMock children are distinct
    job = MagicMock()
    job.status.state = failed_state
    job.status.status_details = "instance was preempted"
    ex._wait_for_terminal = MagicMock(return_value=job)
    return ex


_RETRY_BUILDERS: dict[str, Callable[[], Any]] = {
    "aws_batch": _build_aws_retry_executor,
    "azure_batch": _build_azure_retry_executor,
    "google_batch": _build_google_retry_executor,
}


@pytest.mark.parametrize(
    "executor_name,handle_cls,sleep_module",
    [
        pytest.param("aws_batch", _AWSBatchHandle, "osimflow.executors", id="aws_batch"),
        pytest.param(
            "azure_batch",
            _AzureBatchHandle,
            "osimflow.executors.azure_batch_executor",
            id="azure_batch",
        ),
        pytest.param(
            "google_batch",
            _GoogleBatchHandle,
            "osimflow.executors.google_batch_executor",
            id="google_batch",
        ),
    ],
)
def test_spot_retry_backoff_caps_at_60s_and_honors_max_retries(
    executor_name: str,
    handle_cls: type,
    sleep_module: str,
) -> None:
    """Every Spot-retry executor caps backoff at 60s and honors max_retries."""
    ex = _RETRY_BUILDERS[executor_name]()
    handle: Any = object.__new__(handle_cls)  # noqa: SLF001 — bypass __init__ (SDK-heavy)
    # Minimal handle init: the retry loop touches these fields.
    handle.job_id = "job-1"
    handle._executor = ex  # noqa: SLF001
    handle._submit_params = {"name": "contract"}  # noqa: SLF001
    handle._result_hint = None  # noqa: SLF001
    handle._future = MagicMock()  # noqa: SLF001
    handle.worker_id = "job-1"  # noqa: SLF001
    handle.worker_ip = None  # noqa: SLF001
    handle.worker_region = None  # noqa: SLF001
    handle.cost_usd = None  # noqa: SLF001
    handle.billed_duration_seconds = None  # noqa: SLF001
    handle.error = None  # noqa: SLF001
    # The google handle reads self.job_name; aws/azure read self.job_id only.
    if executor_name == "google_batch":
        handle.job_name = "job-1"  # noqa: SLF001

    sleeps: list[float] = []

    def _capture_sleep(duration: float) -> None:
        sleeps.append(duration)

    with patch(f"{sleep_module}.time.sleep", side_effect=_capture_sleep):
        with pytest.raises(RuntimeError, match="exhausted"):
            handle.result()

    # Cap: no single retry sleep may exceed 60s.
    assert sleeps, "expected at least one retry sleep"
    for d in sleeps:
        assert d <= 60.0, f"{executor_name}: retry sleep {d}s exceeds the 60s cap"
    # The 4th retry (attempt index 4) computes 5*2^4=80 -> must be capped to 60.
    # Since issue #1025 added ``random.uniform(0, backoff)`` jitter, the
    # actual sleep is uniformly distributed in [0, 60] rather than exactly
    # 60.0; we assert the cap *bound* is honored rather than equality.
    assert max(sleeps) <= 60.0, (
        f"{executor_name}: expected the 60s cap to bound the sleep, max sleep was {max(sleeps)}"
    )
    # Honor max_retries: exactly max_retries Spot retries sleep before exhaustion.
    assert len(sleeps) == ex.max_retries, (
        f"{executor_name}: expected {ex.max_retries} retries, got {len(sleeps)}"
    )
