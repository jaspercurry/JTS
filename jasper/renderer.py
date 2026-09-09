# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Renderer state poller.

Consults each renderer daemon directly for its playback state:

  librespot     → /run/librespot/state.env (--onevent hook)
  shairport-sync → org.mpris.MediaPlayer2.ShairportSync DBus
  bluez         → org.bluez.MediaTransport1 presence (system bus)
  USB input       → jasper-fanin DIRECT lane STATUS

`RendererClient.active_renderers()` returns a dict with one boolean
per renderer (`spotactive`, `aplactive`, `btactive`,
`usbsinkactive`).

For source-aware AirPlay/Spotify/Bluetooth transport, callers should use
`jasper.tools.transport.make_transport_dispatcher`, which asks mux for
the effective audible source, then delegates to MPRIS / Spotify Web API
/ Bluetooth AVRCP as appropriate.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from . import librespot_state
from .busctl import system_busctl
from .music_sources import SOURCE_TO_ACTIVE_KEY, Source
from .platform.uds import mux_socket_command
from .source_state import (
    airplay_playing,
    bluetooth_playing,
    spotify_playing,
    usbsink_streaming,
)

logger = logging.getLogger(__name__)


async def airplay_now_playing() -> dict[str, str]:
    """Return Shairport's canonical MPRIS title/artist/album projection."""
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
        "title": str(meta.get("xesam:title", "")),
        "album": str(meta.get("xesam:album", "")),
        "artist": ", ".join(artists) if isinstance(artists, list) else str(artists),
    }


class RendererClient:
    """Renderer state. Every query is read-only and fail-soft (log +
    return a safe default on transport errors). Source-aware routing across
    AirPlay/Spotify lives in `jasper.tools.transport`; stopping a source is
    mux's, over its control socket."""

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
        """Return raw renderer activity keyed by the stable public names.

        ``usbsinkactive`` is fan-in's DIRECT-lane *streaming* edge, the same
        arbitration predicate mux uses — not the level predicate behind
        ``/state.renderers.usbsink.playing`` — so a caller falling back to
        these probes cannot pick a different winner than mux did.
        """
        spot, ap, bt, usb = await asyncio.gather(
            spotify_playing(self._librespot_state_path),
            airplay_playing(),
            bluetooth_playing(),
            usbsink_streaming(),
            return_exceptions=False,
        )
        return {
            SOURCE_TO_ACTIVE_KEY[Source.AIRPLAY]: ap,
            SOURCE_TO_ACTIVE_KEY[Source.BLUETOOTH]: bt,
            SOURCE_TO_ACTIVE_KEY[Source.SPOTIFY]: spot,
            SOURCE_TO_ACTIVE_KEY[Source.USBSINK]: usb,
        }

    async def selected_source(self) -> str | None:
        """Return mux's effective audible source, or None if unknown.

        The answer is mux's own ``active_source`` — the single field that
        already folds the test lease, the manual pin, and a winner that is
        still playing into one name. This is intentionally separate from
        `active_renderers()`, which reports raw renderer activity.

        Fail-soft: an unreachable mux, an unparseable reply, or a STATUS
        without the field all return ``None``.
        """
        try:
            # Seconds, TOTAL deadline. One bounded exchange per observer tick;
            # mux STATUS is a synchronous snapshot on the daemon's side.
            payload = await mux_socket_command("STATUS", timeout=1.0)
        except (OSError, RuntimeError, ValueError) as e:
            logger.debug("mux STATUS unavailable: %s", e)
            return None
        effective = payload.get("active_source")
        return effective if isinstance(effective, str) else None

    # ------------------------------------------------------------------
    # Currentsong — cascades by active source. Returns a dict with at
    # minimum "title", "album", "artist" keys that consumers
    # (transport.py, spotify_routing.py) read from. Empty dict on
    # error / no source.
    # ------------------------------------------------------------------

    async def get_currentsong(self) -> dict[str, Any]:
        active = await self.active_renderers()
        if active.get(SOURCE_TO_ACTIVE_KEY[Source.SPOTIFY]):
            return await self._spot_currentsong()
        if active.get(SOURCE_TO_ACTIVE_KEY[Source.AIRPLAY]):
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
        return await airplay_now_playing()


# ----------------------------------------------------------------------
# DBus helpers — busctl is in systemd, no extra dep. Subprocess output
# parsing is brittle but localized here; callers get clean Python types.
# ----------------------------------------------------------------------

async def _busctl_get_property(
    bus_name: str, object_path: str, interface: str, prop: str,
) -> str | None:
    stdout = await system_busctl(
        "call",
        bus_name, object_path,
        "org.freedesktop.DBus.Properties", "Get", "ss", interface, prop,
    )
    if stdout is None:
        logger.debug("busctl Get %s.%s failed", interface, prop)
        return None
    # busctl returns a single line like:  v s "Playing"
    # (variant of-string of-value). Strip the variant prefix.
    line = stdout.decode("utf-8", "replace").strip()
    m = re.match(r'^v\s+s\s+"(.*)"$', line)
    if m:
        return m.group(1)
    return line


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
