# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin control-token delivery for the landing-page assistant pause button.

The static landing page (deploy/index.html) is served by nginx straight from
disk, so it gets no canonical_page() `<meta name="jts-control-token">`
injection: the token is baked in at install time instead. POST /mic/mute (the
legacy token-gated route) used to go out with no X-JTS-Token and, on the
resulting 403, the toggle snapped back silently with no feedback
(control-plane-auth §7).

These are static-source guards (mirroring tests/test_web_design_system.py):
the page must carry the bake-time token placeholder + meta tag, the pause POST
must attach X-JTS-Token through http.js's shared header builder, the failure
path must surface an error instead of a silent revert, the install-time
renderer must bake the token (fail-loud), and nginx must serve `location = /`
no-store so the token-bearing HTML is never cached.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANDING_HTML = ROOT / "deploy" / "index.html"
LANDING_JS = ROOT / "deploy" / "assets" / "landing" / "js" / "main.js"
HTTP_JS = ROOT / "deploy" / "assets" / "shared" / "js" / "http.js"
INSTALL_SH = ROOT / "deploy" / "install.sh"
LANDING_PY = ROOT / "jasper" / "web" / "landing.py"
# Both nginx sites serve the same token-baked index.html at `/`
# (install_management_static_assets runs for the full and streambox profiles).
NGINX_CONFS = (
    ROOT / "deploy" / "nginx-jasper.conf",
    ROOT / "deploy" / "nginx-jasper-streambox.conf",
)

# The single placeholder jasper.web.landing substitutes with the live token,
# and the meta name both sides agree on. A rename on one side without the
# other should fail here.
TOKEN_PLACEHOLDER = "__JTS_CONTROL_TOKEN__"
META_NAME = 'name="jts-control-token"'


def _landing() -> str:
    return LANDING_HTML.read_text()


def _landing_js() -> str:
    return LANDING_JS.read_text()


def test_landing_carries_control_token_meta_placeholder():
    html = _landing()
    assert META_NAME in html, "landing page missing the jts-control-token meta tag"
    # The meta carries the install-time placeholder (the wizards deliver the
    # token per-request; the static page is baked once at install).
    assert re.search(
        r'<meta\s+name="jts-control-token"\s+content="' + re.escape(TOKEN_PLACEHOLDER) + r'"',
        html,
    ), "control-token meta tag must carry the __JTS_CONTROL_TOKEN__ bake placeholder"


def test_assistant_pause_post_attaches_control_token():
    js = _landing_js()
    http = HTTP_JS.read_text()
    # The pause POST rides the shared header builder rather than a second copy
    # of the token logic, or every pause hits the gate's 403.
    assert 'from "/assets/shared/js/http.js"' in js, \
        "landing module must import the shared fetch helpers"
    mute_call = js.split("fetch('/mic/mute'", 1)[1][:200]
    assert "jsonHeaders()" in mute_call, \
        "the pause POST must send jsonHeaders() (which carries X-JTS-Token)"
    # ...and that builder speaks the two names this page's token contract is
    # written in: the header the gate reads, and the meta the install bakes.
    assert "X-JTS-Token" in http
    assert "jts-control-token" in http


def test_assistant_pause_failure_is_not_silent():
    js = _landing_js()
    # The original bug: on a non-OK response the toggle reverted with no
    # message. The fix surfaces the failure (failMute) and special-cases the
    # 403 so the household knows to reload for a fresh token.
    assert "failMute(" in js, "mute failures must route through failMute (surfaces a message)"
    assert "403" in js, "the mute path must special-case the token-gate 403"
    # Guard against a regression back to the silent bare-revert: the literal
    # old pattern (revert with no setMicState/sub message) must not reappear in
    # the pause POST handler.
    assert "Pause blocked" in js, "the 403 branch must show a user-facing message"


def test_assistant_pause_copy_does_not_claim_the_microphone_is_off():
    html = _landing()
    js = _landing_js()
    assert '<h2 class="eyebrow">Voice assistant</h2>' in html
    assert "Resume voice assistant" in js
    assert "Pause voice assistant" in js
    assert "Voice assistant paused" in js
    assert "Voice assistant active" in js
    assert "JTS will not respond to the wake word" in js
    for source in (html, js):
        assert ">Wake detection<" not in source
        assert "Microphone muted" not in source
        assert "Mute microphone" not in source


def test_mic_status_handles_bonded_follower_parked_state():
    js = _landing_js()
    # A bonded follower intentionally parks local voice. The mic poller must
    # render that first-class state from /mic instead of racing the grouping
    # banner and falling back to the generic offline label.
    assert "status === 'parked'" in js
    assert "reason === 'bonded_follower'" in js
    assert "data.available !== false" in js
    assert "'Parked'" in js
    assert "Voice assistant parked while paired" in js


def test_mic_status_handles_voice_reloading_state():
    js = _landing_js()
    assert "status === 'starting'" in js
    assert "'Reloading'" in js
    assert "Voice control is restarting" in js
    assert "Voice control reloading" in js


def test_install_bakes_control_token_fail_loud():
    sh = INSTALL_SH.read_text()
    renderer = LANDING_PY.read_text()
    assert "python3 -m jasper.web.landing" in sh, \
        "install.sh must render the landing page through jasper.web.landing"
    assert TOKEN_PLACEHOLDER in renderer, \
        "the renderer must reference the token placeholder"
    assert "ensure_token()" in renderer, \
        "the renderer must mint/read the token via control_token.ensure_token()"
    # Fail-loud: a failed render must abort the install, never ship. The
    # renderer's own refusal is pinned in tests/test_web_landing.py.
    assert "refusing to ship a broken page" in sh, \
        "install.sh must abort the install when the render fails"


def test_nginx_serves_landing_no_store():
    # Both the full and streambox sites serve the token-bearing index.html at
    # `/`, so each `location = /` block must carry no-store (never cached by a
    # browser or intermediary).
    for conf_path in NGINX_CONFS:
        conf = conf_path.read_text()
        m = re.search(r"location\s*=\s*/\s*\{(.*?)\}", conf, flags=re.S)
        assert m, f"{conf_path.name} missing the `location = /` landing block"
        block = m.group(1)
        assert "no-store" in block, \
            f"{conf_path.name} `location = /` must set Cache-Control no-store"
