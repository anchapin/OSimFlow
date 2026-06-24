"""REST API for OSimFlow (issue #138, G23a).

Security extensions (issue #268, #395): API key auth, multi-user auth, CORS, rate limiting.
Campaign CRUD (issue #267): multi-campaign management endpoints.
"""

from osimflow.api.app import (
    _ADMIN,
    _READONLY,
    _READWRITE,
    APIKeyMiddleware,
    create_app,
)
from osimflow.api.auth import (
    APIKeyUser,
    MultiUserAPIKeyStore,
    extract_api_key,
    generate_api_key,
    get_user_permission,
    validate_api_key,
)

__all__ = [
    "create_app",
    "extract_api_key",
    "generate_api_key",
    "validate_api_key",
    "get_user_permission",
    "APIKeyUser",
    "APIKeyMiddleware",
    "MultiUserAPIKeyStore",
    "_ADMIN",
    "_READONLY",
    "_READWRITE",
]
