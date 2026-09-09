"""Docker Swarm executor CLI flags (issue #1575).

Owns the ``--docker-swarm-*`` flags that ``osimflow run``/
``osimflow warm-cache`` register for the ``docker_swarm`` executor.
Registered as the ``docker_swarm`` argument hook by
``osimflow/executor_configs/__init__.py``. Docker Swarm needs no
``XConfig`` dataclass — every knob is consumed directly from the
parsed CLI namespace by ``osimflow.__main__._build_executor``.
"""

import argparse
from typing import Any


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
    parser_group.add_argument(
        "--docker-swarm-payload-secret",
        default=None,
        help=(
            "Name of a pre-created Docker secret holding the task-payload "
            "HMAC secret (value = OSIMFLOW_TASK_PAYLOAD_SECRET). When set, "
            "the service mounts the secret at /run/secrets/<name> and the "
            "job env carries OSIMFLOW_TASK_PAYLOAD_SECRET_FILE pointing at "
            "it — the remote runner reads the secret from the file — so "
            "the raw secret never appears in the service spec where it is "
            "readable via 'docker service inspect' (issue #1633). Requires "
            "OSIMFLOW_TASK_PAYLOAD_SECRET on the orchestrator with the "
            "same value for signing. See docs/secret-management.md."
        ),
    )


def kwargs_for_executor(**kwargs: Any) -> dict[str, Any]:
    """Translate the flat CLI / API kwargs into ``DockerSwarmExecutor`` kwargs (issue #1681).

    Adds the previously-missing ``docker_swarm`` wiring on the API
    side: the pre-#1681 hand-rolled API mirror omitted this executor
    entirely so the REST surface supported 9 of the CLI's 10
    executors. The shared factory now constructs it from the same
    flat kwargs the CLI consumes.
    """
    return {
        "poll_interval_s": kwargs.get("docker_swarm_poll_interval_s") or 5.0,
        "max_poll_interval_s": kwargs.get("docker_swarm_max_poll_interval_s") or 60.0,
        "image": kwargs.get("docker_swarm_image") or "nrel/openstudio:3.11.0",
        "network": kwargs.get("docker_swarm_network"),
        "submit_rps": kwargs.get("submit_rps"),
        "payload_secret": kwargs.get("docker_swarm_payload_secret"),
    }
