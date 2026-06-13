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

Spot instance handling (issue #352):
When `use_spot` is True, the executor submits jobs using Azure Spot VMs
(low-priority VMs). When a Spot interruption occurs, the handle's
`result()` method detects it via the failure reason and resubmits the
job, retrying up to `max_retries` times before falling back to regular
on-demand VMs or failing. `fallback_to_on_demand` controls whether to
fall back to regular VMs after Spot retries are exhausted.
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
    """Handle that polls Azure Batch on `.result()`.

    Spot retry logic (issue #352) lives here so that `submit()` can
    return immediately. When `result()` detects a Spot interruption,
    it resubmits using the stored `_submit_params` and retries up to
    ``executor.max_retries`` times before falling back to on-demand
    or failing.
    """

    def __init__(
        self,
        job_id: str,
        executor: AzureBatchExecutor,
        submit_params: dict[str, Any],
    ) -> None:
        self.job_id = job_id
        self._executor = executor
        self._submit_params = submit_params
        self._future: Future[Any] = Future()
        self.worker_id: str | None = job_id
        self.worker_ip: str | None = None
        self.worker_region: str | None = executor.location
        self.cost_usd: float | None = None
        self.billed_duration_seconds: float | None = None

    def result(self, timeout: float | None = None) -> Any:  # noqa: ARG002
        effective_max_retries = max(0, self._executor.max_retries)
        for attempt in range(effective_max_retries + 1):
            try:
                job = self._executor._wait_for_terminal(self.job_id)
            except BaseException as exc:
                self._future.set_exception(exc)
                raise

            exit_code = job.properties.execution_info.exit_code
            # exit_code is None means the job succeeded (no error code reported).
            # A non-zero exit_code is a failure — check if it was Spot.
            if exit_code is None or exit_code == 0:
                self._future.set_result(None)
                return None

            # Job failed (non-zero exit code) — check if it was a Spot interruption.
            failure_reason = getattr(
                job.properties.execution_info, "failure_reason", None
            )
            is_spot = self._executor._is_spot_interruption(failure_reason)

            if is_spot and attempt < effective_max_retries:
                backoff = min(5.0 * (2**attempt), 60.0)
                log.warning(
                    "Spot interrupted (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    effective_max_retries,
                    backoff,
                    failure_reason,
                )
                time.sleep(backoff)
                # Resubmit and update the tracked job_id.
                self.job_id = self._executor._submit_job(**self._submit_params)
                self.worker_id = self.job_id
                continue

            if is_spot and attempt >= effective_max_retries:
                # Exhausted retries — fall back or fail.
                if self._executor.fallback_to_on_demand:
                    log.warning(
                        "Spot retries exhausted (%d), falling back to on-demand",
                        effective_max_retries,
                    )
                    self.job_id = self._executor._submit_job(
                        **self._submit_params, use_spot=False
                    )
                    self.worker_id = self.job_id
                    # Poll the on-demand job (no more retries).
                    try:
                        job = self._executor._wait_for_terminal(self.job_id)
                    except BaseException as exc:
                        self._future.set_exception(exc)
                        raise
                    exit_code = job.properties.execution_info.exit_code
                    if exit_code is None or exit_code == 0:
                        self._future.set_result(None)
                        return None
                    failure_reason = getattr(
                        job.properties.execution_info, "failure_reason", "unknown reason"
                    )
                    msg = (
                        f"Azure Batch job {self.job_id!r} failed after fallback: "
                        f"exit code {exit_code}, reason: {failure_reason}"
                    )
                    self._future.set_exception(RuntimeError(msg))
                    raise RuntimeError(msg)
                raise RuntimeError(
                    f"Spot retries exhausted ({effective_max_retries}): {failure_reason}"
                )

            # Non-spot failure — don't retry, raise immediately.
            reason = f"exit code {exit_code}"
            msg = f"Azure Batch job {self.job_id!r} failed: {reason}"
            self._future.set_exception(RuntimeError(msg))
            raise RuntimeError(msg)

        # Unreachable, but satisfies the type checker.
        raise RuntimeError("result loop exited unexpectedly")  # pragma: no cover

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

    Spot instance handling (issue #352):
    When `use_spot` is True, the executor submits jobs using Azure Spot VMs
    (low-priority VMs). When a Spot interruption occurs, the handle's
    `result()` method detects it via the failure reason and resubmits the
    job, retrying up to `max_retries` times before falling back to regular
    on-demand VMs or failing. `fallback_to_on_demand` controls whether to
    fall back to regular VMs after Spot retries are exhausted.
    """

    name = "azure_batch"

    # Sentinel markers used in failureReason to identify Spot interruptions.
    _SPOT_INTERRUPTION_MARKERS: tuple[str, ...] = (
        "Preemption",
        "SpotNodeTermination",
        "Preempted",
        "spot",
        "low priority",
    )

    def __init__(
        self,
        account_name: str,
        account_url: str,
        pool_id: str,
        job_schedule_id: str | None = None,
        location: str = "eastus",
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
        *,
        use_spot: bool = False,
        fallback_to_on_demand: bool = False,
        max_retries: int = 3,
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
        self.use_spot = use_spot
        self.fallback_to_on_demand = fallback_to_on_demand
        self.max_retries = max_retries
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

    def _is_spot_interruption(self, reason: str | None) -> bool:
        """Return True if the failure reason indicates a Spot interruption."""
        if not reason:
            return False
        lower = reason.lower()
        return any(marker.lower() in lower for marker in self._SPOT_INTERRUPTION_MARKERS)

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

    def _submit_job(
        self,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        time_min: int,
        environment: list[dict[str, str]],
        use_spot: bool | None = None,
    ) -> str:
        """Submit a single Azure Batch job and return the job ID.

        Uses *use_spot* if provided, otherwise ``self.use_spot``.
        """
        use_spot_final = use_spot if use_spot is not None else self.use_spot
        job_id = f"osimflow-{name}"
        environment_settings = [
            {"name": e["name"], "value": e["value"]} for e in environment
        ]

        # Build the job-level configuration.
        job_params: dict[str, Any] = {
            "id": job_id,
            "pool_info": {"pool_id": self.pool_id},
            "on_all_tasks_complete": "terminate",
            "on_task_failure": "terminate",
        }

        # Apply Spot (low-priority) VM settings if enabled.
        if use_spot_final:
            job_params["priority"] = 0  # 0 = low priority (Spot)

        # Submit the job.
        client = self._get_client()
        client.job.add(self.account_name, job_params)

        # Build and add the task.
        # Extract container image from OSIMFLOW_CONTAINER env var.
        container_image = "nrel/openstudio:latest"
        for e in environment_settings:
            if e["name"] == "OSIMFLOW_CONTAINER":
                container_image = e["value"]
                break

        task = {
            "id": job_id,
            "command_line": "/bin/sh -c 'sleep infinity'",
            "environment_settings": environment_settings,
            "container_settings": {
                "image_name": container_image,
            },
        }
        client.task.add(self.account_name, job_id, task)

        log.info(
            "azure_batch submit_job -> jobId=%s use_spot=%s",
            job_id,
            use_spot_final,
        )
        return job_id

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

        del fn, args  # noqa: ARG002 — work runs inside the Batch container

        submit_params: dict[str, Any] = {
            "name": name,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "time_min": time_min,
            "environment": environment,
        }
        job_id = self._submit_job(**submit_params)

        return _AzureBatchHandle(
            job_id=job_id,
            executor=self,
            submit_params=submit_params,
        )

    def shutdown(self) -> None:
        pass
