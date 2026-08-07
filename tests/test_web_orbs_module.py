# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the shared thinking-orb canvas component.

`deploy/assets/shared/js/orbs.js` is a port of MIT-licensed upstream code
whose colours come from `app.css` tokens. Three promises are load-bearing and
none of them is checkable by reading the JS alone at runtime, so they are
pinned here:

1. **Attribution survives.** The port is a derivative work; MIT requires the
   copyright and permission notice to travel with it. A refactor that drops
   the notice file, the header, or the inventory row is a licensing defect,
   not a style nit.
2. **The palette has one owner.** The module's whole reason for reading
   `--orb-ink` / `--orb-paper` is that `app.css` is the single source of
   truth for colour. A hardcoded palette creeping back in would silently
   fork it.
3. **Every advertised state can actually be drawn.** `ORB_STATES` is the
   public list pages iterate; a state with no mode painter is a runtime
   crash on whichever page reaches for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ORBS_JS = Path("deploy/assets/shared/js/orbs.js")
APP_CSS = Path("deploy/assets/app.css")
NOTICE = Path("LICENSES/ThinkingOrbs-MIT.txt")
INVENTORY = Path("LICENSE-third-party.md")

UPSTREAM_URL = "https://github.com/Jakubantalik/thinking-orbs"
UPSTREAM_COMMIT = "e94f207ea122f8cca0aaa6409ab7fe82d55c38f1"

# The nine states upstream ships, and the mode painter each resolves to.
EXPECTED_STATES = {
    "working": "orbits",
    "searching": "globe",
    "solving": "rubik",
    "listening": "wave",
    "connecting": "web",
    "weaving": "braid",
    "composing": "ribbon",
    "breathing": "ring",
    "shaping": "morph",
}


@pytest.fixture(scope="module")
def orbs_src() -> str:
    assert ORBS_JS.is_file(), f"{ORBS_JS} (shared orb component) is missing"
    return ORBS_JS.read_text()


# --------------------------------------------------------------------------
# 1. Attribution
# --------------------------------------------------------------------------


def test_orbs_module_carries_mit_and_both_copyright_holders(orbs_src: str) -> None:
    """The file is a derivative of MIT code, so it is MIT — not the project's
    Apache-2.0 default — and names both holders."""
    header = orbs_src[:1200]
    assert "SPDX-License-Identifier: MIT" in header, (
        "orbs.js ports MIT-licensed draw math; its SPDX identifier must stay "
        "MIT rather than inheriting the project's Apache-2.0 default"
    )
    assert "SPDX-FileCopyrightText: 2026 Jakub Antalik" in header, (
        "the upstream author's copyright line must not be dropped"
    )
    assert "SPDX-FileCopyrightText: 2026 Jasper Curry" in header


def test_orbs_module_points_at_upstream_and_the_preserved_notice(orbs_src: str) -> None:
    assert UPSTREAM_URL in orbs_src, "orbs.js must name its upstream project"
    assert UPSTREAM_COMMIT in orbs_src, (
        "orbs.js must pin the upstream commit it was taken from, so a future "
        "re-sync knows what it is diffing against"
    )
    assert str(NOTICE) in orbs_src, f"orbs.js must point readers at {NOTICE}"


def test_upstream_mit_notice_is_preserved_verbatim() -> None:
    """MIT's one obligation: the copyright line and the permission notice
    ship with the code."""
    assert NOTICE.is_file(), f"{NOTICE} (preserved upstream notice) is missing"
    text = NOTICE.read_text()
    assert "Copyright (c) 2026 Jakub Antalik" in text
    assert "Permission is hereby granted, free of charge" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text
    assert UPSTREAM_URL in text
    assert UPSTREAM_COMMIT in text, (
        "the notice records which upstream commit was verified; keep it "
        "in step with the header in orbs.js"
    )


def test_third_party_inventory_has_a_row_for_the_orb_engine() -> None:
    """LICENSE-third-party.md is the redistribution checklist. A vendored
    port that is not in it will be missed when an image is published."""
    rows = [
        line for line in INVENTORY.read_text().splitlines()
        if line.startswith("|") and "thinking-orbs" in line
    ]
    assert rows, (
        "LICENSE-third-party.md has no row for the thinking-orbs port — add "
        "one to the 'In-repo and JTS-hosted Assets' table"
    )
    row = "\n".join(rows)
    assert str(ORBS_JS) in row, "the inventory row must name the vendored file"
    assert str(NOTICE) in row, "the inventory row must name the preserved notice"
    assert UPSTREAM_COMMIT in row, "the inventory row must pin the verified commit"


# --------------------------------------------------------------------------
# 2. One owner for the palette
# --------------------------------------------------------------------------


def test_app_css_defines_the_orb_ink_ramp() -> None:
    """orbs.js resolves these two custom properties; app.css is where they
    live. Without them every orb silently falls back to its built-in pair
    and stops tracking the palette."""
    css = APP_CSS.read_text()
    root = css[css.index(":root {"): css.index("/* ---------------- base reset")]
    # Strip comments first. The explanatory block above these tokens mentions
    # `--orb-paper: var(--card)` as an override example, and a substring search
    # matched THAT instead of the declaration — deleting the real declaration
    # left this test green. Assert against code, never against prose.
    code = re.sub(r"/\*.*?\*/", "", root, flags=re.DOTALL)

    ink = re.search(r"^\s*--orb-ink:\s*(.+?);", code, re.MULTILINE)
    paper = re.search(r"^\s*--orb-paper:\s*(.+?);", code, re.MULTILINE)
    assert ink, (
        "app.css :root must declare --orb-ink — it is the single source of "
        "truth for orb colour (see orbs.js)"
    )
    assert paper, (
        "app.css :root must declare --orb-paper — it is the single source of "
        "truth for orb colour (see orbs.js)"
    )
    assert "var(--primary)" in ink.group(1), (
        "--orb-ink must derive from --primary so orbs re-skin with the "
        f"palette instead of pinning their own green; got {ink.group(1)!r}"
    )
    assert "var(--background)" in paper.group(1), (
        "--orb-paper must derive from --background so an orb dissolves into "
        f"the page it sits on; got {paper.group(1)!r}"
    )


def test_orbs_module_reads_the_tokens_rather_than_hardcoding_them(orbs_src: str) -> None:
    assert "'--orb-ink'" in orbs_src and "'--orb-paper'" in orbs_src, (
        "orbs.js must resolve its colours from the CSS custom properties"
    )
    # Exactly one hardcoded pair is allowed: the fallback used when the module
    # runs on a page that does not link app.css. It is declared once, as RGB,
    # and the CSS var() chains are built from it — so pin the declaration.
    # A hex literal in the source would mean that single writer had been
    # forked back into two.
    for name, expected in (
        ("FALLBACK_INK", "[30, 45, 32]"),
        ("FALLBACK_PAPER", "[247, 241, 232]"),
    ):
        assert f"const {name} = {expected};" in orbs_src, (
            f"{name} changed or moved. It is the colour an orb falls back to "
            "on a page that does not link app.css; changing it silently "
            "changes that page's rendering."
        )
    hexes = set(re.findall(r"#[0-9a-fA-F]{6}\b", orbs_src))
    hexes -= {"#ff00ff"}  # the colour-parse sentinel in rasterize()
    assert hexes == set(), (
        "orbs.js grew hardcoded colours: "
        f"{sorted(hexes)}. Colour belongs in app.css tokens, and the fallback "
        "pair is declared once as RGB with the var() chains built from it."
    )


def test_orbs_module_does_not_reimplement_dark_mode(orbs_src: str) -> None:
    """Upstream branches on a `dark` boolean. The port deletes that branch:
    a dark surface is expressed by swapping the two tokens, so there is no
    second code path to keep in step."""
    assert not re.search(r"\bdark\s*\?", orbs_src), (
        "orbs.js reintroduced a dark/light branch — theme belongs in the "
        "--orb-ink / --orb-paper pair, not in the renderer"
    )


# --------------------------------------------------------------------------
# 3. Every advertised state can be drawn
# --------------------------------------------------------------------------


def _js_object_body(src: str, decl: str) -> str:
    """The body of a `<decl> = { ... }` object literal."""
    start = src.index(decl)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1: i]
    pytest.fail(f"could not find the end of {decl}")  # pragma: no cover


def _js_object_keys(src: str, decl: str) -> list[str]:
    """Top-level keys of a `<decl> = { ... }` object literal."""
    return re.findall(r"^\s{2}(\w+):", _js_object_body(src, decl), re.MULTILINE)


def _js_string_map(src: str, decl: str) -> dict[str, str]:
    """Top-level `key: 'value'` pairs of a `<decl> = { ... }` object literal."""
    return dict(
        re.findall(r"^\s{2}(\w+):\s*'(\w+)'", _js_object_body(src, decl), re.MULTILINE)
    )


def test_every_advertised_state_has_a_mode_painter(orbs_src: str) -> None:
    start = orbs_src.index("export const ORB_STATES")
    listed = re.findall(r"'(\w+)'", orbs_src[start: orbs_src.index("]", start)])
    assert listed == list(EXPECTED_STATES), (
        f"ORB_STATES drifted from the nine upstream states: {listed}"
    )

    # Compare the VALUES too, not just the keys. An earlier version of this
    # test spelled out the full state -> mode mapping and then only ever used
    # its keys, so rewiring all nine states to the same painter left the whole
    # suite green while every orb rendered the same animation.
    mapping = _js_string_map(orbs_src, "const STATE_TO_MODE")
    assert mapping == EXPECTED_STATES, (
        "STATE_TO_MODE drifted from the upstream state -> mode mapping.\n"
        f"  expected: {EXPECTED_STATES}\n  actual:   {mapping}"
    )

    painters = set(_js_object_keys(orbs_src, "export const MODE_DRAWS"))
    missing = sorted(set(EXPECTED_STATES.values()) - painters)
    assert not missing, (
        f"these modes are mapped from a state but have no painter: {missing}"
    )


def test_every_mode_has_a_base_profile_and_both_size_presets(orbs_src: str) -> None:
    """A state whose mode is missing a 64 or 20 preset throws on the first
    frame, which on the landing page means a blank welcome."""
    profiles = set(_js_object_keys(orbs_src, "const BASE_PROFILES"))
    presets = set(_js_object_keys(orbs_src, "const PRESETS"))
    for mode in sorted(set(EXPECTED_STATES.values())):
        assert mode in profiles, f"BASE_PROFILES is missing '{mode}'"
        assert mode in presets, f"PRESETS is missing '{mode}'"

    block = orbs_src[orbs_src.index("const PRESETS"):orbs_src.index("const presetCache")]
    for mode in sorted(presets):
        chunk = block[block.index(f"  {mode}: {{"):]
        chunk = chunk[: chunk.index("\n  }")]
        for size in ("64:", "20:"):
            assert size in chunk, f"PRESETS.{mode} is missing the {size.rstrip(':')} preset"
