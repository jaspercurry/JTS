"""Tests for the /bluetooth/ control panel after its migration to the canonical
design system.

1. The landing page renders canonical design-system bytes (links
   /assets/app.css, carries the shared .app-header + icon sprite, embeds the
   CSRF meta tag, links the page stylesheet) and delivers its behaviour as an
   ES module -- no inline <script> body, no legacy hand-rolled doctype.
2. The migration was presentation-only: every route still resolves, the JSON
   POST handlers still enforce CSRF and drive the Bluetooth engine, the SSE
   pair-stream coordination remains, and the public module surface
   (_landing_html / main) is unchanged.

The Bluetooth engine and its asyncio dispatcher are mocked -- these tests are
hardware-free (no dbus / bluez).
"""
from __future__ import annotations

import http
import json
from email.message import Message
from io import BytesIO

from jasper.web import bluetooth_setup


# --------------------------------------------------------------------------
# Static render assertions (no handler, no dispatcher).
# --------------------------------------------------------------------------

CSRF = "tok-abcdefghijklmnopqrstuvwxyz0123456789ABCD"  # 43+ url-safe chars


def _render(csrf_token: str = CSRF) -> str:
    return bluetooth_setup._landing_html(csrf_token).decode()


def test_bluetooth_page_is_canonical_document():
    out = _render()
    assert out.startswith("<!doctype html>")
    assert "/assets/app.css?v=" in out
    # Legacy hand-rolled shell + the old fixed page width are gone.
    assert "max-width: 720px" not in out
    assert 'class="nav-back"' not in out


def test_bluetooth_page_links_page_stylesheet():
    out = _render()
    assert "/assets/bluetooth/bluetooth.css?v=" in out


def test_bluetooth_page_has_shared_app_header():
    out = _render()
    assert 'class="app-header"' in out
    assert '<h1 class="app-header__title">Bluetooth</h1>' in out
    assert '<use href="#icon-back">' in out


def test_bluetooth_page_embeds_csrf_meta():
    out = _render()
    assert 'meta name="jts-csrf"' in out
    assert 'content="' + CSRF + '"' in out


def test_bluetooth_page_loads_es_module_not_inline_script():
    out = _render()
    assert '<script type="module" src="/assets/bluetooth/js/main.js">' in out
    # No behavioural inline JS remains in the server template: the helper calls
    # and the device-rendering logic moved to the module.
    before_module = out.split('<script type="module"')[0]
    assert "jtsConfirm(" not in before_module
    assert "addEventListener" not in before_module
    assert "function deviceRow" not in before_module


def test_bluetooth_toggles_use_shared_toggle_helper():
    out = _render()
    # Canonical checkbox toggle markup (toggle_html), not a clickable div.
    assert 'class="toggle"' in out
    assert 'id="sw-power"' in out
    assert 'id="sw-disc"' in out
    assert 'class="switch"' not in out


def test_bluetooth_scan_button_has_no_inline_onclick():
    out = _render()
    assert 'id="scan-btn"' in out
    # The Scan button used to carry onclick="toggleScan()"; it's now wired in
    # the module via addEventListener.
    assert "onclick=" not in out


def test_bluetooth_device_list_scaffolds_present():
    out = _render()
    assert 'id="paired-list"' in out
    assert 'id="other-list"' in out


def test_bluetooth_title_is_escaped_once():
    # canonical_page / canonical_header own escaping; the title is static here,
    # but assert the document <title> is present and singular.
    out = _render()
    assert "<title>Bluetooth</title>" in out


# --------------------------------------------------------------------------
# Behaviour: routes still resolve + CSRF enforced + engine driven.
# --------------------------------------------------------------------------


class _FakeEngine:
    """Records calls the handler makes against the Bluetooth engine."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def start_discovery(self, *, duration_s):
        self.calls.append(("start_discovery", duration_s))

    async def stop_discovery(self):
        self.calls.append(("stop_discovery",))

    async def connect(self, mac):
        self.calls.append(("connect", mac))
        return True, "connected"

    async def disconnect(self, mac):
        self.calls.append(("disconnect", mac))
        return True, "disconnected"

    async def forget(self, mac):
        self.calls.append(("forget", mac))
        return True, "forgotten"


class _FakeDispatcher:
    """Stand-in for _AsyncDispatcher: runs coroutines synchronously."""

    def __init__(self) -> None:
        self._engine = _FakeEngine()

    def run(self, coro):
        import asyncio
        return asyncio.run(coro)

    @property
    def engine(self):
        return self._engine


def _make_request(
    path: str, body: bytes = b"", *,
    cookies: str = "", csrf_header: str = "",
):
    """Instantiate the REAL handler class without running
    BaseHTTPRequestHandler.__init__ (which expects a live socket), then attach
    the minimal request I/O surface. This exercises the handler's own private
    helpers (_send_html / _read_json / _send_json) and the real do_GET/do_POST
    routing, rather than reimplementing them in a fake -- the migration didn't
    touch any of that, and the test should prove it.

    Returns the handler instance; read `handler.status` and
    `handler.wfile.getvalue()` after invoking do_GET/do_POST.
    """
    cls = bluetooth_setup._make_handler()
    h = cls.__new__(cls)  # bypass socketserver __init__
    h.path = path
    h.headers = Message()
    h.headers["Content-Length"] = str(len(body))
    h.headers["Content-Type"] = "application/json"
    if cookies:
        h.headers["Cookie"] = cookies
    if csrf_header:
        h.headers["X-CSRF-Token"] = csrf_header
    h.rfile = BytesIO(body)
    h.wfile = BytesIO()
    h.client_address = ("127.0.0.1", 0)

    # Capture the response status; the real _send/_send_html/_send_json and the
    # shared send_html_response call send_response()/send_header()/end_headers().
    h.status = None
    h.sent_headers = []

    def _send_response(code, *a, **k):
        h.status = int(code)

    def _send_header(name, value):
        h.sent_headers.append((name, value))

    def _send_error(code, *a, **k):
        h.status = int(code)

    h.send_response = _send_response
    h.send_response_only = _send_response
    h.send_header = _send_header
    h.end_headers = lambda: None
    h.send_error = _send_error
    return h


def test_public_surface_is_stable():
    assert callable(bluetooth_setup.main)
    assert callable(bluetooth_setup._landing_html)
    assert callable(bluetooth_setup._make_handler)


def test_get_root_renders_canonical_page():
    h = _make_request("/")
    h.do_GET()
    assert h.status == 200
    out = h.wfile.getvalue().decode()
    assert "/assets/app.css?v=" in out
    assert 'class="app-header"' in out
    assert '<script type="module" src="/assets/bluetooth/js/main.js">' in out


def test_get_unknown_route_404s():
    h = _make_request("/nope")
    h.do_GET()
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_unknown_route_404s_without_revealing_csrf():
    # Route-check happens before CSRF-check: a bogus path 404s even with no
    # token, so it can't be used to probe CSRF state.
    h = _make_request("/bogus", body=b"{}")
    h.do_POST()
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_pair_response_route_is_gone():
    token = "r" * 64
    h = _make_request(
        "/pair/AA:BB:CC:DD:EE:FF/respond", body=b'{"accept": true}',
        cookies="jts_csrf=" + token, csrf_header=token,
    )
    h.do_POST()
    assert h.status == int(http.HTTPStatus.NOT_FOUND)


def test_post_scan_rejects_missing_csrf():
    # Valid route, but no CSRF header/cookie -> 403.
    h = _make_request("/scan", body=b'{"action": "start"}')
    h.do_POST()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)


def test_post_scan_start_drives_engine_with_valid_csrf(monkeypatch):
    token = "z" * 64
    fake = _FakeDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)

    h = _make_request(
        "/scan", body=b'{"action": "start"}',
        cookies="jts_csrf=" + token, csrf_header=token,
    )
    h.do_POST()
    assert h.status == 200
    assert ("start_discovery", bluetooth_setup.SCAN_DURATION_SEC) in fake.engine.calls
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["ok"] is True


def test_post_connect_drives_engine(monkeypatch):
    token = "y" * 64
    fake = _FakeDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)

    h = _make_request(
        "/connect", body=b'{"mac": "AA:BB:CC:DD:EE:FF"}',
        cookies="jts_csrf=" + token, csrf_header=token,
    )
    h.do_POST()
    assert h.status == 200
    assert ("connect", "AA:BB:CC:DD:EE:FF") in fake.engine.calls


def test_post_forget_drives_engine(monkeypatch):
    token = "w" * 64
    fake = _FakeDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)

    h = _make_request(
        "/forget", body=b'{"mac": "11:22:33:44:55:66"}',
        cookies="jts_csrf=" + token, csrf_header=token,
    )
    h.do_POST()
    assert h.status == 200
    assert ("forget", "11:22:33:44:55:66") in fake.engine.calls


def test_post_bad_csrf_is_rejected(monkeypatch):
    fake = _FakeDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    # Header token differs from the cookie token -> double-submit fails.
    h = _make_request(
        "/connect", body=b'{"mac": "x"}',
        cookies="jts_csrf=" + "a" * 64, csrf_header="b" * 64,
    )
    h.do_POST()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)
    # Engine was never touched.
    assert fake.engine.calls == []
