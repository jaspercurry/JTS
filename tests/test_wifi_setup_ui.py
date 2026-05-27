"""Static UI contract tests for jasper.web.wifi_setup.

The nmcli behavior is covered elsewhere; this file pins the page-level
contracts that are easy to regress during markup edits.
"""
from __future__ import annotations

from jasper.web import wifi_setup


def test_landing_html_uses_semantic_radio_switch_and_csrf_meta():
    html = wifi_setup._landing_html("csrf-token").decode("utf-8")

    assert 'meta name="jts-csrf" content="csrf-token"' in html
    assert 'id="radio-toggle"' in html
    assert 'class="switch"' not in html
    assert "headers: jsonHeaders()" in html


def test_network_actions_use_data_attributes_not_inline_js_args():
    html = wifi_setup._landing_html().decode("utf-8")

    assert 'data-action="open-connect"' in html
    assert 'data-action="submit-connect"' in html
    assert 'data-action="open-forget"' in html
    assert "function jsArg" not in html
    assert 'onclick="toggleRadio' not in html
    assert "openConnect('" not in html
    assert "submitForget('" not in html
