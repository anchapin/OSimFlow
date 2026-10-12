"""Coordinator handoff helpers: payload, idempotency key, status and result download."""

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from osimflow import (
    AWSBatchExecutor,  # noqa: F401 — re-exported for the historical ``osimflow.__main__.<X>Executor`` patch seam (pre-#1681).
    AzureBatchExecutor,  # noqa: F401 — see above.
    CampaignConfig,
    DaskJobQueueExecutor,  # noqa: F401 — see above.
    DockerSwarmExecutor,  # noqa: F401 — see above.
    GoogleBatchExecutor,  # noqa: F401 — see above.
    KubernetesExecutor,  # noqa: F401 — see above.
    NomadExecutor,  # noqa: F401 — see above.
    PBSExecutor,  # noqa: F401 — see above.
    SlurmExecutor,  # noqa: F401 — see above.
)
from osimflow.handoff_record import (
    HandoffRecord,
)

log = logging.getLogger("osimflow.__main__")


def _coordinator_status_url(coordinator_url: str, campaign_id: str) -> str:
    """Build the absolute campaign-status URL for a Coordinator campaign."""
    return f"{coordinator_url.rstrip('/')}/api/v1/coordinator/campaigns/{campaign_id}"


def _build_coordinator_payload(args: argparse.Namespace, cfg: CampaignConfig) -> dict[str, Any]:
    """Build the JSON payload posted to ``POST /coordinator/handoff``.

    Factored out so the payload shape and the idempotency-key derivation share
    one source of truth.
    """
    return {
        "name": str(cfg.outdir.name) if cfg.outdir else f"campaign-{int(time.time())}",
        "n_samples": cfg.n_samples,
        "executor": args.executor or "local",
        "openstudio_version": cfg.openstudio_version or "3.10.0",
        "algorithm": cfg.algorithm,
        "max_generations": cfg.max_generations,
        "input_variables": str(cfg.input_variables) if cfg.input_variables else None,
        "template_sim_package": str(cfg.template_sim_package) if cfg.template_sim_package else None,
        "custom_apply_script": args.custom_apply_script,
        "custom_kpi_extractor": args.custom_kpi_extractor,
        "archive_intermediates": cfg.archive_intermediates,
        "result_storage_backend": cfg.result_storage_backend,
        "result_storage_bucket": cfg.result_storage_bucket,
        "extra": {
            "slurm_partition": getattr(args, "slurm_partition", None),
            "aws_batch_queue": getattr(args, "aws_batch_queue", None),
            "nomad_datacentre": getattr(args, "nomad_datacentre", None),
        },
    }


def _coordinator_idempotency_key(payload: dict[str, Any], outdir: Path | None) -> str:
    """Derive a deterministic ``Idempotency-Key`` for a handoff.

    Hashing the resolved payload + the outdir means re-running the *same*
    ``osimflow run --detach ...`` command produces the *same* key, so a retry
    after a lost HTTP response reuses the campaign the Coordinator already
    created instead of minting a second one (issue #630). Changing any
    campaign parameter (or the outdir) yields a different key -> a new
    campaign, which is the desired behaviour.
    """
    import hashlib  # noqa: PLC0415
    import json as json_mod  # noqa: PLC0415

    digest = hashlib.sha256()
    digest.update(json_mod.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
    if outdir is not None:
        digest.update(str(outdir.resolve()).encode("utf-8"))
    return f"osimflow-{digest.hexdigest()[:32]}"


def _print_coordinator_status(record: HandoffRecord) -> int:
    """Poll the Coordinator for live status of a detached campaign.

    Reads the ``campaign_id`` + Coordinator URL from the local handoff record
    (so it works from a fresh shell / rebooted machine) and prints the live
    status returned by ``GET /api/v1/coordinator/campaigns/{id}``.
    """
    status_url = record.status_url or _coordinator_status_url(
        record.coordinator_url, record.campaign_id
    )
    try:
        response = httpx.get(status_url, timeout=15.0)
    except httpx.RequestError as exc:
        print(
            f"error: could not reach Coordinator at {record.coordinator_url} to "
            f"fetch status: {exc}",
            file=sys.stderr,
        )
        return 1

    if response.status_code == 404:
        print(
            f"error: Coordinator has no campaign {record.campaign_id} (HTTP 404). "
            "It may have been deleted, or the Coordinator was reset.",
            file=sys.stderr,
        )
        return 1
    if response.status_code >= 400:
        print(
            f"error: Coordinator returned HTTP {response.status_code} for "
            f"campaign {record.campaign_id}: {response.text}",
            file=sys.stderr,
        )
        return 1

    data = response.json()
    print("Coordinator-backed campaign (detached)")
    print(f"  Campaign ID:  {data.get('campaign_id', record.campaign_id)}")
    print(f"  Name:         {data.get('name', 'unknown')}")
    print(f"  Status:       {data.get('status', 'unknown')}")
    print(f"  Executor:     {data.get('executor', 'unknown')}")
    print(f"  OpenStudio:   {data.get('openstudio_version', 'unknown')}")
    print(f"  Samples:      {data.get('n_samples', 'unknown')}")
    created = data.get("created_at")
    if isinstance(created, (int, float)) and created:
        print(
            f"  Created:      {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(created)))}"
        )
    updated = data.get("updated_at")
    if isinstance(updated, (int, float)) and updated:
        print(
            f"  Updated:      {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(updated)))}"
        )
    print(f"  Status URL:   {status_url}")
    print("  Download:     osimflow download <outdir>")
    return 0


def _download_coordinator_results(
    record: HandoffRecord, output_dir: Path, *, http_get: Any = None
) -> int:
    """Download ONLY the final aggregated artifacts for a detached campaign.

    Fetches ``GET /api/v1/coordinator/campaigns/{id}/results`` and downloads
    the aggregated-results CSV via the Coordinator-provided presigned URL —
    per-sample bytes are deliberately NOT downloaded (issue #630).

    ``http_get`` is an optional injectable callable ``(url, *, timeout,
    follow_redirects) -> response`` used only by tests; production callers
    leave it as ``None`` to use ``httpx.get``.
    """
    results_url = (
        f"{record.coordinator_url.rstrip('/')}"
        f"/api/v1/coordinator/campaigns/{record.campaign_id}/results"
    )
    fetch = http_get if http_get is not None else httpx.get

    try:
        results_response = fetch(results_url, timeout=15.0, follow_redirects=True)
    except httpx.RequestError as exc:
        print(
            f"error: could not reach Coordinator at {record.coordinator_url} to "
            f"list results: {exc}",
            file=sys.stderr,
        )
        return 1
    if results_response.status_code >= 400:
        print(
            f"error: Coordinator returned HTTP {results_response.status_code} "
            f"listing results: {results_response.text}",
            file=sys.stderr,
        )
        return 1

    body = results_response.json()
    result_status = str(body.get("status", "unknown"))
    presigned = body.get("aggregated_results_url")
    agg_key = body.get("aggregated_results_key")

    if not presigned:
        # Nothing to download yet (still running, or aggregation produced no
        # aggregated object). Give the user an actionable, status-aware hint.
        print(
            f"Campaign {record.campaign_id} has no aggregated results to "
            f"download yet (status: {result_status})."
        )
        if result_status in ("pending", "running", "aggregating"):
            print("The campaign is not finished. Re-run this command once it reaches `complete`.")
        elif agg_key:
            print(
                f"An aggregated key is referenced ({agg_key}) but the "
                "Coordinator did not return a presigned URL; check the "
                "Coordinator's S3 permissions."
            )
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / "aggregated_results.csv"
    try:
        file_response = fetch(presigned, timeout=120.0, follow_redirects=True)
    except httpx.RequestError as exc:
        print(f"error: failed to download aggregated results: {exc}", file=sys.stderr)
        return 1
    if file_response.status_code >= 400:
        print(
            f"error: aggregated-results download failed "
            f"(HTTP {file_response.status_code}): {file_response.text}",
            file=sys.stderr,
        )
        return 1

    with open(dest, "wb") as fh:
        fh.write(file_response.content)
    log.info("Downloaded aggregated results for campaign %s -> %s", record.campaign_id, dest)

    print(f"\nDownloaded aggregated results for campaign '{record.campaign_id}' to:")
    print(f"  {dest}")
    print(
        "  (Per-sample bytes were intentionally not downloaded. Run the "
        "campaign locally or query the Coordinator's per-sample API to get them.)"
    )
    return 0
