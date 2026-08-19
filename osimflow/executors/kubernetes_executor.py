"""Kubernetes executor for OSimFlow campaigns (issue #254, #997).

Wraps the Kubernetes Python client (`kubernetes`) to create a Job per
call, then polls the job status with exponential backoff until the
pod reaches a terminal state. The returned Handle carries the job name
and blocks on `.result()` until the task succeeds; on failure it
re-raises a RuntimeError with the pod's failure message.

Each Job runs the ephemeral-runner pattern (issue #996), mirroring
``NomadExecutor``: the container command defaults to
``python -m osimflow.remote_runner`` (or an explicit ``remote_command``
override), the serialized task payload travels in the
``OSIMFLOW_TASK_PAYLOAD`` env var, and the result-transport contract
travels in the ``OSIMFLOW_RESULT_TRANSPORT_MODE`` /
``OSIMFLOW_RESULT_STORAGE_*`` env vars. The runner decodes the payload,
executes the step work function in container-local storage, and pushes
results to object storage — no shared (RWX/NFS) volume is required.
When the transport mode is ``object_storage``, the handle downloads
the job-side artifacts via ``materialize_object_storage_result`` so
Campaign callbacks receive local paths (issue #996).

Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
Kubernetes resource requests and limits. Per-sample
`OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER` are carried as
environment variables — the same env vars `SlurmExecutor` and
`AWSBatchExecutor` export, so downstream work scripts can be
substrate-agnostic.

Native Job controls (issue #997): ``backoff_limit``,
``ttl_seconds_after_finished``, and the optional
``kueue.x-k8s.io/queue-name`` label are configurable. Defaults
preserve the pre-#997 manifest byte-for-byte (backoff=0, no TTL, no
extra labels) so existing campaigns run unchanged when the flags are
left unset.

Security: credentials are sourced from the in-cluster service account
or from `~/.kube/config`. The constructor does **not** accept explicit
credentials; using the configured kubeconfig or in-cluster service
account is the recommended path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, cast

from osimflow.executors.base import BaseExecutor, Handle
from osimflow.executors.transport import (
    coerce_transport_mode,
    encode_transport_value,
    materialize_object_storage_result,
    resolve_result_for_callback,
)

log = logging.getLogger("osimflow.executors.kubernetes")


class _KubernetesHandle(Handle):
    """Handle that polls Kubernetes on `.result()`.

    The work runs in a remote Kubernetes Job (not a thread or submitit
    job), so we cannot back the Future with a local completion.
    Instead, the handle carries a reference to its executor and the
    job name; `result()` blocks on `_wait_for_terminal` and `done()`
    does a single non-blocking status check.

    Result transport (issue #996): when ``result_transport_mode`` is
    ``"object_storage"``, the job-side runner uploaded its artifacts to
    the configured bucket; ``result()`` downloads them to the hint's
    local paths via ``materialize_object_storage_result`` (same
    contract as ``_NomadHandle``) so Campaign callbacks receive local
    paths. For ``"shared_fs"`` / ``"auto"`` modes the hint is decoded
    through ``resolve_result_for_callback`` only.
    """

    def __init__(
        self,
        job_name: str,
        executor: KubernetesExecutor,
        submit_params: dict[str, Any],
        *,
        result_hint: Any = None,
        result_transport_mode: str = "auto",
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> None:
        self.job_id = job_name
        self._job_name = job_name
        self._executor = executor
        self._submit_params = submit_params
        self._result_hint = result_hint
        self._result_transport_mode = coerce_transport_mode(result_transport_mode)
        self._result_storage_backend = result_storage_backend
        self._result_storage_bucket = result_storage_bucket
        self._result_storage_prefix = result_storage_prefix
        self._result_storage_endpoint = result_storage_endpoint
        self._future: Future[Any] = Future()
        self.worker_id: str | None = job_name
        self.worker_ip: str | None = None
        self.worker_region: str | None = None
        self.cost_usd: float | None = None
        self.billed_duration_seconds: float | None = None

    def result(self, timeout: float | None = None) -> Any:  # noqa: ARG002
        try:
            pod_status = self._executor._wait_for_terminal(self._job_name)
        except BaseException as exc:
            self.error = exc  # type: ignore[assignment]
            self._future.set_exception(exc)
            raise

        phase = pod_status.get("status", {}).get("phase", "")
        if phase == "Succeeded":
            resolved = resolve_result_for_callback(
                self._result_hint,
                default=None,
                transport_mode=self._result_transport_mode,
            )
            resolved = materialize_object_storage_result(
                resolved,
                transport_mode=self._result_transport_mode,
                result_storage_backend=self._result_storage_backend,
                result_storage_bucket=self._result_storage_bucket,
                result_storage_prefix=self._result_storage_prefix,
                result_storage_endpoint=self._result_storage_endpoint,
            )
            self._future.set_result(resolved)
            return resolved

        reason = self._extract_failure_reason(pod_status)
        msg = f"Kubernetes job {self._job_name!r} {phase}: {reason}"
        self._future.set_exception(RuntimeError(msg))
        raise RuntimeError(msg)

    def done(self) -> bool:
        if self._future.done():
            return True
        try:
            pod_status = self._executor._get_pod_status(self._job_name)
            phase = pod_status.get("status", {}).get("phase", "")
            return phase in ("Succeeded", "Failed")
        except TimeoutError as exc:
            log.debug("Kubernetes done() timeout for job %s: %s", self._job_name, exc)
            return False
        except ConnectionError as exc:
            log.debug("Kubernetes done() connection error for job %s: %s", self._job_name, exc)
            return False
        except Exception as exc:
            status = getattr(exc, "status", 0)
            if status in (401, 403, 404):
                log.warning(
                    "Kubernetes done() permanent error for job %s: %s [status=%s]",
                    self._job_name,
                    exc,
                    status,
                )
                self._future.set_exception(exc)
                raise
            log.debug("Kubernetes done() transient error for job %s: %s", self._job_name, exc)
            return False

    def _extract_failure_reason(self, pod_status: dict[str, Any]) -> str:
        """Extract the most useful failure reason from a pod status."""
        containers = pod_status.get("status", {}).get("containerStatuses", []) or []
        for container in containers:
            state = container.get("state", {})
            terminated = state.get("terminated", {})
            if terminated:
                exit_code = terminated.get("exitCode", 0)
                if exit_code != 0:
                    reason = terminated.get("reason", "Unknown")
                    return f"exit code {exit_code} ({reason})"
            waiting = state.get("waiting", {})
            if waiting:
                reason = str(waiting.get("reason", "Unknown"))
                message = str(waiting.get("message", ""))
                if message:
                    return f"{reason}: {message}"
                return reason
        return "unknown"


class KubernetesExecutor(BaseExecutor):
    """Kubernetes executor for OSimFlow campaigns (issue #254).

    Wraps the Kubernetes Python client (`kubernetes`) to create a Job per
    call, then polls the job status with exponential backoff until the
    pod reaches a terminal state. The returned Handle carries the job
    name and blocks on `.result()` until the task succeeds; on failure
    it re-raises a RuntimeError.

    Each Job runs the ephemeral-runner pattern (issue #996), mirroring
    ``NomadExecutor``: the container command defaults to
    ``python -m osimflow.remote_runner`` (or an explicit
    ``remote_command`` override run via ``/bin/sh -c``), the task
    payload travels in ``OSIMFLOW_TASK_PAYLOAD``, and the
    result-transport contract in ``OSIMFLOW_RESULT_TRANSPORT_MODE`` /
    ``OSIMFLOW_RESULT_STORAGE_*`` env vars. Worker images must ship the
    ``osimflow`` package for the default command to resolve (see
    ``docs/kubernetes-deployment.md``).

    Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
    Kubernetes resource requests and limits. Per-sample
    `OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER` are carried as
    environment variables — the same env vars `SlurmExecutor` and
    `AWSBatchExecutor` export, so downstream work scripts can be
    substrate-agnostic.

    Security: credentials are sourced from the in-cluster service account
    or from `~/.kube/config`. The constructor does **not** accept explicit
    credentials; using the configured kubeconfig or in-cluster service
    account is the recommended path.

    The Kubernetes Python client is lazy-imported inside `__init__` so
    the local-executor / slurm-executor paths do not pay the import cost.
    """

    name = "kubernetes"

    def __init__(
        self,
        namespace: str = "default",
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
        backoff_limit: int = 0,
        ttl_seconds_after_finished: int | None = None,
        queue_name: str | None = None,
    ):
        self.namespace = namespace
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        # Native Job controls (issue #997). Defaults preserve the
        # pre-#997 manifest byte-for-byte: backoff_limit=0, no TTL, no
        # extra labels. Setting ``backoff_limit`` > 0 enables K8s-native
        # pod retry as an alternative to ``--max-sample-retries`` (the
        # orchestrator-side retry loop); pick one, not both.
        self.backoff_limit = int(backoff_limit)
        self.ttl_seconds_after_finished = (
            int(ttl_seconds_after_finished) if ttl_seconds_after_finished is not None else None
        )
        # Kueue opt-in: applied as the ``kueue.x-k8s.io/queue-name`` label
        # on the Job's metadata. Inert on clusters without Kueue
        # installed (the label is harmless without the controller).
        self.queue_name = queue_name
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy Kubernetes client construction using config.load_kube_config
        or config.load_incluster_config.
        """
        if self._client is None:
            from kubernetes import client, config

            try:
                config.load_kube_config()
            except Exception:
                config.load_incluster_config()
            self._client = client.BatchV1Api()
        return self._client

    def _build_job_name(self, name: str) -> str:
        """Build a valid Kubernetes job name from the task name.

        Kubernetes job names must match DNS-1123 subdomain naming rules:
        lowercase alphanumeric + hyphens, max 253 chars, start/end with
        alphanumeric.
        """
        safe_name = name.lower().replace("_", "-")[:253].strip("-")
        if not safe_name:
            safe_name = "osimflow-task"
        return f"osimflow-{safe_name}"

    @staticmethod
    def _infer_step_name(submit_name: str) -> str:
        """Map a submit name to the remote_runner step identifier.

        Same mapping as ``NomadExecutor._infer_step_name``: the Campaign
        names fan-out tasks ``apply_<sid>`` / ``sim_<sid>`` / ``kpi_<sid>``
        and the single-shot steps ``aggregate`` / ``plots``; the remote
        runner resolves the work function from the step identifier.
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
        so ``osimflow.remote_runner`` can decode either executor's Jobs
        identically (issue #996).
        """
        payload = {
            "schema_version": 1,
            "name": name,
            "step": step_name,
            "args": [KubernetesExecutor._encode_payload_value(a) for a in args],
            "kwargs": {k: KubernetesExecutor._encode_payload_value(v) for k, v in kwargs.items()},
            "result_hint": KubernetesExecutor._encode_payload_value(result_hint),
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
        """Build environment variables for the container.

        Mirrors the ``NomadExecutor._build_job_spec`` env block: the
        serialized task payload travels in ``OSIMFLOW_TASK_PAYLOAD`` and
        the result-transport contract in the ``OSIMFLOW_RESULT_*`` vars
        so ``osimflow.remote_runner`` can execute the step and push
        results to object storage (issue #996). ``OSIMFLOW_STUB_SIM``
        is propagated from the orchestrator environment when set so
        remote pods honour the orchestrator's stub-vs-real CLI choice.
        """
        env: list[dict[str, str]] = []
        if openstudio_version is not None:
            env.append({"name": "OSIMFLOW_OS_VERSION", "value": str(openstudio_version)})
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

    def _wait_for_terminal(self, job_name: str) -> dict[str, Any]:
        """Poll job status with exponential backoff until terminal state.

        Returns the pod status dict for the job's pod.
        """
        delay = self.poll_interval_s
        while True:
            try:
                pod_status = self._get_pod_status(job_name)
            except Exception as exc:
                log.warning("error getting pod status for %s: %s", job_name, exc)
                delay = min(delay * 2, self.max_poll_interval_s)
                time.sleep(delay)
                continue

            phase = pod_status.get("status", {}).get("phase", "")
            if phase in ("Succeeded", "Failed"):
                return pod_status

            log.info(
                "kubernetes poll job=%s phase=%s (sleeping %.1fs)",
                job_name,
                phase,
                delay,
            )
            delay = min(delay * 2, self.max_poll_interval_s)
            time.sleep(delay)

    def _get_pod_status(self, job_name: str) -> dict[str, Any]:
        """Get the pod status for a job's pod.

        Lists pods matching the job selector and returns the first one.
        """
        client = self._get_client()
        label_selector = f"job-name={job_name}"
        pods = client.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=label_selector,
        )
        if not pods.items:
            return {"status": {"phase": "Pending"}}
        return cast(dict[str, Any], pods.items[0].to_dict())

    def _submit_job(
        self,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        time_min: int,
        environment: list[dict[str, str]],
        command: list[str] | None = None,
    ) -> str:
        """Submit a Kubernetes Job and return the job name.

        ``command`` is the container entrypoint. It defaults to the
        ephemeral runner (``python -m osimflow.remote_runner``) so each
        Job executes real campaign work (issue #996); an explicit
        ``remote_command`` override arrives here as
        ``["/bin/sh", "-c", remote_command]``.
        """
        from kubernetes import client

        job_name = self._build_job_name(name)
        container_image = "nrel/openstudio:latest"
        for e in environment:
            if e["name"] == "OSIMFLOW_CONTAINER":
                container_image = e["value"]
                break

        env_vars = [client.V1EnvVar(name=e["name"], value=e["value"]) for e in environment]

        resources = client.V1ResourceRequirements(
            requests={"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
            limits={"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
        )

        container = client.V1Container(
            name="osimflow",
            image=container_image,
            command=command or ["python", "-m", "osimflow.remote_runner"],
            env=env_vars,
            resources=resources,
        )

        template = client.V1PodTemplateSpec(
            spec=client.V1PodSpec(
                containers=[container],
                restart_policy="Never",
            ),
        )

        # Native Job controls (issue #997): plumb ``backoff_limit`` /
        # ``ttl_seconds_after_finished`` from the executor instance onto
        # the V1JobSpec. Defaults preserve the pre-#997 manifest byte
        # representation: backoff_limit=0, ttl_seconds_after_finished
        # absent. The Kueue ``queue-name`` label is added on the
        # metadata only when the executor was constructed with a
        # ``queue_name`` — leaving the metadata untouched by default
        # so behavior is unchanged without opt-in.
        job_spec_kwargs: dict[str, Any] = {
            "template": template,
            "backoff_limit": self.backoff_limit,
            "active_deadline_seconds": int(time_min) * 60 if time_min > 0 else None,
        }
        if self.ttl_seconds_after_finished is not None:
            job_spec_kwargs["ttl_seconds_after_finished"] = self.ttl_seconds_after_finished

        metadata_kwargs: dict[str, Any] = {"name": job_name}
        if self.queue_name:
            metadata_kwargs["labels"] = {"kueue.x-k8s.io/queue-name": self.queue_name}

        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(**metadata_kwargs),
            spec=client.V1JobSpec(**job_spec_kwargs),
        )

        client = self._get_client()
        client.create_namespaced_job(namespace=self.namespace, body=job)

        log.info(
            "kubernetes submit_job -> job=%s namespace=%s",
            job_name,
            self.namespace,
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

        log.info(
            "kubernetes submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
        )

        # Ephemeral-runner contract (issue #996), mirroring NomadExecutor:
        # serialize the step call into the task payload; the Job-side
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
        del fn  # noqa: ARG002 — work runs inside the Job container

        if remote_command:
            command: list[str] = ["/bin/sh", "-c", remote_command]
        else:
            command = ["python", "-m", "osimflow.remote_runner"]

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

        submit_params: dict[str, Any] = {
            "name": name,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "time_min": time_min,
            "environment": environment,
            "command": command,
        }
        job_name = self._submit_job(**submit_params)

        return _KubernetesHandle(
            job_name=job_name,
            executor=self,
            submit_params=submit_params,
            result_hint=result_hint,
            result_transport_mode=(
                str(result_transport_mode) if result_transport_mode is not None else "auto"
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

    def shutdown(self) -> None:
        pass
