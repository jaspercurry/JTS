"""Static UI contract tests for jasper.web.bluetooth_setup.

The behavioral Bluetooth path is hardware-backed, so these tests stay
small and pin the browser-side safety contracts: semantic switches,
CSRF-aware JSON writes, and data attributes instead of generated inline
JavaScript for device metadata.
"""
from __future__ import annotations

from jasper.web import bluetooth_setup


def test_landing_html_uses_semantic_switches_and_csrf_meta():
    html = bluetooth_setup._landing_html("csrf-token").decode("utf-8")

    assert 'meta name="jts-csrf" content="csrf-token"' in html
    assert 'type="checkbox" id="sw-power"' in html
    assert 'type="checkbox" id="sw-disc"' in html
    assert 'class="switch"' not in html
    assert "headers: jsonHeaders()" in html


def test_device_actions_use_data_attributes_not_inline_js():
    html = bluetooth_setup._landing_html().decode("utf-8")

    assert 'data-action="connect"' in html
    assert 'data-action="forget"' in html
    assert 'data-action="pair"' in html
    assert 'onclick="connectDevice' not in html
    assert 'onclick="startPair' not in html
    assert 'onclick="forget(' not in html
    assert "iconSlug(d.icon)" in html
