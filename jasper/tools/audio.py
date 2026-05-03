from __future__ import annotations

from . import tool


def make_audio_tools(camilla):
    """Build closures over a CamillaController and tag them as tools."""

    @tool()
    async def get_volume() -> dict:
        """Return the current speaker volume in dB (CamillaDSP master gain)."""
        return {"volume_db": await camilla.get_volume_db()}

    @tool()
    async def set_volume(level_db: float) -> dict:
        """Set speaker volume in dB. Range roughly -50 (very quiet) to 0 (loud)."""
        await camilla.set_volume_db(level_db)
        return {"ok": True, "volume_db": level_db}

    @tool()
    async def adjust_volume(delta_db: float) -> dict:
        """Adjust speaker volume by delta dB (positive louder, negative softer)."""
        new_db = await camilla.adjust_volume_db(delta_db)
        return {"ok": True, "volume_db": new_db}

    return [get_volume, set_volume, adjust_volume]
