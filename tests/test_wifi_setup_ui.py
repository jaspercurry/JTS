# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static UI contract tests for jasper.web.wifi_setup.

The nmcli behavior is covered elsewhere; this file pins the page-level
contracts that are easy to regress during markup edits.

Post-migration /wifi/ is a pure ES-module page: _landing_html() renders only
the canonical shell + a thin skeleton, and the radio toggle, connect/forget
panels, fetch wiring, and per-network actions are rendered by
deploy/assets/wifi/js/main.js. These contracts now read that module — same
guarantees (semantic native-checkbox toggle, CSRF-meta-backed fetch, data-*
actions instead of inline-onclick args), at their new canonical home.
"""
from __future__ import annotations

from pathlib import Path

from jasper.web import wifi_setup

_WIFI_MAIN_JS = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "assets" / "wifi" / "js" / "main.js"
)


def test_landing_html_uses_semantic_radio_switch_and_csrf_meta():
    html = wifi_setup._landing_html("csrf-token").decode("utf-8")
    js = _WIFI_MAIN_JS.read_text()

    # CSRF token still rides in the page meta tag (the module reads it).
    assert 'meta name="jts-csrf" content="csrf-token"' in html
    # The Wi-Fi radio control is the canonical native-checkbox toggle, not a
    # clickable <div class="switch">. The toggle markup is rendered by the
    # module now, so assert it there.
    assert 'class="toggle"' in js
    assert 'type="checkbox"' in js
    # The legacy clickable-div switch must be gone from both the page and the
    # module.
    assert 'class="switch"' not in html
    assert 'class="switch"' not in js
    # Mutating fetches go through the shared jsonHeaders() (X-CSRF-Token).
    assert "jsonHeaders" in js


def test_network_actions_use_data_attributes_not_inline_js_args():
    html = wifi_setup._landing_html().decode("utf-8")
    js = _WIFI_MAIN_JS.read_text()

    # Per-network actions ride in data-* attributes read by a delegated
    # handler — these are rendered by the module now.
    assert 'data-action="open-connect"' in js
    assert 'data-action="submit-connect"' in js
    assert 'data-action="open-forget"' in js
    # And no untrusted SSID/name is interpolated into inline JS — the old
    # inline-onclick / jsArg anti-patterns stay gone from both surfaces.
    for anti in ("function jsArg", 'onclick="toggleRadio', "openConnect('", "submitForget('"):
        assert anti not in html
        assert anti not in js
