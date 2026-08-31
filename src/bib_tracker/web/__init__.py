"""Web layer: templates, static assets, routes."""

from __future__ import annotations

from pathlib import Path

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
