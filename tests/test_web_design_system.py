# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Invariants for the shared canonical design system.

The redesigned management UI shares one stylesheet — deploy/assets/app.css —
served static by nginx and linked via jasper.web.chrome.canonical_page().
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.web import chrome

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


# Bare selectors app.css owns outright: no page sheet may redeclare these
# (a scoped override like `.wake-page .btn { … }` is unaffected — only a
# bare `.toggle`/`.disclosure`/`.badge` compound at the start of a rule
# is checked).
#
# `.setting-row` is deliberately absent: app.css owns it only inside
# `.settings-list`, because sound-profile/sound.css has an unrelated
# page-local block of the same name.
OWNED_BARE_SELECTORS = (
    ".toggle", ".disclosure", ".badge",
    ".form-actions", ".status-line", ".wizard-steps",
    ".settings-list", ".group-title", ".row-icon", ".chevron",
)


def test_page_css_does_not_redeclare_owned_selectors():
    offenders = []
    # The same inventory used by the focus-contract guards includes static
    # sheets/HTML and every jasper.web module that can carry a page_css string.
    for path in _focus_ring_css_sources():
        if path == APP_CSS:
            continue
        css = _without_css_comments(path.read_text())
        for selector in OWNED_BARE_SELECTORS:
            if re.search(rf"(?m)^\s*{re.escape(selector)}(?![\w-])", css):
                offenders.append(f"{path.relative_to(ROOT)} ({selector})")
    assert not offenders, (
        "canonical styling belongs only in deploy/assets/app.css: "
        + ", ".join(offenders)
    )


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


def test_asset_version_is_url_safe_and_failsoft():
    # Fail-soft: with no readable build.txt the token falls back to a
    # valid (un-busted) value rather than raising.
    version = chrome._asset_version()
    assert version
    assert re.fullmatch(r"[\w.-]+", version), version


def test_canonical_page_observes_manifest_replacement_in_warm_process(
    monkeypatch, tmp_path,
):
    """A wizard activated mid-deploy must not keep the prior asset URL."""
    manifest = tmp_path / "build.txt"
    manifest.write_text("JASPER_GIT_SHA=old123\n")
    monkeypatch.setattr(chrome, "BUILD_MANIFEST_FILE", manifest)

    first = chrome.canonical_page(
        "Status", "", page_css_href="/assets/system-status/system.css",
    ).decode()
    assert '/assets/app.css?v=old123' in first
    assert '/assets/system-status/system.css?v=old123' in first

    replacement = tmp_path / "build.next"
    replacement.write_text("JASPER_GIT_SHA=new456\n")
    replacement.replace(manifest)

    second = chrome.canonical_page(
        "Status", "", page_css_href="/assets/system-status/system.css",
    ).decode()
    assert '/assets/app.css?v=new456' in second
    assert '/assets/system-status/system.css?v=new456' in second


# ---------------------------------------------------------------- #
# docs/design-language.md — the craft rules, pinned.                 #
# Each test below is the enforcement arm of one section of that doc; #
# when a rule changes, change it there and here in the same PR.      #
# ---------------------------------------------------------------- #

DESIGN_LANGUAGE_DOC = ROOT / "docs" / "design-language.md"


# A neutral text tier derived in a page: the FIRST colour of the mix is a
# foreground/neutral token, i.e. "take our text colour and weaken it". Tinting
# a STATUS tone toward the foreground for legibility (--status-warn mixed with
# --text) is a different, allowed thing — it is not a ramp tier.
_PAGE_TEXT_TIER = re.compile(
    r"(?<![-\w])color:\s*color-mix\(\s*in\s+\w+\s*,\s*"
    r"var\(\s*--(?:foreground|muted-foreground|text|muted)\s*\)"
    r"([^;]*)\)"
)


def test_pages_do_not_invent_their_own_text_tiers():
    """design-language.md §4: a text colour derived by weakening a foreground
    token is a fourth tier invented in one file. Mixing belongs in the token
    layer, where --muted-faint is defined once.

    `sound.css` is a known, ledgered exception outside the measurement-flow
    pass's surfaces; it is listed rather than migrated so this guard can stay
    exact about what remains."""
    ledgered = {"deploy/assets/sound-profile/sound.css"}
    offenders: list[str] = []
    for path in _focus_ring_css_sources():
        if path == APP_CSS:
            continue  # the token layer is where mixing is allowed
        rel = str(path.relative_to(ROOT))
        if rel in ledgered:
            continue
        for match in _PAGE_TEXT_TIER.finditer(_without_css_comments(path.read_text())):
            offenders.append(f"{rel}: {match.group(0)[:72]}")

    assert not offenders, (
        "text colours come from the three-tier ramp (--text / --muted / "
        "--muted-faint), not a page-local color-mix "
        "(docs/design-language.md §4):\n" + "\n".join(offenders)
    )


# Off-ladder font sizes HELD per sheet until that page's own pass corrects
# them. The landing page's three are held under docs/design-language.md §2 as
# well — it is the protected reference implementation and 0.92rem -> 14px
# reflows the pair banner. SHRINK-ONLY: the guard asserts a sheet's off-ladder
# set EQUALS its entry here, so a corrected value must be deleted from this
# table and any new stray fails.
OFF_LADDER_HELD: dict[str, set[str]] = {
    "deploy/index.html": {"0.86rem", "0.88rem", "0.92rem"},
    "deploy/assets/bluetooth/bluetooth.css": {"0.7rem", "0.85rem", "0.95rem"},
    "deploy/assets/correction/crossover.css": {
        "0.8125rem", "0.82rem", "0.95rem", "0.9rem", "1.05rem",
    },
    "deploy/assets/sound-profile/sound.css": {"10px", "9px"},
    "deploy/assets/spotify/spotify.css": {"17px"},
    "deploy/assets/system-status/system.css": {
        "10px", "15px", "17px", "18px", "20px", "24px",
    },
    "deploy/assets/tools/tools.css": {"24px", "26px"},
    "deploy/assets/transit/transit.css": {
        "0.8125rem", "0.875rem", "0.95rem", "1.05rem",
    },
    "deploy/assets/wake/wake.css": {
        "0.78rem", "0.82rem", "0.83rem", "0.84rem", "0.86rem", "0.88rem",
        "0.93rem", "0.95rem", "0.9rem", "1rem",
    },
    "deploy/assets/weather/weather.css": {"0.85rem"},
    "deploy/assets/wifi/wifi.css": {"18px"},
    "jasper/web/sources_setup.py": {"0.9rem"},
}
TYPE_LADDER_PX = {"11px", "12px", "13px", "14px", "16px"}


def _off_ladder_sizes(css: str) -> set[str]:
    """font-size values that are not on the ladder.

    NB `em` is relative-to-parent sizing, not a ladder step, so it is skipped —
    but the check must not also swallow `rem`, which IS an absolute size and is
    exactly what this guard exists to catch."""
    out: set[str] = set()
    for raw in re.findall(r"font-size:\s*([^;}]+)", css):
        value = raw.strip()
        if value in TYPE_LADDER_PX:
            continue
        if value.startswith("var(") or value.startswith("calc("):
            continue
        if re.search(r"(?<![a-z])\d*\.?\d+em\b", value):
            continue
        out.add(value)
    return out


@pytest.mark.parametrize(
    "path", _focus_ring_css_sources(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_page_type_stays_on_the_ladder(path: Path):
    """design-language.md §3: 11/12/13/14/16 px, and no new off-ladder value."""
    rel = str(path.relative_to(ROOT))
    found = _off_ladder_sizes(_without_css_comments(path.read_text()))
    held = OFF_LADDER_HELD.get(rel, set())
    assert found == held, (
        f"{rel} type must sit on the 11/12/13/14/16px ladder "
        f"(docs/design-language.md §3). New off-ladder: {sorted(found - held)}. "
        f"Corrected — delete from OFF_LADDER_HELD: {sorted(held - found)}"
    )


def test_off_ladder_held_table_has_no_dead_keys():
    """A key that doesn't match an enumerated source's relative path is never
    read by test_page_type_stays_on_the_ladder (OFF_LADDER_HELD.get(rel) would
    just miss) — it would silently hold a stray value forever instead of
    failing when the value is corrected or the path renamed."""
    sources = {str(p.relative_to(ROOT)) for p in _focus_ring_css_sources()}
    assert set(OFF_LADDER_HELD) <= sources


def test_type_ladder_guard_actually_catches_a_new_off_ladder_value():
    """The guard is only worth having if it fires. An earlier version excluded
    anything containing "em", which silently swallowed `rem` too — the exact
    case it exists to catch."""
    assert _off_ladder_sizes("a { font-size: 0.95rem; }") == {"0.95rem"}
    assert _off_ladder_sizes("a { font-size: 15px; }") == {"15px"}
    # …while the genuinely-exempt shapes stay quiet.
    assert _off_ladder_sizes("a { font-size: 0.95em; }") == set()
    assert _off_ladder_sizes("a { font-size: 14px; }") == set()
    # Held values are held per sheet (OFF_LADDER_HELD), never tree-wide.
    assert _off_ladder_sizes("a { font-size: 0.92rem; }") == {"0.92rem"}


def test_design_language_doc_is_reachable_and_dated():
    """Documentation paradigm rules 3 and 8: every canonical subsystem
    reference doc carries a Last verified footer and is listed in README's
    doc atlas."""
    doc = DESIGN_LANGUAGE_DOC.read_text()
    assert re.search(r"(?m)^Last verified: \d{4}-\d{2}-\d{2}$", doc), (
        "docs/design-language.md needs a `Last verified: YYYY-MM-DD` footer"
    )
    assert "design-language.md" in (ROOT / "README.md").read_text(), (
        "docs/design-language.md must be listed in README's documentation map"
    )


# ---------------------------------------------------------------- #
# docs/UX-AUDIT-2026-09-03.md §5.2 — every page shell renders        #
# `.app-header` (recurrence: /balance/ [deleted], /sync/ [moved]).   #
# ---------------------------------------------------------------- #

# A "page" is a *_setup.py/*_flow.py/*_page.py module that owns a page shell
# (it calls canonical_page() itself). A pure router that delegates every GET
# route to another such module, or a stateless helper (pair_flow.py,
# active_speaker_flow.py), renders no shell of its own and is not a page for
# this guard.
_PAGE_SHELL_MODULES = tuple(
    p for p in (
        sorted((ROOT / "jasper" / "web").glob("*_setup.py"))
        + sorted((ROOT / "jasper" / "web").glob("*_flow.py"))
        + sorted((ROOT / "jasper" / "web").glob("*_page.py"))
    )
    if "canonical_page(" in p.read_text()
)


def _renders_app_header(path: Path) -> bool:
    text = path.read_text()
    if "canonical_header(" in text:
        return True
    if re.search(r'class=["\']app-header\b', text):
        return True
    # Client-rendered pages (chat, system-status): the header comes from the
    # page's own JS module graph, not the Python source.
    for slug in re.findall(r"/assets/([\w-]+)/js/", text):
        js_dir = ROOT / "deploy" / "assets" / slug / "js"
        if not js_dir.is_dir():
            continue
        for js_file in js_dir.rglob("*.js"):
            js_text = js_file.read_text()
            if "app-header" in js_text or "appHeader(" in js_text:
                return True
    return False


@pytest.mark.parametrize(
    "path", _PAGE_SHELL_MODULES, ids=lambda p: p.name,
)
def test_page_shell_renders_app_header(path):
    assert _renders_app_header(path), (
        f"{path.name} renders a page shell with no .app-header "
        "(docs/UX-AUDIT-2026-09-03.md §5.2)"
    )
