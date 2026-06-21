"""Per-sample manifest construction and atomic publishing (issue #625).

Each worker pushes its KPIs plus an atomic ``_manifest.json`` completion
marker directly to object storage via the existing :class:`ResultStorage`
backends (see ``osimflow/storage.py``).  No result bytes return to the
submitting host.

The atomic-write strategy is intentionally backend-aware (see
:func:`write_manifest_atomically`):

* **LocalStorage** — write-temp-then-:func:`os.replace` (atomic on POSIX).
* **S3 / GCS / Azure Blob Storage** — a single object PUT is atomic in every
  major object store, so the manifest is staged through a local temp file and
  uploaded as the *final* operation (after ``kpis.json`` and any archived
  ``eplusout.sql``).  Its appearance is the durability fence that signals
  "this sample is complete".

Manifest schema — contract §3.1
-------------------------------

.. note::

   The referenced contract file
   ``.agents/skills/_shared/api-contracts/coordinator-and-result-streaming.md``
   was **not present** in the worktree for this change.  The fields below are
   taken verbatim from issue #625's acceptance criteria and cross-checked
   against the existing Coordinator PATCH endpoint
   (``osimflow/api/coordinator.py:update_coordinator_campaign_status``).

==================  ==================================================
Field               Meaning
==================  ==================================================
``sample_id``       The sample identifier, e.g. ``"0001"``.
``index``           Zero-based sample index within the campaign.
``status``          ``"completed"`` on success, ``"failed"`` otherwise.
``kpis_key``        Remote key of the uploaded ``kpis.json`` (or ``null``).
``exit_code``       Worker exit code (``0`` on success).
``first_severe_error``  First ``  * Severe`` line from ``eplusout.err``
                        (PRD §6 #4) or ``null``.
``finished_at``     Unix epoch seconds when the sample finished.
==================  ==================================================
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

if TYPE_CHECKING:
    from .storage import ResultStorage

log = logging.getLogger("osimflow.manifest")

#: Regex for the first EnergyPlus ``  * Severe`` line (PRD §6 #4).
#: Two leading spaces, an asterisk, whitespace, then the word ``Severe``.
_SEVERE_RE = re.compile(r"^[ \t]{2}\*+[ \t]+Severe[^\n]*", re.MULTILINE)

#: The §3.1 manifest fields, in canonical (emission) order.
MANIFEST_FIELDS: tuple[str, ...] = (
    "sample_id",
    "index",
    "status",
    "kpis_key",
    "exit_code",
    "first_severe_error",
    "finished_at",
)


def first_severe_error(err_path: Path) -> str | None:
    """Return the first ``  * Severe`` line from ``eplusout.err``, or ``None``.

    Mirrors the reference implementation documented in
    ``docs/measure-runner-guide.md`` §2.3 and the pattern used by
    ``bin/aggregate_results.py`` (PRD §6 #4, AGENTS.md gotcha #4).

    Returns ``None`` when the file is absent or unreadable, or when it
    contains no Severe lines.
    """
    if not err_path.exists():
        return None
    try:
        text = err_path.read_text(errors="replace")
    except OSError as exc:
        log.warning("first_severe_error: cannot read %s: %s", err_path, exc)
        return None
    match = _SEVERE_RE.search(text)
    return match.group(0).strip() if match is not None else None


def build_manifest(
    *,
    sample_id: str,
    index: int,
    status: str,
    kpis_key: str | None,
    exit_code: int,
    first_severe_error: str | None,
    finished_at: float | None = None,
) -> dict[str, Any]:
    """Build the §3.1 manifest dict with fields in canonical order.

    ``finished_at`` defaults to :func:`time.time` when omitted.
    """
    return {
        "sample_id": sample_id,
        "index": index,
        "status": status,
        "kpis_key": kpis_key,
        "exit_code": exit_code,
        "first_severe_error": first_severe_error,
        "finished_at": time.time() if finished_at is None else float(finished_at),
    }


def write_manifest_atomically(
    storage: ResultStorage,
    remote_key: str,
    manifest: dict[str, Any],
    *,
    local_tmp_dir: Path,
) -> None:
    """Write *manifest* to *storage* at *remote_key* atomically.

    The manifest is serialised to a :func:`tempfile.NamedTemporaryFile` in
    *local_tmp_dir* (``fsync``'d before hand-off) and then handed to the
    backend via :meth:`ResultStorage.upload_file`.  Because the
    :class:`ResultStorage` ABC only exposes path-based uploads, the temp file
    is the staging area for every backend; the ABC is **not** modified.

    Atomicity per backend (issue #625 acceptance criteria):

    * **LocalStorage** — after the (no-op) ``upload_file`` we additionally
      :func:`os.replace` the temp file onto the resolved local destination so
      the manifest appears on the local filesystem in a single atomic rename.
    * **S3 / GCS / Azure** — a single object PUT is atomic in every major
      object store, so the staged upload is itself the atomic publish.  The
      temp file is unlinked once the PUT returns.

    Callers must ensure this is invoked *after* the ``kpis.json`` (and any
    archived ``eplusout.sql``) uploads have returned successfully, so the
    manifest's appearance is a faithful "sample complete" fence.
    """
    local_tmp_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, indent=2)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="_manifest_",
        dir=str(local_tmp_dir),
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)

    try:
        # Local import keeps the module-import graph acyclic.
        from .storage import LocalStorage  # noqa: PLC0415

        if isinstance(storage, LocalStorage):
            # LocalStorage.upload_file is a no-op; perform the atomic local
            # rename so the manifest materialises on disk exactly once.
            final_path = _local_dest_for_key(storage, remote_key)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, final_path)
            tmp_path = final_path  # consumed by the rename
        else:
            # Remote backend: a single object PUT is the atomic publish.
            storage.upload_file(tmp_path, remote_key)
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            tmp_path = Path()  # mark consumed
    except Exception:
        # Best-effort cleanup of the staging file on any failure.
        try:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _local_dest_for_key(storage: ResultStorage, remote_key: str) -> Path:
    """Resolve the on-disk destination path for a *remote_key* under LocalStorage.

    LocalStorage has no configured root (it is a no-op backend), so the
    manifest is written next to the campaign outdir when available, otherwise
    under the current working directory.  This branch only fires for explicit
    LocalStorage usage and never for remote backends.
    """
    root = getattr(storage, "_root", None) or getattr(storage, "root", None)
    if not root:
        root = Path.cwd()
    return Path(root) / remote_key


def report_sample_completion(
    *,
    coordinator_url: str,
    campaign_id: str,
    manifest: dict[str, Any],
    api_key: str | None = None,
    timeout_s: float = 10.0,
) -> None:
    """Best-effort PATCH of sample completion to the Coordinator.

    Targets ``PATCH /api/v1/coordinator/campaigns/{campaign_id}/status``
    (contract §3.2 — worker status reporting).  The full §3.1 manifest is sent
    as the JSON body and the coarse sample ``status`` is also passed as a
    query parameter to match the endpoint's signature
    (``update_coordinator_campaign_status``).

    Uses :mod:`httpx` when the ``[api]`` extra is installed, otherwise falls
    back to :mod:`urllib.request` from the stdlib so this adds **no hard
    dependency**.

    This call is best-effort: any network or parsing failure is logged at
    ``WARNING`` and never re-raised — telemetry must not break the worker.
    """
    url = f"{coordinator_url.rstrip('/')}/api/v1/coordinator/campaigns/{campaign_id}/status"
    body = json.dumps(manifest).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params = {"status": str(manifest.get("status", "unknown"))}
    try:
        _do_patch(url, body, headers, params, timeout_s)
    except Exception as exc:  # noqa: BLE001 — best-effort telemetry, never fatal
        log.warning(
            "coordinator status report failed for campaign %s: %s",
            campaign_id,
            exc,
        )


def _do_patch(
    url: str,
    body: bytes,
    headers: dict[str, str],
    params: dict[str, str],
    timeout_s: float,
) -> None:
    """Issue the PATCH via httpx if available, else stdlib urllib."""
    try:
        import httpx  # optional [api]/[dev] extra  # noqa: PLC0415
    except ImportError:
        _patch_urllib(url, body, headers, params, timeout_s)
        return

    with httpx.Client(timeout=timeout_s) as client:
        resp = client.patch(url, content=body, headers=headers, params=params)
        resp.raise_for_status()


def _patch_urllib(
    url: str,
    body: bytes,
    headers: dict[str, str],
    params: dict[str, str],
    timeout_s: float,
) -> None:
    """Stdlib fallback for the Coordinator PATCH (no third-party deps)."""
    full_url = f"{url}?{urlencode(params)}" if params else url
    req = urllib.request.Request(  # noqa: S310 — caller-controlled coordinator URL
        full_url,
        data=body,
        headers=headers,
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            resp.read()
    except urllib.error.HTTPError as exc:
        # 2xx is success; surface anything else as a soft failure.
        if 200 <= exc.code < 300:
            return
        raise
