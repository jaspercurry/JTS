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
SPINNER_PAGE_CSS = tuple(
    ROOT / "deploy" / "assets" / page / f"{page}.css"
    for page in ("bluetooth", "home-assistant", "wifi")
)


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
        '.segmented__btn[aria-pressed="true"],',
        '.segmented__btn[aria-current="page"]',
        ".app-header__tabs",
        "[hidden] { display: none !important; }",
    ):
        assert marker in css, f"app.css missing shared primitive: {marker}"


def test_home_assistant_css_uses_real_tokens_only():
    css = (ROOT / "deploy/assets/home-assistant/home-assistant.css").read_text()
    assert "var(--text-muted" not in css
    assert "var(--space-" not in css
    assert "color: var(--muted);" in css


def test_spinner_primitive_is_shared_without_page_local_copies():
    css = APP_CSS.read_text()
    for marker in (
        ".spinner {",
        ".spinner--button {",
        "--spinner-color:",
        "@keyframes spinner-spin",
    ):
        assert marker in css, f"app.css missing shared spinner contract: {marker}"
    assert re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{"
        r"[^}]*\.spinner\s*\{\s*animation:\s*none;\s*\}",
        css,
        flags=re.S,
    ), "the shared spinner must stop completely under reduced motion"

    local_rule = re.compile(
        r"(?m)^\s*\.(?:spinner|btn-spinner|ha-spinner)\s*\{"
    )
    local_keyframes = re.compile(r"@keyframes\s+(?:bt|ha|wifi)-spin\b")
    offenders: list[str] = []
    for path in SPINNER_PAGE_CSS:
        page_css = _without_css_comments(path.read_text())
        if local_rule.search(page_css) or local_keyframes.search(page_css):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        "page styles must configure app.css's shared spinner instead of "
        "redefining it:\n" + "\n".join(offenders)
    )

    bluetooth_js = (
        ROOT / "deploy" / "assets" / "bluetooth" / "js" / "main.js"
    ).read_text()
    wifi_js = (
        ROOT / "deploy" / "assets" / "wifi" / "js" / "main.js"
    ).read_text()
    ha_js = (
        ROOT / "deploy" / "assets" / "home-assistant" / "js" / "main.js"
    ).read_text()
    assert "spinner spinner--button" in bluetooth_js
    assert "spinner spinner--button" in wifi_js
    assert "btn-spinner" not in bluetooth_js + wifi_js
    assert 's.className = "spinner"' in ha_js
    assert "ha-spinner" not in ha_js

    # Wi-Fi's ghost Scan button historically uses the light edge instead of
    # currentColor. Keep that choice explicit so promotion cannot alter it.
    wifi_css = SPINNER_PAGE_CSS[-1].read_text()
    assert "--button-spinner-color: var(--primary-foreground);" in wifi_css


def test_page_css_does_not_redeclare_canonical_toggle():
    offenders = []
    # The same inventory used by the focus-contract guards includes static
    # sheets/HTML and every jasper.web module that can carry a page_css string.
    for path in _focus_ring_css_sources():
        if path == APP_CSS:
            continue
        css = _without_css_comments(path.read_text())
        if re.search(r"(?m)^\s*\.toggle(?![\w-])", css):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        "canonical .toggle styling belongs only in deploy/assets/app.css: "
        + ", ".join(offenders)
    )


def test_app_css_owns_complete_canonical_toggle_contract():
    css = _without_css_comments(APP_CSS.read_text())
    for selector in (
        r"\.toggle\s*\{",
        r"\.toggle\s+input\s*\{",
        r"\.toggle\s+\.track\s*\{",
        r"\.toggle\s+\.track::before\s*\{",
        r"\.toggle\s+input:checked\s*\+\s*\.track\s*\{",
        r"\.toggle\s+input:checked\s*\+\s*\.track::before\s*\{",
        r"\.toggle\s+input:disabled\s*\+\s*\.track\s*\{",
    ):
        assert re.search(selector, css), (
            f"app.css missing canonical toggle selector: {selector}"
        )
    reduced_motion_blocks = re.findall(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\n\}",
        css,
        flags=re.S,
    )
    toggle_reduced_motion = next(
        (block for block in reduced_motion_blocks if ".toggle .track" in block),
        None,
    )
    assert toggle_reduced_motion, "app.css missing toggle reduced-motion contract"
    assert ".toggle .track::before" in toggle_reduced_motion
    assert "transition: none" in toggle_reduced_motion


def test_app_css_does_not_force_global_svg_size():
    # The landing page sets `svg { width:16px; height:16px }`, which would
    # squash the sound page's full-width EQ graph. The shared sheet must
    # only set `display`, sizing icons via the `.ico` helper instead.
    m = re.search(r"\bsvg\s*\{([^}]*)\}", APP_CSS.read_text())
    assert m, "expected a base svg rule"
    assert "width" not in m.group(1), "shared svg rule must not force a size"


def test_app_header_tab_strip_is_shared_not_page_local():
    """Sound originated the strip; Status now reuses it, so its layout has
    one owner in app.css while Sound keeps only its genuinely local live dot."""
    shared = _without_css_comments(APP_CSS.read_text())
    sound = _without_css_comments(
        (ROOT / "deploy/assets/sound-profile/sound.css").read_text()
    )
    assert re.search(r"\.app-header__tabs\s*\{", shared)
    assert re.search(r"\.app-header__tabs\s*>\s*div\s*\{", shared)
    assert not re.search(r"\.app-header__tabs\s*\{", sound)
    assert not re.search(r"\.app-header__tabs\s*>\s*div\s*\{", sound)
    assert ".app-header__tabs .segmented__btn.is-live::after" in sound


def test_segmented_labels_are_centered_for_links_and_buttons():
    body = _css_body(APP_CSS.read_text(), ".segmented__btn")
    assert "text-align: center" in body


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


def test_asset_version_is_url_safe_and_failsoft():
    # Fail-soft: with no readable build.txt the token falls back to a
    # valid (un-busted) value rather than raising.
    version = _common._asset_version()
    assert version
    assert re.fullmatch(r"[\w.-]+", version), version


def test_canonical_page_observes_manifest_replacement_in_warm_process(
    monkeypatch, tmp_path,
):
    """A wizard activated mid-deploy must not keep the prior asset URL."""
    manifest = tmp_path / "build.txt"
    manifest.write_text("JASPER_GIT_SHA=old123\n")
    monkeypatch.setattr(_common, "_ASSET_VERSION_PATH", str(manifest))

    first = _common.canonical_page(
        "Status", "", page_css_href="/assets/system-status/system.css",
    ).decode()
    assert '/assets/app.css?v=old123' in first
    assert '/assets/system-status/system.css?v=old123' in first

    replacement = tmp_path / "build.next"
    replacement.write_text("JASPER_GIT_SHA=new456\n")
    replacement.replace(manifest)

    second = _common.canonical_page(
        "Status", "", page_css_href="/assets/system-status/system.css",
    ).decode()
    assert '/assets/app.css?v=new456' in second
    assert '/assets/system-status/system.css?v=new456' in second


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


# ---------------------------------------------------------------- #
# docs/design-language.md — the craft rules, pinned.                 #
# Each test below is the enforcement arm of one section of that doc; #
# when a rule changes, change it there and here in the same PR.      #
# ---------------------------------------------------------------- #

DESIGN_LANGUAGE_DOC = ROOT / "docs" / "design-language.md"


def test_text_ramp_has_three_real_tiers():
    """design-language.md §4: --text / --muted / --muted-faint, and no
    synonym alias. --muted-strong used to alias --foreground, which left two
    real tiers and pushed pages into inventing a third with color-mix()."""
    css = _without_css_comments(APP_CSS.read_text())
    for token in ("--text:", "--muted:", "--muted-faint:"):
        assert token in css, f"app.css missing text-ramp tier {token}"
    assert "--muted-strong" not in css, (
        "--muted-strong aliased --foreground, so it was a synonym rather than "
        "a tier. Use --text for full-strength text and --muted-faint for the "
        "tertiary tier (docs/design-language.md §4)."
    )


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


# The landing page is the protected reference implementation
# (docs/design-language.md §2), so these three off-ladder sizes are HELD for
# owner review rather than corrected: 0.92rem -> 14px reflows the pair banner.
# The allowlist exists so the guard can still fail a NEW off-ladder value.
LANDING_OFF_LADDER_HELD = {"0.92rem", "0.88rem", "0.86rem"}
TYPE_LADDER_PX = {"11px", "12px", "13px", "14px", "16px"}


def test_landing_page_type_stays_on_the_ladder():
    """design-language.md §3: 11/12/13/14/16 px, and no new off-ladder value."""
    css = _without_css_comments(LANDING_HTML.read_text())
    sizes = set(re.findall(r"font-size:\s*([^;}]+)", css))
    unexpected = {
        s.strip() for s in sizes
        if s.strip() not in TYPE_LADDER_PX
        and s.strip() not in LANDING_OFF_LADDER_HELD
        and not s.strip().startswith("var(")
        and "em" not in s  # relative-to-parent sizing is not a ladder step
    }
    assert not unexpected, (
        "landing-page type must sit on the 11/12/13/14/16px ladder "
        f"(docs/design-language.md §3); found {sorted(unexpected)}"
    )


def test_touch_targets_meet_the_floor():
    """design-language.md §8: 44px preferred / 40px floor, grown WITHOUT
    changing the rendered box — an absolutely-positioned overlay, or the
    transparent input that already carries the hit.

    Geometry is asserted structurally here (pytest has no browser); the
    rendered-pixel invariance was verified by before/after capture at 375 /
    800 / 1280 px when these landed."""
    app = _without_css_comments(APP_CSS.read_text())
    landing = _without_css_comments(LANDING_HTML.read_text())

    # .icon-button: 32px disc, hit area grown to 44 by a -6px inset overlay.
    icon_after = _css_body(app, ".icon-button::after")
    assert "position: absolute" in icon_after and "inset: -6px" in icon_after, (
        ".icon-button must keep a 44px hit area via an absolute ::after overlay"
    )

    # .toggle: 44x24 switch, but the transparent input is the hit surface.
    toggle_input = _css_body(app, ".toggle input")
    assert "height: 44px" in toggle_input, (
        ".toggle's input carries the tap target and must be 44px tall"
    )
    assert "position: absolute" in toggle_input, (
        "the grown input must stay absolutely positioned so the switch still "
        "measures 44x24 and no layout moves"
    )

    # .mic-action: 30px pill on the landing page, overlay grows it to 44.
    mic_after = _css_body(landing, ".mic-action::after")
    assert "position: absolute" in mic_after and "inset: -7px 0" in mic_after, (
        ".mic-action must keep a 44px hit area via an absolute ::after overlay"
    )
    assert "position: relative" in _css_body(landing, ".mic-action"), (
        "the ::after overlay needs .mic-action to be a positioned ancestor"
    )


def test_design_language_doc_is_reachable_and_dated():
    """Documentation paradigm rules 3 and 8: every HANDOFF-style reference
    carries a Last verified footer and is listed in README's doc atlas."""
    doc = DESIGN_LANGUAGE_DOC.read_text()
    assert re.search(r"(?m)^Last verified: \d{4}-\d{2}-\d{2}$", doc), (
        "docs/design-language.md needs a `Last verified: YYYY-MM-DD` footer"
    )
    assert "design-language.md" in (ROOT / "README.md").read_text(), (
        "docs/design-language.md must be listed in README's documentation map"
    )
