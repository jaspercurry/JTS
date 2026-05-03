from __future__ import annotations

import asyncio
import logging

from camilladsp import CamillaClient

logger = logging.getLogger(__name__)


class CamillaController:
    """Thin wrapper around pycamilladsp for ducking + volume tools.

    pycamilladsp is sync; we offload calls to a thread so we don't block the
    asyncio loop. Reconnect on failure rather than raising into the daemon.
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._client: CamillaClient | None = None
        self._lock = asyncio.Lock()

    def _ensure(self) -> CamillaClient:
        if self._client is None:
            client = CamillaClient(self._host, self._port)
            client.connect()
            self._client = client
        return self._client

    async def _call(self, fn):
        async with self._lock:
            try:
                return await asyncio.to_thread(fn, self._ensure())
            except Exception as e:
                logger.warning("camilla call failed, reconnecting: %s", e)
                self._client = None
                return await asyncio.to_thread(fn, self._ensure())

    async def get_volume_db(self) -> float:
        return float(await self._call(lambda c: c.volume.main_volume()))

    async def set_volume_db(self, db: float) -> None:
        await self._call(lambda c: c.volume.set_main_volume(float(db)))

    async def adjust_volume_db(self, delta_db: float) -> float:
        current = await self.get_volume_db()
        target = current + float(delta_db)
        await self.set_volume_db(target)
        return target


class Ducker:
    """Additive duck/restore around a voice session.

    Apply duck_db (negative number) on duck, reverse it on restore. Done
    additively so mid-session volume changes by the user (set_volume tool)
    persist after the session ends.
    """

    def __init__(self, camilla: CamillaController, duck_db: float) -> None:
        self._camilla = camilla
        self._duck_db = duck_db
        self._ducked = False

    async def duck(self) -> None:
        if self._ducked:
            return
        self._ducked = True
        await self._camilla.adjust_volume_db(self._duck_db)

    async def restore(self) -> None:
        if not self._ducked:
            return
        try:
            await self._camilla.adjust_volume_db(-self._duck_db)
        finally:
            self._ducked = False
