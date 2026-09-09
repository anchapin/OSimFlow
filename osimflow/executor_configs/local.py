"""Local executor configuration + CLI flags (issue #1575).

Owns the ``LocalConfig`` dataclass and the ``--max-workers`` flag that
``osimflow run``/``osimflow warm-cache`` register for the ``local``
executor. Registered as the ``local`` argument hook by
``osimflow/executor_configs/__init__.py``.
"""

import argparse
import dataclasses
from typing import Any


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


def kwargs_for_executor(**kwargs: Any) -> dict[str, Any]:
    """Translate the flat CLI / API kwargs into ``LocalExecutor`` kwargs (issue #1681).

    Honors the campaign-level ``max_concurrent_samples`` quota when
    present in the payload (the CLI computes it from
    ``--resource-quota``; the API surfaces the campaign quota through
    the same field name on ``CampaignCreateRequest``).
    """
    payload: dict[str, Any] = {
        "max_workers": kwargs.get("max_workers"),
        "max_concurrent_samples": kwargs.get("max_concurrent_samples"),
        "submit_rps": kwargs.get("submit_rps"),
    }
    return {k: v for k, v in payload.items() if v is not None}
