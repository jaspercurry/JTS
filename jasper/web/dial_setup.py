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
import datetime
import html
import json
import logging
import os
import shutil
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from ._common import (
    begin_request,
    canonical_header,
    canonical_page,
    reject_csrf,
    send_html_response,
    guard_mutating_request,
)

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

# Where build.sh stages the dial firmware. Matches the default in
# jasper.cli.dial_onboard (--bin). The wizard reports the staged
# .bin's mtime/size on the setup page so the user can see whether
# Force Flash will actually flash something — without this surface,
# a missing or stale .bin caused the silent "auto mode short-circuit"
# UX that hid firmware fixes from new dials.
FIRMWARE_BIN = "/opt/jasper/firmware/dial/jasper-dial.bin"
FIRMWARE_ROOT = "/opt/jasper/firmware/dial"


def _format_mtime(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc,
    ).strftime("%Y-%m-%d %H:%M UTC")


def _newest_firmware_source_mtime(root: str = FIRMWARE_ROOT) -> float | None:
    """Return newest mtime among source inputs that should trigger a
    rebuilt dial .bin. Missing source dirs are normal on development
    hosts that call _read_firmware_status() against a temp bin."""
    candidates: list[str] = [
        os.path.join(root, "build.sh"),
        os.path.join(root, "platformio.ini"),
    ]
    for dirname in ("include", "src"):
        dirpath = os.path.join(root, dirname)
        if not os.path.isdir(dirpath):
            continue
        for base, dirs, files in os.walk(dirpath):
            dirs[:] = [d for d in dirs if d not in {".pio", "__pycache__"}]
            candidates.extend(os.path.join(base, f) for f in files)

    newest: float | None = None
    for path in candidates:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return newest


def _read_firmware_status(
    bin_path: str = FIRMWARE_BIN,
    source_root: str = FIRMWARE_ROOT,
) -> dict[str, Any]:
    """Return firmware freshness metadata for the dial .bin. Always
    returns a dict — fields are None when the file or source tree is
    missing. The wizard surfaces this so users know whether Force Flash
    will actually flash an up-to-date binary."""
    source_mtime = _newest_firmware_source_mtime(source_root)
    try:
        st = os.stat(bin_path)
    except OSError:
        return {
            "present": False,
            "path": bin_path,
            "size_bytes": None,
            "mtime_iso": None,
            "source_newer": False,
            "source_mtime_iso": _format_mtime(source_mtime),
        }
    return {
        "present": True,
        "path": bin_path,
        "size_bytes": st.st_size,
        "mtime_iso": _format_mtime(st.st_mtime),
        "source_newer": source_mtime is not None and source_mtime > st.st_mtime,
        "source_mtime_iso": _format_mtime(source_mtime),
    }


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
#
# Migrated to the canonical design system (canonical_page + app.css). The
# landing page is a static server-rendered card (no JS); the setup page's
# scan/provision behaviour ships as the ES module /assets/dial/js/main.js,
# which reads the CSRF token from the <meta name="jts-csrf"> tag
# canonical_page() emits. Page-specific styling lives in /assets/dial/dial.css.


def _landing_html(csrf_token: str = "") -> bytes:
    body = f"""
{canonical_header("Accessories")}
<main class="page">
  <p class="form-hint">JTS supports a small family of wireless accessories. This
  page can onboard an ELECROW CrowPanel ESP32-S3 <strong>rotary
  dial</strong> — volume, play/pause, hold-to-talk. A web onboarding
  flow for the Waveshare AMOLED touch satellite is in progress; for
  now that one is onboarded from the Pi shell with
  <code>jasper-satellite-onboard</code>.</p>

  <div class="info-card">
    <h2 class="section__title">Onboard a rotary dial</h2>
    <ol class="form-hint">
      <li>Plug the dial into a USB-C port on the JTS Pi.</li>
      <li>Tap <strong>Continue</strong> below — the next page detects the
      device and lets you flash + provision it with the Pi's WiFi.</li>
      <li>Once it's online, unplug from the Pi and connect to USB power.
      The dial reconnects to WiFi from flash on every subsequent boot.</li>
    </ol>
    <div class="form-actions">
      <a class="btn btn--primary" href="setup">Continue</a>
    </div>
  </div>

  <p class="form-hint">This wizard runs <code>jasper-dial-onboard</code> behind
  the scenes — the same CLI tool also works directly from the Pi shell if you
  prefer.</p>
</main>
"""
    return canonical_page("Accessories", body, csrf_token=csrf_token)


def _firmware_banner_html(firmware: dict[str, Any]) -> str:
    """Render the firmware-freshness banner for the setup page. Three states,
    mirroring _read_firmware_status(): present + source-newer (warn), present +
    up-to-date (ok), and not staged (warn). The state machine is unchanged from
    the legacy page; only the wrapper class moved to the canonical .fw-banner
    surface (tone driven by --status-ok / --status-warn in dial.css)."""
    if firmware["present"]:
        size_kb = (firmware["size_bytes"] or 0) // 1024
        if firmware.get("source_newer"):
            source_mtime = html.escape(firmware.get("source_mtime_iso") or "unknown")
            return (
                f'<div class="fw-banner warn">'
                f'<strong>Firmware staged, but source is newer.</strong> '
                f'Force Flash will use <code>{html.escape(firmware["path"])}</code> '
                f'({size_kb} KB, built {html.escape(firmware["mtime_iso"])}) until '
                f'you rebuild from the source last changed {source_mtime}. Run on the Pi:'
                f'<pre>sudo /opt/jasper/.venv/bin/pip install platformio\n'
                f'bash /opt/jasper/firmware/dial/build.sh</pre>'
                f'</div>'
            )
        return (
            f'<div class="fw-banner ok">'
            f'<strong>Firmware ready to flash:</strong> '
            f'<code>{html.escape(firmware["path"])}</code> '
            f'({size_kb} KB, built {html.escape(firmware["mtime_iso"])})'
            f'</div>'
        )
    return (
        '<div class="fw-banner warn">'
        '<strong>No firmware staged.</strong> '
        'Force Flash will not flash anything until <code>jasper-dial.bin</code> '
        'is built. JTS skips optional accessory firmware builds during '
        'base speaker installs; run once on the Pi (SSH in as <code>pi</code>):'
        '<pre>sudo /opt/jasper/.venv/bin/pip install platformio\n'
        'bash /opt/jasper/firmware/dial/build.sh</pre>'
        'then reload this page. (PlatformIO pulls ~300-500 MB of ESP32 '
        'toolchain on first run, so we only install it when you ask.)'
        '</div>'
    )


def _setup_html(
    *, ssid: str, firmware: dict[str, Any], csrf_token: str = "",
) -> bytes:
    ssid_disp = html.escape(ssid) if ssid else "(unknown — check Pi WiFi)"
    fw_banner = _firmware_banner_html(firmware)

    body = f"""
{canonical_header("Onboard a Rotary Dial", back_href=".", back_label="Accessories")}
<main class="page">
  <p class="form-hint">Plug a rotary dial into the Pi's USB-C port. As soon as
  it's detected we'll show you the option to provision it onto the Pi's
  WiFi network ({ssid_disp}).</p>

  {fw_banner}

  <div id="status"><span class="spinner"></span>Scanning for a device…</div>
  <div id="devices"></div>
  <div id="result"></div>
</main>
<script type="module" src="/assets/dial/js/main.js"></script>
"""
    return canonical_page(
        "Onboard a Rotary Dial", body,
        csrf_token=csrf_token,
        page_css_href="/assets/dial/dial.css",
    )


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

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/":
                ctx = begin_request(self)
                self._send_html(_landing_html(ctx["csrf_token"]))
                return
            if path == "/setup":
                ctx = begin_request(self)
                ssid = _read_pi_ssid()
                firmware = _read_firmware_status()
                self._send_html(_setup_html(
                    ssid=ssid, firmware=firmware, csrf_token=ctx["csrf_token"],
                ))
                return
            if path == "/scan":
                self._send_json({"devices": _list_esp32_s3_ports()})
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/onboard":
                if not guard_mutating_request(self):
                    reject_csrf(self)
                    return
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

    from . import _systemd
    sockets = _systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)

    handler_cls = _make_handler()
    server = _systemd.make_http_server(target, handler_cls)

    tracker = _systemd.IdleShutdownTracker()
    _systemd.install_request_idle_bump(handler_cls, tracker)
    tracker.start()

    if sockets:
        logger.info("jasper-dial-web adopting systemd fd")
    else:
        logger.info("jasper-dial-web listening on http://%s:%d", args.host, args.port)

    _systemd.notify_ready()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _systemd.notify_stopping()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
