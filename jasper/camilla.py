from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

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

    async def get_volume_and_mute(self) -> tuple[float, bool]:
        """Single round-trip read of main_volume + main_mute. Used by the
        TTS-gain tracker, which needs to honor mute as well as volume —
        if the user has muted the speaker, TTS shouldn't talk over the
        silence they asked for."""
        def read(c):
            return float(c.volume.main_volume()), bool(c.volume.main_mute())
        return await self._call(read)

    async def get_playback_rms(self) -> tuple[float, float]:
        """Per-channel RMS of CamillaDSP's playback signal in dBFS — the
        level just before the DAC, AFTER every attenuation stage on the
        music chain (source track loudness, AirPlay sender volume,
        Spotify Connect sender volume, MPD volume, Camilla main_volume,
        room correction filters, etc). This is what the TTS gain
        tracker uses to size TTS to the actual perceived music level
        instead of guessing at any single attenuation stage.

        Returns (left_db, right_db). Returns (-inf, -inf) on silence
        — pycamilladsp may report None / very negative numbers when
        the chunk has no signal."""
        def read(c):
            levels = c.levels.playback_rms()
            l = float(levels[0]) if levels and levels[0] is not None else float("-inf")
            r = float(levels[1]) if len(levels) > 1 and levels[1] is not None else l
            return l, r
        return await self._call(read)

    async def set_volume_db(self, db: float) -> None:
        await self._call(lambda c: c.volume.set_main_volume(float(db)))

    async def adjust_volume_db(self, delta_db: float) -> float:
        current = await self.get_volume_db()
        target = current + float(delta_db)
        await self.set_volume_db(target)
        return target


class Ducker:
    """Voice-session ducking around CamillaDSP main_volume.

    `duck()` lowers camilla by `duck_db` (additive). `restore()` reads the
    coordinator's canonical target dB and writes it absolutely.

    Why asymmetric: at duck time nothing else is competing for camilla
    (the voice session is just opening); additive is fine. At restore
    time, anything could have happened during the ducked window —
    crucially, the dial / voice tools / external slider observers could
    have changed `listening_level`. The previous implementation used
    additive restore (`+= -duck_db`), which wedged camilla at
    `pre_duck_value + delta` if any other writer touched it during the
    duck. Real symptom: dial twist during a voice turn → restore
    overshoots by the duck delta → camilla pinned out-of-range positive
    → sustained clipping when the next source connects. Reading the
    canonical target on restore makes the behavior independent of any
    interleaved writes.
    """

    def __init__(
        self,
        camilla: CamillaController,
        duck_db: float,
        target_db_provider: Callable[[], Awaitable[float]],
    ) -> None:
        self._camilla = camilla
        self._duck_db = duck_db
        self._target_db_provider = target_db_provider
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
            target_db = await self._target_db_provider()
            await self._camilla.set_volume_db(target_db)
        finally:
            self._ducked = False
