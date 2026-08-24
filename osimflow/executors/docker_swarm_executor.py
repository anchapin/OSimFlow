"""Docker Swarm executor for OSimFlow campaigns (issue #582).

Wraps the Docker Python SDK (`docker`) to create a Swarm Service per
call, then polls the service's tasks with exponential backoff until
all tasks reach a terminal state. The returned Handle carries the
service name and blocks on `.result()` until the task succeeds; on
failure it re-raises a RuntimeError with the task's failure message.

Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
Docker resource limits. Per-sample `OSIMFLOW_OS_VERSION` and
`OSIMFLOW_CONTAINER` are carried as service labels — the same env
vars `SlurmExecutor` and `AWSBatchExecutor` export, so downstream
work scripts can be substrate-agnostic.

Security: credentials are sourced from the Docker daemon (mounted
socket or `DOCKER_HOST`). The constructor does **not** accept explicit
credentials.

The `docker` package is lazy-imported inside `__init__` and `_get_client`
so the local-executor / slurm-executor paths do not pay the import
cost.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, cast

from osimflow.executors.base import BaseExecutor, Handle

log = logging.getLogger("osimflow.executors.docker_swarm")


def _docker_error_code(exc: Exception) -> int:
    """Extract HTTP status code from a docker API error, or 0 if not applicable."""
    try:
        response = getattr(exc, "response", None)
        if response is None:
            return 0
        return getattr(response, "status_code", 0) or 0
    except Exception:  # noqa: BLE001
        return 0


class _DockerSwarmHandle(Handle):
    """Handle that polls Docker Swarm service tasks on `.result()`.

    The work runs in a remote Swarm service (not a thread or submitit
    job), so we cannot back the Future with a local completion.
    Instead, the handle carries a reference to its executor and the
    service name; `result()` blocks on `_wait_for_terminal` and
    `done()` does a single non-blocking status check.
    """

    def __init__(
        self,
        service_name: str,
        executor: DockerSwarmExecutor,
        submit_params: dict[str, Any],
    ) -> None:
        self.job_id = service_name
        self._service_name = service_name
        self._executor = executor
        self._submit_params = submit_params
        self._future: Future[Any] = Future()
        # Worker tracking (issue #105): populated at submit time.
        self.worker_id: str | None = service_name
        self.worker_ip: str | None = None
        self.worker_region: str | None = None
        self.cost_usd: float | None = None
        self.billed_duration_seconds: float | None = None

    def result(self, timeout: float | None = None) -> Any:  # noqa: ARG002
        # The polling itself doesn't take a `timeout` parameter; the
        # service-level update timeout (when set) is the substrate-level
        # kill. `timeout` here is accepted for the base-class signature
        # but not enforced.
        try:
            task_result = self._executor._wait_for_terminal(self._service_name)
        except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
            self._future.set_exception(exc)
            raise

        status = task_result.get("status", {})
        state = status.get("State", "")
        if state == "complete":
            self._future.set_result(None)
            return None

        # Extract the most useful error message from the task.
        err_msg = self._extract_error_message(task_result)
        msg = f"Docker Swarm service {self._service_name!r} task {state}: {err_msg}"
        self._future.set_exception(RuntimeError(msg))
        raise RuntimeError(msg)

    def done(self) -> bool:
        if self._future.done():
            return True
        try:
            service_status = self._executor._get_service_status(self._service_name)
            tasks = service_status.get("tasks", []) or []
            if not tasks:
                return False
            # A service is "done" when all its tasks are in a terminal state.
            terminal_states = {"complete", "failed", "shutdown", "rejected"}
            return all(task.get("status", {}).get("State", "") in terminal_states for task in tasks)
        except TimeoutError as exc:
            log.debug("Docker Swarm done() timeout for service %s: %s", self._service_name, exc)
            return False
        except ConnectionError as exc:
            log.debug(
                "Docker Swarm done() connection error for service %s: %s", self._service_name, exc
            )
            return False
        except Exception as exc:  # noqa: BLE001
            status_code = _docker_error_code(exc)
            if status_code in (401, 403, 404):
                log.warning(
                    "Docker Swarm done() permanent error for service %s: %s [status=%s]",
                    self._service_name,
                    exc,
                    status_code,
                )
                self._future.set_exception(exc)
                raise
            log.debug(
                "Docker Swarm done() transient error for service %s: %s", self._service_name, exc
            )
            return False

    @staticmethod
    def _extract_error_message(task: dict[str, Any]) -> str:
        """Extract the most useful error message from a task status."""
        status = task.get("status", {})
        # Try to get the error message from the task's status message.
        message = status.get("Message", "")
        if message:
            return str(message)
        # Fall back to the status err string.
        err = status.get("Err", "")
        if err:
            return str(err)
        # Try container status if available.
        container_status = status.get("ContainerStatus", {}) or {}
        if container_status:
            exit_code = container_status.get("ExitCode", 0)
            if exit_code != 0:
                return f"exit code {exit_code}"
        return "unknown"


class DockerSwarmExecutor(BaseExecutor):
    """Docker Swarm executor for OSimFlow campaigns (issue #582).

    Wraps the Docker Python SDK (`docker`) to create a Swarm Service per
    call, then polls the service's tasks with exponential backoff until
    all tasks reach a terminal state. The returned Handle carries the
    service name and blocks on `.result()` until the task succeeds; on
    failure it re-raises a RuntimeError with the task's failure message.

    This executor uses **Services** (not Tasks) as the tracking primitive
    because services persist after their tasks complete, allowing the
    Campaign to query status even after the initial task has finished.

    **Fail-dense by default**: when Docker is unavailable or the daemon is
    not in Swarm mode, ``submit()`` raises a ``RuntimeError`` instead of
    silently falling back to ``LocalExecutor``. This prevents BYOS scripts
    from running in the orchestrator process without an explicit opt-in.

    Dev/CI fallback is available via the ``OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1``
    environment variable. When set, the executor falls back to a
    ``LocalExecutor(max_workers=4)`` with a warning, letting campaigns
    proceed in development/CI environments where Docker Swarm is not
    available. The ``Campaign`` also sets ``OSIMFLOW_DOCKER_SWARM_DRY_RUN=1``
    during ``--dry-run`` execution so the fallback is available there too.

    Resource directives (``cpus``, ``memory_mb``, ``time_min``) are mapped to
    Docker resource limits. Per-sample ``OSIMFLOW_OS_VERSION`` and
    ``OSIMFLOW_CONTAINER`` are carried as service labels — the same env
    vars ``SlurmExecutor`` and ``AWSBatchExecutor`` export, so downstream
    work scripts can be substrate-agnostic.

    Security: credentials are sourced from the Docker daemon (mounted
    socket or ``DOCKER_HOST``). The constructor does **not** accept explicit
    credentials.

    The ``docker`` package is lazy-imported inside ``__init__`` and
    ``_get_client`` so the local-executor / slurm-executor paths do not
    pay the import cost.
    """

    name = "docker_swarm"

    @property
    def requires_remote_runner_payload(self) -> bool:
        return True

    def __init__(
        self,
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
        image: str = "nrel/openstudio:3.11.0",
        network: str | None = None,
    ):
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        self.image = image
        self.network = network
        self._client: Any = None
        self._stub_executor: Any = None

        if self.image.endswith(":latest"):
            log.warning(
                "docker-swarm-image is set to %r — using 'latest' is not recommended "
                "for production due to supply-chain risk. "
                "Pin to a specific version tag (e.g. 'nrel/openstudio:3.11.0') "
                "or use --container-digest for immutable references.",
                self.image,
            )

    def _is_dev_fallback_enabled(self) -> bool:
        """Return True when the dev fallback path is explicitly opted in.

        This is set by two mechanisms:
        - OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1  — general dev/CI opt-in
        - OSIMFLOW_DOCKER_SWARM_DRY_RUN=1       — dry-run mode signalled by Campaign
        """
        import os

        return os.environ.get("OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK", "") not in (
            "",
            "0",
        ) or os.environ.get("OSIMFLOW_DOCKER_SWARM_DRY_RUN", "") not in ("", "0")

    def _check_docker_available(self) -> bool:
        """Return True if the Docker client can reach a Swarm cluster."""
        try:
            client = self._get_client()
            info = client.info()
            return bool(info.get("Swarm", {}).get("ControlAvailable", False))
        except Exception as exc:  # noqa: BLE001
            log.warning("Docker Swarm not available: %s", exc)
            return False

    def _get_client(self) -> Any:
        """Lazily construct the Docker client.

        Raises
        ------
        ImportError
            When the `docker` package is not installed.
        RuntimeError
            When the Docker daemon is not reachable.
        """
        if self._client is None:
            try:
                import docker
            except ImportError as exc:
                raise ImportError(
                    "docker Python SDK is required for DockerSwarmExecutor. "
                    "Install it with: pip install docker"
                ) from exc

            try:
                self._client = docker.from_env()  # type: ignore[attr-defined]
                # Verify the connection works.
                self._client.ping()
            except Exception as exc:
                raise RuntimeError(f"Docker daemon is not reachable: {exc}") from exc

        return self._client

    def _build_service_name(self, name: str) -> str:
        """Build a valid Docker Swarm service name from the task name.

        Service names must match DNS-1123 subdomain naming rules:
        lowercase alphanumeric + hyphens, start/end with alphanumeric.
        """
        safe_name = name.lower().replace("_", "-").replace(".", "-")[:128].strip("-")
        if not safe_name:
            safe_name = "osimflow-task"
        return f"osimflow-{safe_name}"

    def _get_service_status(self, service_name: str) -> dict[str, Any]:
        """Get the current status dict for a Swarm service.

        Returns a dict with a "tasks" key containing the list of tasks.
        """
        client = self._get_client()
        try:
            service = client.services.get(service_name)
            tasks = service.tasks(filters={"desired-state": "running"})
            return {"tasks": tasks}
        except Exception as exc:
            log.warning("error getting service status for %s: %s", service_name, exc)
            return {"tasks": []}

    def _wait_for_terminal(self, service_name: str, timeout: float | None = None) -> dict[str, Any]:
        """Poll service tasks with exponential backoff until all are terminal.

        Returns the first non-running task dict.

        Raises:
            TimeoutError: if *timeout* seconds elapse before a terminal state.
        """
        delay = self.poll_interval_s
        start = time.monotonic()
        while True:
            try:
                service_status = self._get_service_status(service_name)
            except Exception as exc:
                log.warning(
                    "error getting service status for %s: %s (sleeping %.1fs)",
                    service_name,
                    exc,
                    delay,
                )
                if timeout is not None:
                    elapsed = time.monotonic() - start
                    remaining = timeout - elapsed
                    if remaining <= 0:
                        raise TimeoutError(
                            f"Timed out after {elapsed:.1f}s waiting for service {service_name!r}"
                        ) from None
                    delay = min(delay, remaining)
                delay = min(delay * 2, self.max_poll_interval_s)
                time.sleep(delay)
                continue

            tasks = service_status.get("tasks", []) or []
            if not tasks:
                # No tasks yet — still starting up.
                log.info(
                    "docker_swarm poll service=%s no-tasks yet (sleeping %.1fs)",
                    service_name,
                    delay,
                )
                if timeout is not None:
                    elapsed = time.monotonic() - start
                    remaining = timeout - elapsed
                    if remaining <= 0:
                        raise TimeoutError(
                            f"Timed out after {elapsed:.1f}s waiting for service {service_name!r}"
                        )
                    delay = min(delay, remaining)
                delay = min(delay * 2, self.max_poll_interval_s)
                time.sleep(delay)
                continue

            # Check if all tasks are in terminal states.
            terminal_states = {"complete", "failed", "shutdown", "rejected"}
            all_terminal = all(
                task.get("status", {}).get("State", "") in terminal_states for task in tasks
            )
            if all_terminal:
                # Return the first task for error extraction.
                return cast(dict[str, Any], tasks[0])

            # Find the first running task for logging.
            running_task = next(
                (t for t in tasks if t.get("status", {}).get("State", "") == "running"),
                tasks[0] if tasks else {},
            )
            current_state = running_task.get("status", {}).get("State", "unknown")
            log.info(
                "docker_swarm poll service=%s state=%s (sleeping %.1fs)",
                service_name,
                current_state,
                delay,
            )
            if timeout is not None:
                elapsed = time.monotonic() - start
                remaining = timeout - elapsed
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out after {elapsed:.1f}s waiting for service {service_name!r}"
                    ) from None
                delay = min(delay, remaining)
            delay = min(delay * 2, self.max_poll_interval_s)
            time.sleep(delay)

    def _submit_service(
        self,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        time_min: int,
        openstudio_version: str | None,
        container: str | None,
        command: list[str] | None = None,
        task_payload: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> str:
        """Create a Docker Swarm service and return its name.

        When ``command`` is provided, it overrides the default container
        command (e.g. to run ``python -m osimflow.remote_runner``).
        """
        client = self._get_client()

        service_name = self._build_service_name(name)
        image = container or self.image

        # Build labels for tracking.
        labels: dict[str, str] = {
            "osimflow.task": "1",
            "osimflow.sample_name": name,
            "osIMFLOW_OS_VERSION": str(openstudio_version or ""),
            "osimflow.container": image,
        }

        # Build environment variables for the container.
        env: list[str] = []
        if openstudio_version is not None:
            env.append(f"OSIMFLOW_OS_VERSION={openstudio_version}")
        env.append(f"OSIMFLOW_CONTAINER={image}")
        if task_payload is not None:
            env.append(f"OSIMFLOW_TASK_PAYLOAD={task_payload}")
        if result_transport_mode is not None:
            env.append(f"OSIMFLOW_RESULT_TRANSPORT_MODE={result_transport_mode}")
        if result_storage_backend is not None:
            env.append(f"OSIMFLOW_RESULT_STORAGE_BACKEND={result_storage_backend}")
        if result_storage_bucket is not None:
            env.append(f"OSIMFLOW_RESULT_STORAGE_BUCKET={result_storage_bucket}")
        if result_storage_prefix is not None:
            env.append(f"OSIMFLOW_RESULT_STORAGE_PREFIX={result_storage_prefix}")
        if result_storage_endpoint is not None:
            env.append(f"OSIMFLOW_RESULT_STORAGE_ENDPOINT={result_storage_endpoint}")
        stub_sim = os.environ.get("OSIMFLOW_STUB_SIM")
        if stub_sim is not None:
            env.append(f"OSIMFLOW_STUB_SIM={stub_sim}")

        # Resource limits.
        # Docker uses nanocpus (1e-9 CPUs) and memory in bytes.
        nano_cpus = int(cpus * 1e9)
        mem_limit = memory_mb * 1024 * 1024  # bytes

        # Placement constraints — leave empty to let Swarm schedule anywhere.
        placement: list[dict[str, Any]] = []

        # Replicated service with 1 replica — one task per sample.
        # Using "replicated-job" would create an anonymous job, but we use
        # a plain "replicated" service so we get a stable service name for
        # querying after the task completes.
        endpoint_spec: dict[str, Any] = {}
        if self.network:
            endpoint_spec = {
                "Ports": [
                    {
                        "Protocol": "tcp",
                        "TargetPort": 80,
                        "PublishMode": "ingress",
                    }
                ]
            }

        try:
            service = client.services.create(
                name=service_name,
                image=image,
                command=command or ["python", "-m", "osimflow.remote_runner"],
                env=env,
                labels=labels,
                container_labels=labels,
                resources={
                    "Limits": {
                        "NanoCPUs": nano_cpus,
                        "MemoryBytes": mem_limit,
                    },
                    "Reservations": {
                        "NanoCPUs": nano_cpus,
                        "MemoryBytes": mem_limit,
                    },
                },
                placement=placement if placement else None,
                endpoint_spec=endpoint_spec if endpoint_spec else None,
                mode={"Replicated": {"Replicas": 1}},
            )
            log.info(
                "docker_swarm submit_service -> service=%s image=%s",
                service_name,
                image,
            )
            return str(service.name)
        except Exception as exc:
            log.error(
                "failed to create Docker Swarm service %s: %s",
                service_name,
                exc,
            )
            raise RuntimeError(
                f"failed to create Docker Swarm service {service_name!r}: {exc}"
            ) from exc

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
        log.info(
            "docker_swarm submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
        )

        self._container_digest = container_digest

        try:
            is_swarm = self._check_docker_available()
        except (ImportError, RuntimeError) as exc:
            if self._is_dev_fallback_enabled():
                log.warning(
                    "Docker unavailable: %s. Falling back to LocalExecutor (dev-fallback mode).",
                    exc,
                )
                from osimflow.executors import LocalExecutor

                if self._stub_executor is None:
                    self._stub_executor = LocalExecutor(max_workers=4)
                return cast(
                    Handle,
                    self._stub_executor.submit(
                        fn,
                        *args,
                        name=name,
                        cpus=cpus,
                        memory_mb=memory_mb,
                        time_min=time_min,
                        container=container,
                        openstudio_version=openstudio_version,
                        result_hint=result_hint,
                        remote_command=remote_command,
                        result_transport_mode=result_transport_mode,
                        result_storage_backend=result_storage_backend,
                        result_storage_bucket=result_storage_bucket,
                        result_storage_prefix=result_storage_prefix,
                        result_storage_endpoint=result_storage_endpoint,
                        variables_json=variables_json,
                        env=env,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        max_retries=max_retries,
                        worker_id=worker_id,
                        **kwargs,
                    ),
                )
            raise RuntimeError(
                f"Docker daemon is not reachable: {exc}. "
                "Use --executor local for local execution, or set "
                "OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1 to fall back to "
                "LocalExecutor in development/CI environments."
            ) from exc

        if not is_swarm:
            if self._is_dev_fallback_enabled():
                log.warning(
                    "Docker daemon is not in Swarm mode. "
                    "Falling back to LocalExecutor (dev-fallback mode). "
                    "Initialize Swarm with: docker swarm init"
                )
                from osimflow.executors import LocalExecutor

                if self._stub_executor is None:
                    self._stub_executor = LocalExecutor(max_workers=4)
                return cast(
                    Handle,
                    self._stub_executor.submit(
                        fn,
                        *args,
                        name=name,
                        cpus=cpus,
                        memory_mb=memory_mb,
                        time_min=time_min,
                        container=container,
                        openstudio_version=openstudio_version,
                        result_hint=result_hint,
                        remote_command=remote_command,
                        result_transport_mode=result_transport_mode,
                        result_storage_backend=result_storage_backend,
                        result_storage_bucket=result_storage_bucket,
                        result_storage_prefix=result_storage_prefix,
                        result_storage_endpoint=result_storage_endpoint,
                        variables_json=variables_json,
                        env=env,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        max_retries=max_retries,
                        worker_id=worker_id,
                        container_digest=container_digest,
                        **kwargs,
                    ),
                )
            raise RuntimeError(
                "Docker daemon is not in Swarm mode. "
                "Run `docker swarm init` to initialize a Swarm, "
                "use --executor local for local execution, "
                "or set OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1 to fall back to "
                "LocalExecutor in development/CI environments."
            )

        self._stub_executor = None

        # Ephemeral-runner contract (issue #996, #1077): serialize the step
        # call into the task payload; the Swarm-side
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
            command: list[str] = ["/bin/sh", "-c", remote_command]
        else:
            command = ["python", "-m", "osimflow.remote_runner"]

        submit_params: dict[str, Any] = {
            "name": name,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "time_min": time_min,
            "openstudio_version": openstudio_version,
            "container": container,
            "command": command,
            "task_payload": task_payload,
            "result_transport_mode": (
                str(result_transport_mode) if result_transport_mode is not None else None
            ),
            "result_storage_backend": (
                str(result_storage_backend) if result_storage_backend is not None else None
            ),
            "result_storage_bucket": (
                str(result_storage_bucket) if result_storage_bucket is not None else None
            ),
            "result_storage_prefix": (
                str(result_storage_prefix) if result_storage_prefix is not None else None
            ),
            "result_storage_endpoint": (
                str(result_storage_endpoint) if result_storage_endpoint is not None else None
            ),
        }

        del fn  # noqa: ARG002 — work runs inside the Swarm container via remote_runner
        # Unused in Docker Swarm mode: result_hint, remote_command, result_transport_mode,
        # result_storage_*, variables_json, env, stdout/stderr_path, max_retries, worker_id.
        del result_hint, remote_command, result_transport_mode  # noqa: F841
        del result_storage_backend, result_storage_bucket, result_storage_prefix  # noqa: F841
        del result_storage_endpoint, variables_json, env  # noqa: F841
        del stdout_path, stderr_path, max_retries, worker_id, kwargs  # noqa: F841

        service_name = self._submit_service(**submit_params)

        return _DockerSwarmHandle(
            service_name=service_name,
            executor=self,
            submit_params=submit_params,
        )

    def shutdown(self) -> None:
        # Docker client holds a socket; clean up on GC. Nothing to do.
        if self._stub_executor is not None and hasattr(self._stub_executor, "shutdown"):
            self._stub_executor.shutdown()
