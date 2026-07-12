# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

import asyncio
import http
import json
import logging
from email.message import Message
from io import BytesIO
from unittest import mock

import pytest

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
        self.run_calls = 0

    def run(self, coro):
        import asyncio

        self.run_calls += 1
        return asyncio.run(coro)

    @property
    def engine(self):
        return self._engine


def _make_request(
    path: str,
    body: bytes = b"",
    *,
    cookies: str = "",
    csrf_header: str = "",
    content_length: str | None = None,
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
    h.headers["Content-Length"] = (
        str(len(body)) if content_length is None else content_length
    )
    h.headers["Content-Type"] = "application/json"
    if cookies:
        h.headers["Cookie"] = cookies
    if csrf_header:
        h.headers["X-CSRF-Token"] = csrf_header
    h.rfile = BytesIO(body)
    h.wfile = BytesIO()
    h.client_address = ("127.0.0.1", 0)

    # Capture the response status; the real _send_html/_send_json adapters and
    # their shared response helpers call send_response()/send_header()/end_headers().
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


def test_local_json_adapter_preserves_wire_contract():
    h = _make_request("/")

    h._send_json({"label": "café"}, status=http.HTTPStatus.CREATED)

    body = b'{"label": "caf\\u00e9"}'
    assert h.status == int(http.HTTPStatus.CREATED)
    assert h.sent_headers == [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]
    assert h.wfile.getvalue() == body


def test_local_json_adapter_serialization_failure_emits_nothing():
    h = _make_request("/")

    with pytest.raises(TypeError):
        h._send_json({"unsupported": object()})

    assert h.status is None
    assert h.sent_headers == []
    assert h.wfile.getvalue() == b""


def test_unexpected_pair_driver_failure_is_logged_once(monkeypatch, caplog):
    monkeypatch.delenv("JASPER_LOG_JSON", raising=False)

    class _PairEngine:
        async def pair(self, _mac):
            yield {"stage": "pairing"}
            raise RuntimeError("synthetic-pair-crash")

    class _PairDispatcher:
        _loop = object()
        engine = _PairEngine()

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", _PairDispatcher())
    monkeypatch.setattr(
        bluetooth_setup.asyncio,
        "run_coroutine_threadsafe",
        lambda coro, _loop: asyncio.run(coro),
    )
    mac = "AA:BB:CC:DD:EE:FF"

    with caplog.at_level(logging.ERROR, logger=bluetooth_setup.__name__):
        bluetooth_setup._start_pair_stream(mac)

    q = bluetooth_setup._PAIR_STREAMS.pop(mac)
    assert q.get_nowait() == {"stage": "pairing"}
    assert q.get_nowait() == {
        "stage": "error",
        "message": "synthetic-pair-crash",
    }
    assert q.get_nowait() is None
    records = [
        record
        for record in caplog.records
        if record.getMessage() == "event=bluetooth.pair_failed"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is not None
    assert isinstance(records[0].exc_info[1], RuntimeError)


def test_expected_pair_error_event_is_not_logged_as_driver_failure(
    monkeypatch,
    caplog,
):
    monkeypatch.delenv("JASPER_LOG_JSON", raising=False)

    class _PairEngine:
        async def pair(self, _mac):
            yield {"stage": "error", "message": "pairing rejected"}

    class _PairDispatcher:
        _loop = object()
        engine = _PairEngine()

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", _PairDispatcher())
    monkeypatch.setattr(
        bluetooth_setup.asyncio,
        "run_coroutine_threadsafe",
        lambda coro, _loop: asyncio.run(coro),
    )
    mac = "11:22:33:44:55:66"

    with caplog.at_level(logging.ERROR, logger=bluetooth_setup.__name__):
        bluetooth_setup._start_pair_stream(mac)

    q = bluetooth_setup._PAIR_STREAMS.pop(mac)
    assert q.get_nowait() == {
        "stage": "error",
        "message": "pairing rejected",
    }
    assert q.get_nowait() is None
    assert not any(
        record.getMessage() == "event=bluetooth.pair_failed"
        for record in caplog.records
    )


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
        "/pair/AA:BB:CC:DD:EE:FF/respond",
        body=b'{"accept": true}',
        cookies="jts_csrf=" + token,
        csrf_header=token,
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
        "/scan",
        body=b'{"action": "start"}',
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )
    h.do_POST()
    assert h.status == 200
    assert ("start_discovery", bluetooth_setup.SCAN_DURATION_SEC) in fake.engine.calls
    payload = json.loads(h.wfile.getvalue().decode())
    assert payload["ok"] is True


def test_second_scan_request_after_owner_bus_release_never_returns_false_200(
    monkeypatch,
):
    class _FailClosedEngine:
        def __init__(self) -> None:
            self.calls = 0
            self.owner_bus_released = False

        async def start_discovery(self, *, duration_s):
            assert duration_s == bluetooth_setup.SCAN_DURATION_SEC
            self.calls += 1
            if self.calls == 1:
                self.owner_bus_released = True
                raise asyncio.TimeoutError("StartDiscovery timed out")
            assert self.owner_bus_released is True
            raise RuntimeError("BlueZ bus recovery failed: system bus unavailable")

    class _FailClosedDispatcher:
        def __init__(self) -> None:
            self.engine = _FailClosedEngine()

        def run(self, coro):
            return asyncio.run(coro)

    token = "b" * 64
    dispatcher = _FailClosedDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", dispatcher)

    first = _make_request(
        "/scan",
        body=b'{"action": "start"}',
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )
    first.do_POST()
    second = _make_request(
        "/scan",
        body=b'{"action": "start"}',
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )
    second.do_POST()

    assert first.status == int(http.HTTPStatus.BAD_GATEWAY)
    assert second.status == int(http.HTTPStatus.BAD_GATEWAY)
    assert json.loads(second.wfile.getvalue()) == {
        "error": "BlueZ bus recovery failed: system bus unavailable",
    }
    assert dispatcher.engine.calls == 2


@pytest.mark.parametrize("path", ("/power", "/discoverable"))
@pytest.mark.parametrize(
    ("body", "content_length"),
    (
        (b"{", None),
        (b"", None),
        (b"[]", None),
        (b'{"on":false}', "not-a-number"),
        (b"", "1000001"),
        (b'{"on":"false"}', None),
        (b'{"on":0}', None),
        (b'{"on":null}', None),
    ),
)
def test_boolean_adapter_routes_reject_invalid_body_without_dispatch(
    monkeypatch,
    path,
    body,
    content_length,
):
    token = "v" * 64
    fake = _FakeDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    h = _make_request(
        path,
        body=body,
        content_length=content_length,
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )

    h.do_POST()

    assert h.status == int(http.HTTPStatus.BAD_REQUEST)
    assert json.loads(h.wfile.getvalue()) == {
        "error": "on must be true or false",
    }
    assert fake.run_calls == 0
    assert fake.engine.calls == []


def test_boolean_adapter_route_rejects_stream_oserror_without_dispatch(
    monkeypatch,
):
    class BrokenReader:
        def read(self, _length):
            raise OSError("socket reset")

    token = "o" * 64
    fake = _FakeDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    h = _make_request(
        "/power",
        body=b"x",
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )
    h.rfile = BrokenReader()

    h.do_POST()

    assert h.status == int(http.HTTPStatus.BAD_REQUEST)
    assert json.loads(h.wfile.getvalue()) == {
        "error": "on must be true or false",
    }
    assert fake.run_calls == 0
    assert fake.engine.calls == []


@pytest.mark.parametrize(
    ("body", "content_length"),
    (
        (b"{", None),
        (b"[]", None),
        (b"{}", None),
        (b'{"mac":null}', None),
        (b'{"mac":123}', None),
        (b'{"mac":"AA:BB"}', "not-a-number"),
        (b"", "1000001"),
        (b'{"mac":"AA:BB"}', "16"),
    ),
)
@pytest.mark.parametrize("path", ("/pair", "/connect", "/disconnect", "/forget"))
def test_address_routes_reject_invalid_body_without_dispatch(
    monkeypatch,
    path,
    body,
    content_length,
):
    token = "m" * 64
    fake = _FakeDispatcher()
    pair_start = mock.Mock()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "_start_pair_stream", pair_start)
    h = _make_request(
        path,
        body=body,
        content_length=content_length,
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )

    h.do_POST()

    assert h.status == int(http.HTTPStatus.BAD_REQUEST)
    assert json.loads(h.wfile.getvalue()) == {"error": "missing mac"}
    assert fake.run_calls == 0
    assert fake.engine.calls == []
    pair_start.assert_not_called()


@pytest.mark.parametrize(
    ("path", "setter_name"),
    (("/power", "set_powered"), ("/discoverable", "set_discoverable")),
)
@pytest.mark.parametrize("value", (False, True))
def test_boolean_adapter_routes_pass_exact_json_boolean(
    monkeypatch,
    path,
    setter_name,
    value,
):
    token = "q" * 64
    calls = []
    fake = _FakeDispatcher()

    async def setter(on):
        calls.append(on)

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, setter_name, setter)
    h = _make_request(
        path,
        body=json.dumps({"on": value}).encode(),
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )

    h.do_POST()

    assert h.status == int(http.HTTPStatus.OK)
    assert calls == [value]
    assert fake.run_calls == 1


def test_post_connect_drives_engine(monkeypatch):
    token = "y" * 64
    fake = _FakeDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)

    h = _make_request(
        "/connect",
        body=b'{"mac": "AA:BB:CC:DD:EE:FF"}',
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )
    h.do_POST()
    assert h.status == 200
    assert ("connect", "AA:BB:CC:DD:EE:FF") in fake.engine.calls


def test_post_forget_drives_engine(monkeypatch):
    token = "w" * 64
    fake = _FakeDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)

    h = _make_request(
        "/forget",
        body=b'{"mac": "11:22:33:44:55:66"}',
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )
    h.do_POST()
    assert h.status == 200
    assert ("forget", "11:22:33:44:55:66") in fake.engine.calls


def test_post_bad_csrf_is_rejected(monkeypatch):
    fake = _FakeDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    # Header token differs from the cookie token -> double-submit fails.
    h = _make_request(
        "/connect",
        body=b'{"mac": "x"}',
        cookies="jts_csrf=" + "a" * 64,
        csrf_header="b" * 64,
    )
    h.do_POST()
    assert h.status == int(http.HTTPStatus.FORBIDDEN)
    # Engine was never touched.
    assert fake.engine.calls == []
