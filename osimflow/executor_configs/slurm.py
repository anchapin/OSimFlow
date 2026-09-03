"""Slurm executor configuration + CLI flags (issue #1575).

Owns the ``SlurmConfig`` dataclass and the ``--slurm-*`` flags that
``osimflow run``/``osimflow warm-cache`` register for the ``slurm``
executor. Registered as the ``slurm`` argument hook by
``osimflow/executor_configs/__init__.py``.
"""

import argparse
import dataclasses


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
