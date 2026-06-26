# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Smart Spotify-target resolver: decides which device_id to send
start_playback to, and what (if anything) needs to stop first.

Three real-world cases:

1. User is currently AirPlaying Spotify from their phone to the Pi:
   the renderer reports aplactive=1; currentsong has the AirPlay
   metadata; Spotify Web API also reports something is_playing. If
   the AirPlay track and the Spotify track match (by title + artist),
   the AirPlay session IS Spotify — target the active Spotify device
   (the phone) so the song change rides the existing AirPlay stream.
   The user's iPhone hardware volume buttons keep working.

2. User is AirPlaying NON-Spotify (Apple Music, YouTube, podcast)
   or has Bluetooth A2DP active: stop that source first, then start
   librespot on the Pi. Voice command is an exclusive override —
   never two streams at once.

3. Nothing is playing on the Pi: start librespot. Plain case.

The matcher in `_match_track` is conservative but title-only: Spotify
and AirPlay often disagree on artist strings for collaborations,
remasters, or "Various Artists" compilations. A Spotify session paused
on the user's laptop (with the same song that's shown by AirPlay
coincidentally) can't fool it as long as Spotify reports
`is_playing=False` for that laptop session.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from .bluetooth.avrcp import bluetooth_avrcp_call

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
    """True iff the AirPlay-reported title matches Spotify's currently-playing
    title. Title-only on purpose: artist strings get butchered across services
    (collaborations, "Various Artists", remaster suffixes), so requiring artist
    match produced too many false negatives. Two simultaneously-playing
    sessions of the same song title are vanishingly rare, and the
    `is_playing=True` guard already eliminates stale paused-session
    coincidences."""
    if not spotify_playback or not spotify_playback.get("is_playing"):
        return False
    item = spotify_playback.get("item") or {}
    sp_title = item.get("name", "")
    ap_title = airplay_song.get("title") or airplay_song.get("Title") or ""

    if not (sp_title and ap_title):
        return False
    return _normalise(sp_title) == _normalise(ap_title)


def _find_librespot_id(devices: list[dict], name_pattern: str) -> str | None:
    pattern = name_pattern.lower()
    for d in devices:
        if pattern in (d.get("name") or "").lower():
            return d.get("id")
    return None


async def resolve_target(
    sp,                                    # spotipy.Spotify
    renderer,                                 # RendererClient
    librespot_name_pattern: str,
) -> Resolution:
    """Determine the Spotify device_id to target and what (if anything)
    needs to stop first. All four data fetches run in parallel —
    total latency ≈ slowest single call (~200ms)."""

    async def _spotify_playback():
        return await asyncio.to_thread(sp.current_playback)

    async def _spotify_devices():
        return await asyncio.to_thread(sp.devices)

    renderers, playback, devices, song = await asyncio.gather(
        renderer.active_renderers(),
        _spotify_playback(),
        _spotify_devices(),
        renderer.get_currentsong(),
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

    # librespot already active or nothing playing → just target librespot.
    return Resolution(
        device_id=librespot_id,
        stop_renderers=[],
        reason="librespot active or idle" if renderers.get("spotactive") else "idle",
    )


async def stop_renderers(renderer, names: list[str]) -> None:
    """Stop the renderers named in `names`. Names match
    Resolution.stop_renderers values: airplay → pause_airplay()
    (MPRIS Pause on shairport-sync); bluetooth → BlueZ AVRCP Pause
    when the source phone/player exposes a MediaPlayer1 object. After
    pausing AirPlay the service takes a beat to release the audio device
    — a small sleep avoids a race where librespot starts while
    shairport-sync is still draining."""
    for name in names:
        try:
            if name == "airplay":
                await renderer.pause_airplay()
            elif name == "bluetooth":
                await bluetooth_avrcp_call("Pause")
            else:
                logger.warning("unknown renderer to stop: %s", name)
        except Exception as e:  # noqa: BLE001
            logger.warning("stop_renderers(%s) failed: %s", name, e)
    if "airplay" in names:
        await asyncio.sleep(0.25)
