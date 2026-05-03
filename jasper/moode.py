from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from mpd.asyncio import MPDClient

logger = logging.getLogger(__name__)


class MoodeClient:
    """moOde REST API for the few commands it covers; MPD for the rest.

    moOde's REST surface (from the setup guide) is small: toggle_play_pause,
    set_volume, get_currentsong, get_volume. Anything else (next/previous/
    seek) goes through the MPD protocol on port 6600.
    """

    def __init__(self, base_url: str, mpd_host: str, mpd_port: int) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=5.0)
        self._mpd_host = mpd_host
        self._mpd_port = mpd_port
        self._mpd: MPDClient | None = None
        self._mpd_lock = asyncio.Lock()

    async def _rest(self, cmd: str) -> str:
        url = f"{self._base_url}/command/"
        r = await self._http.get(url, params={"cmd": cmd})
        r.raise_for_status()
        return r.text

    async def toggle_play_pause(self) -> None:
        await self._rest("toggle_play_pause")

    async def get_currentsong(self) -> dict[str, Any]:
        # moOde returns JSON for get_currentsong per the setup guide.
        url = f"{self._base_url}/command/"
        r = await self._http.get(url, params={"cmd": "get_currentsong"})
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}

    async def _mpd_client(self) -> MPDClient:
        if self._mpd is None:
            client = MPDClient()
            await client.connect(self._mpd_host, self._mpd_port)
            self._mpd = client
        return self._mpd

    async def _mpd_call(self, fn_name: str, *args):
        async with self._mpd_lock:
            try:
                client = await self._mpd_client()
                return await getattr(client, fn_name)(*args)
            except Exception as e:
                logger.warning("mpd call failed, reconnecting: %s", e)
                self._mpd = None
                client = await self._mpd_client()
                return await getattr(client, fn_name)(*args)

    async def next_track(self) -> None:
        await self._mpd_call("next")

    async def previous_track(self) -> None:
        await self._mpd_call("previous")

    async def play(self) -> None:
        await self._mpd_call("play")

    async def pause(self) -> None:
        await self._mpd_call("pause", 1)

    async def status(self) -> dict[str, Any]:
        return dict(await self._mpd_call("status"))

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._mpd is not None:
            self._mpd.disconnect()
            self._mpd = None
