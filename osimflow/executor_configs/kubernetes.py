"""Kubernetes executor CLI flags (issue #1575).

Owns the ``--kubernetes-*`` flags that ``osimflow run``/
``osimflow warm-cache`` register for the ``kubernetes`` executor.
Registered as the ``kubernetes`` argument hook by
``osimflow/executor_configs/__init__.py``.

Unlike most executors, Kubernetes has no dedicated ``XConfig``
dataclass: its native Job controls (``kubernetes_backoff_limit`` /
``kubernetes_ttl_seconds_after_finished`` /
``kubernetes_queue_name``, issue #997) live as flat fields on
``CampaignConfig`` and are documented in ``osimflow/config.py``;
the remaining knobs are consumed directly from the parsed CLI
namespace by ``osimflow.__main__._build_executor``.
"""

import argparse


def add_arguments(parser_group: argparse.ArgumentParser) -> None:
    """Register the ``kubernetes`` executor's ``run`` flags (issue #1575)."""
    parser_group.add_argument(
        "--kubernetes-namespace",
        default="default",
        help="Kubernetes namespace for jobs (default: default).",
    )
    parser_group.add_argument(
        "--kubernetes-poll-interval-s",
        type=float,
        default=5.0,
        help="Poll interval for Job status (seconds, default: 5.0).",
    )
    parser_group.add_argument(
        "--kubernetes-max-poll-interval-s",
        type=float,
        default=60.0,
        help="Max poll interval for Job status (seconds, default: 60.0).",
    )
    # Native Job controls (issue #997). Defaults preserve the pre-#997
    # manifest byte-for-byte: backoff_limit=0, no TTL, no extra labels.
    parser_group.add_argument(
        "--kubernetes-backoff-limit",
        type=int,
        default=0,
        help=(
            "Native Job ``backoffLimit`` (default: 0 — preserves the "
            "orchestrator-side retry semantics from ``--max-sample-retries``). "
            "Set to >0 to enable K8s-native pod retry as an alternative: "
            "the kubelet restarts the failed pod up to this many times "
            "without a resubmit round-trip through the orchestrator. "
            "Pick one mechanism, not both — running K8s-native retry "
            "and ``--max-sample-retries`` together will double-count "
            "failures. (issue #997)"
        ),
    )
    parser_group.add_argument(
        "--kubernetes-ttl-seconds-after-finished",
        type=int,
        default=None,
        help=(
            "Native Job ``ttlSecondsAfterFinished`` (default: unset — "
            "Jobs are retained in the API server until manually deleted). "
            "Set to a positive integer (seconds) to have the API server "
            "garbage-collect completed/failed Jobs after this delay, "
            "releasing the etcd footprint and pod resources across a "
            "large sweep. (issue #997)"
        ),
    )
    parser_group.add_argument(
        "--kubernetes-queue-name",
        default=None,
        help=(
            "Kueue ClusterQueue name applied as the "
            "``kueue.x-k8s.io/queue-name`` label on Job metadata (issue #997). "
            "When set, Kueue manages the Job through suspend/resume and "
            "honors fair-sharing, priority, and preemption across the "
            "cluster. Inert on clusters without Kueue installed. "
            "Example: --kubernetes-queue-name team-a-cpu."
        ),
    )
