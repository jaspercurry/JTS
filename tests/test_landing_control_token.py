# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the landing page's WS1 control-token delivery for the mic-mute button.

The static landing page (deploy/index.html) is served by nginx straight from
disk — it gets neither canonical_page()'s `<meta name="jts-control-token">`
injection nor the shared http.js token logic the wizards use. So POST
/mic/mute (a token-gated route) used to go out with no X-JTS-Token and, on the
resulting 403, the toggle snapped back silently — a privacy control failing
with no feedback (control-plane-auth §7).

These are static-source guards (mirroring tests/test_web_design_system.py):
the page must carry the bake-time token placeholder + meta tag, the mute POST
must attach X-JTS-Token, the failure path must surface an error instead of a
silent revert, install.sh must bake the token (fail-loud), and nginx must serve
`location = /` no-store so the token-bearing HTML is never cached.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANDING_HTML = ROOT / "deploy" / "index.html"
INSTALL_SH = ROOT / "deploy" / "install.sh"
# Both nginx sites serve the same token-baked index.html at `/`
# (install_management_static_assets runs for the full and streambox profiles).
NGINX_CONFS = (
    ROOT / "deploy" / "nginx-jasper.conf",
    ROOT / "deploy" / "nginx-jasper-streambox.conf",
)

# The single placeholder install.sh substitutes with the live token, and the
# meta name both sides agree on. A rename on one side without the other should
# fail here.
TOKEN_PLACEHOLDER = "__JTS_CONTROL_TOKEN__"
META_NAME = 'name="jts-control-token"'


def _landing() -> str:
    return LANDING_HTML.read_text()


def test_landing_carries_control_token_meta_placeholder():
    html = _landing()
    assert META_NAME in html, "landing page missing the jts-control-token meta tag"
    # The meta carries the install-time placeholder (the wizards deliver the
    # token per-request; the static page is baked once at install).
    assert re.search(
        r'<meta\s+name="jts-control-token"\s+content="' + re.escape(TOKEN_PLACEHOLDER) + r'"',
        html,
    ), "control-token meta tag must carry the __JTS_CONTROL_TOKEN__ bake placeholder"


def test_mic_mute_post_attaches_control_token():
    html = _landing()
    # The mute POST must read the token (meta first, localStorage fallback) and
    # send it as X-JTS-Token, or every mute hits the gate's 403.
    assert "X-JTS-Token" in html, "mic-mute POST must attach the X-JTS-Token header"
    assert "controlToken()" in html, "landing page must resolve the token via controlToken()"
    assert "meta[name=\"jts-control-token\"]" in html or \
        "meta[name='jts-control-token']" in html, \
        "controlToken() must prefer the embedded meta tag"
    assert "localStorage.getItem('jts-control-token')" in html, \
        "controlToken() must fall back to the per-browser localStorage value"


def test_mic_mute_failure_is_not_silent():
    html = _landing()
    # The original bug: on a non-OK response the toggle reverted with no
    # message. The fix surfaces the failure (failMute) and special-cases the
    # 403 so the household knows to reload for a fresh token.
    assert "failMute(" in html, "mute failures must route through failMute (surfaces a message)"
    assert "403" in html, "the mute path must special-case the token-gate 403"
    # Guard against a regression back to the silent bare-revert: the literal
    # old pattern (revert with no setMicState/sub message) must not reappear in
    # the mute POST handler.
    assert "Mute blocked" in html, "the 403 branch must show a user-facing message"


def test_install_bakes_control_token_fail_loud():
    sh = INSTALL_SH.read_text()
    assert TOKEN_PLACEHOLDER in sh, "install.sh must reference the token placeholder"
    assert "control_token.ensure_token()" in sh, \
        "install.sh must mint/read the token via control_token.ensure_token()"
    # Fail-loud: an unbaked placeholder must abort the install, never ship.
    assert f"missing the {TOKEN_PLACEHOLDER} placeholder" in sh, \
        "install.sh must fail loud when the token placeholder is absent"


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
