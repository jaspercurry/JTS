from __future__ import annotations

import asyncio
import logging
from typing import Any

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from . import tool
from ..spotify_routing import Resolution, resolve_target, stop_renderers

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


def make_spotify_tools(sp: spotipy.Spotify | None, moode, librespot_name: str):
    if sp is None:
        return []

    async def _to_thread(fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _resolve_and_stop() -> Resolution:
        """Resolve target and execute any required renderer stops. Returns
        the resolution so the caller has the device_id."""
        resolution = await resolve_target(sp, moode, librespot_name)
        logger.info(
            "spotify routing: device_id=%s stop=%s reason=%s",
            resolution.device_id, resolution.stop_renderers, resolution.reason,
        )
        if resolution.stop_renderers:
            await stop_renderers(moode, resolution.stop_renderers)
        return resolution

    @tool()
    async def spotify_play(query: str, kind: str = "track") -> dict:
        """Search Spotify and start playback. kind is one of: track, album, artist, playlist."""
        # Run search and target-resolution concurrently — they're
        # independent. Saves ~150ms vs sequential.
        search_task = asyncio.create_task(
            _to_thread(sp.search, q=query, type=kind, limit=1)
        )
        resolve_task = asyncio.create_task(_resolve_and_stop())
        results, resolution = await asyncio.gather(search_task, resolve_task)

        items = results.get(f"{kind}s", {}).get("items", [])
        if not items:
            return {"error": f"no {kind} found for: {query}"}
        if not resolution.device_id:
            return {
                "error": "no spotify target available — open Spotify on your "
                "phone once and pick the moOde device, or check that "
                "moOde's Spotify Connect is enabled.",
            }

        if kind == "track":
            await _to_thread(
                sp.start_playback, device_id=resolution.device_id, uris=[items[0]["uri"]]
            )
        else:
            await _to_thread(
                sp.start_playback, device_id=resolution.device_id, context_uri=items[0]["uri"]
            )
        return {"ok": True, "playing": items[0].get("name", query)}

    @tool()
    async def spotify_queue(query: str) -> dict:
        """Search Spotify for a track and add it to the playback queue."""
        search_task = asyncio.create_task(
            _to_thread(sp.search, q=query, type="track", limit=1)
        )
        resolve_task = asyncio.create_task(_resolve_and_stop())
        results, resolution = await asyncio.gather(search_task, resolve_task)

        items = results.get("tracks", {}).get("items", [])
        if not items:
            return {"error": f"no track found for: {query}"}
        if not resolution.device_id:
            return {"error": "no spotify target available"}
        await _to_thread(
            sp.add_to_queue, items[0]["uri"], device_id=resolution.device_id
        )
        return {"ok": True, "queued": items[0].get("name", query)}

    return [spotify_play, spotify_queue]
