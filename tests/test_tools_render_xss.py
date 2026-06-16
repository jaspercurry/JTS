"""XSS regression guard for the /tools/ catalog renderer.

render.js builds card markup from catalog fields (name, description, labels,
setup_url) and assigns it via innerHTML. The /tools/ catalog is the
marketplace's future home for THIRD-PARTY tool text, so the rendering is a
security boundary. Two distinct defenses, both exercised here:

  * Element/attribute content (name, description, labels, data-tool) is run
    through escapeHtml, so a `<script>`/`<img>`/`<svg>` payload can't break out.
  * The setup_url `<a href>` is NOT covered by escaping alone — escapeHtml
    escapes characters but does not validate schemes, so a `javascript:` href
    would survive escaping. render.js's safeSetupUrl must reject any non
    "/..."-path scheme; we assert the `javascript:` URL is dropped (not just
    escaped) while a real "/transit/" link still renders.

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
    # scheme class (javascript:) AND the off-origin class (//host, /\host),
    # while a real same-origin path still renders.
    assert out["noJavascriptScheme"] is True, "a javascript: setup_url survived"
    assert out["noOffOriginHref"] is True, "an off-origin setup_url href survived"
    assert out["safeHrefRendered"] is True, "a safe /transit/ setup link was dropped"
    # needs_setup with no setup_url renders an honest Unavailable badge, never
    # a dead disabled checkbox (the flag_recent_issue degraded case).
    assert out["unavailableRendered"] is True, "no Unavailable badge for a urlless needs_setup tool"
    assert out["noDeadToggle"] is True, "a urlless needs_setup tool rendered a toggle"
