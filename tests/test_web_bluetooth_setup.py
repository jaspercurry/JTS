# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /bluetooth/ control panel after its migration to the canonical
design system.

1. The landing page renders canonical design-system bytes (links
   /assets/app.css, carries the shared .app-header + icon sprite, embeds the
   CSRF meta tag, links the page stylesheet) and delivers its behaviour as an
   ES module -- no inline <script> body, no legacy hand-rolled doctype.
2. Every route still resolves, the JSON POST handlers still enforce CSRF,
   Bluetooth power delegates to the shared persisted source-intent authority,
   adapter-local operations still use the async dispatcher, the SSE pair-stream
   coordination remains, and the public module surface (_landing_html / main)
   is unchanged.

The Bluetooth engine and its asyncio dispatcher are mocked -- these tests are
hardware-free (no dbus / bluez).
"""

from __future__ import annotations

import asyncio
import http
import inspect
import json
import logging
import threading
import time
from email.message import Message
from io import BytesIO
from unittest import mock

import pytest

from jasper.web import bluetooth_setup


# --------------------------------------------------------------------------
# Static render assertions (no handler, no dispatcher).
# --------------------------------------------------------------------------

CSRF = "tok-abcdefghijklmnopqrstuvwxyz0123456789ABCD"  # 43+ url-safe chars


def _availability(
    *,
    available: bool = True,
    radio_present: bool = True,
    any_soft_blocked: bool = False,
    all_soft_blocked: bool = False,
    hard_blocked: bool = False,
    error: str = "",
    missing_units: tuple[str, ...] = (),
) -> bluetooth_setup.BluetoothAvailability:
    return bluetooth_setup.BluetoothAvailability(
        available=available,
        radio_present=radio_present,
        any_soft_blocked=any_soft_blocked,
        all_soft_blocked=all_soft_blocked,
        hard_blocked=hard_blocked,
        error=error,
        missing_units=missing_units,
    )


@pytest.fixture(autouse=True)
def _hardware_free_availability_and_pair_cleanup(monkeypatch):
    class _Snapshot:
        @staticmethod
        def available(_unit):
            return True

        @staticmethod
        def active(unit):
            return bluetooth_setup._unit_active(unit)

        @staticmethod
        def activating(_unit):
            return False

    monkeypatch.setattr(
        bluetooth_setup, "probe_unit_snapshot", lambda _units: _Snapshot(),
    )
    monkeypatch.setattr(
        bluetooth_setup,
        "probe_bluetooth_availability",
        lambda _unit_probe: _availability(),
    )
    with bluetooth_setup._PAIR_STREAMS_LOCK:
        abandoned = list(bluetooth_setup._PAIR_STREAMS.values())
        bluetooth_setup._PAIR_STREAMS.clear()
    for attempt in abandoned:
        bluetooth_setup._cancel_pair_attempt(attempt)
    yield
    with bluetooth_setup._PAIR_STREAMS_LOCK:
        abandoned = list(bluetooth_setup._PAIR_STREAMS.values())
        bluetooth_setup._PAIR_STREAMS.clear()
    for attempt in abandoned:
        bluetooth_setup._cancel_pair_attempt(attempt)


@pytest.fixture
def _background_event_loop():
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=1)
        loop.close()


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

    def run(self, coro, **_kwargs):
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

    attempt = bluetooth_setup._PAIR_STREAMS[mac]
    assert attempt.queue.get_nowait() == {"stage": "pairing"}
    assert attempt.queue.get_nowait() == {
        "stage": "error",
        "message": "synthetic-pair-crash",
    }
    assert attempt.queue.get_nowait() is None
    bluetooth_setup._release_pair_attempt(mac, attempt)
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

    attempt = bluetooth_setup._PAIR_STREAMS[mac]
    assert attempt.queue.get_nowait() == {
        "stage": "error",
        "message": "pairing rejected",
    }
    assert attempt.queue.get_nowait() is None
    bluetooth_setup._release_pair_attempt(mac, attempt)
    assert not any(
        record.getMessage() == "event=bluetooth.pair_failed"
        for record in caplog.records
    )


class _PendingFuture:
    def __init__(self) -> None:
        self.cancelled = False

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancelled = True


class _ImmediateFuture:
    def __init__(self, value) -> None:
        self._value = value

    def result(self):
        return self._value


class _InlineLoop:
    def call_soon_threadsafe(self, callback, *args) -> None:
        callback(*args)


def test_pair_attempt_is_registered_before_driver_submission(monkeypatch):
    mac = "AA:BB:CC:DD:EE:FF"
    driver_future = _PendingFuture()

    class _PairEngine:
        async def pair(self, _mac):
            yield {"stage": "unused"}

    class _PairDispatcher:
        _loop = object()
        engine = _PairEngine()

    def submit(coro, _loop):
        assert mac in bluetooth_setup._PAIR_STREAMS
        coro.close()
        return driver_future

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", _PairDispatcher())
    monkeypatch.setattr(bluetooth_setup.asyncio, "run_coroutine_threadsafe", submit)

    assert bluetooth_setup._start_pair_stream(mac.lower()) is True
    attempt = bluetooth_setup._PAIR_STREAMS[mac]
    assert attempt.driver_future is driver_future
    assert attempt.expiry_timer is not None

    assert bluetooth_setup._release_pair_attempt(mac, attempt) is True
    assert driver_future.cancelled is True


def test_fresh_duplicate_pair_attempt_is_rejected(monkeypatch):
    futures = [_PendingFuture(), _PendingFuture()]
    submissions = 0

    class _PairEngine:
        async def pair(self, _mac):
            yield {"stage": "unused"}

    class _PairDispatcher:
        _loop = object()
        engine = _PairEngine()

    def submit(coro, _loop):
        nonlocal submissions
        coro.close()
        future = futures[submissions]
        submissions += 1
        return future

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", _PairDispatcher())
    monkeypatch.setattr(bluetooth_setup.asyncio, "run_coroutine_threadsafe", submit)

    assert bluetooth_setup._start_pair_stream("aa:bb:cc:dd:ee:ff") is True
    assert bluetooth_setup._start_pair_stream("AA:BB:CC:DD:EE:FF") is False
    assert submissions == 1


def test_stale_pair_attempt_is_replaced_and_cancelled(monkeypatch):
    mac = "AA:BB:CC:DD:EE:FF"
    old_future = _PendingFuture()
    old_queue: asyncio.Queue[dict | None] = asyncio.Queue()
    old_attempt = bluetooth_setup._PairAttempt(
        queue=old_queue,
        created_at=1.0,
        driver_future=old_future,
        consumer_attached=True,
        loop=_InlineLoop(),
    )
    bluetooth_setup._PAIR_STREAMS[mac] = old_attempt
    new_future = _PendingFuture()

    class _PairEngine:
        async def pair(self, _mac):
            yield {"stage": "unused"}

    class _PairDispatcher:
        _loop = object()
        engine = _PairEngine()

    def submit(coro, _loop):
        coro.close()
        return new_future

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", _PairDispatcher())
    monkeypatch.setattr(bluetooth_setup.asyncio, "run_coroutine_threadsafe", submit)
    monkeypatch.setattr(
        bluetooth_setup.time,
        "monotonic",
        lambda: 1.0 + bluetooth_setup.PAIR_STREAM_TTL_SEC,
    )

    assert bluetooth_setup._start_pair_stream(mac) is True
    assert bluetooth_setup._PAIR_STREAMS[mac] is not old_attempt
    assert old_future.cancelled is True
    assert old_queue.get_nowait() == {
        "stage": "error",
        "message": "Pair attempt was superseded.",
    }
    assert old_queue.get_nowait() is None


def test_pair_attempt_ttl_expiry_wakes_blocked_consumer_before_cancel(
    monkeypatch,
    _background_event_loop,
):
    mac = "AA:BB:CC:DD:EE:FF"
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    loop = _background_event_loop
    driver_started = threading.Event()

    async def blocked_driver():
        driver_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Real pair-driver cancellation appends its own sentinel. The
            # explicit terminal error must already be ahead of it.
            await queue.put(None)

    driver_future = asyncio.run_coroutine_threadsafe(blocked_driver(), loop)
    assert driver_started.wait(timeout=1)
    attempt = bluetooth_setup._PairAttempt(
        queue=queue,
        created_at=1.0,
        driver_future=driver_future,
        loop=loop,
    )
    bluetooth_setup._PAIR_STREAMS[mac] = attempt

    class _PairDispatcher:
        _loop = loop

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", _PairDispatcher())
    received: list[dict] = []
    consumer_done = threading.Event()

    def consume() -> None:
        received.extend(bluetooth_setup._consume_pair_stream(mac))
        consumer_done.set()

    consumer_thread = threading.Thread(target=consume, daemon=True)
    consumer_thread.start()
    deadline = time.monotonic() + 1
    while not attempt.consumer_attached and time.monotonic() < deadline:
        time.sleep(0.001)
    assert attempt.consumer_attached is True

    bluetooth_setup._expire_pair_attempt(mac, attempt)

    assert consumer_done.wait(timeout=1)
    consumer_thread.join(timeout=1)
    assert mac not in bluetooth_setup._PAIR_STREAMS
    assert received == [{
        "stage": "error",
        "message": "Pairing timed out.",
    }]
    assert driver_future.cancelled() is True


def test_pair_driver_submission_failure_closes_unowned_coroutine(monkeypatch):
    mac = "AA:BB:CC:DD:EE:FF"
    captured: list[object] = []
    attempts: list[bluetooth_setup._PairAttempt] = []

    class _PairEngine:
        async def pair(self, _mac):
            yield {"stage": "unused"}

    class _PairDispatcher:
        _loop = _InlineLoop()
        engine = _PairEngine()

    def reject(coro, _loop):
        captured.append(coro)
        attempts.append(bluetooth_setup._PAIR_STREAMS[mac])
        attempts[0].consumer_attached = True
        raise RuntimeError("loop stopped")

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", _PairDispatcher())
    monkeypatch.setattr(bluetooth_setup.asyncio, "run_coroutine_threadsafe", reject)

    with pytest.raises(RuntimeError, match="loop stopped"):
        bluetooth_setup._start_pair_stream(mac)

    assert inspect.getcoroutinestate(captured[0]) == inspect.CORO_CLOSED
    assert mac not in bluetooth_setup._PAIR_STREAMS
    assert attempts[0].queue.get_nowait() == {
        "stage": "error",
        "message": "Pairing could not start.",
    }
    assert attempts[0].queue.get_nowait() is None


def test_dispatcher_submission_failure_closes_unowned_coroutine(monkeypatch):
    dispatcher = bluetooth_setup._AsyncDispatcher.__new__(
        bluetooth_setup._AsyncDispatcher,
    )
    dispatcher._loop = _InlineLoop()

    async def operation():
        return None

    coro = operation()
    monkeypatch.setattr(
        bluetooth_setup.asyncio,
        "run_coroutine_threadsafe",
        mock.Mock(side_effect=RuntimeError("loop stopped")),
    )

    with pytest.raises(RuntimeError, match="loop stopped"):
        dispatcher.run(coro)

    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


def test_pair_stream_allows_one_consumer_and_close_cleans_driver(monkeypatch):
    mac = "AA:BB:CC:DD:EE:FF"
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    queue.put_nowait({"stage": "pairing"})
    driver_future = _PendingFuture()
    attempt = bluetooth_setup._PairAttempt(
        queue=queue,
        created_at=1.0,
        driver_future=driver_future,
    )
    bluetooth_setup._PAIR_STREAMS[mac] = attempt

    class _PairDispatcher:
        _loop = object()

    def submit(coro, _loop):
        return _ImmediateFuture(asyncio.run(coro))

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", _PairDispatcher())
    monkeypatch.setattr(bluetooth_setup.asyncio, "run_coroutine_threadsafe", submit)

    first = bluetooth_setup._consume_pair_stream(mac)
    assert next(first) == {"stage": "pairing"}
    assert list(bluetooth_setup._consume_pair_stream(mac)) == [
        {"stage": "error", "message": "pair stream already attached"},
    ]

    first.close()

    assert mac not in bluetooth_setup._PAIR_STREAMS
    assert driver_future.cancelled is True


def test_pair_sse_disconnect_closes_consumer_and_cancels_driver(monkeypatch):
    mac = "AA:BB:CC:DD:EE:FF"
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    queue.put_nowait({"stage": "pairing"})
    driver_future = _PendingFuture()
    attempt = bluetooth_setup._PairAttempt(
        queue=queue,
        created_at=1.0,
        driver_future=driver_future,
    )
    bluetooth_setup._PAIR_STREAMS[mac] = attempt

    class _PairDispatcher:
        _loop = object()

    def submit(coro, _loop):
        return _ImmediateFuture(asyncio.run(coro))

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", _PairDispatcher())
    monkeypatch.setattr(bluetooth_setup.asyncio, "run_coroutine_threadsafe", submit)
    handler = _make_request("/")
    handler._begin_sse = mock.Mock()
    handler._sse_write = mock.Mock(return_value=False)

    handler._stream_pair(mac)

    handler._sse_write.assert_called_once_with({"stage": "pairing"})
    assert mac not in bluetooth_setup._PAIR_STREAMS
    assert driver_future.cancelled is True


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


@pytest.mark.parametrize(
    "path",
    (
        "/pair/not-a-mac/stream",
        "/pair/AA%2FBB%3ACC%3ADD%3AEE%3AFF/stream",
        "/pair/AA%5CBB%3ACC%3ADD%3AEE%3AFF/stream",
        "/pair/%FF/stream",
    ),
)
def test_pair_stream_route_rejects_invalid_encoded_mac(path):
    h = _make_request(path)
    h._stream_pair = mock.Mock()

    h.do_GET()

    assert h.status == int(http.HTTPStatus.BAD_REQUEST)
    h._stream_pair.assert_not_called()


def test_pair_stream_route_decodes_and_normalizes_mac():
    h = _make_request("/pair/aa%3Abb%3Acc%3Add%3Aee%3Aff/stream")
    h._stream_pair = mock.Mock()

    h.do_GET()

    h._stream_pair.assert_called_once_with("AA:BB:CC:DD:EE:FF")


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
    monkeypatch.setattr(
        bluetooth_setup, "source_intent_enabled", mock.Mock(return_value=True),
    )

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

        def run(self, coro, **_kwargs):
            return asyncio.run(coro)

    token = "b" * 64
    dispatcher = _FailClosedDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", dispatcher)
    monkeypatch.setattr(
        bluetooth_setup, "source_intent_enabled", mock.Mock(return_value=True),
    )

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
    assert json.loads(h.wfile.getvalue()) == {"error": "invalid mac"}
    assert fake.run_calls == 0
    assert fake.engine.calls == []
    pair_start.assert_not_called()


@pytest.mark.parametrize(
    ("path", "body"),
    (
        ("/power", {"on": True}),
        ("/discoverable", {"on": True}),
        ("/scan", {"action": "start"}),
        ("/pair", {"mac": "AA:BB:CC:DD:EE:FF"}),
        ("/connect", {"mac": "AA:BB:CC:DD:EE:FF"}),
    ),
)
def test_unavailable_adapter_blocks_only_radio_activation(monkeypatch, path, body):
    token = "a" * 64
    fake = _FakeDispatcher()
    pair_start = mock.Mock()
    request_intent = mock.Mock()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "bonded_follower_active", lambda: False)
    monkeypatch.setattr(bluetooth_setup, "_start_pair_stream", pair_start)
    monkeypatch.setattr(bluetooth_setup, "request_source_intent", request_intent)
    monkeypatch.setattr(
        bluetooth_setup, "source_intent_enabled", mock.Mock(return_value=True),
    )
    monkeypatch.setattr(
        bluetooth_setup,
        "probe_bluetooth_availability",
        lambda _unit_probe: _availability(
            available=False,
            missing_units=("bluealsa.service",),
        ),
    )
    h = _make_request(
        path,
        body=json.dumps(body).encode(),
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )

    h.do_POST()

    assert h.status == int(http.HTTPStatus.CONFLICT)
    assert "bluealsa.service" in json.loads(h.wfile.getvalue())["error"]
    assert fake.run_calls == 0
    assert fake.engine.calls == []
    pair_start.assert_not_called()
    request_intent.assert_not_called()


@pytest.mark.parametrize(
    ("path", "body"),
    (
        ("/power", {"on": False}),
        ("/discoverable", {"on": False}),
        ("/scan", {"action": "stop"}),
        ("/disconnect", {"mac": "AA:BB:CC:DD:EE:FF"}),
        ("/forget", {"mac": "AA:BB:CC:DD:EE:FF"}),
    ),
)
def test_unavailable_adapter_still_allows_shutdown_and_cleanup(
    monkeypatch,
    path,
    body,
):
    token = "c" * 64
    fake = _FakeDispatcher()
    request_intent = mock.Mock()

    async def setter(_on):
        return None

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "bonded_follower_active", lambda: False)
    monkeypatch.setattr(bluetooth_setup, "request_source_intent", request_intent)
    monkeypatch.setattr(bluetooth_setup, "set_discoverable", setter)
    monkeypatch.setattr(
        bluetooth_setup,
        "source_intent_enabled",
        mock.Mock(side_effect=AssertionError("cleanup read source intent")),
    )
    monkeypatch.setattr(
        bluetooth_setup,
        "probe_bluetooth_availability",
        mock.Mock(side_effect=AssertionError("cleanup probed availability")),
    )
    h = _make_request(
        path,
        body=json.dumps(body).encode(),
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )

    h.do_POST()

    assert h.status == int(http.HTTPStatus.OK)
    if path == "/power":
        request_intent.assert_called_once_with(bluetooth_setup.Source.BLUETOOTH, False)
    else:
        request_intent.assert_not_called()


@pytest.mark.parametrize("value", (False, True))
def test_power_route_delegates_exact_boolean_to_shared_source_intent(
    monkeypatch,
    value,
):
    token = "q" * 64
    request_intent = mock.Mock()
    monkeypatch.setattr(bluetooth_setup, "request_source_intent", request_intent)
    monkeypatch.setattr(bluetooth_setup, "bonded_follower_active", lambda: False)
    # A power-intent write must not depend on BlueZ or the adapter dispatcher.
    monkeypatch.setattr(
        bluetooth_setup,
        "_dispatch",
        mock.Mock(side_effect=AssertionError("power route touched dispatcher")),
    )
    h = _make_request(
        "/power",
        body=json.dumps({"on": value}).encode(),
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )

    h.do_POST()

    assert h.status == int(http.HTTPStatus.OK)
    assert json.loads(h.wfile.getvalue()) == {"ok": True, "desired": value}
    request_intent.assert_called_once_with(bluetooth_setup.Source.BLUETOOTH, value)


@pytest.mark.parametrize("value", (False, True))
def test_discoverable_route_passes_exact_boolean_to_async_adapter_setter(
    monkeypatch,
    value,
):
    token = "q" * 64
    calls = []
    fake = _FakeDispatcher()

    async def setter(on):
        calls.append(on)

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "bonded_follower_active", lambda: False)
    monkeypatch.setattr(bluetooth_setup, "set_discoverable", setter)
    if value:
        monkeypatch.setattr(
            bluetooth_setup,
            "source_intent_enabled",
            mock.Mock(return_value=True),
        )
    h = _make_request(
        "/discoverable",
        body=json.dumps({"on": value}).encode(),
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )

    h.do_POST()

    assert h.status == int(http.HTTPStatus.OK)
    assert calls == [value]
    assert fake.run_calls == 1


@pytest.mark.parametrize(
    ("desired", "powered", "effective"),
    (
        (False, False, "off"),
        (True, True, "on"),
        (True, False, "degraded"),
        (False, True, "degraded"),
    ),
)
def test_get_state_exposes_desired_and_effective_source_state(
    monkeypatch,
    desired,
    powered,
    effective,
):
    fake = _FakeDispatcher()

    async def read_adapter_state():
        return {
            "adapter": "hci0",
            "powered": powered,
            "discoverable": False,
            "discovering": False,
        }

    intent_reads = mock.Mock(return_value=desired)
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "adapter_state", read_adapter_state)
    monkeypatch.setattr(bluetooth_setup, "source_intent_enabled", intent_reads)
    monkeypatch.setattr(bluetooth_setup, "_unit_active", lambda _unit: desired)
    monkeypatch.setattr(
        bluetooth_setup,
        "probe_bluetooth_availability",
        lambda _unit_probe: _availability(
            any_soft_blocked=not desired,
            all_soft_blocked=not desired,
        ),
    )
    h = _make_request("/state")

    h.do_GET()

    assert h.status == int(http.HTTPStatus.OK)
    payload = json.loads(h.wfile.getvalue())
    assert payload["powered"] is powered
    assert payload["desired"] is desired
    assert payload["effective"] == effective
    intent_reads.assert_called_once_with(bluetooth_setup.Source.BLUETOOTH)
    assert fake.run_calls == 1


def test_get_state_uses_one_batched_systemd_snapshot(monkeypatch):
    fake = _FakeDispatcher()

    async def read_adapter_state():
        return {
            "adapter": "hci0",
            "powered": False,
            "discoverable": False,
            "discovering": False,
        }

    class _Snapshot:
        @staticmethod
        def available(_unit):
            return True

        @staticmethod
        def active(_unit):
            return False

    probe = mock.Mock(return_value=_Snapshot())
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "adapter_state", read_adapter_state)
    monkeypatch.setattr(
        bluetooth_setup, "source_intent_enabled", mock.Mock(return_value=False),
    )
    monkeypatch.setattr(bluetooth_setup, "probe_unit_snapshot", probe)

    h = _make_request("/state")
    h.do_GET()

    assert h.status == int(http.HTTPStatus.OK)
    probe.assert_called_once_with(bluetooth_setup._STATE_UNITS)


def test_get_state_is_not_on_while_pairing_agent_is_inactive(monkeypatch):
    fake = _FakeDispatcher()

    async def read_adapter_state():
        return {
            "adapter": "hci0",
            "powered": True,
            "discoverable": False,
            "discovering": False,
        }

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "adapter_state", read_adapter_state)
    monkeypatch.setattr(
        bluetooth_setup, "source_intent_enabled", mock.Mock(return_value=True),
    )
    monkeypatch.setattr(
        bluetooth_setup,
        "_unit_active",
        lambda unit: unit != "bt-agent.service",
    )

    state, status = bluetooth_setup._bluetooth_state_snapshot()

    assert status == int(http.HTTPStatus.OK)
    assert state["available"] is True
    assert state["effective"] == "degraded"
    assert "bt-agent.service" in state["degradedReason"]


@pytest.mark.parametrize(
    ("desired", "effective"),
    ((False, "off"), (True, "degraded")),
)
def test_get_state_preserves_desired_intent_when_adapter_read_fails(
    monkeypatch,
    desired,
    effective,
):
    fake = _FakeDispatcher()

    async def failing_adapter_state():
        raise RuntimeError("BlueZ unavailable")

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "adapter_state", failing_adapter_state)
    monkeypatch.setattr(
        bluetooth_setup,
        "source_intent_enabled",
        mock.Mock(return_value=desired),
    )
    monkeypatch.setattr(bluetooth_setup, "_unit_active", lambda _unit: desired)
    monkeypatch.setattr(
        bluetooth_setup,
        "probe_bluetooth_availability",
        lambda _unit_probe: _availability(
            any_soft_blocked=not desired,
            all_soft_blocked=not desired,
        ),
    )
    h = _make_request("/state")

    h.do_GET()

    assert h.status == int(http.HTTPStatus.OK)
    expected = {
        "error": "BlueZ unavailable",
        "powered": False,
        "desired": desired,
        "effective": effective,
        "available": True,
        "parked": False,
        "discoverable": False,
        "discovering": False,
    }
    if desired:
        expected["degradedReason"] = "BlueZ reports the adapter powered off"
    assert json.loads(h.wfile.getvalue()) == expected
    assert fake.run_calls == 1


def test_state_reports_parked_without_rewriting_desired(monkeypatch):
    fake = _FakeDispatcher()

    async def read_adapter_state():
        return {
            "adapter": "hci0",
            "powered": False,
            "discoverable": False,
            "discovering": False,
        }

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "adapter_state", read_adapter_state)
    monkeypatch.setattr(
        bluetooth_setup, "source_intent_enabled", mock.Mock(return_value=True),
    )
    monkeypatch.setattr(bluetooth_setup, "bonded_follower_active", lambda: True)
    monkeypatch.setattr(
        bluetooth_setup,
        "_unit_active",
        lambda _unit: pytest.fail("parked state must not probe source units"),
    )

    h = _make_request("/state")
    h.do_GET()

    payload = json.loads(h.wfile.getvalue())
    assert h.status == int(http.HTTPStatus.OK)
    assert payload["desired"] is True
    assert payload["effective"] == "parked"
    assert payload["parked"] is True


@pytest.mark.parametrize("desired", (False, True))
def test_state_preserves_desired_while_adapter_is_unavailable(monkeypatch, desired):
    fake = _FakeDispatcher()

    async def read_adapter_state():
        return {
            "adapter": "hci0",
            "powered": False,
            "discoverable": False,
            "discovering": False,
        }

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "adapter_state", read_adapter_state)
    monkeypatch.setattr(
        bluetooth_setup, "source_intent_enabled", mock.Mock(return_value=desired),
    )
    monkeypatch.setattr(bluetooth_setup, "_unit_active", lambda _unit: False)
    monkeypatch.setattr(
        bluetooth_setup,
        "probe_bluetooth_availability",
        lambda _unit_probe: _availability(
            available=False,
            radio_present=False,
        ),
    )

    state, status = bluetooth_setup._bluetooth_state_snapshot()

    assert status == int(http.HTTPStatus.OK)
    assert state["desired"] is desired
    assert state["available"] is False
    assert state["effective"] == "unavailable"
    assert state["unavailableReason"] == (
        "No Bluetooth adapter was detected on this device."
    )


def test_parked_state_takes_precedence_over_unavailable_hardware(monkeypatch):
    fake = _FakeDispatcher()

    async def read_adapter_state():
        return {"powered": False, "discoverable": False, "discovering": False}

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "adapter_state", read_adapter_state)
    monkeypatch.setattr(
        bluetooth_setup, "source_intent_enabled", mock.Mock(return_value=True),
    )
    monkeypatch.setattr(bluetooth_setup, "bonded_follower_active", lambda: True)
    monkeypatch.setattr(
        bluetooth_setup,
        "probe_bluetooth_availability",
        lambda _unit_probe: _availability(available=False, radio_present=False),
    )

    state, status = bluetooth_setup._bluetooth_state_snapshot()

    assert status == int(http.HTTPStatus.OK)
    assert state["effective"] == "parked"
    assert state["available"] is False


@pytest.mark.parametrize("path", ("/power", "/scan"))
def test_mutation_is_rejected_while_bonded_follower(monkeypatch, path):
    token = "f" * 64
    request_intent = mock.Mock()
    monkeypatch.setattr(bluetooth_setup, "bonded_follower_active", lambda: True)
    monkeypatch.setattr(bluetooth_setup, "request_source_intent", request_intent)
    body = b'{"on":true}' if path == "/power" else b'{"action":"start"}'
    h = _make_request(
        path,
        body=body,
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )

    h.do_POST()

    assert h.status == int(http.HTTPStatus.CONFLICT)
    assert "stereo pair" in json.loads(h.wfile.getvalue())["error"]
    request_intent.assert_not_called()


def test_failed_power_apply_returns_durable_intent_readback(monkeypatch):
    token = "r" * 64
    fake = _FakeDispatcher()

    async def read_adapter_state():
        return {
            "adapter": "hci0",
            "powered": False,
            "discoverable": False,
            "discovering": False,
        }

    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(bluetooth_setup, "adapter_state", read_adapter_state)
    monkeypatch.setattr(bluetooth_setup, "bonded_follower_active", lambda: False)
    monkeypatch.setattr(
        bluetooth_setup,
        "request_source_intent",
        mock.Mock(side_effect=RuntimeError("reconcile failed after write")),
    )
    monkeypatch.setattr(
        bluetooth_setup, "source_intent_enabled", mock.Mock(return_value=True),
    )
    monkeypatch.setattr(bluetooth_setup, "_unit_active", lambda _unit: False)
    h = _make_request(
        "/power",
        body=b'{"on":true}',
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )

    h.do_POST()

    payload = json.loads(h.wfile.getvalue())
    assert h.status == int(http.HTTPStatus.BAD_GATEWAY)
    assert payload["error"] == "reconcile failed after write"
    assert payload["state"]["desired"] is True
    assert payload["state"]["effective"] == "degraded"


def test_failed_power_apply_returns_unknown_state_without_old_value_rollback(
    monkeypatch,
):
    token = "u" * 64
    monkeypatch.setattr(bluetooth_setup, "bonded_follower_active", lambda: False)
    monkeypatch.setattr(
        bluetooth_setup,
        "request_source_intent",
        mock.Mock(side_effect=RuntimeError("reconcile failed after write")),
    )
    monkeypatch.setattr(
        bluetooth_setup,
        "source_intent_enabled",
        mock.Mock(side_effect=RuntimeError("intent readback unavailable")),
    )
    h = _make_request(
        "/power",
        body=b'{"on":true}',
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )

    h.do_POST()

    payload = json.loads(h.wfile.getvalue())
    assert h.status == int(http.HTTPStatus.BAD_GATEWAY)
    assert payload["state"]["available"] is False
    assert payload["state"]["effective"] == "unavailable"


def test_state_is_unavailable_when_intent_cannot_be_read(monkeypatch):
    monkeypatch.setattr(
        bluetooth_setup,
        "source_intent_enabled",
        mock.Mock(side_effect=RuntimeError("invalid intent file")),
    )
    monkeypatch.setattr(
        bluetooth_setup,
        "_dispatch",
        mock.Mock(side_effect=AssertionError("unavailable state touched BlueZ")),
    )
    h = _make_request("/state")

    h.do_GET()

    assert h.status == int(http.HTTPStatus.BAD_GATEWAY)
    payload = json.loads(h.wfile.getvalue())
    assert payload["available"] is False
    assert payload["effective"] == "unavailable"


def test_post_connect_drives_engine(monkeypatch):
    token = "y" * 64
    fake = _FakeDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(
        bluetooth_setup, "source_intent_enabled", mock.Mock(return_value=True),
    )

    h = _make_request(
        "/connect",
        body=b'{"mac": "AA:BB:CC:DD:EE:FF"}',
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )
    h.do_POST()
    assert h.status == 200
    assert ("connect", "AA:BB:CC:DD:EE:FF") in fake.engine.calls


def test_post_connect_normalizes_mac_before_engine_call(monkeypatch):
    token = "n" * 64
    fake = _FakeDispatcher()
    monkeypatch.setattr(bluetooth_setup, "DISPATCH", fake)
    monkeypatch.setattr(
        bluetooth_setup, "source_intent_enabled", mock.Mock(return_value=True),
    )
    h = _make_request(
        "/connect",
        body=b'{"mac": "  aa:bb:cc:dd:ee:ff  "}',
        cookies="jts_csrf=" + token,
        csrf_header=token,
    )

    h.do_POST()

    assert h.status == int(http.HTTPStatus.OK)
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
