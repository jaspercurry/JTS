from __future__ import annotations

import asyncio
import logging
import re

from . import tool

logger = logging.getLogger(__name__)

# Shairport-sync exposes a standard MPRIS Player interface on the
# system DBus when built with --with-mpris-interface (confirmed
# present on the Pi). When AirPlay is the active source, calling
# Next/Previous/Pause/Play here forwards to the AirPlay sender
# (iPhone, Mac, etc.) via DACP — the same mechanism a HomePod uses
# to accept transport from the receiver side. So "next song" works
# uniformly whether the sender is Apple Music, Spotify, YouTube,
# a podcast app, or anything else casting via AirPlay.
MPRIS_DEST = "org.mpris.MediaPlayer2.ShairportSync"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
MPRIS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

# shairport-sync's gnome interface exposes a `RemoteControl.Available`
# property that is the AUTHORITATIVE signal for whether the AirPlay
# sender registered a DACP endpoint. MPRIS's own CanGoNext/CanGoPrevious
# always read true on this build of shairport, even when the sender
# can't actually accept remote-control commands — that flag is shaped
# for the abstract MPRIS contract, not the concrete AirPlay+DACP
# capability. Browser-based AirPlay sources (YouTube tab, Netflix,
# etc.) and some Mac sources don't expose DACP; the iPhone Music app
# and iPhone Spotify do. Pre-checking RemoteControl.Available means
# we tell the user "your computer doesn't accept remote control" up
# front instead of silently no-op'ing every Next/Previous call.
GNOME_DEST = "org.gnome.ShairportSync"
GNOME_PATH = "/org/gnome/ShairportSync"
GNOME_REMOTE_IFACE = "org.gnome.ShairportSync.RemoteControl"


async def _airplay_remote_available() -> bool:
    """True iff shairport's gnome RemoteControl reports Available=true,
    i.e. the AirPlay sender exposes a DACP endpoint."""
    proc = await asyncio.create_subprocess_exec(
        "dbus-send", "--system", "--print-reply",
        f"--dest={GNOME_DEST}",
        GNOME_PATH,
        f"{MPRIS_PROPS_IFACE}.Get",
        f"string:{GNOME_REMOTE_IFACE}",
        "string:Available",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    if proc.returncode != 0:
        # Treat unreadable as unavailable — better to tell the user
        # the sender doesn't accept control than to silently no-op.
        return False
    return b"boolean true" in stdout


async def _mpris_call(method: str) -> None:
    """Invoke a no-arg method on the shairport MPRIS Player interface."""
    proc = await asyncio.create_subprocess_exec(
        "dbus-send", "--system", "--print-reply",
        f"--dest={MPRIS_DEST}",
        MPRIS_PATH,
        f"{MPRIS_PLAYER_IFACE}.{method}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    if proc.returncode != 0:
        raise RuntimeError(
            f"mpris {method} failed: {stderr.decode(errors='replace').strip()}"
        )


_MPRIS_TITLE = re.compile(r'string\s+"xesam:title"\s*\n\s*variant\s+string\s+"([^"]*)"')
_MPRIS_ARTIST = re.compile(
    r'string\s+"xesam:artist"[^\[]*\[\s*\n\s*string\s+"([^"]*)"'
)
_MPRIS_ALBUM = re.compile(r'string\s+"xesam:album"\s*\n\s*variant\s+string\s+"([^"]*)"')


async def _mpris_now_playing() -> dict[str, str]:
    """Read shairport's MPRIS Metadata property and parse out title/artist/album."""
    proc = await asyncio.create_subprocess_exec(
        "dbus-send", "--system", "--print-reply",
        f"--dest={MPRIS_DEST}",
        MPRIS_PATH,
        f"{MPRIS_PROPS_IFACE}.Get",
        f"string:{MPRIS_PLAYER_IFACE}",
        "string:Metadata",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    if proc.returncode != 0:
        raise RuntimeError(
            f"mpris metadata failed: {stderr.decode(errors='replace').strip()}"
        )
    text = stdout.decode(errors="replace")
    title = (m.group(1) if (m := _MPRIS_TITLE.search(text)) else "")
    artist = (m.group(1) if (m := _MPRIS_ARTIST.search(text)) else "")
    album = (m.group(1) if (m := _MPRIS_ALBUM.search(text)) else "")
    return {"title": title, "artist": artist, "album": album}


async def _detect_source(moode) -> str:
    """Return the active playback source: 'airplay' / 'spotify' / 'bluetooth' / 'mpd'.

    Reads moOde's SQLite-backed renderer flags. Order matters when more
    than one is somehow active: airplay > spotify > bluetooth > mpd.
    """
    renderers = await moode.active_renderers()
    if renderers.get("aplactive"):
        return "airplay"
    if renderers.get("spotactive"):
        return "spotify"
    if renderers.get("btactive"):
        return "bluetooth"
    return "mpd"


def make_transport_tools(moode, router):
    """Source-aware transport tools.

    Routing logic (most-specific first):

      - **AirPlay sender matches a known household account** → call
        the matched account's Spotify Web API targeting their active
        device. iOS Spotify (and any other Spotify-on-iPhone-via-
        AirPlay session) is controllable via this path; iOS 17.4+
        broke the DACP/MPRIS path for AirPlay 2 (shairport-sync
        issue #1822), making Spotify Web API the canonical answer.
      - **AirPlay sender unmatched, but exposes DACP** → MPRIS
        Next/Previous/Pause/Play via shairport's dbus interface.
        Works for legacy AirPlay 1 senders and older iOS / Apple
        Music app builds.
      - **AirPlay sender unmatched and no DACP** → tell the user to
        use the controls on the device they're casting from.
      - **Spotify Connect (no AirPlay)** → router picks the right
        account by is_playing or default; spotipy targets that
        account's active device.
      - **MPD** → MoodeClient methods for radio / local files.
      - **Bluetooth** → not yet implemented.
    """

    async def _spotify_active_device_id(sp) -> str | None:
        try:
            devices = await asyncio.to_thread(sp.devices)
        except Exception as e:  # noqa: BLE001
            logger.warning("spotify devices fetch failed: %s", e)
            return None
        for d in devices.get("devices", []):
            if d.get("is_active"):
                return d.get("id")
        return None

    async def _spotify_call(sp, action: str, device_id: str | None) -> None:
        fn = {
            "next": sp.next_track,
            "previous": sp.previous_track,
            "pause": sp.pause_playback,
            "play": sp.start_playback,
        }[action]
        await asyncio.to_thread(fn, device_id=device_id)

    async def _dispatch(action: str) -> dict:
        source = await _detect_source(moode)
        logger.info("transport dispatch: action=%s source=%s", action, source)
        try:
            if source == "airplay":
                # 1) Try the matched-account Spotify Web API path.
                matched = (
                    await router.resolve_airplay() if router is not None else None
                )
                if matched is not None:
                    device_id = await _spotify_active_device_id(matched.sp)
                    await _spotify_call(matched.sp, action, device_id)
                    logger.info(
                        "airplay+spotify: %s routed to account=%s device_id=%s",
                        action, matched.account.name, device_id,
                    )
                    return {
                        "ok": True,
                        "source": "airplay+spotify",
                        "account": matched.account.name,
                    }
                # 2) Try DACP via MPRIS for senders that expose it.
                if not await _airplay_remote_available():
                    return {
                        "error": "the airplay sender isn't a recognized "
                        "household account, and the device doesn't accept "
                        "remote control. tell the user to use the controls "
                        "on the device they're casting from, or to set up "
                        "their spotify account at jasper.local/spotify.",
                        "source": "airplay",
                    }
                method = {
                    "next": "Next",
                    "previous": "Previous",
                    "pause": "Pause",
                    "play": "Play",
                }[action]
                await _mpris_call(method)
                return {"ok": True, "source": "airplay"}
            if source == "spotify":
                if router is None:
                    return {"error": "spotify not configured"}
                active = await router.active(airplay_active=False)
                if active is None:
                    return {"error": "no spotify account configured"}
                device_id = await _spotify_active_device_id(active.sp)
                await _spotify_call(active.sp, action, device_id)
                return {
                    "ok": True,
                    "source": "spotify",
                    "account": active.account.name,
                }
            if source == "bluetooth":
                return {
                    "error": "bluetooth transport not yet supported. "
                    "tell the user to use the controls on their phone.",
                }
            mpd_fn = {
                "next": moode.next_track,
                "previous": moode.previous_track,
                "pause": moode.pause,
                "play": moode.play,
            }[action]
            await mpd_fn()
            return {"ok": True, "source": "mpd"}
        except Exception as e:  # noqa: BLE001
            logger.warning("transport %s/%s failed: %s", source, action, e)
            return {"error": f"transport failed: {e}"}

    @tool()
    async def next_track() -> dict:
        """Skip to the next song."""
        return await _dispatch("next")

    @tool()
    async def previous_track() -> dict:
        """Go back to the previous song."""
        return await _dispatch("previous")

    @tool()
    async def pause() -> dict:
        """Pause / stop the currently playing music. Use for 'pause', 'stop', or any 'make it stop' phrasing."""
        return await _dispatch("pause")

    @tool()
    async def resume() -> dict:
        """Resume music that was paused. Only call on bare 'play' / 'resume' / 'keep playing' — do NOT call to start a new song or artist; for that, call spotify_play."""
        return await _dispatch("play")

    @tool()
    async def get_now_playing() -> dict:
        """Return metadata about the currently playing track (title, artist, album, source)."""
        source = await _detect_source(moode)
        try:
            if source == "airplay":
                # Prefer matched-account Spotify metadata when possible
                # — iOS Spotify doesn't push usable metadata over the
                # AirPlay channel, but the Web API has it.
                matched = (
                    await router.resolve_airplay() if router is not None else None
                )
                if matched is not None:
                    playback = await asyncio.to_thread(matched.sp.current_playback)
                    if playback and playback.get("item"):
                        item = playback["item"]
                        return {
                            "title": item.get("name", ""),
                            "artist": ", ".join(
                                a.get("name", "") for a in item.get("artists", [])
                            ),
                            "album": item.get("album", {}).get("name", ""),
                            "source": "airplay+spotify",
                            "account": matched.account.name,
                        }
                return {**await _mpris_now_playing(), "source": "airplay"}
            if source == "spotify" and router is not None:
                active = await router.active(airplay_active=False)
                if active is not None:
                    playback = await asyncio.to_thread(active.sp.current_playback)
                    if playback and playback.get("item"):
                        item = playback["item"]
                        return {
                            "title": item.get("name", ""),
                            "artist": ", ".join(
                                a.get("name", "") for a in item.get("artists", [])
                            ),
                            "album": item.get("album", {}).get("name", ""),
                            "source": "spotify",
                            "account": active.account.name,
                        }
                return {"title": "", "artist": "", "album": "", "source": "spotify"}
            song = await moode.get_currentsong()
            return {
                "title": song.get("title") or song.get("Title") or "",
                "artist": song.get("artist") or song.get("Artist") or "",
                "album": song.get("album") or song.get("Album") or "",
                "source": source,
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("get_now_playing(%s) failed: %s", source, e)
            return {"title": "", "artist": "", "album": "", "source": source, "error": str(e)}

    return [next_track, previous_track, pause, resume, get_now_playing]
