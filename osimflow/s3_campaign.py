"""S3-backed detached campaigns: submit, status, list, download (issue #1873).

``osimflow run --detach-s3`` submits the AWS Batch jobs, writes a handoff
record (``<campaign>/_handoff.json``) and the sweep's ``samples.json`` to the
result bucket, then exits. Workers upload each sample's output directory under
``<campaign>/work/sim/<sample_id>/`` and finish it with an empty
``_OSIMFLOW_COMPLETE`` marker, so S3 alone is the source of truth for
status. ``osimflow status|list|download --from-s3`` read that state from any
machine; ``download`` performs the KPI extraction + aggregation locally (the
"finalizer") and writes the server-style ``download_data.csv``.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import OSimFlowRuntimeError
from .storage import ResultStorage

log = logging.getLogger(__name__)

#: Empty object a worker uploads last into each sample result directory.
COMPLETE_MARKER = "_OSIMFLOW_COMPLETE"
#: Uploaded last by a worker whose step raised, after best-effort diagnostics
#: (``out.osw`` / ``run.log``). A success marker, if any, takes precedence.
FAILED_MARKER = "_OSIMFLOW_FAILED"
HANDOFF_NAME = "_handoff.json"
SAMPLES_NAME = "samples.json"
SIM_PREFIX = "work/sim"
DOWNLOAD_CSV_NAME = "download_data.csv"
HANDOFF_VERSION = 1


class S3CampaignError(OSimFlowRuntimeError):
    """Raised for missing/incomplete S3 campaign state."""


class CampaignDetached(Exception):  # noqa: N818
    """Control-flow signal: jobs were submitted and the handoff written; exit cleanly."""


@dataclass
class S3Handoff:
    """Handoff record persisted next to the campaign's S3 results."""

    campaign_id: str
    submitted_at: float
    sample_ids: list[str]
    executor: str = "aws_batch"
    openstudio_version: str = ""
    kpis: list[str] | None = None
    job_ids: dict[str, str | None] = field(default_factory=dict)
    version: int = HANDOFF_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "S3Handoff":
        data = json.loads(raw)
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


def _tmp_roundtrip_write(storage: ResultStorage, key: str, text: str, tmp_dir: Path) -> None:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    local = tmp_dir / "payload"
    local.write_text(text)
    storage.upload_file(local, key)


class S3CampaignStore:
    """Read/write detached-campaign state through a :class:`ResultStorage`.

    The storage must be rooted at the bucket (empty prefix) so keys are
    ``<campaign_id>/...``.
    """

    def __init__(self, storage: ResultStorage, scratch: Path) -> None:
        self.storage = storage
        self.scratch = scratch

    def handoff_exists(self, campaign_id: str) -> bool:
        return f"{campaign_id}/{HANDOFF_NAME}" in set(self.storage.list_results(campaign_id))

    def write_handoff(self, record: S3Handoff, samples_json: Path | None = None) -> None:
        cid = record.campaign_id
        if samples_json is not None and samples_json.is_file():
            self.storage.upload_file(samples_json, f"{cid}/{SAMPLES_NAME}")
        _tmp_roundtrip_write(self.storage, f"{cid}/{HANDOFF_NAME}", record.to_json(), self.scratch)

    def read_handoff(self, campaign_id: str) -> S3Handoff:
        local = self.scratch / f"{campaign_id}-{HANDOFF_NAME}"
        self.scratch.mkdir(parents=True, exist_ok=True)
        try:
            self.storage.download_file(f"{campaign_id}/{HANDOFF_NAME}", local)
            return S3Handoff.from_json(local.read_text())
        except (OSError, ValueError) as exc:
            raise S3CampaignError(
                f"no detached campaign {campaign_id!r} found in the result bucket "
                f"(missing {campaign_id}/{HANDOFF_NAME}); was it submitted with --detach-s3?"
            ) from exc

    def list_campaigns(self, prefix: str = "") -> list[S3Handoff]:
        """Handoff records of every detached campaign whose id starts with *prefix*."""
        records: list[S3Handoff] = []
        for key in self.storage.list_results(prefix):
            parts = key.split("/")
            if len(parts) == 2 and parts[1] == HANDOFF_NAME and parts[0].startswith(prefix):
                try:
                    records.append(self.read_handoff(parts[0]))
                except S3CampaignError:
                    log.warning("unreadable handoff for %s", parts[0], exc_info=True)
        return sorted(records, key=lambda r: r.submitted_at)

    def sample_states(self, record: S3Handoff) -> dict[str, str]:
        """Terminal state per sample: ``"complete"`` or ``"failed"`` (issue #1878).

        A success marker wins over a failure marker, so a Batch retry that
        eventually succeeds is reported as complete.
        """
        cid = record.campaign_id
        head = f"{cid}/{SIM_PREFIX}/"
        states: dict[str, str] = {}
        for key in self.storage.list_results(head):
            if not key.startswith(head):
                continue
            sid, _, name = key[len(head) :].partition("/")
            if sid not in record.sample_ids:
                continue
            if name == COMPLETE_MARKER:
                states[sid] = "complete"
            elif name == FAILED_MARKER:
                states.setdefault(sid, "failed")
        return states

    def completed_samples(self, record: S3Handoff) -> set[str]:
        """Samples with a terminal marker (succeeded or failed)."""
        return set(self.sample_states(record))

    def status(self, campaign_id: str) -> dict[str, Any]:
        record = self.read_handoff(campaign_id)
        states = self.sample_states(record)
        done = set(states)
        total = len(record.sample_ids)
        return {
            "campaign_id": campaign_id,
            "total": total,
            "completed": len(done),
            "failed": sorted(sid for sid, st in states.items() if st == "failed"),
            "pending": sorted(set(record.sample_ids) - done),
            "state": "completed" if total and len(done) == total else "running",
            "submitted_at": record.submitted_at,
        }

    def download(  # noqa: PLR0912
        self,
        campaign_id: str,
        outdir: Path,
        *,
        allow_partial: bool = False,
        include_artifacts: bool = False,
    ) -> dict[str, Any]:
        """Fetch completed samples, then aggregate + write ``download_data.csv``.

        Already-downloaded samples (local marker present) are skipped, and the
        aggregation is only redone when something new arrived or the output is
        missing, so repeated calls are idempotent.
        """
        from .server_csv import build_server_csv_frame  # noqa: PLC0415
        from .work import aggregate_results, extract_kpis  # noqa: PLC0415

        record = self.read_handoff(campaign_id)
        states = self.sample_states(record)
        done = set(states)
        if len(done) < len(record.sample_ids) and not allow_partial:
            raise S3CampaignError(
                f"campaign {campaign_id} is not complete ({len(done)}/{len(record.sample_ids)} "
                "samples); re-run later or pass --allow-partial"
            )
        outdir.mkdir(parents=True, exist_ok=True)
        identity = outdir / ".s3_campaign_id"
        if identity.is_file() and identity.read_text().strip() != campaign_id:
            raise S3CampaignError(
                f"{outdir} holds a download of campaign {identity.read_text().strip()!r}; "
                f"use a different --output-dir for {campaign_id!r}"
            )
        identity.write_text(campaign_id)
        sim_root = outdir / "work" / "sim"
        kpi_dir = outdir / "kpis"
        kpi_dir.mkdir(parents=True, exist_ok=True)
        samples_path = outdir / SAMPLES_NAME
        if not samples_path.is_file():
            try:
                self.storage.download_file(f"{campaign_id}/{SAMPLES_NAME}", samples_path)
            except OSError:
                log.warning("samples.json not available for %s", campaign_id, exc_info=True)

        fetched: list[str] = []
        for sid in sorted(done):
            sim_dir = sim_root / sid
            local_marker = sim_dir / (
                COMPLETE_MARKER if states[sid] == "complete" else FAILED_MARKER
            )
            if local_marker.is_file():
                continue
            head = f"{campaign_id}/{SIM_PREFIX}/{sid}/"
            for key in self.storage.list_results(head):
                rel = key[len(head) :]
                if not key.startswith(head) or not rel or rel in {COMPLETE_MARKER, FAILED_MARKER}:
                    continue
                if not include_artifacts and rel.startswith("run/") and rel != "run/run.log":
                    continue
                dest = sim_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                self.storage.download_file(key, dest)
            local_marker.touch()
            fetched.append(sid)

        csv_path = outdir / "aggregated_results.csv"
        if fetched or not csv_path.is_file():
            kpi_files: list[Path] = []
            sim_dirs: list[Path] = []
            for sid in record.sample_ids:
                sim_dir = sim_root / sid
                if sid in done:
                    try:
                        kpi_files.append(
                            extract_kpis(
                                sim_dir,
                                sid,
                                kpi_dir,
                                openstudio_version=record.openstudio_version or None,
                                kpis=record.kpis,
                            )
                        )
                    except Exception:
                        log.warning("KPI extraction failed for %s", sid, exc_info=True)
                else:
                    sim_dir.mkdir(parents=True, exist_ok=True)
                sim_dirs.append(sim_dir)
            aggregate_results(
                kpi_files,
                sim_dirs,
                outdir,
                samples_json=samples_path if samples_path.is_file() else None,
            )
        server_csv = outdir / DOWNLOAD_CSV_NAME
        frame = build_server_csv_frame(outdir)
        if not frame.empty:
            frame.to_csv(server_csv, index=False)
        return {
            "campaign_id": campaign_id,
            "fetched": fetched,
            "completed": len(done),
            "failed": sorted(sid for sid, st in states.items() if st == "failed"),
            "total": len(record.sample_ids),
            "server_csv": server_csv if server_csv.is_file() else None,
            "aggregated_csv": csv_path,
        }


def new_handoff(
    campaign_id: str,
    sample_ids: list[str],
    *,
    openstudio_version: str,
    kpis: list[str] | None,
    job_ids: dict[str, str | None],
    executor: str = "aws_batch",
) -> S3Handoff:
    return S3Handoff(
        campaign_id=campaign_id,
        submitted_at=time.time(),
        sample_ids=sample_ids,
        executor=executor,
        openstudio_version=openstudio_version,
        kpis=kpis,
        job_ids=job_ids,
    )
