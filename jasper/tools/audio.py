# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Voice volume tools use the source-aware coordinator or bonded pair leader."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import tool
from ..log_event import log_event
from ..platform.control_client import AsyncControlClient, ControlError

if TYPE_CHECKING:
    from ..volume_coordinator import VolumeCoordinator


logger = logging.getLogger(__name__)

# 4 s covers control's 2.5 s leader hop; build lazily to keep imports light.
_control_client: AsyncControlClient | None = None


def _get_control_client() -> AsyncControlClient:
    global _control_client
    if _control_client is None:
        _control_client = AsyncControlClient(timeout=4.0)
    return _control_client


def _pair_follower_active() -> bool:
    """Read the follower role fresh; bonded content bypasses its local DSP.
    The control API forwards volume writes to the audible leader."""
    from ..multiroom.config import load_config  # lazy: import cost, multiroom stays out of jasper-voice until a volume tool runs; test patch boundary (tests/test_tools_audio.py)
    from ..multiroom.effective_role import effective_follower_leader_addr  # lazy: import cost, multiroom stays out of jasper-voice until a volume tool runs

    return effective_follower_leader_addr(load_config()) is not None


async def _pair_volume(path: str, body: dict | None = None) -> dict | None:
    """Return None only when not a follower; failures must never write local volume."""
    if not _pair_follower_active():
        return None
    client = _get_control_client()
    try:
        if body is None:
            resp = await client.get(path)
        else:
            resp = await client.post(path, body)
    except ControlError as e:
        log_event(
            logger,
            "volume.pair_tool_forward_failed",
            path=path,
            error=e,
            level=logging.WARNING,
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



def _make_get_volume(coordinator: "VolumeCoordinator"):
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
        state = coordinator.get_volume_state()
        return {"percent": state.effective_percent}


    return get_volume


def _make_set_volume(coordinator: "VolumeCoordinator"):
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


    return set_volume


def _make_adjust_volume(coordinator: "VolumeCoordinator"):
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


    return adjust_volume


def _make_mute(coordinator: "VolumeCoordinator"):
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


    return mute


def _make_unmute(coordinator: "VolumeCoordinator"):
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


    return unmute


def make_audio_tools(coordinator: "VolumeCoordinator"):
    return [
        _make_get_volume(coordinator), _make_set_volume(coordinator),
        _make_adjust_volume(coordinator), _make_mute(coordinator), _make_unmute(coordinator),
    ]
