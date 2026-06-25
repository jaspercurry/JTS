# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import os
import re

from . import tool
from ..spotify_router import airplay_client_name

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
BLUEZ_DEST = "org.bluez"
BLUEZ_PLAYER_IFACE = "org.bluez.MediaPlayer1"

_BLUEZ_PLAYER_PATH_RE = re.compile(
    rb"(/org/bluez/hci\d+/dev_[A-F0-9_]+/player\d+)"
)
_BLUEALSA_A2DP_DEVICE_RE = re.compile(
    rb"/org/bluealsa/(hci\d+/dev_[A-F0-9_]+)/a2dpsnk/source"
)
_BUSCTL_QUOTED_VALUE_RE = re.compile(r'"([^"]*)"')


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


async def _bluetooth_active_device_path() -> str | None:
    """Return the BlueZ Device1 path for the active A2DP sink, if known.

    bluealsa-cli publishes paths under org.bluealsa; BlueZ publishes
    the matching AVRCP player under org.bluez. The hci/dev suffix is
    shared, so translate:
      /org/bluealsa/hci0/dev_AA_BB/... → /org/bluez/hci0/dev_AA_BB
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluealsa-cli", "list-pcms",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as e:
        logger.debug("bluealsa-cli list-pcms failed: %s", e)
        return None
    if proc.returncode != 0:
        return None
    match = _BLUEALSA_A2DP_DEVICE_RE.search(stdout)
    if match is None:
        return None
    return f"/org/bluez/{match.group(1).decode('ascii')}"


async def _bluetooth_player_paths() -> list[str]:
    """Return BlueZ AVRCP player object paths currently registered."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "tree", BLUEZ_DEST,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as e:
        logger.debug("bluez player tree failed: %s", e)
        return []
    if proc.returncode != 0:
        return []
    return sorted({
        match.group(1).decode("ascii")
        for match in _BLUEZ_PLAYER_PATH_RE.finditer(stdout)
    })


async def _bluetooth_player_path() -> str | None:
    """Return the active device's AVRCP player path, or the first player."""
    active_device = await _bluetooth_active_device_path()
    players = await _bluetooth_player_paths()
    if active_device is not None:
        prefix = active_device + "/"
        for path in players:
            if path.startswith(prefix):
                return path
    return players[0] if players else None


async def _bluetooth_player_status(path: str) -> str:
    """Read org.bluez.MediaPlayer1.Status, or empty string if unknown."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "get-property",
            BLUEZ_DEST, path, BLUEZ_PLAYER_IFACE, "Status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as e:
        logger.debug("bluez player status failed: %s", e)
        return ""
    if proc.returncode != 0:
        return ""
    match = _BUSCTL_QUOTED_VALUE_RE.search(stdout.decode("utf-8", "replace"))
    return match.group(1).lower() if match else ""


async def _bluetooth_call(method: str) -> None:
    """Invoke a no-arg AVRCP method on the active BlueZ MediaPlayer1."""
    path = await _bluetooth_player_path()
    if path is None:
        raise RuntimeError("bluetooth AVRCP player not available")
    if method == "PlayPause":
        status = await _bluetooth_player_status(path)
        method = "Pause" if status == "playing" else "Play"
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "call",
            BLUEZ_DEST, path, BLUEZ_PLAYER_IFACE, method,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as e:
        raise RuntimeError(f"bluetooth {method} failed: {e}") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"bluetooth {method} failed: "
            f"{stderr.decode(errors='replace').strip()}"
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


async def _detect_source(renderer) -> str:
    """Return the active playback source: 'airplay' / 'spotify' /
    'bluetooth' / 'none'.

    Reads the renderer's per-source flags. Order matters when more
    than one is somehow active: airplay > spotify > bluetooth.
    'none' means no renderer is currently producing audio.
    """
    renderers = await renderer.active_renderers()
    if renderers.get("aplactive"):
        return "airplay"
    if renderers.get("spotactive"):
        return "spotify"
    if renderers.get("btactive"):
        return "bluetooth"
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
        metadata = await _mpris_now_playing()
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
    behaves identically whether triggered by voice or by the dial.

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
        fn = {
            "next": sp.next_track,
            "previous": sp.previous_track,
            "pause": sp.pause_playback,
            "play": sp.start_playback,
        }[action]
        await asyncio.to_thread(fn, device_id=device_id)

    async def _spotify_toggle(sp, device_id: str | None) -> None:
        # Spotipy has no native toggle — query then dispatch.
        if await _spotify_is_playing(sp):
            await asyncio.to_thread(sp.pause_playback, device_id=device_id)
        else:
            await asyncio.to_thread(sp.start_playback, device_id=device_id)

    async def _dispatch(action: str) -> dict:
        source = await _detect_source(renderer)
        logger.info("transport dispatch: action=%s source=%s", action, source)
        # Used in two failure messages below; resolve once.
        hostname = os.environ.get("JASPER_HOSTNAME", "jts.local")
        try:
            if source == "airplay":
                matched = await _resolve_airplay_account(router)
                if matched is not None:
                    device_id = await _spotify_active_device_id(matched.sp)
                    if action == "toggle":
                        await _spotify_toggle(matched.sp, device_id)
                    else:
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
                # MPRIS PlayPause is a single-call native toggle —
                # cleaner than state-query-then-dispatch, and the
                # only path that works for AirPlay senders we can't
                # introspect (browser tabs, Apple Music, etc.).
                method = {
                    "next": "Next",
                    "previous": "Previous",
                    "pause": "Pause",
                    "play": "Play",
                    "toggle": "PlayPause",
                }[action]
                await _mpris_call(method)
                return {"ok": True, "source": "airplay"}
            if source == "spotify":
                if router is None:
                    return {"error": "spotify not configured"}
                # Lazy rebuild covers the post-revocation re-link path:
                # if the router went empty after a bad refresh, give it
                # one more chance before we tell the user it's broken.
                if not router.clients:
                    await router.refresh_if_empty()
                active = await router.active(airplay_active=False)
                if active is None:
                    if router.empty_reason() == "revoked":
                        from .spotify import _format_name_list
                        names = router.revoked_account_names()
                        who = (
                            _format_name_list(names) if names
                            else "your spotify account"
                        )
                        return {
                            "error": f"spotify signed {who} out. "
                            f"tell the user to re-link at {hostname}/spotify.",
                        }
                    return {"error": "no spotify account configured"}
                device_id = await _spotify_active_device_id(active.sp)
                if action == "toggle":
                    await _spotify_toggle(active.sp, device_id)
                else:
                    await _spotify_call(active.sp, action, device_id)
                return {
                    "ok": True,
                    "source": "spotify",
                    "account": active.account.name,
                }
            if source == "bluetooth":
                method = {
                    "next": "Next",
                    "previous": "Previous",
                    "pause": "Pause",
                    "play": "Play",
                    "toggle": "PlayPause",
                }[action]
                await _bluetooth_call(method)
                return {"ok": True, "source": "bluetooth"}
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
            # Bluetooth A2DP has no reliable AVRCP metadata; "none"
            # means nothing's playing. Either way, no metadata.
            return {"title": "", "artist": "", "album": "", "source": source}
        except Exception as e:  # noqa: BLE001
            logger.warning("get_now_playing(%s) failed: %s", source, e)
            return {"title": "", "artist": "", "album": "", "source": source, "error": str(e)}

    return [next_track, previous_track, pause, resume, get_now_playing]
