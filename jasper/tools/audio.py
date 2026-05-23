"""Volume control voice tools.

Routes through `VolumeCoordinator` so "set volume to 50%" pushes to
whichever source is currently active (AirPlay sender DBus, Spotify
Connect HTTP, Bluetooth DBus) rather than only adjusting CamillaDSP's
main_volume — the latter is upstream of those source-side attenuators
and feels disconnected when the user has, say, the iPhone slider at
30%.

The coordinator owns persistence, mute state, and echo-prevention.
This module is just the tool-registration surface that the Gemini
function-call layer sees.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import tool

if TYPE_CHECKING:
    from ..volume_coordinator import VolumeCoordinator


# Re-exported for tests + control daemon — same dB scale as CamillaDSP
# and volume_persistence. Most callers shouldn't need these directly:
# the coordinator handles unit conversion internally.
VOLUME_MIN_DB = -50.0
VOLUME_MAX_DB = 0.0

# Step used in the system instruction's "volume up / down" guidance.
# 10 listening-level points = ~5 dB on the legacy camilla scale and
# matches Sonos/Echo/HomePod step size. Keep this here so the
# system prompt's example doesn't drift from the tool default.
DEFAULT_STEP_PERCENT = 10


def _percent_to_db(percent: int) -> float:
    p = max(0, min(100, int(percent)))
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    return VOLUME_MIN_DB + (span * p / 100.0)


def _db_to_percent(db: float) -> int:
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    p = (float(db) - VOLUME_MIN_DB) / span * 100.0
    return max(0, min(100, round(p)))


def make_audio_tools(coordinator: "VolumeCoordinator"):
    """Build volume-control tools backed by the source-aware coordinator.

    The coordinator's `set_listening_level` / `adjust_listening_level`
    methods push to the active source's own attenuator (or CamillaDSP
    when idle) and persist the canonical level. Mute state is
    coordinator-internal so a daemon restart doesn't lose the pre-mute
    level.
    """

    @tool()
    async def get_volume() -> dict:
        """Return the current speaker volume as a percentage 0-100.

        Call this for any "what's the volume?" / "how loud is it?"
        question; don't change the volume on a query. This tracks
        the user-perceived level — for music via AirPlay/Spotify/BT,
        that's the source slider's position; otherwise CamillaDSP's
        main fader.

        Voice answer style: 'Volume is at 70%.' Just the number,
        no preamble.
        """
        return {"percent": coordinator.get_listening_level()}

    @tool()
    async def set_volume(percent: int) -> dict:
        """Set speaker volume to an absolute percentage 0-100.

        Call when the user names a specific level ('set volume to
        sixty', 'volume eighty').

        Voice answer style: speak the new `percent` from the result
        ('Volume sixty.'). No preamble; no confirmation question.
        """
        applied = await coordinator.set_listening_level(percent)
        return {"ok": True, "percent": applied}

    @tool()
    async def adjust_volume(delta_percent: int) -> dict:
        """Adjust speaker volume by a relative delta in percent
        (positive louder, negative softer).

        Default step for bare 'volume up' / 'volume down' is +10 /
        -10. For 'a lot louder' / 'a lot quieter' use ±20 to ±30.
        For 'a little' use ±5.

        Voice answer style: speak the new `percent` from the result
        ('Volume seventy.'). No preamble; no confirmation question.
        """
        applied = await coordinator.adjust_listening_level(int(delta_percent))
        return {"ok": True, "percent": applied}

    @tool()
    async def mute() -> dict:
        """Mute the speaker. Unmute restores the prior level.

        Voice answer style: 'Muted.' One word.
        """
        await coordinator.mute()
        return {"ok": True, "muted": True}

    @tool()
    async def unmute() -> dict:
        """Restore speaker to its pre-mute level (50% if nothing
        saved).

        Voice answer style: 'Unmuted.' One word.
        """
        applied = await coordinator.unmute(fallback_level=50)
        return {"ok": True, "percent": applied}

    return [get_volume, set_volume, adjust_volume, mute, unmute]
