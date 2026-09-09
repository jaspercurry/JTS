# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from contextlib import suppress
from typing import TYPE_CHECKING

import numpy as np

from .audio_buffer import AudioBuffer, InputFrame
from .dsp_numpy import resample_poly
from .mic_presence import read_mic_presence
from . import wake_ports

# `sounddevice` (PortAudio bindings) is a Pi-side dep absent from the dev venv,
# so the two places that open a stream import it lazily and this module stays
# importable off-hardware. Pinned by tests/test_lazy_imports.py.

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import sounddevice as sd


class InputDeviceUnavailable(RuntimeError):
    """The primary microphone input could not be opened at startup.

    Raised by the voice daemon's leg factory when the must-have "on"
    wake leg's device won't open (absent card, PortAudio "No input
    device matching ...", busy capture, or a malformed/unbindable UDP
    transport). The daemon's ``main()`` catches it and exits
    ``VOICE_MIC_UNAVAILABLE_EXIT`` so systemd parks the unit cleanly
    instead of crash-looping toward ``StartLimitAction=reboot``."""

    def __init__(self, device: str, cause: BaseException | None = None) -> None:
        self.device = device
        detail = f": {type(cause).__name__}: {cause}" if cause is not None else ""
        super().__init__(
            f"primary microphone input {device!r} unavailable{detail}"
        )


def _log_audio_open_failure(role: str, device: str, exc: BaseException) -> None:
    """Dump environmental state when a sounddevice stream open fails.

    Best-effort: a logging helper must NEVER mask the underlying audio
    failure, so every snapshot path swallows its own exception. The caller
    still re-raises the original.
    """
    # The AEC reconciler already owns "is there a microphone". Once it has
    # confirmed absence, a capture-open failure is that same expected fact, so
    # log one line and skip the snapshot cascade. See jasper/mic_presence.py.
    if role == "MicCapture":
        try:
            if read_mic_presence().absent_confirmed:
                logger.warning(
                    "audio open failed (expected): role=capture device=%r — no "
                    "microphone present per the AEC reconciler; voice parked, "
                    "auto-starts on reconnect (%s)",
                    device, type(exc).__name__,
                )
                return
        except Exception:  # noqa: BLE001 — the gate must never mask the failure
            pass

    import sounddevice as sd  # lazy: optional dep — see module top.

    logger.error(
        "audio open failed: role=%s device=%r exc=%s: %s",
        role, device, type(exc).__name__, exc,
    )
    try:
        # A target device missing from this list means the dongle/mic
        # de-enumerated — the most common cause.
        devices = sd.query_devices()
        logger.error("audio open failed: portaudio devices = %s", list(devices))
    except Exception as e:  # noqa: BLE001
        logger.warning("audio open failed: query_devices snapshot failed: %s", e)
    for cmd, label in (
        (["aplay", "-l"], "aplay -l"),
        (["arecord", "-l"], "arecord -l"),
    ):
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=2.0,
            ).stdout
            logger.error("audio open failed: %s =\n%s", label, out.strip())
        except Exception as e:  # noqa: BLE001
            logger.warning("audio open failed: %s snapshot failed: %s", label, e)
    try:
        # USB-disconnect / xhci reset events correlate with dongle dropouts.
        out = subprocess.run(
            ["dmesg", "--ctime"],
            capture_output=True, text=True, timeout=2.0,
        ).stdout
        tail = "\n".join(out.strip().splitlines()[-20:])
        logger.error("audio open failed: dmesg tail =\n%s", tail)
    except Exception as e:  # noqa: BLE001
        logger.warning("audio open failed: dmesg snapshot failed: %s", e)


# Capture consumers discard audio older than 1 s. Slow turn acquisition has
# its own 20 s budget; it must not make idle wake detection replay a backlog.
CAPTURE_MAX_AGE_SEC = 1.0
CAPTURE_MAX_FRAMES = 64
CAPTURE_GAP_SEC = 0.32  # Four normal 80 ms packets without input.


class _CaptureQueue:
    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ready = asyncio.Event()
        self._lock = threading.Lock()
        self._buffer = AudioBuffer(CAPTURE_MAX_FRAMES, CAPTURE_MAX_AGE_SEC)
        self._notification_pending = False
        self._last_received_at: float | None = None
        self.last_frame: InputFrame | None = None

    @property
    def dropped_frames(self) -> int:
        return self._buffer.dropped_frames

    def put_nowait(
        self, pcm, captured_at: float | None = None, *, discontinuity: bool = False,
    ) -> None:
        now = time.monotonic() if captured_at is None else captured_at
        with self._lock:
            discontinuity |= (
                self._last_received_at is not None
                and now - self._last_received_at > CAPTURE_GAP_SEC
            )
            self._last_received_at = now
            self._buffer.append(pcm, now, discontinuity=discontinuity)
            # PortAudio's thread retains at most one loop notification, not
            # one scheduled callback (and PCM array) per captured frame.
            if not self._notification_pending:
                self._notification_pending = True
                self._loop.call_soon_threadsafe(self._notify)

    def _notify(self) -> None:
        with self._lock:
            self._notification_pending = False
            self._ready.set()

    async def get(self):
        while True:
            with self._lock:
                frame = self._buffer.pop()
                if frame is not None:
                    self.last_frame = frame
                    return frame.pcm
                self._ready.clear()
            await self._ready.wait()


class MicCapture:
    """Capture channel 0 as 80 ms mono frames at 16 kHz.

    PortAudio does not resample: the source must support the requested
    capture rate, which must be an integer multiple of the output rate.
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
        # _CaptureQueue binds a running loop; construction must stay callable
        # from sync code.
        self._queue: _CaptureQueue | None = None
        self._stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            logger.debug("mic status: %s", status)
        if self._queue is None:
            return
        captured_at = time.monotonic()
        # Take channel 0 (mono). UMIK-2 et al. expose stereo, but the L
        # capsule is what we want for voice; R is silent or duplicate.
        ch0 = indata[:, 0]
        if self._decimation == 1:
            chunk = ch0.astype(np.int16, copy=True)
        else:
            # Polyphase resample with a built-in anti-alias filter, not
            # naive stride-decimation, which would alias voice content
            # above 8 kHz back into the audible band.
            resampled = resample_poly(ch0, up=1, down=self._decimation)
            chunk = np.clip(resampled, -32768, 32767).astype(np.int16)
        self._queue.put_nowait(chunk, captured_at, discontinuity=bool(status))

    @property
    def last_frame(self) -> InputFrame | None:
        return self._queue.last_frame if self._queue is not None else None

    @property
    def dropped_frames(self) -> int:
        return self._queue.dropped_frames if self._queue is not None else 0

    async def __aenter__(self) -> "MicCapture":
        import sounddevice as sd  # lazy: optional dep — see module top.

        self._queue = _CaptureQueue()
        try:
            self._stream = sd.InputStream(
                device=self._device,
                samplerate=self._capture_rate,
                channels=self._capture_channels,
                dtype="int16",
                blocksize=self._capture_block,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as e:  # noqa: BLE001
            if self._stream is not None:
                with suppress(Exception):
                    self._stream.close()
                self._stream = None
            _log_audio_open_failure("MicCapture", self._device, e)
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            except BaseException:  # noqa: BLE001
                with suppress(Exception):
                    stream.close()
                raise
            else:
                stream.close()

    async def frames(self):
        if self._queue is None:
            raise RuntimeError("MicCapture.frames() called before __aenter__")
        while True:
            yield await self._queue.get()


class UdpMicCapture:
    """Mono 16 kHz int16 UDP input. Timestamps measure application ingress;
    the unsequenced PCM carrier cannot expose sender or kernel queue age.
    """

    OUTPUT_RATE = MicCapture.OUTPUT_RATE
    OUTPUT_FRAME_SAMPLES = MicCapture.OUTPUT_FRAME_SAMPLES

    def __init__(
        self, host: str = "127.0.0.1", port: int = 9876,
    ) -> None:
        self._host = host
        self._port = port
        self._queue: _CaptureQueue | None = None
        self._transport: asyncio.BaseTransport | None = None

    @property
    def last_frame(self) -> InputFrame | None:
        return self._queue.last_frame if self._queue is not None else None

    @property
    def dropped_frames(self) -> int:
        return self._queue.dropped_frames if self._queue is not None else 0

    async def __aenter__(self) -> "UdpMicCapture":
        loop = asyncio.get_running_loop()
        self._queue = _CaptureQueue()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _UdpMicProtocol(self._queue),
                local_addr=(self._host, self._port),
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "UdpMicCapture bind failed: host=%s port=%d exc=%s: %s",
                self._host, self._port, type(e).__name__, e,
            )
            raise
        logger.info(
            "UdpMicCapture listening on %s:%d (frame=%d samples @ %d Hz)",
            self._host, self._port, self.OUTPUT_FRAME_SAMPLES, self.OUTPUT_RATE,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def frames(self):
        if self._queue is None:
            raise RuntimeError("UdpMicCapture.frames() called before __aenter__")
        while True:
            yield await self._queue.get()


class _UdpMicProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: _CaptureQueue) -> None:
        self._queue = queue
        self._gap_pending = False

    def datagram_received(self, data: bytes, _addr) -> None:
        if not data:
            return
        if len(data) % 2 != 0:
            self._gap_pending = True
            logger.warning(
                "UdpMicCapture: dropping malformed packet (%d bytes, odd)",
                len(data),
            )
            return
        chunk = np.frombuffer(data, dtype=np.int16)
        self._queue.put_nowait(chunk, discontinuity=self._gap_pending)
        self._gap_pending = False


def make_mic_capture(
    device: str | int,
    capture_rate: int = MicCapture.OUTPUT_RATE,
    capture_channels: int = 1,
):
    """Construct the right mic-capture flavour for a device string.

    `device` matching `udp:PORT` / `udp://HOST:PORT` → `UdpMicCapture`
    (the AEC bridge sends post-processed mic to that socket;
    `capture_rate` / `capture_channels` are ignored because the
    bridge has already resampled to 16 kHz mono and the format is
    fixed at the bridge↔voice transport contract).

    Anything else → `MicCapture` (PortAudio + ALSA path: chip-direct
    via `Array`, or any other USB mic).
    """
    if isinstance(device, str):
        udp = wake_ports.parse_udp_device(device)
        if udp is not None:
            host, port = udp
            return UdpMicCapture(host=host, port=port)
    return MicCapture(
        device, capture_rate=capture_rate, capture_channels=capture_channels,
    )
