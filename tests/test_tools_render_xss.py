"""XSS regression guard for the /tools/ catalog renderer.

render.js builds card markup from catalog fields (name, description, labels,
setup_url) and assigns it via innerHTML. The /tools/ catalog is the
marketplace's future home for THIRD-PARTY tool text, so escaping every field
through the shared escapeHtml is a security boundary. The conventions test only
asserts the escaper is imported and not re-declared; this one renders a
deliberately malicious tool through the real module (via a Node harness) and
asserts no executable markup survives — turning the runtime-verified claim into
a regression-guarded one.

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
