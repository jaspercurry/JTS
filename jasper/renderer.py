"""Renderer state poller + transport dispatcher.

Consults each renderer daemon directly for its playback state:

  librespot     → /run/librespot/state.json (--onevent hook)
  shairport-sync → org.mpris.MediaPlayer2.ShairportSync DBus
  bluez-alsa    → bluealsa-cli list-pcms (subprocess)
  MPD           → python-mpd2 (rare on this box — only if user
                  installed mpd themselves for radio)

`RendererClient.active_renderers()` returns a dict with one boolean
per renderer (`spotactive`, `aplactive`, `btactive`, plus
backwards-compatible `slactive`/`rbactive` always-False keys for
squeezelite/roon-bridge that callers may still check).

Transport (next/prev/pause/play/toggle) routes to MPD if reachable.
For source-aware AirPlay/Spotify transport, callers should use
`jasper.tools.transport.make_transport_dispatcher`, which delegates
to MPRIS / Spotify Web API based on the active source.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from mpd.asyncio import MPDClient

from . import librespot_state
from .source_state import airplay_playing, bluetooth_playing, spotify_playing

logger = logging.getLogger(__name__)


class RendererClient:
    """Renderer state + control. Read-only state queries are
    fail-soft (log + return safe default on transport errors).
    Transport methods target MPD; source-aware routing across
    AirPlay/Spotify lives in `jasper.tools.transport`."""

    def __init__(
        self,
        *,
        mpd_host: str,
        mpd_port: int,
        librespot_state_path: str = librespot_state.DEFAULT_PATH,
    ) -> None:
        self._librespot_state_path = librespot_state_path
        self._mpd_host = mpd_host
        self._mpd_port = mpd_port
        self._mpd: MPDClient | None = None
        self._mpd_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # State queries — read-only, fail-soft. None of these methods raise
    # on transport errors; they log and return a safe default.
    # ------------------------------------------------------------------

    async def active_renderers(self) -> dict[str, bool]:
        """Returns a dict keyed by renderer name. slactive (squeezelite)
        and rbactive (roon bridge) are always False — neither is
        installed by default — and remain in the shape so callers
        that iterate the dict don't need backend-specific branches."""
        spot, ap, bt = await asyncio.gather(
            spotify_playing(self._librespot_state_path),
            airplay_playing(),
            bluetooth_playing(),
            return_exceptions=False,
        )
        return {
            "aplactive": ap,
            "btactive": bt,
            "spotactive": spot,
            "slactive": False,
            "rbactive": False,
        }

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
        # Bluetooth A2DP doesn't have reliable AVRCP metadata via
        # bluez-alsa; falling back to MPD currentsong covers the
        # rare case where MPD is the active source.
        try:
            song = dict(await self._mpd_call("currentsong"))
            return song
        except Exception as e:  # noqa: BLE001
            logger.debug("mpd currentsong failed: %s", e)
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

    async def status(self) -> dict[str, Any]:
        try:
            return dict(await self._mpd_call("status"))
        except Exception as e:  # noqa: BLE001
            logger.debug("mpd status failed: %s", e)
            return {}

    # ------------------------------------------------------------------
    # Transport — these MPD calls only fire for the MPD source. Without
    # MPD installed, MPD calls fail soft. Source-aware routing for
    # AirPlay/Spotify lives in jasper.tools.transport.
    # ------------------------------------------------------------------

    async def toggle_play_pause(self) -> None:
        # Only handles the MPD source (and is_playing→pause,
        # otherwise→play). Callers that need source-aware toggle
        # should use make_transport_dispatcher.dispatch("toggle").
        try:
            client = await self._mpd_client()
            status = await client.status()
            if status.get("state") == "play":
                await client.pause(1)
            else:
                await client.play()
        except Exception as e:  # noqa: BLE001
            logger.debug("mpd toggle failed (likely no mpd): %s", e)

    async def next_track(self) -> None:
        await self._mpd_call("next")

    async def previous_track(self) -> None:
        await self._mpd_call("previous")

    async def pause(self) -> None:
        await self._mpd_call("pause", 1)

    async def play(self) -> None:
        await self._mpd_call("play")

    # ------------------------------------------------------------------
    # pause_airplay — pauses an active AirPlay session via MPRIS so
    # another source can take the speaker. Spotify pause goes via the
    # Spotify Web API at the caller's spotify_router instance (librespot
    # has no local control HTTP); Bluetooth has no graceful pause API
    # on bluez-alsa, so its caller logs and moves on.
    # ------------------------------------------------------------------

    async def pause_airplay(self) -> None:
        await _busctl_call_method(
            "org.mpris.MediaPlayer2.ShairportSync",
            "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player",
            "Pause",
        )

    # ------------------------------------------------------------------
    # MPD plumbing — reconnects on disconnect; serialised via a lock
    # since python-mpd2's async client isn't reentrant.
    # ------------------------------------------------------------------

    async def _mpd_client(self) -> MPDClient:
        if self._mpd is None:
            client = MPDClient()
            await client.connect(self._mpd_host, self._mpd_port)
            self._mpd = client
        return self._mpd

    async def _mpd_call(self, fn_name: str, *args: Any) -> Any:
        async with self._mpd_lock:
            try:
                client = await self._mpd_client()
                return await getattr(client, fn_name)(*args)
            except Exception as e:  # noqa: BLE001
                logger.warning("mpd call failed, reconnecting: %s", e)
                self._mpd = None
                client = await self._mpd_client()
                return await getattr(client, fn_name)(*args)

    async def aclose(self) -> None:
        if self._mpd is not None:
            self._mpd.disconnect()
            self._mpd = None


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
