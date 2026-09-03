"""Dask-JobQueue executor CLI flags (issue #1575).

Owns the ``--dask-*`` cluster flags that ``osimflow run``/
``osimflow warm-cache`` register for the ``dask_jobqueue`` executor.
Registered as the ``dask_jobqueue`` argument hook by
``osimflow/executor_configs/__init__.py``.

``--task-queue`` and ``--dask-scheduler-address`` stay in
``osimflow/__main__.py``: they configure the campaign-level
distributed task queue (``CampaignConfig.dag.task_queue`` /
``dask_scheduler_address`` — see ``osimflow/taskqueue.py``), not the
DaskJobQueueExecutor itself. Dask-JobQueue needs no ``XConfig``
dataclass — every cluster knob is consumed directly from the parsed
CLI namespace by ``osimflow.__main__._build_executor``.
"""

import argparse


def add_arguments(parser_group: argparse.ArgumentParser) -> None:
    """Register the ``dask_jobqueue`` executor's ``run`` flags (issue #1575)."""
    parser_group.add_argument(
        "--dask-cluster-type",
        choices=["slurm", "pbs", "kubernetes"],
        default="slurm",
        help="Dask-JobQueue cluster backend (default: slurm).",
    )
    parser_group.add_argument(
        "--dask-min-workers",
        type=int,
        default=1,
        help="Minimum number of Dask workers to keep alive (default: 1).",
    )
    parser_group.add_argument(
        "--dask-max-workers",
        type=int,
        default=10,
        help="Maximum number of Dask workers to scale up to (default: 10).",
    )
    parser_group.add_argument(
        "--dask-cpus-per-worker",
        type=int,
        default=2,
        help="CPUs per Dask worker (default: 2).",
    )
    parser_group.add_argument(
        "--dask-memory-per-worker",
        default="4GiB",
        help="Memory per Dask worker (default: 4GiB).",
    )
    parser_group.add_argument(
        "--dask-walltime",
        default="02:00:00",
        help="Walltime for Dask cluster jobs (default: 02:00:00).",
    )
    parser_group.add_argument(
        "--dask-queue",
        default=None,
        help=" HPC queue/partition for Dask workers (e.g. short, gpu).",
    )
    parser_group.add_argument(
        "--dask-project",
        default=None,
        help="HPC project/account for Dask workers.",
    )
