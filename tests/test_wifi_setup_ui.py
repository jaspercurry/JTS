# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static UI contract tests for jasper.web.wifi_setup.

The nmcli behavior is covered elsewhere; this file pins the page-level
contracts that are easy to regress during markup edits.

Post-migration /wifi/ is a pure ES-module page: _landing_html() renders only
the canonical shell + a thin skeleton. The radio toggle, connect/forget
panels, fetch wiring, and per-network actions are rendered at runtime by
deploy/assets/wifi/js/main.js. What IS pinned here (hardware-free, no
browser/DOM) is the Python-rendered contract: the CSRF meta the module
reads, the page correctly delegating to the real module file, and the legacy
server-rendered switch/inline-JS markup staying gone from the shell. The
module's own rendered *structure* (the current-network card, saved/available
network rows, scan health) is pinned by the Node DOM harness at
tests/js/wifi_render_harness.mjs, run below.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jasper.web import wifi_setup

_NODE = shutil.which("node")
_WIFI_MAIN_JS = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "assets" / "wifi" / "js" / "main.js"
)
_HARNESS = Path("tests/js/wifi_render_harness.mjs")


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


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_wifi_module_renders_its_states_with_the_expected_dom_structure():
    proc = subprocess.run(
        [_NODE, str(_HARNESS), str(_WIFI_MAIN_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"wifi render harness errored:\n{proc.stderr}"
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {"ok": True}
