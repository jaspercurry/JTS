# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static UI contract tests for jasper.web.wifi_setup.

The nmcli behavior is covered elsewhere; this file pins the page-level
contracts that are easy to regress during markup edits.

Post-migration /wifi/ is a pure ES-module page: _landing_html() renders only
the canonical shell + a thin skeleton. The radio toggle, connect/forget
panels, fetch wiring, and per-network actions are rendered at runtime by
deploy/assets/wifi/js/main.js — this suite is hardware-free and has no
browser/DOM, so it cannot execute that module or assert on what it renders
(there is no tests/js/wifi_*.mjs harness for it, unlike bluetooth/crossover).
What IS pinned here is the Python-rendered contract: the CSRF meta the module
reads, the page correctly delegating to the real module file, and the legacy
server-rendered switch/inline-JS markup staying gone from the shell.
"""
from __future__ import annotations

from pathlib import Path

from jasper.web import wifi_setup

_WIFI_MAIN_JS = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "assets" / "wifi" / "js" / "main.js"
)


def test_landing_html_uses_csrf_meta_and_drops_legacy_switch_markup():
    html = wifi_setup._landing_html("csrf-token").decode("utf-8")

    # CSRF token still rides in the page meta tag (the module reads it).
    assert 'meta name="jts-csrf" content="csrf-token"' in html
    # The legacy clickable-div switch and inline-onclick/jsArg anti-patterns
    # must stay out of the server-rendered shell.
    for anti in (
        'class="switch"', "function jsArg", 'onclick="toggleRadio',
        "openConnect('", "submitForget('",
    ):
        assert anti not in html


def test_landing_html_delegates_to_the_real_wifi_module():
    html = wifi_setup._landing_html().decode("utf-8")
    assert '<script type="module" src="/assets/wifi/js/main.js">' in html
    assert _WIFI_MAIN_JS.is_file(), f"missing {_WIFI_MAIN_JS}"
