"""S3 result-store helpers for the CLI (``--from-s3`` / ``--detach-s3``)."""

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from osimflow import (
    AWSBatchExecutor,  # noqa: F401 — re-exported for the historical ``osimflow.__main__.<X>Executor`` patch seam (pre-#1681).
    AzureBatchExecutor,  # noqa: F401 — see above.
    DaskJobQueueExecutor,  # noqa: F401 — see above.
    DockerSwarmExecutor,  # noqa: F401 — see above.
    GoogleBatchExecutor,  # noqa: F401 — see above.
    KubernetesExecutor,  # noqa: F401 — see above.
    NomadExecutor,  # noqa: F401 — see above.
    PBSExecutor,  # noqa: F401 — see above.
    SlurmExecutor,  # noqa: F401 — see above.
)

log = logging.getLogger("osimflow.__main__")


def _s3_store(args: argparse.Namespace, scratch: Path) -> Any:
    """Build an S3CampaignStore from the ``--result-storage-*`` flags."""
    from osimflow.s3_campaign import S3CampaignStore  # noqa: PLC0415
    from osimflow.storage import build_result_storage  # noqa: PLC0415

    if not getattr(args, "result_storage_bucket", None):
        raise SystemExit("error: --from-s3 / --detach-s3 require --result-storage-bucket")
    storage = build_result_storage(
        "s3",
        args.result_storage_bucket,
        endpoint_url=getattr(args, "result_storage_endpoint", None),
        allow_insecure_endpoint=bool(getattr(args, "allow_insecure_storage_endpoint", False)),
    )
    return S3CampaignStore(storage, scratch)


def _cmd_s3(args: argparse.Namespace, action: str) -> int:
    """Handle ``list|status|download --from-s3`` (issue #1873)."""
    import json as json_mod  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from osimflow.s3_campaign import S3CampaignError  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="osimflow-s3-") as tmp:
        try:
            store = _s3_store(args, Path(tmp))
            if action == "list":
                rows = [
                    {
                        "campaign_id": r.campaign_id,
                        "samples": len(r.sample_ids),
                        "submitted_at": r.submitted_at,
                    }
                    for r in store.list_campaigns(args.prefix)
                ]
                print(json_mod.dumps(rows, indent=2))
                return 0
            cid = args.outdir.name
            if action == "status":
                print(json_mod.dumps(store.status(cid), indent=2))
                return 0
            dest = args.output_dir or args.outdir
            result = store.download(cid, dest, allow_partial=args.allow_partial)
            print(
                f"downloaded {cid}: {result['completed']}/{result['total']} samples, "
                f"{len(result['fetched'])} new, {len(result['failed'])} failed -> {dest}"
            )
            return 0
        except (S3CampaignError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1


def _detach_s3_already_submitted(args: argparse.Namespace) -> bool:
    """Idempotency guard: True (and a notice) if the handoff already exists."""
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="osimflow-s3-") as tmp:
        store = _s3_store(args, Path(tmp))
        cid = Path(args.outdir).name
        if store.handoff_exists(cid):
            print(f"campaign {cid} already submitted (handoff in S3); nothing to do")
            return True
    return False
