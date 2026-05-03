from __future__ import annotations

from . import tool


def make_transport_tools(moode):
    @tool()
    async def toggle_play_pause() -> dict:
        """Toggle play/pause on the current music source."""
        await moode.toggle_play_pause()
        return {"ok": True}

    @tool()
    async def skip_next() -> dict:
        """Skip to the next track."""
        await moode.next_track()
        return {"ok": True}

    @tool()
    async def skip_previous() -> dict:
        """Go back to the previous track."""
        await moode.previous_track()
        return {"ok": True}

    @tool()
    async def get_now_playing() -> dict:
        """Return metadata about the currently playing track (title, artist, album)."""
        song = await moode.get_currentsong()
        return {
            "title": song.get("title") or song.get("Title") or "",
            "artist": song.get("artist") or song.get("Artist") or "",
            "album": song.get("album") or song.get("Album") or "",
        }

    return [toggle_play_pause, skip_next, skip_previous, get_now_playing]
