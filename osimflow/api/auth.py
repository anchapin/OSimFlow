"""Authentication and authorization helpers for the REST API (issue #268, #395).

This module provides:
- API key validation helpers
- Multi-user API key store with per-user permission levels
  (keys hashed at rest, issue #1552)
- Permission checking decorators
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import Request
from starlette.responses import JSONResponse, Response

from osimflow.errors import OSimFlowValueError

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


def hash_api_key(key: str) -> str:
    """Return the hex SHA-256 digest of an API key (issue #1552).

    Keys are stored *hashed at rest* in ``api_keys_file`` — a leak of
    the file itself (home-directory tarball, backup, accidental commit)
    discloses no reusable credential.  The digest is unsalted by
    design: keys are high-entropy ``secrets.token_urlsafe(32)`` values
    (not low-entropy passwords), lookups are exact-match by presented
    key, and a per-user salt would break the fixed-position digest
    comparison that keeps :meth:`MultiUserAPIKeyStore.validate` flat.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _is_sha256_hex(value: object) -> bool:
    """Return True when *value* looks like a 64-char lowercase-hex digest."""
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


# Migration pointer embedded in every fail-closed plaintext rejection
# (issue #1552).  Points operators at the documented one-liner.
API_KEYS_FILE_MIGRATION_HINT = (
    'api_keys_file must store keys hashed at rest as "key_sha256" '
    '(sha256 hex digest, issue #1552); plaintext "key" entries are no '
    "longer accepted. Migrate an existing file with the one-liner under "
    "docs/secret-management.md §'Hashed API keys at rest (issue #1552)'."
)


class APIKeyQueryParameterError(RuntimeError):
    """An API key was supplied via the ``api_key`` query parameter (issue #1466).

    Query strings are recorded by reverse proxies, access logs, browser
    history, and ``Referer`` headers, so the query channel turned
    bearer-equivalent credentials into durable log artifacts on every
    hop.  Key extraction is header-only (:func:`extract_api_key`); this
    error carries the migration hint the API returns as a 401 so the
    client knows to switch to the ``X-API-Key`` header.
    """


# Migration hint returned to clients still using the removed query channel.
API_KEY_QUERY_PARAM_MIGRATION_HINT = (
    "api_key query parameter is no longer accepted; pass the X-API-Key header instead"
)


def extract_api_key(request: Request) -> str | None:
    """Extract the API key from a request — header-only (issue #268, #1466).

    The ``X-API-Key`` header is the sole accepted transport.  When an
    ``api_key`` query parameter is present but the header is absent,
    raises :class:`APIKeyQueryParameterError` so callers reject the
    request with a 401 and the migration hint — query strings leak into
    proxy/access logs, browser history, and ``Referer`` headers.

    Returns ``None`` when no key is supplied via the header.
    """
    header_key = request.headers.get("X-API-Key")
    if header_key:
        return str(header_key)
    query_key = request.query_params.get("api_key")
    if query_key:
        raise APIKeyQueryParameterError(API_KEY_QUERY_PARAM_MIGRATION_HINT)
    return None


def validate_api_key(provided: str | None, expected: str) -> bool:
    """Validate *provided* against *expected* using constant-time comparison.

    Returns ``True`` if the keys match, ``False`` otherwise (including when
    *provided* is ``None``).
    """
    if provided is None:
        return False
    return secrets.compare_digest(provided, expected)


def _validate_keys_file_permissions(file_path: Path, *, allow_insecure_perms: bool = False) -> None:
    """Refuse to load an API keys file with group/world readable mode (issue #1480).

    On a shared HPC login node or multi-tenant host, a world- or
    group-readable keys file hands every local account every API key —
    including ``admin``-role keys — and the server gave no signal at
    startup.  This validator mirrors
    :func:`osimflow.storage._validate_storage_endpoint` (issue #1386):
    the project posture is to fail closed on misconfigured credential
    material unless the operator has explicitly opted in with
    ``allow_insecure_perms=True``.

    Parameters
    ----------
    file_path
        The resolved, existing file to inspect.  Must be a path whose
        ``stat()`` we can call — typically the result of
        ``Path(...).resolve()`` so symlinks are followed and we check
        the actual file being read.
    allow_insecure_perms
        When ``True``, group/world readable files are accepted with a
        loud ``WARNING``.  Defaults to ``False`` (fail-closed).

    Raises
    ------
    OSimFlowValueError
        When *file_path* is group or world readable and
        ``allow_insecure_perms`` is ``False``.
    """
    # stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH == 0o077.
    # On Windows those bits are always 0, so the check is a no-op there
    # (chmod 0644 has no portable meaning) — the dedicated tests skip
    # Windows with ``pytest.mark.skipif(sys.platform == "win32", ...)``.
    try:
        mode = file_path.stat().st_mode
    except OSError as exc:
        raise OSimFlowValueError(f"Cannot stat api_keys_file {file_path}: {exc}") from exc
    insecure_bits = mode & 0o077
    if insecure_bits == 0:
        return
    if allow_insecure_perms:
        log.warning(
            "INSECURE api_keys_file permissions (issue #1480): %s is %s, "
            "which is group/world readable (bits=0o%03o). Any local account "
            "on this host can read every API key — including admin-role "
            "keys — from the file. Allowed because "
            "--allow-insecure-api-keys-file was set. Fix with "
            "'chmod 0600 <file>' (do not use in production).",
            file_path,
            oct(mode & 0o7777),
            insecure_bits,
        )
        return
    raise OSimFlowValueError(
        f"Insecure api_keys_file permissions (issue #1480): {file_path} "
        f"is {oct(mode & 0o7777)}, which is group/world readable "
        f"(bits=0o{insecure_bits:03o}). On a shared host any local account "
        f"can read every API key from this file. Fix with "
        f"'chmod 0600 <file>' or pass --allow-insecure-api-keys-file to "
        f"override (dev/test only)."
    )


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

    Keys are stored **hashed at rest** as ``key_sha256`` entries (issue
    #1552): a file that leaks via backup, tarball, or accidental commit
    discloses no reusable credential.  :meth:`validate` hashes the
    presented key and compares digests against every entry with
    ``hmac.compare_digest`` in a fixed-order scan with no early exit,
    so response timing does not reveal which list position matched.

    The store supports two modes:
    - Single key mode: When ``single_key`` is set, validates against that one key
      with the server's global read_only setting.  The key itself is supplied
      in-memory (``--api-key``), never persisted, so no at-rest hashing
      applies; behaviour is unchanged from the caller's perspective.
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
        """Create a store with multiple users (issue #395, #1552).

        Entries may carry either ``"key"`` (plaintext — hashed
        immediately, never retained) or ``"key_sha256"`` (pre-hashed,
        e.g. loaded from an ``api_keys_file``).  An entry carrying
        **both** fields is rejected: it is ambiguous which credential
        is authoritative.  This is the programmatic/in-memory path;
        :meth:`from_file` additionally fails closed on any plaintext
        entry so no at-rest plaintext is silently accepted.
        """
        normalized: list[dict[str, str]] = []
        for index, user in enumerate(users):
            has_plain = "key" in user
            has_hashed = "key_sha256" in user
            if has_plain and has_hashed:
                raise OSimFlowValueError(
                    f"User entry {index} has both 'key' and 'key_sha256' — "
                    f"ambiguous; provide exactly one (issue #1552)."
                )
            if has_plain:
                entry = {k: v for k, v in user.items() if k != "key"}
                entry["key_sha256"] = hash_api_key(str(user["key"]))
                normalized.append(entry)
                continue
            if not has_hashed:
                raise OSimFlowValueError(
                    f"User entry {index} has neither 'key' nor 'key_sha256' (issue #1552)."
                )
            digest = user["key_sha256"]
            if not _is_sha256_hex(digest):
                raise OSimFlowValueError(
                    f"User entry {index} has malformed 'key_sha256' "
                    f"(expected 64-char lowercase hex sha256 digest, "
                    f"issue #1552)."
                )
            normalized.append(dict(user))
        return cls(users=normalized)

    @classmethod
    def from_file(
        cls,
        file_path: Path,
        *,
        allow_insecure_perms: bool = False,
    ) -> MultiUserAPIKeyStore:
        """Load API keys from a JSON file (issue #395, #1480, #1552).

        File format — keys are stored **hashed at rest** (issue #1552)::

            {
                "users": [
                    {"key_sha256": "<sha256-hex-of-key-1>", "user_id": "alice", "role": "admin"},
                    {"key_sha256": "<sha256-hex-of-key-2>", "user_id": "bob", "role": "readonly"}
                ]
            }

        Compute a digest with ``osimflow.api.auth.hash_api_key`` or
        ``hashlib.sha256(key.encode()).hexdigest()``.  Plaintext
        ``"key"`` entries — in whole or mixed with hashed entries —
        are **rejected** with :class:`OSimFlowValueError` pointing at
        the migration one-liner in ``docs/secret-management.md``:
        accepting plaintext silently would keep every key disclosable
        from the file itself, the exact exposure this issue closes.

        Security (issue #1480): the file must have restrictive permissions
        (mode ``0600`` recommended).  A file that is group or world
        readable is **refused** with :class:`OSimFlowValueError` because
        any local account on the host can read every API key — including
        ``admin``-role keys — and the server would otherwise give no
        signal at startup.  Pass ``allow_insecure_perms=True`` (or the
        ``--allow-insecure-api-keys-file`` CLI flag, see ``serve``) to
        accept a permissive file with a loud warning.  On Windows the
        group/world bits are always 0, so the check is effectively a
        no-op there — the dedicated tests cover the POSIX paths only.

        Raises
        ------
        OSimFlowValueError
            If the file does not have a ``.json`` or ``.keys`` extension,
            is not a regular file (e.g. a symlink to ``/dev/null`` or
            ``/etc/passwd``), cannot be read, contains invalid JSON,
            contains plaintext ``"key"`` entries (fail closed, issue
            #1552 — migrate first), or has group/world readable mode
            while ``allow_insecure_perms`` is ``False``.  Inherits
            :class:`ValueError` so legacy ``except ValueError:`` clauses
            keep matching.
        """
        resolved = file_path.resolve()
        if not resolved.is_file():
            raise OSimFlowValueError(f"api_keys_file must be a regular file, got {resolved}")
        if resolved.suffix not in (".json", ".keys"):
            raise OSimFlowValueError(
                f"api_keys_file must have .json or .keys extension, got {resolved.suffix!r}"
            )
        _validate_keys_file_permissions(resolved, allow_insecure_perms=allow_insecure_perms)
        try:
            keys_data = json.loads(resolved.read_text())
        except json.JSONDecodeError as exc:
            log.error("Failed to parse JSON from %s: %s", resolved, exc)
            raise OSimFlowValueError(f"Invalid JSON in api_keys_file: {exc}") from exc
        except OSError as exc:
            log.error("Failed to read %s: %s", resolved, exc)
            raise OSimFlowValueError(f"Cannot read api_keys_file: {exc}") from exc
        users = keys_data.get("users", [])
        if not users:
            raise OSimFlowValueError("No users found in api_keys_file")
        # Fail closed on plaintext at rest (issue #1552) — no silent
        # acceptance, and no partial mixing of the two formats.
        both_field_entries = [
            i
            for i, u in enumerate(users)
            if isinstance(u, dict) and "key" in u and "key_sha256" in u
        ]
        if both_field_entries:
            raise OSimFlowValueError(
                f"Insecure api_keys_file (issue #1552): entries at indices "
                f"{both_field_entries} have both 'key' and 'key_sha256' — "
                f"ambiguous which credential is authoritative; provide "
                f"exactly one. {API_KEYS_FILE_MIGRATION_HINT}"
            )
        plaintext_entries = [i for i, u in enumerate(users) if isinstance(u, dict) and "key" in u]
        if plaintext_entries:
            if any(isinstance(u, dict) and "key_sha256" in u for u in users):
                detail = (
                    f"entries mix plaintext 'key' (indices {plaintext_entries}) "
                    f"and hashed 'key_sha256' formats — hash every entry"
                )
            else:
                detail = f"plaintext 'key' entries found at indices {plaintext_entries}"
            raise OSimFlowValueError(
                f"Insecure api_keys_file (issue #1552): {detail}. {API_KEYS_FILE_MIGRATION_HINT}"
            )
        return cls.from_users(users)

    def validate(self, provided_key: str | None) -> APIKeyUser | None:
        """Validate an API key and return the user if valid.

        Single-key mode uses constant-time comparison against the
        in-memory key (unchanged behaviour from the caller's
        perspective).  Multi-user mode (issue #1552) hashes the
        presented key once with SHA-256 and compares the digest
        against **every** entry's stored ``key_sha256`` with
        ``hmac.compare_digest`` — a fixed-order scan with no early
        exit, so response timing does not reveal which list position
        matched (the pre-#1552 early-return loop was a small
        user-enumeration oracle).  Returns ``None`` if the key is
        invalid.
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

        # Multi-user mode — flat-timing digest scan (issue #1552):
        # hash once, compare against ALL entries, no early exit.
        presented_digest = hash_api_key(provided_key)
        matched: APIKeyUser | None = None
        for user in self.users:
            stored_digest = str(user.get("key_sha256", ""))
            if (
                hmac.compare_digest(presented_digest.encode("utf-8"), stored_digest.encode("utf-8"))
                and matched is None
            ):  # first match wins, but the scan continues
                matched = APIKeyUser(
                    key=provided_key,
                    user_id=user.get("user_id", "unknown"),
                    role=user.get("role", _READONLY),
                )
        return matched

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
