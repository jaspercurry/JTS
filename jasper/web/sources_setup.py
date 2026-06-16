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
When a renderer unit is not installed, the page is still present but the
unavailable rows are disabled and explain which unit is missing instead
of pretending every source can be started.

This page renders on the canonical design system (canonical_page); its
behaviour ships as the static ES module deploy/assets/sources/js/main.js,
not inline <script>. The routes, JSON shapes, CSRF gate, systemctl/DBus
backends, and fail-soft logging are unchanged from the legacy look.

URL surface (after nginx strips /sources/):
  GET  /         page render
  GET  /state    {airplay, bluetooth, spotify_connect, usbsink} → {enabled: bool, available: bool, unavailableReason?: str}
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

from ..install_profile import install_profile_allows_local_sources, read_install_profile
from ..log_event import log_event
from ._common import (
    bonded_follower_active,
    begin_request,
    canonical_banner,
    canonical_header,
    canonical_page,
    reject_csrf,
    send_html_response,
    toggle_html,
    guard_read_request,
    guard_mutating_request,
)

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
SOURCE_UNAVAILABLE = {
    "airplay": (
        "AirPlay is not installed on this speaker. Re-run install.sh to "
        "set up the local renderer stack."
    ),
    "spotify_connect": (
        "Spotify Connect is not installed on this speaker. Re-run install.sh "
        "to set up the local renderer stack."
    ),
    "bluetooth": (
        "Bluetooth audio is not installed on this speaker. Re-run install.sh "
        "to set up the local renderer stack."
    ),
    "usbsink": (
        "USB Audio Input is not installed on this speaker. Re-run install.sh "
        "to set up the local renderer stack."
    ),
}
IDLE_SHUTDOWN_SEC = 600.0

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


def _unit_available(unit: str) -> bool:
    """True iff systemd knows about this unit file.

    Endpoint installs deliberately omit full-speaker renderer units. The
    sources page still exists there, but rows for absent units must be
    disabled and explicit rather than relying on a failing `systemctl start`.
    """
    rc, out = _systemctl("list-unit-files", unit, "--no-legend", timeout=5)
    if rc != 0:
        return False
    for line in out.splitlines():
        fields = line.split()
        if fields and fields[0] == unit:
            return True
    return False


def _unit_active(unit: str) -> bool:
    rc, out = _systemctl("is-active", unit, timeout=5)
    return rc == 0 and out == "active"


def _local_sources_allowed() -> bool:
    """True when this install role may run local source renderers."""
    try:
        return install_profile_allows_local_sources(read_install_profile())
    except ValueError as e:
        logger.warning("invalid install profile while rendering /sources: %s", e)
        return False


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


def _source_state(
    *, enabled: bool, available: bool, unavailable_reason: str = "",
) -> dict[str, bool | str]:
    state: dict[str, bool | str] = {
        "enabled": bool(enabled and available),
        "available": bool(available),
    }
    if not available and unavailable_reason:
        state["unavailableReason"] = unavailable_reason
    return state


def _systemd_source_state(source: str, unit: str) -> dict[str, bool | str]:
    available = _local_sources_allowed() and _unit_available(unit)
    return _source_state(
        enabled=_unit_active(unit) if available else False,
        available=available,
        unavailable_reason=SOURCE_UNAVAILABLE[source],
    )


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


def _gather_state() -> dict[str, dict[str, bool | str]]:
    """One-shot snapshot of all four sources. The BT branch runs an
    asyncio task because dbus-next is async-only; the rest are sync
    systemctl probes."""
    bt_available, bt_powered, bt_has_hid = asyncio.run(_bt_state())
    local_sources_allowed = _local_sources_allowed()
    usbsink_unit_available = (
        local_sources_allowed and _unit_available(USBSINK_UNIT)
    )
    usbsink_dtoverlay_available = (
        _usbsink_available() if usbsink_unit_available else False
    )
    usbsink_available = usbsink_unit_available and usbsink_dtoverlay_available
    if not usbsink_unit_available:
        usbsink_reason = SOURCE_UNAVAILABLE["usbsink"]
    elif not usbsink_dtoverlay_available:
        usbsink_reason = (
            "USB gadget mode is not enabled in /boot/firmware/config.txt. "
            "Re-run install.sh and reboot before enabling USB Audio Input."
        )
    else:
        usbsink_reason = ""
    bt_available_for_role = local_sources_allowed and bt_available
    if not local_sources_allowed:
        bt_unavailable_reason = SOURCE_UNAVAILABLE["bluetooth"]
    elif not bt_available:
        bt_unavailable_reason = "Bluetooth adapter not available on this device."
    else:
        bt_unavailable_reason = ""
    return {
        # Sibling key, not a source: the JS iterates a fixed SOURCES list,
        # so this rides alongside safely. Satellite-only installs park every
        # local source; bonded followers on full/streambox installs are parked
        # by the grouping reconciler. The page disables toggles and explains;
        # POST /set 409s.
        "pair": {"parked": bonded_follower_active()},
        "airplay": _systemd_source_state("airplay", AIRPLAY_UNIT),
        "bluetooth": {
            "enabled": bool(local_sources_allowed and bt_powered and bt_available),
            "available": bt_available_for_role,
            "hasPairedHid": bt_has_hid,
            **({} if bt_available_for_role else {
                "unavailableReason": bt_unavailable_reason,
            }),
        },
        "spotify_connect": _systemd_source_state(
            "spotify_connect", SPOTIFY_CONNECT_UNIT,
        ),
        "usbsink": _source_state(
            enabled=_unit_active(USBSINK_UNIT) if usbsink_available else False,
            available=usbsink_available,
            unavailable_reason=usbsink_reason,
        ),
    }


def _apply(source: str, enabled: bool) -> None:
    """Route the toggle to the right backend. Caller has already
    validated `source` is in VALID_SOURCES."""
    if source == "airplay":
        if not (_local_sources_allowed() and _unit_available(AIRPLAY_UNIT)):
            raise RuntimeError(SOURCE_UNAVAILABLE["airplay"])
        _set_unit(AIRPLAY_UNIT, enabled)
    elif source == "spotify_connect":
        if not (
            _local_sources_allowed() and _unit_available(SPOTIFY_CONNECT_UNIT)
        ):
            raise RuntimeError(SOURCE_UNAVAILABLE["spotify_connect"])
        _set_unit(SPOTIFY_CONNECT_UNIT, enabled)
    elif source == "bluetooth":
        if not _local_sources_allowed():
            raise RuntimeError(SOURCE_UNAVAILABLE["bluetooth"])
        asyncio.run(_set_bt(enabled))
    elif source == "usbsink":
        if not (_local_sources_allowed() and _unit_available(USBSINK_UNIT)):
            raise RuntimeError(SOURCE_UNAVAILABLE["usbsink"])
        if not _usbsink_available():
            raise RuntimeError(
                "USB gadget mode is not enabled in /boot/firmware/config.txt. "
                "Re-run install.sh and reboot before enabling USB Audio Input."
            )
        # jasper-usbsink.service Requires=+PartOf= the init.service, so
        # systemctl enable/disable --now propagates to both — the init
        # creates the ConfigFS gadget and loads libcomposite when on,
        # tears down + rmmods when off, returning RAM to baseline.
        _set_unit(USBSINK_UNIT, enabled)


# Per-page CSS layered on app.css. Just the source-row layout + notes; the
# toggle, card, header, and banner are shared primitives in app.css. Status
# colour is the one knob: the unavailable note reuses --status-warn.
_PAGE_CSS = """
.sources { display: flex; flex-direction: column; }
.source-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 1rem; padding: 0.9rem 0;
  border-bottom: 1px solid var(--border);
}
.source-row:last-child { border-bottom: none; }
.source-text { min-width: 0; }
.source-name { font-weight: 600; color: var(--text); }
.source-note { color: var(--muted); font-size: 0.9rem; margin-top: 0.2rem; }
.source-note.warn { color: var(--status-warn); }
.source-note code {
  font-size: 0.95em; padding: 1px 5px;
  border-radius: var(--radius-sm); background: var(--foreground-005);
}
"""


def _source_row(
    *, name: str, input_id: str, note_html: str = "", unavailable_html: str = "",
) -> str:
    """One source row: name + optional notes on the left, toggle on the
    right. The toggle is disabled at first paint; the ES module's /state
    poll hydrates checked/disabled within a poll cycle (mirrors the
    legacy behaviour)."""
    notes = ""
    if note_html:
        notes += note_html
    if unavailable_html:
        notes += unavailable_html
    return f"""
    <div class="source-row">
      <div class="source-text">
        <div class="source-name">{name}</div>
        {notes}
      </div>
      {toggle_html(input_id, disabled=True)}
    </div>
    """


def _index_html(csrf_token: str = "", *, status_msg: str = "") -> bytes:
    """Render the sources page. Initial toggle state is loaded from the
    server on the first /state poll (one extra round trip on page load —
    keeps the HTML static and cache-friendly)."""
    pair_note = (
        '<div class="info-card info-card--accent" id="pair-note" '
        'style="display:none" role="note">This speaker is part of a '
        "stereo pair — music plays through the pair leader, so local "
        "sources are parked. Unpair on "
        '<a href="/rooms/">the Speakers page</a> to use them again.'
        "</div>"
    )
    rows = "".join([
        _source_row(
            name="AirPlay", input_id="t-airplay",
            unavailable_html=(
                '<div class="source-note warn" id="airplay-unavailable-note" '
                'style="display:none">AirPlay is not installed on this speaker. '
                "Re-run install.sh to set up the local renderer stack.</div>"
            ),
        ),
        _source_row(
            name="Bluetooth", input_id="t-bluetooth",
            note_html=(
                '<div class="source-note warn" id="bt-note" style="display:none">'
                "Bluetooth adapter not available on this device.</div>"
            ),
        ),
        _source_row(
            name="Spotify Connect", input_id="t-spotify_connect",
            unavailable_html=(
                '<div class="source-note warn" '
                'id="spotify_connect-unavailable-note" style="display:none">'
                "Spotify Connect is not installed on this speaker. Re-run "
                "install.sh to set up the local renderer stack.</div>"
            ),
        ),
        _source_row(
            name="USB Audio Input", input_id="t-usbsink",
            note_html=(
                '<div class="source-note" id="usbsink-note">'
                "Plug a computer into the Pi's USB data/OTG port through a "
                "compatible power/data splitter or hub. Your computer sees "
                "the speaker as a USB audio output device.</div>"
            ),
            unavailable_html=(
                '<div class="source-note warn" id="usbsink-unavailable-note" '
                'style="display:none">USB gadget mode not enabled in '
                "<code>/boot/firmware/config.txt</code> — re-run install.sh "
                "and reboot.</div>"
            ),
        ),
    ])
    body = f"""
{canonical_header("Music sources")}
<main class="page">
  {canonical_banner(status_msg)}
  <p class="form-hint">Turn each playback source on or off. AirPlay and
  Spotify Connect persist across reboots; Bluetooth comes back on after a
  reboot (use this for runtime mute, not permanent disable). USB Audio
  Input is off by default — flip it on to use JTS as a USB audio output
  for a computer plugged into the Pi's USB data/OTG port through a
  compatible power/data splitter or hub.</p>

  <section class="info-card">
    <h2 class="section__title">Sources</h2>
    <div class="sources" id="sources">
      {pair_note}{rows}
    </div>
  </section>
</main>
<script type="module" src="/assets/sources/js/main.js"></script>
"""
    return canonical_page(
        "Music sources", body, csrf_token=csrf_token, page_css=_PAGE_CSS,
    )


def _make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

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
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                send_html_response(
                    self,
                    _index_html(ctx["csrf_token"], status_msg=ctx["flash"]),
                )
                return
            if path == "/state":
                if not guard_read_request(self):
                    return
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
                if not guard_mutating_request(self):
                    reject_csrf(self)
                    return
                body = self._read_json()
                source = str(body.get("source") or "")
                enabled = bool(body.get("enabled"))
                if source not in VALID_SOURCES:
                    self._send_json(
                        {"error": f"unknown source {source!r}"}, status=400,
                    )
                    return
                if bonded_follower_active():
                    # `enable --now` would START a parked renderer and
                    # reopen the advertise/leak hole until the next
                    # reconcile. Sources are pair-managed while bonded;
                    # intent changes happen after unpairing.
                    self._send_json(
                        {"error": "sources are managed by the stereo "
                                  "pair while this speaker is a "
                                  "follower — unpair on /rooms/ to "
                                  "change local sources"},
                        status=409,
                    )
                    return
                try:
                    _apply(source, enabled)
                except Exception as e:  # noqa: BLE001
                    logger.exception("toggle %s -> %s failed", source, enabled)
                    self._send_json({"error": str(e)}, status=502)
                    return
                log_event(
                    logger,
                    "sources.set",
                    source=source,
                    enabled=enabled,
                    client=self.address_string(),
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
    from . import _systemd
    sockets = _systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)
    server = make_server(target)

    tracker = _systemd.IdleShutdownTracker(
        idle_threshold_sec=IDLE_SHUTDOWN_SEC,
    )
    _systemd.install_request_idle_bump(server.RequestHandlerClass, tracker)
    tracker.start()

    if sockets:
        logger.info(
            "jasper-sources-web adopting systemd fd (idle=%ds)",
            int(IDLE_SHUTDOWN_SEC),
        )
    else:
        logger.info(
            "jasper-sources-web listening on http://%s:%d",
            args.host, args.port,
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
