"""Sources on/off page at /sources/.

Playback-source toggles:

  - **AirPlay** ↔ `shairport-sync.service` (systemctl enable/disable +
    start/stop). nqptp stays running either way; it's a tiny PTP daemon
    shairport depends on but doesn't itself produce audio.
  - **Bluetooth** ↔ bluez `Adapter1.Powered` DBus property. Same call
    the /bluetooth/ wizard uses (jasper.bluetooth.adapter.set_powered).
    Runtime-only — a reboot brings the radio back on; the bluetooth
    daemon itself stays running so /bluetooth/ keeps showing live state.
  - **Spotify Connect** ↔ `librespot.service` (systemctl enable/disable
    + start/stop). Toggling this is NOT the same as "claiming" librespot
    to a Spotify account — claiming is a separate one-time OAuth step
    needed only for voice cold-start (see /spotify/). Basic phone-side
    Spotify Connect works without claiming.
  - **USB Audio Input** ↔ `jasper-usbsink.service` (systemctl
    enable/disable + start/stop). The main service owns the
    `jasper-usbsink-init.service` ConfigFS lifecycle through systemd
    Requires/PartOf wiring.

AirPlay, Bluetooth, and Spotify Connect default ON. USB Audio Input
defaults OFF so it has zero resident RAM cost until explicitly enabled.
The toggle is the only knob; there's no per-source settings on this page.

State polling: clients GET /state every few seconds to reflect external
changes (operator ran `systemctl stop shairport-sync` from SSH, etc.).

URL surface (after nginx strips /sources/):
  GET  /         page render
  GET  /state    {airplay, bluetooth, spotify_connect, usbsink} → {enabled: bool, available: bool}
  POST /set      {source, enabled} → same shape as /state on success
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ._common import TOGGLE_CSS, begin_request, send_html_response, wrap_page

logger = logging.getLogger(__name__)


# (source-key, systemd-unit) pairs. The wizard refers to sources by key
# (airplay / bluetooth / spotify_connect / usbsink) in its JSON; the
# systemd units are an implementation detail kept here.
AIRPLAY_UNIT = "shairport-sync.service"
SPOTIFY_CONNECT_UNIT = "librespot.service"
# Toggling jasper-usbsink.service via systemctl --now also propagates
# to jasper-usbsink-init.service via the Requires/PartOf chain in the
# main unit. No need to touch the init unit directly.
USBSINK_UNIT = "jasper-usbsink.service"

VALID_SOURCES = ("airplay", "bluetooth", "spotify_connect", "usbsink")

# /boot/firmware/config.txt line that install.sh's set_usb_gadget_mode
# writes. Without this, the BCM2712 OTG controller stays in host mode
# (the Pi 5 default) and the USB-C port is power-only — flipping the
# wizard toggle on would just fail at the init.service ConfigFS
# write. The wizard surfaces this as `available: false` so the row
# shows disabled instead of presenting a broken on/off.
BOOT_CONFIG_PATH = "/boot/firmware/config.txt"
USBSINK_DTOVERLAY_LINE = "dtoverlay=dwc2,dr_mode=peripheral"


def _usbsink_available() -> bool:
    """True iff the dtoverlay that puts the USB-C port in peripheral
    mode is present in /boot/firmware/config.txt. Fail-soft on read
    errors (treat as unavailable so the toggle is disabled) — the
    operator can re-run install.sh to recover."""
    try:
        with open(BOOT_CONFIG_PATH) as f:
            content = f.read()
    except OSError as e:
        logger.debug("usbsink dtoverlay probe failed: %s", e)
        return False
    # Tolerate leading whitespace and trailing comments.
    for line in content.splitlines():
        if line.strip().startswith(USBSINK_DTOVERLAY_LINE):
            return True
    return False


def _systemctl(*args: str, timeout: int = 10) -> tuple[int, str]:
    """Run `systemctl <args>` and return (rc, stripped-stdout). Errors
    are logged but not raised; the caller decides how to surface them."""
    try:
        proc = subprocess.run(
            ["systemctl", *args],
            check=False, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("systemctl %s failed: %s", " ".join(args), e)
        return 1, ""


def _unit_active(unit: str) -> bool:
    rc, out = _systemctl("is-active", unit, timeout=5)
    return rc == 0 and out == "active"


def _set_unit(unit: str, enabled: bool) -> None:
    """Enable+start or disable+stop a systemd unit. Failures are logged
    but not raised — partial state ("disabled but still running" or
    "enabled but stopped") is rare and self-heals on the next toggle.

    enable/disable is paired with start/stop so the on/off state
    survives a reboot."""
    if enabled:
        _systemctl("enable", unit, "--now")
    else:
        _systemctl("disable", unit, "--now")


async def _bt_state() -> tuple[bool, bool, bool]:
    """Return (available, powered, has_paired_hid). Available=False when
    bluez itself isn't reachable on this host (no BT hardware, daemon
    not running). has_paired_hid is True when a wireless remote
    (volume knob etc.) is paired — the wizard surfaces this as a
    confirm-before-off prompt so toggling BT doesn't silently kill
    the remote."""
    try:
        from ..bluetooth.adapter import has_paired_hid, state
        s = await state()
        powered = bool(s.get("powered", False))
        hid = False
        try:
            hid = await has_paired_hid()
        except Exception as e:  # noqa: BLE001
            # Non-fatal: the powered toggle still works, we just lose
            # the warning. Logged in case the helper itself breaks.
            logger.debug("has_paired_hid probe failed: %s", e)
        return True, powered, hid
    except Exception as e:  # noqa: BLE001
        # DBusError on no-hardware Pi, ImportError on stripped builds,
        # other OSErrors if bluez is wedged. Treat as "unavailable".
        logger.debug("bluetooth state probe failed: %s", e)
        return False, False, False


async def _set_bt(enabled: bool) -> None:
    from ..bluetooth.adapter import set_powered
    await set_powered(enabled)


def _gather_state() -> dict[str, dict[str, bool]]:
    """One-shot snapshot of all four sources. The BT branch runs an
    asyncio task because dbus-next is async-only; the rest are sync
    systemctl probes."""
    bt_available, bt_powered, bt_has_hid = asyncio.run(_bt_state())
    return {
        "airplay": {
            "enabled": _unit_active(AIRPLAY_UNIT),
            "available": True,
        },
        "bluetooth": {
            "enabled": bt_powered,
            "available": bt_available,
            "hasPairedHid": bt_has_hid,
        },
        "spotify_connect": {
            "enabled": _unit_active(SPOTIFY_CONNECT_UNIT),
            "available": True,
        },
        "usbsink": {
            "enabled": _unit_active(USBSINK_UNIT),
            # available=False shows the toggle as disabled with a
            # note pointing at the dtoverlay + reboot path. Once
            # install.sh has added the dtoverlay and the user has
            # rebooted, the row becomes interactive.
            "available": _usbsink_available(),
        },
    }


def _apply(source: str, enabled: bool) -> None:
    """Route the toggle to the right backend. Caller has already
    validated `source` is in VALID_SOURCES."""
    if source == "airplay":
        _set_unit(AIRPLAY_UNIT, enabled)
    elif source == "spotify_connect":
        _set_unit(SPOTIFY_CONNECT_UNIT, enabled)
    elif source == "bluetooth":
        asyncio.run(_set_bt(enabled))
    elif source == "usbsink":
        # jasper-usbsink.service Requires=+PartOf= the init.service, so
        # systemctl enable/disable --now propagates to both — the init
        # creates the ConfigFS gadget and loads libcomposite when on,
        # tears down + rmmods when off, returning RAM to baseline.
        _set_unit(USBSINK_UNIT, enabled)


# Per-row layout layered on top of PAGE_STYLE + TOGGLE_CSS. The iOS
# switch markup itself (label.toggle > input + span.track) lives in
# _common.TOGGLE_CSS so /wake/'s detection layers can reuse the same
# visual without copy-pasting the rules.
_PAGE_CSS = f"""
<style>
{TOGGLE_CSS}
  .source-row {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 1em 0;
    border-bottom: 1px solid #eee;
  }}
  .source-row:last-child {{ border-bottom: none; }}
  .source-name {{ font-weight: 600; font-size: 1.05em; color: #222; }}
  .source-note {{ color: #888; font-size: 0.9em; margin-top: 0.2em; }}
</style>
"""


def _index_html() -> bytes:
    """Render the sources page. Initial state is loaded from the server
    on first poll (one extra round trip on page load — keeps the HTML
    static and cache-friendly)."""
    body = _PAGE_CSS + """
<p class="sub">Turn each playback source on or off. AirPlay and Spotify
Connect persist across reboots; Bluetooth comes back on after a reboot
(use this for runtime mute, not permanent disable). USB Audio Input
is off by default — flip it on to use JTS as a USB audio output for a
computer plugged into the Pi's USB-C port.</p>

<div id="sources">
  <div class="source-row">
    <div>
      <div class="source-name">AirPlay</div>
    </div>
    <label class="toggle">
      <input type="checkbox" id="t-airplay" disabled>
      <span class="track"></span>
    </label>
  </div>
  <div class="source-row">
    <div>
      <div class="source-name">Bluetooth</div>
      <div class="source-note" id="bt-note" style="display:none">
        Bluetooth adapter not available on this device.
      </div>
    </div>
    <label class="toggle">
      <input type="checkbox" id="t-bluetooth" disabled>
      <span class="track"></span>
    </label>
  </div>
  <div class="source-row">
    <div>
      <div class="source-name">Spotify Connect</div>
    </div>
    <label class="toggle">
      <input type="checkbox" id="t-spotify_connect" disabled>
      <span class="track"></span>
    </label>
  </div>
  <div class="source-row">
    <div>
      <div class="source-name">USB Audio Input</div>
      <div class="source-note" id="usbsink-note">
        Plug a computer into the Pi's USB-C port (via the 8086 splitter).
        Your computer sees JTS as a USB audio output device.
      </div>
      <div class="source-note" id="usbsink-unavailable-note" style="display:none">
        USB gadget mode not enabled in /boot/firmware/config.txt —
        re-run install.sh and reboot.
      </div>
    </div>
    <label class="toggle">
      <input type="checkbox" id="t-usbsink" disabled>
      <span class="track"></span>
    </label>
  </div>
</div>

<script>
  // Four toggles, all wired to the same backend. Optimistic UI: we
  // flip the checkbox immediately, then POST and reconcile from the
  // response (rolls back on failure). Poll /state every 4 s when the
  // tab is visible — picks up external systemctl changes from SSH.
  (function() {
    var POLL_MS = 4000;
    var SOURCES = ['airplay', 'bluetooth', 'spotify_connect', 'usbsink'];
    var inFlight = {};
    var dirty = {};
    var ignorePollUntil = 0;
    var latestState = {};

    function el(id) { return document.getElementById(id); }

    function applyState(state) {
      latestState = state;
      SOURCES.forEach(function(name) {
        var s = state[name] || {};
        var input = el('t-' + name);
        if (dirty[name]) return;  // user toggled mid-flight; don't clobber
        input.checked = !!s.enabled;
        input.disabled = s.available === false;
      });
      var btUnavailable = state.bluetooth && state.bluetooth.available === false;
      el('bt-note').style.display = btUnavailable ? '' : 'none';
      // USB sink shows a "needs reboot" note when the dtoverlay
      // is missing (install.sh hasn't been run with the gadget
      // section, or it's been manually removed).
      var usbUnavailable = state.usbsink && state.usbsink.available === false;
      el('usbsink-note').style.display = usbUnavailable ? 'none' : '';
      el('usbsink-unavailable-note').style.display = usbUnavailable ? '' : 'none';
    }

    async function fetchState() {
      if (document.visibilityState === 'hidden') return;
      if (Date.now() < ignorePollUntil) return;
      try {
        var resp = await fetch('./state', {cache: 'no-store'});
        if (resp.ok) applyState(await resp.json());
      } catch (_) {}
    }

    async function postToggle(name, want) {
      // Optimistic flip already happened on click. Mark dirty so polls
      // don't overwrite while we wait for the server.
      dirty[name] = true;
      inFlight[name] = true;
      // Pause polling briefly so a poll fired right before this POST
      // doesn't reconcile back to the old value.
      ignorePollUntil = Date.now() + 1500;
      try {
        var resp = await fetch('./set', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({source: name, enabled: want}),
        });
        if (resp.ok) {
          var state = await resp.json();
          dirty[name] = false;
          applyState(state);
        } else {
          // Server refused — roll back the optimistic flip.
          dirty[name] = false;
          el('t-' + name).checked = !want;
        }
      } catch (_) {
        dirty[name] = false;
        el('t-' + name).checked = !want;
      } finally {
        inFlight[name] = false;
      }
    }

    SOURCES.forEach(function(name) {
      var input = el('t-' + name);
      input.addEventListener('change', function() {
        // Warn before turning Bluetooth off while a wireless remote
        // (volume knob, etc.) is paired — otherwise the remote
        // silently stops working until BT is turned back on.
        if (name === 'bluetooth' && !input.checked &&
            latestState.bluetooth && latestState.bluetooth.hasPairedHid) {
          var ok = window.confirm(
            'Turning Bluetooth off will also disconnect paired ' +
            'wireless remotes (volume knob, etc.). They will not ' +
            'work again until Bluetooth is turned back on.\\n\\n' +
            'Turn Bluetooth off anyway?',
          );
          if (!ok) {
            // Revert the optimistic flip and skip the POST entirely.
            input.checked = true;
            return;
          }
        }
        postToggle(name, input.checked);
      });
    });

    setInterval(fetchState, POLL_MS);
    fetchState();
  })();
</script>
"""
    return wrap_page("Sources", body)


def _make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                # Mint the CSRF cookie even though this page's POSTs are
                # JSON-bodied and protected by Content-Type + SameSite=Strict
                # rather than the form-token check — keeps cookie state
                # consistent across wizards so a refresh from one page
                # doesn't drop the token used by another.
                begin_request(self)
                self._send_html(_index_html())
                return
            if path == "/state":
                try:
                    self._send_json(_gather_state())
                except Exception as e:  # noqa: BLE001
                    logger.exception("/state failed")
                    self._send_json({"error": str(e)}, status=502)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path == "/set":
                body = self._read_json()
                source = str(body.get("source") or "")
                enabled = bool(body.get("enabled"))
                if source not in VALID_SOURCES:
                    self._send_json(
                        {"error": f"unknown source {source!r}"}, status=400,
                    )
                    return
                try:
                    _apply(source, enabled)
                except Exception as e:  # noqa: BLE001
                    logger.exception("toggle %s -> %s failed", source, enabled)
                    self._send_json({"error": str(e)}, status=502)
                    return
                logger.info(
                    "event=sources.set source=%s enabled=%s client=%s",
                    source, enabled, self.address_string(),
                )
                # Read-back the state we just applied so the client UI
                # reconciles against truth (in case systemctl no-op'd
                # or DBus rejected the property write).
                try:
                    self._send_json(_gather_state())
                except Exception as e:  # noqa: BLE001
                    logger.exception("/set readback failed")
                    self._send_json({"error": str(e)}, status=502)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def make_server(target) -> ThreadingHTTPServer:
    """Used by jasper.web.__main__ to colocate this server with the
    other settings wizards inside one process. `target` is a
    socket/tuple/int per _systemd.make_http_server's contract."""
    from . import _systemd
    return _systemd.make_http_server(target, _make_handler())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-sources-web",
        description="Audio source on/off toggles for the Jasper smart speaker",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_SOURCES_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_SOURCES_WEB_PORT", "8773")),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server((args.host, args.port))
    logger.info("jasper-sources-web listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
