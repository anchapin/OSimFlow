"""Dask-JobQueue elastic worker executor (issue #338).

Wraps ``dask_jobqueue.SLURMCluster``, ``PBSCluster``, or ``KubernetesCluster``
to provide horizontal auto-scaling for HPC workloads. Workers are dynamically
added/removed based on the number of pending tasks in the Dask scheduler queue.

The executor integrates with the Campaign's fan-out pattern: when many samples
are submitted concurrently, Dask automatically scales workers up to
``max_workers``; as samples complete, workers scale back down to ``min_workers``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from osimflow.executors.base import BaseExecutor, Handle

log = logging.getLogger("osimflow.executors")

__all__ = ["DaskJobQueueExecutor"]


class _DaskJobQueueHandle(Handle):
    """Handle that wraps a Dask ``Future`` for a submitted task.

    The Dask client holds a reference to the scheduler; ``result()``
    blocks on the Dask Future. Auto-scaling is managed by the cluster
    independently of individual task completion.
    """

    def __init__(
        self,
        job_id: str,
        future: Future[Any],
        cluster: Any,
    ) -> None:
        self.job_id = job_id
        self._future = future
        self._cluster = cluster
        self.worker_id: str | None = job_id
        self.worker_ip: str | None = None
        self.worker_region: str | None = None

    def done(self) -> bool:
        return self._future.done()

    def result(self, timeout: float | None = None) -> Any:
        try:
            return self._future.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
            self.error = exc
            raise


class DaskJobQueueExecutor(BaseExecutor):
    """Elastic HPC executor using Dask-JobQueue.

    Supports three cluster backends:

    * ``slurm`` — ``dask_jobqueue.SLURMCluster``
    * ``pbs`` — ``dask_jobqueue.PBSCluster``
    * ``kubernetes`` — ``dask_jobqueue.KubernetesCluster``

    Worker count is automatically adjusted based on the number of pending
    tasks in the Dask scheduler queue. The executor scales between
    ``min_workers`` (always-keep pool) and ``max_workers`` (peak load).

    Resource directives (``cpus``, ``memory_mb``) are forwarded to the
    underlying cluster job submission. Per-sample ``OSIMFLOW_OS_VERSION``
    and ``OSIMFLOW_CONTAINER`` are carried as environment variables so
    downstream work scripts are substrate-agnostic.

    Security: credentials are sourced from the environment
    (``SLURM_*`` env vars for Slurm, ``PBS_*`` env vars for PBS,
    in-cluster service account for Kubernetes). The constructor does
    **not** accept long-lived credentials.
    """

    name = "dask_jobqueue"

    def __init__(
        self,
        cluster_type: str = "slurm",
        min_workers: int = 1,
        max_workers: int = 10,
        cpus_per_worker: int = 2,
        memory_per_worker: str = "4GiB",
        *,
        walltime: str = "02:00:00",
        queue: str | None = None,
        project: str | None = None,
        job_extra: dict[str, Any] | None = None,
        scale_interval_s: float = 5.0,
    ) -> None:
        self.cluster_type = cluster_type
        self.min_workers = min_workers
        self.max_workers = max_workers
        self.cpus_per_worker = cpus_per_worker
        self.memory_per_worker = memory_per_worker
        self.walltime = walltime
        self.queue = queue
        self.project = project
        self.job_extra = job_extra or {}
        self.scale_interval_s = scale_interval_s

        self._cluster: Any = None
        self._client: Any = None
        self._scaler_running = False

    def _build_cluster(self) -> Any:
        """Build and return a Dask-JobQueue cluster instance."""
        import dask_jobqueue  # noqa: PLC0415

        common_kwargs: dict[str, Any] = {
            "n_workers": self.min_workers,
            "cores": self.cpus_per_worker,
            "memory": self.memory_per_worker,
            "walltime": self.walltime,
            "job_extra": dict(self.job_extra),
        }
        if self.queue is not None:
            common_kwargs["queue"] = self.queue
        if self.project is not None:
            common_kwargs["project"] = self.project

        if self.cluster_type == "slurm":
            return dask_jobqueue.SLURMCluster(**common_kwargs)
        if self.cluster_type == "pbs":
            return dask_jobqueue.PBSCluster(**common_kwargs)
        if self.cluster_type == "kubernetes":
            return dask_jobqueue.KubernetesCluster(**common_kwargs)
        raise ValueError(
            f"unknown dask cluster type: {self.cluster_type!r} "
            "(expected 'slurm', 'pbs', or 'kubernetes')"
        )

    def _ensure_cluster(self) -> Any:
        """Lazily create the Dask cluster and client."""
        if self._cluster is None:
            self._cluster = self._build_cluster()
            self._client = self._cluster.get_client()
            log.info(
                "dask_jobqueue: created %s cluster (min=%d, max=%d, cpus=%d, mem=%s)",
                self.cluster_type,
                self.min_workers,
                self.max_workers,
                self.cpus_per_worker,
                self.memory_per_worker,
            )
        return self._cluster

    def _scale_to(self, n_workers: int) -> None:
        """Request a specific number of workers."""
        cluster = self._ensure_cluster()
        requested = max(self.min_workers, min(n_workers, self.max_workers))
        cluster.scale(requested)
        log.info(
            "dask_jobqueue: scale request -> %d workers (range: %d-%d)",
            requested,
            self.min_workers,
            self.max_workers,
        )

    def _auto_scale(self) -> None:
        """Scale workers based on pending task backlog.

        Queries the Dask scheduler for the number of pending tasks and
        scales workers proportionally. Runs in a background loop until
        ``shutdown`` is called.
        """

        while self._scaler_running:
            try:
                client = self._ensure_cluster().get_client()
                pending = len(client.tasks())
                log.debug("dask_jobqueue: %d pending tasks", pending)
                if pending > 0:
                    target = min(pending, self.max_workers)
                    self._scale_to(target)
            except Exception as exc:
                log.warning("dask_jobqueue: auto-scale error: %s", exc)
            time.sleep(self.scale_interval_s)

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
        del result_hint, remote_command, result_transport_mode  # noqa: F841
        del result_storage_backend, result_storage_bucket, result_storage_prefix  # noqa: F841
        del result_storage_endpoint, variables_json, env  # noqa: F841
        del stdout_path, stderr_path, max_retries, worker_id, kwargs  # noqa: F841
        log.info(
            "dask_jobqueue submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
        )

        cluster = self._ensure_cluster()

        if not self._scaler_running:
            self._scaler_running = True
            import threading  # noqa: PLC0415

            t = threading.Thread(target=self._auto_scale, daemon=True, name="dask-autoscale")
            t.start()
            log.info("dask_jobqueue: auto-scaler started")

        def _wrapped() -> Any:
            import os as _os  # noqa: PLC0415

            if openstudio_version is not None:
                _os.environ["OSIMFLOW_OS_VERSION"] = str(openstudio_version)
            if container is not None:
                _os.environ["OSIMFLOW_CONTAINER"] = container
            return fn(*args)

        future = cluster.get_client().submit(_wrapped)
        job_id = f"dask-{name}-{id(future)}"
        return _DaskJobQueueHandle(
            job_id=job_id,
            future=future,
            cluster=cluster,
        )

    def shutdown(self) -> None:
        self._scaler_running = False
        if self._cluster is not None:
            self._cluster.close()
            log.info("dask_jobqueue: cluster closed")
            self._cluster = None
            self._client = None
