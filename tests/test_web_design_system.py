"""Invariants for the shared canonical design system.

The redesigned management UI shares one stylesheet — deploy/assets/app.css —
served static by nginx and linked via jasper.web._common.canonical_page().
Until the landing page (deploy/index.html) is migrated to link app.css, both
files carry the design TOKENS; these tests guard against the two drifting
apart and against the stylesheet losing the primitives pages rely on.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.web import _common

ROOT = Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "deploy" / "assets" / "app.css"
LANDING_HTML = ROOT / "deploy" / "index.html"


def test_app_css_exists():
    assert APP_CSS.is_file(), f"missing shared stylesheet {APP_CSS}"


def test_landing_links_app_css_without_duplicating_tokens():
    # The landing page is converged onto the shared stylesheet: it links
    # app.css and must NOT carry its own copy of the design tokens or
    # @font-face rules, so there is one source of truth (no drift).
    landing = LANDING_HTML.read_text()
    assert "/assets/app.css" in landing
    assert ":root {" not in landing
    assert "@font-face" not in landing


def test_app_css_carries_shared_primitives():
    css = APP_CSS.read_text()
    for marker in (
        "@font-face", "--primary:", ".page", ".eyebrow",
        ".segmented", ".btn", ".sr-only", "prefers-reduced-motion",
    ):
        assert marker in css, f"app.css missing shared primitive: {marker}"


def test_app_css_does_not_force_global_svg_size():
    # The landing page sets `svg { width:16px; height:16px }`, which would
    # squash the sound page's full-width EQ graph. The shared sheet must
    # only set `display`, sizing icons via the `.ico` helper instead.
    m = re.search(r"\bsvg\s*\{([^}]*)\}", APP_CSS.read_text())
    assert m, "expected a base svg rule"
    assert "width" not in m.group(1), "shared svg rule must not force a size"


def test_asset_version_is_url_safe_and_failsoft(monkeypatch):
    # Fail-soft: with no readable build.txt the token falls back to a
    # valid (un-busted) value rather than raising.
    monkeypatch.setattr(_common, "_asset_version_cache", None)
    version = _common._asset_version()
    assert version
    assert re.fullmatch(r"[\w.-]+", version), version
