r"""XSS regression guard for the /tools/ catalog renderer.

render.js builds card/detail markup from catalog fields (name, summary,
description, details, labels, category, pack, setup_url) and assigns it via
innerHTML. The /tools/ catalog is the marketplace's future home for
THIRD-PARTY tool text, so the rendering is a security boundary. Two distinct
defenses, both exercised here:

  * Element/attribute content (name, description, labels, data-tool) is run
    through escapeHtml, so a `<script>`/`<img>`/`<svg>` payload can't break out.
  * The setup_url `<a href>` is NOT covered by escaping alone — escapeHtml
    escapes characters but does not validate origin/scheme, so a `javascript:`
    href or an off-origin path would survive escaping. render.js's safeSetupUrl
    must RESOLVE the value against the page origin and drop anything that leaves
    it — the scheme class (`javascript:`) and the off-origin class, which
    includes protocol-relative `//host`, the backslash form `/\host`, and the
    whitespace-obfuscated `/<TAB>/host` / `/<LF>/host` (the URL parser folds
    "\" -> "/" and strips tab/newline before parsing, so a second-character
    regex misses these). We assert all are dropped while a real "/transit/"
    link still renders.

The conventions test only asserts the escaper is imported and not re-declared;
this one renders deliberately malicious tools through the real module (via a
Node harness) and asserts no executable markup survives.

Skips when node isn't on PATH (e.g. a CI image without it).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_HARNESS = Path("tests/js/tools_render_harness.mjs")
_ESCAPE = Path("deploy/assets/shared/js/escape.js")
_RENDER = Path("deploy/assets/tools/js/render.js")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not on PATH")


def test_render_escapes_every_untrusted_tool_field():
    proc = subprocess.run(
        [_NODE, str(_HARNESS), str(_ESCAPE), str(_RENDER)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"render harness errored:\n{proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    # A malicious name/description/label/setup_url must not produce a raw tag —
    # every untrusted `<` must be escaped to `&lt;`, so none of the payload tag
    # types survive.
    assert out["noScriptTag"] is True, "a <script> payload survived escaping"
    assert out["noImgTag"] is True, "an <img> payload survived escaping"
    assert out["noSvgTag"] is True, "an <svg> payload survived escaping"
    # And the payloads were genuinely escaped (entities present), not dropped.
    assert out["escapedEntitiesPresent"] is True
    # The setup_url href is its own boundary: safeSetupUrl must drop the
    # scheme class (javascript:) AND the off-origin class — //host, /\host, and
    # the whitespace-obfuscated /<TAB>/host, /<LF>/host that resolve off-origin
    # — while a real same-origin path still renders. noOffOriginHref resolves
    # every rendered href against a fixed base, so it catches the tab/newline
    # forms a raw-string regex would miss.
    assert out["noJavascriptScheme"] is True, "a javascript: setup_url survived"
    assert out["noOffOriginHref"] is True, "an off-origin setup_url href survived"
    assert out["safeHrefRendered"] is True, "a safe /transit/ setup link was dropped"
    # needs_setup with no setup_url renders an honest Unavailable badge, never
    # a dead disabled checkbox (the flag_recent_issue degraded case).
    assert out["unavailableRendered"] is True, "no Unavailable badge for a urlless needs_setup tool"
    assert out["noDeadToggle"] is True, "a urlless needs_setup tool rendered a toggle"
    assert out["noOnOffBadges"] is True, "active/off status badges should not render"
    assert out["packCardsClickable"] is True, "pack cards should expose full-card navigation"
