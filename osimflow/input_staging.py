"""Controller-to-worker input staging through object storage (issue #1809).

Remote Batch jobs run on a filesystem that shares nothing with the
controller, so a ``Path`` in a task payload is meaningless there.  This
module adds the smallest input-artifact contract the zero-shared-filesystem
substrates need:

* **Controller** (:class:`InputStager`): every ``Path`` leaf of a step call
  is replaced by a small tagged reference.  Existing files/directories are
  uploaded as *immutable, content-addressed* blobs plus a JSON manifest
  (``_inputs/blobs/<sha256>`` / ``_inputs/manifests/<sha256>.json``); paths
  that do not exist yet become output-only references.  The manifest digest
  travels inside the (HMAC-signed) task payload, so a tampered manifest is
  rejected.
* **Worker** (:class:`WorkerPathRemapper`): references are materialized into a
  scratch directory (every blob verified for size and SHA-256) and the
  payload is rewritten to the scratch paths.  After the step runs, result
  paths are mapped back to the controller's original paths so the existing
  result-transport upload/materialize keys line up.

Every failure (missing object, short/corrupt download, invalid manifest,
permission error) raises :class:`InputStagingError` -- there is deliberately
no fallback to the original (nonexistent) controller path.
"""

import hashlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import OSimFlowRuntimeError
from .executors.transport import (
    _PATH_MARKER_KEY,
    _PATH_MARKER_VALUE,
    _PATH_VALUE_KEY,
    local_path_to_storage_key,
)
from .storage import ResultStorage

__all__ = [
    "InputStager",
    "InputStagingError",
    "WorkerPathRemapper",
    "STAGED_INPUT_TYPE",
    "STAGED_OUTPUT_TYPE",
    "INPUT_KEY_PREFIX",
    "MANIFEST_SCHEMA_VERSION",
    "result_upload_plan",
    "PAYLOAD_REF_KEY",
    "spill_task_payload",
    "fetch_spilled_payload",
]

log = logging.getLogger("osimflow.input_staging")

STAGED_INPUT_TYPE = "staged_input"
STAGED_OUTPUT_TYPE = "staged_output"
INPUT_KEY_PREFIX = "_inputs"
MANIFEST_SCHEMA_VERSION = 1
_CHUNK = 1024 * 1024
PAYLOAD_REF_KEY = "payload_ref_sha256"


class InputStagingError(OSimFlowRuntimeError):
    """Raised for any controller/worker input-staging failure (issue #1809)."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _blob_key(sha256: str) -> str:
    return f"{INPUT_KEY_PREFIX}/blobs/{sha256[:2]}/{sha256}"


def _manifest_key(sha256: str) -> str:
    return f"{INPUT_KEY_PREFIX}/manifests/{sha256}.json"


def _is_staged_ref(value: Any) -> bool:  # noqa: ANN401
    return isinstance(value, dict) and value.get(_PATH_MARKER_KEY) in {
        STAGED_INPUT_TYPE,
        STAGED_OUTPUT_TYPE,
    }


def _collect_path_leaves(value: Any) -> list[Path]:  # noqa: ANN401
    if isinstance(value, Path):
        return [value]
    if isinstance(value, dict):
        return [p for v in value.values() for p in _collect_path_leaves(v)]
    if isinstance(value, (list, tuple)):
        return [p for v in value for p in _collect_path_leaves(v)]
    return []


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class InputStager:
    """Controller-side stager: uploads inputs and rewrites payload paths."""

    def __init__(self, storage: ResultStorage) -> None:
        self._storage = storage
        self._lock = threading.Lock()
        self._uploaded_blobs: set[str] = set()
        self._hash_cache: dict[tuple[str, int, int], str] = {}

    def _hash(self, path: Path) -> str:
        st = path.stat()
        cache_key = (str(path), st.st_size, st.st_mtime_ns)
        with self._lock:
            cached = self._hash_cache.get(cache_key)
        if cached is not None:
            return cached
        digest = _sha256_file(path)
        with self._lock:
            self._hash_cache[cache_key] = digest
        return digest

    def _upload_blob(self, path: Path, sha256: str) -> None:
        with self._lock:
            if sha256 in self._uploaded_blobs:
                return
        try:
            self._storage.upload_file(path, _blob_key(sha256))
        except Exception as exc:
            log.error("input staging: upload failed for %s: %s", path, exc, exc_info=True)
            raise InputStagingError(f"failed to stage input file {path}: {exc}") from exc
        with self._lock:
            self._uploaded_blobs.add(sha256)

    def stage_path(self, path: Path, *, role: str = "input") -> dict[str, Any]:
        """Stage *path* and return its tagged payload reference."""
        if not path.exists():
            return {
                _PATH_MARKER_KEY: STAGED_OUTPUT_TYPE,
                "path": str(path),
                "kind": "unknown",
            }
        if path.is_dir():
            kind = "dir"
            files: list[dict[str, Any]] = []
            dirs: list[str] = []
            for child in sorted(path.rglob("*")):
                rel = child.relative_to(path).as_posix()
                if child.is_dir():
                    dirs.append(rel)
                elif child.is_file():
                    sha = self._hash(child)
                    self._upload_blob(child, sha)
                    files.append(
                        {
                            "path": rel,
                            "size": child.stat().st_size,
                            "sha256": sha,
                            "mode": child.stat().st_mode & 0o777,
                        }
                    )
        elif path.is_file():
            kind = "file"
            sha = self._hash(path)
            self._upload_blob(path, sha)
            st = path.stat()
            files = [
                {"path": path.name, "size": st.st_size, "sha256": sha, "mode": st.st_mode & 0o777}
            ]
            dirs = []
        else:
            raise InputStagingError(f"cannot stage non-regular path: {path}")

        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "kind": kind,
            "name": path.name,
            "files": files,
            "dirs": dirs,
        }
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        manifest_sha = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory(prefix="osimflow-stage-") as tmp:
            local = Path(tmp) / "manifest.json"
            local.write_bytes(raw)
            try:
                self._storage.upload_file(local, _manifest_key(manifest_sha))
            except Exception as exc:
                log.error("input staging: manifest upload failed: %s", exc, exc_info=True)
                raise InputStagingError(f"failed to stage manifest for {path}: {exc}") from exc
        return {
            _PATH_MARKER_KEY: STAGED_INPUT_TYPE,
            "path": str(path),
            "kind": kind,
            "role": role,
            "manifest_sha256": manifest_sha,
        }

    def stage_task_paths(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        result_hint: Any = None,  # noqa: ANN401
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Return ``(args, kwargs)`` with every ``Path`` leaf replaced by a reference.

        Paths under (or equal to) a ``result_hint`` path, and paths that do
        not exist yet, are outputs; everything else is read-only input.
        """
        outputs = _collect_path_leaves(result_hint)
        refs: dict[str, dict[str, Any]] = {}

        def convert(value: Any) -> Any:  # noqa: ANN401
            if isinstance(value, Path):
                key = str(value)
                if key not in refs:
                    role = "output" if any(_is_within(value, o) for o in outputs) else "input"
                    refs[key] = self.stage_path(value, role=role)
                return dict(refs[key])
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [convert(v) for v in value]
            return value

        return tuple(convert(a) for a in args), {k: convert(v) for k, v in kwargs.items()}


class WorkerPathRemapper:
    """Worker-side materializer and original<->scratch path mapper."""

    def __init__(self, storage: ResultStorage, scratch_root: Path) -> None:
        self._storage = storage
        self._root = scratch_root
        self._mapping: dict[str, Path] = {}
        self._outputs: set[str] = set()
        self._counter = 0

    @property
    def output_originals(self) -> set[str]:
        return set(self._outputs)

    def _next_dir(self) -> Path:
        self._counter += 1
        return self._root / "paths" / str(self._counter)

    def resolve(self, value: Any) -> Any:  # noqa: ANN401
        """Rewrite staged references in a raw JSON payload value to scratch paths."""
        if _is_staged_ref(value):
            return {
                _PATH_MARKER_KEY: _PATH_MARKER_VALUE,
                _PATH_VALUE_KEY: str(self._materialize(value)),
            }
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    def _materialize(self, ref: dict[str, Any]) -> Path:
        original = ref.get("path")
        if not isinstance(original, str) or not original:
            raise InputStagingError("invalid staged reference: missing path")
        if original in self._mapping:
            return self._mapping[original]
        base = self._next_dir()
        name = PurePosixPath(original.replace("\\", "/")).name or "item"
        if ref.get(_PATH_MARKER_KEY) == STAGED_OUTPUT_TYPE:
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            self._mapping[original] = target
            self._outputs.add(original)
            return target
        target = self._materialize_manifest(ref, base)
        self._mapping[original] = target
        if ref.get("role") == "output":
            self._outputs.add(original)
        return target

    def _materialize_manifest(self, ref: dict[str, Any], base: Path) -> Path:  # noqa: PLR0912
        manifest_sha = ref.get("manifest_sha256")
        if not isinstance(manifest_sha, str) or len(manifest_sha) != 64:
            raise InputStagingError("invalid staged reference: bad manifest_sha256")
        base.mkdir(parents=True, exist_ok=True)
        manifest_path = base / ".manifest.json"
        self._download(_manifest_key(manifest_sha), manifest_path)
        raw = manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest_sha:
            raise InputStagingError(
                f"staged manifest {manifest_sha} failed integrity check (corrupt or tampered)"
            )
        manifest_path.unlink()
        try:
            manifest = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InputStagingError(f"staged manifest {manifest_sha} is not valid JSON") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
            or manifest.get("kind") not in {"file", "dir"}
            or not isinstance(manifest.get("files"), list)
            or not isinstance(manifest.get("dirs"), list)
            or not isinstance(manifest.get("name"), str)
        ):
            raise InputStagingError(f"staged manifest {manifest_sha} has an invalid schema")

        kind = manifest["kind"]
        top = base / "content"
        if kind == "file":
            if len(manifest["files"]) != 1:
                raise InputStagingError("file manifest must list exactly one file")
            root = top / _safe_rel(manifest["name"])
        else:
            root = top / _safe_rel(manifest["name"] or "dir")
            root.mkdir(parents=True, exist_ok=True)
            for rel in manifest["dirs"]:
                (root / _safe_rel(rel)).mkdir(parents=True, exist_ok=True)
        for entry in manifest["files"]:
            if not isinstance(entry, dict):
                raise InputStagingError("invalid manifest file entry")
            rel = _safe_rel(str(entry.get("path", "")))
            sha = entry.get("sha256")
            size = entry.get("size")
            if not isinstance(sha, str) or len(sha) != 64 or not isinstance(size, int):
                raise InputStagingError(f"invalid manifest entry for {rel}")
            dest = root if kind == "file" else root / rel
            self._download(_blob_key(sha), dest)
            if dest.stat().st_size != size:
                raise InputStagingError(
                    f"incomplete download for {rel}: expected {size} bytes, got {dest.stat().st_size}"
                )
            if _sha256_file(dest) != sha:
                raise InputStagingError(f"checksum mismatch for staged input {rel}")
            mode = entry.get("mode")
            if isinstance(mode, int):
                os.chmod(dest, mode & 0o777 or 0o600)
        return root

    def _download(self, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._storage.download_file(key, dest)
        except Exception as exc:
            log.error("input staging: download failed for %s: %s", key, exc, exc_info=True)
            raise InputStagingError(f"failed to fetch staged object {key!r}: {exc}") from exc
        if not dest.is_file():
            raise InputStagingError(f"staged object {key!r} was not materialized")

    def scratch_for(self, original: Path) -> Path:
        """Return the scratch location backing *original* (itself when unmapped)."""
        direct = self._mapping.get(str(original))
        if direct is not None:
            return direct
        for orig, scratch in self._mapping.items():
            root = Path(orig)
            if root in original.parents:
                return scratch / original.relative_to(root)
        return original

    def to_original(self, value: Any) -> Any:  # noqa: ANN401, PLR0911
        """Map scratch ``Path`` leaves in a step result back to controller paths."""
        if isinstance(value, Path):
            for orig, scratch in self._mapping.items():
                if value == scratch:
                    return Path(orig)
                if scratch in value.parents:
                    return Path(orig) / value.relative_to(scratch)
            return value
        if isinstance(value, dict):
            return {k: self.to_original(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.to_original(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.to_original(v) for v in value)
        return value


def _safe_rel(rel: str) -> Path:
    posix = PurePosixPath(rel)
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise InputStagingError(f"unsafe path in staged manifest: {rel!r}")
    return Path(*posix.parts)


def result_upload_plan(
    remapper: WorkerPathRemapper,
    result: Any,  # noqa: ANN401
    prefix: str | None,
) -> list[tuple[Path, str]]:
    """Return ``(scratch_path, storage_key)`` pairs to upload after a step.

    Keys derive from the *original controller path* so the controller's
    ``materialize_object_storage_result`` downloads them to the same place.
    """
    originals: dict[str, Path] = {}
    for p in _collect_path_leaves(result):
        originals[str(p)] = p
    for o in remapper.output_originals:
        originals.setdefault(o, Path(o))
    plan: list[tuple[Path, str]] = []
    for orig in originals.values():
        key = local_path_to_storage_key(orig, prefix)
        if key:
            plan.append((remapper.scratch_for(orig), key))
    return plan


def spill_task_payload(storage: ResultStorage, raw: str) -> str:
    """Upload an oversized task payload and return a small pointer payload.

    AWS Batch caps ``containerOverrides`` at 8192 bytes, which fan-in steps
    (one staged reference per sample) can exceed. The pointer carries the
    SHA-256 of the real payload; it is what gets HMAC-signed, so the spilled
    object is integrity-bound to the signature.
    """
    data = raw.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory(prefix="osimflow-payload-") as tmp:
        local = Path(tmp) / "payload.json"
        local.write_bytes(data)
        try:
            storage.upload_file(local, f"{INPUT_KEY_PREFIX}/payloads/{sha}.json")
        except Exception as exc:
            log.error("input staging: payload spill failed: %s", exc, exc_info=True)
            raise InputStagingError(f"failed to stage oversized task payload: {exc}") from exc
    return json.dumps({"schema_version": 1, PAYLOAD_REF_KEY: sha}, separators=(",", ":"))


def fetch_spilled_payload(storage: ResultStorage, sha256: str) -> str:
    """Download and integrity-check a payload written by :func:`spill_task_payload`."""
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise InputStagingError("invalid spilled payload reference")
    with tempfile.TemporaryDirectory(prefix="osimflow-payload-") as tmp:
        dest = Path(tmp) / "payload.json"
        try:
            storage.download_file(f"{INPUT_KEY_PREFIX}/payloads/{sha256}.json", dest)
        except Exception as exc:
            log.error("input staging: payload fetch failed: %s", exc, exc_info=True)
            raise InputStagingError(f"failed to fetch spilled task payload: {exc}") from exc
        data = dest.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise InputStagingError("spilled task payload failed integrity check")
    return data.decode("utf-8")
