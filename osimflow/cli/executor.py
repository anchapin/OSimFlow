"""Executor construction and BYOS audit-logger helpers for the CLI."""

import argparse
import logging
import warnings
from pathlib import Path

from osimflow import (
    AWSBatchExecutor,  # noqa: F401 — re-exported for the historical ``osimflow.__main__.<X>Executor`` patch seam (pre-#1681).
    AzureBatchExecutor,  # noqa: F401 — see above.
    BaseExecutor,
    DaskJobQueueExecutor,  # noqa: F401 — see above.
    DockerSwarmExecutor,  # noqa: F401 — see above.
    GoogleBatchExecutor,  # noqa: F401 — see above.
    KubernetesExecutor,  # noqa: F401 — see above.
    NomadExecutor,  # noqa: F401 — see above.
    PBSExecutor,  # noqa: F401 — see above.
    SlurmExecutor,  # noqa: F401 — see above.
)
from osimflow.audit import AuditLogger
from osimflow.cli.parser import _extract_max_concurrent_samples

log = logging.getLogger("osimflow.__main__")


def _byos_audit_logger(args: argparse.Namespace) -> AuditLogger | None:
    """Audit logger bound to the campaign outdir for BYOS events (#1399).

    Returns ``None`` when no outdir is resolvable (e.g. ``osimflow serve``
    code paths that construct their own logger); callers treat ``None``
    as "no audit sink configured".
    """
    outdir_raw = getattr(args, "outdir", None)
    if not outdir_raw:
        return None
    return AuditLogger(Path(outdir_raw))


#: Executors whose jobs pull the OpenStudio image from a managed cloud
#: registry at campaign scale. Running one of these without
#: ``--container-digest`` triggers the mutable-tag supply-chain warning
#: (issue #1320).
_CLOUD_EXECUTORS = frozenset({"aws_batch", "azure_batch", "google_batch"})


def _warn_if_mutable_tag(executor_name: str, container_digest: str | None) -> None:
    """Warn when a cloud executor would pull by mutable tag (issue #1320).

    A compromised or backdoored registry tag update would affect every
    active campaign. Digest pinning is the documented production default
    (docs/container-image-strategy.md); this warning makes skipping it a
    conscious decision rather than an oversight.
    """
    if container_digest is not None:
        return
    if executor_name not in _CLOUD_EXECUTORS:
        return
    warnings.warn(
        f"--container-digest is NOT set; executor '{executor_name}' will pull "
        "nrel/openstudio:<version> by MUTABLE tag from the registry. A compromised "
        "or backdoored tag update would affect this campaign (supply-chain risk). "
        "Pin a content-addressed digest for production: "
        "`docker inspect --format '{{index .RepoDigests 0}}' nrel/openstudio:<version>` "
        "and pass it via --container-digest. See docs/container-image-strategy.md.",
        UserWarning,
        stacklevel=2,
    )


def _build_executor(args: argparse.Namespace) -> BaseExecutor:  # noqa: PLR0911, PLR0912
    """Dispatch to the correct executor based on ``args.executor``.

    Issue #1681: the per-executor ``if args.executor == "..."`` chain
    used to live in both this function and
    ``osimflow.api.campaigns._build_executor_from_request``. The two
    copies silently drifted (the API mirror omitted ``docker_swarm``)
    so the REST surface supported 9 of the CLI's 10 executors. The
    CLI now forwards its parsed namespace into the shared
    :func:`osimflow.executor_configs.build_executor` factory; the
    REST API does the same with its request body. Per-executor
    kwargs translation lives in each module under
    :mod:`osimflow.executor_configs`.

    The campaign-level ``max_concurrent_samples`` quota is the only
    CLI-specific derivation (extracted from ``--resource-quota``),
    so it is computed here and merged into the flat kwargs payload
    the factory consumes — preserving the pre-#1681 behaviour for
    every substrate.
    """
    payload = vars(args).copy()
    payload["max_concurrent_samples"] = _extract_max_concurrent_samples(
        getattr(args, "resource_quota", None)
    )
    from osimflow.executor_configs import build_executor  # noqa: PLC0415

    return build_executor(args.executor, **payload)
