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


def test_module_uses_csrf_aware_json_writes():
    """CSRF-aware JSON writes moved with the JS into the ES module: every
    mutating POST goes through jsonHeaders() (which attaches X-CSRF-Token),
    never a raw inline Content-Type header."""
    js = _MODULE_JS.read_text()
    assert "headers: jsonHeaders()" in js
    # jsonHeaders comes from the shared http.js module, not re-declared here.
    assert '"/assets/shared/js/http.js"' in js
    assert '"Content-Type": "application/json"' not in js


def test_scan_toggle_uses_shared_post_json_and_alert_recovery():
    js = _MODULE_JS.read_text()
    assert 'postJSON("scan", {action})' not in js
    assert "postJSON('scan', {action})" in js
    assert 'from "./scan.js"' in js


def test_power_switch_renders_and_writes_persisted_desired_state():
    """The source switch represents user intent, even while the adapter is
    temporarily unavailable; the physical adapter state must not flip it back."""
    js = _MODULE_JS.read_text()
    assert "power.checked = !!state.desired;" in js
    assert "const previous = !!state.desired;" in js
    assert "power.checked = !!state.powered;" not in js
    assert "body: JSON.stringify({on: target})" in js


def test_power_confirmation_cannot_leak_unknown_intent_on_mutation_race():
    """Confirmation yields; mutation ownership must precede unknown state."""
    js = _MODULE_JS.read_text()
    start = js.index("async function togglePower")
    acquire = js.index("if (!beginMutation()) {", start)
    restore = js.index("restoreToggle();", acquire)
    unknown = js.index("powerIntentUnknown = true;", acquire)

    assert acquire < restore < unknown


def test_mutation_gate_stays_closed_until_preexisting_state_get_drains():
    js = _MODULE_JS.read_text()
    start = js.index("async function finishMutation()")
    end = js.index("// HID profile fragments", start)
    function = js[start:end]

    assert function.index("await stateFetchPromise;") < function.index(
        "await fetchState(true);"
    ) < function.index("mutationInFlight = false;") < function.index(
        "renderToggles();"
    )


def test_pairing_mode_and_scan_controls_remain_gated_by_adapter_power():
    js = _MODULE_JS.read_text()
    assert "sd.disabled = mutationInFlight || parked || (!state.discoverable" in js
    assert "&& (unavailable || !state.desired || !state.powered));" in js
    assert "btn.disabled = mutationInFlight || parked || (!scanning" in js


def test_unavailable_state_allows_pairing_mode_off_and_scan_stop_only():
    js = _MODULE_JS.read_text()
    assert "if (target && (" in js
    assert "state.available === false || state.parked || !state.desired" in js
    assert "const scanning = !parked && (state.discovering || intent);" in js
    assert "discovering: !!state.discovering || Date.now() < scanIntentUntil" in js


def test_unavailable_and_parked_state_gate_activation_without_trapping_off():
    js = _MODULE_JS.read_text()
    assert "if (!r.ok)" in js
    assert "effective: 'unavailable'" in js
    assert "power.disabled = mutationInFlight || powerIntentUnknown || parked" in js
    assert "|| (unavailable && !state.desired);" in js
    assert "Managed by this speaker’s stereo pair." in js
    assert "state.unavailableReason || state.error" in js
    assert "Bluetooth state unavailable." in js


def test_unavailable_state_keeps_disconnect_and_forget_available():
    js = _MODULE_JS.read_text()
    assert "const mutationDisabled = mutationInFlight ? ' disabled' : '';" in js
    assert "const radioActionDisabled = (" in js
    assert 'data-action="disconnect"' in js
    assert 'data-action="forget"' in js
    assert "${mutationDisabled}>Disconnect</button>" in js
    assert "${mutationDisabled}>Forget</button>" in js
    assert "${radioActionDisabled}>Connect</button>" in js
    assert "${radioActionDisabled}>Pair</button>" in js


def test_failed_power_apply_uses_authoritative_state_readback():
    js = _MODULE_JS.read_text()
    assert "data.state && typeof data.state === 'object'" in js
    assert "state = data.state;" in js
    state = js.index("state = data.state;")
    known = js.index("powerIntentUnknown = false;", state)
    render = js.index("renderToggles();", known)
    assert state < known < render
    assert "await jtsAlert('Bluetooth toggle failed:" in js


def test_device_transport_failures_are_visible_before_mutation_finishes():
    js = _MODULE_JS.read_text()
    connect_start = js.index("async function connectDevice")
    forget_start = js.index("async function forget", connect_start)
    click_start = js.index("document.addEventListener", forget_start)
    connect = js[connect_start:forget_start]
    forget = js[forget_start:click_start]

    assert "} catch (error) {" in connect
    assert "'Connect' : 'Disconnect'} failed:" in connect
    assert "} catch (error) {" in forget
    assert "await jtsAlert('Forget failed:" in forget


def test_desired_on_adapter_degraded_message_is_actionable():
    js = _MODULE_JS.read_text()
    assert "if (state.effective === 'degraded')" in js
    assert "state.desired" in js
    assert "Set to on, but the Bluetooth radio is not ready." in js


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
def test_scan_toggle_browser_module_handles_success_and_failures():
    proc = subprocess.run(
        [_NODE, str(_SCAN_HARNESS), str(_SCAN_JS)],
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
    assert 'data-action="connect"' in js
    assert 'data-action="forget"' in js
    assert 'data-action="pair"' in js
    assert 'onclick="connectDevice' not in js
    assert 'onclick="startPair' not in js
    # The server HTML carries no inline onclick either.
    html = bluetooth_setup._landing_html().decode("utf-8")
    assert "onclick=" not in html


def test_empty_device_lists_render_before_the_first_stream_event():
    """Zero paired devices must not leave the page stuck on Loading forever."""
    js = _MODULE_JS.read_text()
    assert "renderDevices();\nstartDeviceStream();" in js
    assert "No paired devices yet." in js


def test_connected_unpaired_ble_devices_do_not_render_as_ready():
    """A BLE accessory can be radio-linked before it is paired. The UI should
    not describe that state as plain Connected, because JTS cannot use the
    accessory profile until BlueZ has a pair record."""
    js = _MODULE_JS.read_text()
    assert "(d.paired ? paired : other).push(d)" in js
    assert "function deviceRow(d)" in js
    assert "const isPaired = !!d.paired" in js
    assert "const canRemoveUnpaired = !isPaired" in js
    assert "deviceRow(d, true)" not in js
    assert "deviceRow(d, false)" not in js
    assert "(d.connected || d.trusted) && !d.paired" in js
    assert "Pair required" in js
    assert "badge linked" in js
    assert ">Remove</button>" in js
    assert 'data-action="forget"' in js


def test_bluetooth_module_has_no_code_entry_flow():
    js = _MODULE_JS.read_text()
    assert "confirm_passkey" not in js
    assert "request_passkey" not in js
    assert "request_pincode" not in js
    assert "data-pair-action" not in js
