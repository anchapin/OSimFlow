"""Variable management API endpoints (issue #347).

Provides CRUD operations for runtime variable management:
  - GET  /api/v1/variables          — list all variables
  - GET  /api/v1/variables/{name}    — get a single variable
  - POST /api/v1/variables           — add a new variable
  - PUT  /api/v1/variables/{name}    — update a variable
  - DELETE /api/v1/variables/{name}  — delete a variable

Variables are stored in the campaign's ``variables.yml`` file.
When variables are modified, the GENERATE_LHS_SAMPLES cache entries
are invalidated so the next campaign run re-generates samples.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request

from osimflow.api.schemas import (
    VariableDeleteResponse,
    VariableDetailResponse,
    VariableListResponse,
    VariableSummary,
    VariableUpdateRequest,
)
from osimflow.validation import (
    DISTRIBUTION_PARAMS,
    VALID_DISTRIBUTIONS,
)

log = logging.getLogger("osimflow.api.variables")

variables_router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _variables_yml_path(request: Request) -> Path:
    """Return the path to the campaign's variables.yml.

    Uses ``campaigns_base_dir`` when set, otherwise falls back to ``outdir``.
    Raises 503 if neither is configured.
    """
    base: Path | None = getattr(request.app.state, "campaigns_base_dir", None)
    if base is None:
        base = getattr(request.app.state, "outdir", None)
    if base is None:
        raise HTTPException(status_code=503, detail="No output directory configured")
    var_path = base / "variables.yml"
    return var_path


def _load_variables(yml_path: Path) -> list[dict[str, Any]]:
    """Load and return the variables list from a variables.yml file.

    Returns an empty list if the file does not exist yet.
    Raises :class:`HTTPException` (500) on parse errors.
    """
    if not yml_path.exists():
        return []
    try:
        data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to parse variables.yml: {exc}",
        ) from exc
    if not isinstance(data, dict):
        return []
    variables: Any = data.get("variables", [])
    if not isinstance(variables, list):
        return []
    return variables


def _save_variables(yml_path: Path, variables: list[dict[str, Any]]) -> None:
    """Write the variables list back to a variables.yml file.

    Creates the file (and any missing parent directories) if it does not exist.
    """
    yml_path.parent.mkdir(parents=True, exist_ok=True)
    document = {"variables": variables}
    yml_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    log.info("saved %d variables to %s", len(variables), yml_path)


def _invalidate_lhs_cache(request: Request) -> None:
    """Drop GENERATE_LHS_SAMPLES cache entries for the campaign.

    Called after any variable mutation so the next run re-generates samples.
    """
    base: Path | None = getattr(request.app.state, "campaigns_base_dir", None)
    if base is None:
        base = getattr(request.app.state, "outdir", None)
    if base is None:
        return  # Nothing to invalidate

    cache_db = base / "work" / "cache.sqlite"
    if not cache_db.exists():
        return

    try:
        conn = sqlite3.connect(cache_db, timeout=10.0)
        cur = conn.execute(
            "DELETE FROM cache_entries WHERE step=?",
            ("GENERATE_LHS_SAMPLES",),
        )
        conn.commit()
        conn.close()
        if cur.rowcount > 0:
            log.info(
                "cache INVALIDATE GENERATE_LHS_SAMPLES (%d rows) after variable mutation",
                cur.rowcount,
            )
    except Exception as exc:
        log.warning("failed to invalidate LHS cache: %s", exc)


def _var_to_summary(var: dict[str, Any]) -> VariableSummary:
    """Convert a variable dict to a VariableSummary."""
    return VariableSummary(
        name=var.get("name", ""),
        distribution=var.get("distribution", ""),
        description=var.get("description"),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/variables — list all variables
# ---------------------------------------------------------------------------


@variables_router.get("/api/v1/variables", response_model=VariableListResponse)  # type: ignore[untyped-decorator]
async def list_variables(request: Request) -> VariableListResponse:
    """List all variables from the campaign's variables.yml.

    Returns an empty list if no variables.yml exists yet.
    """
    yml_path = _variables_yml_path(request)
    variables = _load_variables(yml_path)
    summaries = [_var_to_summary(v) for v in variables]
    return VariableListResponse(variables=summaries, total=len(summaries))


# ---------------------------------------------------------------------------
# GET /api/v1/variables/{name} — get a single variable
# ---------------------------------------------------------------------------


@variables_router.get("/api/v1/variables/{var_name}", response_model=VariableDetailResponse)  # type: ignore[untyped-decorator]
async def get_variable(var_name: str, request: Request) -> VariableDetailResponse:
    """Get full details for a single variable by name.

    Raises 404 if the variable is not found.
    """
    yml_path = _variables_yml_path(request)
    variables = _load_variables(yml_path)
    for var in variables:
        if var.get("name") == var_name:
            return VariableDetailResponse(
                name=var.get("name", ""),
                distribution=var.get("distribution", ""),
                description=var.get("description"),
                min=var.get("min"),
                max=var.get("max"),
                mean=var.get("mean"),
                sigma=var.get("sigma"),
                mode=var.get("mode"),
                values=var.get("values"),
                alpha=var.get("alpha"),
                beta=var.get("beta"),
                rate=var.get("rate"),
                target=var.get("target"),
                mapping=var.get("mapping"),
            )
    raise HTTPException(status_code=404, detail=f"Variable '{var_name}' not found")


# ---------------------------------------------------------------------------
# POST /api/v1/variables — add a new variable
# ---------------------------------------------------------------------------


@variables_router.post(  # type: ignore[untyped-decorator]
    "/api/v1/variables",
    response_model=VariableDetailResponse,
    status_code=201,
)
async def create_variable(
    body: dict[str, Any],
    request: Request,
) -> VariableDetailResponse:
    """Add a new variable to the campaign's variables.yml.

    Validates the variable definition against the same schema used by
    ``validate_variables_yml``.  Returns 403 in read-only mode.
    """
    if getattr(request.app.state, "read_only", True):
        raise HTTPException(
            status_code=403,
            detail="Variable creation requires --enable-writes mode",
        )

    yml_path = _variables_yml_path(request)
    variables = _load_variables(yml_path)

    # Check for duplicate name
    var_name = body.get("name")
    if not var_name or not isinstance(var_name, str):
        raise HTTPException(status_code=400, detail="'name' is required and must be a string")
    for existing in variables:
        if existing.get("name") == var_name:
            raise HTTPException(
                status_code=409,
                detail=f"Variable '{var_name}' already exists",
            )

    # Validate distribution
    dist = body.get("distribution")
    if not dist or not isinstance(dist, str):
        raise HTTPException(
            status_code=400,
            detail="'distribution' is required and must be a string",
        )
    if dist not in VALID_DISTRIBUTIONS:
        available = ", ".join(sorted(VALID_DISTRIBUTIONS))
        raise HTTPException(
            status_code=400,
            detail=f"Unknown distribution '{dist}'. Valid: {available}",
        )

    # Build the new variable entry, keeping all fields the user provided
    new_var: dict[str, Any] = {"name": var_name, "distribution": dist}
    for key, value in body.items():
        if key not in ("name", "distribution"):
            new_var[key] = value

    # Validate the full variable definition
    try:
        _validate_single_var_for_api(new_var)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    variables.append(new_var)
    _save_variables(yml_path, variables)
    _invalidate_lhs_cache(request)

    log.info("created variable '%s' in %s", var_name, yml_path)

    return VariableDetailResponse(
        name=new_var.get("name", ""),
        distribution=new_var.get("distribution", ""),
        description=new_var.get("description"),
        min=new_var.get("min"),
        max=new_var.get("max"),
        mean=new_var.get("mean"),
        sigma=new_var.get("sigma"),
        mode=new_var.get("mode"),
        values=new_var.get("values"),
        alpha=new_var.get("alpha"),
        beta=new_var.get("beta"),
        rate=new_var.get("rate"),
        target=new_var.get("target"),
        mapping=new_var.get("mapping"),
    )


# ---------------------------------------------------------------------------
# PUT /api/v1/variables/{name} — update a variable
# ---------------------------------------------------------------------------


@variables_router.put("/api/v1/variables/{var_name}", response_model=VariableDetailResponse)  # type: ignore[untyped-decorator]
async def update_variable(
    var_name: str,
    body: VariableUpdateRequest,
    request: Request,
) -> VariableDetailResponse:
    """Update an existing variable in the campaign's variables.yml.

    Raises 404 if the variable does not exist.  Raises 403 in read-only mode.
    """
    if getattr(request.app.state, "read_only", True):
        raise HTTPException(
            status_code=403,
            detail="Variable update requires --enable-writes mode",
        )

    yml_path = _variables_yml_path(request)
    variables = _load_variables(yml_path)

    # Find the variable index
    idx = next((i for i, v in enumerate(variables) if v.get("name") == var_name), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Variable '{var_name}' not found")

    # Build updated variable — copy all fields, override with non-None body fields
    updated: dict[str, Any] = dict(variables[idx])
    _apply_variable_updates(updated, body)

    # Validate the updated variable
    _validate_single_var_for_api(updated)

    # Check for name conflicts after update
    new_name = updated["name"]
    if any(i != idx and v.get("name") == new_name for i, v in enumerate(variables)):
        raise HTTPException(status_code=409, detail=f"Variable '{new_name}' already exists")

    variables[idx] = updated
    _save_variables(yml_path, variables)
    _invalidate_lhs_cache(request)

    log.info("updated variable '%s' in %s", var_name, yml_path)

    return VariableDetailResponse(
        name=updated.get("name", ""),
        distribution=updated.get("distribution", ""),
        description=updated.get("description"),
        min=updated.get("min"),
        max=updated.get("max"),
        mean=updated.get("mean"),
        sigma=updated.get("sigma"),
        mode=updated.get("mode"),
        values=updated.get("values"),
        alpha=updated.get("alpha"),
        beta=updated.get("beta"),
        rate=updated.get("rate"),
        target=updated.get("target"),
        mapping=updated.get("mapping"),
    )


# ---------------------------------------------------------------------------
# DELETE /api/v1/variables/{name} — delete a variable
# ---------------------------------------------------------------------------


@variables_router.delete("/api/v1/variables/{var_name}", response_model=VariableDeleteResponse)  # type: ignore[untyped-decorator]
async def delete_variable(
    var_name: str,
    request: Request,
) -> VariableDeleteResponse:
    """Delete a variable from the campaign's variables.yml.

    Raises 404 if the variable does not exist.  Raises 403 in read-only mode.
    """
    if getattr(request.app.state, "read_only", True):
        raise HTTPException(
            status_code=403,
            detail="Variable deletion requires --enable-writes mode",
        )

    yml_path = _variables_yml_path(request)
    variables = _load_variables(yml_path)

    idx = None
    for i, var in enumerate(variables):
        if var.get("name") == var_name:
            idx = i
            break
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Variable '{var_name}' not found")

    variables.pop(idx)
    _save_variables(yml_path, variables)
    _invalidate_lhs_cache(request)

    log.info("deleted variable '%s' from %s", var_name, yml_path)

    return VariableDeleteResponse(name=var_name, status="deleted")


# ---------------------------------------------------------------------------
# Validation helpers (mirrors validation.py logic for API use)
# ---------------------------------------------------------------------------


def _apply_variable_updates(
    updated: dict[str, Any],
    body: VariableUpdateRequest,
) -> None:
    """Apply non-None fields from body to updated variable dict."""
    # Fields that need distribution validation before being set
    if body.distribution is not None:
        if body.distribution not in VALID_DISTRIBUTIONS:
            available = ", ".join(sorted(VALID_DISTRIBUTIONS))
            raise HTTPException(
                status_code=400,
                detail=f"Unknown distribution '{body.distribution}'. Valid: {available}",
            )
        updated["distribution"] = body.distribution

    # Simple field copies — None fields are skipped
    for key in (
        "name",
        "description",
        "min",
        "max",
        "mean",
        "sigma",
        "mode",
        "values",
        "alpha",
        "beta",
        "rate",
        "target",
        "mapping",
    ):
        value = getattr(body, key, None)
        if value is not None:
            updated[key] = value


def _validate_single_var_for_api(var: dict[str, Any]) -> None:
    """Validate a single variable definition for API mutations.

    Raises :class:`HTTPException` (400) on validation failure.
    """
    if not var.get("name"):
        raise HTTPException(status_code=400, detail="Variable 'name' is required")
    dist = var.get("distribution", "")
    if not dist:
        raise HTTPException(status_code=400, detail="Variable 'distribution' is required")
    if dist not in VALID_DISTRIBUTIONS:
        available = ", ".join(sorted(VALID_DISTRIBUTIONS))
        raise HTTPException(
            status_code=400,
            detail=f"Unknown distribution '{dist}'. Valid: {available}",
        )

    required_params = DISTRIBUTION_PARAMS[dist]
    for param in required_params:
        if param not in var:
            raise HTTPException(
                status_code=400,
                detail=f"Distribution '{dist}' requires parameter '{param}'",
            )

    # Dispatch to distribution-specific validator
    _VALIDATORS[dist](var)


# ---------------------------------------------------------------------------
# Per-distribution validators
# ---------------------------------------------------------------------------


def _v_uniform(var: dict[str, Any]) -> None:
    if not isinstance(var.get("min"), (int, float)) or not isinstance(var.get("max"), (int, float)):
        raise HTTPException(status_code=400, detail="'min' and 'max' must be numeric")
    if var["min"] >= var["max"]:
        raise HTTPException(
            status_code=400,
            detail=f"'min' ({var['min']}) must be less than 'max' ({var['max']})",
        )


def _v_normal_lognormal(var: dict[str, Any]) -> None:
    if not isinstance(var.get("mean"), (int, float)) or not isinstance(
        var.get("sigma"), (int, float)
    ):
        raise HTTPException(status_code=400, detail="'mean' and 'sigma' must be numeric")
    if var["sigma"] <= 0:
        raise HTTPException(status_code=400, detail="'sigma' must be positive")


def _v_triangular(var: dict[str, Any]) -> None:
    if not isinstance(var.get("min"), (int, float)) or not isinstance(var.get("max"), (int, float)):
        raise HTTPException(status_code=400, detail="'min' and 'max' must be numeric")
    if var["min"] >= var["max"]:
        raise HTTPException(
            status_code=400,
            detail=f"'min' ({var['min']}) must be less than 'max' ({var['max']})",
        )
    if "mode" in var and not isinstance(var["mode"], (int, float)):
        raise HTTPException(status_code=400, detail="'mode' must be numeric")


def _v_discrete_categorical(var: dict[str, Any]) -> None:
    values = var.get("values")
    if not isinstance(values, list) or len(values) == 0:
        raise HTTPException(status_code=400, detail="'values' must be a non-empty list")


def _v_beta(var: dict[str, Any]) -> None:
    if not isinstance(var.get("alpha"), (int, float)) or not isinstance(
        var.get("beta"), (int, float)
    ):
        raise HTTPException(status_code=400, detail="'alpha' and 'beta' must be numeric")
    if var["alpha"] <= 0 or var["beta"] <= 0:
        raise HTTPException(status_code=400, detail="'alpha' and 'beta' must be positive")


def _v_gamma(var: dict[str, Any]) -> None:
    if not isinstance(var.get("alpha"), (int, float)):
        raise HTTPException(status_code=400, detail="'alpha' must be numeric")
    if var["alpha"] <= 0:
        raise HTTPException(status_code=400, detail="'alpha' must be positive")


def _v_exponential(var: dict[str, Any]) -> None:
    if not isinstance(var.get("rate"), (int, float)):
        raise HTTPException(status_code=400, detail="'rate' must be numeric")
    if var["rate"] <= 0:
        raise HTTPException(status_code=400, detail="'rate' must be positive")


_VALIDATORS: dict[str, Callable[..., Any]] = {
    "uniform": _v_uniform,
    "normal": _v_normal_lognormal,
    "lognormal": _v_normal_lognormal,
    "triangular": _v_triangular,
    "discrete": _v_discrete_categorical,
    "categorical": _v_discrete_categorical,
    "beta": _v_beta,
    "gamma": _v_gamma,
    "exponential": _v_exponential,
}
