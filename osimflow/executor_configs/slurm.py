"""Slurm executor configuration + CLI flags (issue #1575).

Owns the ``SlurmConfig`` dataclass and the ``--slurm-*`` flags that
``osimflow run``/``osimflow warm-cache`` register for the ``slurm``
executor. Registered as the ``slurm`` argument hook by
``osimflow/executor_configs/__init__.py``.
"""

import argparse
import dataclasses
import math
from typing import Any

from osimflow.executors import get_step_resources


@dataclasses.dataclass(frozen=True)
class SlurmConfig:
    """Slurm executor configuration.

    Attributes
    ----------
    qos
        Quality of Service for the Slurm job.
    constraint
        Constraint for the Slurm job (e.g., "gpu").
    gres
        Generic resource specification (e.g., "gpu:1").
    cost_per_node_hour
        Cost per node-hour in USD for cost tracking.
    """

    qos: str | None = None
    constraint: str | None = None
    gres: str | None = None
    cost_per_node_hour: float = 0.0


def add_arguments(parser_group: argparse.ArgumentParser) -> None:
    """Register the ``slurm`` executor's ``run`` flags (issue #1575)."""
    parser_group.add_argument("--slurm-partition", default="short")
    parser_group.add_argument("--slurm-account", default=None)
    parser_group.add_argument(
        "--slurm-real",
        action="store_true",
        help="Submit to real Slurm (default: submitit DebugExecutor)",
    )
    parser_group.add_argument(
        "--slurm-qos",
        default=None,
        help="Slurm QoS (e.g. 'high'). Requires submitit >= 1.5.",
    )
    parser_group.add_argument(
        "--slurm-constraint",
        default=None,
        help="Slurm constraint feature (e.g. 'gpu'). Requires submitit >= 1.5.",
    )
    parser_group.add_argument(
        "--slurm-gres",
        default=None,
        help="Slurm generic resources (e.g. 'gpu:1'). Requires submitit >= 1.5.",
    )
    parser_group.add_argument(
        "--slurm-cost-per-node-hour",
        type=float,
        default=None,
        help=(
            "Slurm cost in USD per node-hour for cost tracking (issue #447). "
            "When set alongside --track-costs, this rate is used instead of the "
            "default $0.10/node·hr to estimate job costs."
        ),
    )


#: Per-job Slurm resource directives (issue #1681). The previous
#: hand-rolled API mirror hardcoded these values; this module is now
#: the single source of truth. Sourced from
#: :func:`osimflow.executors.get_step_resources` so the simulation
#: step's CPU/memory/time defaults inform the Slurm ``#SBATCH``
#: directives without duplicating the policy in a per-surface constant.
_DEFAULT_SLURM_DIRECTIVES: dict[str, int] = dict(get_step_resources("RUN_OPENSTUDIO_SIM"))


def kwargs_for_executor(**kwargs: Any) -> dict[str, Any]:
    """Translate the flat CLI / API kwargs into ``SlurmExecutor`` kwargs (issue #1681).

    The Slurm executor constructor takes ``partition``, ``account``,
    ``cpus_per_task``, ``mem_gb``, ``time_h``, ``debug``, ``qos``,
    ``constraint``, ``gres``, and ``submit_rps``. The first three
    resource directives are derived from
    :data:`osimflow.executors.DEFAULT_STEP_RESOURCES` for the
    ``RUN_OPENSTUDIO_SIM`` step (issue #1681 acceptance: replace the
    hand-rolled ``cpus_per_task: 2, mem_gb: 4, time_h: 2`` defaults
    with the shared registry policy).
    """
    cpus_per_task = int(_DEFAULT_SLURM_DIRECTIVES["cpus"])
    mem_gb = max(1, math.ceil(_DEFAULT_SLURM_DIRECTIVES["memory_mb"] / 1024))
    time_h = max(1, math.ceil(_DEFAULT_SLURM_DIRECTIVES["time_min"] / 60))
    return {
        "partition": kwargs.get("slurm_partition") or "short",
        "account": kwargs.get("slurm_account"),
        "cpus_per_task": cpus_per_task,
        "mem_gb": mem_gb,
        "time_h": time_h,
        "debug": not bool(kwargs.get("slurm_real", False)),
        "qos": kwargs.get("slurm_qos"),
        "constraint": kwargs.get("slurm_constraint"),
        "gres": kwargs.get("slurm_gres"),
        "submit_rps": kwargs.get("submit_rps"),
    }
