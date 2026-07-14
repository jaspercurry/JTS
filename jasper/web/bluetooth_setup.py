# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""/bluetooth/ — generic Bluetooth control panel.

Phone-Settings-style page: live device list, pair anything, no
per-device-class wizards. Backed by `jasper.bluetooth.BluetoothEngine`.

Routes (nginx strips /bluetooth/):
  GET  /                       landing HTML
  GET  /state                  adapter state JSON
  GET  /devices/stream         SSE: device add/update/remove
  POST /scan                   {"action": "start"|"stop"}
  POST /power                  {"on": bool} — shared persisted source intent
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
import re
import subprocess
import threading
import time
import urllib.parse
from concurrent.futures import Future
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from dbus_next.errors import DBusError  # type: ignore

from ..bluetooth.availability import (
    BLUETOOTH_CONTROL_PLANE_UNIT,
    BluetoothAvailability,
    bluetooth_unavailable_reason,
    probe_bluetooth_availability,
)
from ._common import (
    JsonBodyError,
    begin_request,
    bonded_follower_active,
    canonical_header,
    canonical_page,
    guard_mutating_request,
    guard_read_request,
    read_json_object,
    reject_csrf,
    send_html_response,
    send_json_response,
    toggle_html,
)
from ._unit_snapshot import UnitSnapshot, probe_unit_snapshot
from ..bluetooth.adapter import (
    DISCOVERABLE_AUTO_OFF_SEC,
    set_discoverable,
    state as adapter_state,
)
from ..bluetooth.engine import BluetoothEngine
from ..log_event import log_event
from ..local_sources import local_source_lifecycle
from ..music_sources import Source
from ..source_intent import (
    request_source_intent,
    source_intent_enabled,
)

# Default scan duration when the user clicks Scan. Server-side
# enforced — even if the user closes the tab the scan auto-stops.
# Long enough to catch slow-advertising devices (knobs are ~1-2 s
# per advertisement), short enough not to leave the radio hot.
SCAN_DURATION_SEC = 30.0
STATE_PROBE_TIMEOUT_SEC = 5.0
MUTATION_TIMEOUT_SEC = 35.0
_MAC_ADDRESS_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
PAIR_STREAM_TTL_SEC = 120.0

logger = logging.getLogger(__name__)

_BLUETOOTH_LIFECYCLE = local_source_lifecycle(Source.BLUETOOTH)
_STATE_UNITS = tuple(dict.fromkeys((
    BLUETOOTH_CONTROL_PLANE_UNIT,
    *_BLUETOOTH_LIFECYCLE.runtime_units,
)))


def _normalize_mac(value: object, *, url_encoded: bool = False) -> str | None:
    """Validate one public MAC value and return its canonical spelling."""
    if not isinstance(value, str):
        return None
    candidate = value
    if url_encoded:
        # A path segment must stay one segment before and after decoding.
        if "/" in candidate or "\\" in candidate:
            return None
        try:
            candidate = urllib.parse.unquote(candidate, errors="strict")
        except UnicodeDecodeError:
            return None
    else:
        candidate = candidate.strip()
    if not _MAC_ADDRESS_RE.fullmatch(candidate):
        return None
    return candidate.upper()


def _unit_active(unit: str) -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            check=False,
            timeout=5,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "active"


def _unit_available(unit: str) -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "show", unit, "-p", "LoadState", "--value"],
            check=False,
            timeout=STATE_PROBE_TIMEOUT_SEC,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "loaded"


def _effective_bluetooth_state(
    *,
    desired: bool,
    powered: bool,
    parked: bool = False,
    availability: BluetoothAvailability | None = None,
    unit_snapshot: UnitSnapshot | None = None,
) -> tuple[str, str]:
    """Compare desired intent with radio and source-resource truth."""
    if parked:
        return "parked", ""
    active_probe = unit_snapshot.active if unit_snapshot else _unit_active
    active = {
        unit: active_probe(unit) for unit in _BLUETOOTH_LIFECYCLE.runtime_units
    }
    reasons: list[str] = []
    unit_available = unit_snapshot.available if unit_snapshot else _unit_available
    hardware = availability or probe_bluetooth_availability(unit_available)
    if hardware.error:
        reasons.append(f"Bluetooth availability probe is incomplete: {hardware.error}")
    any_soft_blocked = hardware.any_soft_blocked
    fully_soft_blocked = hardware.all_soft_blocked
    if desired:
        if any_soft_blocked is True:
            reasons.append("the radio is RF-killed")
        if not powered:
            reasons.append("BlueZ reports the adapter powered off")
        inactive = [unit for unit, running in active.items() if not running]
        if inactive:
            reasons.append(f"required services are inactive: {', '.join(inactive)}")
        effective = "on" if not reasons else "degraded"
    else:
        still_active = [unit for unit, running in active.items() if running]
        if still_active:
            reasons.append(f"services are still active: {', '.join(still_active)}")
        if powered:
            reasons.append("BlueZ still reports the adapter powered on")
        if fully_soft_blocked is False:
            reasons.append("the radio is not RF-killed")
        effective = "off" if not reasons else "degraded"
    return effective, "; ".join(reasons)


def _bluetooth_state_snapshot() -> tuple[dict[str, Any], int]:
    """Return one desired/effective snapshot and its HTTP status.

    Intent is the authoritative switch value. Adapter read failures remain a
    successful, degraded snapshot when intent is readable; an invalid intent
    file is unavailable and returns 502 so clients cannot render a guessed Off.
    """
    try:
        desired = source_intent_enabled(Source.BLUETOOTH)
    except RuntimeError as exc:
        return ({
            "error": str(exc),
            "powered": False,
            "desired": False,
            "effective": "unavailable",
            "available": False,
            "parked": False,
            "discoverable": False,
            "discovering": False,
        }, HTTPStatus.BAD_GATEWAY)

    parked = bonded_follower_active()
    unit_snapshot = probe_unit_snapshot(_STATE_UNITS)
    availability = probe_bluetooth_availability(unit_snapshot.available)
    try:
        raw = _dispatch().run(
            adapter_state(),
            timeout_sec=STATE_PROBE_TIMEOUT_SEC,
        )
    except (DBusError, OSError, RuntimeError, asyncio.TimeoutError) as exc:
        effective, degraded_reason = _effective_bluetooth_state(
            desired=desired,
            powered=False,
            parked=parked,
            availability=availability,
            unit_snapshot=unit_snapshot,
        )
        if not availability.available and not parked:
            effective = "unavailable"
        payload: dict[str, Any] = {
            "error": str(exc),
            "powered": False,
            "desired": desired,
            "effective": effective,
            "available": availability.available,
            "parked": parked,
            "discoverable": False,
            "discovering": False,
        }
        if degraded_reason:
            payload["degradedReason"] = degraded_reason
        if not availability.available:
            payload["unavailableReason"] = bluetooth_unavailable_reason(availability)
        return payload, HTTPStatus.OK

    state = dict(raw)
    powered = bool(state.get("powered", False))
    state["desired"] = desired
    state["available"] = availability.available
    state["parked"] = parked
    effective, degraded_reason = _effective_bluetooth_state(
        desired=desired,
        powered=powered,
        parked=parked,
        availability=availability,
        unit_snapshot=unit_snapshot,
    )
    if not availability.available and not parked:
        effective = "unavailable"
    if not availability.available:
        state["unavailableReason"] = bluetooth_unavailable_reason(availability)
    state["effective"] = effective
    if degraded_reason:
        state["degradedReason"] = degraded_reason
    else:
        state.pop("degradedReason", None)
    return state, HTTPStatus.OK


# ============================================================
# Dispatcher — one asyncio loop on a background thread
# ============================================================


def _close_awaitable(awaitable: Any) -> None:
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()


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

    def run(self, coro, *, timeout_sec: float | None = None):
        """Submit a coroutine to the loop and wait for the result.
        Used from sync HTTP handler threads."""
        if self._loop is None:
            _close_awaitable(coro)
            raise RuntimeError("dispatcher not started")
        try:
            fut: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except (RuntimeError, TypeError):
            # run_coroutine_threadsafe owns the coroutine only after a
            # successful submission. Close it when the loop rejects it.
            _close_awaitable(coro)
            raise
        try:
            return fut.result(timeout=timeout_sec)
        except TimeoutError:
            fut.cancel()
            raise

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

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            send_json_response(self, payload, status=status)

        def _read_json(self) -> dict[str, Any]:
            try:
                return read_json_object(self, max_bytes=1_000_000)
            except (JsonBodyError, OSError):
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
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                self._send_html(_landing_html(ctx["csrf_token"]))
                return
            if path == "/state":
                if not guard_read_request(self):
                    return
                state, status = _bluetooth_state_snapshot()
                self._send_json(state, status=status)
                return
            if path == "/devices/stream":
                if not guard_read_request(self):
                    return
                self._stream_devices()
                return
            if path.startswith("/pair/") and path.endswith("/stream"):
                if not guard_read_request(self):
                    return
                encoded_mac = path[len("/pair/"):-len("/stream")]
                mac = _normalize_mac(encoded_mac, url_encoded=True)
                if mac is None:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                self._stream_pair(mac)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path not in {
                    "/power", "/discoverable", "/scan", "/pair",
                    "/connect", "/disconnect", "/forget",
                }:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            body = self._read_json()
            if path in {"/power", "/discoverable"} and not isinstance(
                body.get("on"), bool,
            ):
                self._send_json(
                    {"error": "on must be true or false"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            mac = ""
            if path in {"/pair", "/connect", "/disconnect", "/forget"}:
                normalized_mac = _normalize_mac(body.get("mac"))
                if normalized_mac is None:
                    self._send_json({"error": "invalid mac"}, status=400)
                    return
                mac = normalized_mac
            if bonded_follower_active():
                # The canonical source coordinator parks every local Bluetooth
                # resource after grouping applies a bonded-follower role. A direct wizard request must
                # not temporarily restart, advertise, scan, or mutate it.
                self._send_json(
                    {"error": "Bluetooth is managed by the stereo pair "
                              "while this speaker is a follower — unpair "
                              "on /rooms/ to change it"},
                    status=HTTPStatus.CONFLICT,
                )
                return
            activates_radio = (
                (path == "/discoverable" and body.get("on") is True)
                or (path == "/scan" and body.get("action") == "start")
                or path in {"/pair", "/connect"}
            )
            if activates_radio:
                try:
                    bluetooth_desired = source_intent_enabled(Source.BLUETOOTH)
                except RuntimeError as exc:
                    self._send_json(
                        {"error": f"Bluetooth intent is unavailable: {exc}"},
                        status=HTTPStatus.BAD_GATEWAY,
                    )
                    return
                if not bluetooth_desired:
                    self._send_json(
                        {"error": "Bluetooth is turned off in Sources"},
                        status=HTTPStatus.CONFLICT,
                    )
                    return
            if (path == "/power" and body.get("on") is True) or activates_radio:
                availability = probe_bluetooth_availability(_unit_available)
                if not availability.available:
                    self._send_json(
                        {"error": bluetooth_unavailable_reason(availability)},
                        status=HTTPStatus.CONFLICT,
                    )
                    return
            try:
                if path == "/power":
                    on = body["on"]
                    request_source_intent(Source.BLUETOOTH, on)
                    self._send_json({"ok": True, "desired": on})
                    return
                if path == "/discoverable":
                    on = body["on"]
                    _dispatch().run(
                        set_discoverable(on),
                        timeout_sec=MUTATION_TIMEOUT_SEC,
                    )
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
                            timeout_sec=MUTATION_TIMEOUT_SEC,
                        )
                        self._send_json(
                            {"ok": True,
                             "duration_s": SCAN_DURATION_SEC},
                        )
                        return
                    if action == "stop":
                        _dispatch().run(
                            _dispatch().engine.stop_discovery(),
                            timeout_sec=MUTATION_TIMEOUT_SEC,
                        )
                        self._send_json({"ok": True})
                        return
                    self._send_json(
                        {"error": "action must be start or stop"},
                        status=400,
                    )
                    return
                if path == "/pair":
                    # Pair is fully streaming — return ok now; the
                    # client opens /pair/<mac>/stream to consume events.
                    # Server-side: kick off the pair coroutine on the
                    # dispatcher loop and stash the generator so the
                    # subsequent /stream request can consume it.
                    if not _start_pair_stream(mac):
                        self._send_json(
                            {"error": "pair attempt already in flight"},
                            status=HTTPStatus.CONFLICT,
                        )
                        return
                    self._send_json({"ok": True})
                    return
                if path == "/connect":
                    ok, msg = _dispatch().run(
                        _dispatch().engine.connect(mac),
                        timeout_sec=MUTATION_TIMEOUT_SEC,
                    )
                    if not ok:
                        self._send_json({"error": msg}, status=502)
                        return
                    self._send_json({"ok": True, "message": msg})
                    return
                if path == "/disconnect":
                    ok, msg = _dispatch().run(
                        _dispatch().engine.disconnect(mac),
                        timeout_sec=MUTATION_TIMEOUT_SEC,
                    )
                    if not ok:
                        self._send_json({"error": msg}, status=502)
                        return
                    self._send_json({"ok": True, "message": msg})
                    return
                if path == "/forget":
                    ok, msg = _dispatch().run(
                        _dispatch().engine.forget(mac),
                        timeout_sec=MUTATION_TIMEOUT_SEC,
                    )
                    if not ok:
                        self._send_json({"error": msg}, status=502)
                        return
                    self._send_json({"ok": True, "message": msg})
                    return
            except Exception as e:  # noqa: BLE001
                logger.exception("POST %s failed", path)
                payload: dict[str, Any] = {"error": str(e)}
                if path == "/power":
                    # request_source_intent persists before it reconciles. If
                    # convergence fails, return the authoritative readback so
                    # the client does not falsely restore the old switch.
                    state, _status = _bluetooth_state_snapshot()
                    payload["state"] = state
                self._send_json(payload, status=502)
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
            stream = _consume_pair_stream(mac)
            try:
                for event in stream:
                    if not self._sse_write(event):
                        return
            except Exception as e:  # noqa: BLE001
                logger.exception("pair stream failed")
                self._sse_write({"stage": "error", "message": str(e)})
            finally:
                stream.close()

    return Handler


# ============================================================
# Pair stream coordination
# ============================================================


@dataclass
class _PairAttempt:
    queue: "asyncio.Queue[dict | None]"
    created_at: float
    driver_future: Any = None
    consumer_attached: bool = False
    expiry_timer: threading.Timer | None = None
    loop: asyncio.AbstractEventLoop | None = None


# In-flight pair attempts keyed by uppercase MAC. Registration precedes the
# POST response; the sole SSE consumer owns cleanup/cancellation.
_PAIR_STREAMS: dict[str, _PairAttempt] = {}
_PAIR_STREAMS_LOCK = threading.Lock()


def _cancel_pair_attempt(
    attempt: _PairAttempt,
    *,
    terminal_message: str | None = None,
) -> None:
    timer = attempt.expiry_timer
    if timer is not None:
        timer.cancel()
    if terminal_message is not None and attempt.loop is not None:
        def _wake_consumer() -> None:
            attempt.queue.put_nowait(
                {"stage": "error", "message": terminal_message},
            )
            attempt.queue.put_nowait(None)

        try:
            # asyncio.Queue is loop-owned. Wake an attached q.get on that loop,
            # before cancellation lets the driver append its own sentinel.
            attempt.loop.call_soon_threadsafe(_wake_consumer)
        except RuntimeError:
            # A stopped loop has no live SSE consumer left to wake.
            pass
    future = attempt.driver_future
    cancel = getattr(future, "cancel", None)
    done = getattr(future, "done", None)
    if callable(cancel) and (not callable(done) or not done()):
        cancel()


def _release_pair_attempt(
    mac: str,
    attempt: _PairAttempt,
    *,
    terminal_message: str | None = None,
) -> bool:
    """Remove an attempt only when it is still the registered generation."""
    with _PAIR_STREAMS_LOCK:
        if _PAIR_STREAMS.get(mac) is not attempt:
            return False
        _PAIR_STREAMS.pop(mac, None)
    _cancel_pair_attempt(attempt, terminal_message=terminal_message)
    return True


def _expire_pair_attempt(mac: str, attempt: _PairAttempt) -> None:
    if _release_pair_attempt(
        mac,
        attempt,
        terminal_message="Pairing timed out.",
    ):
        log_event(logger, "bluetooth.pair_stream_expired", mac=mac)


def _start_pair_stream(mac: str) -> bool:
    """Kick off a pair coroutine on the dispatcher loop. Events flow
    into a queue keyed by MAC; the /stream handler drains it.

    Queue registration is synchronous and happens before POST /pair can reply,
    so an immediately-following SSE GET cannot race ahead of the driver task.
    Returns False when the same device already has an unconsumed attempt.
    """
    mac_u = mac.upper()
    dispatcher = _dispatch()
    loop = dispatcher._loop  # noqa: SLF001 — single owner
    if loop is None:
        raise RuntimeError("dispatcher not started")
    q: asyncio.Queue[dict | None] = asyncio.Queue()
    attempt = _PairAttempt(queue=q, created_at=time.monotonic(), loop=loop)
    stale_attempt: _PairAttempt | None = None
    with _PAIR_STREAMS_LOCK:
        previous = _PAIR_STREAMS.get(mac_u)
        if previous is not None:
            if time.monotonic() - previous.created_at < PAIR_STREAM_TTL_SEC:
                return False
            _PAIR_STREAMS.pop(mac_u, None)
            stale_attempt = previous
        _PAIR_STREAMS[mac_u] = attempt
    if stale_attempt is not None:
        _cancel_pair_attempt(
            stale_attempt,
            terminal_message="Pair attempt was superseded.",
        )

    async def _drive() -> None:
        try:
            async for event in dispatcher.engine.pair(mac_u):
                await q.put(event)
        except Exception as e:  # noqa: BLE001
            log_event(
                logger,
                "bluetooth.pair_failed",
                level=logging.ERROR,
                exc_info=True,
            )
            await q.put({"stage": "error", "message": str(e)})
        finally:
            await q.put(None)  # sentinel
            # Don't pop from _PAIR_STREAMS yet — the consumer may not
            # have started reading yet. Cleanup happens in the
            # consumer's finally block.

    drive_coro = _drive()
    try:
        driver_future = asyncio.run_coroutine_threadsafe(drive_coro, loop)
    except (RuntimeError, TypeError):
        _close_awaitable(drive_coro)
        _release_pair_attempt(
            mac_u,
            attempt,
            terminal_message="Pairing could not start.",
        )
        raise
    try:
        expiry_timer = threading.Timer(
            PAIR_STREAM_TTL_SEC,
            _expire_pair_attempt,
            args=(mac_u, attempt),
        )
        expiry_timer.daemon = True
        with _PAIR_STREAMS_LOCK:
            if _PAIR_STREAMS.get(mac_u) is attempt:
                attempt.driver_future = driver_future
                attempt.expiry_timer = expiry_timer
                registered = True
            else:
                registered = False
        if not registered:
            attempt.driver_future = driver_future
            _cancel_pair_attempt(attempt)
            return False
        expiry_timer.start()
    except (OSError, RuntimeError):
        _release_pair_attempt(
            mac_u,
            attempt,
            terminal_message="Pairing could not start.",
        )
        raise
    return True


def _consume_pair_stream(mac: str):
    """Generator that drains the queue for `mac` on the dispatcher
    loop and yields events to the HTTP handler thread."""
    mac_u = mac.upper()
    dispatcher = _dispatch()
    loop = dispatcher._loop  # noqa: SLF001
    if loop is None:
        return
    claimed = False
    with _PAIR_STREAMS_LOCK:
        attempt = _PAIR_STREAMS.get(mac_u)
        if attempt is not None and not attempt.consumer_attached:
            attempt.consumer_attached = True
            claimed = True
    if attempt is None:
        # No pair attempt is in flight for this MAC (the user may
        # have hit the stream URL without POSTing /pair first).
        yield {"stage": "error", "message": "no pair attempt in flight"}
        return
    if not claimed:
        yield {"stage": "error", "message": "pair stream already attached"}
        return
    q = attempt.queue
    try:
        while True:
            fut: Future = asyncio.run_coroutine_threadsafe(q.get(), loop)
            item = fut.result()
            if item is None:
                return
            yield item
    finally:
        _release_pair_attempt(mac_u, attempt)


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
