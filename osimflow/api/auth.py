"""Authentication and authorization helpers for the REST API (issue #268, #395).

This module provides:
- API key validation helpers
- Multi-user API key store with per-user permission levels
- Permission checking decorators
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger("osimflow.api.auth")

# Permission levels for multi-user auth (issue #395)
_READONLY = "readonly"
_READWRITE = "readwrite"
_ADMIN = "admin"

# All permission levels in order of increasing access
_PERMISSION_LEVELS = (_READONLY, _READWRITE, _ADMIN)


def _has_permission(user_role: str | None, required: str) -> bool:
    """Check if user_role satisfies the required permission level.

    Permissions are hierarchical: admin > readwrite > readonly.
    """
    if user_role is None:
        return False
    try:
        user_level = _PERMISSION_LEVELS.index(user_role)
        required_level = _PERMISSION_LEVELS.index(required)
        return user_level >= required_level
    except ValueError:
        return False


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


class APIKeyUser:
    """Represents an authenticated API user with their permission level (issue #395)."""

    __slots__ = ("key", "user_id", "role")

    def __init__(self, *, key: str, user_id: str, role: str) -> None:
        self.key = key
        self.user_id = user_id
        self.role = role

    def has_permission(self, required: str) -> bool:
        """Check if this user has at least the required permission level."""
        return _has_permission(self.role, required)


class MultiUserAPIKeyStore:
    """Store for multiple API keys with per-user permissions (issue #395).

    This enables multi-user deployments where each user has their own API key
    and a role that determines their access level.

    The store supports two modes:
    - Single key mode: When ``single_key`` is set, validates against that one key
      with the server's global read_only setting.
    - Multi-user mode: When ``users`` is populated, validates against the list
      of users and respects per-user roles.
    """

    __slots__ = ("single_key", "users")

    def __init__(
        self,
        *,
        single_key: str | None = None,
        users: list[dict[str, str]] | None = None,
    ) -> None:
        self.single_key = single_key
        self.users = users or []

    @classmethod
    def from_single_key(cls, key: str | None) -> MultiUserAPIKeyStore:
        """Create a store with a single API key (backward-compatible mode)."""
        return cls(single_key=key)

    @classmethod
    def from_users(cls, users: list[dict[str, str]]) -> MultiUserAPIKeyStore:
        """Create a store with multiple users (issue #395)."""
        return cls(users=users)

    @classmethod
    def from_file(cls, file_path: Path) -> MultiUserAPIKeyStore:
        """Load API keys from a JSON file (issue #395).

        File format::

            {
                "users": [
                    {"key": "api-key-1", "user_id": "alice", "role": "admin"},
                    {"key": "api-key-2", "user_id": "bob", "role": "readonly"}
                ]
            }

        Raises
        ------
        ValueError
            If the file does not have a ``.json`` or ``.keys`` extension,
            is not a regular file (e.g. a symlink to ``/dev/null`` or
            ``/etc/passwd``), cannot be read, or contains invalid JSON.
        """
        resolved = file_path.resolve()
        if not resolved.is_file():
            raise ValueError(f"api_keys_file must be a regular file, got {resolved}")
        if resolved.suffix not in (".json", ".keys"):
            raise ValueError(
                f"api_keys_file must have .json or .keys extension, got {resolved.suffix!r}"
            )
        try:
            keys_data = json.loads(resolved.read_text())
        except json.JSONDecodeError as exc:
            log.error("Failed to parse JSON from %s: %s", resolved, exc)
            raise ValueError(f"Invalid JSON in api_keys_file: {exc}") from exc
        except OSError as exc:
            log.error("Failed to read %s: %s", resolved, exc)
            raise ValueError(f"Cannot read api_keys_file: {exc}") from exc
        users = keys_data.get("users", [])
        if not users:
            raise ValueError("No users found in api_keys_file")
        return cls.from_users(users)

    def validate(self, provided_key: str | None) -> APIKeyUser | None:
        """Validate an API key and return the user if valid.

        Uses constant-time comparison to prevent timing attacks.
        Returns ``None`` if the key is invalid.
        """
        if provided_key is None:
            return None

        # Check single key mode first (backward compatible)
        if self.single_key is not None:
            if validate_api_key(provided_key, self.single_key):
                # In single-key mode, role is determined by server's read_only setting
                # (checked by get_user_permission() via request.app.state.read_only).
                # Return None so callers cannot infer a specific role.
                return None
            return None

        # Multi-user mode
        for user in self.users:
            if validate_api_key(provided_key, user["key"]):
                return APIKeyUser(
                    key=provided_key,
                    user_id=user.get("user_id", "unknown"),
                    role=user.get("role", _READONLY),
                )
        return None

    def get_user_role(self, provided_key: str | None) -> str | None:
        """Get the role for a provided API key, or None if invalid."""
        user = self.validate(provided_key)
        return user.role if user else None


# ---------------------------------------------------------------------------
# Permission checking helpers
# ---------------------------------------------------------------------------


def get_user_permission(request: Request, required: str) -> bool:
    """Check if the authenticated user has the required permission level (issue #395).

    In multi-user mode, uses the per-user role from the API key.
    In single-key or no-auth mode, uses the server-level read_only setting.

    Parameters
    ----------
    request
        The FastAPI request object with ``state.api_user`` set by the middleware.
    required
        Required permission level: ``readonly``, ``readwrite``, or ``admin``.

    Returns
    -------
    bool
        True if the user has sufficient permissions.
    """
    api_user: APIKeyUser | None = getattr(request.state, "api_user", None)

    if api_user is None:
        # No auth configured or single-key auth (api_user=None defers to server read_only).
        # Fall back to server-level read_only setting.
        return not getattr(request.app.state, "read_only", True)

    # Multi-user mode: check per-user role
    return api_user.has_permission(required)


def require_permission(required: str) -> Any:
    """Decorator that checks if the user has the required permission level.

    Usage::

        @events_router.post("/endpoint")
        @require_permission("readwrite")
        async def endpoint(request: Request) -> dict[str, str]:
            ...
    """

    def decorator(func: Any) -> Any:
        async def wrapper(*args: Any, **kwargs: Any) -> Response:
            # Assume request is the last positional arg or in kwargs
            request_obj: Request | None = None
            for arg in args:
                if isinstance(arg, Request):
                    request_obj = arg
                    break
            if request_obj is None:
                request_obj = kwargs.get("request")

            if request_obj is None:
                return JSONResponse(
                    status_code=500,
                    content={"detail": "Internal error: request not found"},
                )

            if not get_user_permission(request_obj, required):
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"{required.capitalize()} permission required"},
                )

            return await func(*args, **kwargs)  # type: ignore[no-any-return]

        return wrapper

    return decorator
