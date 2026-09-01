"""Resource-directive propagation capture for real-substrate E2Es (issue #1403).

``BaseExecutor.submit()`` takes ``cpus`` / ``memory_mb`` / ``time_min``
directives that translate differently per substrate (Boto3
``containerOverrides`` for AWS Batch, Nomad ``Resources`` block, PBS
``-l select:ncpus:mem``, Swarm ``Limits``/``Reservations``, Google Batch
``ComputeResource``). Before #1403 none of the real-substrate campaign
tests verified that the configured directives actually reach the
substrate, so a regression in the translation layer passed every E2E.

This module provides:

* :func:`record_submit_directives` — wraps ``executor.submit`` with a
  recording spy so a campaign run captures every directive it hands to
  the executor plus the resulting job ids.
* :func:`assert_sim_fanout_directives` — asserts the RUN_OPENSTUDIO_SIM
  fan-out carried the DEFAULT_STEP_RESOURCES values (4 CPUs / 8192 MB).
* Per-substrate wire-format probes (``aws_*``, ``nomad_*``, ``pbs_*``,
  ``swarm_*``, ``google_*``) that re-read the produced job from the
  substrate's own API and assert the directives round-tripped.

Every probe re-reads substrate state via the substrate SDK; the tests
that call them remain env-gated exactly as before (issue #1020), so PR
CI is never coupled to live infrastructure.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

#: RUN_OPENSTUDIO_SIM defaults from ``osimflow.executors.DEFAULT_STEP_RESOURCES``.
SIM_CPUS = 4
SIM_MEMORY_MB = 8192


class SubmitRecorder:
    """Captures every ``submit()`` directive handed to an executor."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []


def record_submit_directives(executor: Any) -> SubmitRecorder:  # noqa: ANN401
    """Wrap ``executor.submit`` so a campaign run records its directives.

    The spy is installed as an instance attribute (shadowing the bound
    method), which both the Campaign's ``submit()`` and
    ``submit_request()`` dispatch paths honour. The original bound
    method is preserved on the recorder so tests can restore it.
    """
    recorder = SubmitRecorder()
    original = executor.submit  # bound method

    def _spy(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        record: dict[str, Any] = {
            "name": kwargs.get("name", "?"),
            "cpus": kwargs.get("cpus"),
            "memory_mb": kwargs.get("memory_mb"),
            "time_min": kwargs.get("time_min"),
        }
        handle = original(*args, **kwargs)
        record["job_id"] = getattr(handle, "worker_id", None)
        recorder.records.append(record)
        return handle

    executor.submit = _spy  # type: ignore[method-assign]
    recorder.original = original  # type: ignore[attr-defined]
    return recorder


def assert_sim_fanout_directives(recorder: SubmitRecorder, *, min_tasks: int = 3) -> None:
    """Assert the simulation fan-out round-tripped the default directives.

    A 3-sample campaign fans RUN_OPENSTUDIO_SIM out once per sample, so
    at least *min_tasks* submissions must carry the
    ``DEFAULT_STEP_RESOURCES["RUN_OPENSTUDIO_SIM"]`` values.
    """
    assert recorder.records, "executor.submit was never called by the campaign"
    sim_records = [
        r for r in recorder.records if r["cpus"] == SIM_CPUS and r["memory_mb"] == SIM_MEMORY_MB
    ]
    assert len(sim_records) >= min_tasks, (
        f"expected >= {min_tasks} submissions with cpus={SIM_CPUS} "
        f"memory_mb={SIM_MEMORY_MB} (RUN_OPENSTUDIO_SIM fan-out); "
        f"got {len(sim_records)} from {len(recorder.records)} total: {recorder.records!r}"
    )
    missing_ids = [r for r in sim_records if not r["job_id"]]
    assert not missing_ids, (
        "RUN_OPENSTUDIO_SIM handles exposed no job id via worker_id; "
        "the wire-format probe cannot run without them"
    )


# ---------------------------------------------------------------------------
# Per-substrate wire-format probes
# ---------------------------------------------------------------------------


def aws_describe_jobs_resources(
    job_ids: list[str],
    *,
    queue_name: str | None = None,
) -> list[dict[str, Any]]:
    """Re-read AWS Batch jobs and return their container resource views."""
    import boto3  # noqa: PLC0415

    client = boto3.client("batch")
    described = client.describe_jobs(jobs=job_ids)["jobs"]
    assert len(described) == len(job_ids), (
        f"describe_jobs returned {len(described)} of {len(job_ids)} requested jobs"
    )
    out: list[dict[str, Any]] = []
    for job in described:
        container = job.get("container") or {}
        out.append(
            {
                "vcpus": container.get("vcpus"),
                "memory": container.get("memory"),
                "job_id": job.get("jobId"),
            }
        )
    return out


def nomad_job_resources(address: str, job_id: str, *, token: str | None = None) -> dict[str, Any]:
    """Fetch a Nomad job's task Resources block via the HTTP API."""
    import urllib.request  # noqa: PLC0415

    req = urllib.request.Request(  # noqa: S310 — operator-provided Nomad addr
        f"{address.rstrip('/')}/v1/job/{job_id}"
    )
    if token:
        req.add_header("X-Nomad-Token", token)
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        job = json.loads(resp.read().decode("utf-8"))
    task_group = (job.get("TaskGroups") or [{}])[0]
    task = (task_group.get("Tasks") or [{}])[0]
    return task.get("Resources") or {}


def pbs_job_resources(job_id: str) -> dict[str, str]:
    """Return the ``Resource_List.*`` pairs from ``qstat -f`` for *job_id*."""
    proc = subprocess.run(  # noqa: S603
        ["qstat", "-f", job_id],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    resources: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Resource_List."):
            key, _, value = stripped.partition(" = ")
            resources[key.removeprefix("Resource_List.")] = value
    return resources


def swarm_service_resources(service_name: str) -> dict[str, Any]:
    """Return the service's Limits/Reservations via ``docker service inspect``."""
    proc = subprocess.run(  # noqa: S603
        ["docker", "service", "inspect", service_name, "--format", "{{json .Spec}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    spec = json.loads(proc.stdout)
    return spec.get("TaskTemplate", {}).get("Resources", {})


def google_job_compute_resource(client: Any, job_name: str) -> dict[str, Any]:  # noqa: ANN401
    """Return a Google Batch job's ComputeResource view (cpu_cores/memory_mb)."""
    job = client.get(name=job_name)
    spec = job.task_groups[0].task_spec
    return {
        "cpu_cores": spec.compute_resource.cpu_cores,
        "memory_mb": spec.compute_resource.memory_mb,
    }
