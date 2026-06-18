"""Variable Designer — web-based variable YAML editor (issue #587).

Provides:
  - GET /ui/designer/ — redirect to the Variable Designer HTML page

Served at ``/ui/designer/`` when the FastAPI app is created with
``variable_editor=True`` (set automatically when ``--editor`` is passed
on the CLI).

The frontend is a self-contained HTML/JS file that provides:
  - Distribution dropdowns for each variable type
  - Visual preview canvas for distributions
  - Live YAML validation
  - Import/export functionality
  - Measure browser
  - YAML preview panel
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, RedirectResponse

log = logging.getLogger("osimflow.api.variable_designer")

variable_designer_router = APIRouter()


@variable_designer_router.get("/ui/designer/")
async def variable_designer_redirect() -> RedirectResponse:
    """Redirect /ui/designer/ to the Variable Designer HTML page."""
    return RedirectResponse(url="/static/variable_designer.html")
