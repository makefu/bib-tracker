"""Rendering, and the page-versus-fragment split.

Full pages live in templates/pages/ and only include partials; every HTMX
target is its own file in templates/partials/. So one endpoint can serve both
without either duplicating markup or needing a template-fragment library
(jinja2-fragments is not packaged in nixpkgs).
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import TEMPLATES_DIR
from .filters import register_filters

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_filters(templates.env)


def is_fragment(request: Request) -> bool:
    """True for an HTMX swap, but not for a history restore, which needs the
    whole page back."""
    return bool(request.headers.get("HX-Request")) and not request.headers.get("HX-History-Restore-Request")


def render(request: Request, page: str, partial: str | None, context: dict[str, Any]) -> HTMLResponse:
    template = partial if (partial and is_fragment(request)) else page
    return templates.TemplateResponse(request, template, context)
