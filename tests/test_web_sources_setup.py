# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free tests for the /sources/ wizard page (sources_setup.py).

Renders the page and drives the /state + /set handlers in-process. State
derivation and enable-time preconditions live in
``jasper.local_sources.status`` (see tests/test_local_sources_status.py);
these tests pin only what this module itself owns: the page render, the
route/CSRF/JSON-framing contract, and that /set defers to the shared
coordinator (``jasper.local_sources.status`` + ``request_source_intent``)
rather than re-implementing it.
"""
from __future__ import annotations

import io
import json
from email.message import Message
from http import HTTPStatus
from pathlib import Path

import pytest

from jasper.music_sources import Source
from jasper.web import _common
from jasper.web import sources_setup as mod
from tests._web_test_helpers import assert_canonical_page

CSRF = "x" * 43

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_MODULE = REPO_ROOT / "deploy" / "assets" / "sources" / "js" / "main.js"


# ---- render -----------------------------------------------------------------


def test_renders_through_canonical_page():
    html = mod._index_html(csrf_token=CSRF, status_msg="Saved.").decode("utf-8")
    assert_canonical_page(html)
    assert '<meta name="jts-csrf"' in html
    assert "Playback sources" in html


def test_no_legacy_switch_markup():
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    # toggle_html (canonical) is used; the legacy clickable switch is gone,
    # and no unrendered template placeholder survived.
    assert 'class="switch"' not in html
    assert 'class="slider"' not in html
    assert html.count('class="toggle"') == 4
    assert "{toggle_" not in html


def test_behaviour_ships_as_es_module():
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    assert '<script type="module" src="/assets/sources/js/main.js">' in html
    # No inline behaviour script survived the migration.
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
    # first-paint contract (no checked); checked-state is covered by
    # jasper.local_sources.status's tests + the ES module.
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    assert "checked" not in html


def test_usb_unavailable_note_present_but_hidden_at_render():
    # The hardware note exists in the markup (the /state poll un-hides it
    # when available=false); it is not server-gated.
    html = mod._index_html(csrf_token=CSRF).decode("utf-8")
    assert 'id="usbsink-unavailable-note"' in html
    assert "current hardware configuration" in html
    assert 'id="usbsink-unavailable-note" hidden' in html


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


def test_get_state_returns_snapshot(monkeypatch):
    monkeypatch.setattr(
        mod.source_status,
        "read_source_status",
        lambda: {
            "pair": {"parked": False},
            "airplay": {"enabled": True, "desired": True, "effective": "on", "available": True},
        },
    )
    h = _drive("GET", "/state")
    assert h.status == 200
    payload = _body_json(h)
    assert payload["airplay"]["enabled"] is True


def test_get_state_failure_is_explicit_for_initial_hydration(monkeypatch):
    def fail_state():
        raise RuntimeError("invalid source intent")

    monkeypatch.setattr(mod.source_status, "read_source_status", fail_state)
    h = _drive("GET", "/state")
    assert h.status == 502
    assert _body_json(h) == {"error": "invalid source intent"}


def test_post_set_without_csrf_is_rejected():
    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "airplay", "enabled": True}).encode(),
    )
    # reject_csrf sends 403 before the handler (and _apply) ever runs.
    assert h.status == int(HTTPStatus.FORBIDDEN)


def test_post_set_with_csrf_dispatches_and_reads_back(monkeypatch):
    applied = []
    monkeypatch.setattr(
        mod, "_apply", lambda source, enabled: applied.append((source, enabled))
    )
    monkeypatch.setattr(
        mod.source_status,
        "read_source_status",
        lambda: {
            "pair": {"parked": False},
            "airplay": {}, "bluetooth": {}, "spotify_connect": {}, "usbsink": {},
        },
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


def test_post_set_blocked_enable_returns_502_with_reason(monkeypatch):
    """`_apply` raises `enable_blocker`'s reason verbatim; the route
    surfaces it as the /set error without ever requesting the intent."""
    monkeypatch.setattr(
        mod.source_status,
        "enable_blocker",
        lambda target: (
            "AirPlay is not installed on this speaker. Re-run install.sh "
            "to set up the local renderer stack."
        ),
    )
    monkeypatch.setattr(
        mod, "request_source_intent",
        lambda *a: pytest.fail("must not request a blocked intent"),
    )
    monkeypatch.setattr(
        mod.source_status, "read_source_status",
        lambda: {"pair": {"parked": False}},
    )

    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "airplay", "enabled": True}).encode(),
        csrf_cookie=CSRF, csrf_header=CSRF,
    )

    assert h.status == 502
    assert "not installed on this speaker" in _body_json(h)["error"]


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
    monkeypatch.setattr(
        mod.source_status, "read_source_status", lambda: durable_state,
    )

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
        mod.source_status,
        "read_source_status",
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


def test_post_set_unknown_source_400():
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
    monkeypatch,
    body,
    content_length,
    expected_reads,
):
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


def test_post_set_request_body_oserror_remains_distinct(monkeypatch):
    monkeypatch.setattr(mod, "_apply", lambda *_a: pytest.fail("must not apply"))
    handler = _make_inst(
        "/set",
        body=b"{}",
        cookies=f"{_common.CSRF_COOKIE_NAME}={CSRF}",
        csrf_header=CSRF,
    )
    handler.rfile = _TrackingReader(b"{}", fail=True)

    with pytest.raises(OSError):
        handler.do_POST()

    assert handler.status is None


def test_post_unknown_path_is_404():
    h = _drive("POST", "/bogus", body=b"{}")
    assert h.status == int(HTTPStatus.NOT_FOUND)


def test_get_unknown_path_is_404():
    h = _drive("GET", "/bogus")
    assert h.status == int(HTTPStatus.NOT_FOUND)


def test_set_rejected_while_bonded_follower(monkeypatch):
    """The pair owns source choices while bonded, so a follower cannot
    accumulate hidden desired state that surprises the household on unpair."""
    monkeypatch.setattr(mod.source_status, "sources_parked", lambda: True)
    monkeypatch.setattr(mod, "_apply", lambda *a: pytest.fail("must not apply"))
    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": "airplay", "enabled": True}).encode(),
        csrf_cookie=CSRF, csrf_header=CSRF,
    )
    assert h.status == 409
    assert "stereo pair" in _body_json(h)["error"]


# ---- _apply: the route defers to the shared coordinator, never re-implements it


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
def test_post_set_routes_each_source_through_shared_coordinator(
    monkeypatch, wizard_key, source, enabled,
):
    """One route test over the four sources x on/off: `_apply` consults
    `enable_blocker` only when enabling (a stale desired-on source must stay
    turn-offable even when blocked from turning back on) and always hands
    the result to the shared source-intent coordinator."""
    monkeypatch.setattr(mod.source_status, "sources_parked", lambda: False)
    monkeypatch.setattr(
        mod.source_status, "read_source_status", lambda: {"pair": {"parked": False}},
    )
    blocker_calls = []

    def enable_blocker(target):
        blocker_calls.append(target)
        return ""

    monkeypatch.setattr(mod.source_status, "enable_blocker", enable_blocker)
    applied = []
    monkeypatch.setattr(
        mod, "request_source_intent",
        lambda target, desired: applied.append((target, desired)),
    )

    h = _drive(
        "POST", "/set",
        body=json.dumps({"source": wizard_key, "enabled": enabled}).encode(),
        csrf_cookie=CSRF, csrf_header=CSRF,
    )

    assert h.status == 200
    assert applied == [(source, enabled)]
    assert blocker_calls == ([source] if enabled else [])


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
    handler_end = text.index("startPolling(fetchState", handler_start)
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
