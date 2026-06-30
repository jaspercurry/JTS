# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
from datetime import datetime, timezone
import json
import logging
import os
import re
from typing import Any

from . import bluealsa_probe
from . import librespot_state

logger = logging.getLogger(__name__)


# Match a non-empty xesam:title in busctl's MPRIS Metadata output.
# Format is a single line containing key/type/value triples; the
# title appears as:  "xesam:title" s "Some Song Name"
# (string type indicator `s`, then quoted value).
# Empty metadata renders as `v a{sv} 0\n` with no title key at all,
# so a search-fail is the phantom signal.
_AIRPLAY_TITLE_RE = re.compile(rb'"xesam:title"\s+s\s+"([^"]+)"')

# Default path the jasper-usbsink daemon publishes its state to.
# Kept in sync with jasper.usbsink.state_publisher.DEFAULT_STATE_PATH.
# Both definitions exist so neither module pulls the other into its
# import graph (jasper-mux doesn't need to import the usbsink daemon
# just to know where its state file is).
USBSINK_STATE_PATH = "/run/jasper-usbsink/state.json"
USBSINK_STATE_FRESH_SEC = 5.0


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
            "org.mpris.MediaPlayer2.ShairportSync",
            "/org/mpris/MediaPlayer2",
            "org.freedesktop.DBus.Properties", "Get", "ss",
            "org.mpris.MediaPlayer2.Player", "Metadata",
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
            "org.mpris.MediaPlayer2.ShairportSync",
            "/org/mpris/MediaPlayer2",
            "org.freedesktop.DBus.Properties", "Get", "ss",
            "org.mpris.MediaPlayer2.Player", "PlaybackStatus",
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
    data = read_usbsink_state(state_path)
    if data is None:
        return False
    return bool(data.get("playing", False))


def read_usbsink_state(state_path: str = USBSINK_STATE_PATH) -> dict[str, Any] | None:
    """Read jasper-usbsink's small JSON state file, fail-soft."""
    try:
        with open(state_path) as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        logger.debug("usbsink_playing probe failed: %s", e)
        return None
    return data if isinstance(data, dict) else None


def usbsink_state_fresh_host_connected(
    state_path: str = USBSINK_STATE_PATH,
    *,
    max_age_sec: float = USBSINK_STATE_FRESH_SEC,
) -> bool:
    """True when the USB sink daemon is freshly publishing a present gadget.

    This is deliberately weaker than ``usbsink_playing``: a host can be
    connected and momentarily RMS-quiet between tracks. Mux's lean FIFO lane uses
    this to avoid tearing down the low-latency pipe on ordinary quiet passages,
    while still leaving lean if the daemon/state disappears.
    """
    data = read_usbsink_state(state_path)
    if not data or not data.get("host_connected", False):
        return False
    raw_updated = data.get("updated_at")
    if not isinstance(raw_updated, str):
        return False
    try:
        updated = datetime.fromisoformat(raw_updated)
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
    return age <= max_age_sec


async def bluetooth_playing() -> bool:
    """bluealsa-cli list-pcms prints one line per BlueALSA PCM path.
    On an idle box this is empty; with a phone connected and an A2DP
    stream open you get one or more lines like
    /org/bluealsa/hci0/dev_XX_../a2dpsnk/source. Best-effort — can't
    distinguish "phone connected, not playing" from "connected and
    streaming" without AVRCP, which bluez-alsa doesn't expose
    reliably."""
    stdout = await bluealsa_probe.list_pcms(logger)
    if stdout is None:
        return False
    return b"a2dpsnk/source" in stdout
