from __future__ import annotations

import asyncio
import logging

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class MicCapture:
    """Continuous mono 16 kHz mic capture, exposed as an asyncio queue.

    Output frames: 1280 samples (80 ms) of 16 kHz int16 mono — the
    openWakeWord-recommended frame size and small enough to keep Gemini
    Live responsive. Consumers (wake-word, Gemini session) see 16 kHz
    mono regardless of what the underlying mic does.

    Capture-side rate/channels are configurable because not every mic
    supports 16 kHz mono natively. PortAudio (sounddevice's backend) does
    NOT do automatic ALSA `plughw` resampling — opening a 48 kHz-only mic
    at 16 kHz raises `Invalid sample rate`. So we open at the device's
    supported rate (16000 for XVF3800, 48000 for MiniDSP UMIK-2 et al.),
    take channel 0, and polyphase-downsample to 16 kHz here.
    """

    OUTPUT_RATE = 16000
    OUTPUT_FRAME_SAMPLES = 1280  # 80 ms at 16 kHz

    def __init__(
        self,
        device: str | int,
        capture_rate: int = OUTPUT_RATE,
        capture_channels: int = 1,
    ) -> None:
        if capture_rate < self.OUTPUT_RATE:
            raise RuntimeError(
                f"capture_rate {capture_rate} must be >= {self.OUTPUT_RATE}"
            )
        if capture_rate % self.OUTPUT_RATE != 0:
            raise RuntimeError(
                f"capture_rate {capture_rate} must be an integer multiple "
                f"of {self.OUTPUT_RATE} (downsample ratio must be exact)"
            )
        self._device = device
        self._capture_rate = capture_rate
        self._capture_channels = capture_channels
        self._decimation = capture_rate // self.OUTPUT_RATE
        # Block size at the capture rate that yields exactly OUTPUT_FRAME_SAMPLES
        # frames at OUTPUT_RATE after downsampling.
        self._capture_block = self.OUTPUT_FRAME_SAMPLES * self._decimation
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)
        self._stream: sd.InputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            logger.debug("mic status: %s", status)
        if self._loop is None:
            return
        # Take channel 0 (mono). UMIK-2 et al. expose stereo, but the L
        # capsule is what we want for voice; R is silent or duplicate.
        ch0 = indata[:, 0]
        if self._decimation == 1:
            chunk = ch0.astype(np.int16, copy=True)
        else:
            # Polyphase resample with built-in anti-alias filter. We use
            # scipy here (already installed transitively for openwakeword)
            # rather than naive stride-decimation, which would alias voice
            # content above 8 kHz back into the audible band.
            from scipy.signal import resample_poly  # local import: keeps daemon startup fast
            resampled = resample_poly(
                ch0.astype(np.float32), up=1, down=self._decimation,
            )
            chunk = np.clip(resampled, -32768, 32767).astype(np.int16)
        # call_soon_threadsafe schedules _enqueue to run on the loop thread,
        # which is the only place asyncio.Queue.put_nowait can raise
        # QueueFull. Catching it here in the callback would never fire.
        self._loop.call_soon_threadsafe(self._enqueue, chunk)

    def _enqueue(self, chunk: np.ndarray) -> None:
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            logger.warning("mic queue full, dropping frame")

    async def __aenter__(self) -> "MicCapture":
        self._loop = asyncio.get_running_loop()
        self._stream = sd.InputStream(
            device=self._device,
            samplerate=self._capture_rate,
            channels=self._capture_channels,
            dtype="int16",
            blocksize=self._capture_block,
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
    """Plays Gemini's 24 kHz int16 mono PCM stream out to an ALSA device.

    The output device may not natively support 24 kHz mono — `jasper_dongle`
    (the shared dmix wrapping the Apple USB-C dongle) is fixed at 48 kHz
    and PortAudio doesn't go through ALSA's `plug` layer for rate
    conversion. So we let the caller configure an `output_rate` and
    polyphase-upsample 24 kHz → output_rate inside `write()`.
    """

    INPUT_RATE = 24000

    def __init__(
        self,
        device: str | int,
        output_rate: int = INPUT_RATE,
        gain_db: float = 0.0,
    ) -> None:
        if output_rate < self.INPUT_RATE:
            raise RuntimeError(
                f"output_rate {output_rate} must be >= {self.INPUT_RATE}"
            )
        if output_rate % self.INPUT_RATE != 0:
            raise RuntimeError(
                f"output_rate {output_rate} must be an integer multiple "
                f"of {self.INPUT_RATE} (upsample ratio must be exact)"
            )
        self._device = device
        self._output_rate = output_rate
        self._upsample = output_rate // self.INPUT_RATE
        # Static gain applied to TTS PCM before resample/write. Gemini
        # outputs at consistent level — no gain stage between us and the
        # dongle without this. gain_db<0 attenuates; gain_db=0 passthrough.
        self._gain_linear = float(10 ** (gain_db / 20.0)) if gain_db != 0.0 else 1.0
        self._stream: sd.RawOutputStream | None = None

    async def __aenter__(self) -> "TtsPlayout":
        self._stream = sd.RawOutputStream(
            device=self._device,
            samplerate=self._output_rate,
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
        # Fast path: no gain, no resample
        if self._upsample == 1 and self._gain_linear == 1.0:
            await asyncio.to_thread(self._stream.write, pcm)
            return
        # Apply gain (if non-passthrough) and/or upsample. Both share a
        # float32 intermediate so we only do one int16 cast at the end.
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if self._gain_linear != 1.0:
            arr = arr * self._gain_linear
        if self._upsample > 1:
            # Polyphase resample with built-in anti-alias filter. Same
            # reasoning as MicCapture's downsampler — naive zero-stuff
            # would create high-frequency images.
            from scipy.signal import resample_poly  # local: keep startup fast
            arr = resample_poly(arr, up=self._upsample, down=1)
        out_i16 = np.clip(arr, -32768, 32767).astype(np.int16)
        await asyncio.to_thread(self._stream.write, out_i16.tobytes())

    async def flush(self) -> None:
        """Drop any audio currently buffered inside sounddevice / ALSA so
        the speaker goes silent immediately. Used for barge-in: when the
        user interrupts the model, we want sub-50ms cutoff, not the
        100-300ms tail you'd get from waiting for buffered samples to
        finish playing.

        sounddevice's abort() stops the stream and discards pending
        samples (vs. stop() which finishes them). Restart with start()
        so the next write() works immediately."""
        if self._stream is None:
            return
        try:
            await asyncio.to_thread(self._stream.abort)
            await asyncio.to_thread(self._stream.start)
        except Exception as e:  # noqa: BLE001
            logger.warning("tts flush failed: %s", e)
