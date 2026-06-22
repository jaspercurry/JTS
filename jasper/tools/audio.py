# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

import logging
from typing import TYPE_CHECKING

from . import tool
from ..control.client import AsyncControlClient, ControlError

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

logger = logging.getLogger(__name__)

# The control-API transport is OWNED by jasper.control.client (base URL,
# timeout policy, error model) — the tools compose it instead of re-rolling
# urllib. 4 s outer timeout covers jasper-control's own 2.5 s leader hop.
# Module-level so tests can swap in a fake; constructed lazily so importing
# this module costs nothing on speakers that never bond.
_control_client: AsyncControlClient | None = None


def _get_control_client() -> AsyncControlClient:
    global _control_client
    if _control_client is None:
        _control_client = AsyncControlClient(timeout=4.0)
    return _control_client


def _pair_follower_active() -> bool:
    """True when this speaker is an ACTIVE bonded follower — the one shared
    predicate (multiroom.config.follower_leader_addr), read fresh from the
    tiny grouping.env each call. While bonded, the follower's own
    coordinator is INAUDIBLE (bonded content bypasses its CamillaDSP), so
    "Jarvis, louder" spoken to the follower must move the PAIR volume via
    the local control API — whose /volume* handlers already forward to the
    leader. One forwarding implementation total."""
    from ..multiroom.config import follower_leader_addr, load_config

    return follower_leader_addr(load_config()) is not None


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

    async def _pair_volume(path: str, body: dict | None = None) -> dict | None:
        """None → not a bonded follower (use the local coordinator).
        A dict → the pair-volume result to return, or an `error` the LLM
        speaks — never fall back to the local coordinator on failure,
        whose writes are inaudible while bonded (a lie to the user)."""
        if not _pair_follower_active():
            return None
        client = _get_control_client()
        try:
            if body is None:
                resp = await client.get(path)
            else:
                resp = await client.post(path, body)
        except ControlError as e:
            logger.warning(
                "event=volume.pair_tool_forward_failed path=%s error=%s",
                path, e,
            )
            return {
                "error": "Couldn't reach the pair leader to change the "
                         "volume. The other speaker may be offline.",
            }
        payload = resp.json()
        payload = payload if isinstance(payload, dict) else {}
        if not resp.ok:
            # jasper-control relays the leader's own error verdicts
            # (status + body) — pass the specific reason to the LLM.
            return {
                "error": str(payload.get("error"))
                if payload.get("error")
                else "The pair leader rejected the volume change.",
            }
        return payload

    @tool(labels=("music", "volume"))
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
        fwd = await _pair_volume("/volume")
        if fwd is not None:
            if "error" in fwd:
                return fwd
            return {"percent": int(fwd.get("percent", 0))}
        return {"percent": coordinator.get_listening_level()}

    @tool(labels=("music", "volume"))
    async def set_volume(percent: int) -> dict:
        """Set speaker volume to an absolute percentage 0-100.

        Call when the user names a specific level ('set volume to
        sixty', 'volume eighty').

        Voice answer style: speak the new `percent` from the result
        ('Volume sixty.'). No preamble; no confirmation question.
        """
        fwd = await _pair_volume("/volume/set", {"percent": int(percent)})
        if fwd is not None:
            if "error" in fwd:
                return fwd
            return {"ok": True, "percent": int(fwd.get("percent", 0))}
        applied = await coordinator.set_listening_level(percent)
        return {"ok": True, "percent": applied}

    @tool(labels=("music", "volume"))
    async def adjust_volume(delta_percent: int) -> dict:
        """Adjust speaker volume by a relative delta in percent
        (positive louder, negative softer).

        Default step for bare 'volume up' / 'volume down' is +10 /
        -10. For 'a lot louder' / 'a lot quieter' use ±20 to ±30.
        For 'a little' use ±5.

        Voice answer style: speak the new `percent` from the result
        ('Volume seventy.'). No preamble; no confirmation question.
        """
        fwd = await _pair_volume(
            "/volume/adjust", {"delta_percent": int(delta_percent)},
        )
        if fwd is not None:
            if "error" in fwd:
                return fwd
            return {"ok": True, "percent": int(fwd.get("percent", 0))}
        applied = await coordinator.adjust_listening_level(int(delta_percent))
        return {"ok": True, "percent": applied}

    @tool(labels=("music", "volume", "mute"))
    async def mute() -> dict:
        """Mute the speaker. Unmute restores the prior level.

        Voice answer style: 'Muted.' One word.
        """
        fwd = await _pair_volume("/volume/mute", {"muted": True})
        if fwd is not None:
            if "error" in fwd:
                return fwd
            return {"ok": True, "muted": True}
        await coordinator.mute()
        return {"ok": True, "muted": True}

    @tool(labels=("music", "volume", "mute"))
    async def unmute() -> dict:
        """Restore speaker to its pre-mute level (50% if nothing
        saved).

        Voice answer style: 'Unmuted.' One word.
        """
        fwd = await _pair_volume("/volume/mute", {"muted": False})
        if fwd is not None:
            if "error" in fwd:
                return fwd
            return {"ok": True, "percent": int(fwd.get("percent", 0))}
        applied = await coordinator.unmute(fallback_level=50)
        return {"ok": True, "percent": applied}

    return [get_volume, set_volume, adjust_volume, mute, unmute]
