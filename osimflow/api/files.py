"""File upload/download API endpoints (issue #273).

Provides:
  - POST   /api/v1/files/upload   — multipart file upload with type validation
  - GET    /api/v1/files           — list uploaded files
  - GET    /api/v1/files/{file_id} — file download with range-request support
  - DELETE /api/v1/files/{file_id} — delete an uploaded file (requires --enable-writes)

Files are stored under ``{base_dir}/uploads/{category}/{filename}``.
Metadata (file_id → file info mapping) is persisted in
``{base_dir}/uploads/files_index.json``.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from osimflow.api.schemas import (
    FileDeleteResponse,
    FileInfo,
    FileListResponse,
    FileUploadResponse,
)
from osimflow.validation import (
    ValidationError as OsimflowValidationError,
)
from osimflow.validation import (
    sanitize_filename,
    validate_path_within_base,
)

log = logging.getLogger("osimflow.api.files")

files_router = APIRouter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB per file

# Allowed file extensions per category.
ALLOWED_EXTENSIONS: dict[str, set[str]] = {
    "seed_model": {".osm"},
    "measure": {".rb", ".py"},
    "weather": {".epw"},
    "config": {".yml", ".yaml"},
}

# Reverse lookup: extension → category (for auto-detection fallback).
_EXTENSION_TO_CATEGORY: dict[str, str] = {}
for _cat, _exts in ALLOWED_EXTENSIONS.items():
    for _ext in _exts:
        _EXTENSION_TO_CATEGORY[_ext] = _cat

INDEX_FILE = "files_index.json"

# MIME types for known extensions (fallback to application/octet-stream).
_MIME_OVERRIDES: dict[str, str] = {
    ".osm": "application/json",  # OpenStudio Model (JSON-based)
    ".epw": "text/plain",
    ".rb": "text/x-ruby",
    ".py": "text/x-python",
    ".yml": "text/yaml",
    ".yaml": "text/yaml",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uploads_base_dir(request: Request) -> Path:
    """Return the uploads root directory.

    Uses ``campaigns_base_dir`` when set, otherwise falls back to ``outdir``.
    Raises 503 if neither is configured.
    """
    base: Path | None = getattr(request.app.state, "campaigns_base_dir", None)
    if base is None:
        base = getattr(request.app.state, "outdir", None)
    if base is None:
        raise HTTPException(status_code=503, detail="No output directory configured")
    uploads_dir = base / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    return uploads_dir


def _load_index(uploads_dir: Path) -> dict[str, dict[str, Any]]:
    """Load the file index from disk.  Returns empty dict when absent."""
    index_path = uploads_dir / INDEX_FILE
    if not index_path.exists():
        return {}
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        log.warning("corrupt files index at %s — starting fresh", index_path)
        return {}


def _save_index(uploads_dir: Path, index: dict[str, dict[str, Any]]) -> None:
    """Persist the file index to disk."""
    index_path = uploads_dir / INDEX_FILE
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")


def _validate_extension(filename: str, category: str) -> str:
    """Return the normalised lower-case extension if it is allowed.

    Raises :class:`HTTPException` (400) on mismatch.
    """
    _, ext = os.path.splitext(filename)
    ext = ext.lower()
    allowed = ALLOWED_EXTENSIONS.get(category)
    if allowed is None:
        valid = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"Unknown category '{category}'. Valid: {valid}",
        )
    if ext not in allowed:
        allowed_str = ", ".join(sorted(allowed))
        raise HTTPException(
            status_code=400,
            detail=f"File extension '{ext}' not allowed for category '{category}'. "
            f"Allowed: {allowed_str}",
        )
    return ext


def _detect_category(filename: str) -> str | None:
    """Auto-detect file category from extension.  Returns None if unknown."""
    _, ext = os.path.splitext(filename)
    return _EXTENSION_TO_CATEGORY.get(ext.lower())


def _content_type_for(filename: str) -> str:
    """Return a suitable Content-Type for the file."""
    _, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _parse_range_header(
    range_header: str,
    file_size: int,
) -> tuple[int, int] | None:
    """Parse an HTTP Range header (bytes=start-end).

    Returns ``(start, end)`` (inclusive) or ``None`` if the header is
    absent or malformed.
    """
    if not range_header.startswith("bytes=") or "-" not in range_header[6:]:
        return None
    range_spec = range_header[6:]
    start_str, end_str = range_spec.split("-", 1)
    if start_str == "" and end_str == "":
        return None
    try:
        # Suffix range: bytes=-N → last N bytes
        if start_str == "":
            suffix_len = int(end_str)
            return max(0, file_size - suffix_len), file_size - 1
        start = int(start_str)
        end = file_size - 1 if end_str == "" else min(int(end_str), file_size - 1)
    except (ValueError, TypeError):
        return None
    if start > end or start >= file_size:
        return None
    return start, end


# ---------------------------------------------------------------------------
# POST /api/v1/files/upload — multipart file upload
# ---------------------------------------------------------------------------


@files_router.post("/api/v1/files/upload", response_model=FileUploadResponse, status_code=201)
async def upload_file(
    file: UploadFile,
    request: Request,
    category: str = Query(
        ...,
        description="File category: seed_model | measure | weather | config",
    ),
) -> FileUploadResponse:
    """Upload a file to the server.

    Accepts seed models (``.osm``), measures (``.rb``/``.py``), weather
    files (``.epw``), and config files (``.yml``/``.yaml``).  Maximum
    file size: 100 MB.

    The ``category`` query parameter determines the storage subdirectory
    and which extensions are accepted.

    Returns the file ID, path, and metadata.
    """
    if getattr(request.app.state, "read_only", True):
        raise HTTPException(
            status_code=403,
            detail="File upload requires --enable-writes mode",
        )

    if file.filename is None:
        raise HTTPException(status_code=400, detail="Filename is required")

    # Sanitize and validate filename
    try:
        safe_name = sanitize_filename(file.filename)
    except OsimflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Validate extension against category
    _validate_extension(safe_name, category)

    uploads_dir = _uploads_base_dir(request)
    category_dir = uploads_dir / category
    category_dir.mkdir(parents=True, exist_ok=True)

    # Resolve destination, guarding against path traversal
    dest = (category_dir / safe_name).resolve()
    try:
        validate_path_within_base(dest, uploads_dir.resolve())
    except OsimflowValidationError:
        raise HTTPException(status_code=400, detail="Invalid file path") from None

    # Read file content with size limit enforcement
    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(content)} bytes, max {MAX_FILE_SIZE_BYTES} bytes)",
        )

    # Write to disk (overwrite if same name exists)
    dest.write_bytes(content)

    # Generate file ID and update index
    file_id = uuid.uuid4().hex
    index = _load_index(uploads_dir)

    # Remove any previous entry pointing to the same path
    for old_id, info in list(index.items()):
        if info.get("path") == str(dest.relative_to(uploads_dir)):
            del index[old_id]

    relative_path = str(dest.relative_to(uploads_dir))
    index[file_id] = {
        "file_id": file_id,
        "filename": safe_name,
        "category": category,
        "size_bytes": len(content),
        "path": relative_path,
    }
    _save_index(uploads_dir, index)

    log.info("uploaded file %s (%d bytes) → %s [%s]", safe_name, len(content), file_id, category)

    return FileUploadResponse(
        file_id=file_id,
        filename=safe_name,
        category=category,
        size_bytes=len(content),
        path=relative_path,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/files — list uploaded files
# ---------------------------------------------------------------------------


@files_router.get("/api/v1/files", response_model=FileListResponse)
async def list_files(
    request: Request,
    category: str | None = Query(default=None, description="Filter by category"),
) -> FileListResponse:
    """List all uploaded files, optionally filtered by category."""
    uploads_dir = _uploads_base_dir(request)
    index = _load_index(uploads_dir)

    files: list[FileInfo] = []
    for info in index.values():
        if category is not None and info.get("category") != category:
            continue
        files.append(
            FileInfo(
                file_id=info["file_id"],
                filename=info["filename"],
                category=info["category"],
                size_bytes=info["size_bytes"],
                path=info["path"],
            )
        )

    return FileListResponse(files=files, total=len(files))


# ---------------------------------------------------------------------------
# GET /api/v1/files/{file_id} — file download
# ---------------------------------------------------------------------------


@files_router.get("/api/v1/files/{file_id}")
async def download_file(
    file_id: str,
    request: Request,
) -> Response:
    """Download a file by ID.

    Supports HTTP Range requests for large file downloads.
    """
    uploads_dir = _uploads_base_dir(request)
    index = _load_index(uploads_dir)

    info = index.get(file_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"File '{file_id}' not found")

    file_path = (uploads_dir / info["path"]).resolve()

    # Path traversal guard
    try:
        validate_path_within_base(file_path, uploads_dir.resolve())
    except OsimflowValidationError:
        raise HTTPException(status_code=400, detail="Invalid file path") from None

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File no longer exists on disk")

    file_size = file_path.stat().st_size
    content_type = _content_type_for(info["filename"])

    # Range request support
    range_header = request.headers.get("range", "")
    if range_header:
        parsed = _parse_range_header(range_header, file_size)
        if parsed is None:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        start, end = parsed
        length = end - start + 1

        async def _range_stream() -> Any:
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk_size = min(64 * 1024, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            _range_stream(),
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(length),
                "Accept-Ranges": "bytes",
            },
        )

    # Full file response
    return Response(
        content=file_path.read_bytes(),
        media_type=content_type,
        headers={
            "Content-Length": str(file_size),
            "Content-Disposition": f'attachment; filename="{info["filename"]}"',
            "Accept-Ranges": "bytes",
        },
    )


# ---------------------------------------------------------------------------
# DELETE /api/v1/files/{file_id} — delete uploaded file
# ---------------------------------------------------------------------------


@files_router.delete("/api/v1/files/{file_id}", response_model=FileDeleteResponse)
async def delete_file(
    file_id: str,
    request: Request,
) -> FileDeleteResponse:
    """Delete an uploaded file by ID.

    Requires ``--enable-writes`` mode.
    """
    if getattr(request.app.state, "read_only", True):
        raise HTTPException(
            status_code=403,
            detail="File deletion requires --enable-writes mode",
        )

    uploads_dir = _uploads_base_dir(request)
    index = _load_index(uploads_dir)

    info = index.get(file_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"File '{file_id}' not found")

    file_path = (uploads_dir / info["path"]).resolve()

    # Path traversal guard
    try:
        validate_path_within_base(file_path, uploads_dir.resolve())
    except OsimflowValidationError:
        raise HTTPException(status_code=400, detail="Invalid file path") from None

    # Remove file from disk (best-effort)
    if file_path.is_file():
        file_path.unlink()
        log.info("deleted file %s from disk", file_path)

    # Remove from index
    del index[file_id]
    _save_index(uploads_dir, index)

    log.info("deleted file record %s (%s)", file_id, info["filename"])

    return FileDeleteResponse(file_id=file_id, status="deleted")
