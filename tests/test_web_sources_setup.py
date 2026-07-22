# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free tests for the /sources/ wizard (sources_setup.py).

Renders the page and drives the /state + /set handlers in-process; mocks
all systemctl / DBus / boot-config reads. No network, no hardware.

The page was migrated to the canonical design system (canonical_page +
toggle_html + an ES module). These tests pin both the canonical-look
markers and the unchanged behaviour: the four per-source toggles, the
/state snapshot shape, the /set CSRF gate + dispatch + read-back, the
USB hardware-capability guard, and the Bluetooth DBus / HID-warning path.
"""
from __future__ import annotations

import io
import json
from email.message import Message
from http import HTTPStatus
from pathlib import Path

import pytest

from jasper.local_sources import local_source_lifecycle
from jasper.music_sources import Source
from jasper.web import _common
from jasper.web import sources_setup as mod

CSRF = "x" * 43

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_MODULE = REPO_ROOT / "deploy" / "assets" / "sources" / "js" / "main.js"
AIRPLAY_UNIT = local_source_lifecycle(Source.AIRPLAY).intent_unit
SPOTIFY_CONNECT_UNIT = local_source_lifecycle(Source.SPOTIFY).intent_unit
assert AIRPLAY_UNIT is not None
assert SPOTIFY_CONNECT_UNIT is not None


# ---- render -----------------------------------------------------------------


def test_renders_through_canonical_page():
    html = mod._index_html(csrf_token=CSRF, status_msg="Saved.").decode("utf-8")
    # Canonical document shell: doctype + cache-busted app.css + CSRF meta.
    assert html.startswith("<!doctype html>")
    assert "/assets/app.css" in html
    assert '<meta name="jts-csrf"' in html
    # The sticky app header (canonical_header) is present.
    assert 'class="app-header"' in html
    assert "Music sources" in html


def test_no_legacy_switch_markup():
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    # toggle_html (canonical) is used; the legacy clickable switch is gone.
    assert 'class="switch"' not in html
    assert 'class="slider"' not in html
    assert 'class="toggle"' in html


def test_behaviour_ships_as_es_module():
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    assert '<script type="module" src="/assets/sources/js/main.js">' in html
    # No inline behaviour script survived the migration.
    assert "csrf_fetch_helpers_js" not in html
    assert "addEventListener" not in html
    assert "setInterval" not in html


def test_every_source_row_rendered():
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    for label in ("AirPlay", "Bluetooth", "Spotify Connect", "USB Audio Input"):
        assert label in html
    # Each source's toggle keeps its stable t-<key> id (the ES module binds
    # to these). All four start disabled (hydrated by the /state poll).
    for key in ("airplay", "bluetooth", "spotify_connect", "usbsink"):
        marker = f'id="t-{key}"'
        assert marker in html
        seg = html[html.index(marker):html.index(marker) + 120]
        assert "disabled" in seg
    assert "new computer audio takes over" in html
    assert "pin another source to prevent automatic switching" in html


def test_first_paint_toggles_are_disabled_and_unchecked():
    # _index_html renders all toggles disabled at first paint; the checked
    # state is hydrated client-side from /state. So here we only assert the
    # first-paint contract (no checked); checked-state is covered by the
    # _gather_state tests + the ES module.
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    assert "checked" not in html


def test_usb_unavailable_note_present_but_hidden_at_render():
    # The hardware note exists in the markup (the /state poll un-hides it
    # when available=false); it is not server-gated.
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    assert 'id="usbsink-unavailable-note"' in html
    assert "current hardware configuration" in html
    assert 'id="usbsink-unavailable-note" style="display:none"' in html


def test_profile_unavailable_notes_present_but_hidden_at_render():
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    assert 'id="airplay-unavailable-note"' in html
    assert 'id="spotify_connect-unavailable-note"' in html
    assert "not installed on this speaker" in html


def test_initial_state_error_surface_is_present_and_controls_start_disabled():
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    assert 'id="sources-state-error"' in html
    assert "Controls are paused" in html


def test_status_banner_severity_and_escaping():
    ok = mod._index_html(csrf_token=CSRF, status_msg="Saved.").decode("utf-8")
    assert "banner--ok" in ok
    xss = mod._index_html(
        csrf_token=CSRF, status_msg="<script>alert(1)</script>"
    ).decode("utf-8")
    assert "<script>alert(1)</script>" not in xss
    assert "&lt;script&gt;" in xss


# ---- _gather_state ----------------------------------------------------------


@pytest.fixture
def stub_backends(monkeypatch):
    """Stub intent and hardware probes so state reads are deterministic."""

    def _stub(
        *,
        active=(),
        available_units=None,
        usb_ready=True,
        usb_card=False,
        bt=(True, False),
        bt_adapter=True,
        intents=None,
        parked=False,
    ):
        active_set = set(active)
        if AIRPLAY_UNIT in active_set:
            active_set.update(
                local_source_lifecycle(Source.AIRPLAY).health_units
            )
        if available_units is None:
            available_units = {
                *local_source_lifecycle(Source.AIRPLAY).health_units,
                *mod.BLUETOOTH_RUNTIME_UNITS,
                SPOTIFY_CONNECT_UNIT,
                mod.USBSINK_UNIT,
                # The composite gadget owner replaced the old init unit; its
                # availability is what /sources checks for "the USB stack is
                # installed". Host-visible AUDIO presence is a separate signal
                # (the uac2 ALSA card) stubbed via usb_card below.
                mod.USBSINK_GADGET_UNIT,
            }
        available_set = set(available_units)
        if intents is None:
            intents = {
                Source.AIRPLAY: True,
                Source.BLUETOOTH: True,
                Source.SPOTIFY: True,
                Source.USBSINK: False,
            }
        monkeypatch.setattr(mod, "read_source_intents", lambda: dict(intents))
        monkeypatch.setattr(mod, "bonded_follower_active", lambda: parked)
        monkeypatch.setattr(mod, "_local_sources_allowed", lambda: True)
        monkeypatch.setattr(
            mod, "_unit_available", lambda unit: unit in available_set,
        )
        monkeypatch.setattr(mod, "_unit_active", lambda unit: unit in active_set)
        class _Snapshot:
            @staticmethod
            def available(unit):
                return unit in available_set

            @staticmethod
            def active(unit):
                return unit in active_set

            @staticmethod
            def activating(_unit):
                return False

        monkeypatch.setattr(mod, "probe_unit_snapshot", lambda _units: _Snapshot())
        monkeypatch.setattr(
            mod,
            "_usbsink_capability",
            lambda *a, **k: (
                usb_ready,
                "" if usb_ready else "USB output DAC uses the shared port",
            ),
        )
        # The uac2 ALSA card is the host-visible "USB audio device advertised"
        # signal now that the composite gadget can outlive audio (it also carries the
        # USB management network), so gadget-active is no longer that proxy.
        monkeypatch.setattr(mod, "_uac2_card_present", lambda: usb_card)
        monkeypatch.setattr(
            mod,
            "_bluetooth_availability",
            lambda _snapshot=None: mod.BluetoothAvailability(
                available=(
                    bt_adapter
                    and all(unit in available_set for unit in mod.BLUETOOTH_RUNTIME_UNITS)
                ),
                radio_present=bt_adapter,
                any_soft_blocked=not intents[Source.BLUETOOTH],
                all_soft_blocked=not intents[Source.BLUETOOTH],
                hard_blocked=False,
            ),
        )
        monkeypatch.setattr(mod, "read_fanin_status", lambda: {})
        monkeypatch.setattr(
            mod.os.path,
            "isdir",
            lambda path: path == mod.BLUETOOTH_ADAPTER_PATH and bt_adapter,
        )

        async def _bt():
            return bt

        monkeypatch.setattr(mod, "_bt_state", _bt)

    return _stub


def test_gather_state_shape(stub_backends):
    stub_backends(
        active={
            AIRPLAY_UNIT,
            SPOTIFY_CONNECT_UNIT,
            *mod.BLUETOOTH_RUNTIME_UNITS,
        },
        usb_ready=False,
        bt=(True, True),
    )
    state = mod._gather_state()
    assert state["airplay"] == {
        "enabled": True,
        "desired": True,
        "effective": "on",
        "available": True,
    }
    assert state["spotify_connect"] == {
        "enabled": True,
        "desired": True,
        "effective": "on",
        "available": True,
    }
    assert state["bluetooth"] == {
        "enabled": True,
        "desired": True,
        "effective": "on",
        "available": True,
        "hasPairedHid": True,
    }
    # USB unavailable because the output DAC owns the shared data port.
    assert state["usbsink"]["enabled"] is False
    assert state["usbsink"]["desired"] is False
    assert state["usbsink"]["effective"] == "off"
    assert state["usbsink"]["available"] is False
    assert "output DAC" in str(state["usbsink"]["unavailableReason"])


def test_source_state_keeps_availability_independent_from_effective_off():
    state = mod._source_state(
        desired=False,
        observed=False,
        available=False,
        unavailable_reason="hardware cannot provide this source",
    )

    assert state == {
        "enabled": False,
        "desired": False,
        "effective": "off",
        "available": False,
        "unavailableReason": "hardware cannot provide this source",
    }


def test_source_state_reports_off_drift_even_when_source_is_unavailable():
    state = mod._source_state(
        desired=False,
        observed=True,
        available=False,
        unavailable_reason="hardware cannot provide this source",
    )

    assert state["effective"] == "degraded"
    assert state["available"] is False
    assert state["unavailableReason"] == "hardware cannot provide this source"
    assert "current runtime state does not match" in str(state["degradedReason"])


def test_gather_state_renderer_units_unavailable(stub_backends):
    stub_backends(available_units=set(), usb_ready=True)
    state = mod._gather_state()

    # Availability never rewrites the user's durable choice.
    assert state["airplay"]["enabled"] is True
    assert state["airplay"]["desired"] is True
    assert state["airplay"]["effective"] == "unavailable"
    assert state["airplay"]["available"] is False
    assert "not installed on this speaker" in str(
        state["airplay"]["unavailableReason"]
    )
    assert state["spotify_connect"]["enabled"] is True
    assert state["spotify_connect"]["effective"] == "unavailable"
    assert state["spotify_connect"]["available"] is False
    assert state["usbsink"]["enabled"] is False
    assert state["usbsink"]["available"] is False
    unavailable = " ".join(
        str(item.get("unavailableReason") or "")
        for item in state.values()
    )
    assert "install.sh" in unavailable


def test_gather_state_endpoint_profile_disables_stale_renderer_units(
    stub_backends, monkeypatch,
):
    stub_backends(
        active={AIRPLAY_UNIT, SPOTIFY_CONNECT_UNIT, mod.USBSINK_UNIT},
        bt=(True, False),
        usb_ready=True,
    )
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: False)

    state = mod._gather_state()

    assert state["airplay"]["enabled"] is True
    assert state["airplay"]["desired"] is True
    assert state["airplay"]["effective"] == "unavailable"
    assert state["airplay"]["available"] is False
    assert state["spotify_connect"]["enabled"] is True
    assert state["spotify_connect"]["available"] is False
    assert state["bluetooth"]["enabled"] is True
    assert state["bluetooth"]["available"] is False
    assert "not installed on this speaker" in str(
        state["bluetooth"]["unavailableReason"]
    )
    assert state["usbsink"]["enabled"] is False
    assert state["usbsink"]["available"] is False


def test_gather_state_bluetooth_unavailable(stub_backends):
    stub_backends(bt=(False, False), bt_adapter=False)
    state = mod._gather_state()["bluetooth"]
    assert state["enabled"] is True
    assert state["desired"] is True
    assert state["effective"] == "unavailable"
    assert state["available"] is False
    assert state["hasPairedHid"] is False
    assert "Bluetooth adapter" in str(state["unavailableReason"])


@pytest.mark.parametrize(
    "availability,expected",
    [
        (
            mod.BluetoothAvailability(
                available=False,
                radio_present=True,
                any_soft_blocked=False,
                all_soft_blocked=False,
                hard_blocked=True,
            ),
            "hardware radio switch",
        ),
        (
            mod.BluetoothAvailability(
                available=False,
                radio_present=True,
                any_soft_blocked=False,
                all_soft_blocked=False,
                hard_blocked=False,
                missing_units=("bt-agent.service",),
            ),
            "bt-agent.service",
        ),
    ],
)
def test_sources_reuses_specific_bluetooth_unavailable_reason(
    stub_backends,
    monkeypatch,
    availability,
    expected,
):
    stub_backends()
    monkeypatch.setattr(
        mod, "_bluetooth_availability", lambda _snapshot=None: availability,
    )

    state = mod._gather_state()["bluetooth"]

    assert state["available"] is False
    assert expected in str(state["unavailableReason"])


def test_gather_state_keeps_enabled_intent_when_runtime_is_degraded(
    stub_backends,
):
    stub_backends(active=set(), bt=(False, False))

    state = mod._gather_state()

    for key in ("airplay", "bluetooth", "spotify_connect"):
        assert state[key]["enabled"] is True
        assert state[key]["desired"] is True
        assert state[key]["effective"] == "degraded"
        assert state[key]["available"] is True
        assert "degradedReason" in state[key]


def test_gather_state_bluetooth_off_remains_available(stub_backends):
    stub_backends(
        bt=(False, False),
        intents={
            Source.AIRPLAY: True,
            Source.BLUETOOTH: False,
            Source.SPOTIFY: True,
            Source.USBSINK: False,
        },
    )

    state = mod._gather_state()["bluetooth"]

    assert state == {
        "enabled": False,
        "desired": False,
        "effective": "off",
        "available": True,
        "hasPairedHid": False,
    }


# ---- _apply routing ---------------------------------------------------------


@pytest.mark.parametrize(
    ("wizard_key", "source"),
    [
        ("airplay", Source.AIRPLAY),
        ("bluetooth", Source.BLUETOOTH),
        ("spotify_connect", Source.SPOTIFY),
        ("usbsink", Source.USBSINK),
    ],
)
@pytest.mark.parametrize("enabled", [True, False])
def test_apply_routes_each_source_through_shared_coordinator(
    monkeypatch, wizard_key, source, enabled,
):
    events = []

    def local_sources_allowed():
        events.append(("validate-role",))
        return True

    def unit_available(unit):
        events.append(("validate-unit", unit))
        return True

    def usbsink_capability():
        events.append(("validate-usb-hardware",))
        return True, ""

    def bluetooth_present():
        events.append(("validate-bluetooth-hardware",))
        return mod.BluetoothAvailability(
            available=True,
            radio_present=True,
            any_soft_blocked=False,
            all_soft_blocked=False,
            hard_blocked=False,
        )

    monkeypatch.setattr(mod, "_local_sources_allowed", local_sources_allowed)
    monkeypatch.setattr(mod, "_unit_available", unit_available)
    monkeypatch.setattr(mod, "_usbsink_capability", usbsink_capability)
    monkeypatch.setattr(mod, "_bluetooth_availability", bluetooth_present)
    monkeypatch.setattr(
        mod,
        "request_source_intent",
        lambda target, desired: events.append(("request", target, desired)),
    )

    mod._apply(wizard_key, enabled)

    requests = [event for event in events if event[0] == "request"]
    assert requests == [("request", source, enabled)]
    if enabled:
        assert events[0][0].startswith("validate-")
    else:
        assert events == requests
    assert events[-1] == requests[0]


def test_apply_refuses_unavailable_renderer(monkeypatch):
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(mod, "_unit_available", lambda unit: False)
    monkeypatch.setattr(
        mod,
        "request_source_intent",
        lambda *a: pytest.fail("must not request intent"),
    )

    with pytest.raises(RuntimeError, match="not installed on this speaker"):
        mod._apply("airplay", True)


@pytest.mark.parametrize(
    ("wizard_key", "source"),
    [
        ("airplay", Source.AIRPLAY),
        ("spotify_connect", Source.SPOTIFY),
        ("bluetooth", Source.BLUETOOTH),
    ],
)
def test_apply_off_persists_when_source_hardware_or_units_are_missing(
    monkeypatch, wizard_key, source,
):
    calls = []
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(mod, "_unit_available", lambda _unit: False)
    monkeypatch.setattr(
        mod,
        "_bluetooth_availability",
        lambda: mod.BluetoothAvailability(
            available=False,
            radio_present=False,
            any_soft_blocked=None,
            all_soft_blocked=None,
            hard_blocked=None,
            error="missing",
        ),
    )
    monkeypatch.setattr(
        mod,
        "request_source_intent",
        lambda target, desired: calls.append((target, desired)),
    )

    mod._apply(wizard_key, False)

    assert calls == [(source, False)]


@pytest.mark.parametrize(
    ("wizard_key", "source"),
    [
        ("airplay", Source.AIRPLAY),
        ("spotify_connect", Source.SPOTIFY),
        ("bluetooth", Source.BLUETOOTH),
        ("usbsink", Source.USBSINK),
    ],
)
def test_apply_off_persists_on_profile_without_local_sources(
    monkeypatch, wizard_key, source,
):
    calls = []
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: False)
    monkeypatch.setattr(
        mod,
        "request_source_intent",
        lambda target, desired: calls.append((target, desired)),
    )

    mod._apply(wizard_key, False)

    assert calls == [(source, False)]


def test_apply_refuses_usbsink_without_hardware_capability(monkeypatch):
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(mod, "_unit_available", lambda unit: True)
    monkeypatch.setattr(
        mod,
        "_usbsink_capability",
        lambda: (False, "USB output DAC uses the shared port"),
    )
    monkeypatch.setattr(
        mod,
        "request_source_intent",
        lambda *a: pytest.fail("must not request intent"),
    )

    with pytest.raises(RuntimeError, match="USB output DAC"):
        mod._apply("usbsink", True)


def test_apply_refuses_renderer_when_local_sources_disallowed(monkeypatch):
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: False)
    monkeypatch.setattr(mod, "_unit_available", lambda unit: True)
    monkeypatch.setattr(
        mod,
        "request_source_intent",
        lambda *a: pytest.fail("must not request intent"),
    )

    with pytest.raises(RuntimeError, match="not installed on this speaker"):
        mod._apply("spotify_connect", True)


def test_apply_refuses_bluetooth_when_local_sources_disallowed(monkeypatch):
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: False)
    monkeypatch.setattr(
        mod,
        "request_source_intent",
        lambda *a: pytest.fail("must not request intent"),
    )

    with pytest.raises(RuntimeError, match="not installed on this speaker"):
        mod._apply("bluetooth", True)


@pytest.mark.parametrize(
    "availability,expected",
    [
        (
            mod.BluetoothAvailability(
                available=False,
                radio_present=False,
                any_soft_blocked=None,
                all_soft_blocked=None,
                hard_blocked=None,
            ),
            "Bluetooth adapter",
        ),
        (
            mod.BluetoothAvailability(
                available=False,
                radio_present=True,
                any_soft_blocked=False,
                all_soft_blocked=False,
                hard_blocked=True,
            ),
            "hardware radio switch",
        ),
        (
            mod.BluetoothAvailability(
                available=False,
                radio_present=True,
                any_soft_blocked=False,
                all_soft_blocked=False,
                hard_blocked=False,
                missing_units=("bt-agent.service",),
            ),
            "bt-agent.service",
        ),
    ],
)
def test_apply_bluetooth_reuses_specific_availability_reason(
    monkeypatch, availability, expected,
):
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(mod, "_bluetooth_availability", lambda: availability)
    monkeypatch.setattr(
        mod,
        "request_source_intent",
        lambda *a: pytest.fail("must not request intent"),
    )

    with pytest.raises(RuntimeError, match=expected):
        mod._apply("bluetooth", True)


# ---- handler routing + CSRF -------------------------------------------------
#
# The Handler's do_GET/do_POST call its OWN _send_json / _read_json methods
# (defined inside _make_handler), so a detached stand-in object can't drive
# them — we need a real Handler instance. We build one with object.__new__ to
# skip BaseHTTPRequestHandler.__init__ (which would parse a socket), then graft
# the request I/O on and override the response sinks so the base class's
# log_request / requestline machinery never fires.


def _make_inst(path: str, body: bytes = b"", cookies: str = "",
               csrf_header: str | None = None):
    handler_cls = mod._make_handler()
    inst = handler_cls.__new__(handler_cls)
    inst.path = path
    headers = Message()
    headers["Content-Length"] = str(len(body))
    headers["Content-Type"] = "application/json"
    if cookies:
        headers["Cookie"] = cookies
    if csrf_header:
        headers["X-CSRF-Token"] = csrf_header
    inst.headers = headers
    inst.rfile = io.BytesIO(body)
    inst.wfile = io.BytesIO()
    inst.client_address = ("127.0.0.1", 0)
    inst.status = None
    inst.sent_headers = []

    # Override the response sinks on the instance so we capture status/headers
    # and never invoke BaseHTTPRequestHandler.log_request (needs raw_requestline).
    def send_response(status, *a, **k):
        inst.status = int(status)

    def send_header(name, value):
        inst.sent_headers.append((name, value))

    def send_error(status, *a, **k):
        inst.status = int(status)

    inst.send_response = send_response
    inst.send_response_only = send_response
    inst.send_header = send_header
    inst.end_headers = lambda: None
    inst.send_error = send_error
    inst.log_message = lambda *a, **k: None
    inst.address_string = lambda: "127.0.0.1"
    return inst


def _drive(method: str, path: str, *, body=b"", csrf_cookie=None, csrf_header=None):
    cookies = f"{_common.CSRF_COOKIE_NAME}={csrf_cookie}" if csrf_cookie else ""
    inst = _make_inst(path, body=body, cookies=cookies, csrf_header=csrf_header)
    getattr(inst, f"do_{method}")()
    return inst


def _body_json(inst) -> dict:
    return json.loads(inst.wfile.getvalue().decode("utf-8"))


class _TrackingReader(io.BytesIO):
    def __init__(self, body: bytes, *, fail: bool = False) -> None:
        super().__init__(body)
        self.fail = fail
        self.read_calls: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_calls.append(size)
        if self.fail:
            raise OSError("request body read failed")
        return super().read(size)


def test_get_state_returns_snapshot(stub_backends, monkeypatch):
    stub_backends(active={AIRPLAY_UNIT}, usb_ready=True, bt=(True, False))
    delegated_probe = mod.probe_unit_snapshot
    calls: list[tuple[str, ...]] = []

    def counted_probe(units):
        calls.append(tuple(units))
        return delegated_probe(units)

    monkeypatch.setattr(mod, "probe_unit_snapshot", counted_probe)
    h = _drive("GET", "/state")
    assert h.status == 200
    payload = _body_json(h)
    assert payload["airplay"]["enabled"] is True
    assert payload["usbsink"]["available"] is True
    assert calls == [mod._STATE_UNITS]


def test_get_state_failure_is_explicit_for_initial_hydration(monkeypatch):
    def fail_state():
        raise RuntimeError("invalid source intent")

    monkeypatch.setattr(mod, "_gather_state", fail_state)
    h = _drive("GET", "/state")
    assert h.status == 502
    assert _body_json(h) == {"error": "invalid source intent"}


def test_post_set_without_csrf_is_rejected(stub_backends, monkeypatch):
    stub_backends()
    monkeypatch.setattr(mod, "_apply", lambda *a: pytest.fail("must not apply"))
    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "airplay", "enabled": True}).encode(),
    )
    # reject_csrf sends 403; _apply must never run (asserted via the stub above).
    assert h.status == int(HTTPStatus.FORBIDDEN)


def test_post_set_with_csrf_dispatches_and_reads_back(stub_backends, monkeypatch):
    stub_backends(active=set(), usb_ready=True, bt=(True, False))
    applied = []
    monkeypatch.setattr(
        mod, "_apply", lambda source, enabled: applied.append((source, enabled))
    )
    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "airplay", "enabled": True}).encode(),
        csrf_cookie=CSRF, csrf_header=CSRF,
    )
    assert applied == [("airplay", True)]
    payload = _body_json(h)
    # Read-back returns the full /state snapshot ("pair" is sibling
    # metadata for the parked-toggles state, not a source).
    assert set(payload) == {
        "pair", "airplay", "bluetooth", "spotify_connect", "usbsink",
    }
    assert payload["pair"] == {"parked": False}


def test_post_set_reconcile_failure_returns_durable_readback(monkeypatch):
    durable_state = {
        "pair": {"parked": False},
        "airplay": {
            "enabled": True,
            "desired": True,
            "effective": "degraded",
            "available": True,
            "degradedReason": "AirPlay is still converging.",
        },
    }

    def fail_after_intent_write(_source, _enabled):
        raise RuntimeError("reconcile start failed")

    monkeypatch.setattr(mod, "_apply", fail_after_intent_write)
    monkeypatch.setattr(mod, "_gather_state", lambda: durable_state)

    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "airplay", "enabled": True}).encode(),
        csrf_cookie=CSRF, csrf_header=CSRF,
    )

    assert h.status == 502
    payload = _body_json(h)
    assert payload == {
        "error": "reconcile start failed",
        "state": durable_state,
    }
    assert payload["state"]["airplay"]["desired"] is True
    assert payload["state"]["airplay"]["effective"] == "degraded"


def test_post_set_success_keeps_durable_choice_when_state_readback_fails(
    monkeypatch,
):
    monkeypatch.setattr(mod, "_apply", lambda _source, _enabled: None)
    monkeypatch.setattr(
        mod,
        "_gather_state",
        lambda: (_ for _ in ()).throw(RuntimeError("state read failed")),
    )

    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "airplay", "enabled": False}).encode(),
        csrf_cookie=CSRF, csrf_header=CSRF,
    )

    assert h.status == 502
    assert _body_json(h) == {
        "error": "state read failed",
        "desired": False,
        "intentRecorded": True,
    }


def test_post_set_unknown_source_400(stub_backends, monkeypatch):
    stub_backends()
    monkeypatch.setattr(mod, "_apply", lambda *a: pytest.fail("must not apply"))
    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "nope", "enabled": True}).encode(),
        csrf_cookie=CSRF, csrf_header=CSRF,
    )
    assert h.status == 400
    assert "unknown source" in _body_json(h)["error"]


@pytest.mark.parametrize(
    "enabled",
    [None, 0, 1, "", "false", [], {}],
)
def test_post_set_rejects_non_boolean_enabled_without_applying(
    monkeypatch, enabled,
):
    monkeypatch.setattr(mod, "_apply", lambda *_a: pytest.fail("must not apply"))

    h = _drive(
        "POST",
        "/set",
        body=json.dumps({"source": "airplay", "enabled": enabled}).encode(),
        csrf_cookie=CSRF,
        csrf_header=CSRF,
    )

    assert h.status == 400
    assert _body_json(h) == {"error": "enabled must be true or false"}


def test_post_set_rejects_missing_enabled_without_applying(monkeypatch):
    monkeypatch.setattr(mod, "_apply", lambda *_a: pytest.fail("must not apply"))

    h = _drive(
        "POST",
        "/set",
        body=json.dumps({"source": "airplay"}).encode(),
        csrf_cookie=CSRF,
        csrf_header=CSRF,
    )

    assert h.status == 400
    assert _body_json(h) == {"error": "enabled must be true or false"}


@pytest.mark.parametrize(
    ("body", "content_length", "expected_reads"),
    [
        (b"{", 1, [1]),
        (b"\xff", 1, [1]),
        (b"[]", 2, [2]),
        (b"{}", "invalid", []),
        (b"{}", -1, []),
        (b"{}", mod._JSON_BODY_LIMIT + 1, []),
        (b'{"source":"airplay","enabled":true}', 36, [36]),
    ],
)
def test_post_set_rejects_invalid_json_framing_without_applying(
    stub_backends,
    monkeypatch,
    body,
    content_length,
    expected_reads,
):
    stub_backends()
    monkeypatch.setattr(mod, "_apply", lambda *_a: pytest.fail("must not apply"))
    handler = _make_inst(
        "/set",
        body=body,
        cookies=f"{_common.CSRF_COOKIE_NAME}={CSRF}",
        csrf_header=CSRF,
    )
    handler.headers.replace_header("Content-Length", str(content_length))
    handler.rfile = _TrackingReader(body)

    handler.do_POST()

    assert handler.status == 400
    assert _body_json(handler) == {"error": "unknown source ''"}
    assert handler.rfile.read_calls == expected_reads


def test_post_set_request_body_oserror_remains_distinct(
    stub_backends,
    monkeypatch,
):
    stub_backends()
    monkeypatch.setattr(mod, "_apply", lambda *_a: pytest.fail("must not apply"))
    handler = _make_inst(
        "/set",
        body=b"{}",
        cookies=f"{_common.CSRF_COOKIE_NAME}={CSRF}",
        csrf_header=CSRF,
    )
    handler.rfile = _TrackingReader(b"{}", fail=True)

    with pytest.raises(OSError, match="request body read failed"):
        handler.do_POST()

    assert handler.status is None


def test_post_unknown_path_is_404():
    h = _drive("POST", "/bogus", body=b"{}")
    assert h.status == int(HTTPStatus.NOT_FOUND)


def test_get_unknown_path_is_404():
    h = _drive("GET", "/bogus")
    assert h.status == int(HTTPStatus.NOT_FOUND)


# ---- the ES module is wired and clean ---------------------------------------


def test_es_module_exists_and_uses_shared_helpers():
    assert SOURCES_MODULE.exists(), "deploy/assets/sources/js/main.js must exist"
    text = SOURCES_MODULE.read_text(encoding="utf-8")
    assert 'from "/assets/shared/js/http.js"' in text
    assert 'from "/assets/shared/js/dialog.js"' in text
    # Behaviour preserved: optimistic toggle, /state poll, /set POST.
    assert "./state" in text
    assert "./set" in text
    assert "jtsConfirm" in text  # Bluetooth HID guard kept
    assert "showStateError" in text
    assert "s.available === false && !s.enabled" in text
    assert "payload.intentRecorded === true" in text
    assert (
        "postInFlight || parked || (s.available === false && !s.enabled)"
        in text
    )
    refresh_start = text.index("async function refreshAfterMutation()")
    refresh_end = text.index("async function postToggle", refresh_start)
    refresh = text[refresh_start:refresh_end]
    assert refresh.index("await stateFetchPromise;") < refresh.index(
        "postInFlight = false;"
    ) < refresh.index("return fetchState();")


def test_es_module_prioritizes_actionable_degradation_over_unavailability():
    text = SOURCES_MODULE.read_text(encoding="utf-8")

    generic_start = text.index('const note = el(name + "-unavailable-note")')
    generic_end = text.index("const bt = state.bluetooth", generic_start)
    generic = text[generic_start:generic_end]
    assert generic.index("if (degraded)") < generic.index("unavailable &&")

    bluetooth_start = text.index("const bt = state.bluetooth")
    bluetooth_end = text.index("const usb = state.usbsink", bluetooth_start)
    bluetooth = text[bluetooth_start:bluetooth_end]
    assert bluetooth.index("if (btDegraded)") < bluetooth.index("btUnavailable &&")


def test_bluetooth_confirmation_posts_captured_intent_not_polled_dom_state():
    text = SOURCES_MODULE.read_text(encoding="utf-8")
    handler_start = text.index('input.addEventListener("change"')
    handler_end = text.index("setInterval(fetchState", handler_start)
    handler = text[handler_start:handler_end]

    capture = handler.index("const want = !!input.checked;")
    confirm = handler.index("const ok = await jtsConfirm(")
    restore_visual = handler.index("input.checked = want;", confirm)
    post = handler.index("await postToggle(name, want);", restore_visual)
    assert capture < confirm < restore_visual < post
    assert "postToggle(name, input.checked)" not in handler


def test_es_module_has_no_native_dialogs_or_innerhtml():
    # Scan code lines only — the module's header comment legitimately *names*
    # the native popup to explain why it uses jtsConfirm instead (same
    # comment-skipping rule the wizard-conventions test uses).
    code = "\n".join(
        line for line in SOURCES_MODULE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    )
    assert ".innerHTML" not in code
    for native in ("window.confirm", "window.alert", "window.prompt"):
        assert native not in code


def test_set_rejected_while_bonded_follower(stub_backends, monkeypatch):
    """The pair owns source choices while bonded, so a follower cannot
    accumulate hidden desired state that surprises the household on unpair."""
    stub_backends()
    monkeypatch.setattr(mod, "bonded_follower_active", lambda: True)
    monkeypatch.setattr(mod, "_apply", lambda *a: pytest.fail("must not apply"))
    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "airplay", "enabled": True}).encode(),
        csrf_cookie=CSRF, csrf_header=CSRF,
    )
    assert h.status == 409
    assert "stereo pair" in _body_json(h)["error"]


def test_parked_bluetooth_outranks_unavailable_across_sources_surface(
    stub_backends,
):
    stub_backends(parked=True, bt_adapter=False)
    state = mod._gather_state()["bluetooth"]
    assert state["effective"] == "parked"
    assert state["available"] is False


def test_state_reports_parked_pair_without_rewriting_desired(stub_backends):
    intents = {
        Source.AIRPLAY: True,
        Source.BLUETOOTH: False,
        Source.SPOTIFY: True,
        Source.USBSINK: False,
    }
    stub_backends(usb_ready=True, intents=intents, parked=True, bt=(False, False))

    state = mod._gather_state()

    assert state["pair"] == {"parked": True}
    for key, source in (
        ("airplay", Source.AIRPLAY),
        ("bluetooth", Source.BLUETOOTH),
        ("spotify_connect", Source.SPOTIFY),
        ("usbsink", Source.USBSINK),
    ):
        assert state[key]["enabled"] is intents[source]
        assert state[key]["desired"] is intents[source]
        assert state[key]["effective"] == "parked"
