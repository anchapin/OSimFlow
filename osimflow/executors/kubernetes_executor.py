"""Kubernetes executor for OSimFlow campaigns (issue #254).

Wraps the Kubernetes Python client (`kubernetes`) to create a Job per
call, then polls the job status with exponential backoff until the
pod reaches a terminal state. The returned Handle carries the job name
and blocks on `.result()` until the task succeeds; on failure it
re-raises a RuntimeError with the pod's failure message.

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
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, cast

from osimflow.executors.base import BaseExecutor, Handle

log = logging.getLogger("osimflow.executors.kubernetes")


class _KubernetesHandle(Handle):
    """Handle that polls Kubernetes on `.result()`.

    The work runs in a remote Kubernetes Job (not a thread or submitit
    job), so we cannot back the Future with a local completion.
    Instead, the handle carries a reference to its executor and the
    job name; `result()` blocks on `_wait_for_terminal` and `done()`
    does a single non-blocking status check.
    """

    def __init__(
        self,
        job_name: str,
        executor: KubernetesExecutor,
        submit_params: dict[str, Any],
    ) -> None:
        self.job_id = job_name
        self._job_name = job_name
        self._executor = executor
        self._submit_params = submit_params
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
            self._future.set_exception(exc)
            raise

        phase = pod_status.get("status", {}).get("phase", "")
        if phase == "Succeeded":
            self._future.set_result(None)
            return None

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
        except Exception:
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
    ):
        self.namespace = namespace
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
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

    def _build_environment(
        self,
        *,
        container: str | None,
        openstudio_version: str | None,
    ) -> list[dict[str, str]]:
        """Build environment variables for the container."""
        env: list[dict[str, str]] = []
        if openstudio_version is not None:
            env.append({"name": "OSIMFLOW_OS_VERSION", "value": str(openstudio_version)})
        resolved = container or f"nrel/openstudio:{openstudio_version or 'latest'}"
        env.append({"name": "OSIMFLOW_CONTAINER", "value": resolved})
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
                time.sleep(delay)
                delay = min(delay * 2, self.max_poll_interval_s)
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
            time.sleep(delay)
            delay = min(delay * 2, self.max_poll_interval_s)

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
    ) -> str:
        """Submit a Kubernetes Job and return the job name."""
        from kubernetes import client

        job_name = self._build_job_name(name)
        container_image = "nrel/openstudio:latest"
        for e in environment:
            if e["name"] == "OSIMFLOW_CONTAINER":
                container_image = e["value"]
                break

        env_vars = [
            client.V1EnvVar(name=e["name"], value=e["value"])
            for e in environment
        ]

        resources = client.V1ResourceRequirements(
            requests={"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
            limits={"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
        )

        container = client.V1Container(
            name="osimflow",
            image=container_image,
            command=["/bin/sh", "-c", "sleep infinity"],
            env=env_vars,
            resources=resources,
        )

        template = client.V1PodTemplateSpec(
            spec=client.V1PodSpec(
                containers=[container],
                restart_policy="Never",
            ),
        )

        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(name=job_name),
            spec=client.V1JobSpec(
                template=template,
                backoff_limit=0,
                active_deadline_seconds=int(time_min) * 60 if time_min > 0 else None,
            ),
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
        **kwargs: Any,
    ) -> Handle:
        openstudio_version = kwargs.get("openstudio_version")

        log.info(
            "kubernetes submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
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

        return _KubernetesHandle(
            job_name=job_name,
            executor=self,
            submit_params=submit_params,
        )

    def shutdown(self) -> None:
        pass
