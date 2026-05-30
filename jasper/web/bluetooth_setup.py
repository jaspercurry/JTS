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
  POST /pair/<mac>/respond     {"accept": bool, "value"?: str|int}
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
import html
import json
import logging
import os
import threading
from concurrent.futures import Future
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from ._common import (
    DIALOG_CSS,
    NAV_BACK_CSS,
    NAV_BACK_HTML,
    TOGGLE_CSS,
    begin_request,
    csrf_fetch_helpers_js,
    csrf_meta_html,
    dialog_helpers_js,
    reject_csrf,
    send_html_response,
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
    block on the result. The engine, agent, and observer all live
    on this loop, so they share one bus connection and one set of
    signal subscriptions across the whole daemon.
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

_PAGE_STYLE = TOGGLE_CSS + """
  :root {
    --green: #1db954; --red: #c44; --grey: #999; --soft: #666;
    --bg: #fafafa; --card: #fff; --border: #e6e6e6;
  }
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui,
         sans-serif; max-width: 720px; margin: 2em auto; padding: 0 1em;
         color: #222; background: var(--bg); }
  h1 { margin-bottom: 0.25em; } h2 { margin-top: 2em; }
  .sub { color: var(--soft); margin-top: 0; }
  .msg { background: #e8f4ff; border: 1px solid #abd; padding: 0.6em 0.8em;
         border-radius: 6px; margin: 1em 0; }
  .err { background: #ffe8e8; border-color: #d99; }
  .ok  { background: #e8ffec; border-color: #9c9; }
  button {
    background: var(--green); color: white; border: 0;
    padding: 0.5em 1em; border-radius: 4px; font-size: 0.95em;
    cursor: pointer;
  }
  button[disabled] { background: #bbb; cursor: not-allowed; }
  button.secondary { background: #4a4a4a; }
  button.danger { background: transparent; color: var(--red);
                  border: 1px solid var(--red); }
  button.danger:hover { background: var(--red); color: white; }
  button:hover:not([disabled]) { filter: brightness(1.1); }
  /* Scan button keeps a stable width between "Scan" and "Scanning"
     so the layout doesn't jitter as the label flips. */
  #scan-btn { min-width: 7.5em; }
  #scan-btn.scanning { background: #4a4a4a; }
  .btn-spinner {
    display: inline-block; width: 0.85em; height: 0.85em;
    border: 2px solid rgba(255,255,255,0.35);
    border-top-color: white;
    border-radius: 50%; animation: spin 0.8s linear infinite;
    vertical-align: -0.15em; margin-right: 0.45em;
  }

  .toggles { background: var(--card); border: 1px solid var(--border);
             border-radius: 8px; padding: 1em 1.2em; margin-bottom: 1em; }
  .toggle-row { display: flex; align-items: center;
                justify-content: space-between; padding: 0.5em 0; }
  .toggle-row + .toggle-row { border-top: 1px solid var(--border); }
  .toggle-row .label { font-weight: 600; }
  .toggle-row .hint { color: var(--soft); font-size: 0.85em;
                      margin-top: 0.15em; }

  .device-list { background: var(--card); border: 1px solid var(--border);
                 border-radius: 8px; overflow: hidden; margin: 0.5em 0; }
  .device-list .empty { color: var(--soft); padding: 1em 1.2em;
                        font-style: italic; }
  .device {
    display: flex; align-items: center; padding: 0.8em 1.2em;
    border-bottom: 1px solid var(--border); gap: 1em;
  }
  .device:last-child { border-bottom: 0; }
  .device .icon { font-size: 1.5em; width: 1.5em; text-align: center;
                  color: var(--soft); }
  .device .info { flex: 1; }
  .device .name { font-weight: 600; }
  .device .meta {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    color: var(--soft); font-size: 0.85em;
  }
  .device .badges { margin-left: 0.5em; }
  .device .badge {
    display: inline-block; padding: 0.1em 0.5em; border-radius: 10px;
    font-size: 0.75em; font-weight: 600; margin-left: 0.3em;
    background: #eef; color: #336;
  }
  .device .badge.connected { background: #def9d9; color: #163; }
  .device .badge.connecting { background: #fff3cf; color: #6b4a00; }
  .device .badge.paired { background: #f0e8ff; color: #436; }
  .device .actions { display: flex; gap: 0.4em; }
  .device .rssi {
    color: var(--soft); font-size: 0.8em; min-width: 3em; text-align: right;
  }
  .device .metrics {
    display: flex; gap: 0.9em; align-items: flex-start;
    margin-right: 0.4em;
  }
  .device .metric {
    text-align: right; line-height: 1.1;
  }
  .device .metric .label {
    color: var(--soft); font-size: 0.7em; text-transform: uppercase;
    letter-spacing: 0.04em; margin-bottom: 0.15em;
  }
  .device .metric .value {
    font-size: 0.95em; font-weight: 600; color: #222;
  }
  .device .metric .value.bars { letter-spacing: 0.04em; }

  .pair-card {
    background: #fffbe6; border: 1px solid #d9c97a;
    padding: 0.8em 1.2em; border-radius: 8px; margin: 0.8em 0;
  }
  .pair-card .stage { display: flex; align-items: center; gap: 0.4em;
                      margin: 0.2em 0; color: var(--soft); }
  .pair-card .stage.active { color: #222; font-weight: 600; }
  .pair-card .stage.done { color: #163; }
  .pair-card .passkey {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 2em; letter-spacing: 0.1em; font-weight: 700;
    text-align: center; padding: 0.4em 0; color: #222;
  }
  .pair-card .prompt-buttons { display: flex; gap: 0.5em;
                                justify-content: center; margin-top: 0.5em; }

  .spinner {
    display: inline-block; width: 1em; height: 1em;
    border: 2px solid #ddd; border-top-color: var(--green);
    border-radius: 50%; animation: spin 0.8s linear infinite;
    vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Simple icon glyphs (font-awesome-ish; just unicode for now). */
  .icon-phone::before { content: "\\1F4F1"; }
  .icon-computer::before { content: "\\1F4BB"; }
  .icon-audio-headphones::before { content: "\\1F3A7"; }
  .icon-audio-card::before { content: "\\1F50A"; }
  .icon-input-keyboard::before { content: "\\1F39B"; }
  .icon-device::before { content: "\\1F4E1"; }
""" + NAV_BACK_CSS + DIALOG_CSS


def _landing_html(csrf_token: str = "") -> bytes:
    body = """
<p class="sub">Pair phones (to use this as a Bluetooth speaker), volume
knobs, headphones, anything that speaks Bluetooth.</p>

<div class="toggles">
  <div class="toggle-row">
    <div>
      <div class="label">Bluetooth</div>
      <div class="hint" id="bt-hint">Loading…</div>
    </div>
    <label class="toggle">
      <input type="checkbox" id="sw-power" aria-label="Bluetooth" disabled>
      <span class="track"></span>
    </label>
  </div>
  <div class="toggle-row">
    <div>
      <div class="label">Discoverable</div>
      <div class="hint" id="disc-hint">
        While on, other devices can see &amp; pair JTS as a speaker.
        Auto-turns off after 5&nbsp;min.
      </div>
    </div>
    <label class="toggle">
      <input type="checkbox" id="sw-disc" aria-label="Discoverable" disabled>
      <span class="track"></span>
    </label>
  </div>
</div>

<h2>My devices</h2>
<div class="device-list" id="paired-list">
  <div class="empty">Loading…</div>
</div>

<h2>Other devices
  <button id="scan-btn" onclick="toggleScan()"
          style="float:right;font-size:0.8em;padding:0.3em 0.8em;">Scan</button>
</h2>
<div class="device-list" id="other-list">
  <div class="empty">Nothing nearby. Try scanning.</div>
</div>

<p class="sub" style="margin-top: 3em; font-size: 0.85em;">
Already paired devices stay paired even when Bluetooth is off; turning
it back on lets them reconnect. Forget a device to wipe its pair record.
</p>

<script>{dialog_helpers_js}</script>
<script>
let state = { powered: false, discoverable: false, discovering: false };
let devices = new Map(); // path → device
let evtSrc = null;
let pairStreams = new Map(); // mac → EventSource
let stateTimer = null;
let scanIntentUntil = 0;  // ms; client-side window where we treat
                           // the button as scanning even before the
                           // server polling catches up
{csrf_fetch_helpers_js}

// -------- adapter state + toggles --------

async function fetchState() {
  try {
    const r = await fetch('state', { cache: 'no-store' });
    state = await r.json();
    renderToggles();
  } catch (e) {
    document.getElementById('bt-hint').textContent =
      'Bluetooth daemon unreachable.';
  }
}

function renderToggles() {
  const power = document.getElementById('sw-power');
  power.checked = !!state.powered;
  power.disabled = false;
  const sd = document.getElementById('sw-disc');
  sd.checked = !!state.discoverable;
  sd.disabled = !state.powered;
  let hint = state.powered ? `On — adapter ${state.adapter || 'hci0'}` : 'Off';
  if (state.discovering) hint += ' · scanning…';
  document.getElementById('bt-hint').textContent = hint;

  const btn = document.getElementById('scan-btn');
  btn.disabled = !state.powered;
  // Treat the button as "scanning" if the server reports Discovering
  // OR we just clicked Scan in the last ~3 s — bridges the gap
  // between optimistic click and the polling cycle confirming it.
  const intent = Date.now() < scanIntentUntil;
  const scanning = state.discovering || intent;
  btn.classList.toggle("scanning", scanning);
  btn.innerHTML = scanning
    ? "<span class=\\"btn-spinner\\"></span>Scanning"
    : "Scan";

  // While scanning, poll faster so the button reverts promptly when
  // the auto-stop fires server-side.
  schedulePoll(scanning ? 1500 : 5000);
}

function schedulePoll(ms) {
  if (stateTimer !== null) clearInterval(stateTimer);
  stateTimer = setInterval(fetchState, ms);
}

// HID profile fragments — 0x1124 (BR/EDR HID) and 0x1812 (BLE HOGP).
// VK-01-class knobs advertise HOGP only, not classic HID. Mirrors
// jasper.bluetooth.models.is_hid_uuids so the warning fires in
// the same conditions on either side.
const HID_UUID_FRAGMENTS = ['00001124-', '00001812-'];

function pairedHidNames() {
  const names = [];
  for (const d of devices.values()) {
    if (!d.paired) continue;
    const uu = (d.uuids || []).join(' ').toLowerCase();
    if (HID_UUID_FRAGMENTS.some(f => uu.includes(f))) {
      names.push(d.name || 'Unknown device');
    }
  }
  return names;
}

async function togglePower() {
  const input = document.getElementById('sw-power');
  const previous = !!state.powered;
  const target = !!input.checked;
  function restoreToggle() {
    input.checked = previous;
  }
  if (target === previous) return;
  // Warn before turning Bluetooth off while a wireless remote
  // (volume knob, etc.) is paired — otherwise the remote silently
  // stops working until BT is turned back on.
  if (!target) {
    const hidNames = pairedHidNames();
    if (hidNames.length) {
      const which = hidNames.length === 1
        ? hidNames[0]
        : hidNames.length + ' paired remotes';
      const ok = await jtsConfirm(
        'Turning Bluetooth off will also disconnect ' + which +
        '. Wireless remotes will not work again until Bluetooth ' +
        'is turned back on.\\n\\nTurn Bluetooth off anyway?',
        {danger: true},
      );
      if (!ok) {
        restoreToggle();
        return;
      }
    }
  }
  try {
    const r = await fetch('power', {
      method: 'POST', headers: jsonHeaders(),
      body: JSON.stringify({on: target}),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      restoreToggle();
      jtsAlert('Bluetooth toggle failed: ' + (data.error || data.message || r.status));
    }
  } catch (e) {
    restoreToggle();
    jtsAlert('Network error talking to the Bluetooth backend.');
  } finally {
    setTimeout(fetchState, 300);
  }
}

async function toggleDisc() {
  const input = document.getElementById('sw-disc');
  if (!state.powered) {
    input.checked = false;
    return;
  }
  const previous = !!state.discoverable;
  const target = !!input.checked;
  function restoreToggle() {
    input.checked = previous;
  }
  if (target === previous) return;
  try {
    const r = await fetch('discoverable', {
      method: 'POST', headers: jsonHeaders(),
      body: JSON.stringify({on: target}),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      restoreToggle();
      jtsAlert('Discoverable toggle failed: ' + (data.error || data.message || r.status));
    }
  } catch (e) {
    restoreToggle();
    jtsAlert('Network error talking to the Bluetooth backend.');
  } finally {
    setTimeout(fetchState, 300);
  }
}

async function toggleScan() {
  const action = state.discovering ? 'stop' : 'start';
  // Optimistic UI: assume the click took effect so the button
  // flips immediately. State polling will correct us if not.
  if (action === 'start') {
    scanIntentUntil = Date.now() + 3000;
  } else {
    scanIntentUntil = 0;
  }
  renderToggles();
  await fetch('scan', {
    method: 'POST', headers: jsonHeaders(),
    body: JSON.stringify({action}),
  });
  // Pull fresh state a beat after the POST so the button label
  // matches reality without waiting for the next poll tick.
  setTimeout(fetchState, 200);
}

// -------- live device list --------

function startDeviceStream() {
  if (evtSrc) evtSrc.close();
  evtSrc = new EventSource('devices/stream');
  evtSrc.onmessage = ev => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    if (data.action === 'remove') {
      devices.delete(data.device.path);
    } else {
      devices.set(data.device.path, data.device);
    }
    renderDevices();
  };
  evtSrc.onerror = () => {
    // Auto-reconnect on stream drop.
    setTimeout(startDeviceStream, 2000);
  };
}

function renderDevices() {
  const paired = [];
  const other = [];
  for (const d of devices.values()) {
    (d.paired ? paired : other).push(d);
  }
  // Paired: connected first, then by name.
  paired.sort((a, b) => (b.connected - a.connected)
    || (a.name || a.address).localeCompare(b.name || b.address));
  // Other: by RSSI desc (nulls last), then name.
  other.sort((a, b) => {
    const ar = a.rssi ?? -200, br = b.rssi ?? -200;
    if (ar !== br) return br - ar;
    return (a.name || a.address).localeCompare(b.name || b.address);
  });

  document.getElementById('paired-list').innerHTML = paired.length
    ? paired.map(d => deviceRow(d, true)).join('')
    : '<div class="empty">No paired devices yet.</div>';
  document.getElementById('other-list').innerHTML = other.length
    ? other.map(d => deviceRow(d, false)).join('')
    : '<div class="empty">Nothing nearby. Try scanning.</div>';
}

function deviceRow(d, isPaired) {
  // Bluez fills Alias with a MAC-shaped string when the remote
  // doesn't broadcast a name — server side filters those out into
  // empty `name`, so we cleanly fall back to a placeholder here
  // (mirrors iPhone's "Unknown" + MAC layout).
  const hasName = !!d.name;
  const label = hasName ? d.name : 'Unknown device';
  // MAC is shown only when there's no friendly name — most users
  // don't care about MACs and showing them on every named device
  // is visual noise. Unknown devices show MAC so they can still
  // be told apart.
  const metaLine = hasName ? '' :
    `<div class="meta">${escapeHtml(d.address)}</div>`;
  let badges = '';
  if (d.connected && d.servicesResolved === false) {
    badges += '<span class="badge connecting">Connecting</span>';
  } else if (d.connected) {
    badges += '<span class="badge connected">Connected</span>';
  }
  else if (d.paired) badges += '<span class="badge paired">Paired</span>';
  let actions = '';
  if (isPaired) {
    actions = d.connected
      ? `<button class="secondary" data-action="disconnect" data-mac="${escapeHtml(d.address)}">Disconnect</button>`
      : `<button data-action="connect" data-mac="${escapeHtml(d.address)}">Connect</button>`;
    actions += ` <button class="danger" data-action="forget" data-mac="${escapeHtml(d.address)}" data-label="${escapeHtml(label)}">Forget</button>`;
  } else {
    actions = `<button data-action="pair" data-mac="${escapeHtml(d.address)}">Pair</button>`;
  }
  // Metrics. Render each label only when bluez actually has a value
  // for it — surfacing a "—" placeholder suggests we're polling for
  // the value and coming up empty, but in fact bluez doesn't expose
  // RSSI for connected BLE devices at all (they stop advertising
  // once linked, and HCI Read-RSSI is a BT-Classic-only command).
  // Showing nothing is more honest than a perpetual dash.
  let metrics = '';
  if (isPaired) {
    const parts = [];
    if (d.battery !== null && d.battery !== undefined) {
      parts.push(`
        <div class="metric">
          <div class="label">Battery</div>
          <div class="value">${d.battery}%</div>
        </div>`);
    } else if (d.connected && d.batteryCapable) {
      parts.push(`
        <div class="metric">
          <div class="label">Battery</div>
          <div class="value">No reading</div>
        </div>`);
    }
    if (d.rssi !== null && d.rssi !== undefined) {
      parts.push(`
        <div class="metric">
          <div class="label">Signal</div>
          <div class="value"><span class="bars">${rssiBars(d.rssi)}</span></div>
        </div>`);
    }
    if (parts.length) {
      metrics = `<div class="metrics">${parts.join('')}</div>`;
    }
  } else if (d.rssi !== null && d.rssi !== undefined) {
    metrics = `<div class="rssi">${rssiBars(d.rssi)}</div>`;
  }
  return `
    <div class="device" id="d-${cssIdSafe(d.address)}">
      <div class="icon icon-${iconSlug(d.icon)}"></div>
      <div class="info">
        <div class="name">${escapeHtml(label)} ${badges}</div>
        ${metaLine}
        <div id="pair-${cssIdSafe(d.address)}"></div>
      </div>
      ${metrics}
      <div class="actions">${actions}</div>
    </div>
  `;
}

function rssiBars(rssi) {
  if (rssi >= -60) return '●●●●';
  if (rssi >= -75) return '●●●○';
  if (rssi >= -85) return '●●○○';
  return '●○○○';
}

// -------- pair flow --------

async function startPair(mac) {
  if (pairStreams.has(mac)) return; // already pairing this device
  const slot = document.getElementById(`pair-${cssIdSafe(mac)}`);
  if (!slot) return;
  slot.innerHTML = `<div class="pair-card" id="pc-${cssIdSafe(mac)}">
    <div class="stage active" id="ps-${cssIdSafe(mac)}-init">
      <span class="spinner"></span> Starting pair…
    </div>
  </div>`;

  await fetch('pair', {
    method: 'POST', headers: jsonHeaders(),
    body: JSON.stringify({mac}),
  });

  const es = new EventSource(`pair/${encodeURIComponent(mac)}/stream`);
  pairStreams.set(mac, es);
  const card = document.getElementById(`pc-${cssIdSafe(mac)}`);

  es.onmessage = ev => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    renderPairStage(mac, data, card);
    if (data.stage === 'ready' || data.stage === 'error') {
      es.close();
      pairStreams.delete(mac);
      // Hide card after a short delay so user can read the final state.
      setTimeout(() => {
        const slot = document.getElementById(`pair-${cssIdSafe(mac)}`);
        if (slot) slot.innerHTML = '';
      }, data.stage === 'error' ? 8000 : 4000);
    }
  };
  es.onerror = () => {
    es.close();
    pairStreams.delete(mac);
  };
}

function renderPairStage(mac, data, card) {
  if (!card) return;
  const init = document.getElementById(`ps-${cssIdSafe(mac)}-init`);
  if (init) init.remove();
  const stageId = `ps-${cssIdSafe(mac)}-${data.stage}`;
  let stageEl = document.getElementById(stageId);
  if (!stageEl) {
    stageEl = document.createElement('div');
    stageEl.className = 'stage active';
    stageEl.id = stageId;
    card.appendChild(stageEl);
  }
  // Mark previous stage as done (visual cue).
  Array.from(card.querySelectorAll('.stage.active')).forEach(el => {
    if (el !== stageEl) {
      el.classList.remove('active');
      el.classList.add('done');
    }
  });

  if (data.stage === 'starting') {
    stageEl.innerHTML = `<span class="spinner"></span> Starting pair…`;
  } else if (data.stage === 'trusting') {
    stageEl.innerHTML = `<span class="spinner"></span> Trusting…`;
  } else if (data.stage === 'pairing') {
    stageEl.innerHTML = `<span class="spinner"></span> Pairing…`;
  } else if (data.stage === 'confirm_passkey') {
    stageEl.classList.remove('active');
    card.innerHTML = `
      <div>Confirm that this code matches what's shown on the device:</div>
      <div class="passkey">${formatPasskey(data.passkey)}</div>
      <div class="prompt-buttons">
        <button data-pair-action="respond" data-mac="${escapeHtml(mac)}" data-accept="true">Yes, matches</button>
        <button class="danger" data-pair-action="respond" data-mac="${escapeHtml(mac)}" data-accept="false">No</button>
      </div>
    `;
  } else if (data.stage === 'request_passkey') {
    card.innerHTML = `
      <div>Enter the passkey shown on the device:</div>
      <input type="number" id="pk-${cssIdSafe(mac)}" maxlength="6"
             style="font-size:1.5em;text-align:center;padding:0.3em;
                    margin:0.5em auto;display:block;width:6em;">
      <div class="prompt-buttons">
        <button data-pair-action="passkey" data-mac="${escapeHtml(mac)}">Enter</button>
        <button class="danger" data-pair-action="respond" data-mac="${escapeHtml(mac)}" data-accept="false">Cancel</button>
      </div>
    `;
  } else if (data.stage === 'request_pincode') {
    card.innerHTML = `
      <div>This device wants a PIN code (legacy pairing). Common
      defaults: <code>0000</code>, <code>1234</code>.</div>
      <input type="text" id="pc-${cssIdSafe(mac)}" maxlength="16"
             value="0000"
             style="font-size:1.3em;text-align:center;padding:0.3em;
                    margin:0.5em auto;display:block;width:10em;">
      <div class="prompt-buttons">
        <button data-pair-action="pincode" data-mac="${escapeHtml(mac)}">Enter</button>
        <button class="danger" data-pair-action="respond" data-mac="${escapeHtml(mac)}" data-accept="false">Cancel</button>
      </div>
    `;
  } else if (data.stage === 'display_passkey') {
    stageEl.classList.remove('active');
    card.innerHTML = `
      <div>Type this on the device:</div>
      <div class="passkey">${formatPasskey(data.passkey)}</div>
      <div class="sub" style="text-align:center;font-size:0.85em">
        Waiting for the device to enter the passkey…
      </div>
    `;
  } else if (data.stage === 'paired') {
    stageEl.innerHTML = '✓ Paired';
    stageEl.classList.remove('active');
    stageEl.classList.add('done');
  } else if (data.stage === 'connecting') {
    stageEl.innerHTML = `<span class="spinner"></span> Connecting…`;
  } else if (data.stage === 'wiring') {
    stageEl.innerHTML = `<span class="spinner"></span> ${escapeHtml(data.detail || 'Configuring…')}`;
  } else if (data.stage === 'ready') {
    stageEl.innerHTML = '✓ Ready';
    stageEl.classList.remove('active');
    stageEl.classList.add('done');
    card.style.background = '#e8ffec';
    card.style.borderColor = '#9c9';
    if (data.detail) {
      const det = document.createElement('div');
      det.className = 'sub';
      det.style.marginTop = '0.4em';
      det.textContent = data.detail;
      card.appendChild(det);
    }
  } else if (data.stage === 'error') {
    card.innerHTML = `
      <div style="color:var(--red);font-weight:600;margin-bottom:0.4em">
        Pairing failed.
      </div>
      <div>${escapeHtml(data.message || 'Unknown error')}</div>
    `;
    card.style.background = '#ffe8e8';
    card.style.borderColor = '#d99';
  }
}

async function respondPair(mac, accept) {
  await fetch(`pair/${encodeURIComponent(mac)}/respond`, {
    method: 'POST', headers: jsonHeaders(),
    body: JSON.stringify({accept}),
  });
}

async function submitPasskey(mac) {
  const v = document.getElementById(`pk-${cssIdSafe(mac)}`).value;
  await fetch(`pair/${encodeURIComponent(mac)}/respond`, {
    method: 'POST', headers: jsonHeaders(),
    body: JSON.stringify({accept: true, value: parseInt(v, 10) || 0}),
  });
}

async function submitPincode(mac) {
  const v = document.getElementById(`pc-${cssIdSafe(mac)}`).value;
  await fetch(`pair/${encodeURIComponent(mac)}/respond`, {
    method: 'POST', headers: jsonHeaders(),
    body: JSON.stringify({accept: true, value: v}),
  });
}

// -------- connect / disconnect / forget --------

async function connectDevice(mac, connect) {
  const path = connect ? 'connect' : 'disconnect';
  await fetch(path, {
    method: 'POST', headers: jsonHeaders(),
    body: JSON.stringify({mac}),
  });
}

async function forget(mac, label) {
  if (!await jtsConfirm(`Forget "${label}"? You'll need to re-pair to use it.`, {danger: true})) return;
  const r = await fetch('forget', {
    method: 'POST', headers: jsonHeaders(),
    body: JSON.stringify({mac}),
  });
  const data = await r.json();
  if (data.error) jtsAlert('Forget failed: ' + data.error);
}

document.addEventListener('click', function(e) {
  const actionBtn = e.target.closest('button[data-action]');
  if (actionBtn) {
    const mac = actionBtn.dataset.mac || '';
    if (actionBtn.dataset.action === 'pair') startPair(mac);
    if (actionBtn.dataset.action === 'connect') connectDevice(mac, true);
    if (actionBtn.dataset.action === 'disconnect') connectDevice(mac, false);
    if (actionBtn.dataset.action === 'forget') {
      forget(mac, actionBtn.dataset.label || 'Unknown device');
    }
    return;
  }

  const pairBtn = e.target.closest('button[data-pair-action]');
  if (!pairBtn) return;
  const mac = pairBtn.dataset.mac || '';
  if (pairBtn.dataset.pairAction === 'respond') {
    respondPair(mac, pairBtn.dataset.accept === 'true');
  } else if (pairBtn.dataset.pairAction === 'passkey') {
    submitPasskey(mac);
  } else if (pairBtn.dataset.pairAction === 'pincode') {
    submitPincode(mac);
  }
});

// -------- helpers --------

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
function cssIdSafe(s) { return String(s).replace(/[^a-zA-Z0-9]/g, '_'); }
function iconSlug(s) { return String(s || 'device').replace(/[^a-zA-Z0-9_-]/g, '') || 'device'; }
function formatPasskey(p) {
  // Bluez passkey is uint32 0..999999; zero-pad to 6.
  const n = Number.parseInt(p, 10);
  return String(Number.isFinite(n) ? n : 0).padStart(6, '0');
}

// -------- bootstrap --------

document.getElementById('sw-power').addEventListener('change', togglePower);
document.getElementById('sw-disc').addEventListener('change', toggleDisc);
fetchState();
startDeviceStream();
schedulePoll(5000);
</script>
"""
    return _wrap_page(
        "Bluetooth",
        body.replace("{csrf_fetch_helpers_js}", csrf_fetch_helpers_js()).replace(
            "{dialog_helpers_js}", dialog_helpers_js()
        ),
        csrf_token,
    )


def _wrap_page(title: str, body: str, csrf_token: str = "") -> bytes:
    csrf = csrf_meta_html(csrf_token) if csrf_token else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_PAGE_STYLE}</style>
</head>
<body>
{csrf}
{NAV_BACK_HTML}
<h1>{html.escape(title)}</h1>
{body}
</body>
</html>""".encode()


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
                or (path.startswith("/pair/") and path.endswith("/respond"))
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
                if path.startswith("/pair/") and path.endswith("/respond"):
                    mac = path[len("/pair/"):-len("/respond")]
                    accept = bool(body.get("accept"))
                    value = body.get("value")
                    ok = _dispatch().engine.respond_prompt(
                        mac, accept=accept, value=value,
                    )
                    self._send_json({"ok": ok})
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
            "jasper-bluetooth-web adopting systemd fd (Discoverable "
            "auto-off after %ds when toggled on)",
            DISCOVERABLE_AUTO_OFF_SEC,
        )
    else:
        logger.info(
            "jasper-bluetooth-web listening on http://%s:%d (Discoverable "
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
