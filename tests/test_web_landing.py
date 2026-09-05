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

LANDING_HTML = Path(__file__).resolve().parents[1] / "deploy" / "index.html"
# Placeholder shape: __UPPER_SNAKE__, the only such tokens in the template.
TEMPLATE_PLACEHOLDERS = tuple(
    sorted(set(re.findall(r"__[A-Z][A-Z_]*__", LANDING_HTML.read_text())))
)

FAKE_TEMPLATE = (
    '<link href="/assets/app.css?v=__APP_CSS_VERSION__">'
    '<meta content="__JTS_CONTROL_TOKEN__">'
    "<body>__JTS_ICON_SPRITE__"
    '<a class="setting-row"><svg class="chevron"><use href="#icon-chevron">'
    "</use></svg></a>"
    "<script>var BAKED_CAPS = __JTS_CAPS_JSON__;</script>"
)


def _render(template: str = FAKE_TEMPLATE, *, token: str = "tok-1") -> str:
    return render_landing(
        template,
        app_css_version="abc1234",
        caps_json='{"voice_brain": true}',
        control_token=token,
    )


def test_renderer_covers_exactly_the_shipped_templates_placeholders() -> None:
    covered = substitutions(app_css_version="", caps_json="", control_token="")

    assert TEMPLATE_PLACEHOLDERS, "expected placeholders in deploy/index.html"
    assert tuple(sorted(covered)) == TEMPLATE_PLACEHOLDERS


def test_render_substitutes_every_placeholder() -> None:
    out = _render()

    assert "/assets/app.css?v=abc1234" in out
    assert 'content="tok-1"' in out
    assert 'var BAKED_CAPS = {"voice_brain": true};' in out
    assert CANONICAL_ICON_SPRITE in out


def test_render_escapes_the_control_token() -> None:
    # The token lands inside an HTML attribute; the base64url alphabet is safe
    # but a hand-edited or future token must not be able to break out of it.
    out = _render(token='a"><script>x</script>')

    assert "<script>x</script>" not in out
    assert "&quot;&gt;&lt;script&gt;" in out


def test_symbols_the_landing_page_references_are_in_the_shared_sprite() -> None:
    used = set(re.findall(r'<use href="#(icon-[a-z]+)"', LANDING_HTML.read_text()))
    shipped = set(re.findall(r'<symbol id="(icon-[a-z]+)"', CANONICAL_ICON_SPRITE))

    assert used, "expected the landing page to reference sprite symbols"
    assert used <= shipped, f"missing from the shared sprite: {sorted(used - shipped)}"


@pytest.mark.parametrize("missing", TEMPLATE_PLACEHOLDERS)
def test_render_refuses_a_template_missing_a_placeholder(missing: str) -> None:
    with pytest.raises(ValueError):
        _render(FAKE_TEMPLATE.replace(missing, "gone"))


def test_shipped_landing_page_renders_with_nothing_left_behind() -> None:
    assert not re.search(
        r"__[A-Z][A-Z_]*__", _render(LANDING_HTML.read_text())
    )
