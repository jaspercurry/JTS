"""Smart Spotify-target resolver: decides which device_id to send
start_playback to, and what (if anything) needs to stop first.

Three real-world cases:

1. User is currently AirPlaying Spotify from their phone to the Pi:
   moOde's renderer state has aplactive=1; moOde's currentsong has the
   AirPlay metadata; Spotify Web API also reports something is_playing.
   If the AirPlay track and the Spotify track match (by title + artist),
   the AirPlay session IS Spotify — target the active Spotify device
   (the phone) so the song change rides the existing AirPlay stream.
   The user's iPhone hardware volume buttons keep working.

2. User is AirPlaying NON-Spotify (Apple Music, YouTube, podcast),
   or has Bluetooth A2DP active, or moOde MPD is playing a radio
   station: stop that source first, then start librespot on the Pi.
   Voice command is an exclusive override — never two streams at once.

3. Nothing is playing on the Pi: start librespot. Plain case.

The matcher in `_match_track` is conservative — title AND artist must
both align after normalisation. A Spotify session paused on the user's
laptop (with the same song that's shown by AirPlay coincidentally)
can't fool it as long as Spotify reports `is_playing=False` for that
laptop session.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Resolution:
    device_id: str | None
    stop_renderers: list[str] = field(default_factory=list)
    reason: str = ""


_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalise(s: str) -> str:
    """Lowercase, strip leading 'the/a/an', collapse non-alphanumeric to single
    spaces, trim. Two strings normalise to the same value if they're 'the same
    thing' for human matching purposes."""
    if not s:
        return ""
    s = s.lower().strip()
    s = _LEADING_ARTICLE.sub("", s)
    s = _NON_ALNUM.sub(" ", s)
    return s.strip()


def _match_track(airplay_song: dict, spotify_playback: dict | None) -> bool:
    """True iff the AirPlay-reported metadata matches the Spotify-reported
    currently-playing track. Conservative: title AND artist must both align,
    Spotify must be is_playing, both fields non-empty."""
    if not spotify_playback or not spotify_playback.get("is_playing"):
        return False
    item = spotify_playback.get("item") or {}
    sp_title = item.get("name", "")
    sp_artists = item.get("artists") or []
    sp_artist = sp_artists[0].get("name", "") if sp_artists else ""

    ap_title = airplay_song.get("title") or airplay_song.get("Title") or ""
    ap_artist = airplay_song.get("artist") or airplay_song.get("Artist") or ""

    if not (sp_title and sp_artist and ap_title and ap_artist):
        return False
    return (
        _normalise(sp_title) == _normalise(ap_title)
        and _normalise(sp_artist) == _normalise(ap_artist)
    )


def _find_librespot_id(devices: list[dict], name_pattern: str) -> str | None:
    pattern = name_pattern.lower()
    for d in devices:
        if pattern in (d.get("name") or "").lower():
            return d.get("id")
    return None


async def resolve_target(
    sp,                                    # spotipy.Spotify
    moode,                                 # MoodeClient
    librespot_name_pattern: str,
) -> Resolution:
    """Determine the Spotify device_id to target and what (if anything) on
    moOde needs to stop first. All four data fetches run in parallel —
    total latency ≈ slowest single call (~200ms)."""

    async def _spotify_playback():
        return await asyncio.to_thread(sp.current_playback)

    async def _spotify_devices():
        return await asyncio.to_thread(sp.devices)

    renderers, playback, devices, song = await asyncio.gather(
        moode.active_renderers(),
        _spotify_playback(),
        _spotify_devices(),
        moode.get_currentsong(),
        return_exceptions=True,
    )
    # Defensive: any of the four can fail; we treat exceptions as
    # "nothing known" rather than aborting the whole resolution.
    if isinstance(renderers, Exception):
        logger.debug("active_renderers failed: %s", renderers)
        renderers = {}
    if isinstance(playback, Exception):
        logger.debug("current_playback failed: %s", playback)
        playback = None
    if isinstance(devices, Exception):
        logger.debug("devices failed: %s", devices)
        devices = {"devices": []}
    if isinstance(song, Exception):
        logger.debug("get_currentsong failed: %s", song)
        song = {}

    librespot_id = _find_librespot_id(devices.get("devices", []), librespot_name_pattern)

    if renderers.get("aplactive"):
        if _match_track(song, playback):
            return Resolution(
                device_id=playback["device"]["id"],
                stop_renderers=[],
                reason="airplay carrying spotify (metadata match)",
            )
        return Resolution(
            device_id=librespot_id,
            stop_renderers=["airplay"],
            reason="airplay carrying non-spotify",
        )

    if renderers.get("btactive"):
        return Resolution(
            device_id=librespot_id,
            stop_renderers=["bluetooth"],
            reason="bluetooth active",
        )

    # MPD playing a radio/local track? (state=play, file is a real path —
    # not an "Airplay Active"/"Bluetooth Active" placeholder.)
    state = (song.get("state") or "").lower()
    file_field = song.get("file") or ""
    is_placeholder = file_field.lower().startswith(("airplay", "bluetooth"))
    if state == "play" and file_field and not is_placeholder and not renderers.get("spotactive"):
        return Resolution(
            device_id=librespot_id,
            stop_renderers=["mpd"],
            reason="mpd playing local/radio",
        )

    # librespot already active or nothing playing → just target librespot.
    return Resolution(
        device_id=librespot_id,
        stop_renderers=[],
        reason="librespot active or idle" if renderers.get("spotactive") else "idle",
    )


async def stop_renderers(moode, names: list[str]) -> None:
    """Stop the moOde renderers named in `names`. Names match
    Resolution.stop_renderers values. mpd is paused; airplay/bluetooth
    are disabled via renderer_onoff. After disabling the service takes
    a beat to actually release the audio device — small sleep avoids a
    race where librespot starts while shairport-sync is still draining."""
    for name in names:
        try:
            if name == "mpd":
                await moode.pause()
            elif name in ("airplay", "bluetooth"):
                await moode.disable_renderer(name)
            else:
                logger.warning("unknown renderer to stop: %s", name)
        except Exception as e:  # noqa: BLE001
            logger.warning("stop_renderers(%s) failed: %s", name, e)
    if any(n in ("airplay", "bluetooth") for n in names):
        await asyncio.sleep(0.25)
