# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The install-time render of the static landing page.

nginx serves the result straight from disk, so an unsubstituted placeholder
ships a broken page: the renderer must know every placeholder the shipped
template carries, refuse the render when one is absent, and leave none behind.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.web._common import CANONICAL_ICON_SPRITE
from jasper.web.landing import render_landing, substitutions
from jasper.web.nav import NAV

LANDING_HTML = Path(__file__).resolve().parents[1] / "deploy" / "index.html"
# Placeholder shape: __UPPER_SNAKE__, the only such tokens in the template.
TEMPLATE_PLACEHOLDERS = tuple(
    sorted(set(re.findall(r"__[A-Z][A-Z_]*__", LANDING_HTML.read_text())))
)

FAKE_TEMPLATE = (
    '<link href="/assets/app.css?v=__APP_CSS_VERSION__">'
    '<meta content="__JTS_CONTROL_TOKEN__">'
    "<body>__JTS_ICON_SPRITE__"
    '<nav class="groups">__JTS_NAV_GROUPS__</nav>'
    "__JTS_CAPS_ISLAND__"
    '<script type="module" src="/assets/landing/js/main.js"></script>'
)


def _render(template: str = FAKE_TEMPLATE, *, token: str = "tok-1") -> str:
    return render_landing(
        template,
        app_css_version="abc1234",
        caps={"voice_brain": True},
        control_token=token,
    )


def test_renderer_covers_exactly_the_shipped_templates_placeholders() -> None:
    covered = substitutions(app_css_version="", caps={}, control_token="")

    assert TEMPLATE_PLACEHOLDERS, "expected placeholders in deploy/index.html"
    assert tuple(sorted(covered)) == TEMPLATE_PLACEHOLDERS


def test_render_substitutes_every_placeholder() -> None:
    out = _render()

    assert "/assets/app.css?v=abc1234" in out
    assert 'content="tok-1"' in out
    assert (
        '<script type="application/json" id="landing-caps">'
        '{"voice_brain": true}</script>'
    ) in out
    assert CANONICAL_ICON_SPRITE in out


def test_render_escapes_the_control_token() -> None:
    # The token lands inside an HTML attribute; the base64url alphabet is safe
    # but a hand-edited or future token must not be able to break out of it.
    out = _render(token='a"><script>x</script>')

    assert "<script>x</script>" not in out
    assert "&quot;&gt;&lt;script&gt;" in out


def test_symbols_the_landing_page_references_are_in_the_shared_sprite() -> None:
    used = set(re.findall(r'<use href="#(icon-[a-z]+)"', _render(LANDING_HTML.read_text())))
    shipped = set(re.findall(r'<symbol id="(icon-[a-z]+)"', CANONICAL_ICON_SPRITE))

    assert used, "expected the landing page to reference sprite symbols"
    assert used <= shipped, f"missing from the shared sprite: {sorted(used - shipped)}"


def test_landing_settings_rows_are_the_nav_manifest_in_order() -> None:
    """The served groups are `nav.NAV` rendered: same rows, same order, no
    hand-written row left behind (docs/UX-AUDIT-2026-09-03.md §2)."""
    groups = _render(LANDING_HTML.read_text()).split('<nav class="groups"', 1)[1]
    rendered = re.findall(
        r'<a class="setting-row[^"]*"[^>]*href="([^"]+)".*?'
        r'<span class="setting-title">([^<]+)</span>\s*'
        r'<span class="setting-status"(?: id="([^"]+)")?>',
        groups,
        re.DOTALL,
    )

    assert rendered == [(row.path, row.label, row.status_id) for row in NAV]


@pytest.mark.parametrize("missing", TEMPLATE_PLACEHOLDERS)
def test_render_refuses_a_template_missing_a_placeholder(missing: str) -> None:
    with pytest.raises(ValueError):
        _render(FAKE_TEMPLATE.replace(missing, "gone"))


def test_shipped_landing_page_renders_with_nothing_left_behind() -> None:
    assert not re.search(
        r"__[A-Z][A-Z_]*__", _render(LANDING_HTML.read_text())
    )


def test_shipped_landing_page_carries_one_module_and_its_caps_island() -> None:
    """One ES-module entry (docs/web-ia.md §3) reading its capability ceiling
    from the shared `json_island()` shape — no inline behaviour left to drift
    from the hub pages that share settings-status.js."""
    out = _render(LANDING_HTML.read_text())

    assert '<script type="module" src="/assets/landing/js/main.js"></script>' in out
    assert '<script type="application/json" id="landing-caps">' in out
    assert "<script>" not in out
