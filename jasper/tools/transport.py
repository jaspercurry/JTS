# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging

from ..busctl import run_busctl
from ..source_state import (
    GNOME_DEST, GNOME_PATH, GNOME_REMOTE_IFACE, MPRIS_DEST, MPRIS_PATH, MPRIS_PLAYER_IFACE,
)
from ..identity.reader import resolve_hostname
from ..music_sources import SOURCE_TO_ACTIVE_KEY, Source
from ..bluetooth.avrcp import bluetooth_avrcp_call as _bluetooth_call
from ..renderer import airplay_now_playing
from . import tool
from .spotify import ensure_clients, no_account_msg
from ..spotify_router import airplay_client_name

logger = logging.getLogger(__name__)

_PLAYER_METHODS = {
    "next": "Next",
    "previous": "Previous",
    "pause": "Pause",
    "play": "Play",
    "toggle": "PlayPause",
}


async def _airplay_remote_available() -> bool:
    """True iff shairport's gnome RemoteControl reports Available=true,
    i.e. the AirPlay sender exposes a DACP endpoint."""
    result = await run_busctl(
        "get-property", GNOME_DEST, GNOME_PATH, GNOME_REMOTE_IFACE, "Available",
        timeout=2.0,
    )
    return result is not None and result.returncode == 0 and result.stdout.strip() == b"b true"


async def _mpris_call(method: str) -> None:
    """Invoke a no-arg method on the shairport MPRIS Player interface."""
    result = await run_busctl(
        "call", MPRIS_DEST, MPRIS_PATH, MPRIS_PLAYER_IFACE, method, timeout=2.0,
    )
    if result is None:
        raise RuntimeError(f"mpris {method} unavailable or timed out")
    if result.returncode != 0:
        raise RuntimeError(
            f"mpris {method} failed: {result.stderr.decode(errors='replace').strip()}"
        )


async def _detect_source(renderer) -> str:
    """Return the active playback source: 'airplay' / 'spotify' /
    'bluetooth' / 'usbsink' / 'none'.

    Prefer mux's effective audible source when available: manual source
    selection and guarded handoff policy live there. Fall back to raw
    renderer flags for older RendererClient fakes or when mux is
    unavailable. Raw-probe priority matters when more than one renderer
    is somehow active: airplay > spotify > bluetooth > usbsink.
    'none' means no renderer is currently producing audio that transport
    can target.
    """
    selected_source = getattr(renderer, "selected_source", None)
    if selected_source is not None:
        try:
            selected = await selected_source()
        except Exception as e:  # noqa: BLE001
            logger.debug("selected_source failed; falling back to probes: %s", e)
        else:
            if selected in {"airplay", "spotify", "bluetooth", "usbsink"}:
                return selected
            if selected == "idle":
                return "none"
            if selected:
                logger.debug("selected_source returned unknown source %r", selected)
    renderers = await renderer.active_renderers()
    for source in (
        Source.AIRPLAY,
        Source.SPOTIFY,
        Source.BLUETOOTH,
        Source.USBSINK,
    ):
        if renderers.get(SOURCE_TO_ACTIVE_KEY[source]):
            return source.value
    return "none"


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


async def _resolve_airplay_account(router):
    """Cross-reference MPRIS title with each account's current_playback.
    Returns None if router unconfigured or no title-match found.
    Module-level so both make_transport_dispatcher (for routing) and
    make_transport_tools.get_now_playing (for metadata) can call it
    without duplicating the closure."""
    if router is None:
        return None
    client_name = await airplay_client_name()
    if not client_name:
        return None
    try:
        metadata = await airplay_now_playing()
    except (RuntimeError, asyncio.TimeoutError, FileNotFoundError):
        return None
    title = metadata.get("title", "")
    if not title:
        return None
    return await router.resolve_for_transport(client_name, title)


def make_transport_dispatcher(renderer, router):
    """Returns `async dispatch(action) -> dict`, the source-aware
    transport routing function. Both the voice-tool decorators
    (make_transport_tools) and external callers (jasper-control's
    HTTP toggle endpoint) share this implementation so that `pause`
    behaves identically whether triggered by voice or by the remote.

    Routing logic for AirPlay (the interesting case):

      - Cross-reference shairport's MPRIS `xesam:title` against each
        configured account's `current_playback.item.name`. If exactly
        one matches, that's the AirPlay sender — route Next/Previous/
        Pause/Play/Toggle to that account via the Spotify Web API.
      - If no Spotify account is playing the AirPlay-pushed track,
        the sender is something else (Apple Music, podcast, browser
        tab). Try DACP via shairport's MPRIS — works for legacy
        AirPlay 1 and older Apple Music builds; silently no-ops on
        iOS 17.4+ Spotify (shairport-sync #1822), but those will
        have hit the title-match path above.
      - If DACP isn't available either, tell the user to use the
        controls on the device they're casting from.

    Spotify Connect (no AirPlay): router picks the active or default
    account; spotipy targets that account's active device.

    Bluetooth: AVRCP via BlueZ MediaPlayer1 when the source phone
    exposes a player object. No-source: error response telling the
    model nothing is playing.

    Toggle action: query the current is-playing state for the active
    source and dispatch pause-or-play accordingly. MPRIS exposes a
    native PlayPause method which is preferred for non-Spotify AirPlay.
    """

    async def _spotify_is_playing(sp) -> bool:
        try:
            playback = await asyncio.to_thread(sp.current_playback)
        except Exception as e:  # noqa: BLE001
            logger.warning("spotify current_playback failed: %s", e)
            return False
        return bool(playback and playback.get("is_playing"))

    async def _spotify_call(sp, action: str, device_id: str | None) -> None:
        if action == "toggle":
            action = "pause" if await _spotify_is_playing(sp) else "play"
        fn = {
            "next": sp.next_track,
            "previous": sp.previous_track,
            "pause": sp.pause_playback,
            "play": sp.start_playback,
        }[action]
        await asyncio.to_thread(fn, device_id=device_id)

    async def _dispatch(action: str) -> dict:
        source = await _detect_source(renderer)
        logger.info("transport dispatch: action=%s source=%s", action, source)
        # Used in two failure messages below; resolve once.
        hostname = resolve_hostname()
        try:
            if source == "airplay":
                matched = await _resolve_airplay_account(router)
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
                # No Spotify account playing the AirPlay track — try
                # DACP for non-Spotify senders that expose it.
                if not await _airplay_remote_available():
                    return {
                        "error": "the airplay sender isn't playing a track "
                        "from any configured spotify account, and the device "
                        "doesn't accept remote control. tell the user to use "
                        "the controls on the device they're casting from, or "
                        f"to link their spotify account at {hostname}/spotify.",
                        "source": "airplay",
                    }
                await _mpris_call(_PLAYER_METHODS[action])
                return {"ok": True, "source": "airplay"}
            if source == "spotify":
                active = None
                if await ensure_clients(router):
                    active = await router.active(airplay_active=False)
                if active is None:
                    return {"error": no_account_msg(router, f"{hostname}/spotify")}
                device_id = await _spotify_active_device_id(active.sp)
                await _spotify_call(active.sp, action, device_id)
                return {
                    "ok": True,
                    "source": "spotify",
                    "account": active.account.name,
                }
            if source == "bluetooth":
                await _bluetooth_call(_PLAYER_METHODS[action])
                return {"ok": True, "source": "bluetooth"}
            if source == "usbsink":
                return {
                    "error": (
                        "usb audio input is playing from the host computer; "
                        "control playback on the computer."
                    ),
                    "source": "usbsink",
                }
            # source == "none" — no renderer is currently producing
            # audio, so there's nothing to pause/skip.
            return {
                "error": "nothing is playing right now. "
                "use spotify_play to start a track.",
                "source": "none",
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("transport %s/%s failed: %s", source, action, e)
            return {"error": f"transport failed: {e}"}

    return _dispatch


def make_transport_tools(renderer, router):
    """Voice-side tool wrappers around the transport dispatcher."""
    _dispatch = make_transport_dispatcher(renderer, router)

    @tool(labels=("music", "playback", "transport"))
    async def next_track() -> dict:
        """Skip to the next song.

        Voice answer style: 'Skipping.' One word. No preamble.
        On error speak the `error` field verbatim.
        """
        return await _dispatch("next")

    @tool(labels=("music", "playback", "transport"))
    async def previous_track() -> dict:
        """Go back to the previous song.

        Voice answer style: 'Going back.' Two words. No preamble.
        On error speak the `error` field verbatim.
        """
        return await _dispatch("previous")

    @tool(labels=("music", "playback", "transport"))
    async def pause() -> dict:
        """Pause / stop the currently playing music. Use for 'pause',
        'stop', or any 'make it stop' phrasing.

        Voice answer style: 'Paused.' One word. No preamble.
        On error speak the `error` field verbatim.
        """
        return await _dispatch("pause")

    @tool(labels=("music", "playback", "transport"))
    async def resume() -> dict:
        """Resume music that was paused. Only call on bare 'play' /
        'resume' / 'keep playing' — do NOT call to start a new song
        or artist; for that, call spotify_play.

        Voice answer style: 'Resuming.' One word. No preamble.
        On error speak the `error` field verbatim.
        """
        return await _dispatch("play")

    @tool(labels=("music", "playback", "transport"))
    async def get_now_playing() -> dict:
        """Return metadata about the currently playing track (title,
        artist, album, source).

        Use for "what's playing?", "who is this?", "what song is
        this?". DO NOT call as a chaser after spotify_play —
        Spotify's current_playback lags by several seconds and may
        report the previous track.

        Voice answer style: '<title> by <artist>.' or '<title> by
        <artist> from <album>' for richer queries. If `title` is
        empty, say "Nothing is playing right now."
        """
        source = await _detect_source(renderer)
        try:
            if source == "airplay":
                matched = await _resolve_airplay_account(router)
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
                return {**await airplay_now_playing(), "source": "airplay"}
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
            # Bluetooth A2DP has no reliable AVRCP metadata; "none"
            # means nothing's playing. Either way, no metadata.
            return {"title": "", "artist": "", "album": "", "source": source}
        except Exception as e:  # noqa: BLE001
            logger.warning("get_now_playing(%s) failed: %s", source, e)
            return {"title": "", "artist": "", "album": "", "source": source, "error": str(e)}

    return [next_track, previous_track, pause, resume, get_now_playing]
