"""Google Cloud Batch executor for OSimFlow campaigns (issue #254, issue #331).

Wraps the Google Cloud Batch SDK (`google-cloud-batch`) to launch one
Batch task per call, then polls the Cloud Batch service for job status
with exponential backoff until the task reaches a terminal state. The
returned Handle carries the Google Cloud Batch job name and blocks on
`.result()` until the task succeeds; on failure it re-raises a RuntimeError
with the error message.

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

Spot/preemptible instance handling (issue #352):
When `use_spot` is True, the executor requests Spot VMs (preemptible)
via the allocation policy. When a Spot/preemptible interruption occurs,
the handle's `result()` method detects it via the status details and
resubmits the job, retrying up to `max_retries` times before falling back
to regular on-demand VMs or failing. `fallback_to_on_demand` controls
whether to fall back to regular VMs after Spot retries are exhausted.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from osimflow.executors.base import BaseExecutor, Handle
from osimflow.executors.transport import resolve_result_for_callback

log = logging.getLogger("osimflow.executors.google_batch")


def _google_error_code(exc: Exception) -> int:
    """Extract HTTP status code from a Google API error, or 0 if not applicable."""
    try:
        return getattr(exc, "status_code", 0) or 0
    except Exception:  # noqa: BLE001
        return 0


class _GoogleBatchHandle(Handle):
    """Handle that polls Google Cloud Batch on `.result()`.

    Spot/preemptible retry logic (issue #352) lives here so that
    `submit()` can return immediately. When `result()` detects a
    preemptible VM interruption, it resubmits using the stored
    `_submit_params` and retries up to ``executor.max_retries`` times
    before falling back to on-demand or failing.
    """

    def __init__(
        self,
        job_name: str,
        executor: GoogleBatchExecutor,
        submit_params: dict[str, Any],
        *,
        result_hint: Any = None,
    ) -> None:
        self.job_id = job_name
        self.job_name = job_name
        self._executor = executor
        self._submit_params = submit_params
        self._result_hint = result_hint
        self._future: Future[Any] = Future()
        self.worker_id: str | None = job_name
        self.worker_ip: str | None = None
        self.worker_region: str | None = executor.region
        self.cost_usd: float | None = None
        self.billed_duration_seconds: float | None = None

    def result(self, timeout: float | None = None) -> Any:  # noqa: ARG002
        effective_max_retries = max(0, self._executor.max_retries)
        for attempt in range(effective_max_retries + 1):
            try:
                job = self._executor._wait_for_terminal(self.job_name)
            except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
                self._future.set_exception(exc)
                raise

            status = job.status.state
            if status == self._executor._batch_v1.JobStatus.State.SUCCEEDED:
                resolved = resolve_result_for_callback(self._result_hint, default=None)
                self._future.set_result(resolved)
                return resolved

            if status == self._executor._batch_v1.JobStatus.State.FAILED:
                status_details = str(job.status.status_details or "")
                is_spot = self._executor._is_spot_interruption(status_details)

                if is_spot and attempt < effective_max_retries:
                    backoff = min(5.0 * (2**attempt), 60.0)
                    jittered_backoff = random.uniform(0, backoff)
                    log.warning(
                        "Spot/preemptible interrupted (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1,
                        effective_max_retries,
                        jittered_backoff,
                        status_details,
                    )
                    time.sleep(jittered_backoff)
                    self.job_name = self._executor._submit_job(**self._submit_params)
                    self.worker_id = self.job_name
                    continue

                if is_spot and attempt >= effective_max_retries:
                    if self._executor.fallback_to_on_demand:
                        log.warning(
                            "Spot retries exhausted (%d), falling back to on-demand",
                            effective_max_retries,
                        )
                        self.job_name = self._executor._submit_job(
                            **self._submit_params, use_spot=False
                        )
                        self.worker_id = self.job_name
                        try:
                            job = self._executor._wait_for_terminal(self.job_name)
                        except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
                            self._future.set_exception(exc)
                            raise
                        status = job.status.state
                        if status == self._executor._batch_v1.JobStatus.State.SUCCEEDED:
                            resolved = resolve_result_for_callback(self._result_hint, default=None)
                            self._future.set_result(resolved)
                            return resolved
                        status_details = str(job.status.status_details or "unknown reason")
                        msg = (
                            f"Google Batch job {self.job_name!r} failed after fallback: "
                            f"status={status}, details: {status_details}"
                        )
                        self._future.set_exception(RuntimeError(msg))
                        raise RuntimeError(msg)
                    raise RuntimeError(
                        f"Spot retries exhausted ({effective_max_retries}): {status_details}"
                    )

                reason = f"job {self.job_name} failed: {status_details}"
                self._future.set_exception(RuntimeError(reason))
                raise RuntimeError(reason)

            self._future.set_result(None)
            return None

        raise RuntimeError("result loop exited unexpectedly")  # pragma: no cover

    def done(self) -> bool:
        if self._future.done():
            return True
        try:
            job = self._executor._get_job(self.job_name)
            state_name = str(job.status.state.name)
            return "SUCCEEDED" in state_name or "FAILED" in state_name
        except TimeoutError as exc:
            log.debug("Google Batch done() timeout for job %s: %s", self.job_name, exc)
            return False
        except ConnectionError as exc:
            log.debug("Google Batch done() connection error for job %s: %s", self.job_name, exc)
            return False
        except Exception as exc:
            error_code = _google_error_code(exc)
            if error_code in (401, 403, 404):
                log.warning(
                    "Google Batch done() permanent error for job %s: %s [code=%s]",
                    self.job_name,
                    exc,
                    error_code,
                )
                self._future.set_exception(exc)
                raise
            log.debug(
                "Google Batch done() transient error for job %s: %s [code=%s]",
                self.job_name,
                exc,
                error_code,
            )
            return False


class GoogleBatchExecutor(BaseExecutor):
    """Google Cloud Batch executor (issue #254, issue #331).

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

    Spot/preemptible instance handling (issue #352):
    When `use_spot` is True, the executor requests Spot VMs (preemptible)
    via the allocation policy. When a Spot/preemptible interruption occurs,
    the handle's `result()` method detects it via the status details and
    resubmits the job, retrying up to `max_retries` times before falling back
    to regular on-demand VMs or failing. `fallback_to_on_demand` controls
    whether to fall back to regular VMs after Spot retries are exhausted.
    """

    name = "google_batch"

    _SPOT_INTERRUPTION_MARKERS: tuple[str, ...] = (
        "preempted",
        "preempt",
        "spot",
        "instance was preempted",
    )

    def __init__(
        self,
        project_id: str,
        region: str,
        batch_service_account: str | None = None,
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
        *,
        use_spot: bool = False,
        fallback_to_on_demand: bool = False,
        max_retries: int = 3,
    ):
        from google.cloud import batch_v1

        self._batch_v1 = batch_v1
        self.project_id = project_id
        self.region = region
        self.batch_service_account = batch_service_account
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        self.use_spot = use_spot
        self.fallback_to_on_demand = fallback_to_on_demand
        self.max_retries = max_retries
        self._client: Any = None
        # Issue #1081: digest pinning. Initialized in the constructor so
        # ``_resolve_container_image`` is callable without going through
        # ``submit()`` (e.g. unit tests); overridden by ``submit()``.
        self._container_digest: str | None = None

    def _get_client(self) -> Any:
        """Lazy Google Cloud Batch synchronous client construction."""
        if self._client is None:
            self._client = self._batch_v1.BatchServiceClient()
        return self._client

    def _get_job(self, job_name: str) -> Any:
        """Get a job by name from Google Cloud Batch."""
        assert self._client is not None, "_get_client must be called first"
        return self._client.get_job(job_name)

    def _is_spot_interruption(self, status_details: str | None) -> bool:
        """Return True if the status details indicate a Spot/preemptible interruption."""
        if not status_details:
            return False
        lower = status_details.lower()
        return any(marker.lower() in lower for marker in self._SPOT_INTERRUPTION_MARKERS)

    def _wait_for_terminal(self, job_name: str, timeout: float | None = None) -> Any:
        """Poll Google Cloud Batch with exponential backoff until terminal state.

        Raises:
            TimeoutError: if *timeout* seconds elapse before a terminal state.
        """
        delay = self.poll_interval_s
        start = time.monotonic()
        while True:
            job = self._get_job(job_name)
            state = job.status.state
            state_str = str(state.name)
            if "SUCCEEDED" in state_str or "FAILED" in state_str:
                return job
            log.info(
                "google_batch poll job=%s state=%s (sleeping %.1fs)", job_name, state_str, delay
            )
            if timeout is not None:
                elapsed = time.monotonic() - start
                remaining = timeout - elapsed
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out after {elapsed:.1f}s waiting for job {job_name!r}"
                    )
                delay = min(delay, remaining)
            delay = min(delay * 2, self.max_poll_interval_s)
            time.sleep(delay)

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
        # Issue #1081: a pinned SHA256 digest overrides the mutable tag
        # for the OSIMFLOW_CONTAINER env var the worker reads.
        container_digest = getattr(self, "_container_digest", None)
        if container_digest is not None:
            resolved = container_digest
        else:
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
        """Submit a single Google Cloud Batch job and return the job name."""
        use_spot_final = use_spot if use_spot is not None else self.use_spot
        job_name = f"projects/{self.project_id}/locations/{self.region}/jobs/osimflow-{name}"

        container_image = "nrel/openstudio:latest"
        for e in environment:
            if e["name"] == "OSIMFLOW_CONTAINER":
                container_image = e["value"]
                break

        task_group = self._batch_v1.TaskGroup(
            task_count=1,
            task_spec=self._batch_v1.TaskSpec(
                container=self._batch_v1.ContainerSpec(
                    image_uri=container_image,
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

        instance_policy = self._batch_v1.InstancePolicy(
            machine_type="e2-standard-4",
        )
        if use_spot_final:
            instance_policy.scheduling = self._batch_v1.Scheduling(
                preemptible=True,
            )

        job = self._batch_v1.Job(
            name=job_name,
            task_groups=[task_group],
            allocation_policy=self._batch_v1.AllocationPolicy(
                service_account=self.batch_service_account,
                instances=[instance_policy],
            ),
            logs_policy=self._batch_v1.LogsPolicy(
                destination=self._batch_v1.LogsPolicy.Destination.CLOUD_LOGGING,
            ),
        )

        client = self._get_client()
        client.create_job(
            self._batch_v1.CreateJobRequest(
                parent=f"projects/{self.project_id}/locations/{self.region}",
                job=job,
                job_id=f"osimflow-{name}",
            ),
        )

        log.info(
            "google_batch submit_job -> job=%s use_spot=%s",
            job_name,
            use_spot_final,
        )
        return job_name

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        container_digest: str | None = None,
        openstudio_version: str | None = None,
        result_hint: Any = None,
        remote_command: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
        variables_json: str | None = None,
        env: dict[str, str] | None = None,
        stdout_path: Any = None,
        stderr_path: Any = None,
        max_retries: int | None = None,
        worker_id: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        self._container_digest = container_digest
        del remote_command, result_transport_mode, result_storage_backend  # noqa: F841
        del result_storage_bucket, result_storage_prefix, result_storage_endpoint  # noqa: F841
        del variables_json, env, stdout_path, stderr_path, max_retries, worker_id, kwargs  # noqa: F841, ARG002

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

        del fn, args  # noqa: ARG002

        submit_params: dict[str, Any] = {
            "name": name,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "time_min": time_min,
            "environment": environment,
        }
        job_name = self._submit_job(**submit_params)

        return _GoogleBatchHandle(
            job_name=job_name,
            executor=self,
            submit_params=submit_params,
            result_hint=result_hint,
        )

    def shutdown(self) -> None:
        pass
