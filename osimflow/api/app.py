"""FastAPI application for OSimFlow campaign monitoring.

Security features (issue #268):
- API key authentication (``X-API-Key`` header or ``api_key`` query param)
- CORS middleware with configurable origins
- Rate limiting via slowapi (default 60/minute)
- Read-only mode by default; ``--enable-writes`` required for mutations
- ``/health`` remains unauthenticated for load balancer probes
"""

import json
import logging
import secrets
from pathlib import Path
from typing import Any, cast

import pandas as pd
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

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
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For can contain multiple IPs: client, proxy1, proxy2...
        # The first IP is the original client.
        client_ip = forwarded_for.split(",")[0].strip()
        if client_ip:
            return client_ip
    return get_remote_address(request)

router = APIRouter()

PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/", "/static/index.html", ""})


# ---------------------------------------------------------------------------
# Auth helpers (issue #268)
# ---------------------------------------------------------------------------


def generate_api_key() -> str:
    """Generate a cryptographically secure API key.

    Returns a URL-safe base64 string (~43 characters of entropy).
    """
    return secrets.token_urlsafe(32)


def extract_api_key(request: Request) -> str | None:
    """Extract the API key from a request.

    Checks the ``X-API-Key`` header first, then the ``api_key`` query
    parameter.  Returns ``None`` if neither is present.
    """
    header_key = request.headers.get("X-API-Key")
    if header_key:
        return str(header_key)
    query_key = request.query_params.get("api_key")
    if query_key:
        return str(query_key)
    return None


def validate_api_key(provided: str | None, expected: str) -> bool:
    """Validate *provided* against *expected* using constant-time comparison.

    Returns ``True`` if the keys match, ``False`` otherwise (including when
    *provided* is ``None``).
    """
    if provided is None:
        return False
    return secrets.compare_digest(provided, expected)


class APIKeyMiddleware(BaseHTTPMiddleware):
    """HTTP middleware that enforces API key authentication.

    Requests to paths in :data:`PUBLIC_PATHS` always pass through.
    When ``app.state.api_key`` is ``None``, authentication is disabled
    (backward-compatible with pre-#268 callers).
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Any,  # noqa: ANN401  (starlette callable protocol)
    ) -> Response:
        normalised = request.url.path.rstrip("/")
        if normalised in PUBLIC_PATHS:
            return cast(Response, await call_next(request))

        expected_key: str | None = getattr(request.app.state, "api_key", None)
        if expected_key is None:
            return cast(Response, await call_next(request))

        provided = extract_api_key(request)
        if not validate_api_key(provided, expected_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )
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
    cors_origins: list[str] | None = None,
    rate_limit: str = "60/minute",
    ui_enabled: bool = False,
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
    api_key
        API key for authentication.  When ``None``, authentication is
        disabled.  When set, all non-public endpoints require the
        ``X-API-Key`` header or ``api_key`` query parameter to match.
    cors_origins
        List of allowed CORS origins.  When ``None`` or empty, CORS is
        not configured (same-origin only).  Use ``["*"]`` for all
        origins.
    rate_limit
        Rate limit string for slowapi (default ``"60/minute"``).
    """
    app = FastAPI(
        title="OSimFlow API",
        version="0.1.0",
        description="REST API for monitoring OSimFlow campaigns",
    )

    # --- Application state ---
    app.state.outdir = outdir
    app.state.campaigns_base_dir = campaigns_base_dir
    app.state.read_only = read_only
    app.state.api_key = api_key

    # --- CORS middleware ---
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["X-API-Key", "Content-Type"],
        )

    # --- Rate limiting (slowapi) ---
    limiter = Limiter(key_func=_get_real_remote_address, default_limits=[rate_limit])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)

    # --- Authentication middleware (innermost user middleware) ---
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
