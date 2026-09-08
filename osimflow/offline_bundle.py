"""Fail-closed verification of offline bundles (issue #1640).

``scripts/bundle_offline.py`` writes ``bundle_manifest.json`` with a
SHA-256 digest for every bundled asset (pip wheels, docker image tars,
EPW weather files). Air-gapped installs move the bundle over USB /
relay transfer — the highest tamper-exposure deployment mode — so the
manifest digests must actually be checked before any asset is
consumed (wheel install, image load, weather copy). Previously the
manifest was generated but never read back.

The CLI run path calls :func:`verify_offline_bundle` as soon as the
``--offline-bundle`` config is resolved and before the campaign
starts, so a substituted or corrupted asset aborts the run with a
clear error instead of silently executing.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from osimflow.errors import OSimFlowRuntimeError

__all__ = [
    "MANIFEST_FILENAME",
    "OfflineBundleError",
    "verify_offline_bundle",
]

#: Name of the manifest file written at the bundle root by
#: ``scripts/bundle_offline.py``.
MANIFEST_FILENAME = "bundle_manifest.json"

#: Manifest section name -> on-disk subdirectory holding its assets
#: (mirrors the layout produced by ``scripts/bundle_offline.py``).
_ASSET_SECTIONS: dict[str, str] = {
    "pip_wheels": "pip",
    "docker_images": "docker",
    "weather_files": "weather",
}

_CHUNK_SIZE = 65536


class OfflineBundleError(OSimFlowRuntimeError):
    """An offline bundle failed integrity verification (issue #1640).

    Also catchable as :class:`osimflow.errors.OSimFlowError` and as
    ``RuntimeError`` (the historic base for campaign-side failures).
    """


def _sha256(path: Path) -> str:
    """Stream the file through SHA-256 in chunks (docker tars are large)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_offline_bundle(bundle_path: Path) -> dict[str, Any]:
    """Verify every asset listed in the bundle's ``bundle_manifest.json``.

    Assets are hashed with streaming SHA-256 and compared against the
    digests recorded at bundle-creation time. Verification happens
    *before* any asset is consumed (pip wheel install, docker image
    load, weather copy) and **fails closed**: a missing bundle
    directory, a missing manifest, a malformed manifest, a missing
    asset, or a single mismatched byte raises :class:`OfflineBundleError`
    naming the offending asset.

    Only assets *listed* in the manifest are verified. Extra unlisted
    files inside the bundle directory are permitted (they are never
    consumed through the manifest contract); if an operator needs a
    strict inventory, compare directory listings against the manifest
    separately.

    Parameters
    ----------
    bundle_path
        The extracted offline bundle directory (the one containing
        ``bundle_manifest.json`` plus ``pip/``, ``docker/``, and
        ``weather/`` subdirectories).

    Returns
    -------
    dict[str, Any]
        The parsed manifest, on success.

    Raises
    ------
    OfflineBundleError
        On any integrity failure described above.
    """
    bundle = Path(bundle_path)
    if not bundle.is_dir():
        raise OfflineBundleError(
            f"offline bundle directory not found: {bundle} — refusing to use "
            "unverified bundle assets (assets are verified against "
            f"{MANIFEST_FILENAME} before consumption; issue #1640)"
        )
    manifest_path = bundle / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise OfflineBundleError(
            f"offline bundle manifest missing: {manifest_path} — refusing to "
            "use unverified bundle assets (issue #1640)"
        )
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OfflineBundleError(
            f"offline bundle manifest is not valid JSON: {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise OfflineBundleError(
            f"offline bundle manifest must be a JSON object: {manifest_path} "
            f"(got {type(manifest).__name__})"
        )

    for section, subdir in _ASSET_SECTIONS.items():
        entries: Any = manifest.get(section, [])
        if not isinstance(entries, list):
            raise OfflineBundleError(
                f"offline bundle manifest section '{section}' must be a list "
                f"in {manifest_path} (got {type(entries).__name__})"
            )
        for entry in entries:
            if not isinstance(entry, dict) or "name" not in entry or "sha256" not in entry:
                raise OfflineBundleError(
                    f"offline bundle manifest section '{section}' has a "
                    f"malformed entry (needs 'name' and 'sha256'): {entry!r}"
                )
            name = str(entry["name"])
            expected = str(entry["sha256"])
            if not name or "/" in name or "\\" in name or name in {".", ".."}:
                # Asset names written by scripts/bundle_offline.py are bare
                # filenames; anything else is an attempted path traversal.
                raise OfflineBundleError(
                    f"offline bundle manifest asset name must be a bare "
                    f"filename, got {name!r} in section '{section}'"
                )
            asset_path = bundle / subdir / name
            if not asset_path.is_file():
                raise OfflineBundleError(
                    f"offline bundle asset missing: {asset_path} (listed in "
                    f"{MANIFEST_FILENAME} section '{section}') — refusing to "
                    "use a partially transferred bundle (issue #1640)"
                )
            actual = _sha256(asset_path)
            if actual != expected:
                raise OfflineBundleError(
                    f"offline bundle asset failed SHA-256 verification: "
                    f"{asset_path}: expected {expected}, got {actual} — the "
                    "bundle is corrupted or was tampered with; refusing to "
                    "consume it (issue #1640)"
                )
    return manifest
