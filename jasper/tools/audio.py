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

import asyncio
import json
import logging
import os
import urllib.request
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

logger = logging.getLogger(__name__)

# jasper-control's local HTTP port — same default the systemd unit uses.
_CONTROL_PORT = int(os.environ.get("JASPER_CONTROL_PORT", "8780"))

# Test seam for the one network call (mirrors control server's
# _pair_urlopen — patching the stdlib would intercept unrelated clients).
_pair_urlopen = urllib.request.urlopen


def _pair_volume_request(path: str, body: dict | None) -> dict | None:
    """Drive a volume change through the LOCAL control API when this
    speaker is an ACTIVE bonded follower, returning the relayed result.

    While bonded, the follower's own coordinator is INAUDIBLE — bonded
    content bypasses its CamillaDSP entirely — so "Jarvis, louder" spoken
    to the follower must move the PAIR volume. jasper-control already owns
    the one forwarding implementation (its /volume* handlers relay to the
    leader), so the tools reuse it via loopback rather than growing a
    second forward here. Returns None when this speaker is solo/leader
    (callers use the local coordinator as always). Raises on transport
    failure — the async wrapper turns that into a spoken error, never a
    silent inert write. Sync by design; called via asyncio.to_thread.
    """
    from ..multiroom.config import load_config

    cfg = load_config()
    if not (
        cfg.enabled
        and cfg.error is None
        and cfg.role == "follower"
        and cfg.leader_addr
    ):
        return None
    url = f"http://127.0.0.1:{_CONTROL_PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    # 4 s: covers control's own 2.5 s leader hop plus loopback overhead.
    with _pair_urlopen(req, timeout=4.0) as resp:
        payload = json.loads(resp.read().decode())
    return payload if isinstance(payload, dict) else {}


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
        try:
            return await asyncio.to_thread(_pair_volume_request, path, body)
        except Exception as e:  # noqa: BLE001 — degrade to a spoken error
            logger.warning(
                "event=volume.pair_tool_forward_failed path=%s error=%s",
                path, e,
            )
            return {
                "error": "Couldn't reach the pair leader to change the "
                         "volume. The other speaker may be offline.",
            }

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
        fwd = await _pair_volume("/volume")
        if fwd is not None:
            if "error" in fwd:
                return fwd
            return {"percent": int(fwd.get("percent", 0))}
        return {"percent": coordinator.get_listening_level()}

    @tool()
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
        fwd = await _pair_volume(
            "/volume/adjust", {"delta_percent": int(delta_percent)},
        )
        if fwd is not None:
            if "error" in fwd:
                return fwd
            return {"ok": True, "percent": int(fwd.get("percent", 0))}
        applied = await coordinator.adjust_listening_level(int(delta_percent))
        return {"ok": True, "percent": applied}

    @tool()
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

    @tool()
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
