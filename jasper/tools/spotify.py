from __future__ import annotations

import asyncio
import logging

from . import tool
from ..spotify_routing import resolve_target, stop_renderers

logger = logging.getLogger(__name__)


def make_spotify_tools(router, moode, librespot_name: str):
    """Multi-account-aware Spotify tools.

    `router` is a `jasper.spotify_router.Router` that picks the right
    household member's spotipy client for the current voice command:

      - When AirPlay is active and the sender's `ClientName` matches a
        configured account → that account's client.
      - Otherwise, whichever account's Web API session reports
        is_playing=true.
      - Otherwise, the registry's default account.

    Returns an empty tool list if no accounts are configured (fresh
    install, nobody has visited jasper.local/spotify yet)."""
    if router is None or not router.clients:
        return []

    async def _resolve_for_play() -> "tuple[object, str | None, list[str], str] | None":
        """Pick the active account and decide where to start_playback.
        Returns (sp, device_id, stop_renderers, account_name) or None
        if no account / device combination can be reached."""
        # Pick the account first. If AirPlay is active and matches a
        # household account, we use THAT — so the user who is
        # AirPlaying gets their own Spotify content.
        renderers = await moode.active_renderers()
        airplay_active = bool(renderers.get("aplactive"))
        ac = await router.active(airplay_active=airplay_active)
        if ac is None:
            return None
        # Use existing resolver for device targeting + renderer
        # cleanup. Operates on this account's spotipy client.
        resolution = await resolve_target(ac.sp, moode, librespot_name)
        return ac.sp, resolution.device_id, resolution.stop_renderers, ac.account.name

    @tool()
    async def spotify_play(query: str, kind: str = "track") -> dict:
        """Search Spotify and start playback. kind is one of: track, album, artist, playlist."""
        resolved = await _resolve_for_play()
        if resolved is None:
            return {
                "error": "no spotify account configured. tell the user to "
                "set one up at jasper.local/spotify.",
            }
        sp, device_id, stops, account_name = resolved
        results = await asyncio.to_thread(sp.search, q=query, type=kind, limit=1)
        items = results.get(f"{kind}s", {}).get("items", [])
        if not items:
            return {"error": f"no {kind} found for: {query}"}
        if not device_id:
            return {
                "error": "no spotify target device available. tell the user "
                "to open spotify on their phone or check that moOde's "
                "spotify connect is running.",
            }
        if stops:
            await stop_renderers(moode, stops)
        if kind == "track":
            await asyncio.to_thread(
                sp.start_playback, device_id=device_id, uris=[items[0]["uri"]]
            )
        else:
            await asyncio.to_thread(
                sp.start_playback, device_id=device_id, context_uri=items[0]["uri"]
            )
        return {
            "ok": True,
            "playing": items[0].get("name", query),
            "account": account_name,
        }

    @tool()
    async def spotify_queue(query: str) -> dict:
        """Search Spotify for a track and add it to the playback queue."""
        resolved = await _resolve_for_play()
        if resolved is None:
            return {"error": "no spotify account configured"}
        sp, device_id, _, account_name = resolved
        results = await asyncio.to_thread(sp.search, q=query, type="track", limit=1)
        items = results.get("tracks", {}).get("items", [])
        if not items:
            return {"error": f"no track found for: {query}"}
        if not device_id:
            return {"error": "no spotify target device available"}
        await asyncio.to_thread(
            sp.add_to_queue, items[0]["uri"], device_id=device_id
        )
        return {
            "ok": True,
            "queued": items[0].get("name", query),
            "account": account_name,
        }

    return [spotify_play, spotify_queue]
