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
from pydantic import BaseModel, Field

from osimflow.api.auth import get_user_permission
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


@variables_router.get("/api/v1/variables", response_model=VariableListResponse)
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


@variables_router.get("/api/v1/variables/{var_name}", response_model=VariableDetailResponse)
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


@variables_router.post(
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
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="Variable creation requires readwrite permission",
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


@variables_router.put("/api/v1/variables/{var_name}", response_model=VariableDetailResponse)
async def update_variable(
    var_name: str,
    body: VariableUpdateRequest,
    request: Request,
) -> VariableDetailResponse:
    """Update an existing variable in the campaign's variables.yml.

    Raises 404 if the variable does not exist.  Raises 403 in read-only mode.
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="Variable update requires readwrite permission",
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


@variables_router.delete("/api/v1/variables/{var_name}", response_model=VariableDeleteResponse)
async def delete_variable(
    var_name: str,
    request: Request,
) -> VariableDeleteResponse:
    """Delete a variable from the campaign's variables.yml.

    Raises 404 if the variable does not exist.  Raises 403 in read-only mode.
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="Variable deletion requires readwrite permission",
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
# POST /api/v1/variables/batch_update — atomic batch update
# ---------------------------------------------------------------------------


class VariableBatchUpdateItem(BaseModel):
    """One item in a batch variable update request."""

    name: str = Field(description="Variable name to update")
    rename_to: str | None = Field(
        default=None,
        description="New name for the variable (optional). When provided, the variable is renamed.",
    )
    distribution: str | None = Field(default=None, description="Distribution type")
    description: str | None = Field(default=None)
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    sigma: float | None = None
    mode: float | None = None
    values: list[Any] | None = None
    alpha: float | None = None
    beta: float | None = None
    rate: float | None = None
    target: str | None = None
    mapping: dict[str, Any] | None = None


class VariableBatchUpdateRequest(BaseModel):
    """Request body for ``POST /api/v1/variables/batch_update``."""

    variables: list[VariableBatchUpdateItem] = Field(
        description="List of variable updates to apply atomically"
    )


class VariableBatchUpdateError(BaseModel):
    """Error detail for a single failed variable update in a batch."""

    name: str = Field(description="Variable name that failed")
    error: str = Field(description="Error message")


class VariableBatchUpdateResponse(BaseModel):
    """Response for ``POST /api/v1/variables/batch_update``."""

    updated: list[VariableDetailResponse] = Field(
        default_factory=list,
        description="List of successfully updated variables",
    )
    errors: list[VariableBatchUpdateError] = Field(
        default_factory=list,
        description="List of failed variable updates",
    )


@variables_router.post(
    "/api/v1/variables/batch_update",
    response_model=VariableBatchUpdateResponse,
)
async def batch_update_variables(  # noqa: PLR0912
    body: VariableBatchUpdateRequest,
    request: Request,
) -> VariableBatchUpdateResponse:
    """Atomically update multiple variables in the campaign's variables.yml.

    All variables are validated before any are updated.  If any variable
    is invalid (not found, unknown distribution, missing required parameters),
    the entire batch is rejected with a 400 error listing all failures.

    Returns 403 in read-only mode.
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="Variable batch update requires readwrite permission",
        )

    yml_path = _variables_yml_path(request)
    variables = _load_variables(yml_path)

    # Build a name → index map for fast lookup
    name_to_idx: dict[str, int] = {}
    for i, v in enumerate(variables):
        var_name = v.get("name", "")
        if var_name:
            name_to_idx[var_name] = i

    # Phase 1: validate all updates before applying any
    # Two error types:
    #   - phase1a_errors: non-fatal rename conflicts — let other items proceed
    #   - phase1b_errors: fatal validation failures — return early (atomic reject)
    phase1a_errors: list[VariableBatchUpdateError] = []
    phase1b_errors: list[VariableBatchUpdateError] = []
    # Keep track of which names would be used after updates for conflict checking
    post_update_names: set[str] = set()

    # Track per-item computed data for Phase 2
    # item_key -> {"is_rename": bool, "idx": int, "final_name": str, "updated": dict}
    item_data: dict[int, dict[str, Any]] = {}
    # Track items that failed Phase 1a so Phase 2/3 can skip them
    phase1a_failed: set[int] = set()

    for item in body.variables:
        var_name = item.name
        item_key = id(item)

        # Check if variable exists
        if var_name not in name_to_idx:
            phase1b_errors.append(
                VariableBatchUpdateError(
                    name=var_name,
                    error=f"Variable '{var_name}' not found",
                )
            )
            continue

        # Detect whether this item performs a rename: rename_to is set
        is_rename = item.rename_to is not None

        # Phase 1a: detect within-batch name conflicts for renames
        if is_rename:
            if item.rename_to in post_update_names:
                phase1a_errors.append(
                    VariableBatchUpdateError(
                        name=var_name,
                        error=f"Name conflict: '{item.rename_to}' already updated in this batch",
                    )
                )
                phase1a_failed.add(item_key)
                continue
            assert item.rename_to is not None
            post_update_names.add(item.rename_to)

        # Build the updated variable dict
        idx = name_to_idx[var_name]
        updated: dict[str, Any] = dict(variables[idx])

        # Apply updates - use item attributes
        if item.distribution is not None:
            if item.distribution not in VALID_DISTRIBUTIONS:
                phase1b_errors.append(
                    VariableBatchUpdateError(
                        name=var_name,
                        error=f"Unknown distribution '{item.distribution}'. Valid: {', '.join(sorted(VALID_DISTRIBUTIONS))}",
                    )
                )
                continue
            updated["distribution"] = item.distribution

        # Copy all non-None fields from the item (including rename via rename_to)
        for key in (
            "name",
            "rename_to",
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
            value = getattr(item, key, None)
            if value is not None:
                updated[key] = value

        # Apply rename if specified
        if is_rename:
            updated["name"] = item.rename_to

        # Phase 1b: validate the updated variable
        try:
            _validate_single_var_for_api(updated)
        except HTTPException as exc:
            phase1b_errors.append(VariableBatchUpdateError(name=var_name, error=str(exc.detail)))
            continue
        except Exception as exc:
            phase1b_errors.append(VariableBatchUpdateError(name=var_name, error=str(exc)))
            continue

        # Compute final name for this item (needed for Phase 2 cross-item check)
        final_name = item.rename_to if is_rename else updated.get("name", var_name)
        item_data[item_key] = {
            "is_rename": is_rename,
            "idx": idx,
            "final_name": final_name,
            "updated": updated,
            "var_name": var_name,
        }

    # Phase 1b errors are fatal — reject entire batch atomically
    if phase1b_errors:
        return VariableBatchUpdateResponse(errors=phase1b_errors)

    # Phase 1a errors are non-fatal — let other items proceed through Phase 2

    # Phase 2: cross-item conflict check for renames.
    # Check that each rename target is not an original variable name that is NOT
    # being renamed away in this batch. Phase 2 only runs for items that passed
    # Phase 1a (stored in item_data).
    existing_var_names = {v["name"] for v in variables}
    # Map: original_name -> final_name (None if being renamed away)
    original_to_final: dict[str, str | None] = {}
    for item in body.variables:
        item_info = item_data.get(id(item))
        if item_info is None:
            continue  # Phase 1a failed
        if item_info["is_rename"]:
            original_to_final[item.name] = item_info["final_name"]
        else:
            original_to_final[item.name] = None  # not renamed

    phase2_errors: list[VariableBatchUpdateError] = []
    phase2_passed_items: set[int] = set(item_data.keys())
    for item in body.variables:
        item_key = id(item)
        if item_key not in phase2_passed_items:
            continue  # Phase 1a failed
        item_info = item_data[item_key]
        if item_info["is_rename"]:
            final_name = item_info["final_name"]
            # If final_name is an original variable name, that original must be
            # being renamed away in this batch (so the name becomes available)
            if final_name in existing_var_names:
                original_var_final = original_to_final.get(final_name)
                if original_var_final is not None:
                    # final_name is an existing variable that is NOT being renamed
                    phase2_errors.append(
                        VariableBatchUpdateError(
                            name=item_info["var_name"],
                            error=f"Variable '{final_name}' already exists",
                        )
                    )
                    break

    # Phase 2 errors are fatal — reject entire batch
    if phase2_errors:
        return VariableBatchUpdateResponse(errors=phase2_errors)

    # Phase 3: apply all updates (items that passed Phase 1a and Phase 2)
    updated_vars: list[VariableDetailResponse] = []
    for item in body.variables:
        item_key = id(item)
        if item_key not in phase2_passed_items:
            continue  # skip items that failed Phase 1a or Phase 2
        var_name = item.name
        idx = name_to_idx[var_name]
        updated = dict(variables[idx])

        if item.distribution is not None:
            updated["distribution"] = item.distribution

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
            value = getattr(item, key, None)
            if value is not None:
                updated[key] = value

        # Apply rename if specified
        if item.rename_to is not None:
            updated["name"] = item.rename_to

        variables[idx] = updated
        updated_vars.append(
            VariableDetailResponse(
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
        )

    _save_variables(yml_path, variables)
    _invalidate_lhs_cache(request)

    log.info("batch updated %d variables in %s", len(updated_vars), yml_path)

    return VariableBatchUpdateResponse(updated=updated_vars, errors=phase1a_errors)


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
