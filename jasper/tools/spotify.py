from __future__ import annotations

import asyncio
import logging
from typing import Any

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from . import tool

logger = logging.getLogger(__name__)

SPOTIFY_SCOPE = (
    "user-modify-playback-state user-read-playback-state "
    "user-read-currently-playing"
)


def build_spotify(cfg) -> spotipy.Spotify | None:
    if not cfg.spotify_enabled:
        return None
    auth = SpotifyOAuth(
        client_id=cfg.spotify_client_id,
        client_secret=cfg.spotify_client_secret,
        redirect_uri=cfg.spotify_redirect_uri,
        scope=SPOTIFY_SCOPE,
        cache_path=cfg.spotify_cache_path,
        open_browser=False,
    )
    return spotipy.Spotify(auth_manager=auth)


def make_spotify_tools(sp: spotipy.Spotify | None):
    if sp is None:
        return []

    async def _to_thread(fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _active_device_id() -> str | None:
        devices = await _to_thread(sp.devices)
        for d in devices.get("devices", []):
            if d.get("is_active"):
                return d["id"]
        if devices.get("devices"):
            return devices["devices"][0]["id"]
        return None

    @tool()
    async def spotify_play(query: str, kind: str = "track") -> dict:
        """Search Spotify and start playback. kind is one of: track, album, artist, playlist."""
        results: dict[str, Any] = await _to_thread(sp.search, q=query, type=kind, limit=1)
        items = results.get(f"{kind}s", {}).get("items", [])
        if not items:
            return {"error": f"no {kind} found for: {query}"}
        device_id = await _active_device_id()
        if not device_id:
            return {"error": "no active spotify device"}
        if kind == "track":
            await _to_thread(sp.start_playback, device_id=device_id, uris=[items[0]["uri"]])
        else:
            await _to_thread(sp.start_playback, device_id=device_id, context_uri=items[0]["uri"])
        return {"ok": True, "playing": items[0].get("name", query)}

    @tool()
    async def spotify_queue(query: str) -> dict:
        """Search Spotify for a track and add it to the playback queue."""
        results = await _to_thread(sp.search, q=query, type="track", limit=1)
        items = results.get("tracks", {}).get("items", [])
        if not items:
            return {"error": f"no track found for: {query}"}
        device_id = await _active_device_id()
        await _to_thread(sp.add_to_queue, items[0]["uri"], device_id=device_id)
        return {"ok": True, "queued": items[0].get("name", query)}

    return [spotify_play, spotify_queue]
