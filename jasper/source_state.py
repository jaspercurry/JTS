"""Source-state probes for the four music renderers.

Each `<source>_playing()` returns True iff that renderer is
currently producing audio. Probes are fail-soft: any transport
error (missing daemon, missing CLI, timeout, parse miss) is
logged at debug and returns False.

Both `jasper.renderer.RendererClient.active_renderers` (consumed
by voice tools, transport, volume coordinator) and `jasper.mux`'s
source-arbiter tick loop call into here. The probes had lived
duplicated in those two modules; consolidating them here gives
both callers a single shape to depend on and one place to evolve
when daemons change.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass

from . import librespot_state

logger = logging.getLogger(__name__)


# Match a non-empty xesam:title in busctl's MPRIS Metadata output.
# Format is a single line containing key/type/value triples; the
# title appears as:  "xesam:title" s "Some Song Name"
# (string type indicator `s`, then quoted value).
# Empty metadata renders as `v a{sv} 0\n` with no title key at all,
# so a search-fail is the phantom signal.
_AIRPLAY_TITLE_RE = re.compile(rb'"xesam:title"\s+s\s+"([^"]+)"')
_DBUS_STRING_RE = re.compile(rb'"((?:[^"\\]|\\.)*)"')
_DBUS_BOOL_RE = re.compile(rb"\b(true|false)\b")

SHAIRPORT_MPRIS_BUS = "org.mpris.MediaPlayer2.ShairportSync"
SHAIRPORT_MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
SHAIRPORT_GNOME_BUS = "org.gnome.ShairportSync"
SHAIRPORT_GNOME_PATH = "/org/gnome/ShairportSync"
SHAIRPORT_REMOTE_IFACE = "org.gnome.ShairportSync.RemoteControl"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

# Default path the jasper-usbsink daemon publishes its state to.
# Kept in sync with jasper.usbsink.state_publisher.DEFAULT_STATE_PATH.
# Both definitions exist so neither module pulls the other into its
# import graph (jasper-mux doesn't need to import the usbsink daemon
# just to know where its state file is).
USBSINK_STATE_PATH = "/run/jasper-usbsink/state.json"


@dataclass(frozen=True)
class AirPlaySessionState:
    """Receiver-side AirPlay session state, separate from audibility.

    `connected` answers "does shairport-sync still have a sender
    session?". It is intentionally broader than `airplay_playing()`,
    which answers "is AirPlay producing audible music right now?".
    Mux uses this for cleanup only; source arbitration still depends on
    `airplay_playing()` to avoid phantom SETUP flapping.
    """

    connected: bool
    client_name: str = ""
    player_state: str = ""
    remote_control_available: bool | None = None
    probed: bool = True


async def spotify_playing(
    librespot_state_path: str = librespot_state.DEFAULT_PATH,
) -> bool:
    """librespot writes /run/librespot/state.json on every player
    event via its --onevent hook. Reading on every probe is cheap
    (file is a few hundred bytes); is_playing returns False on
    missing/malformed file."""
    return librespot_state.is_playing(librespot_state_path)


def _airplay_metadata_gate_disabled() -> bool:
    """Env-var escape hatch for the metadata-corroboration predicate.

    Set JASPER_AIRPLAY_METADATA_GATE=disabled to revert airplay_playing()
    to its pre-2026-05-22 contract (PlaybackStatus alone). Useful if a
    field condition is found where shairport's xesam:title is genuinely
    empty during real audio (so far no such case is known) and the
    full revert is needed without a redeploy.
    """
    return os.environ.get(
        "JASPER_AIRPLAY_METADATA_GATE", "",
    ).strip().lower() == "disabled"


def _parse_dbus_string(stdout: bytes) -> str:
    m = _DBUS_STRING_RE.search(stdout)
    if not m:
        return ""
    # busctl/dbus-send escape embedded quotes and backslashes. We only
    # need a readable operator-facing value, not a complete DBus parser.
    text = m.group(1).decode("utf-8", "replace")
    return text.replace(r"\"", '"').replace(r"\\", "\\")


def _parse_dbus_bool(stdout: bytes) -> bool | None:
    m = _DBUS_BOOL_RE.search(stdout)
    if not m:
        return None
    return m.group(1) == b"true"


async def _airplay_remote_property(name: str) -> bytes | None:
    """Read a shairport-sync GNOME RemoteControl property via DBus."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "call",
            SHAIRPORT_GNOME_BUS,
            SHAIRPORT_GNOME_PATH,
            DBUS_PROPS_IFACE, "Get", "ss",
            SHAIRPORT_REMOTE_IFACE, name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
        logger.debug("busctl RemoteControl.%s probe failed: %s", name, e)
        return None
    if proc.returncode != 0:
        return None
    return stdout


async def airplay_session_state() -> AirPlaySessionState:
    """Return whether shairport-sync still has an AirPlay sender session.

    This is deliberately not an audio-active probe. A macOS/iOS sender
    can leave the AP2 session connected after the user switches the
    speaker to another protocol. We use shairport-sync's GNOME DBus
    RemoteControl surface because it exposes sender/session details
    (`ClientName`, `PlayerState`, `Available`) that MPRIS playback
    status alone cannot distinguish.

    Fail-soft: if DBus cannot be read, return disconnected. The mux
    still preempts AirPlay when `airplay_playing()` is true; this probe
    only adds cleanup for the connected-but-not-audible case.
    """
    client_raw, player_raw, available_raw = await asyncio.gather(
        _airplay_remote_property("ClientName"),
        _airplay_remote_property("PlayerState"),
        _airplay_remote_property("Available"),
    )
    probed = any(
        raw is not None for raw in (client_raw, player_raw, available_raw)
    )
    client_name = _parse_dbus_string(client_raw or b"")
    player_state = _parse_dbus_string(player_raw or b"")
    available = _parse_dbus_bool(available_raw or b"")
    connected = (
        bool(client_name.strip())
        or available is True
        or player_state.strip().lower() in {"playing", "paused"}
    )
    return AirPlaySessionState(
        connected=connected,
        client_name=client_name,
        player_state=player_state,
        remote_control_available=available,
        probed=probed,
    )


async def _airplay_has_metadata_title() -> bool:
    """True iff shairport-sync's MPRIS Metadata carries a non-empty
    xesam:title at the moment we ask.

    Corroborates PlaybackStatus when distinguishing genuine AirPlay
    sessions from phantom SETUPs. Apple devices (macOS especially)
    cycle SETUP→TEARDOWN every ~30 s when JTS is selected as an
    AirPlay output but no app is sustained-streaming. shairport-sync
    reports PlaybackStatus=Playing for those cycles even though no
    audio frames carry a track title from the sender. Genuine sessions
    populate xesam:title with the sender's current track.

    Fail-soft: any DBus / busctl error returns False, treating an
    unverifiable session as phantom. The off-switch above is the
    escape hatch if this ever produces false negatives in the field.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "call",
            SHAIRPORT_MPRIS_BUS,
            SHAIRPORT_MPRIS_PATH,
            DBUS_PROPS_IFACE, "Get", "ss",
            MPRIS_PLAYER_IFACE, "Metadata",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        logger.debug("busctl Metadata probe failed: %s", e)
        return False
    if proc.returncode != 0:
        return False
    return _AIRPLAY_TITLE_RE.search(stdout) is not None


async def airplay_playing() -> bool:
    """True iff shairport-sync is currently emitting AirPlay audio.

    Predicate is two-part since 2026-05-22:
      1) MPRIS `PlaybackStatus == "Playing"`, AND
      2) MPRIS `Metadata` carries a non-empty `xesam:title`.

    The metadata corroboration is what distinguishes a *genuine*
    AirPlay session (sender carries track title in DAAP metadata)
    from a *phantom* SETUP — the latter happens whenever an Apple
    device (notably macOS) has JTS selected as an AirPlay output
    but no app is sustained-streaming; macOS opens/tears down audio
    streams on ~30 s cycles as a keepalive, and shairport-sync
    reports PlaybackStatus=Playing for each cycle even though no
    audio actually reaches the speakers (ALSA loopback typically
    owned by librespot). Trusting PlaybackStatus alone caused
    jasper-mux to flap source every 30 s and the volume coordinator
    to duck Spotify by -25 dB on each cycle.

    Off-switch (env-driven, see _airplay_metadata_gate_disabled):
        JASPER_AIRPLAY_METADATA_GATE=disabled
    reverts to the pre-fix PlaybackStatus-only behaviour.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "busctl", "--system", "call",
            SHAIRPORT_MPRIS_BUS,
            SHAIRPORT_MPRIS_PATH,
            DBUS_PROPS_IFACE, "Get", "ss",
            MPRIS_PLAYER_IFACE, "PlaybackStatus",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        logger.debug("busctl PlaybackStatus probe failed: %s", e)
        return False
    if proc.returncode != 0:
        return False
    # busctl emits a single line like:  v s "Playing"
    # (variant-of-string-of-value). Substring match is robust to
    # leading/trailing whitespace busctl may add.
    if b'"Playing"' not in stdout:
        return False
    # PlaybackStatus is Playing. Corroborate with metadata unless
    # the gate is disabled via the escape-hatch env var.
    if _airplay_metadata_gate_disabled():
        return True
    return await _airplay_has_metadata_title()


async def usbsink_playing(state_path: str = USBSINK_STATE_PATH) -> bool:
    """jasper-usbsink publishes RMS-based playing state to
    /run/jasper-usbsink/state.json (atomic writes, hysteresis-debounced).
    Reading is cheap — the file is well under 1 KB. Missing file (the
    feature is disabled or the daemon hasn't started yet) and
    malformed JSON both resolve to False, matching the fail-soft
    convention of the other probes."""
    try:
        with open(state_path) as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        logger.debug("usbsink_playing probe failed: %s", e)
        return False
    return bool(data.get("playing", False))


async def bluetooth_playing() -> bool:
    """bluealsa-cli list-pcms prints one line per BlueALSA PCM path.
    On an idle box this is empty; with a phone connected and an A2DP
    stream open you get one or more lines like
    /org/bluealsa/hci0/dev_XX_../a2dpsnk/source. Best-effort — can't
    distinguish "phone connected, not playing" from "connected and
    streaming" without AVRCP, which bluez-alsa doesn't expose
    reliably."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluealsa-cli", "list-pcms",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        logger.debug("bluealsa-cli list-pcms probe failed: %s", e)
        return False
    return b"a2dpsnk/source" in stdout
