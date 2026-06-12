"""REST API for OSimFlow (issue #138, G23a).

Security extensions (issue #268): API key auth, CORS, rate limiting.
Campaign CRUD (issue #267): multi-campaign management endpoints.
"""

from osimflow.api.app import create_app, extract_api_key, generate_api_key, validate_api_key

__all__ = ["create_app", "extract_api_key", "generate_api_key", "validate_api_key"]
