"""Accessories hub — http://jts.local/dial/.

Was the rotary-dial onboarding wizard exclusively; reworked as a
multi-device accessories page when the Anticater VK-01 volume knob
landed. The URL stays at /dial/ (no nginx churn) but the page is now
about *all* accessories: the ESP32 rotary dial, third-party HID
accessories (volume knobs etc.), and — in the future — the AMOLED
satellite.

Routes (paths after nginx strips /dial/):
  GET  /                  landing — list of connected accessories +
                          chooser of types to add
  GET  /setup/dial        ESP32-S3 rotary dial wizard (scan + flash +
                          provision the Pi's WiFi)
  GET  /setup/knob        Bluetooth pair wizard for HID accessories
                          (Anticater VK-01 today; any BT HID later)
  GET  /scan              JSON list of plugged-in ESP32-S3 devices
                          (used by /setup/dial)
  POST /onboard           run jasper-dial-onboard for a port
                          (used by /setup/dial)
  GET  /knob-pair-stream  Server-Sent Events stream of BT pair-flow
                          status (used by /setup/knob)

Stack: stdlib http.server (no extra deps), parallel to spotify_setup.
ThreadingHTTPServer — one request per thread, fine for setup pages
and a single concurrent SSE stream.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import shutil
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

# Same recognized USB IDs as the CLI's find_dial(): Espressif VID +
# the JTAG/serial PIDs S3 chips ship with.
ESP32_S3_VID = 0x303A
ESP32_S3_PIDS = {0x1001, 0x1002, 0x4001}

# Where the dial CLI lives in the deployed venv.
ONBOARD_BIN = "/opt/jasper/.venv/bin/jasper-dial-onboard"


# ============================================================
# USB ESP32-S3 enumeration (dial wizard)
# ============================================================


def _list_esp32_s3_ports() -> list[dict[str, Any]]:
    """Enumerate plugged-in ESP32-S3 devices on USB CDC. Returns a
    list of {port, vid, pid, serial, description} dicts. Empty list
    on no matches or if pyserial isn't importable."""
    try:
        from serial.tools import list_ports
    except ImportError:
        logger.warning("pyserial not available; can't list serial ports")
        return []
    out = []
    for p in list_ports.comports():
        if p.vid == ESP32_S3_VID and p.pid in ESP32_S3_PIDS:
            out.append({
                "port": p.device,
                "vid": f"0x{p.vid:04x}",
                "pid": f"0x{p.pid:04x}",
                "serial": p.serial_number or "",
                "description": p.description or "",
            })
    return out


def _read_pi_ssid() -> str:
    """Best-effort read of the Pi's current WiFi SSID, for display
    (not the password — we never show that in the UI). Returns the
    SSID string or an empty placeholder."""
    if not shutil.which("nmcli"):
        return ""
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return ""
    for line in out.splitlines():
        if ":802-11-wireless" in line or ":wifi" in line:
            return line.split(":", 1)[0]
    return ""


# ============================================================
# HTML rendering
# ============================================================


_PAGE_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         max-width: 720px; margin: 2em auto; padding: 0 1em; color: #222; }
  h1 { margin-bottom: 0.25em; } h2 { margin-top: 2em; }
  .sub { color: #666; margin-top: 0; }
  .msg { background: #e8f4ff; border: 1px solid #abd; padding: 0.6em 0.8em;
          border-radius: 6px; margin: 1em 0; }
  .err { background: #ffe8e8; border-color: #d99; }
  .ok  { background: #e8ffec; border-color: #9c9; }
  button, a.btn {
    background: #1db954; color: white; border: 0;
    padding: 0.6em 1.2em; border-radius: 4px; font-size: 1em;
    cursor: pointer; text-decoration: none; display: inline-block;
  }
  button[disabled] { background: #bbb; cursor: not-allowed; }
  button.secondary, a.btn.secondary { background: #4a4a4a; }
  button:hover:not([disabled]), a.btn:hover { filter: brightness(1.1); }
  .device-card {
    background: #f4f4f4; padding: 0.8em 1em; border-radius: 6px;
    margin: 0.6em 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.95em;
  }
  .device-card .port { font-weight: 600; color: #1a6; }
  .device-card .meta { color: #666; font-size: 0.85em; margin-top: 0.3em; }
  .add-card {
    display: block; background: #fafafa; border: 1px solid #ddd;
    border-radius: 8px; padding: 1em 1.2em; margin: 0.6em 0;
    text-decoration: none; color: #222;
  }
  .add-card:hover { background: #f0f0f0; border-color: #bbb; }
  .add-card h3 { margin: 0 0 0.2em 0; color: #1a6; }
  .add-card .what { color: #666; font-size: 0.9em; }
  .accessory-row {
    display: flex; align-items: center; justify-content: space-between;
    background: #f4f4f4; padding: 0.8em 1em; border-radius: 6px;
    margin: 0.4em 0;
  }
  .accessory-row .name { font-weight: 600; }
  .accessory-row .mac, .accessory-row .ip {
    color: #666; font-size: 0.85em;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .accessory-row .battery { color: #1a6; font-weight: 600; }
  .accessory-row .offline { color: #c44; }
  .accessory-row .forget {
    background: transparent; color: #c44; border: 1px solid #c44;
    padding: 0.3em 0.7em; font-size: 0.85em;
  }
  .accessory-row .forget:hover { background: #c44; color: white; }
  .step { background: #fafafa; border: 1px solid #ddd; padding: 0.8em 1em;
           border-radius: 6px; margin: 0.6em 0; }
  .step .num { display: inline-block; width: 1.6em; height: 1.6em;
                background: #1db954; color: white; border-radius: 50%;
                text-align: center; line-height: 1.6em; font-weight: 600;
                margin-right: 0.5em; }
  .step.done .num { background: #999; }
  .step.active .num { background: #ff9; color: #555;
                       animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
  .spinner {
    display: inline-block; width: 1em; height: 1em;
    border: 2px solid #ddd; border-top-color: #1db954;
    border-radius: 50%; animation: spin 0.8s linear infinite;
    vertical-align: middle; margin-right: 0.4em;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  pre { background: #1e1e1e; color: #ddd; padding: 0.8em; border-radius: 6px;
        overflow-x: auto; font-size: 0.85em; }
"""


def _wrap_page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_PAGE_STYLE}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
{body}
</body>
</html>""".encode()


def _landing_html() -> bytes:
    body = """
<p class="sub">Wireless controls and satellite devices connected to JTS.
The rotary dial and AMOLED touch satellite run our firmware over WiFi;
third-party Bluetooth HID accessories (volume knobs, macro pads) talk to
the Pi via the kernel's HID layer and a small bridge daemon.</p>

<h2>Currently connected</h2>
<div id="connected"><p class="sub"><span class="spinner"></span>Loading…</p></div>

<h2>Add a new accessory</h2>

<a class="add-card" href="setup/knob">
  <h3>Volume knob &nbsp;<span style="color:#666;font-weight:400;font-size:0.85em">— Anticater VK-01 (Bluetooth)</span></h3>
  <p class="what">Pair a desktop volume knob over Bluetooth. Rotate to set volume, click to mute. ~$25, USB-C charged, ~30-day battery life on BT.</p>
</a>

<a class="add-card" href="setup/dial">
  <h3>Rotary dial &nbsp;<span style="color:#666;font-weight:400;font-size:0.85em">— CrowPanel ESP32-S3 (WiFi)</span></h3>
  <p class="what">Onboard a JTS-firmware ELECROW CrowPanel display-knob: volume, play/pause, hold-to-talk. Pushes the Pi's WiFi creds over USB once; runs wirelessly afterward.</p>
</a>

<p class="sub" style="margin-top:2em; font-size:0.85em">More accessory types coming as we add support — a touch + mic satellite (AMOLED) is in progress and will appear here once Phase 1.3 ships.</p>

<script>
async function loadConnected() {
  const el = document.getElementById('connected');
  let dial = null;
  let bt = [];
  try {
    const r = await fetch('/dial/status', { cache: 'no-store' });
    if (r.ok) dial = await r.json();
  } catch (e) { /* offline, OK */ }
  try {
    const r = await fetch('/accessories/list', { cache: 'no-store' });
    if (r.ok) {
      const data = await r.json();
      bt = data.accessories || [];
    }
  } catch (e) { /* offline, OK */ }

  const parts = [];
  if (dial && dial.last_seen_at !== null) {
    const online = dial.online === true;
    const age = dial.age_seconds === null ? '?' : Math.round(dial.age_seconds) + 's';
    parts.push(`
      <div class="accessory-row">
        <div>
          <div class="name">Rotary dial <span class="${online ? '' : 'offline'}">${online ? '· online' : '· offline (' + age + ' ago)'}</span></div>
          <div class="ip">${escapeHtml(dial.last_seen_ip || '')}</div>
        </div>
      </div>
    `);
  }
  for (const a of bt) {
    const status = a.connected ? '· connected' : '· paired, not connected';
    const statusClass = a.connected ? '' : 'offline';
    const batt = a.battery !== null && a.battery !== undefined
      ? `<span class="battery">${a.battery}%</span>`
      : '<span class="sub" style="font-size:0.85em">battery: unknown</span>';
    parts.push(`
      <div class="accessory-row">
        <div>
          <div class="name">${escapeHtml(a.name || '(unnamed)')} <span class="${statusClass}">${status}</span></div>
          <div class="mac">${escapeHtml(a.mac)} &middot; ${batt}</div>
        </div>
        <button class="forget" onclick="forget('${escapeHtml(a.mac)}', '${escapeHtml(a.name)}')">Forget</button>
      </div>
    `);
  }
  if (parts.length === 0) {
    el.innerHTML = '<p class="sub">No accessories connected yet. Add one below.</p>';
  } else {
    el.innerHTML = parts.join('');
  }
}

async function forget(mac, name) {
  if (!confirm(`Forget "${name}"? You'll need to re-pair to use it again.`)) return;
  try {
    const r = await fetch('/accessories/forget', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mac: mac }),
    });
    const data = await r.json();
    if (data.error) {
      alert('Forget failed: ' + data.error);
    } else {
      loadConnected();
    }
  } catch (e) {
    alert('Forget request failed: ' + e);
  }
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

loadConnected();
setInterval(loadConnected, 5000);
</script>
"""
    return _wrap_page("Accessories", body)


def _setup_dial_html(*, ssid: str) -> bytes:
    ssid_disp = html.escape(ssid) if ssid else "(unknown — check Pi WiFi)"
    body = f"""
<p class="sub"><a href="../">← Accessories</a></p>

<p>Onboard a JTS-firmware rotary dial — the ELECROW CrowPanel 1.28" ESP32-S3
display-knob. Plug it into the Pi via USB once so this wizard can push the
Pi's WiFi creds ({ssid_disp}). The dial runs wirelessly afterward.</p>

<div id="status"><span class="spinner"></span>Scanning for a USB-plugged dial…</div>
<div id="devices"></div>
<div id="result"></div>

<script>
const statusEl = document.getElementById('status');
const devicesEl = document.getElementById('devices');
const resultEl = document.getElementById('result');
let pollTimer = null;
let lastDevices = [];
let busy = false;

async function scan() {{
  if (busy) return;
  try {{
    const r = await fetch('../scan', {{ cache: 'no-store' }});
    const data = await r.json();
    render(data.devices || []);
  }} catch (e) {{
    console.warn('scan failed', e);
  }}
}}

function render(devices) {{
  if (devices.length === lastDevices.length &&
      devices.every((d, i) => d.port === (lastDevices[i] && lastDevices[i].port))) {{
    return;
  }}
  lastDevices = devices;
  if (devices.length === 0) {{
    statusEl.innerHTML = '<span class="spinner"></span>Waiting for a USB device…';
    devicesEl.innerHTML = '';
    return;
  }}
  statusEl.textContent = devices.length + ' device(s) detected:';
  devicesEl.innerHTML = devices.map(d => `
    <div class="device-card">
      <div class="port">${{d.port}}</div>
      <div class="meta">
        VID ${{d.vid}} · PID ${{d.pid}} · Serial ${{d.serial || '(none)'}}<br>
        ${{d.description}}
      </div>
      <p style="margin-top: 0.8em;">
        <button onclick="provision('${{d.port}}', false)">Provision (smart)</button>
        <button class="secondary" onclick="provision('${{d.port}}', true)">Force flash + provision</button>
      </p>
      <p class="sub" style="margin-top: 0.4em; font-size: 0.85em;">
        <strong>Smart</strong> probes the device first — if it's already
        running JTS firmware, only the WiFi creds get pushed (no flash).
        Use <strong>Force</strong> only if the device is stuck or you
        want to bring an unflashed ESP32-S3 onto JTS.
      </p>
    </div>
  `).join('');
}}

async function provision(port, force) {{
  if (busy) return;
  busy = true;
  clearInterval(pollTimer);
  resultEl.innerHTML = '<div class="msg"><span class="spinner"></span>Provisioning ' + port + '…<br><small>This can take 30-90 seconds. Don\\'t unplug.</small></div>';
  try {{
    const r = await fetch('../onboard', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ port: port, force_flash: force }}),
    }});
    const data = await r.json();
    if (data.ok) {{
      resultEl.innerHTML = `
        <div class="msg ok">
          <strong>Done.</strong> ${{data.message || 'Dial is online.'}}
          <p style="margin: 0.4em 0 0 0;"><small>You can unplug from the Pi now and connect to USB power.</small></p>
        </div>
        ${{data.log ? '<details><summary>Show log</summary><pre>' + escapeHtml(data.log) + '</pre></details>' : ''}}
      `;
    }} else {{
      resultEl.innerHTML = `
        <div class="msg err">
          <strong>Failed.</strong> ${{escapeHtml(data.error || 'Unknown error')}}
        </div>
        ${{data.log ? '<details open><summary>Log</summary><pre>' + escapeHtml(data.log) + '</pre></details>' : ''}}
      `;
    }}
  }} catch (e) {{
    resultEl.innerHTML = '<div class="msg err">Request failed: ' + escapeHtml(String(e)) + '</div>';
  }} finally {{
    busy = false;
    pollTimer = setInterval(scan, 2000);
  }}
}}

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }})[c]);
}}

scan();
pollTimer = setInterval(scan, 2000);
</script>
"""
    return _wrap_page("Onboard a Rotary Dial", body)


def _setup_knob_html() -> bytes:
    body = """
<p class="sub"><a href="../">← Accessories</a></p>

<p>Pair an <strong>Anticater VK-01</strong> volume knob over Bluetooth. Once
paired, rotate it to change volume and click to mute, all wirelessly. The
knob doesn't need to be plugged into the Pi after this — it runs on its
internal battery (~30 days per charge) and reconnects automatically when in
range.</p>

<h2>Step 1 — Put the knob in pairing mode</h2>
<div class="step">
  <span class="num">1</span>
  Press <strong>and hold</strong> the knob's button for about 3 seconds, until
  the indicator starts blinking. The knob is now advertising for ~60 seconds.
</div>

<h2>Step 2 — Pair with the Pi</h2>
<div class="step">
  <span class="num">2</span>
  Click <strong>Start pairing</strong> below. The Pi will scan for nearby
  accessories in pair mode and pair with the first one it sees.
</div>

<p style="margin-top: 1.5em;">
  <button id="startBtn" onclick="startPair()">Start pairing</button>
  <a class="btn secondary" href="../">Cancel</a>
</p>

<div id="progress" style="margin-top: 2em;"></div>

<p class="sub" style="margin-top: 3em; font-size: 0.85em;">
Other BT HID accessories (macro pads, foot pedals) usually work the same
way — put yours in pair mode and click Start. We'll improve this page when
more device types are formally supported.</p>

<script>
const STEPS = [
  { key: 'scanning', label: 'Scanning for accessories in pair mode' },
  { key: 'found', label: 'Accessory found' },
  { key: 'pairing', label: 'Pairing' },
  { key: 'trusted', label: 'Trusting' },
  { key: 'connected', label: 'Connecting' },
];

let evtSrc = null;

function startPair() {
  const btn = document.getElementById('startBtn');
  btn.disabled = true;
  btn.textContent = 'Pairing…';
  const progress = document.getElementById('progress');
  progress.innerHTML = STEPS.map((s, i) =>
    `<div class="step" id="s-${s.key}"><span class="num">${i + 1}</span> ${s.label}</div>`
  ).join('');

  evtSrc = new EventSource('../knob-pair-stream');
  let lastStep = -1;
  evtSrc.onmessage = ev => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    const idx = STEPS.findIndex(s => s.key === data.status);
    if (idx >= 0) {
      // Mark previous steps done, current active.
      for (let i = 0; i < STEPS.length; i++) {
        const el = document.getElementById('s-' + STEPS[i].key);
        if (!el) continue;
        if (i < idx) el.classList.add('done');
        if (i === idx) el.classList.add('active');
        if (i > idx) el.classList.remove('done', 'active');
      }
      if (data.name) {
        document.getElementById('s-found').innerHTML =
          `<span class="num">2</span> Accessory found — <strong>${escapeHtml(data.name)}</strong> <code>${escapeHtml(data.mac || '')}</code>`;
      }
      lastStep = Math.max(lastStep, idx);
      if (data.status === 'connected') {
        evtSrc.close();
        document.getElementById('s-' + data.status).classList.add('done');
        document.getElementById('s-' + data.status).classList.remove('active');
        progress.innerHTML += `
          <div class="msg ok" style="margin-top:1em;">
            <strong>Paired and connected.</strong>
            <p style="margin:0.4em 0 0 0">You can put the knob anywhere now — it'll reconnect on its own when in Bluetooth range. <a href="../">Back to accessories</a>.</p>
          </div>
        `;
        document.getElementById('startBtn').textContent = 'Pair another';
        document.getElementById('startBtn').disabled = false;
      }
    }
    if (data.status === 'error') {
      evtSrc.close();
      progress.innerHTML += `
        <div class="msg err" style="margin-top:1em;">
          <strong>Pairing failed.</strong> ${escapeHtml(data.message || 'Unknown error')}
          <p style="margin:0.4em 0 0 0">Make sure the knob's indicator is blinking (pair mode) and try again.</p>
        </div>
      `;
      document.getElementById('startBtn').textContent = 'Try again';
      document.getElementById('startBtn').disabled = false;
    }
  };
  evtSrc.onerror = () => {
    if (lastStep < STEPS.length - 1) {
      progress.innerHTML += `<div class="msg err" style="margin-top:1em;"><strong>Stream interrupted.</strong> Try again.</div>`;
      document.getElementById('startBtn').textContent = 'Try again';
      document.getElementById('startBtn').disabled = false;
    }
    if (evtSrc) evtSrc.close();
  };
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}
</script>
"""
    return _wrap_page("Pair a Volume Knob", body)


# ============================================================
# Dial-onboarding subprocess wrapper (existing)
# ============================================================


def _run_onboard(port: str, *, force_flash: bool, timeout_s: float = 180.0) -> dict[str, Any]:
    """Invoke jasper-dial-onboard for `port`. Returns a dict with
    {ok, message, log, error?}.

    Smart vs Force semantics:
      smart  → --auto: mDNS pre-check first (already-online dials
               short-circuit instantly with no serial / no chip
               reset), then probe; never flashes an arbitrary
               ESP32-S3, just pushes WiFi creds if the device speaks
               Improv.
      force  → --flash: bypass mDNS, bypass probe; always flash the
               staged firmware bin and push WiFi creds.
    """
    cmd = [ONBOARD_BIN, "--port", port, "--verbose"]
    if force_flash:
        cmd.append("--flash")
    else:
        cmd.append("--auto")
    logger.info("running onboard: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "error": f"onboard timed out after {timeout_s:.0f}s",
            "log": (e.stdout or "") + (e.stderr or ""),
        }
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"could not run jasper-dial-onboard: {e}"}
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        if "auto mode short-circuit" in log:
            message = "Dial was already online — no changes made."
        else:
            message = "Dial provisioned and online."
        return {"ok": True, "message": message, "log": log}
    return {
        "ok": False,
        "error": f"jasper-dial-onboard exit code {proc.returncode}",
        "log": log,
    }


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
            self._send(status, body, "text/html; charset=utf-8")

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

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/":
                self._send_html(_landing_html())
                return
            # Legacy alias — /setup was the dial wizard before this
            # page became the accessories hub. Redirect to keep any
            # external links / bookmarks alive.
            if path in ("/setup", "/setup/dial"):
                ssid = _read_pi_ssid()
                self._send_html(_setup_dial_html(ssid=ssid))
                return
            if path == "/setup/knob":
                self._send_html(_setup_knob_html())
                return
            if path == "/scan":
                self._send_json({"devices": _list_esp32_s3_ports()})
                return
            if path == "/knob-pair-stream":
                self._stream_knob_pair()
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/onboard":
                body = self._read_json()
                port = (body.get("port") or "").strip()
                force = bool(body.get("force_flash"))
                if not port or not port.startswith("/dev/"):
                    self._send_json(
                        {"ok": False, "error": "missing or invalid port"},
                        status=400,
                    )
                    return
                # Sanity-check: only run for ports that match an
                # actually-plugged ESP32-S3 right now.
                ports_now = {d["port"] for d in _list_esp32_s3_ports()}
                if port not in ports_now:
                    self._send_json(
                        {
                            "ok": False,
                            "error": f"{port} is not a recognized ESP32-S3 device (not plugged in?)",
                        },
                        status=400,
                    )
                    return
                result = _run_onboard(port, force_flash=force)
                status = 200 if result.get("ok") else 502
                self._send_json(result, status=status)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _stream_knob_pair(self) -> None:
            """SSE stream of bluez pair-flow events. One per yielded
            status from `pair_first_hid()`. ThreadingHTTPServer puts
            us in our own thread, so we can run an asyncio loop here
            without blocking other requests."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            # Disable nginx's response buffering for this location so
            # SSE events show up live instead of accumulating until
            # the upstream closes.
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            # Import inside the handler so a missing dbus-next (e.g.,
            # editable install before pip-install) doesn't break the
            # rest of the wizard.
            try:
                from ..accessories.pairing import pair_first_hid
                from ..accessories.registry import VK01
            except Exception as e:  # noqa: BLE001
                logger.exception("can't import pair flow")
                try:
                    self.wfile.write(
                        f"data: {json.dumps({'status': 'error', 'message': f'pair module not available: {e}'})}\n\n".encode()
                    )
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                # The "Volume knob" button on the wizard is the only
                # one wired today. We pin its BT name filter so a
                # stray nearby Apple Magic Mouse / Surface Dial in
                # pair mode doesn't get picked up first.
                gen = pair_first_hid(name_regex=VK01.bt_name_regex)
                while True:
                    try:
                        event = loop.run_until_complete(gen.__anext__())
                    except StopAsyncIteration:
                        break
                    except Exception as e:  # noqa: BLE001
                        logger.exception("pair flow raised")
                        event = {"status": "error",
                                 "message": f"internal: {e}"}
                    try:
                        self.wfile.write(
                            f"data: {json.dumps(event)}\n\n".encode(),
                        )
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        # Client closed the stream. Cancel the generator
                        # and clean up — bluez stays in whatever state
                        # the cleanup-finally inside pair_first_hid
                        # leaves it (which is "stopped discovery, agent
                        # unregistered, bus disconnected").
                        try:
                            loop.run_until_complete(gen.aclose())
                        except Exception:  # noqa: BLE001
                            pass
                        return
                    if event.get("status") in ("connected", "error"):
                        # Drive the generator's finally block, then exit.
                        try:
                            loop.run_until_complete(gen.aclose())
                        except Exception:  # noqa: BLE001
                            pass
                        return
            finally:
                try:
                    loop.close()
                except Exception:  # noqa: BLE001
                    pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-dial-web",
        description="Accessories hub (rotary dial + BT HID pairing wizards) at /dial/",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_DIAL_WEB_HOST", "127.0.0.1"),
        help="bind host (default 127.0.0.1; nginx proxies /dial/ in front)",
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_DIAL_WEB_PORT", "8766")),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = ThreadingHTTPServer((args.host, args.port), _make_handler())
    logger.info(
        "jasper-dial-web (accessories hub) listening on http://%s:%d",
        args.host, args.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
