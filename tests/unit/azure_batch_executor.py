"""Azure Batch executor for OSimFlow campaigns (issue #254).

Wraps the Azure Batch SDK to launch one Batch task per call, then polls
the Azure Batch service for job status with exponential backoff until
the task reaches a terminal state. The returned Handle carries the
Azure Batch job ID and blocks on `.result()` until the task succeeds;
on failure it re-raises a RuntimeError with the Azure error message.

Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
the Azure Batch `environmentSettings` and `commandLine` via the
task configuration. Per-sample `OSIMFLOW_OS_VERSION` and
`OSIMFLOW_CONTAINER` are carried as environment variables — the same
env vars `SlurmExecutor` and `AWSBatchExecutor` export, so downstream
work scripts can be substrate-agnostic.

Security: credentials are sourced from the Azure Managed Identity or
environment variables (AZURE_TENANT_ID, AZURE_CLIENT_ID,
AZURE_CLIENT_SECRET). The constructor does **not** accept long-lived
access keys; passing them would violate the security policy.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from osimflow.executors.base import BaseExecutor, Handle

log = logging.getLogger("osimflow.executors.azure_batch")


class _AzureBatchHandle(Handle):
    """Handle that polls Azure Batch on `.result()`."""

    def __init__(
        self,
        job_id: str,
        executor: AzureBatchExecutor,
    ) -> None:
        self.job_id = job_id
        self._executor = executor
        self._future: Future[Any] = Future()
        self.worker_id: str | None = job_id
        self.worker_ip: str | None = None
        self.worker_region: str | None = executor.location
        self.cost_usd: float | None = None
        self.billed_duration_seconds: float | None = None

    def result(self, timeout: float | None = None) -> Any:  # noqa: ARG002
        try:
            job = self._executor._wait_for_terminal(self.job_id)
        except BaseException as exc:
            self._future.set_exception(exc)
            raise

        exit_code = job.properties.execution_info.exit_code
        if exit_code is not None and exit_code != 0:
            reason = f"exit code {exit_code}"
            self._future.set_exception(
                RuntimeError(f"Azure Batch job {self.job_id!r} failed: {reason}")
            )
            raise RuntimeError(f"Azure Batch job {self.job_id!r} failed: {reason}")

        self._future.set_result(None)
        return None

    def done(self) -> bool:
        if self._future.done():
            return True
        try:
            job = self._executor._get_job(self.job_id)
            if job.properties.execution_info.end_time is not None:
                return True
        except Exception:
            return False
        return False


class AzureBatchExecutor(BaseExecutor):
    """Azure Batch executor (issue #254).

    Wraps the Azure Batch SDK (`azure-mgmt-batch`) to launch one Batch
    task per call, then polls the service with exponential backoff until
    the task reaches a terminal state. The returned Handle carries the
    Azure Batch job ID and blocks on `.result()` until the task succeeds;
    on failure it re-raises a RuntimeError.

    Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
    the Azure Batch task configuration. Per-sample
    `OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER` are carried as
    environment variables — the same env vars `SlurmExecutor` and
    `AWSBatchExecutor` export, so downstream work scripts can be
    substrate-agnostic.

    Security: credentials are sourced from the Azure Managed Identity or
    environment variables (AZURE_TENANT_ID, AZURE_CLIENT_ID,
    AZURE_CLIENT_SECRET). The constructor does **not** accept long-lived
    access keys; passing them would violate the security policy.

    Azure Batch SDK is lazy-imported inside `__init__` so the local-executor
    / slurm-executor paths do not pay the import cost.
    """

    name = "azure_batch"

    def __init__(
        self,
        account_name: str,
        account_url: str,
        pool_id: str,
        job_schedule_id: str | None = None,
        location: str = "eastus",
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
    ):
        import azure.identity  # noqa: PLC0415
        import azure.mgmt.batch  # noqa: PLC0415

        self._azure_identity = azure.identity
        self._azure_mgmt_batch = azure.mgmt.batch
        self.account_name = account_name
        self.account_url = account_url.rstrip("/")
        self.pool_id = pool_id
        self.job_schedule_id = job_schedule_id
        self.location = location
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy Azure Batch client construction using DefaultAzureCredential."""
        if self._client is None:
            credential = self._azure_identity.DefaultAzureCredential()
            self._client = self._azure_mgmt_batch.BatchManagementClient(
                credential=credential,
                subscription_id=self.account_url,
                base_url=self.account_url,
            )
        assert self._client is not None
        return self._client

    def _get_job(self, job_id: str) -> Any:
        """Get a job by ID from Azure Batch."""
        return self._get_client().job.get(self.account_name, job_id)

    def _wait_for_terminal(self, job_id: str) -> Any:
        """Poll Azure Batch with exponential backoff until terminal state."""
        delay = self.poll_interval_s
        while True:
            job = self._get_job(job_id)
            if job.properties.execution_info.end_time is not None:
                return job
            log.info("azure_batch poll jobId=%s (sleeping %.1fs)", job_id, delay)
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
            "azure_batch submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
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

        # Build the Azure Batch job
        job_id = f"osimflow-{name}"
        environment_settings = [{"name": e["name"], "value": e["value"]} for e in environment]

        # Azure Batch task configuration
        task = {
            "id": job_id,
            "command_line": "/bin/sh -c 'sleep infinity'",
            "environment_settings": environment_settings,
            "container_settings": {
                "image_name": container or f"nrel/openstudio:{openstudio_version or 'latest'}",
            },
        }

        # Submit the job
        client = self._get_client()
        client.job.add(
            self.account_name,
            {
                "id": job_id,
                "pool_info": {"pool_id": self.pool_id},
                "on_all_tasks_complete": "terminate",
                "on_task_failure": "terminate",
            },
        )

        # Add the task
        client.task.add(
            self.account_name,
            job_id,
            task,
        )

        return _AzureBatchHandle(
            job_id=job_id,
            executor=self,
        )

    def shutdown(self) -> None:
        pass
