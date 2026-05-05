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


def make_audio_tools(camilla, persistence=None):
    """Build the volume-control tool surface.

    `persistence` (optional VolumePersistence) hooks the user-facing
    set/adjust/mute/unmute actions to disk so the speaker comes up at
    its last commanded level after a restart. None disables persistence
    (used in unit tests that don't want to touch disk).
    """
    # Closure state for mute/unmute. Stores the pre-mute percent so
    # unmute restores the prior level. None = not currently muted.
    mute_state: dict[str, int | None] = {"saved": None}

    def _persist(db: float) -> None:
        if persistence is not None:
            persistence.save_now(db)

    @tool()
    async def get_volume() -> dict:
        """Return the current speaker volume as a percentage 0-100.
        This is the speaker's own volume setting (CamillaDSP main fader),
        not a measure of how loud the room currently sounds — upstream
        attenuators like the iPhone's AirPlay slider can make audio play
        quieter than this number suggests, but that's a separate slider
        the user controls on their device."""
        db = await camilla.get_volume_db()
        return {"percent": _db_to_percent(db)}

    @tool()
    async def set_volume(percent: int) -> dict:
        """Set speaker volume to an absolute percentage 0-100."""
        target_db = _percent_to_db(percent)
        await camilla.set_volume_db(target_db)
        mute_state["saved"] = None
        _persist(target_db)
        return {"ok": True, "percent": _db_to_percent(target_db)}

    @tool()
    async def adjust_volume(delta_percent: int) -> dict:
        """Adjust speaker volume by a relative delta in percent (positive louder, negative softer). Use +10 / -10 for 'volume up' / 'volume down'."""
        current_db = await camilla.get_volume_db()
        current_pct = _db_to_percent(current_db)
        new_pct = max(0, min(100, current_pct + int(delta_percent)))
        new_db = _percent_to_db(new_pct)
        await camilla.set_volume_db(new_db)
        mute_state["saved"] = None
        _persist(new_db)
        return {"ok": True, "percent": new_pct}

    @tool()
    async def mute() -> dict:
        """Mute the speaker. Unmute restores the prior level."""
        current_pct = _db_to_percent(await camilla.get_volume_db())
        if current_pct > 0:
            mute_state["saved"] = current_pct
        await camilla.set_volume_db(VOLUME_MIN_DB)
        # Don't persist a mute as the canonical volume — if the user
        # mutes and the daemon restarts, we'd come up silent forever.
        # The pre-mute level (mute_state["saved"]) was last persisted
        # at its time of being set, which is what we want a restart to
        # restore to.
        return {"ok": True, "muted": True}

    @tool()
    async def unmute() -> dict:
        """Restore speaker to its pre-mute level (50% if nothing saved)."""
        target_pct = mute_state["saved"] or 50
        mute_state["saved"] = None
        target_db = _percent_to_db(target_pct)
        await camilla.set_volume_db(target_db)
        _persist(target_db)
        return {"ok": True, "percent": target_pct}

    return [get_volume, set_volume, adjust_volume, mute, unmute]
