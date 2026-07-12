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
USB-gadget dtoverlay guard, and the Bluetooth DBus / HID-warning path.
"""
from __future__ import annotations

import io
import json
from email.message import Message
from http import HTTPStatus
from pathlib import Path

import pytest

from jasper.web import _common
from jasper.web import sources_setup as mod

CSRF = "x" * 43

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_MODULE = REPO_ROOT / "deploy" / "assets" / "sources" / "js" / "main.js"


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


def test_first_paint_toggles_are_disabled_and_unchecked():
    # _index_html renders all toggles disabled at first paint; the checked
    # state is hydrated client-side from /state. So here we only assert the
    # first-paint contract (no checked); checked-state is covered by the
    # _gather_state tests + the ES module.
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    assert "checked" not in html


def test_usb_unavailable_note_present_but_hidden_at_render():
    # The dtoverlay note exists in the markup (the /state poll un-hides it
    # when available=false); it is not server-gated.
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    assert 'id="usbsink-unavailable-note"' in html
    assert "re-run install.sh and reboot" in html
    assert "/boot/firmware/config.txt" in html


def test_profile_unavailable_notes_present_but_hidden_at_render():
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    assert 'id="airplay-unavailable-note"' in html
    assert 'id="spotify_connect-unavailable-note"' in html
    assert "not installed on this speaker" in html


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
    """Stub the systemctl + DBus probes so _gather_state / _apply run pure."""
    def _stub(
        *,
        active=(),
        available_units=None,
        usb_ready=True,
        usb_card=False,
        bt=(True, True, False),
    ):
        active_set = set(active)
        if available_units is None:
            available_units = {
                mod.AIRPLAY_UNIT,
                mod.SPOTIFY_CONNECT_UNIT,
                mod.USBSINK_UNIT,
                # The composite gadget owner replaced the old init unit; its
                # availability is what /sources checks for "the USB stack is
                # installed". Host-visible AUDIO presence is a separate signal
                # (the uac2 ALSA card) stubbed via usb_card below.
                mod.USBSINK_GADGET_UNIT,
            }
        available_set = set(available_units)
        monkeypatch.setattr(mod, "_local_sources_allowed", lambda: True)
        monkeypatch.setattr(
            mod, "_unit_available", lambda unit: unit in available_set,
        )
        monkeypatch.setattr(mod, "_unit_active", lambda unit: unit in active_set)
        monkeypatch.setattr(mod, "_usbsink_available", lambda *a, **k: usb_ready)
        # The uac2 ALSA card is the host-visible "USB audio device advertised"
        # signal now that the composite gadget is always-on (it also carries the
        # USB management network), so gadget-active is no longer that proxy.
        monkeypatch.setattr(mod, "_uac2_card_present", lambda: usb_card)

        async def _bt():
            return bt

        monkeypatch.setattr(mod, "_bt_state", _bt)

    return _stub


def test_gather_state_shape(stub_backends):
    stub_backends(
        active={mod.AIRPLAY_UNIT, mod.SPOTIFY_CONNECT_UNIT},
        usb_ready=False,
        bt=(True, True, True),
    )
    state = mod._gather_state()
    assert state["airplay"] == {"enabled": True, "available": True}
    assert state["spotify_connect"] == {"enabled": True, "available": True}
    assert state["bluetooth"] == {
        "enabled": True, "available": True, "hasPairedHid": True,
    }
    # USB unavailable because the dtoverlay is absent.
    assert state["usbsink"]["enabled"] is False
    assert state["usbsink"]["available"] is False
    assert "config.txt" in str(state["usbsink"]["unavailableReason"])


def test_gather_state_renderer_units_unavailable(stub_backends):
    stub_backends(available_units=set(), usb_ready=True)
    state = mod._gather_state()

    assert state["airplay"]["enabled"] is False
    assert state["airplay"]["available"] is False
    assert "not installed on this speaker" in str(
        state["airplay"]["unavailableReason"]
    )
    assert state["spotify_connect"]["enabled"] is False
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
        active={mod.AIRPLAY_UNIT, mod.SPOTIFY_CONNECT_UNIT, mod.USBSINK_UNIT},
        bt=(True, True, False),
        usb_ready=True,
    )
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: False)

    state = mod._gather_state()

    assert state["airplay"]["enabled"] is False
    assert state["airplay"]["available"] is False
    assert state["spotify_connect"]["enabled"] is False
    assert state["spotify_connect"]["available"] is False
    assert state["bluetooth"]["enabled"] is False
    assert state["bluetooth"]["available"] is False
    assert "not installed on this speaker" in str(
        state["bluetooth"]["unavailableReason"]
    )
    assert state["usbsink"]["enabled"] is False
    assert state["usbsink"]["available"] is False


def test_gather_state_bluetooth_unavailable(stub_backends):
    stub_backends(bt=(False, False, False))
    state = mod._gather_state()["bluetooth"]
    assert state["enabled"] is False
    assert state["available"] is False
    assert state["hasPairedHid"] is False
    assert "Bluetooth adapter" in str(state["unavailableReason"])


# ---- _apply routing ---------------------------------------------------------


def test_apply_routes_each_source(monkeypatch):
    units = []
    persisted = []
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(mod, "_unit_available", lambda unit: True)
    monkeypatch.setattr(mod, "_usbsink_available", lambda: True)
    monkeypatch.setattr(
        mod, "_set_unit", lambda unit, enabled: units.append((unit, enabled))
    )
    # USB enable persists its enable INTENT through the root source-intent helper
    # (enable is manage-unit-files, which the non-root broker can't run), then
    # recomposes the always-on composite gadget + starts the bridge via the
    # restart broker (manage_units) — capture each separately.
    monkeypatch.setattr(
        mod, "_persist_source_intent",
        lambda unit, enabled: persisted.append((unit, enabled)),
    )
    managed = []
    monkeypatch.setattr(
        mod, "manage_units",
        lambda *u, **kw: managed.append((u, kw.get("verb"))) or {"ok": True},
    )
    bt_calls = []

    async def _set_bt(enabled):
        bt_calls.append(enabled)

    monkeypatch.setattr(mod, "_set_bt", _set_bt)

    mod._apply("airplay", True)
    mod._apply("spotify_connect", False)
    mod._apply("usbsink", True)
    mod._apply("bluetooth", False)

    assert (mod.AIRPLAY_UNIT, True) in units
    assert (mod.SPOTIFY_CONNECT_UNIT, False) in units
    # USB enable is a four-step ordering: persist the enable intent (root helper),
    # recompose the gadget so the uac2 card appears, start the bridge, then kick
    # the coupling reconcile to arm the combo. Only the last three are broker
    # (manage_units) calls; the persist is the source-intent helper.
    assert persisted == [(mod.USBSINK_UNIT, True)]
    assert managed == [
        ((mod.USBSINK_GADGET_UNIT,), "restart"),
        ((mod.USBSINK_UNIT,), "start"),
        ((mod.COUPLING_AUTO_UNIT,), "start"),
    ]
    assert bt_calls == [False]


def test_apply_refuses_unavailable_renderer(monkeypatch):
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(mod, "_unit_available", lambda unit: False)
    monkeypatch.setattr(
        mod, "_set_unit", lambda *a: pytest.fail("must not call systemctl"),
    )

    with pytest.raises(RuntimeError, match="not installed on this speaker"):
        mod._apply("airplay", True)


def test_apply_refuses_usbsink_without_dtoverlay(monkeypatch):
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: True)
    monkeypatch.setattr(mod, "_unit_available", lambda unit: True)
    monkeypatch.setattr(mod, "_usbsink_available", lambda: False)
    monkeypatch.setattr(
        mod, "_set_unit", lambda *a: pytest.fail("must not call systemctl"),
    )

    with pytest.raises(RuntimeError, match="USB gadget mode"):
        mod._apply("usbsink", True)


def test_apply_refuses_renderer_when_local_sources_disallowed(monkeypatch):
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: False)
    monkeypatch.setattr(mod, "_unit_available", lambda unit: True)
    monkeypatch.setattr(
        mod, "_set_unit", lambda *a: pytest.fail("must not call systemctl"),
    )

    with pytest.raises(RuntimeError, match="not installed on this speaker"):
        mod._apply("spotify_connect", True)


def test_apply_refuses_bluetooth_when_local_sources_disallowed(monkeypatch):
    monkeypatch.setattr(mod, "_local_sources_allowed", lambda: False)

    async def fail_bt(_enabled):
        pytest.fail("must not call bluetooth DBus backend")

    monkeypatch.setattr(mod, "_set_bt", fail_bt)

    with pytest.raises(RuntimeError, match="not installed on this speaker"):
        mod._apply("bluetooth", True)


def test_set_unit_persists_intent_then_starts_or_stops(monkeypatch):
    # WS1 Phase 3b: _set_unit persists the enable/disable INTENT via the root
    # source-intent helper (manage-unit-files can't be brokered non-root), then
    # start/stops at runtime via the broker (manage-units, granted) — no direct
    # systemctl and no enable-now/disable-now broker verb (which fail-soft).
    persisted = []
    managed = []
    monkeypatch.setattr(
        mod, "_persist_source_intent",
        lambda unit, enabled: persisted.append((unit, enabled)),
    )
    monkeypatch.setattr(
        mod, "manage_units",
        lambda *units, **kw: managed.append((units, kw.get("verb"))) or {"ok": True},
    )
    mod._set_unit("foo.service", True)
    mod._set_unit("foo.service", False)
    assert persisted == [("foo.service", True), ("foo.service", False)]
    assert (("foo.service",), "start") in managed
    assert (("foo.service",), "stop") in managed
    # The enable/disable persistence never goes through the broker's
    # manage-unit-files verbs (they fail-soft for the non-root broker).
    verbs = [verb for _units, verb in managed]
    assert "enable-now" not in verbs and "disable-now" not in verbs


# ---- honesty layer: a failed toggle must LOOK failed -----------------------
#
# The pre-existing bug: the broker returns rc=1 ("Interactive authentication
# required") on enable/disable, sources_setup swallowed it, and the /set POST
# returned 200 — a toggle that lied. These pin that a broker rc!=0 on the
# persistence (enable/disable) OR the runtime (start/stop) half surfaces as a
# visible error, never a silent 200.


def test_persist_source_intent_raises_on_broker_kick_failure(
    monkeypatch, tmp_path, caplog,
):
    # A broker rc!=0 on the source-intent reconcile kick (the enable/disable
    # persistence path) MUST raise + log a WARN — a failed persist can't be seen
    # in the /state read-back (a runtime start/stop can look right now yet be
    # wrong after reboot), so silence here is the dangerous case.
    monkeypatch.setattr(mod, "SOURCE_INTENT_ENV", str(tmp_path / "intent.env"))
    monkeypatch.setattr(
        mod, "manage_units",
        lambda *u, **kw: {
            "ok": False, "rc": 1, "error": "Interactive authentication required",
        },
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError, match="could not persist"):
            mod._persist_source_intent("shairport-sync.service", False)
    assert any(
        "event=sources.intent_apply_failed" in r.getMessage() for r in caplog.records
    )


def test_set_unit_raises_on_runtime_failure(monkeypatch):
    # Persistence succeeds, but the runtime start/stop (broker manage-units)
    # fails — _set_unit must still raise so the toggle surfaces the failure.
    monkeypatch.setattr(mod, "_persist_source_intent", lambda *a, **k: None)
    monkeypatch.setattr(
        mod, "manage_units",
        lambda *u, **kw: {"ok": False, "rc": 1, "error": "start failed"},
    )
    with pytest.raises(RuntimeError, match="start failed"):
        mod._set_unit("shairport-sync.service", True)


def test_post_set_enable_disable_failure_returns_error_not_200(
    stub_backends, monkeypatch, tmp_path,
):
    # END-TO-END reproduction of the reported bug: POST /set with the broker
    # returning rc=1 "Interactive authentication required" on the enable/disable
    # persistence path must return a non-200 error response, NOT 200-and-silence.
    stub_backends(active=set(), usb_ready=True, bt=(True, False, False))
    monkeypatch.setattr(mod, "SOURCE_INTENT_ENV", str(tmp_path / "intent.env"))
    monkeypatch.setattr(
        mod, "manage_units",
        lambda *u, **kw: {
            "ok": False, "rc": 1, "error": "Interactive authentication required",
        },
    )
    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "airplay", "enabled": False}).encode(),
        csrf_cookie=CSRF, csrf_header=CSRF,
    )
    assert h.status == 502
    assert h.status != 200
    assert "error" in _body_json(h)


# ---- dtoverlay probe --------------------------------------------------------


def test_usbsink_available_reads_boot_config(monkeypatch, tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("# header\ndtoverlay=dwc2,dr_mode=peripheral\nother=1\n")
    monkeypatch.setattr(mod, "BOOT_CONFIG_PATH", str(cfg))
    assert mod._usbsink_available() is True

    cfg.write_text("# header\nother=1\n")
    assert mod._usbsink_available() is False


def test_usbsink_available_failsoft_on_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "BOOT_CONFIG_PATH", str(tmp_path / "absent.txt"))
    assert mod._usbsink_available() is False


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


def test_get_state_returns_snapshot(stub_backends):
    stub_backends(active={mod.AIRPLAY_UNIT}, usb_ready=True, bt=(True, False, False))
    h = _drive("GET", "/state")
    assert h.status == 200
    payload = _body_json(h)
    assert payload["airplay"]["enabled"] is True
    assert payload["usbsink"]["available"] is True


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
    stub_backends(active=set(), usb_ready=True, bt=(True, False, False))
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


def test_post_set_surfaces_apply_notice_on_usbsink_row(stub_backends, monkeypatch):
    # When _apply returns a notice (the USB combo could not be armed live), the
    # /set read-back must carry it as usbsink.degradedReason so the UI is honest
    # rather than reporting a clean success (no silent failure).
    stub_backends(active=set(), usb_ready=True, bt=(True, False, False))
    monkeypatch.setattr(
        mod, "_apply", lambda source, enabled: mod._USBSINK_COMBO_REBOOT_NOTICE
    )
    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "usbsink", "enabled": True}).encode(),
        csrf_cookie=CSRF, csrf_header=CSRF,
    )
    assert h.status == 200
    payload = _body_json(h)
    assert payload["usbsink"]["degradedReason"] == mod._USBSINK_COMBO_REBOOT_NOTICE


def test_post_set_no_notice_leaves_usbsink_row_clean(stub_backends, monkeypatch):
    # The common path (_apply returns None) must not inject a degradedReason.
    stub_backends(active=set(), usb_ready=True, bt=(True, False, False))
    monkeypatch.setattr(mod, "_apply", lambda source, enabled: None)
    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "usbsink", "enabled": True}).encode(),
        csrf_cookie=CSRF, csrf_header=CSRF,
    )
    assert h.status == 200
    payload = _body_json(h)
    assert "degradedReason" not in payload["usbsink"]


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
    """The dumb-follower profile parks every source; `enable --now` from
    the wizard would START parked source resources and reopen the
    advertise/leak hole until the next reconcile — so /set 409s with the
    pair story and applies NOTHING."""
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


def test_state_reports_parked_pair(stub_backends, monkeypatch):
    stub_backends()
    monkeypatch.setattr(mod, "bonded_follower_active", lambda: True)
    assert mod._gather_state()["pair"] == {"parked": True}
