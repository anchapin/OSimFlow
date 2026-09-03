"""Local executor configuration + CLI flags (issue #1575).

Owns the ``LocalConfig`` dataclass and the ``--max-workers`` flag that
``osimflow run``/``osimflow warm-cache`` register for the ``local``
executor. Registered as the ``local`` argument hook by
``osimflow/executor_configs/__init__.py``.
"""

import argparse
import dataclasses


@dataclasses.dataclass(frozen=True)
class LocalConfig:
    """Local executor configuration.

    Attributes
    ----------
    max_workers
        Maximum number of parallel workers (stored separately, accessed
        via CLI --max-workers, not this config).
    """

    max_workers: int = 1


def add_arguments(parser_group: argparse.ArgumentParser) -> None:
    """Register the ``local`` executor's ``run`` flags (issue #1575)."""
    parser_group.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="Local executor parallelism",
    )
