"""PBS executor CLI flags (issue #1575).

Owns the ``--pbs-*`` flags that ``osimflow run``/``osimflow warm-cache``
register for the ``pbs`` executor. Registered as the ``pbs`` argument
hook by ``osimflow/executor_configs/__init__.py``. PBS needs no
``XConfig`` dataclass — every knob is consumed directly from the
parsed CLI namespace by ``osimflow.__main__._build_executor``.
"""

import argparse
from typing import Any


def add_arguments(parser_group: argparse.ArgumentParser) -> None:
    """Register the ``pbs`` executor's ``run`` flags (issue #1575)."""
    parser_group.add_argument(
        "--pbs-server",
        default=None,
        help=(
            "PBS server/cluster address (e.g. pbsserver). "
            "Defaults to the PBS_DEFAULT env var or system default."
        ),
    )
    parser_group.add_argument(
        "--pbs-queue",
        default=None,
        help="PBS queue to submit jobs to (e.g. batch).",
    )
    parser_group.add_argument(
        "--pbs-real",
        action="store_true",
        help="Submit to real PBS (default: debug mode runs locally).",
    )


def kwargs_for_executor(**kwargs: Any) -> dict[str, Any]:
    """Translate the flat CLI / API kwargs into ``PBSExecutor`` kwargs (issue #1681)."""
    return {
        "server": kwargs.get("pbs_server"),
        "queue": kwargs.get("pbs_queue"),
        "debug": not bool(kwargs.get("pbs_real", False)),
        "submit_rps": kwargs.get("submit_rps"),
    }
