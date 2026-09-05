# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The install-time render of the static landing page and the area hubs.

nginx serves the result straight from disk, so an unsubstituted placeholder
ships a broken page: the renderer must know every placeholder the shipped
template carries, refuse the render when one is absent, and leave none behind.
A hub is the same manifest rows under a hub path, rendered whole — no
template, no daemon, and no per-request state to leak into a file on disk.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.install_profile import system_capabilities_for_profile
from jasper.web._common import CANONICAL_ICON_SPRITE
from jasper.web.landing import render_landing, substitutions, write_hub_pages
from jasper.web.nav import children, entry, hub_paths, render_hub

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


# Both shipped profiles: a hub's rows differ only in which caps gate them.
PROFILES = ("full", "streambox")


def _render(template: str = FAKE_TEMPLATE, *, token: str = "tok-1") -> str:
    return render_landing(
        template,
        app_css_version="abc1234",
        caps={"voice_brain": True},
        control_token=token,
    )


def _hub(path: str, profile: str) -> str:
    return render_hub(
        path,
        caps=system_capabilities_for_profile(profile),
        app_css_version="abc1234",
    )


def _rendered_rows(page: str) -> list[tuple[str, frozenset[str]]]:
    """Each row's href with the gates that hide it: its own plus its
    section's, where a gate shared by the whole group is hoisted."""
    rows = []
    for section in page.split('<section class="settings-section"')[1:]:
        shared = re.match(r'[^>]*data-requires="([^"]+)"', section)
        for href, attrs in re.findall(
            r'<a class="setting-row[^"]*" href="([^"]+)"([^>]*)>', section
        ):
            own = re.search(r'data-requires="([^"]+)"', attrs)
            rows.append((href, frozenset(m.group(1) for m in (shared, own) if m)))
    return rows


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
    """The served groups are the manifest's landing rows rendered: same rows,
    same order, no hand-written row left behind
    (docs/UX-AUDIT-2026-09-03.md §2)."""
    groups = _render(LANDING_HTML.read_text()).split('<nav class="groups"', 1)[1]
    rendered = re.findall(
        r'<a class="setting-row[^"]*"[^>]*href="([^"]+)".*?'
        r'<span class="setting-title">([^<]+)</span>\s*'
        r'<span class="setting-status"(?: id="([^"]+)")?>',
        groups,
        re.DOTALL,
    )

    assert rendered == [
        (row.path, row.label, row.status_id) for row in children("/")
    ]


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("path", hub_paths())
def test_hub_renders_its_manifest_children_in_order_behind_their_gates(
    path: str, profile: str
) -> None:
    """A hub IS its manifest children: every row once, in order, carrying
    exactly the capabilities that gate it (docs/UX-AUDIT-2026-09-03.md §2)."""
    assert _rendered_rows(_hub(path, profile)) == [
        (row.path, frozenset(row.requires)) for row in children(path)
    ]


@pytest.mark.parametrize("path", hub_paths())
def test_hub_section_headings_never_repeat_the_page_title(path: str) -> None:
    """The eyebrow above a group is dropped where it would say what the header
    already says (docs/web-ia.md §2); the other groups keep theirs."""
    headings = re.findall(
        r'<h2 class="eyebrow group-title"[^>]*>([^<]+)</h2>', _hub(path, "full")
    )

    assert headings == [
        group
        for group in dict.fromkeys(row.group for row in children(path))
        if group != entry(path).label
    ]


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("path", hub_paths())
def test_hub_is_static_and_gates_fail_closed(path: str, profile: str) -> None:
    """A page written to disk carries no secret and no unsubstituted shell
    text, and every gated element ships `hidden` so gating only reveals."""
    out = _hub(path, profile)

    assert "jts-control-token" not in out
    assert not re.search(r"__[A-Z][A-Z_]*__", out)
    assert '<script type="application/json" id="landing-caps">' in out
    assert '<script type="module" src="/assets/hub/js/main.js"></script>' in out
    assert "<script>" not in out
    for line in out.splitlines():
        if "data-requires=" in line:
            assert " hidden" in line, line.strip()


def test_install_writes_one_page_per_hub(tmp_path: Path) -> None:
    write_hub_pages(tmp_path, caps={"content_dsp": True}, app_css_version="abc1234")

    assert sorted(
        page.relative_to(tmp_path).as_posix() for page in tmp_path.rglob("*.html")
    ) == sorted(f"{hub.strip('/')}/index.html" for hub in hub_paths())


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
