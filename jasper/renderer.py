# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Renderer state poller + AirPlay-pause control.

Consults each renderer daemon directly for its playback state:

  librespot     → /run/librespot/state.json (--onevent hook)
  shairport-sync → org.mpris.MediaPlayer2.ShairportSync DBus
  bluez-alsa    → bluealsa-cli list-pcms (subprocess)
  jasper-usbsink → /run/jasper-usbsink/state.json

`RendererClient.active_renderers()` returns a dict with one boolean
per renderer (`spotactive`, `aplactive`, `btactive`,
`usbsinkactive`).

For source-aware AirPlay/Spotify transport, callers should use
`jasper.tools.transport.make_transport_dispatcher`, which delegates
to MPRIS / Spotify Web API based on the active source.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from . import librespot_state
from .source_state import (
    airplay_playing,
    bluetooth_playing,
    spotify_playing,
    usbsink_playing,
)

logger = logging.getLogger(__name__)

MUX_CONTROL_SOCKET = "/run/jasper-mux/control.sock"


class RendererClient:
    """Renderer state + AirPlay-pause control. Read-only state queries
    are fail-soft (log + return safe default on transport errors).
    Source-aware routing across AirPlay/Spotify lives in
    `jasper.tools.transport`."""

    def __init__(
        self,
        *,
        librespot_state_path: str = librespot_state.DEFAULT_PATH,
    ) -> None:
        self._librespot_state_path = librespot_state_path

    # ------------------------------------------------------------------
    # State queries — read-only, fail-soft. None of these methods raise
    # on transport errors; they log and return a safe default.
    # ------------------------------------------------------------------

    async def active_renderers(self) -> dict[str, bool]:
        """Returns a dict keyed by renderer name. `usbsinkactive`
        is the playing state from /run/jasper-usbsink/state.json —
        False whenever the feature is disabled or the daemon hasn't
        produced a state file yet (e.g. during the brief boot window
        before jasper-usbsink starts publishing)."""
        spot, ap, bt, usb = await asyncio.gather(
            spotify_playing(self._librespot_state_path),
            airplay_playing(),
            bluetooth_playing(),
            usbsink_playing(),
            return_exceptions=False,
        )
        return {
            "aplactive": ap,
            "btactive": bt,
            "spotactive": spot,
            "usbsinkactive": usb,
        }

    async def selected_source(self) -> str | None:
        """Return mux's effective audible source, or None if unknown/idle.

        This is intentionally separate from `active_renderers()`, which
        reports raw renderer activity. Mux controls the audible fan-in
        lane in both manual mode and auto mode once a winner has been
        selected, so volume/dashboard callers should prefer this policy
        layer when it is available.
        """
        socket_path = os.environ.get(
            "JASPER_MUX_CONTROL_SOCKET", MUX_CONTROL_SOCKET,
        )
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
        except (FileNotFoundError, ConnectionRefusedError,
                asyncio.TimeoutError, OSError) as e:
            logger.debug("mux status unavailable: %s", e)
            return None
        try:
            writer.write(b"STATUS\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=1.0)
        except (asyncio.TimeoutError, ConnectionResetError, OSError) as e:
            logger.debug("mux STATUS failed: %s", e)
            return None
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        try:
            payload = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        selected = payload.get("selected_source")
        if isinstance(selected, str):
            return selected
        winner = payload.get("winner")
        return winner if isinstance(winner, str) else None

    # ------------------------------------------------------------------
    # Currentsong — cascades by active source. Returns a dict with at
    # minimum "title", "album", "artist" keys that consumers
    # (transport.py, spotify_routing.py) read from. Empty dict on
    # error / no source.
    # ------------------------------------------------------------------

    async def get_currentsong(self) -> dict[str, Any]:
        active = await self.active_renderers()
        if active.get("spotactive"):
            return await self._spot_currentsong()
        if active.get("aplactive"):
            return await self._ap_currentsong()
        # Bluetooth A2DP doesn't expose reliable AVRCP metadata via
        # bluez-alsa, and there's no other source we can introspect.
        return {}

    async def _spot_currentsong(self) -> dict[str, Any]:
        # librespot's --onevent hook only gives us TRACK_ID / URI;
        # title/artist/album require a Spotify Web API lookup.
        # Voice tools that need rich metadata go through
        # jasper.spotify_router (which already does Web API). For
        # the renderer's purposes we return the URI so transport
        # routing can identify the source as Spotify.
        uri = librespot_state.track_uri(self._librespot_state_path)
        if not uri:
            return {}
        return {
            "title": "",
            "album": "",
            "artist": "",
            "uri": uri,
        }

    async def _ap_currentsong(self) -> dict[str, Any]:
        out = await _busctl_get_property(
            "org.mpris.MediaPlayer2.ShairportSync",
            "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player",
            "Metadata",
        )
        if not out:
            return {}
        meta = _parse_mpris_metadata(out)
        artists = meta.get("xesam:artist") or []
        return {
            "title": meta.get("xesam:title", ""),
            "album": meta.get("xesam:album", ""),
            "artist": ", ".join(artists) if isinstance(artists, list) else str(artists),
        }

    # ------------------------------------------------------------------
    # pause_airplay — pauses an active AirPlay session via MPRIS so
    # another source can take the speaker. Spotify pause goes via the
    # Spotify Web API at the caller's spotify_router instance
    # (librespot has no local control HTTP); Bluetooth transport lives
    # in jasper.bluetooth.avrcp.
    # ------------------------------------------------------------------

    async def pause_airplay(self) -> None:
        await _busctl_call_method(
            "org.mpris.MediaPlayer2.ShairportSync",
            "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player",
            "Pause",
        )


# ----------------------------------------------------------------------
# DBus helpers — busctl is in systemd, no extra dep. Subprocess output
# parsing is brittle but localized here; callers get clean Python types.
# ----------------------------------------------------------------------

async def _busctl_get_property(
    bus_name: str, object_path: str, interface: str, prop: str,
) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "call",
            bus_name, object_path,
            "org.freedesktop.DBus.Properties", "Get", "ss", interface, prop,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        logger.debug("busctl Get %s.%s failed: %s", interface, prop, e)
        return None
    if proc.returncode != 0:
        return None
    # busctl returns a single line like:  v s "Playing"
    # (variant of-string of-value). Strip the variant prefix.
    line = stdout.decode("utf-8", "replace").strip()
    m = re.match(r'^v\s+s\s+"(.*)"$', line)
    if m:
        return m.group(1)
    return line


async def _busctl_call_method(
    bus_name: str, object_path: str, interface: str, method: str,
) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "call",
            bus_name, object_path, interface, method,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        logger.debug("busctl Call %s.%s failed: %s", interface, method, e)
        return False
    return proc.returncode == 0


_MPRIS_KV_RE = re.compile(r'"([^"]+)"\s+(\w[\w\d]*)\s+([^"]*?(?:"[^"]*"\s*)*)')


def _parse_mpris_metadata(busctl_out: str) -> dict[str, Any]:
    """Best-effort parser for busctl's MPRIS Metadata output.

    Format example (single line, soft-wrapped here):
        v a{sv} 5 "mpris:trackid" o "/org/.../A" \
            "xesam:title" s "PROSTITUTE" \
            "xesam:album" s "PROSTITUTE" \
            "xesam:artist" as 1 "Labrinth" \
            "mpris:length" x 164610000

    We pick out the keys we care about (xesam:title, xesam:album,
    xesam:artist) and ignore the rest.
    """
    result: dict[str, Any] = {}
    # xesam:title  s "..."
    for key in ("xesam:title", "xesam:album"):
        m = re.search(rf'"{re.escape(key)}"\s+s\s+"([^"]*)"', busctl_out)
        if m:
            result[key] = m.group(1)
    # xesam:artist  as N "v1" "v2" ... — N is exact count; can't use
    # a greedy quoted-string match because the next key (e.g.
    # "mpris:length") also looks like a quoted string and would get
    # swept in.
    m = re.search(r'"xesam:artist"\s+as\s+(\d+)\s+', busctl_out)
    if m:
        count = int(m.group(1))
        rest = busctl_out[m.end():]
        items: list[str] = []
        pos = 0
        for _ in range(count):
            sub = re.search(r'"([^"]*)"', rest[pos:])
            if not sub:
                break
            items.append(sub.group(1))
            pos += sub.end()
        result["xesam:artist"] = items
    return result
