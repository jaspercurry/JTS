# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static UI contract tests for jasper.web.bluetooth_setup.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jasper.web import bluetooth_setup

_MODULE_JS = Path("deploy/assets/bluetooth/js/main.js")
_SCAN_JS = Path("deploy/assets/bluetooth/js/scan.js")
_SCAN_HARNESS = Path("tests/js/bluetooth_scan_test.mjs")
_NODE = shutil.which("node")


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


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_bluetooth_browser_modules_handle_scan_and_device_action_states():
    proc = subprocess.run(
        [_NODE, str(_SCAN_HARNESS), str(_SCAN_JS), str(_MODULE_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"Bluetooth scan harness errored:\n{proc.stderr}"
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {"ok": True}


def test_device_actions_use_data_attributes_not_inline_js():
    """Device rows are rendered client-side in the ES module; untrusted
    device metadata rides in escaped data-* attributes consumed by a single
    delegated click handler, never generated inline onclick."""
    js = _MODULE_JS.read_text()
    assert 'onclick="connectDevice' not in js
    assert 'onclick="startPair' not in js
    # The server HTML carries no inline onclick either.
    html = bluetooth_setup._landing_html().decode("utf-8")
    assert "onclick=" not in html
