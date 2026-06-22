# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
_DETAIL_HARNESS = Path("tests/js/tools_detail_harness.mjs")
_ESCAPE = Path("deploy/assets/shared/js/escape.js")
_RENDER = Path("deploy/assets/tools/js/render.js")
_DETAIL = Path("deploy/assets/tools/js/detail.js")

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
    assert out["configuredSetupLinkRendered"] is True, "configured setup pages should render as Configure links"
    # needs_setup with no setup_url renders an honest Unavailable badge, never
    # a dead disabled checkbox (the flag_recent_issue degraded case).
    assert out["unavailableRendered"] is True, "no Unavailable badge for a urlless needs_setup tool"
    assert out["noDeadToggle"] is True, "a urlless needs_setup tool rendered a toggle"
    assert out["noOnOffBadges"] is True, "active/off status badges should not render"
    assert out["packCardsClickable"] is True, "pack cards should expose full-card navigation"
    assert out["toolCountInTitleRow"] is True, "pack/detail tool counts should sit by the title"
    assert out["noDuplicateDetailBack"] is True, "detail cards should not duplicate the header back link"
    assert out["guideLinkRendered"] is True, "pack details should link to the authoring guide"
    assert out["noCustomPromptCount"] is True, "custom prompt counts should not render as metadata"
    assert out["noTimeoutMetadata"] is True, "tool timeout metadata should not render"
    assert out["noRiskFlagMetadata"] is True, "risk flags should not render in the operator UI"
    assert out["toolTitleDisclosure"] is True, (
        "tool details should disclose from the tool title/description row"
    )
    assert out["resetOnlyForCustomPrompt"] is True, "reset should appear only for custom prompts"
    assert out["saveStartsHiddenDisabled"] is True, "save should start hidden and disabled"
    assert out["cancelStartsHidden"] is True, "cancel should start hidden"


def test_prompt_editor_actions_follow_view_and_edit_modes():
    proc = subprocess.run(
        [_NODE, str(_DETAIL_HARNESS), str(_DETAIL)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"detail harness errored:\n{proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] is True
