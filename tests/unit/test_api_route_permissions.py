"""Route-permission contract test (issue #1626, mirroring the #1551 pattern).

Enumerates every mutating route (POST / PUT / PATCH / DELETE) on every
``APIRouter`` defined under ``osimflow/api/`` and asserts that the endpoint
function's source contains a permission check
(``require_permission`` / ``get_user_permission``) — OR that the route is
explicitly allowlisted below with a justification.

New mutating endpoints must either carry a permission gate or extend
``_ALLOWED_UNGUARDED`` with a reason; unlisted routes fail this test, and
allowlist entries that no longer match a live route also fail so the list
cannot rot.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
pytest.importorskip("boto3", reason="osimflow[aws] extra required")
from fastapi import APIRouter
from fastapi.routing import APIRoute

import osimflow.api

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_PERMISSION_MARKERS = ("require_permission", "get_user_permission")

# Allowlisted unguarded mutating routes. Key: ``<module>.<function>``.
# Value: ``(required_source_marker_or_None, justification)``. When a marker
# is given it MUST still appear in the endpoint source, so an allowlisted
# route that loses its alternative-auth mechanism fails loudly.
_ALLOWED_UNGUARDED: dict[str, tuple[str | None, str]] = {
    "osimflow.api.coordinator.array_complete": (
        "_verify_eventbridge_signature",
        "EventBridge webhook endpoint (issue #626): authenticated by its own "
        "fail-closed webhook-secret check (X-OSimFLOW-Webhook-Secret), a "
        "machine-to-machine caller rather than an API-key user.",
    ),
    "osimflow.api.campaigns.compare_campaigns_post": (
        None,
        # TODO(follow-up out of #1626 scope): POST verb carries read-only
        # semantics (computes a multi-campaign comparison, mutates no
        # state). Gate on require_permission(request, "readonly") for 401
        # parity with the other endpoints in a follow-up issue.
        "Read-only comparison query exposed over POST (issue #404 shape); "
        "no server state is mutated.",
    ),
    "osimflow.api.app.validate_config": (
        None,
        # TODO(follow-up out of #1626 scope): pure pre-flight validation
        # of caller-supplied paths (issue #398) exposed over POST; no
        # server state is mutated. Gate on require_permission(request,
        # "readonly") for 401 parity in a follow-up issue.
        "Pre-flight configuration validation over POST; mutates no state.",
    ),
}


def _iter_routers() -> Any:
    """Yield ``(module_name, router)`` for every APIRouter under osimflow/api.

    Note: some modules (notably ``app.py``) re-export other modules'
    routers; route attribution below keys on the endpoint function's own
    ``__module__`` so re-exports cannot misattribute a route.
    """
    api_dir = Path(osimflow.api.__file__).parent
    for mod_info in pkgutil.iter_modules([str(api_dir)]):
        module = importlib.import_module(f"osimflow.api.{mod_info.name}")
        for obj in vars(module).values():
            if isinstance(obj, APIRouter):
                yield mod_info.name, obj


def _collect_mutating_routes() -> dict[str, str]:
    """Return {qualified_endpoint_name: source} for every mutating route."""
    found: dict[str, str] = {}
    for _module_name, router in _iter_routers():
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            if not (route.methods & _MUTATING_METHODS):
                continue
            endpoint = route.endpoint
            qualified = f"{endpoint.__module__}.{endpoint.__name__}"
            if qualified in found:
                # Same endpoint reached through a re-exported router.
                continue
            source = inspect.getsource(endpoint)
            found[qualified] = source
    return found


def _is_guarded(source: str) -> bool:
    return any(marker in source for marker in _PERMISSION_MARKERS)


class TestMutatingRoutesHavePermissionGates:
    def test_every_mutating_route_is_guarded_or_allowlisted(self) -> None:
        routes = _collect_mutating_routes()
        assert routes, "route enumeration found no mutating routes — the scan is broken"

        unguarded: list[str] = []
        for qualified, source in sorted(routes.items()):
            if _is_guarded(source):
                continue
            allowed = _ALLOWED_UNGUARDED.get(qualified)
            if allowed is None:
                unguarded.append(qualified)
                continue
            marker, justification = allowed
            assert justification.strip(), f"allowlist entry for {qualified} lacks a justification"
            if marker is not None:
                assert marker in source, (
                    f"{qualified} is allowlisted on the strength of {marker!r}, "
                    "but that marker no longer appears in its source"
                )
        assert not unguarded, (
            "Mutating routes without a permission check and without an "
            "allowlist entry (add require_permission or extend "
            f"_ALLOWED_UNGUARDED with a justification): {unguarded}"
        )

    def test_allowlist_entries_all_match_live_routes(self) -> None:
        routes = _collect_mutating_routes()
        stale = sorted(set(_ALLOWED_UNGUARDED) - set(routes))
        assert not stale, (
            "Stale _ALLOWED_UNGUARDED entries (route removed, renamed, or now "
            f"guarded — clean up the allowlist): {stale}"
        )

    def test_allowlisted_routes_are_actually_unguarded(self) -> None:
        """A route that gained a real gate no longer belongs in the allowlist."""
        routes = _collect_mutating_routes()
        for qualified, (_marker, _justification) in sorted(_ALLOWED_UNGUARDED.items()):
            source = routes.get(qualified)
            if source is None:
                continue
            assert not _is_guarded(source), (
                f"{qualified} now has a permission check — remove it from _ALLOWED_UNGUARDED"
            )
