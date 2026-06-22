# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static UI contract tests for jasper.web.bluetooth_setup.

The behavioral Bluetooth path is hardware-backed, so these tests stay
small and pin the browser-side safety contracts: semantic switches,
CSRF-aware JSON writes, and data attributes instead of generated inline
JavaScript for device metadata.

Since the page's migration to the canonical design system, its behaviour
ships as a static ES module (deploy/assets/bluetooth/js/main.js) rather
than an inline <script>, so the JS-side contracts (jsonHeaders() CSRF
writes, data-action attributes for untrusted device rows) are asserted
against that module file; the server-rendered HTML still owns the CSRF
meta tag and the semantic toggle switches.
"""
from __future__ import annotations

from pathlib import Path

from jasper.web import bluetooth_setup

_MODULE_JS = Path("deploy/assets/bluetooth/js/main.js")


def test_landing_html_uses_semantic_switches_and_csrf_meta():
    html = bluetooth_setup._landing_html("csrf-token").decode("utf-8")

    assert 'meta name="jts-csrf" content="csrf-token"' in html
    # Semantic <input type=checkbox> toggles (from the shared toggle_html()
    # helper), never a clickable <div class="switch">. toggle_html() renders
    # the id before the type attribute.
    assert 'id="sw-power" type="checkbox"' in html
    assert 'id="sw-disc" type="checkbox"' in html
    assert 'class="toggle"' in html
    assert 'class="switch"' not in html


def test_module_uses_csrf_aware_json_writes():
    """CSRF-aware JSON writes moved with the JS into the ES module: every
    mutating POST goes through jsonHeaders() (which attaches X-CSRF-Token),
    never a raw inline Content-Type header."""
    js = _MODULE_JS.read_text()
    assert "headers: jsonHeaders()" in js
    # jsonHeaders comes from the shared http.js module, not re-declared here.
    assert '"/assets/shared/js/http.js"' in js
    assert '"Content-Type": "application/json"' not in js


def test_device_actions_use_data_attributes_not_inline_js():
    """Device rows are rendered client-side in the ES module; untrusted
    device metadata rides in escaped data-* attributes consumed by a single
    delegated click handler, never generated inline onclick."""
    js = _MODULE_JS.read_text()
    assert 'data-action="connect"' in js
    assert 'data-action="forget"' in js
    assert 'data-action="pair"' in js
    assert 'onclick="connectDevice' not in js
    assert 'onclick="startPair' not in js
    # The server HTML carries no inline onclick either.
    html = bluetooth_setup._landing_html().decode("utf-8")
    assert "onclick=" not in html


def test_bluetooth_module_has_no_code_entry_flow():
    js = _MODULE_JS.read_text()
    assert "confirm_passkey" not in js
    assert "request_passkey" not in js
    assert "request_pincode" not in js
    assert "data-pair-action" not in js
