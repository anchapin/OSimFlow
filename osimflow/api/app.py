"""FastAPI application for OSimFlow campaign monitoring.

Security features (issue #268, #395):
- API key authentication (``X-API-Key`` header or ``api_key`` query param)
- Multi-user support with per-user permission levels (issue #395)
- CORS middleware with configurable origins
- Rate limiting via a custom sliding-window middleware (default 60/minute)
- Read-only mode by default; ``--enable-writes`` required for mutations
- ``/health`` remains unauthenticated for load balancer probes
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pandas as pd
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from osimflow.api.auth import (
    APIKeyUser,
    MultiUserAPIKeyStore,
    extract_api_key,
    validate_api_key,
)
from osimflow.api.campaigns import campaigns_router
from osimflow.api.events import events_router
from osimflow.api.files import files_router
from osimflow.api.measures import measures_router
from osimflow.api.pat_compat import pat_compat_router
from osimflow.api.timeseries import timeseries_router
from osimflow.api.ui import ui_router
from osimflow.api.variables import variables_router
from osimflow.validation import ValidationError as OsimflowValidationError
from osimflow.validation import sanitize_filename, sanitize_sample_id, validate_path_within_base

log = logging.getLogger("osimflow.api")


def _get_real_remote_address(request: Request) -> str:
    """Extract the real client IP for rate limiting.

    When OSimFlow is deployed behind a load balancer (for horizontal
    scaling), ``get_remote_address`` returns the load balancer's IP instead
    of the client's IP.  This function checks the ``X-Forwarded-For`` header
    first (set by most HTTP load balancers) and falls back to
    ``get_remote_address`` when the header is not present.

    Horizontal scaling with multiple API instances behind a load balancer
    requires that all instances share the same ``X-Forwarded-For`` header
    handling so that per-client rate limits are correctly enforced.
    """
    forwarded_for = request.headers.get("X-Forwarded-For") or request.headers.get("X_FORWARDED_FOR")
    if forwarded_for:
        # X-Forwarded-For can contain multiple IPs: client, proxy1, proxy2...
        # The first IP is the original client.
        client_ip = forwarded_for.split(",")[0].strip()
        if client_ip:
            return client_ip  # type: ignore[no-any-return]
    return get_remote_address(request)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Rate limiting (issue #454 — replaces slowapi middleware)
# ---------------------------------------------------------------------------


# Maps rate-limit unit strings to seconds.
_RATE_UNIT_SECONDS: dict[str, int] = {
    "second": 1,
    "seconds": 1,
    "minute": 60,
    "minutes": 60,
    "hour": 3600,
    "hours": 3600,
    "day": 86400,
    "days": 86400,
}

_RATE_RE: re.Pattern[str] = re.compile(r"^\s*(\d+)\s*/\s*(\w+)\s*$")


def _parse_rate_limit(rate_limit: str) -> tuple[int, int]:
    """Parse a rate-limit string like ``"2/minute"`` into ``(count, window_s)``.

    Raises ``ValueError`` on malformed input.
    """
    match = _RATE_RE.match(rate_limit)
    if not match:
        raise ValueError(f"Invalid rate limit string: {rate_limit!r}")
    count = int(match.group(1))
    unit = match.group(2).lower()
    if unit not in _RATE_UNIT_SECONDS:
        raise ValueError(f"Unknown rate limit unit: {unit!r}")
    return count, _RATE_UNIT_SECONDS[unit]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-process sliding-window rate limiter.

    Replaces slowapi's ``SlowAPIMiddleware`` which did not reliably
    maintain its in-memory counter between sequential ``TestClient``
    requests under pytest-xdist in CI (issue #454).

    Each ``create_app()`` call creates a **fresh** middleware instance
    so the counter is isolated per app — critical for tests that each
    build their own app.

    The limiter uses a sliding-window counter: every request appends
    a monotonic timestamp to a per-key bucket; entries older than the
    window are pruned on each check.
    """

    def __init__(
        self,
        app: Any,  # noqa: ANN401 (Starlette ASGI app)
        *,
        rate_limit: str,
        key_func: Callable[[Request], str] = _get_real_remote_address,
    ) -> None:
        super().__init__(app)
        self.max_requests, self.window_seconds = _parse_rate_limit(rate_limit)
        self.key_func = key_func
        # Per-key list of monotonic timestamps of accepted requests.
        self._hits: dict[str, list[float]] = defaultdict(list)

    async def dispatch(
        self,
        request: Request,
        call_next: Any,  # noqa: ANN401 (Starlette callable)
    ) -> Response:
        key = self.key_func(request)
        now = time.monotonic()
        cutoff = now - self.window_seconds

        # Prune expired entries for this key.
        bucket = self._hits[key]
        fresh = [t for t in bucket if t > cutoff]
        self._hits[key] = fresh

        if len(fresh) >= self.max_requests:
            retry_after = max(1, int(self.window_seconds - (now - fresh[0])) + 1)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
                headers={"Retry-After": str(retry_after)},
            )

        fresh.append(now)
        return cast(Response, await call_next(request))


router = APIRouter()

PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/", "/static/index.html", ""})

# Re-export permission constants for convenience
_ADMIN = "admin"
_READONLY = "readonly"
_READWRITE = "readwrite"


class APIKeyMiddleware(BaseHTTPMiddleware):
    """HTTP middleware that enforces API key authentication.

    Requests to paths in :data:`PUBLIC_PATHS` always pass through.
    When no API keys are configured, authentication is disabled
    (backward-compatible with pre-#268 callers).

    Supports two modes (issue #395):
    - Single key mode: ``app.state.api_key_store`` is a single key string.
      All authenticated users get server-level read_only permission.
    - Multi-user mode: ``app.state.api_key_store`` is a
      ``MultiUserAPIKeyStore`` with per-user roles.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Any,  # noqa: ANN401  (starlette callable protocol)
    ) -> Response:
        normalised = request.url.path.rstrip("/")
        if normalised in PUBLIC_PATHS:
            return cast(Response, await call_next(request))

        key_store: MultiUserAPIKeyStore | str | None = getattr(
            request.app.state, "api_key_store", None
        )
        if isinstance(key_store, str):
            key_store = MultiUserAPIKeyStore.from_single_key(key_store)
        if key_store is None:
            return cast(Response, await call_next(request))

        provided = extract_api_key(request)

        if isinstance(key_store, MultiUserAPIKeyStore) and key_store.single_key is not None:
            if not validate_api_key(provided, key_store.single_key):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing API key"},
                )
            request.state.api_user = APIKeyUser(key=provided or "", user_id="default", role=_ADMIN)
        elif isinstance(key_store, MultiUserAPIKeyStore):
            user = key_store.validate(provided)
            if user is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing API key"},
                )
            request.state.api_user = user
        else:
            return cast(Response, await call_next(request))

        return cast(Response, await call_next(request))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_run_json(request: Request) -> dict[str, Any]:
    """Load and return run.json from outdir."""
    if request.app.state.outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")
    run_json_path: Path = request.app.state.outdir / "run.json"
    if not run_json_path.exists():
        raise HTTPException(
            status_code=404,
            detail="run.json not found — campaign may not have started",
        )
    raw: Any = json.loads(run_json_path.read_text())
    return raw  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Health / readiness
# ---------------------------------------------------------------------------


@router.get("/health")  # type: ignore[untyped-decorator]
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "alive"}


@router.get("/ready")  # type: ignore[untyped-decorator]
async def ready(request: Request) -> dict[str, Any]:
    """Readiness probe — checks if run.json is accessible."""
    try:
        data = _load_run_json(request)
        return {"status": "ready", "campaign_id": data.get("campaign_id")}
    except HTTPException:
        return {"status": "not_ready", "reason": "run.json not available"}


# ---------------------------------------------------------------------------
# Campaign / steps
# ---------------------------------------------------------------------------


@router.get("/api/v1/campaign")  # type: ignore[untyped-decorator]
async def get_campaign(request: Request) -> dict[str, Any]:
    """Get campaign metadata from run.json."""
    data = _load_run_json(request)
    return {
        "campaign_id": data.get("campaign_id"),
        "config_summary": data.get("config_summary", {}),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "baseline_comparison": data.get("baseline_comparison"),
    }


@router.get("/api/v1/cache/stats")  # type: ignore[untyped-decorator]
async def get_cache_stats(request: Request) -> dict[str, Any]:
    """Get cache hit/miss statistics from the campaign cache."""
    cache = getattr(request.app.state, "cache", None)
    if cache is None:
        raise HTTPException(status_code=503, detail="Cache not available")
    stats = cache.get_stats()
    hit_rate = cache.get_cache_hit_rate()
    return {
        "hits": stats.hits,
        "misses": stats.misses,
        "invalidations": stats.invalidations,
        "total_keys": stats.total_keys,
        "hit_rate": hit_rate,
    }


@router.get("/api/v1/steps")  # type: ignore[untyped-decorator]
async def get_steps(request: Request) -> dict[str, Any]:
    """Get step traces from run.json."""
    data = _load_run_json(request)
    return {
        "steps": data.get("steps", []),
        "total_steps": len(data.get("steps", [])),
    }


# ---------------------------------------------------------------------------
# Sample endpoints (issue #147)
# ---------------------------------------------------------------------------


@router.get("/api/v1/samples")  # type: ignore[untyped-decorator]
async def get_samples(
    request: Request,
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    per_page: int = Query(50, ge=1, le=500, description="Items per page (max 500)"),
) -> dict[str, Any]:
    """Get paginated per-sample traces from run.json."""
    data = _load_run_json(request)
    all_samples: list[dict[str, Any]] = data.get("per_sample", [])
    total = len(all_samples)
    start = (page - 1) * per_page
    end = start + per_page
    page_samples = all_samples[start:end]
    return {
        "samples": page_samples,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/api/v1/samples/{sid}")  # type: ignore[untyped-decorator]
async def get_sample_detail(sid: str, request: Request) -> dict[str, Any]:
    """Get detail for a single sample, including KPIs and log files."""
    # Validate sample ID to prevent path traversal.
    try:
        safe_sid = sanitize_sample_id(sid)
    except OsimflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    data = _load_run_json(request)
    all_samples: list[dict[str, Any]] = data.get("per_sample", [])
    sample: dict[str, Any] | None = None
    for s in all_samples:
        if s.get("sample_id") == safe_sid:
            sample = s
            break
    if sample is None:
        raise HTTPException(status_code=404, detail=f"Sample '{safe_sid}' not found")

    kpis: dict[str, Any] | None = None
    log_files: dict[str, str] = {}
    if request.app.state.outdir is not None:
        outdir_resolved: Path = request.app.state.outdir.resolve()
        sim_dir = outdir_resolved / "work" / "sim" / safe_sid
        # Validate the resolved sim_dir stays within outdir.
        try:
            validate_path_within_base(sim_dir.resolve(), outdir_resolved)
        except OsimflowValidationError:
            raise HTTPException(status_code=400, detail="Invalid sample directory") from None
        for kpi_name in ("kpi.json", "kpis.json"):
            kpi_path = sim_dir / kpi_name
            if kpi_path.exists():
                kpis = json.loads(kpi_path.read_text())
                break
        for log_name in ("stdout.log", "stderr.log"):
            log_path = sim_dir / log_name
            if log_path.exists():
                log_files[log_name] = str(log_path)

    return {
        "sample_id": safe_sid,
        "kpis": kpis,
        "log_files": log_files,
        **sample,
    }


@router.get("/api/v1/samples/{sid}/logs/{log_name}")  # type: ignore[untyped-decorator]
async def get_sample_log(sid: str, log_name: str, request: Request) -> Response:
    """Get the content of a sample's log file.

    Returns the raw log file content with appropriate content-type.
    """
    if request.app.state.outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")

    # Validate sample ID to prevent path traversal.
    try:
        safe_sid = sanitize_sample_id(sid)
    except OsimflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Explicitly reject ".." in sample IDs — resolve() normalizes .. before
    # is_relative_to() can detect traversal, so we must catch it here.
    if ".." in safe_sid:
        raise HTTPException(status_code=400, detail="Invalid sample ID") from None

    # Validate log name to prevent path traversal.
    try:
        safe_log_name = sanitize_filename(log_name)
    except OsimflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if safe_log_name not in ("stdout.log", "stderr.log"):
        raise HTTPException(status_code=400, detail="Only stdout.log and stderr.log are allowed")

    outdir_resolved: Path = request.app.state.outdir.resolve()
    log_path = outdir_resolved / "work" / "sim" / safe_sid / safe_log_name

    try:
        validate_path_within_base(log_path.resolve(), outdir_resolved)
    except OsimflowValidationError:
        raise HTTPException(status_code=400, detail="Invalid log path") from None

    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"Log file '{safe_log_name}' not found")

    return FileResponse(
        log_path,
        media_type="text/plain",
        filename=safe_log_name,
    )


# ---------------------------------------------------------------------------
# Results / failures
# ---------------------------------------------------------------------------


@router.get("/api/v1/results")  # type: ignore[untyped-decorator]
async def get_results(request: Request) -> list[dict[str, Any]]:
    """Read aggregated_results.csv and return as JSON array."""
    if request.app.state.outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")
    csv_path: Path = request.app.state.outdir / "aggregated_results.csv"
    if not csv_path.exists():
        raise HTTPException(
            status_code=404,
            detail="aggregated_results.csv not found",
        )
    df = pd.read_csv(csv_path)
    records: list[dict[str, Any]] = json.loads(df.to_json(orient="records"))
    return records


@router.get("/api/v1/failures")  # type: ignore[untyped-decorator]
async def get_failures(request: Request) -> list[dict[str, Any]]:
    """Read failed_simulations.csv and return as JSON array."""
    if request.app.state.outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")
    csv_path: Path = request.app.state.outdir / "failed_simulations.csv"
    if not csv_path.exists():
        raise HTTPException(
            status_code=404,
            detail="failed_simulations.csv not found",
        )
    df = pd.read_csv(csv_path)
    records: list[dict[str, Any]] = json.loads(df.to_json(orient="records"))
    return records


# ---------------------------------------------------------------------------
# Pareto front
# ---------------------------------------------------------------------------


@router.get("/api/v1/pareto")  # type: ignore[untyped-decorator]
async def get_pareto(request: Request) -> dict[str, Any]:
    """Read pareto front data from outdir/pareto/gen_*.json files."""
    if request.app.state.outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")
    pareto_dir: Path = request.app.state.outdir / "pareto"
    if not pareto_dir.exists():
        raise HTTPException(
            status_code=404,
            detail="No pareto data found",
        )
    gen_files = sorted(pareto_dir.glob("gen_*.json"))
    if not gen_files:
        raise HTTPException(
            status_code=404,
            detail="No pareto data found",
        )
    generations: list[dict[str, Any]] = []
    for gf in gen_files:
        gen_data = json.loads(gf.read_text())
        gen_data["_file"] = gf.name
        generations.append(gen_data)
    return {
        "generations": generations,
        "total_generations": len(generations),
    }


# ---------------------------------------------------------------------------
# Plots (issue #264)
# ---------------------------------------------------------------------------


@router.get("/api/v1/plots")  # type: ignore[untyped-decorator]
async def get_plots(request: Request) -> dict[str, Any]:
    """List available PNG plot files from the campaign outdir."""
    if request.app.state.outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")
    plots: list[dict[str, Any]] = []
    # Look for PNG files directly in outdir and in outdir/plots/
    search_dirs: list[Path] = [request.app.state.outdir]
    plots_dir = request.app.state.outdir / "plots"
    if plots_dir.is_dir():
        search_dirs.append(plots_dir)
    for search_dir in search_dirs:
        for png_file in sorted(search_dir.glob("*.png")):
            plots.append({"name": png_file.name, "size": png_file.stat().st_size})
    # Deduplicate by name (prefer plots/ subdir)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for p in reversed(plots):
        if p["name"] not in seen:
            seen.add(p["name"])
            unique.append(p)
    unique.reverse()
    return {"plots": unique, "total": len(unique)}


@router.get("/api/v1/plots/{filename}")  # type: ignore[untyped-decorator]
async def get_plot_file(filename: str, request: Request) -> FileResponse:
    """Serve a single PNG plot file."""
    if request.app.state.outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")
    # Sanitize filename to prevent path traversal.
    try:
        safe_name = sanitize_filename(filename)
    except OsimflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not safe_name.endswith(".png"):
        raise HTTPException(status_code=400, detail="Only .png files are allowed")
    # Check plots/ subdir first, then outdir root
    outdir_resolved: Path = request.app.state.outdir.resolve()
    for search_dir in [outdir_resolved / "plots", outdir_resolved]:
        candidate = (search_dir / safe_name).resolve()
        # Path traversal check: candidate must be within search_dir.
        try:
            validate_path_within_base(candidate, outdir_resolved)
        except OsimflowValidationError:
            raise HTTPException(status_code=400, detail="Invalid plot path") from None
        if candidate.is_file():
            return FileResponse(candidate, media_type="image/png")
    raise HTTPException(status_code=404, detail=f"Plot '{safe_name}' not found")


# ---------------------------------------------------------------------------
# Error diagnosis (issue #385)
# ---------------------------------------------------------------------------

# Error classification patterns — mirrors aggregate_results.py for consistency.
_ERROR_FAILURE_PATTERNS: list[tuple[str, list[re.Pattern[str]]]] = [
    (
        "convergence",
        [
            re.compile(r"did not converge", re.IGNORECASE),
            re.compile(r"exceeded max iterations", re.IGNORECASE),
            re.compile(r"not converged", re.IGNORECASE),
            re.compile(r"iteration.?limit", re.IGNORECASE),
        ],
    ),
    (
        "surface_geometry",
        [
            re.compile(r"surface.*(?:intersection|non.convex)", re.IGNORECASE),
            re.compile(r"non.convex\s*surface", re.IGNORECASE),
            re.compile(r"zero.area\s*surface", re.IGNORECASE),
            re.compile(r"surfaceless\s*zone", re.IGNORECASE),
            re.compile(r"detected.*zero.area", re.IGNORECASE),
        ],
    ),
    (
        "hvac_sizing",
        [
            re.compile(r"autosize.*(?:failed|out.of.range)", re.IGNORECASE),
            re.compile(r"plant\s*loop.*(?:not.converged|no.demand|no.load)", re.IGNORECASE),
            re.compile(r"no\s*load\s*on\s*plant\s*loop", re.IGNORECASE),
            re.compile(r"sizing.*(?:failed|error)", re.IGNORECASE),
        ],
    ),
    (
        "schedule",
        [
            re.compile(r"schedule.*(?:not.found|invalid|does not exist)", re.IGNORECASE),
            re.compile(r"(?:missing|unknown)\s*schedule", re.IGNORECASE),
        ],
    ),
    (
        "material_construction",
        [
            re.compile(r"material.*(?:not.found|does not exist)", re.IGNORECASE),
            re.compile(r"construction.*(?:not.found|does not exist|invalid)", re.IGNORECASE),
            re.compile(r"(?:missing|unknown)\s*(?:material|construction)", re.IGNORECASE),
        ],
    ),
    (
        "weather_file",
        [
            re.compile(r"weather\s*file.*(?:error|not.found|invalid|missing)", re.IGNORECASE),
            re.compile(r"cannot\s*(?:open|find|read).*\.epw", re.IGNORECASE),
        ],
    ),
    (
        "memory_timeout",
        [
            re.compile(r"(?:allocation|memory).*error", re.IGNORECASE),
            re.compile(r"timeout", re.IGNORECASE),
            re.compile(r"out\s*of\s*memory", re.IGNORECASE),
        ],
    ),
    (
        "timestep_instability",
        [
            re.compile(r"temperatures?\s*out\s*of\s*bounds?", re.IGNORECASE),
            re.compile(r"node.*temperature\s*out\s*of\s*range", re.IGNORECASE),
            re.compile(r"facsimile.*failed", re.IGNORECASE),
            re.compile(r"timestep.*(?:unstable|error)", re.IGNORECASE),
        ],
    ),
]

_ERROR_CATEGORY_SUGGESTIONS: dict[str, str] = {
    "convergence": "Consider increasing iteration limits or relaxing convergence tolerances in the HVAC controller settings.",
    "surface_geometry": "Simplify geometry or fix non-convex surfaces. Check for coincident/overlapping surfaces.",
    "hvac_sizing": "Review autosizing parameters or provide manual sizing values. Verify design-day definitions.",
    "schedule": "Check schedule names in the model match those referenced by objects (e.g., thermostat, lights).",
    "material_construction": "Verify all materials and constructions are defined and referenced correctly in the model.",
    "weather_file": "Verify the EPW weather file exists, is readable, and has the expected format.",
    "memory_timeout": "Reduce model complexity or increase available compute resources (memory/timestep count).",
    "timestep_instability": "Reduce the simulation timestep (e.g., from 60 to 10 minutes) or relax convergence criteria.",
    "generic_severe": "Review the full eplusout.err for additional context around this error.",
}


def _classify_error_line(line: str) -> str:
    """Classify an error line into a failure category."""
    for category, patterns in _ERROR_FAILURE_PATTERNS:
        for pat in patterns:
            if pat.search(line):
                return category
    return "generic_severe"


def _count_severe_errors(err_path: Path) -> int:
    """Count total severe error lines in an EnergyPlus error file."""
    count = 0
    try:
        with err_path.open() as f:
            for line in f:
                if "  * Severe" in line or "** Severe" in line:
                    count += 1
    except (OSError, UnicodeDecodeError):
        log.warning("Could not read error file for counting: %s", err_path)
    return count


def _find_root_cause_line(err_path: Path) -> str:
    """Find the earliest root-cause line from an EnergyPlus error file."""
    first_severe = ""
    try:
        with err_path.open() as f:
            for line in f:
                stripped = line.strip()
                if not first_severe and ("  * Severe" in line or "** Severe" in line):
                    first_severe = stripped
                for _cat, patterns in _ERROR_FAILURE_PATTERNS:
                    for pat in patterns:
                        if pat.search(stripped):
                            return stripped
    except (OSError, UnicodeDecodeError):
        log.warning("Could not read error file for root cause: %s", err_path)
    return first_severe


def _diagnose_sample_error(err_path: Path) -> dict[str, Any]:
    """Diagnose an EnergyPlus error file and return actionable information.

    Returns a dict with keys: category, summary, root_cause_line,
    total_severe_errors, diagnosis_suggestion, severity.
    """
    error_summary = ""
    try:
        with err_path.open() as f:
            for line in f:
                if "  * Severe" in line or "** Severe" in line:
                    error_summary = line.strip()
                    break
    except (OSError, UnicodeDecodeError):
        log.warning("Could not read error file: %s", err_path)
        return {
            "category": "generic_severe",
            "summary": "",
            "root_cause_line": "",
            "total_severe_errors": 0,
            "diagnosis_suggestion": _ERROR_CATEGORY_SUGGESTIONS["generic_severe"],
            "severity": "high",
        }

    category = _classify_error_line(error_summary)
    total_severe = _count_severe_errors(err_path)
    root_cause = _find_root_cause_line(err_path)
    suggestion = _ERROR_CATEGORY_SUGGESTIONS.get(
        category, _ERROR_CATEGORY_SUGGESTIONS["generic_severe"]
    )

    return {
        "category": category,
        "summary": error_summary[:500],
        "root_cause_line": root_cause[:500] if root_cause else error_summary[:500],
        "total_severe_errors": total_severe,
        "diagnosis_suggestion": suggestion,
        "severity": "critical" if total_severe > 10 else "high",
    }


@router.get("/api/v1/errors/{sample_id}")  # type: ignore[untyped-decorator]
async def get_sample_error(sid: str, request: Request) -> dict[str, Any]:
    """Get detailed error diagnosis for a failed sample.

    Reads the ``eplusout.err`` file from the sample's simulation directory
    and returns classified error information with actionable suggestions.
    """
    # Validate sample ID to prevent path traversal.
    try:
        safe_sid = sanitize_sample_id(sid)
    except OsimflowValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.app.state.outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")

    outdir_resolved: Path = request.app.state.outdir.resolve()
    sim_dir = outdir_resolved / "work" / "sim" / safe_sid

    # Validate the resolved sim_dir stays within outdir.
    try:
        validate_path_within_base(sim_dir.resolve(), outdir_resolved)
    except OsimflowValidationError:
        raise HTTPException(status_code=400, detail="Invalid sample directory") from None

    err_path = sim_dir / "eplusout.err"
    if not err_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No error file found for sample '{safe_sid}'",
        )

    diagnosis = _diagnose_sample_error(err_path)
    return {
        "sample_id": safe_sid,
        "error_summary": diagnosis["summary"],
        "failure_category": diagnosis["category"],
        "root_cause_line": diagnosis["root_cause_line"],
        "total_severe_errors": diagnosis["total_severe_errors"],
        "diagnosis_suggestion": diagnosis["diagnosis_suggestion"],
        "severity": diagnosis["severity"],
        "log_path": str(err_path),
    }


# ---------------------------------------------------------------------------
# Root redirect (issue #264)
# ---------------------------------------------------------------------------


@router.get("/")  # type: ignore[untyped-decorator]
async def root_redirect() -> RedirectResponse:
    """Redirect root to the web GUI."""
    return RedirectResponse(url="/static/index.html")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(
    outdir: Path | None = None,
    *,
    campaigns_base_dir: Path | None = None,
    read_only: bool = True,
    api_key: str | None = None,
    api_keys_file: Path | None = None,
    cors_origins: list[str] | None = None,
    rate_limit: str = "60/minute",
    ui_enabled: bool = False,
    registry_path: Path | None = None,
    cache_db_path: Path | None = None,
) -> FastAPI:
    """Create the FastAPI application.

    Parameters
    ----------
    outdir
        Path to the campaign output directory containing ``run.json``.
        Used by the legacy single-campaign endpoints and as a fallback
        when ``campaigns_base_dir`` is not set.
    campaigns_base_dir
        Path to the base directory containing multiple campaign
        subdirectories (issue #267).  Each subdirectory is identified
        by its directory name (``campaign_id``) and must contain a
        ``run.json`` to be discoverable.
    read_only
        If ``True`` (default), only GET endpoints are available.
        This is the server-level default for users without explicit roles.
    api_key
        Single API key for authentication (backward-compatible mode).
        When ``None`` and ``api_keys_file`` is not set, authentication
        is disabled.  When set, all non-public endpoints require the
        ``X-API-Key`` header or ``api_key`` query parameter to match.
        In single-key mode, all authenticated users get admin access.
    api_keys_file
        Path to a JSON file containing multiple API keys with per-user
        roles (issue #395).  When set, ``api_key`` is ignored.
        File format::

            {
                "users": [
                    {"key": "api-key-1", "user_id": "alice", "role": "admin"},
                    {"key": "api-key-2", "user_id": "bob", "role": "readonly"}
                ]
            }

        Roles: ``readonly`` (GET only), ``readwrite`` (GET + POST for
        stop/cancel), ``admin`` (full access including campaign creation).
    cors_origins
        List of allowed CORS origins.  When ``None`` or empty, CORS is
        not configured (same-origin only).  Use ``["*"]`` for all
        origins.
    rate_limit
        Rate limit string, e.g. ``"60/minute"`` (default ``"60/minute"``).
    ui_enabled
        Enable the web UI router (issue #337).
    registry_path
        Path to the campaign registry database (issue #404).  When set,
        the ``POST /api/v1/campaigns/compare`` endpoint can resolve
        campaign IDs via the registry.  When ``None``, registry-based
        lookups are disabled and campaign IDs are resolved solely via
        ``campaigns_base_dir``.
    """
    app = FastAPI(
        title="OSimFlow API",
        version="0.1.0",
        description="REST API for monitoring OSimFlow campaigns",
    )

    # --- Build API key store (issue #395) ---
    key_store: MultiUserAPIKeyStore | None = None
    if api_keys_file is not None:
        try:
            keys_data = json.loads(api_keys_file.read_text())
            users = keys_data.get("users", [])
            if not users:
                raise ValueError("No users found in api_keys_file")
            key_store = MultiUserAPIKeyStore.from_users(users)
            log.info("Loaded %d API keys from %s", len(users), api_keys_file)
        except Exception as exc:
            log.error("Failed to load API keys from %s: %s", api_keys_file, exc)
            raise ValueError(f"Invalid api_keys_file: {exc}") from exc
    elif api_key is not None:
        key_store = MultiUserAPIKeyStore.from_single_key(api_key)

    # --- Application state ---
    app.state.outdir = outdir
    app.state.campaigns_base_dir = campaigns_base_dir
    app.state.read_only = read_only
    # Store the key store for the middleware
    app.state.api_key_store = key_store
    # Keep api_key for backward compat
    app.state.api_key = api_key

    # --- Registry for campaign ID resolution (issue #404) ---
    registry: Any = None
    if registry_path is not None:
        try:
            from osimflow.registry import CampaignRegistry  # noqa: PLC0415

            registry = CampaignRegistry(db_path=registry_path)
            log.info("registry loaded from %s", registry_path)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to load registry from %s: %s", registry_path, exc)
    app.state.registry = registry

    # --- Cache for hit rate statistics (issue #426) ---
    cache: Any = None
    if cache_db_path is not None:
        try:
            from osimflow.cache import SQLiteCache  # noqa: PLC0415

            cache = SQLiteCache(cache_db_path)
            log.info("cache loaded from %s", cache_db_path)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to load cache from %s: %s", cache_db_path, exc)
    app.state.cache = cache

    # --- CORS middleware ---
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["X-API-Key", "Content-Type"],
        )

    # --- Rate limiting (issue #454) ---
    # Custom in-process middleware replaces slowapi's SlowAPIMiddleware,
    # which did not reliably maintain its counter between sequential
    # TestClient requests under pytest-xdist in CI.
    #
    # Middleware ordering (add_middleware inserts at front → last added
    # is outermost):
    #   1. add CORS (if configured)
    #   2. add RateLimitMiddleware   ← runs after auth
    #   3. add APIKeyMiddleware      ← outermost, runs first
    # Result: APIKey(RateLimit(CORS(router)))
    app.add_middleware(
        RateLimitMiddleware,
        rate_limit=rate_limit,
        key_func=_get_real_remote_address,
    )

    # Store a reference for tests / introspection.  The actual
    # middleware instance is created lazily by Starlette's
    # add_middleware, so we expose the configured rate-limit string.
    app.state.limiter = rate_limit  # type: ignore[assignment]

    # --- Authentication middleware (outermost — runs first) ---
    app.add_middleware(APIKeyMiddleware)

    # --- Routes ---
    app.include_router(router)
    app.include_router(events_router)
    app.include_router(campaigns_router)
    app.include_router(pat_compat_router)
    app.include_router(files_router)
    app.include_router(timeseries_router)
    app.include_router(variables_router)
    app.include_router(measures_router)

    # --- Web UI router (issue #337) ---
    if ui_enabled:
        app.include_router(ui_router)
        log.info("web UI enabled at /ui/")

    # --- Static files for the web GUI (issue #264) ---
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        log.info("web GUI mounted at /static/ from %s", static_dir)

    return app
