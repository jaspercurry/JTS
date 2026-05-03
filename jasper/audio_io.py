from __future__ import annotations

import asyncio
import logging

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class MicCapture:
    """Continuous mono 16 kHz mic capture, exposed as an asyncio queue.

    Audio frames are 1280 samples (80 ms) — the openWakeWord-recommended frame
    size and small enough to keep Gemini Live responsive.
    """

    FRAME_SAMPLES = 1280
    SAMPLE_RATE = 16000

    def __init__(self, device: str | int) -> None:
        self._device = device
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)
        self._stream: sd.InputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            logger.debug("mic status: %s", status)
        if self._loop is None:
            return
        # int16 mono frame; copy because PortAudio reuses the buffer
        chunk = indata[:, 0].astype(np.int16, copy=True)
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, chunk)
        except asyncio.QueueFull:
            logger.warning("mic queue full, dropping frame")

    async def __aenter__(self) -> "MicCapture":
        self._loop = asyncio.get_running_loop()
        self._stream = sd.InputStream(
            device=self._device,
            samplerate=self.SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=self.FRAME_SAMPLES,
            callback=self._callback,
        )
        self._stream.start()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def frames(self):
        while True:
            yield await self._queue.get()


class TtsPlayout:
    """Plays Gemini's 24 kHz int16 PCM stream out to an ALSA device."""

    SAMPLE_RATE = 24000

    def __init__(self, device: str | int) -> None:
        self._device = device
        self._stream: sd.RawOutputStream | None = None

    async def __aenter__(self) -> "TtsPlayout":
        self._stream = sd.RawOutputStream(
            device=self._device,
            samplerate=self.SAMPLE_RATE,
            channels=1,
            dtype="int16",
        )
        self._stream.start()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def write(self, pcm: bytes) -> None:
        if self._stream is None:
            return
        await asyncio.to_thread(self._stream.write, pcm)
