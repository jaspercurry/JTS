"""User-initiated rotary-dial onboarding wizard. Public surface:
http://jts.local/dial/ — visible there because nginx reverse-proxies
/dial/ → http://127.0.0.1:8766/.

Why a wizard and not a udev auto-trigger:
  Earlier this lived under a udev rule that fired on any ESP32-S3
  plug-in, then ran jasper-dial-onboard --auto. Two problems:
    1. Too broad — Espressif's USB VID isn't unique to JTS, so an
       unrelated S3 board (the user's other side projects) would
       also trigger flashing.
    2. Surprising — silent flashing on plug-in, no way for the user
       to confirm "yes, this is my dial, go ahead".
  This wizard makes the action explicit: visit /dial/, click a
  button to start scanning, see the detected device, click another
  button to flash + provision. The CLI tool (jasper-dial-onboard)
  still does the actual work; this is a thin web wrapper around it.

Stack: stdlib http.server (no extra deps), parallel to spotify_setup.
One thread per request — fine for an occasional setup page.

Routes (paths after nginx strips /dial/):
  GET  /                  landing page with "Continue" button
  GET  /setup             setup page with detection + provision UI
  GET  /scan              JSON list of plugged-in ESP32-S3 devices
  POST /onboard           run jasper-dial-onboard for a port (JSON in/out)
"""
from __future__ import annotations

import argparse
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

# Same recognized USB IDs as the CLI's find_dial(): Espressif VID + the
# JTAG/serial PIDs S3 chips ship with. We display whatever we find here
# in the wizard UI so the user can confirm; we don't try to filter
# beyond this at the listing layer (the boot-log probe inside
# jasper-dial-onboard is the actual JTS-vs-other-S3 filter, and runs
# only when the user clicks Provision).
ESP32_S3_VID = 0x303A
ESP32_S3_PIDS = {0x1001, 0x1002, 0x4001}

# Where the CLI tool lives in the deployed venv. Hardcoded because the
# wizard runs out of the same /opt/jasper venv.
ONBOARD_BIN = "/opt/jasper/.venv/bin/jasper-dial-onboard"


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
         max-width: 620px; margin: 2em auto; padding: 0 1em; color: #222; }
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
<p class="sub">JTS supports an ELECROW CrowPanel ESP32-S3 rotary dial as a
wireless physical controller — volume, play/pause, hold-to-talk.</p>

<p>To onboard a fresh dial:</p>

<ol>
  <li>Plug the dial into a USB-C port on the JTS Pi.</li>
  <li>Click <strong>Continue</strong> below — the next page detects the
  device and lets you flash + provision it with the Pi's WiFi.</li>
  <li>Once it's online, unplug from the Pi and connect to USB power.
  The dial reconnects to WiFi from flash on every subsequent boot.</li>
</ol>

<p style="margin-top: 2em;"><a class="btn" href="setup">Continue</a></p>

<p class="sub" style="margin-top: 3em; font-size: 0.85em;">
This wizard runs <code>jasper-dial-onboard</code> behind the scenes —
the same CLI tool also works directly from the Pi shell if you
prefer.</p>
"""
    return _wrap_page("Onboard a Rotary Dial", body)


def _setup_html(*, ssid: str) -> bytes:
    ssid_disp = html.escape(ssid) if ssid else "(unknown — check Pi WiFi)"
    body = f"""
<p class="sub">Plug a rotary dial into the Pi's USB-C port. As soon as
it's detected we'll show you the option to provision it onto the Pi's
WiFi network ({ssid_disp}).</p>

<div id="status"><span class="spinner"></span>Scanning for a device…</div>
<div id="devices"></div>
<div id="result"></div>

<p style="margin-top: 2em;"><a class="btn secondary" href=".">← Back</a></p>

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
    const r = await fetch('scan', {{ cache: 'no-store' }});
    const data = await r.json();
    render(data.devices || []);
  }} catch (e) {{
    console.warn('scan failed', e);
  }}
}}

function render(devices) {{
  if (devices.length === lastDevices.length &&
      devices.every((d, i) => d.port === (lastDevices[i] && lastDevices[i].port))) {{
    return; // no change
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
  // Stop polling while we run — the onboard call will reset the chip,
  // and the polling loop would interfere.
  clearInterval(pollTimer);
  resultEl.innerHTML = '<div class="msg"><span class="spinner"></span>Provisioning ' + port + '…<br><small>This can take 30-90 seconds. Don\\'t unplug.</small></div>';
  try {{
    const r = await fetch('onboard', {{
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
    // Resume polling so the user can onboard another dial without
    // reloading the page.
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


# ============================================================
# Onboarding subprocess wrapper
# ============================================================


def _run_onboard(port: str, *, force_flash: bool, timeout_s: float = 180.0) -> dict[str, Any]:
    """Invoke jasper-dial-onboard for `port`. Returns a dict with
    {ok, message, log, error?}. Captures both stdout and stderr so the
    web UI can show them on failure.

    Smart vs Force semantics:
      smart  → --auto: mDNS pre-check first (already-online dials
               short-circuit instantly with no serial / no chip
               reset), then probe; never flashes an arbitrary
               ESP32-S3, just pushes WiFi creds if the device speaks
               Improv. Right call for the common case.
      force  → --flash: bypass mDNS, bypass probe; always flash the
               staged firmware bin and push WiFi creds. Right call
               when a dial is wedged / on stale firmware / brand-new
               unflashed S3 board you explicitly want to bring onto
               JTS.
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
        # The CLI's --auto mode short-circuits without touching the
        # dial when mDNS reports it's already online; surface that
        # distinct case in the UI ("nothing changed") vs. the real
        # provision-completed case ("just configured").
        if "auto mode short-circuit" in log:
            message = "Dial was already online — no changes made."
        else:
            message = "Dial provisioned and online."
        return {
            "ok": True,
            "message": message,
            "log": log,
        }
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
            if path == "/setup":
                ssid = _read_pi_ssid()
                self._send_html(_setup_html(ssid=ssid))
                return
            if path == "/scan":
                self._send_json({"devices": _list_esp32_s3_ports()})
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
                # actually-plugged ESP32-S3 right now. Stops a
                # crafted POST from running esptool against arbitrary
                # tty paths on the Pi.
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

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-dial-web",
        description="User-initiated web wizard for onboarding a JTS rotary dial",
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
    logger.info("jasper-dial-web listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
