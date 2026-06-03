"""/bluetooth/ — generic Bluetooth control panel.

Phone-Settings-style page: live device list, pair anything, no
per-device-class wizards. Backed by `jasper.bluetooth.BluetoothEngine`.

Routes (nginx strips /bluetooth/):
  GET  /                       landing HTML
  GET  /state                  adapter state JSON
  GET  /devices/stream         SSE: device add/update/remove
  POST /scan                   {"action": "start"|"stop"}
  POST /power                  {"on": bool}
  POST /discoverable           {"on": bool}
  POST /pair                   {"mac": "..."} — returns {ok: true}
  GET  /pair/<mac>/stream      SSE: pair-flow status events
  POST /connect                {"mac": "..."}
  POST /disconnect             {"mac": "..."}
  POST /forget                 {"mac": "..."}

Stack: stdlib http.server (ThreadingHTTPServer) — same shape as the
sibling spotify_setup / voice_setup / dial_setup wizards. One thread
per request; the engine itself owns one event loop in the dispatcher
thread.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
from concurrent.futures import Future
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from ._common import (
    begin_request,
    canonical_header,
    canonical_page,
    reject_csrf,
    send_html_response,
    toggle_html,
    verify_csrf,
)
from ..bluetooth.adapter import (
    DISCOVERABLE_AUTO_OFF_SEC,
    set_discoverable,
    set_powered,
    state as adapter_state,
)
from ..bluetooth.engine import BluetoothEngine

# Default scan duration when the user clicks Scan. Server-side
# enforced — even if the user closes the tab the scan auto-stops.
# Long enough to catch slow-advertising devices (knobs are ~1-2 s
# per advertisement), short enough not to leave the radio hot.
SCAN_DURATION_SEC = 30.0

logger = logging.getLogger(__name__)


# ============================================================
# Dispatcher — one asyncio loop on a background thread
# ============================================================


class _AsyncDispatcher:
    """Runs an asyncio event loop on a dedicated thread. The HTTP
    handlers (which are sync) submit coroutines via `run()` and
    block on the result. The engine and observer live on this loop,
    so they share one bus connection and one set of signal
    subscriptions across the whole daemon.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._engine = BluetoothEngine()
        self._thread = threading.Thread(
            target=self._run, name="bluetooth-loop", daemon=True,
        )
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=10)
        if self._loop is None:
            raise RuntimeError("dispatcher loop failed to start")
        # Engine bootstrap on the loop.
        self.run(self._engine.start())

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    def run(self, coro):
        """Submit a coroutine to the loop and wait for the result.
        Used from sync HTTP handler threads."""
        if self._loop is None:
            raise RuntimeError("dispatcher not started")
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    def stream(self, coro_gen):
        """Submit an async generator and yield its items synchronously.
        Used for SSE endpoints — the HTTP handler thread iterates
        this and writes each item to the wire."""
        if self._loop is None:
            raise RuntimeError("dispatcher not started")
        q: "asyncio.Queue[tuple[str, Any]]" = asyncio.Queue()
        SENTINEL = ("done", None)

        async def _drive():
            try:
                async for item in coro_gen:
                    await q.put(("item", item))
            except Exception as e:  # noqa: BLE001
                await q.put(("error", e))
            finally:
                await q.put(SENTINEL)

        asyncio.run_coroutine_threadsafe(_drive(), self._loop)

        while True:
            fut: Future = asyncio.run_coroutine_threadsafe(q.get(), self._loop)
            kind, payload = fut.result()
            if kind == "item":
                yield payload
            elif kind == "error":
                raise payload  # type: ignore[misc]
            else:
                return

    @property
    def engine(self) -> BluetoothEngine:
        return self._engine


# Module-level singleton populated in `main()`.
DISPATCH: _AsyncDispatcher | None = None


def _dispatch() -> _AsyncDispatcher:
    if DISPATCH is None:
        raise RuntimeError("bluetooth dispatcher not initialised")
    return DISPATCH


# ============================================================
# HTML
# ============================================================

# Page-specific stylesheet served static from /assets/ (the device list,
# streaming pair card, adapter-toggle rows, and device glyphs that the shared
# app.css doesn't cover). Shared chrome — .app-header, .page, .section,
# .info-card, .btn--*, .toggle, .banner, the icon sprite — comes from app.css.
PAGE_CSS_HREF = "/assets/bluetooth/bluetooth.css"


def _landing_html(csrf_token: str = "") -> bytes:
    # Server-rendered chrome only — the live behaviour (adapter state polling,
    # device list, the full streaming pair flow) lives in the ES module
    # /assets/bluetooth/js/main.js, which hydrates these scaffolds. The toggles
    # use the shared toggle_html() helper + the canonical .toggle CSS; the
    # device-list/pair-card visuals come from bluetooth.css. Untrusted device
    # names never touch this server template — the module escapes them on the
    # client (see main.js escapeHtml / data-* delegated handler).
    body = f"""
{canonical_header("Bluetooth")}
<main class="page">
  <p class="bt-intro">Pair phones for Bluetooth speaker playback, plus volume
  knobs and other no-code Bluetooth accessories.</p>

  <section class="section">
    <div class="info-card">
      <div class="toggle-row">
        <div>
          <label class="label" for="sw-power">Bluetooth</label>
          <div class="hint" id="bt-hint">Loading…</div>
        </div>
        {toggle_html("sw-power", disabled=True)}
      </div>
      <div class="toggle-row">
        <div>
          <label class="label" for="sw-disc">Pairing mode</label>
          <div class="hint" id="disc-hint">
            While on, nearby devices can see and pair with JTS.
            No code required. Auto-turns off after 5&nbsp;min.
          </div>
        </div>
        {toggle_html("sw-disc", disabled=True)}
      </div>
    </div>
  </section>

  <section class="section">
    <h2 class="eyebrow">My devices</h2>
    <div class="device-list" id="paired-list">
      <div class="empty">Loading…</div>
    </div>
  </section>

  <section class="section">
    <div class="bt-section-head">
      <h2 class="eyebrow">Other devices</h2>
      <button id="scan-btn" class="btn btn--ghost" type="button">Scan</button>
    </div>
    <div class="device-list" id="other-list">
      <div class="empty">Nothing nearby. Try scanning.</div>
    </div>
  </section>

  <p class="bt-footnote">
  Already paired devices stay paired even when Bluetooth is off; turning
  it back on lets them reconnect. Forget a device to wipe its pair record.
  </p>
</main>
<script type="module" src="/assets/bluetooth/js/main.js"></script>
"""
    return canonical_page(
        "Bluetooth", body, csrf_token=csrf_token, page_css_href=PAGE_CSS_HREF,
    )


# ============================================================
# HTTP handler
# ============================================================


def _make_handler() -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self._send(status, body, "application/json")

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 1_000_000:
                return {}
            try:
                raw = self.rfile.read(length)
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                return {}

        def _begin_sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            # nginx: disable response buffering for this location.
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

        def _sse_write(self, payload: dict) -> bool:
            try:
                self.wfile.write(
                    f"data: {json.dumps(payload)}\n\n".encode("utf-8"),
                )
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        # ---------- routes ----------

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/":
                ctx = begin_request(self)
                self._send_html(_landing_html(ctx["csrf_token"]))
                return
            if path == "/state":
                try:
                    st = _dispatch().run(adapter_state())
                except Exception as e:  # noqa: BLE001
                    self._send_json(
                        {"error": str(e), "powered": False,
                         "discoverable": False},
                        status=502,
                    )
                    return
                self._send_json(st)
                return
            if path == "/devices/stream":
                self._stream_devices()
                return
            if path.startswith("/pair/") and path.endswith("/stream"):
                mac = path[len("/pair/"):-len("/stream")]
                self._stream_pair(mac)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if not (
                path in {
                    "/power", "/discoverable", "/scan", "/pair",
                    "/connect", "/disconnect", "/forget",
                }
            ):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not verify_csrf(self):
                reject_csrf(self)
                return
            body = self._read_json()
            try:
                if path == "/power":
                    on = bool(body.get("on"))
                    _dispatch().run(set_powered(on))
                    self._send_json({"ok": True})
                    return
                if path == "/discoverable":
                    on = bool(body.get("on"))
                    _dispatch().run(set_discoverable(on))
                    self._send_json({"ok": True})
                    return
                if path == "/scan":
                    action = (body.get("action") or "").strip()
                    if action == "start":
                        # Engine owns discovery so it stays on our
                        # long-lived bus (bluez auto-stops when the
                        # client disconnects — a short-lived helper
                        # would lose the scan instantly).
                        _dispatch().run(
                            _dispatch().engine.start_discovery(
                                duration_s=SCAN_DURATION_SEC,
                            ),
                        )
                        self._send_json(
                            {"ok": True,
                             "duration_s": SCAN_DURATION_SEC},
                        )
                        return
                    if action == "stop":
                        _dispatch().run(
                            _dispatch().engine.stop_discovery(),
                        )
                        self._send_json({"ok": True})
                        return
                    self._send_json(
                        {"error": "action must be start or stop"},
                        status=400,
                    )
                    return
                if path == "/pair":
                    mac = (body.get("mac") or "").strip()
                    if not mac:
                        self._send_json({"error": "missing mac"}, status=400)
                        return
                    # Pair is fully streaming — return ok now; the
                    # client opens /pair/<mac>/stream to consume events.
                    # Server-side: kick off the pair coroutine on the
                    # dispatcher loop and stash the generator so the
                    # subsequent /stream request can consume it.
                    _start_pair_stream(mac)
                    self._send_json({"ok": True})
                    return
                if path == "/connect":
                    mac = (body.get("mac") or "").strip()
                    ok, msg = _dispatch().run(
                        _dispatch().engine.connect(mac),
                    )
                    if not ok:
                        self._send_json({"error": msg}, status=502)
                        return
                    self._send_json({"ok": True, "message": msg})
                    return
                if path == "/disconnect":
                    mac = (body.get("mac") or "").strip()
                    ok, msg = _dispatch().run(
                        _dispatch().engine.disconnect(mac),
                    )
                    if not ok:
                        self._send_json({"error": msg}, status=502)
                        return
                    self._send_json({"ok": True, "message": msg})
                    return
                if path == "/forget":
                    mac = (body.get("mac") or "").strip()
                    ok, msg = _dispatch().run(
                        _dispatch().engine.forget(mac),
                    )
                    if not ok:
                        self._send_json({"error": msg}, status=502)
                        return
                    self._send_json({"ok": True, "message": msg})
                    return
            except Exception as e:  # noqa: BLE001
                logger.exception("POST %s failed", path)
                self._send_json({"error": str(e)}, status=502)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        # ---------- SSE streams ----------

        def _stream_devices(self) -> None:
            self._begin_sse()

            async def _drive():
                engine = _dispatch().engine
                async with await engine.observer.subscribe() as sub:
                    async for action, device in sub.events():
                        yield {
                            "action": action,
                            "device": device.to_json(),
                        }

            try:
                for event in _dispatch().stream(_drive()):
                    if not self._sse_write(event):
                        return
            except Exception as e:  # noqa: BLE001
                logger.exception("device stream failed")
                self._sse_write({"action": "error", "message": str(e)})

        def _stream_pair(self, mac: str) -> None:
            self._begin_sse()
            try:
                for event in _consume_pair_stream(mac):
                    if not self._sse_write(event):
                        return
            except Exception as e:  # noqa: BLE001
                logger.exception("pair stream failed")
                self._sse_write({"stage": "error", "message": str(e)})

    return Handler


# ============================================================
# Pair stream coordination
# ============================================================


# In-flight pair attempts keyed by uppercase MAC. Each entry is an
# asyncio.Queue produced by `_pair_driver` running on the dispatcher
# loop. The /stream handler consumes from the queue via the dispatcher.
_PAIR_STREAMS: dict[str, "asyncio.Queue[dict | None]"] = {}
_PAIR_STREAMS_LOCK = threading.Lock()


def _start_pair_stream(mac: str) -> None:
    """Kick off a pair coroutine on the dispatcher loop. Events flow
    into a queue keyed by MAC; the /stream handler drains it."""
    mac_u = mac.upper()
    dispatcher = _dispatch()
    loop = dispatcher._loop  # noqa: SLF001 — single owner
    if loop is None:
        return

    async def _drive() -> None:
        q: asyncio.Queue = asyncio.Queue()
        with _PAIR_STREAMS_LOCK:
            _PAIR_STREAMS[mac_u] = q
        try:
            async for event in dispatcher.engine.pair(mac_u):
                await q.put(event)
        except Exception as e:  # noqa: BLE001
            await q.put({"stage": "error", "message": str(e)})
        finally:
            await q.put(None)  # sentinel
            # Don't pop from _PAIR_STREAMS yet — the consumer may not
            # have started reading yet. Cleanup happens in the
            # consumer's finally block.

    asyncio.run_coroutine_threadsafe(_drive(), loop)


def _consume_pair_stream(mac: str):
    """Generator that drains the queue for `mac` on the dispatcher
    loop and yields events to the HTTP handler thread."""
    mac_u = mac.upper()
    dispatcher = _dispatch()
    loop = dispatcher._loop  # noqa: SLF001
    if loop is None:
        return
    with _PAIR_STREAMS_LOCK:
        q = _PAIR_STREAMS.get(mac_u)
    if q is None:
        # No pair attempt is in flight for this MAC (the user may
        # have hit the stream URL without POSTing /pair first).
        yield {"stage": "error", "message": "no pair attempt in flight"}
        return
    try:
        while True:
            fut: Future = asyncio.run_coroutine_threadsafe(q.get(), loop)
            item = fut.result()
            if item is None:
                return
            yield item
    finally:
        with _PAIR_STREAMS_LOCK:
            # Only clean up if the queue's now empty (sentinel drained).
            if _PAIR_STREAMS.get(mac_u) is q and q.empty():
                _PAIR_STREAMS.pop(mac_u, None)


# ============================================================
# Entry point
# ============================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-bluetooth-web",
        description="Generic Bluetooth control panel at /bluetooth/",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("JASPER_BLUETOOTH_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_BLUETOOTH_WEB_PORT", "8769")),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    global DISPATCH
    DISPATCH = _AsyncDispatcher()
    DISPATCH.start()

    # When socket-activated by systemd, adopt the inherited listener
    # instead of binding fresh. Direct CLI invocation falls through.
    from . import _systemd
    sockets = _systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)

    handler_cls = _make_handler()
    server = _systemd.make_http_server(target, handler_cls)

    # Idle-exit after 10 min of no requests so the resident set goes
    # to zero between admin sessions. ~17 MB Pss savings when idle.
    tracker = _systemd.IdleShutdownTracker()
    _systemd.install_request_idle_bump(handler_cls, tracker)
    tracker.start()

    if sockets:
        logger.info(
            "jasper-bluetooth-web adopting systemd fd (pairing mode "
            "auto-off after %ds when toggled on)",
            DISCOVERABLE_AUTO_OFF_SEC,
        )
    else:
        logger.info(
            "jasper-bluetooth-web listening on http://%s:%d (pairing mode "
            "auto-off after %ds when toggled on)",
            args.host, args.port, DISCOVERABLE_AUTO_OFF_SEC,
        )

    _systemd.notify_ready()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _systemd.notify_stopping()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
