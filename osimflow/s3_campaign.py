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

import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ._work_scripts.aggregate_results import extract_failure
from .errors import OSimFlowError, OSimFlowRuntimeError
from .osw_results import OUT_OSW_NAME, has_valid_osw_completion
from .storage import ResultStorage

log = logging.getLogger(__name__)

#: Empty object a worker uploads last into each sample result directory.
COMPLETE_MARKER = "_OSIMFLOW_COMPLETE"
#: Uploaded last by a worker whose step raised, after best-effort diagnostics
#: (``out.osw`` / ``run.log``). A success marker, if any, takes precedence.
FAILED_MARKER = "_OSIMFLOW_FAILED"
#: Content a retry attempt writes over a stale ``_OSIMFLOW_FAILED`` (no delete API).
RETRYING_STATE = "retrying"
HANDOFF_NAME = "_handoff.json"
SAMPLES_NAME = "samples.json"
SIM_PREFIX = "work/sim"
DOWNLOAD_CSV_NAME = "download_data.csv"
HANDOFF_VERSION = 1
CLAIMS_PREFIX = "_claims"
JOBS_PREFIX = "_jobs"
CLAIM_SUBMITTING = "submitting"
CLAIM_SUBMITTED = "submitted"
#: Claim lease; the submitter renews it as it submits, a crashed one lets it lapse.
DEFAULT_CLAIM_LEASE_S = 900.0
CLAIM_ACQUIRED = "acquired"
CLAIM_ALREADY_SUBMITTED = "already_submitted"
CLAIM_IN_PROGRESS = "in_progress"
BATCH_DESCRIBE_JOBS_LIMIT = 100


class S3CampaignError(OSimFlowRuntimeError):
    """Raised for missing/incomplete S3 campaign state."""


class CampaignDetached(OSimFlowError):  # noqa: N818
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
        raw_job_ids = known.get("job_ids")
        if isinstance(raw_job_ids, dict):
            known["job_ids"] = {
                str(k): (str(v) if v is not None else None) for k, v in raw_job_ids.items()
            }
        else:
            known["job_ids"] = {}
        return cls(**known)


def batch_job_name(campaign_id: str, sample_id: str) -> str:
    """Deterministic AWS Batch job name for a campaign sample (issue #1881).

    Batch names allow ``[A-Za-z0-9_-]`` up to 128 chars; when clipped, a hash of
    the full pair keeps the name unique and stable.
    """
    raw = f"osimflow-{campaign_id}-{sample_id}"
    clean = re.sub(r"[^A-Za-z0-9_-]", "-", raw)
    if clean == raw and len(raw) <= 128:
        return raw
    digest = hashlib.sha256(f"{campaign_id}\0{sample_id}".encode()).hexdigest()[:12]
    return f"{clean[:115]}-{digest}"


@dataclass
class SubmitClaim:
    """Conditional claim on a detached submit (issue #1881).

    Claims are generation-numbered objects ``<campaign>/_claims/<NNNNNN>.json``
    created with an atomic create-if-absent, so exactly one process owns a
    generation. Taking over a lapsed lease means atomically creating the next
    generation; a superseded owner notices on its next renew and aborts.
    """

    campaign_id: str
    owner: str
    generation: int
    state: str
    claimed_at: float
    lease_expires_at: float
    version: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "SubmitClaim":
        data = json.loads(raw)
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    @property
    def resumed(self) -> bool:
        """True when this claim took over an earlier, unfinished submission."""
        return self.generation > 1


def _tmp_roundtrip_write(storage: ResultStorage, key: str, text: str, tmp_dir: Path) -> None:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    local = tmp_dir / "payload"
    local.write_text(text)
    storage.upload_file(local, key)


def _format_batch_failure_reason(job_id: str, job: dict[str, Any]) -> str:
    """Format a single-line failure diagnostic from an AWS Batch ``describe_jobs`` entry."""
    resolved_id = str(job.get("jobId") or job_id)
    container = job.get("container")
    if not isinstance(container, dict):
        container = {}
    attempts = job.get("attempts")
    if not container and isinstance(attempts, list) and attempts:
        last_attempt = attempts[-1]
        if isinstance(last_attempt, dict) and isinstance(last_attempt.get("container"), dict):
            container = last_attempt["container"]
    exit_code = container.get("exitCode")
    status_reason = " ".join(str(job.get("statusReason") or "").split())
    container_reason = " ".join(str(container.get("reason") or "").split())
    header = f"AWS Batch job {resolved_id} FAILED"
    if exit_code is not None:
        header = f"{header} (exit_code={exit_code})"
    reasons: list[str] = []
    if container_reason:
        reasons.append(container_reason)
    if status_reason and status_reason not in container_reason:
        if container_reason and container_reason in status_reason:
            reasons = [status_reason]
        else:
            reasons.append(status_reason)
    if reasons:
        return f"{header}: {'; '.join(reasons)}"
    return header


class S3CampaignStore:
    """Read/write detached-campaign state through a :class:`ResultStorage`.

    The storage must be rooted at the bucket (empty prefix) so keys are
    ``<campaign_id>/...``.
    """

    def __init__(
        self,
        storage: ResultStorage,
        scratch: Path,
        *,
        batch_client: Any | None = None,
    ) -> None:
        self.storage = storage
        self.scratch = scratch
        self._batch_client = batch_client

    def _get_batch_client(self) -> Any | None:
        if self._batch_client is not None:
            return self._batch_client
        try:
            import boto3  # noqa: PLC0415
            from botocore.exceptions import (  # noqa: PLC0415
                BotoCoreError,
                ClientError,
                NoCredentialsError,
                NoRegionError,
            )
        except ImportError:
            log.warning(
                "boto3 is not installed; skipping AWS Batch job reconciliation",
                exc_info=True,
            )
            return None
        try:
            region = (
                os.environ.get("AWS_REGION")
                or os.environ.get("AWS_DEFAULT_REGION")
                or boto3.Session().region_name
            )
            self._batch_client = (
                boto3.client("batch", region_name=region) if region else boto3.client("batch")
            )
            return self._batch_client
        except (NoRegionError, NoCredentialsError, BotoCoreError, ClientError, OSError):
            log.warning(
                "could not initialize AWS Batch client; skipping job reconciliation",
                exc_info=True,
            )
            return None

    @staticmethod
    def _claim_key(campaign_id: str, generation: int) -> str:
        return f"{campaign_id}/{CLAIMS_PREFIX}/{generation:06d}.json"

    def _claim_generations(self, campaign_id: str) -> list[int]:
        head = f"{campaign_id}/{CLAIMS_PREFIX}/"
        gens: list[int] = []
        for key in self.storage.list_results(head):
            stem = key[len(head) :].removesuffix(".json")
            if key.startswith(head) and stem.isdigit():
                gens.append(int(stem))
        return sorted(gens)

    def _read_claim(self, campaign_id: str, generation: int) -> SubmitClaim:
        self.scratch.mkdir(parents=True, exist_ok=True)
        local = self.scratch / f"claim-{generation}-{uuid.uuid4().hex}.json"
        try:
            self.storage.download_file(self._claim_key(campaign_id, generation), local)
            return SubmitClaim.from_json(local.read_text())
        finally:
            local.unlink(missing_ok=True)

    def claim_submission(
        self, campaign_id: str, *, lease_s: float = DEFAULT_CLAIM_LEASE_S
    ) -> tuple[str, SubmitClaim | None]:
        """Try to become the single submitter of *campaign_id* (issue #1881).

        Returns ``(outcome, claim)`` where outcome is ``"acquired"`` (claim
        returned), ``"already_submitted"`` or ``"in_progress"`` (another live
        submitter holds the lease, or this call lost a takeover race).
        """
        if self.handoff_exists(campaign_id):
            return CLAIM_ALREADY_SUBMITTED, None
        gens = self._claim_generations(campaign_id)
        latest = gens[-1] if gens else 0
        now = time.time()
        if latest:
            current = self._read_claim(campaign_id, latest)
            if current.state == CLAIM_SUBMITTED:
                return CLAIM_ALREADY_SUBMITTED, None
            if current.lease_expires_at > now:
                return CLAIM_IN_PROGRESS, None
        claim = SubmitClaim(
            campaign_id=campaign_id,
            owner=uuid.uuid4().hex,
            generation=latest + 1,
            state=CLAIM_SUBMITTING,
            claimed_at=now,
            lease_expires_at=now + lease_s,
        )
        if not self.storage.put_if_absent(
            self._claim_key(campaign_id, claim.generation), claim.to_json().encode()
        ):
            return CLAIM_IN_PROGRESS, None
        return CLAIM_ACQUIRED, claim

    def _write_claim(self, claim: SubmitClaim) -> None:
        gens = self._claim_generations(claim.campaign_id)
        if gens and gens[-1] != claim.generation:
            raise S3CampaignError(
                f"submit claim for {claim.campaign_id} was taken over by generation "
                f"{gens[-1]}; this submitter (generation {claim.generation}) must stop"
            )
        _tmp_roundtrip_write(
            self.storage,
            self._claim_key(claim.campaign_id, claim.generation),
            claim.to_json(),
            self.scratch,
        )

    def renew_claim(self, claim: SubmitClaim, lease_s: float = DEFAULT_CLAIM_LEASE_S) -> None:
        """Extend the lease; raises :class:`S3CampaignError` if superseded."""
        claim.lease_expires_at = time.time() + lease_s
        self._write_claim(claim)

    def finish_claim(self, claim: SubmitClaim) -> None:
        """Mark the claim ``submitted`` (after the handoff record is durable)."""
        claim.state = CLAIM_SUBMITTED
        self._write_claim(claim)

    def release_claim(self, claim: SubmitClaim) -> None:
        """Expire the lease now so a retry can resume immediately (best effort)."""
        if claim.state == CLAIM_SUBMITTED:
            return
        claim.lease_expires_at = 0.0
        try:
            self._write_claim(claim)
        except (OSError, S3CampaignError):
            log.warning("could not release submit claim for %s", claim.campaign_id, exc_info=True)

    def record_job(self, campaign_id: str, sample_id: str, job_id: str, job_name: str) -> None:
        """Durably record a submitted job; the first record per sample wins."""
        body = json.dumps(
            {"sample_id": sample_id, "job_id": job_id, "job_name": job_name, "at": time.time()}
        )
        self.storage.put_if_absent(f"{campaign_id}/{JOBS_PREFIX}/{sample_id}.json", body.encode())

    def recorded_jobs(self, campaign_id: str) -> dict[str, str]:
        """``sample_id -> job_id`` for every job a previous submitter recorded."""
        head = f"{campaign_id}/{JOBS_PREFIX}/"
        jobs: dict[str, str] = {}
        self.scratch.mkdir(parents=True, exist_ok=True)
        for key in self.storage.list_results(head):
            if not key.startswith(head) or not key.endswith(".json"):
                continue
            local = self.scratch / "job.json"
            self.storage.download_file(key, local)
            data = json.loads(local.read_text())
            jobs[str(data["sample_id"])] = str(data["job_id"])
        return jobs

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

    def _is_retrying(self, failed_marker_key: str) -> bool:
        local = self.scratch / "failed-marker"
        self.scratch.mkdir(parents=True, exist_ok=True)
        try:
            self.storage.download_file(failed_marker_key, local)
            return local.read_text().strip() == RETRYING_STATE
        except OSError:
            return False

    def _pending_batch_jobs(
        self, record: S3Handoff, states: dict[str, str]
    ) -> dict[str, list[str]]:
        pending_sids = [
            sid for sid in record.sample_ids if states.get(sid) not in {"complete", "failed"}
        ]
        if not pending_sids:
            return {}
        cid = record.campaign_id
        handoff_jobs = record.job_ids if isinstance(record.job_ids, dict) else {}
        fallback_jobs: dict[str, str] | None = None
        job_to_sids: dict[str, list[str]] = {}
        for sid in pending_sids:
            raw_jid = handoff_jobs.get(sid)
            jid = str(raw_jid).strip() if raw_jid is not None else ""
            if not jid:
                if fallback_jobs is None:
                    try:
                        fallback_jobs = self.recorded_jobs(cid)
                    except (OSError, ValueError):
                        log.warning(
                            "could not read recorded jobs for %s during reconciliation",
                            cid,
                            exc_info=True,
                        )
                        fallback_jobs = {}
                jid = fallback_jobs.get(sid, "").strip()
            if jid:
                job_to_sids.setdefault(jid, []).append(sid)
        return job_to_sids

    def _describe_batch_jobs(
        self, campaign_id: str, job_ids: list[str]
    ) -> dict[str, dict[str, Any]] | None:
        client = self._get_batch_client()
        if client is None:
            return None
        described_by_id: dict[str, dict[str, Any]] = {}
        for i in range(0, len(job_ids), BATCH_DESCRIBE_JOBS_LIMIT):
            chunk = job_ids[i : i + BATCH_DESCRIBE_JOBS_LIMIT]
            try:
                response = client.describe_jobs(jobs=chunk)
            except Exception:
                log.warning(
                    "describe_jobs failed while reconciling pending samples for %s",
                    campaign_id,
                    exc_info=True,
                )
                return None
            jobs_list = response.get("jobs", []) if isinstance(response, dict) else []
            for job in jobs_list:
                if isinstance(job, dict) and job.get("jobId"):
                    described_by_id[str(job["jobId"])] = job
        return described_by_id

    def _mark_batch_sample_failed(
        self, campaign_id: str, sample_id: str, reason: str, states: dict[str, str]
    ) -> None:
        sample_head = f"{campaign_id}/{SIM_PREFIX}/{sample_id}/"
        complete_key = f"{sample_head}{COMPLETE_MARKER}"
        try:
            sample_keys = set(self.storage.list_results(sample_head))
        except OSError:
            log.warning(
                "could not re-check S3 completion marker for %s/%s",
                campaign_id,
                sample_id,
                exc_info=True,
            )
            sample_keys = set()
        if complete_key in sample_keys:
            states[sample_id] = "complete"
            return
        failed_key = f"{sample_head}{FAILED_MARKER}"
        try:
            _tmp_roundtrip_write(self.storage, failed_key, f"{reason}\n", self.scratch)
        except OSError:
            log.warning(
                "could not persist %s for %s/%s",
                FAILED_MARKER,
                campaign_id,
                sample_id,
                exc_info=True,
            )
        states[sample_id] = "failed"

    def _reconcile_batch_failures(self, record: S3Handoff, states: dict[str, str]) -> None:
        """Reconcile pending ``aws_batch`` samples against ``describe_jobs`` (issue #2058)."""
        if record.executor != "aws_batch":
            return
        job_to_sids = self._pending_batch_jobs(record, states)
        if not job_to_sids:
            return
        described_by_id = self._describe_batch_jobs(record.campaign_id, list(job_to_sids))
        if described_by_id is None:
            return
        for jid, sids in job_to_sids.items():
            job = described_by_id.get(jid)
            if not job or job.get("status") != "FAILED":
                continue
            reason = _format_batch_failure_reason(jid, job)
            for sid in sids:
                self._mark_batch_sample_failed(record.campaign_id, sid, reason, states)

    def sample_states(
        self,
        record: S3Handoff | str | None = None,
        *,
        campaign_id: str | None = None,
    ) -> dict[str, str]:
        """Terminal state per sample: ``"complete"`` or ``"failed"`` (issue #1878, #2058).

        A success marker wins over a failure marker, so a Batch retry that
        eventually succeeds is reported as complete. When *record* uses the
        ``aws_batch`` executor, pending samples without an S3 marker are
        reconciled against AWS Batch ``describe_jobs``.
        """
        if isinstance(record, str):
            record = self.read_handoff(record)
        elif record is None:
            if not campaign_id:
                raise ValueError("sample_states requires either record or campaign_id")
            record = self.read_handoff(campaign_id)
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
            elif name == FAILED_MARKER and not self._is_retrying(key):
                states.setdefault(sid, "failed")
        self._reconcile_batch_failures(record, states)
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
            is_complete = states[sid] == "complete"
            local_marker = sim_dir / (COMPLETE_MARKER if is_complete else FAILED_MARKER)
            if local_marker.is_file():
                continue
            head = f"{campaign_id}/{SIM_PREFIX}/{sid}/"
            for key in self.storage.list_results(head):
                rel = key[len(head) :]
                if not key.startswith(head) or not rel or rel == COMPLETE_MARKER:
                    continue
                if is_complete and rel == FAILED_MARKER:
                    continue
                if not include_artifacts and rel.startswith("run/") and rel != "run/run.log":
                    continue
                dest = sim_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                self.storage.download_file(key, dest)
            if not local_marker.is_file():
                local_marker.parent.mkdir(parents=True, exist_ok=True)
                local_marker.touch()
            fetched.append(sid)

        csv_path = outdir / "aggregated_results.csv"
        if fetched or not csv_path.is_file():
            kpi_files: list[Path] = []
            sim_dirs: list[Path] = []
            for sid in record.sample_ids:
                if sid not in done:
                    continue
                sim_dir = sim_root / sid
                if (
                    states.get(sid) == "complete"
                    and (
                        (sim_dir / "eplusout.sql").is_file()
                        or has_valid_osw_completion(sim_dir / OUT_OSW_NAME)
                    )
                    and extract_failure(sim_dir) is None
                ):
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
