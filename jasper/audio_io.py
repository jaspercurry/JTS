from __future__ import annotations

import asyncio
import logging
import subprocess
import time

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


def _log_audio_open_failure(role: str, device: str, exc: BaseException) -> None:
    """Dump environmental state when a sounddevice stream open fails.

    Called from MicCapture / TtsPlayout `__aenter__` immediately
    before re-raising. The bare exception (typically
    `ValueError: No <kind> device matching '<name>'`) doesn't tell
    us whether ALSA can see the device, whether dmesg has a recent
    USB-disconnect line, or what PortAudio actually has enumerated —
    all common when the Apple dongle de-enumerates after losing
    its analog load, or when the AEC bridge's loopback isn't fed.
    Capturing this snapshot once at failure beats blind reasoning
    from a stack trace days later.

    Best-effort: a logging helper must NEVER mask or suppress the
    underlying audio failure, so every snapshot path is wrapped in
    `try/except` and falls through to `logger.warning` rather than
    raising. The caller still re-raises the original exception.
    """
    logger.error(
        "audio open failed: role=%s device=%r exc=%s: %s",
        role, device, type(exc).__name__, exc,
    )
    try:
        # PortAudio's view — what sounddevice could see at the
        # moment of failure. If our target device isn't in this
        # list, the dongle/mic disappeared (most common cause).
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
        # Last 20 lines of dmesg catches USB-disconnect / xhci
        # reset events that often correlate with dongle dropouts.
        out = subprocess.run(
            ["dmesg", "--ctime"],
            capture_output=True, text=True, timeout=2.0,
        ).stdout
        tail = "\n".join(out.strip().splitlines()[-20:])
        logger.error("audio open failed: dmesg tail =\n%s", tail)
    except Exception as e:  # noqa: BLE001
        logger.warning("audio open failed: dmesg snapshot failed: %s", e)


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
        # Lazy queue init — see UdpMicCapture for rationale (construct
        # from sync code shouldn't fail on stale event-loop state).
        self._queue: asyncio.Queue[np.ndarray] | None = None
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
        if self._queue is None:
            return  # callback fired before __aenter__ completed; drop
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            logger.warning("mic queue full, dropping frame")

    async def __aenter__(self) -> "MicCapture":
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=64)
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
            # Common causes: chip not enumerated (USB-OUT shared
            # bus reset), or device-name typo. (The pre-PR-2
            # "bridge daemon down" failure mode is now handled by
            # UdpMicCapture's separate code path.) Dump full ALSA +
            # PortAudio state so the next restart's log shows what
            # was visible at failure.
            _log_audio_open_failure("MicCapture", self._device, e)
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def frames(self):
        if self._queue is None:
            raise RuntimeError("MicCapture.frames() called before __aenter__")
        while True:
            yield await self._queue.get()


class UdpMicCapture:
    """Mic capture that receives mono 16 kHz int16 frames over UDP.

    Same `frames()` async-generator contract as `MicCapture` so
    voice_daemon's WakeLoop is transport-agnostic. Pairs with
    jasper-aec-bridge sending UDP packets of `OUTPUT_FRAME_SAMPLES`
    int16 samples to `127.0.0.1:<port>` (the AEC'd mic stream).

    Why UDP instead of snd-aloop LoopbackAEC: snd-aloop's
    `loopback_cable` struct persists in kernel state across consumer
    death; a SIGKILL'd consumer leaves the cable half-bound with the
    internal timer wedged (`hw_ptr=0`), and only `rmmod && modprobe
    snd_aloop` (after stopping every consumer) or a reboot can
    recover. Hit in production 2026-05-11.  UDP localhost has no
    kernel-side state to corrupt: either side can crash without
    affecting the other, `sendto()` is non-blocking (eliminates the
    bridge SIGTERM-observability issue), and there's no module to
    reload.  ~256 kbps loopback traffic is effectively zero-loss on
    Linux's `lo`.  Standard pattern in Mumble, VoIP gateways, Snapcast.
    """

    OUTPUT_RATE = MicCapture.OUTPUT_RATE
    OUTPUT_FRAME_SAMPLES = MicCapture.OUTPUT_FRAME_SAMPLES

    def __init__(
        self, host: str = "127.0.0.1", port: int = 9876,
    ) -> None:
        self._host = host
        self._port = port
        # Queue is lazily created in __aenter__ so the class is safe
        # to construct from sync code (e.g. unit tests that just
        # assert factory dispatch). In Python 3.9 `asyncio.Queue()`
        # calls `get_event_loop()` at construction; if there's a
        # stale-closed loop in the thread (a real-world scenario in
        # test suites), it raises. Deferring keeps the class
        # construct-anywhere.
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._transport: asyncio.BaseTransport | None = None

    async def __aenter__(self) -> "UdpMicCapture":
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=64)
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
    """Translates UDP datagrams of int16 PCM into queue items.

    Each datagram is one mic frame (`OUTPUT_FRAME_SAMPLES` int16
    samples = 2 * 1280 = 2560 bytes by default). Out-of-order /
    lost packets are effectively impossible on `lo` at our rate, so
    no sequence number / reordering buffer.
    """

    def __init__(self, queue: asyncio.Queue[np.ndarray]) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, _addr) -> None:
        if not data:
            return
        # Defensive: a malformed sender could send odd byte counts.
        # `np.frombuffer` would raise a ValueError; we'd rather drop
        # the bad packet and keep the daemon healthy.
        if len(data) % 2 != 0:
            logger.warning(
                "UdpMicCapture: dropping malformed packet (%d bytes, odd)",
                len(data),
            )
            return
        chunk = np.frombuffer(data, dtype=np.int16)
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            logger.warning("UdpMicCapture queue full, dropping frame")


def parse_udp_device(device: str) -> tuple[str, int] | None:
    """If `device` denotes a UDP mic source, return (host, port).

    Accepted forms:
      - `udp://<host>:<port>`     full URL form
      - `udp:<port>`              shorthand, host = 127.0.0.1

    Returns None if the device string is not a UDP form, so callers
    fall through to the PortAudio path. Raises ValueError if the
    string starts with `udp` but is malformed (typo guard).
    """
    if not device.lower().startswith("udp"):
        return None
    rest = device[3:]
    if rest.startswith("://"):
        rest = rest[3:]
        if ":" not in rest:
            raise ValueError(
                f"udp device {device!r} missing port (expected udp://HOST:PORT)"
            )
        host, port_str = rest.rsplit(":", 1)
    elif rest.startswith(":"):
        host = "127.0.0.1"
        port_str = rest[1:]
    else:
        raise ValueError(
            f"udp device {device!r} malformed; "
            f"use 'udp:PORT' or 'udp://HOST:PORT'"
        )
    try:
        port = int(port_str)
    except ValueError as e:
        raise ValueError(
            f"udp device {device!r} has non-integer port {port_str!r}"
        ) from e
    if not (1 <= port <= 65535):
        raise ValueError(f"udp device {device!r} port {port} out of range")
    return host, port


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
        udp = parse_udp_device(device)
        if udp is not None:
            host, port = udp
            return UdpMicCapture(host=host, port=port)
    return MicCapture(
        device, capture_rate=capture_rate, capture_channels=capture_channels,
    )


class TtsPlayout:
    """Plays Gemini's 24 kHz int16 mono PCM stream out to an ALSA device.

    The output device may not natively support 24 kHz mono — `jasper_dongle`
    (the shared dmix wrapping the Apple USB-C dongle) is fixed at 48 kHz
    and PortAudio doesn't go through ALSA's `plug` layer for rate
    conversion. So we let the caller configure an `output_rate` and
    polyphase-upsample 24 kHz → output_rate inside `write()`.

    Hearing-safety bounds on gain. TTS bypasses CamillaDSP entirely
    (writes to the dongle's dmix alongside CamillaDSP's output — see
    docs/audio-paths.md), so its level is set by us alone. A bug,
    malformed env value, or stale volume reading from TtsVolumeTracker
    must NEVER be allowed to play TTS at a level that could damage
    hearing. `set_gain_db` clamps every input to [MIN_TTS_GAIN_DB,
    MAX_TTS_GAIN_DB]; the cap exists even if every other check
    upstream fails. -6 dB ceiling means TTS peaks max out around
    -9 dBFS at the dongle (Gemini's source peaks ~-3 dBFS), well
    below the dongle's reference output level.
    """

    INPUT_RATE = 24000

    # Absolute ceiling on TTS gain. Even if main_volume + offset would
    # push higher (positive offset, runaway, etc.), we clamp here. The
    # whole point: never let TTS get loud enough to hurt.
    MAX_TTS_GAIN_DB = -6.0
    # Floor — below this, TTS is effectively silent. Used when the
    # user mutes, when Camilla is unreachable at startup, or when a
    # volume reading looks malformed.
    MIN_TTS_GAIN_DB = -60.0

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
        # Linear gain factor applied before resample/write. Updated at
        # runtime via set_gain_db so TTS tracks Camilla's main_volume.
        # Initial value is the floor (effectively silent) so the daemon
        # cannot accidentally play TTS loud during the brief window
        # between TtsPlayout construction and the first volume read.
        # The volume tracker's first tick sets a real value; until then
        # we'd rather have inaudible TTS than blast.
        self._gain_linear = float(10 ** (self.MIN_TTS_GAIN_DB / 20.0))
        self._gain_db = self.MIN_TTS_GAIN_DB
        self._stream: sd.RawOutputStream | None = None
        # One-shot warning latch: if a caller invokes write() before
        # entering the async context (so _stream is still None), log
        # once. The class is a context manager and the underlying
        # ALSA stream only opens in __aenter__; without that, write()
        # used to silently no-op, which was the cause of "I can't
        # hear the cue" being mis-diagnosed as routing problems.
        self._closed_stream_warned = False
        # Apply the constructor's gain_db through the same clamp +
        # validation path as runtime updates. If a caller passes the
        # legacy "-8.0 fixed gain" value, this becomes the active
        # level. Live tracking will overwrite it on the first tick.
        self.set_gain_db(gain_db)

    def set_gain_db(self, db: float) -> None:
        """Update TTS gain. Clamped to [MIN, MAX]; non-finite or
        out-of-range inputs are rejected (prior gain held). Single-
        float assignment is atomic under the GIL, so no lock is
        needed for the read path in write()."""
        try:
            db = float(db)
        except (TypeError, ValueError):
            logger.warning("tts gain rejected (not a number): %r", db)
            return
        if db != db or db in (float("inf"), float("-inf")):
            logger.warning("tts gain rejected (non-finite): %r", db)
            return
        clamped = max(self.MIN_TTS_GAIN_DB, min(self.MAX_TTS_GAIN_DB, db))
        if clamped == self._gain_db:
            return
        # 0.0 dB → 1.0 linear; floor → ~0.001 linear. Computed once
        # per change, not per write.
        self._gain_linear = float(10 ** (clamped / 20.0))
        self._gain_db = clamped
        if clamped != db:
            logger.info(
                "tts gain set: requested %.1f dB → clamped to %.1f dB",
                db, clamped,
            )
        else:
            logger.info("tts gain set: %.1f dB", clamped)

    @property
    def gain_db(self) -> float:
        return self._gain_db

    async def __aenter__(self) -> "TtsPlayout":
        # Open as STEREO even though our input is mono. The dongle's
        # dmix (`pcm.jasper_out` in /root/.asoundrc) is configured at
        # channels=2 with no plug layer; opening at channels=1
        # against it makes PortAudio do something quietly broken —
        # mono samples land in the stereo frame interleave as if they
        # were L/R pairs, and audio comes out at half speed with
        # weird amplitude. Manual mono→stereo duplication in write()
        # is unambiguous and matches the dmix's native shape.
        try:
            self._stream = sd.RawOutputStream(
                device=self._device,
                samplerate=self._output_rate,
                channels=2,
                dtype="int16",
            )
            self._stream.start()
        except Exception as e:  # noqa: BLE001
            # Most common cause of "No output device matching ..." is
            # the Apple dongle de-enumerating because nothing's
            # plugged into its 3.5 mm jack (it loses USB Audio class
            # exposure without an analog load). Dump enough state to
            # tell that case apart from "device exists but is busy"
            # or "PortAudio internal error" — the bare ValueError
            # alone wasn't enough to root-cause the 9000+ restart
            # spiral on 2026-05-10.
            _log_audio_open_failure("TtsPlayout", self._device, e)
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def write(self, pcm: bytes) -> None:
        """Input is MONO int16 PCM at INPUT_RATE (24kHz) — same shape
        Gemini Live emits and what cue WAVs are stored at. Internally
        we apply gain + upsample + mono→stereo duplication, then
        hand off to the (stereo) sounddevice stream."""
        if self._stream is None:
            if not self._closed_stream_warned:
                logger.warning(
                    "TtsPlayout.write called on a closed stream — "
                    "%d bytes silently dropped. Did you forget "
                    "`async with tts:`? (Suppressing further such "
                    "warnings for this instance.)",
                    len(pcm),
                )
                self._closed_stream_warned = True
            return
        # Always go through the numpy pipeline so the mono→stereo
        # duplication at the end runs uniformly. The dropped fast
        # path was for "no gain, no upsample" which is a test-only
        # config in practice — production always has both.
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if self._gain_linear != 1.0:
            arr = arr * self._gain_linear
        if self._upsample > 1:
            # Polyphase resample with built-in anti-alias filter. Same
            # reasoning as MicCapture's downsampler — naive zero-stuff
            # would create high-frequency images.
            from scipy.signal import resample_poly  # local: keep startup fast
            arr = resample_poly(arr, up=self._upsample, down=1)
        # Mono → stereo: each mono sample becomes a (L, R) pair with
        # L=R. np.repeat(arr, 2) interleaves correctly: [s0, s0, s1,
        # s1, …]. The stream is opened at channels=2 so this is the
        # exact byte layout it expects.
        mono_i16 = np.clip(arr, -32768, 32767).astype(np.int16)
        stereo_i16 = np.repeat(mono_i16, 2)
        write_start = time.monotonic()
        await asyncio.to_thread(self._stream.write, stereo_i16.tobytes())
        write_ms = (time.monotonic() - write_start) * 1000
        chunk_ms = len(mono_i16) * 1000 / self._output_rate
        # Slow writes stall consumer dequeue, letting the idle watchdog's tail timer fire mid-playback.
        if write_ms > chunk_ms + 100:
            logger.warning(
                "tts.write slow: %.0fms for %.0fms of audio "
                "(%d frames @ %d Hz)",
                write_ms, chunk_ms, len(mono_i16), self._output_rate,
            )

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
