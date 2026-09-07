"""Container image signature / SLSA-provenance verification (issue #1385).

ADR-0002 adopts ``nrel/openstudio:<version>`` and ``--container-digest``
(issue #1081) pins the image content, but neither proves the image came
from a trusted signer: a registry compromise can serve a *different*
image under a known digest. This module shells out to ``cosign verify``
at campaign init when the operator opts in via
``--require-cosign-identity``; on verification failure the campaign
refuses to run (``CosignVerificationError``), which is strictly
stronger than forcing a cache miss — nothing is written to the cache
from an untrusted image.

Scope guard (issue #1385): this is the post-pull verification half
only. Mutable-tag defaults are owned by issue #1320.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from .errors import OSimFlowRuntimeError

log = logging.getLogger("osimflow.cosign")

#: Default lookaside rekor / OIDC issuer for keyless cosign signatures.
#: Operators embedding NREL's signing workflow can override via
#: ``--cosign-oidc-issuer``.
DEFAULT_COSIGN_OIDC_ISSUER = "https://token.actions.githubusercontent.com"

#: Per-invocation budget for the ``cosign verify`` subprocess. Registry
#: round-trips dominate; two minutes covers cold tuf/rekor lookups
#: without hanging campaign init indefinitely.
COSIGN_VERIFY_TIMEOUT_S = 120.0


class CosignVerificationError(OSimFlowRuntimeError):
    """Raised when ``cosign verify`` rejects (or cannot check) an image.

    Mirrors the ``ImageDigestUnavailableError`` sentinel from issue
    #1218: a signature-verification failure must never be silently
    swallowed, because a cache hit would otherwise let a substituted
    image satisfy the campaign.
    """


def build_cosign_image_ref(
    *,
    container: str,
    container_digest: str | None,
    openstudio_version: str,
) -> str:
    """Return the fully-qualified image reference to verify.

    Digest-pinned form (``<container>@sha256:...``) wins when a digest
    is supplied; otherwise the mutable version tag
    (``<container>:<version>``) is used, matching the ``CONTAINER_OS``
    contract in :mod:`osimflow.campaign`.
    """
    container = container.rstrip("/")
    if "@" in container:
        # Already a fully-qualified ref with digest (e.g. from CONTAINER_OS).
        return container
    if container_digest:
        return f"{container}@{container_digest}"
    return f"{container}:{openstudio_version}"


def verify_image_signature(
    image_ref: str,
    certificate_identity: str,
    certificate_oidc_issuer: str = DEFAULT_COSIGN_OIDC_ISSUER,
    *,
    cosign_binary: str | None = None,
    timeout_s: float = COSIGN_VERIFY_TIMEOUT_S,
) -> None:
    """Verify ``image_ref`` via keyless ``cosign verify``.

    Parameters
    ----------
    image_ref
        Fully-qualified reference, e.g. ``nrel/openstudio@sha256:...``.
    certificate_identity
        Expected signer identity (e.g. ``https://github.com/NREL/OpenStudio/...``).
    certificate_oidc_issuer
        Expected OIDC issuer for the keyless certificate.
    cosign_binary
        Explicit path to the ``cosign`` executable. When ``None`` the
        binary is resolved from ``PATH``.
    timeout_s
        Subprocess budget in seconds.

    Raises
    ------
    CosignVerificationError
        When the binary is unavailable, times out, or exits non-zero.
        The subprocess stderr is embedded to make CI failures actionable.
    """
    resolved = cosign_binary or shutil.which("cosign")
    if resolved is None:
        raise CosignVerificationError(
            "cosign binary not found on PATH. Install cosign "
            "(https://docs.sigstore.dev/cosign/system_config/installation/) "
            "or pass --require-cosign-identity only on hosts that have it."
        )
    cmd = [
        str(resolved),
        "verify",
        "--certificate-identity",
        certificate_identity,
        "--certificate-oidc-issuer",
        certificate_oidc_issuer,
        image_ref,
    ]
    log.info("verifying image signature: %s", image_ref)
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CosignVerificationError(
            f"cosign verify timed out after {timeout_s:.0f}s for {image_ref!r}"
        ) from exc
    except OSError as exc:
        raise CosignVerificationError(
            f"cosign verify could not execute {resolved!r}: {exc}"
        ) from exc
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise CosignVerificationError(
            f"cosign verify FAILED for {image_ref!r} "
            f"(identity={certificate_identity!r}, issuer={certificate_oidc_issuer!r}, "
            f"exit={proc.returncode}): {stderr_tail}"
        )
    log.info("cosign verification OK for %s", image_ref)


def triangulate_image_ref(
    image_ref: str,
    *,
    cosign_binary: str | None = None,
    timeout_s: float = COSIGN_VERIFY_TIMEOUT_S,
) -> str:
    """Resolve a mutable-tag ref to its digest-pinned form via ``cosign triangulate``.

    Issue #1536: verifying a tag ref resolves tag→digest once inside
    ``cosign verify``, but the executors then pull by tag later — a tag
    re-push between verify and pull substitutes the executed image while
    the receipt still attests "verified: true" (TOCTOU).  Triangulating
    FIRST and verifying the digest-pinned ref closes the window.

    Returns the digest-pinned reference (``<repo>@sha256:<hex>``).
    Returns *image_ref* unchanged when it is already digest-pinned.

    Raises
    ------
    CosignVerificationError
        When the binary is unavailable, times out, or exits non-zero —
        the caller must refuse to run (issue #1536).
    """
    if "@sha256:" in image_ref:
        return image_ref
    resolved = cosign_binary or shutil.which("cosign")
    if resolved is None:
        raise CosignVerificationError(
            "cosign binary not found on PATH. Install cosign "
            "(https://docs.sigstore.dev/cosign/system_config/installation/) "
            "— a tag ref requires triangulation before verification "
            "(issue #1536)."
        )
    cmd = [str(resolved), "triangulate", image_ref]
    log.info("triangulating image ref: %s", image_ref)
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CosignVerificationError(
            f"cosign triangulate timed out after {timeout_s:.0f}s for {image_ref!r}"
        ) from exc
    except OSError as exc:
        raise CosignVerificationError(
            f"cosign triangulate could not execute {resolved!r}: {exc}"
        ) from exc
    pinned = (proc.stdout or "").strip()
    if proc.returncode != 0 or "@sha256:" not in pinned:
        stderr_tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise CosignVerificationError(
            f"cosign triangulate FAILED for {image_ref!r} "
            f"(exit={proc.returncode}): {stderr_tail} — refusing to verify a "
            "mutable-tag ref without its digest (issue #1536)."
        )
    log.info("triangulated %s -> %s", image_ref, pinned)
    return pinned


def write_cosign_receipt(
    outdir: Path,
    *,
    image_ref: str,
    certificate_identity: str,
    certificate_oidc_issuer: str,
    verified_digest: str | None = None,
    triangulated_from_tag: str | None = None,
) -> Path:
    """Persist the verification inputs alongside ``run.json`` for audit.

    The receipt is a small JSON file (``cosign_verification.json``)
    recording *what* was verified — the image reference, expected
    identity, issuer, and (issue #1536) the digest ``cosign verify``
    actually resolved plus the source tag when the ref was
    triangulated — so post-hoc audits can re-run verification against
    the same inputs.
    """
    receipt: dict[str, object] = {
        "image_ref": image_ref,
        "certificate_identity": certificate_identity,
        "certificate_oidc_issuer": certificate_oidc_issuer,
        "verified": True,
    }
    if verified_digest is not None:
        receipt["verified_digest"] = verified_digest
    if triangulated_from_tag is not None:
        receipt["triangulated_from_tag"] = triangulated_from_tag
    path = outdir / "cosign_verification.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
