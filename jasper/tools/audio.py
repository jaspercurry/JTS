from __future__ import annotations

from . import tool

# CamillaDSP main fader is the speaker output gain — applied AFTER the
# pipeline, so it works regardless of source (AirPlay / Spotify / MPD).
# moOde's `cmd=set_volume` is gated when a renderer is active, so we
# can't use that for voice control. The Ducker (camilla.py) is additive
# against this same fader; user volume changes during a session shift
# the duck baseline, which is the desired behavior.
VOLUME_MIN_DB = -50.0   # 0%   — effectively silent at the speaker
VOLUME_MAX_DB = 0.0     # 100% — full digital scale, loudest

# Step used in the system instruction's "volume up / down" guidance.
# 10% maps to 5 dB on this scale, which matches the per-step amplitude
# change of mainstream voice assistants (Sonos / Echo / HomePod use
# 8–10% steps). Lives here so retuning is one place.
DEFAULT_STEP_PERCENT = 10


def _percent_to_db(percent: int) -> float:
    p = max(0, min(100, int(percent)))
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    return VOLUME_MIN_DB + (span * p / 100.0)


def _db_to_percent(db: float) -> int:
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    p = (float(db) - VOLUME_MIN_DB) / span * 100.0
    return max(0, min(100, round(p)))


def make_audio_tools(camilla):
    # Closure state for mute/unmute. Stores the pre-mute percent so
    # unmute restores the prior level. None = not currently muted.
    mute_state: dict[str, int | None] = {"saved": None}

    @tool()
    async def get_volume() -> dict:
        """Return the current speaker volume as a percentage 0-100."""
        db = await camilla.get_volume_db()
        return {"percent": _db_to_percent(db)}

    @tool()
    async def set_volume(percent: int) -> dict:
        """Set speaker volume to an absolute percentage 0-100."""
        target_db = _percent_to_db(percent)
        await camilla.set_volume_db(target_db)
        mute_state["saved"] = None
        return {"ok": True, "percent": _db_to_percent(target_db)}

    @tool()
    async def adjust_volume(delta_percent: int) -> dict:
        """Adjust speaker volume by a relative delta in percent (positive louder, negative softer). Use +10 / -10 for 'volume up' / 'volume down'."""
        current_db = await camilla.get_volume_db()
        current_pct = _db_to_percent(current_db)
        new_pct = max(0, min(100, current_pct + int(delta_percent)))
        await camilla.set_volume_db(_percent_to_db(new_pct))
        mute_state["saved"] = None
        return {"ok": True, "percent": new_pct}

    @tool()
    async def mute() -> dict:
        """Mute the speaker. Unmute restores the prior level."""
        current_pct = _db_to_percent(await camilla.get_volume_db())
        if current_pct > 0:
            mute_state["saved"] = current_pct
        await camilla.set_volume_db(VOLUME_MIN_DB)
        return {"ok": True, "muted": True}

    @tool()
    async def unmute() -> dict:
        """Restore speaker to its pre-mute level (50% if nothing saved)."""
        target_pct = mute_state["saved"] or 50
        mute_state["saved"] = None
        await camilla.set_volume_db(_percent_to_db(target_pct))
        return {"ok": True, "percent": target_pct}

    return [get_volume, set_volume, adjust_volume, mute, unmute]
