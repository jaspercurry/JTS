# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Source-state probes for the four music renderers.

Each `<source>_playing()` returns True iff that renderer is currently producing
audio and preserves the historical fail-soft bool contract. The mux uses the
matching `<source>_playing_observed()` probes, whose third ``None`` state means
"the probe failed, do not reinterpret that as the source stopping." This keeps
a transient D-Bus/CLI/status failure from creating a false stop/start edge.

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
import math
import os
import re
from pathlib import Path
from typing import Any

from . import bluealsa_probe
from . import librespot_state
from .fanin.status import (
    FANIN_INPUT_SOURCE_DIRECT,
    fanin_usbsink_input,
    read_fanin_status,
)

logger = logging.getLogger(__name__)


# Match a non-empty xesam:title in busctl's MPRIS Metadata output.
# Format is a single line containing key/type/value triples; the
# title appears as:  "xesam:title" s "Some Song Name"
# (string type indicator `s`, then quoted value).
# Empty metadata renders as `v a{sv} 0\n` with no title key at all,
# so a search-fail is the phantom signal.
_AIRPLAY_TITLE_RE = re.compile(rb'"xesam:title"\s+s\s+"([^"]+)"')

# The RMS level (dBFS) at or below which the USB lane is treated as NOT playing —
# so a host streaming digital silence (a muted Zoom, an idle tab) does not seize
# the speaker. Applied against fan-in's reported per-lane rms_dbfs for the
# DIRECT-capture lane (the live USB path). This is the single definition of the
# gate: fan-in is the sole live USB ingress owner. The retired
# jasper-usbsink-audio daemon's former `PLAYING_RMS_DBFS` anchor constant
# (and the cross-language drift guard that pinned it to this value) were deleted
# 2026-07-11. tests/test_usbsink_playing_rms_contract.py now pins that
# `jasper.mux` imports this constant rather than re-declaring its own copy.
USBSINK_PLAYING_RMS_DBFS = -60.0


async def spotify_playing(
    librespot_state_path: str = librespot_state.DEFAULT_PATH,
) -> bool:
    """librespot writes /run/librespot/state.json on every player
    event via its --onevent hook. Reading on every probe is cheap
    (file is a few hundred bytes); is_playing returns False on
    missing/malformed file."""
    return await spotify_playing_observed(librespot_state_path) is True


async def spotify_playing_observed(
    librespot_state_path: str = librespot_state.DEFAULT_PATH,
) -> bool | None:
    """Tri-state Spotify observation for source arbitration.

    A missing state file is a definite inactive state: librespot has not emitted
    an event yet. A malformed/unreadable file is unknown, because treating a
    torn or temporarily inaccessible observation as "stopped" can make the mux
    flutter away from an otherwise healthy session.
    """
    path = Path(librespot_state_path)
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return False
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("librespot state observation failed (%s): %s", path, exc)
        return None
    if not isinstance(state, dict):
        return None
    if state.get("playing") is True:
        return True
    if state.get("paused") is True or state.get("stopped") is True:
        return False
    return False


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


async def _airplay_has_metadata_title_observed() -> bool | None:
    """True iff shairport-sync's MPRIS Metadata carries a non-empty
    xesam:title at the moment we ask.

    Corroborates PlaybackStatus when distinguishing genuine AirPlay
    sessions from phantom SETUPs. Apple devices (macOS especially)
    cycle SETUP→TEARDOWN every ~30 s when JTS is selected as an
    AirPlay output but no app is sustained-streaming. shairport-sync
    reports PlaybackStatus=Playing for those cycles even though no
    audio frames carry a track title from the sender. Genuine sessions
    populate xesam:title with the sender's current track.

    Transport failures are unknown rather than inactive. The public bool wrapper
    below retains the historical fail-soft behavior for non-mux callers.
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
        return None
    if proc.returncode != 0:
        return False
    return _AIRPLAY_TITLE_RE.search(stdout) is not None


async def _airplay_has_metadata_title() -> bool:
    """Historical bool wrapper used by tests and non-arbiter callers."""
    return await _airplay_has_metadata_title_observed() is True


async def airplay_playing_observed() -> bool | None:
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
        return None
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
    return await _airplay_has_metadata_title_observed()


async def airplay_playing() -> bool:
    """Historical fail-soft bool wrapper around the mux's tri-state probe."""
    return await airplay_playing_observed() is True


async def usbsink_playing() -> bool:
    """USB activity from the sole live ingress owner: fan-in DIRECT.

    Read fan-in's bounded STATUS probe off the event loop, then require both
    current direct-capture health and audible pre-mute level. Missing/old
    snapshots fail soft to ``False``.
    """

    status = await asyncio.to_thread(read_fanin_status)
    return usbsink_direct_playing(status) is True


def _nonnegative_int_counter(value: Any) -> int | None:
    """Return a JSON u64-ish counter value, rejecting bools and bad shapes."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def usbsink_direct_frames_read(
    fanin_status: dict[str, Any] | None,
) -> int | None:
    """Cumulative liveness counter on fan-in's USB DIRECT lane, else None.

    Returns a counter only when the usbsink lane is in direct mode
    (``source == "direct"``), meaning fan-in owns the live gadget capture.

    Prefer ``resampler.input_frames``: direct capture accounts host input there
    on builds where the lane-level ``frames_read`` can remain frozen at 0.
    Fall back to lane-level ``frames_read`` for older/no-resampler snapshots.
    A single snapshot is not enough; the value becomes a liveness signal only as
    a delta across mux ticks.
    """
    lane = fanin_usbsink_input(fanin_status)
    if not (
        isinstance(lane, dict)
        and lane.get("source") == FANIN_INPUT_SOURCE_DIRECT
    ):
        return None

    resampler = lane.get("resampler")
    if isinstance(resampler, dict):
        frames = _nonnegative_int_counter(resampler.get("input_frames"))
        if frames is not None:
            return frames
    return _nonnegative_int_counter(lane.get("frames_read"))


def usbsink_direct_streaming(
    fanin_status: dict[str, Any] | None,
) -> bool | None:
    """Fan-in's edge-detected USB streaming state, when available.

    New fan-in builds sample their existing host-input counter on a lightweight
    helper thread and publish this boolean in ``direct.streaming``. Older builds
    omit it; mux then falls back to comparing the cumulative frame counter across
    patrols. ``None`` also covers a missing/malformed STATUS response, allowing
    the arbiter to retain its last known state rather than invent a stop.
    """
    lane = fanin_usbsink_input(fanin_status)
    if not (
        isinstance(lane, dict)
        and lane.get("source") == FANIN_INPUT_SOURCE_DIRECT
    ):
        return None
    direct = lane.get("direct")
    if not isinstance(direct, dict):
        return None
    value = direct.get("streaming")
    return value if isinstance(value, bool) else None


def _finite_float(value: Any) -> float | None:
    """Coerce ``value`` to a finite float, else ``None`` (rejects bools)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def usbsink_direct_rms_dbfs(
    fanin_status: dict[str, Any] | None,
) -> float | None:
    """Most-recent-period content level (dBFS) on fan-in's USB DIRECT lane, else
    ``None``.

    Mirrors :func:`usbsink_direct_frames_read`: a value is returned only when the
    usbsink lane is in direct mode (``source == "direct"``), i.e. fan-in owns the
    live gadget capture and reports its pre-mute level directly.
    ``None`` when there is no direct lane, the STATUS is missing / malformed, or
    the lane carries no numeric ``rms_dbfs`` (an older fan-in build predating the
    per-lane level)."""
    lane = fanin_usbsink_input(fanin_status)
    if not (
        isinstance(lane, dict)
        and lane.get("source") == FANIN_INPUT_SOURCE_DIRECT
    ):
        return None
    return _finite_float(lane.get("rms_dbfs"))


def usbsink_direct_audible(
    fanin_status: dict[str, Any] | None,
    *,
    threshold_dbfs: float = USBSINK_PLAYING_RMS_DBFS,
) -> bool | None:
    """Whether fan-in's USB DIRECT lane is emitting audible content right now.

    ``True`` / ``False`` from the direct lane's most-recent-period ``rms_dbfs``
    vs the shared :data:`USBSINK_PLAYING_RMS_DBFS` threshold. ``None`` when
    there is no direct lane or no numeric level to compare (older fan-in) —
    callers pick the fail-soft direction. This is the instantaneous *level*
    half of combo liveness; mux pairs it with the frames-advanced *liveness*
    half (see ``jasper.mux.step_combo_liveness``)."""
    rms = usbsink_direct_rms_dbfs(fanin_status)
    if rms is None:
        return None
    return rms > threshold_dbfs


def usbsink_direct_playing(
    fanin_status: dict[str, Any] | None,
) -> bool | None:
    """Current USB activity from fan-in's DIRECT lane, or ``None`` if absent.

    ``direct.health`` proves capture is flowing now; ``rms_dbfs`` rejects a
    host that is merely streaming digital silence. Older direct snapshots that
    predate the health field fall back to the same RMS gate.
    """

    lane = fanin_usbsink_input(fanin_status)
    if not (
        isinstance(lane, dict)
        and lane.get("source") == FANIN_INPUT_SOURCE_DIRECT
    ):
        return None
    audible = usbsink_direct_audible(fanin_status)
    if audible is None:
        return False
    direct = lane.get("direct")
    if not isinstance(direct, dict) or "health" not in direct:
        return audible
    return direct.get("health") == "capturing" and audible


def usbsink_direct_muted(
    fanin_status: dict[str, Any] | None,
) -> bool | None:
    """Whether fan-in's USB DIRECT lane is currently MIX-muted, else ``None``.

    Mirrors :func:`usbsink_direct_rms_dbfs`: a value is returned only when the
    usbsink lane is in direct mode (``source == "direct"``), i.e. fan-in owns the
    live gadget capture — the fan-in lane MIX-mute is how mux silences USB.
    ``None`` when there is no direct lane, the STATUS is missing /
    malformed, or the lane predates the per-lane ``muted`` flag (older fan-in
    build). This is the mute STATE, separate from the ``rms_dbfs`` /
    ``frames_read`` telemetry the lane keeps reporting PRE-mute (so mux still
    sees a muted-but-streaming host as active)."""
    lane = fanin_usbsink_input(fanin_status)
    if not (
        isinstance(lane, dict)
        and lane.get("source") == FANIN_INPUT_SOURCE_DIRECT
    ):
        return None
    value = lane.get("muted")
    return value if isinstance(value, bool) else None


async def bluetooth_playing_observed() -> bool | None:
    """bluealsa-cli list-pcms prints one line per BlueALSA PCM path.
    On an idle box this is empty; with a phone connected and an A2DP
    stream open you get one or more lines like
    /org/bluealsa/hci0/dev_XX_../a2dpsnk/source. Best-effort — can't
    distinguish "phone connected, not playing" from "connected and
    streaming" without AVRCP, which bluez-alsa doesn't expose
    reliably."""
    stdout = await bluealsa_probe.list_pcms(logger)
    if stdout is None:
        return None
    return b"a2dpsnk/source" in stdout


async def bluetooth_playing() -> bool:
    """Historical fail-soft bool wrapper around the mux's tri-state probe."""
    return await bluetooth_playing_observed() is True
