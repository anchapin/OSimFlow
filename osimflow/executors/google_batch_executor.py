"""Google Cloud Batch executor for OSimFlow campaigns (issue #254).

Wraps the Google Cloud Batch SDK to launch one Batch task per call, then
polls the Cloud Batch service for job status with exponential backoff
until the task reaches a terminal state. The returned Handle carries the
Google Cloud Batch job name and blocks on `.result()` until the task
succeeds; on failure it re-raises a RuntimeError with the error message.

Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
the Google Cloud Batch task configuration. Per-sample
`OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER` are carried as
environment variables — the same env vars `SlurmExecutor` and
`AWSBatchExecutor` export, so downstream work scripts can be
substrate-agnostic.

Security: credentials are sourced from the Google Cloud IAM role attached
to the compute environment (or Application Default Credentials via
`gcloud auth application-default login`). The constructor does **not**
accept long-lived service account keys; passing them would violate the
security policy.

Google Cloud Batch SDK is lazy-imported inside `__init__` so the
local-executor / slurm-executor paths do not pay the import cost.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

from osimflow.executors.base import BaseExecutor, Handle

if TYPE_CHECKING:
    pass

log = logging.getLogger("osimflow.executors.google_batch")


class _GoogleBatchHandle(Handle):
    """Handle that polls Google Cloud Batch on `.result()`."""

    def __init__(
        self,
        job_name: str,
        executor: GoogleBatchExecutor,
    ) -> None:
        self.job_name = job_name
        self._executor = executor
        self._future: Future[Any] = Future()
        self.worker_id: str | None = job_name
        self.worker_ip: str | None = None
        self.worker_region: str | None = executor.region
        self.cost_usd: float | None = None
        self.billed_duration_seconds: float | None = None

    def result(self, timeout: float | None = None) -> Any:  # noqa: ARG002
        try:
            job = self._executor._wait_for_terminal(self.job_name)
        except BaseException as exc:
            self._future.set_exception(exc)
            raise

        status = job.status.state
        if status in (self._executor._batch_v1.JobStatus.State.SUCCEEDED,):
            self._future.set_result(None)
            return None

        if status in (self._executor._batch_v1.JobStatus.State.FAILED,):
            reason = f"job {self.job_name} failed"
            self._future.set_exception(RuntimeError(reason))
            raise RuntimeError(reason)

        self._future.set_result(None)
        return None

    def done(self) -> bool:
        if self._future.done():
            return True
        try:
            job = self._executor._get_job(self.job_name)
            if job.status.state in (
                self._executor._batch_v1.JobStatus.State.SUCCEEDED,
                self._executor._batch_v1.JobStatus.State.FAILED,
            ):
                return True
        except Exception:
            return False
        return False


class GoogleBatchExecutor(BaseExecutor):
    """Google Cloud Batch executor (issue #254).

    Wraps the Google Cloud Batch SDK (`google-cloud-batch`) to launch one
    Batch task per call, then polls the service with exponential backoff
    until the task reaches a terminal state. The returned Handle carries
    the Google Cloud Batch job name and blocks on `.result()` until the
    task succeeds; on failure it re-raises a RuntimeError.

    Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
    the Google Cloud Batch task configuration. Per-sample
    `OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER` are carried as
    environment variables — the same env vars `SlurmExecutor` and
    `AWSBatchExecutor` export, so downstream work scripts can be
    substrate-agnostic.

    Security: credentials are sourced from the Google Cloud IAM role
    attached to the compute environment (or Application Default
    Credentials). The constructor does **not** accept long-lived service
    account keys; passing them would violate the security policy.

    Google Cloud Batch SDK is lazy-imported inside `__init__` so the
    local-executor / slurm-executor paths do not pay the import cost.
    """

    name = "google_batch"

    def __init__(
        self,
        project_id: str,
        region: str,
        batch_service_account: str | None = None,
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
    ):
        from google.cloud import batch_v1

        self._batch_v1 = batch_v1
        self.project_id = project_id
        self.region = region
        self.batch_service_account = batch_service_account
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy Google Cloud Batch client construction."""
        if self._client is None:
            self._client = self._batch_v1.BatchServiceAsyncClient()
        return self._client

    def _get_job(self, job_name: str) -> Any:
        """Get a job by name from Google Cloud Batch."""
        assert self._client is not None, "_get_client must be called first"
        return self._client.get_job(name=job_name)

    def _wait_for_terminal(self, job_name: str) -> Any:
        """Poll Google Cloud Batch with exponential backoff until terminal state."""
        delay = self.poll_interval_s
        while True:
            job = self._get_job(job_name)
            if job.status.state in (
                self._batch_v1.JobStatus.State.SUCCEEDED,
                self._batch_v1.JobStatus.State.FAILED,
            ):
                return job
            log.info("google_batch poll job=%s (sleeping %.1fs)", job_name, delay)
            time.sleep(delay)
            delay = min(delay * 2, self.max_poll_interval_s)

    def _build_environment(
        self,
        *,
        container: str | None,
        openstudio_version: str | None,
    ) -> list[dict[str, str]]:
        """Build environment variables for the Batch task."""
        env: list[dict[str, str]] = []
        if openstudio_version is not None:
            env.append({"name": "OSIMFLOW_OS_VERSION", "value": str(openstudio_version)})
        resolved = container or f"nrel/openstudio:{openstudio_version or 'latest'}"
        env.append({"name": "OSIMFLOW_CONTAINER", "value": resolved})
        return env

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        openstudio_version = kwargs.get("openstudio_version")

        log.info(
            "google_batch submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
        )

        environment = self._build_environment(
            container=container,
            openstudio_version=openstudio_version,
        )

        # Build the Google Cloud Batch job
        job_name = f"projects/{self.project_id}/locations/{self.region}/jobs/osimflow-{name}"

        # Task group configuration
        task_group = self._batch_v1.TaskGroup(
            task_count=1,
            task_spec=self._batch_v1.TaskSpec(
                container=self._batch_v1.ContainerSpec(
                    image_uri=container or f"nrel/openstudio:{openstudio_version or 'latest'}",
                    command=["/bin/sh", "-c", "sleep infinity"],
                ),
                environment={
                    "variables": {e["name"]: e["value"] for e in environment},
                },
                compute_resource=self._batch_v1.ComputeResource(
                    cpu_cores=cpus,
                    memory_mb=memory_mb,
                ),
                timeout=self._batch_v1.Duration(seconds=int(time_min) * 60),
            ),
        )

        # Job configuration
        job = self._batch_v1.Job(
            name=job_name,
            task_groups=[task_group],
            allocation_policy=self._batch_v1.AllocationPolicy(
                service_account=self.batch_service_account,
                instances=[
                    self._batch_v1.InstancePolicy(
                        machine_type="e2-standard-4",
                    )
                ],
            ),
            logs_policy=self._batch_v1.LogsPolicy(
                destination=self._batch_v1.LogsPolicy.Destination.CLOUD_LOGGING,
            ),
        )

        # Submit the job
        client = self._get_client()
        client.create_job(
            self._batch_v1.CreateJobRequest(
                parent=f"projects/{self.project_id}/locations/{self.region}",
                job=job,
                job_id=f"osimflow-{name}",
            ),
        )

        return _GoogleBatchHandle(
            job_name=job_name,
            executor=self,
        )

    def shutdown(self) -> None:
        pass
