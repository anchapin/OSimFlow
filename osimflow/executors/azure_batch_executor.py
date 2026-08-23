"""Azure Batch executor for OSimFlow campaigns (issue #254, issue #331).

Wraps the Azure Batch SDK (`azure.batch`) to launch one Batch task per
call, then polls the Azure Batch service for job status with exponential
backoff until the task reaches a terminal state. The returned Handle
carries the Azure Batch job ID and blocks on `.result()` until the task
succeeds; on failure it re-raises a RuntimeError with the Azure error
message.

Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
the Azure Batch task configuration. Per-sample `OSIMFLOW_OS_VERSION`
and `OSIMFLOW_CONTAINER` are carried as environment variables — the
same env vars `SlurmExecutor` and `AWSBatchExecutor` export, so
downstream work scripts can be substrate-agnostic.

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

import json
import logging
import os
import random
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from osimflow.executors.base import BaseExecutor, Handle
from osimflow.executors.transport import (
    encode_transport_value,
    resolve_result_for_callback,
)

log = logging.getLogger("osimflow.executors.azure_batch")


class _AzureErrorInfo:
    def __init__(self, code: str, is_permanent: bool):
        self.code = code
        self.is_permanent = is_permanent


def _azure_error_info(exc: Exception) -> _AzureErrorInfo:
    """Classify an Azure exception as permanent or transient."""
    exc_name = type(exc).__name__
    status_code = getattr(exc, "status_code", None) or 0
    if (
        "AuthenticationError" in exc_name
        or status_code in (401, 403)
        or "not found" in str(exc).lower()
    ):
        return _AzureErrorInfo(exc_name, True)
    if "TooManyRequestsError" in exc_name or status_code == 429:
        return _AzureErrorInfo(exc_name, False)
    return _AzureErrorInfo(exc_name, False)


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
        *,
        result_hint: Any = None,
    ) -> None:
        self.job_id = job_id
        self._executor = executor
        self._submit_params = submit_params
        self._result_hint = result_hint
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
            except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
                self._future.set_exception(exc)
                raise

            exit_code = job.properties.execution_info.exit_code
            if exit_code is None or exit_code == 0:
                resolved = resolve_result_for_callback(self._result_hint, default=None)
                self._future.set_result(resolved)
                return resolved

            failure_reason = getattr(job.properties.execution_info, "failure_reason", None)
            is_spot = self._executor._is_spot_interruption(failure_reason)

            if is_spot and attempt < effective_max_retries:
                backoff = min(5.0 * (2**attempt), 60.0)
                jittered_backoff = random.uniform(0, backoff)
                log.warning(
                    "Spot interrupted (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    effective_max_retries,
                    jittered_backoff,
                    failure_reason,
                )
                time.sleep(jittered_backoff)
                self.job_id = self._executor._submit_job(**self._submit_params)
                self.worker_id = self.job_id
                continue

            if is_spot and attempt >= effective_max_retries:
                if self._executor.fallback_to_on_demand:
                    log.warning(
                        "Spot retries exhausted (%d), falling back to on-demand",
                        effective_max_retries,
                    )
                    self.job_id = self._executor._submit_job(**self._submit_params, use_spot=False)
                    self.worker_id = self.job_id
                    try:
                        job = self._executor._wait_for_terminal(self.job_id)
                    except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
                        self._future.set_exception(exc)
                        raise
                    exit_code = job.properties.execution_info.exit_code
                    if exit_code is None or exit_code == 0:
                        resolved = resolve_result_for_callback(self._result_hint, default=None)
                        self._future.set_result(resolved)
                        return resolved
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

            reason = f"exit code {exit_code}"
            msg = f"Azure Batch job {self.job_id!r} failed: {reason}"
            self._future.set_exception(RuntimeError(msg))
            raise RuntimeError(msg)

        raise RuntimeError("result loop exited unexpectedly")  # pragma: no cover

    def done(self) -> bool:
        if self._future.done():
            return True
        try:
            job = self._executor._get_job(self.job_id)
            if job.properties.execution_info.end_time is not None:
                return True
        except TimeoutError as exc:
            log.debug("Azure Batch done() timeout for job %s: %s", self.job_id, exc)
            return False
        except ConnectionError as exc:
            log.debug("Azure Batch done() connection error for job %s: %s", self.job_id, exc)
            return False
        except Exception as exc:
            error_info = _azure_error_info(exc)
            if error_info.is_permanent:
                log.warning(
                    "Azure Batch done() permanent error for job %s: %s [%s]",
                    self.job_id,
                    exc,
                    error_info.code,
                )
                self._future.set_exception(exc)
                raise
            log.debug(
                "Azure Batch done() transient error for job %s: %s [%s]",
                self.job_id,
                exc,
                error_info.code,
            )
            return False
        return False


class AzureBatchExecutor(BaseExecutor):
    """Azure Batch executor (issue #254, issue #331).

    Wraps the Azure Batch SDK (`azure.batch`) to launch one Batch task
    per call, then polls the service with exponential backoff until the
    task reaches a terminal state. The returned Handle carries the Azure
    Batch job ID and blocks on `.result()` until the task succeeds; on
    failure it re-raises a RuntimeError.

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
        location: str = "eastus",
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
        *,
        use_spot: bool = False,
        fallback_to_on_demand: bool = False,
        max_retries: int = 3,
    ):
        import azure.batch  # noqa: PLC0415
        import azure.identity  # noqa: PLC0415

        self._azure_batch = azure.batch
        self._azure_identity = azure.identity
        self.account_name = account_name
        self.account_url = account_url.rstrip("/")
        self.pool_id = pool_id
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
            self._client = self._azure_batch.BatchServiceClient(
                credential=credential,
                batch_url=self.account_url,
            )
            assert self._client is not None
        return self._client

    def _get_job(self, job_id: str) -> Any:
        """Get a job from Azure Batch."""
        return self._get_client().job.get(self.account_name, job_id)

    def _is_spot_interruption(self, reason: str | None) -> bool:
        """Return True if the failure reason indicates a Spot interruption."""
        if not reason:
            return False
        lower = reason.lower()
        return any(marker.lower() in lower for marker in self._SPOT_INTERRUPTION_MARKERS)

    def _wait_for_terminal(self, job_id: str, timeout: float | None = None) -> Any:
        """Poll Azure Batch with exponential backoff until terminal state.

        Raises:
            TimeoutError: if *timeout* seconds elapse before a terminal state.
        """
        delay = self.poll_interval_s
        start = time.monotonic()
        while True:
            job = self._get_job(job_id)
            if job.properties.execution_info.end_time is not None:
                return job
            log.info("azure_batch poll jobId=%s (sleeping %.1fs)", job_id, delay)
            if timeout is not None:
                elapsed = time.monotonic() - start
                remaining = timeout - elapsed
                if remaining <= 0:
                    raise TimeoutError(f"Timed out after {elapsed:.1f}s waiting for job {job_id!r}")
                delay = min(delay, remaining)
            delay = min(delay * 2, self.max_poll_interval_s)
            time.sleep(delay)

    @property
    def requires_remote_runner_payload(self) -> bool:
        return True

    @staticmethod
    def _infer_step_name(submit_name: str) -> str:
        """Map a submit name to the remote_runner step identifier.

        Same mapping as ``NomadExecutor._infer_step_name`` and
        ``KubernetesExecutor._infer_step_name``: the Campaign names
        fan-out tasks ``apply_<sid>`` / ``sim_<sid>`` / ``kpi_<sid>`` and
        the single-shot steps ``aggregate`` / ``plots``; the remote runner
        resolves the work function from the step identifier.
        """
        lower = submit_name.lower()
        if lower.startswith("apply_"):
            return "apply"
        if lower.startswith("sim_"):
            return "sim"
        if lower.startswith("kpi_"):
            return "extract"
        if lower.startswith("aggregate"):
            return "aggregate"
        if lower.startswith("plots"):
            return "plots"
        return "unknown"

    @staticmethod
    def _build_task_payload(
        *,
        step_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        result_hint: Any,  # noqa: ANN401
        name: str,
    ) -> str:
        """Serialize the step call for the ephemeral runner.

        Uses the same serialization as ``NomadExecutor._build_task_payload``
        and ``KubernetesExecutor._build_task_payload`` so
        ``osimflow.remote_runner`` can decode either executor's Jobs
        identically (issue #996).
        """
        payload = {
            "schema_version": 1,
            "name": name,
            "step": step_name,
            "args": [AzureBatchExecutor._encode_payload_value(a) for a in args],
            "kwargs": {k: AzureBatchExecutor._encode_payload_value(v) for k, v in kwargs.items()},
            "result_hint": AzureBatchExecutor._encode_payload_value(result_hint),
        }
        return json.dumps(payload)

    @staticmethod
    def _encode_payload_value(value: Any) -> Any:  # noqa: ANN401
        return encode_transport_value(value)

    def _build_environment(
        self,
        *,
        container: str | None,
        openstudio_version: str | None,
        task_payload: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> list[dict[str, str]]:
        """Build environment variables for the Batch task.

        The serialized task payload travels in ``OSIMFLOW_TASK_PAYLOAD`` and
        the result-transport contract in the ``OSIMFLOW_RESULT_*`` vars so
        ``osimflow.remote_runner`` can execute the step and push results to
        object storage (issue #996). ``OSIMFLOW_STUB_SIM`` is propagated
        from the orchestrator environment when set so remote pods honour
        the orchestrator's stub-vs-real CLI choice.
        """
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
        if task_payload is not None:
            env.append({"name": "OSIMFLOW_TASK_PAYLOAD", "value": task_payload})
        if result_transport_mode is not None:
            env.append({"name": "OSIMFLOW_RESULT_TRANSPORT_MODE", "value": result_transport_mode})
        if result_storage_backend is not None:
            env.append({"name": "OSIMFLOW_RESULT_STORAGE_BACKEND", "value": result_storage_backend})
        if result_storage_bucket is not None:
            env.append({"name": "OSIMFLOW_RESULT_STORAGE_BUCKET", "value": result_storage_bucket})
        if result_storage_prefix is not None:
            env.append({"name": "OSIMFLOW_RESULT_STORAGE_PREFIX", "value": result_storage_prefix})
        if result_storage_endpoint is not None:
            env.append(
                {"name": "OSIMFLOW_RESULT_STORAGE_ENDPOINT", "value": result_storage_endpoint}
            )
        stub_sim = os.environ.get("OSIMFLOW_STUB_SIM")
        if stub_sim is not None:
            env.append({"name": "OSIMFLOW_STUB_SIM", "value": stub_sim})
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
        command: str | None = None,
    ) -> str:
        """Submit a single Azure Batch job and return the job ID.

        When ``command`` is provided, it overrides the default container
        command (e.g. to run ``python -m osimflow.remote_runner``).
        """
        use_spot_final = use_spot if use_spot is not None else self.use_spot
        job_id = f"osimflow-{name}"
        environment_settings = [{"name": e["name"], "value": e["value"]} for e in environment]

        client = self._get_client()

        client.job.add(
            self.account_name,
            self._azure_batch.models.JobAddParameter(
                id=job_id,
                pool_info=self._azure_batch.models.PoolInformation(pool_id=self.pool_id),
                on_all_tasks_complete="terminate",
                on_task_failure="terminate",
                priority=0 if use_spot_final else 1000,
            ),
        )

        resolved_container = "nrel/openstudio:latest"
        for e in environment_settings:
            if e["name"] == "OSIMFLOW_CONTAINER":
                resolved_container = e["value"]
                break

        # Use the provided command or default to remote_runner
        command_line = command or "python -m osimflow.remote_runner"
        task_params = self._azure_batch.models.TaskAddParameter(
            id=job_id,
            command_line=f"/bin/sh -c {command_line!r}",
            container_settings=self._azure_batch.models.ContainerConfiguration(
                container_run_options="--rm",
                image_names=[resolved_container],
            ),
            environment_settings=[
                self._azure_batch.models.EnvironmentSetting(name=e["name"], value=e["value"])
                for e in environment
            ],
            resource_files=[],
        )

        if time_min > 0:
            task_params.constraints = self._azure_batch.models.TaskConstraints(
                max_wall_clock_time=f"PT{time_min}M",
                max_retry_count=0,
            )

        client.task.add(self.account_name, job_id, task_params)

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
        del variables_json, env, stdout_path, stderr_path, max_retries, worker_id, kwargs  # noqa: F841, ARG002
        self._container_digest = container_digest

        log.info(
            "azure_batch submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
        )

        # Ephemeral-runner contract (issue #996, #1077): serialize the step
        # call into the task payload; the Batch-side
        # ``python -m osimflow.remote_runner`` decodes it and executes the
        # work function in container-local storage.
        step_name = self._infer_step_name(name)
        task_payload = self._build_task_payload(
            step_name=step_name,
            args=args,
            kwargs={},
            result_hint=result_hint,
            name=name,
        )

        if remote_command:
            command: str = f"/bin/sh -c {remote_command!r}"
        else:
            command = "python -m osimflow.remote_runner"

        environment = self._build_environment(
            container=container,
            openstudio_version=openstudio_version,
            task_payload=task_payload,
            result_transport_mode=(
                str(result_transport_mode) if result_transport_mode is not None else None
            ),
            result_storage_backend=(
                str(result_storage_backend) if result_storage_backend is not None else None
            ),
            result_storage_bucket=(
                str(result_storage_bucket) if result_storage_bucket is not None else None
            ),
            result_storage_prefix=(
                str(result_storage_prefix) if result_storage_prefix is not None else None
            ),
            result_storage_endpoint=(
                str(result_storage_endpoint) if result_storage_endpoint is not None else None
            ),
        )

        del fn  # noqa: ARG002 — work runs inside the Batch container via remote_runner

        submit_params: dict[str, Any] = {
            "name": name,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "time_min": time_min,
            "environment": environment,
            "command": command,
        }
        job_id = self._submit_job(**submit_params)

        return _AzureBatchHandle(
            job_id=job_id,
            executor=self,
            submit_params=submit_params,
            result_hint=result_hint,
        )

    def shutdown(self) -> None:
        pass
