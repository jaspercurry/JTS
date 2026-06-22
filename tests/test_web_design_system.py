# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Invariants for the shared canonical design system.

The redesigned management UI shares one stylesheet — deploy/assets/app.css —
served static by nginx and linked via jasper.web._common.canonical_page().
The landing page (deploy/index.html) links app.css rather than carrying its
own design TOKENS; these tests enforce that single source of truth (no
duplicated token block to drift) and that the stylesheet keeps the
primitives pages rely on.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.web import _common

ROOT = Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "deploy" / "assets" / "app.css"
LANDING_HTML = ROOT / "deploy" / "index.html"


def _without_css_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _focus_ring_css_sources() -> list[Path]:
    paths = [
        APP_CSS,
        LANDING_HTML,
        ROOT / "jasper" / "web" / "_common.py",
    ]
    paths.extend(sorted((ROOT / "deploy" / "assets").rglob("*.css")))
    paths.extend(sorted((ROOT / "deploy").glob("*.html")))
    # Wizard modules carry per-page CSS (the `page_css` argument to
    # canonical_page()) as inline strings — AGENTS.md's "do not add
    # page-level focus outlines" promise covers those too, not just the
    # shared sheet and the static per-page .css files.
    paths.extend(sorted((ROOT / "jasper" / "web").glob("*.py")))
    return list(dict.fromkeys(paths))


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
        "[hidden] { display: none !important; }",
    ):
        assert marker in css, f"app.css missing shared primitive: {marker}"


def test_app_css_does_not_force_global_svg_size():
    # The landing page sets `svg { width:16px; height:16px }`, which would
    # squash the sound page's full-width EQ graph. The shared sheet must
    # only set `display`, sizing icons via the `.ico` helper instead.
    m = re.search(r"\bsvg\s*\{([^}]*)\}", APP_CSS.read_text())
    assert m, "expected a base svg rule"
    assert "width" not in m.group(1), "shared svg rule must not force a size"


def test_shared_styles_suppress_browser_focus_outlines():
    css = APP_CSS.read_text()
    assert ":where(a, button, input, select, textarea, [tabindex]):focus" in css
    assert "outline: none;" in css


def test_web_css_does_not_reintroduce_focus_ring_selectors():
    offenders: list[str] = []
    for path in _focus_ring_css_sources():
        text = _without_css_comments(path.read_text())
        for selector in (":focus-visible", ":focus-within"):
            if selector in text:
                offenders.append(f"{path.relative_to(ROOT)} contains {selector}")

    assert not offenders, (
        "jts.local pages should not render focus rings; use selected/active "
        "component state instead:\n" + "\n".join(offenders)
    )


def test_web_css_only_uses_outline_to_suppress_focus_chrome():
    offenders: list[str] = []
    for path in _focus_ring_css_sources():
        text = _without_css_comments(path.read_text())
        for match in re.finditer(r"\boutline\s*:\s*([^;{}]+);", text):
            value = match.group(1).strip().lower()
            if value != "none":
                offenders.append(
                    f"{path.relative_to(ROOT)} has outline: {match.group(1).strip()}"
                )

    assert not offenders, (
        "CSS outline is reserved for suppressing browser focus chrome:\n"
        + "\n".join(offenders)
    )


def test_asset_version_is_url_safe_and_failsoft(monkeypatch):
    # Fail-soft: with no readable build.txt the token falls back to a
    # valid (un-busted) value rather than raising.
    monkeypatch.setattr(_common, "_asset_version_cache", None)
    version = _common._asset_version()
    assert version
    assert re.fullmatch(r"[\w.-]+", version), version


# The three-tier typographic grammar (docs/HANDOFF-management-ui.md) lives partly
# in the system page's ES modules, which the Python render tests don't execute —
# so guard the grammar statically here.
SYSTEM_JS = ROOT / "deploy" / "assets" / "system-status" / "js"


def _css_body(css: str, selector: str) -> str:
    """The declaration block for a single-rule selector (no nested braces)."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"expected a CSS rule for {selector!r}"
    return m.group(1)


def test_typographic_grammar_tiers_do_not_reflatten():
    """Card titles are CASED (.section__title); row labels are the EYEBROW tier
    (.deflist dt, uppercase). Guards against collapsing the two into one style —
    the collision the card-title promotion fixed."""
    css = APP_CSS.read_text()
    assert "uppercase" not in _css_body(css, ".section__title"), \
        "card titles must stay cased, not become EYEBROW"
    assert "text-transform: uppercase" in _css_body(css, ".deflist dt"), \
        "row labels must remain the uppercase EYEBROW tier"
    # …and titledCard actually renders the cased class (not the old eyebrow()).
    assert "section__title" in (SYSTEM_JS / "components.js").read_text(), \
        "titledCard must render the cased .section__title"


def test_cpu_stat_shows_bare_percentage():
    """CPU usage shows just the percentage; the per-core bars carry the
    breakdown, so the '% total' qualifier stays gone."""
    assert "% total" not in (SYSTEM_JS / "sections.js").read_text()
