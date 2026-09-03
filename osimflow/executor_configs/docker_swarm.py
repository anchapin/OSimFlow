"""Docker Swarm executor CLI flags (issue #1575).

Owns the ``--docker-swarm-*`` flags that ``osimflow run``/
``osimflow warm-cache`` register for the ``docker_swarm`` executor.
Registered as the ``docker_swarm`` argument hook by
``osimflow/executor_configs/__init__.py``. Docker Swarm needs no
``XConfig`` dataclass — every knob is consumed directly from the
parsed CLI namespace by ``osimflow.__main__._build_executor``.
"""

import argparse


def add_arguments(parser_group: argparse.ArgumentParser) -> None:
    """Register the ``docker_swarm`` executor's ``run`` flags (issue #1575)."""
    parser_group.add_argument(
        "--docker-swarm-poll-interval-s",
        type=float,
        default=5.0,
        help="Docker Swarm polling interval in seconds (default: 5.0).",
    )
    parser_group.add_argument(
        "--docker-swarm-max-poll-interval-s",
        type=float,
        default=60.0,
        help="Docker Swarm max polling interval in seconds (default: 60.0).",
    )
    parser_group.add_argument(
        "--docker-swarm-image",
        default="nrel/openstudio:3.11.0",
        help="Docker image for Swarm services (default: nrel/openstudio:3.11.0). "
        "WARNING: Using 'latest' is not recommended for production due to "
        "supply-chain risk — the image digest can change over time.",
    )
    parser_group.add_argument(
        "--docker-swarm-network",
        default=None,
        help="Docker network to attach Swarm services to.",
    )
