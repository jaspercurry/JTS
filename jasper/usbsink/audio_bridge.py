"""Audio bridge: UAC2Gadget capture → Loopback playback.

Two sounddevice streams connected by a bounded queue:

    sd.InputStream(UAC2Gadget, 48k S32 stereo)
        │  capture callback: convert S32→S16, compute RMS, enqueue
        ▼
    queue.Queue(maxsize=N)
        │
        ▼
    sd.OutputStream(Loopback,0,0, 48k S16 stereo)
        │  playback callback: dequeue (or silence on underrun), gate
        │  on `preempted` (write zeros instead of audio)

Two separate streams (not one duplex `sd.Stream`) because PortAudio's
duplex mode requires both ends use the same underlying ALSA device
context; UAC2Gadget and Loopback are different cards. Two streams
share a Python queue between their PortAudio threads; both threads
run lock-free except for the queue.

The bridge exposes minimal external surface:
  - `start()` / `stop()` for lifecycle
  - `set_preempted(bool)` for the mux preempt protocol
  - `last_rms_dbfs` for the state publisher to read (-inf when no audio)
  - `frames_passed` / `frames_dropped` counters for diagnostics

State publishing, preempt HTTP, mux integration, volume bridging —
all of that lives in sibling modules and composes on top of this. The
bridge itself stays small and audio-only.
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Stream parameters. Both ends are stereo @ 48 kHz to match snd-aloop
# and the dongle dmix. Different sample sizes:
#   - capture: S32_LE (the UAC2 gadget descriptor's c_ssize=4)
#   - playback: S16_LE (what shairport/librespot/bluez-alsa write into
#     hw:Loopback,0,0, so CamillaDSP captures it at S32_LE via plug
#     wrapping like the rest of the music chain)
SAMPLE_RATE = 48000
CHANNELS = 2
# Block size = 10 ms. Small enough that preempt + host pause/resume
# transitions land within one mux tick; large enough that PortAudio's
# per-callback overhead is amortized.
BLOCK_FRAMES = 480

# Queue capacity in blocks. At 10 ms/block, 8 blocks = 80 ms of slack
# before the output side starts dropping frames. Sized to absorb a
# brief stall in either direction (e.g. systemd reset_failed kick).
QUEUE_MAXBLOCKS = 8


@dataclass
class BridgeStats:
    """Lightweight counters for diagnostics. Reads are not locked —
    a torn read is benign (only used for logging snapshots)."""
    frames_captured: int = 0
    frames_played: int = 0
    frames_dropped_full: int = 0
    frames_underrun: int = 0
    capture_errors: int = 0
    playback_errors: int = 0
    started_at_mono: float = field(default_factory=time.monotonic)


class AudioBridge:
    """Captures the host's audio from the UAC2Gadget ALSA card and
    replays it into hw:Loopback,0,0 where the rest of the music chain
    picks it up.

    Lifecycle:
        bridge = AudioBridge(capture_device="UAC2Gadget", ...)
        bridge.start()         # opens both streams; non-blocking
        bridge.set_preempted(True)   # silence output (mux preempt)
        bridge.stop()          # closes streams

    Thread safety: `set_preempted` is racy-but-safe — Python attribute
    writes on a single bool are atomic. `last_rms_dbfs` and the stats
    counters are written from the capture callback thread and read by
    main-thread diagnostics; tiny tearing in the int counters is benign.
    """

    def __init__(
        self,
        *,
        capture_device: str = "UAC2Gadget",
        playback_device: str = "hw:CARD=Loopback,DEV=0",
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        block_frames: int = BLOCK_FRAMES,
        queue_maxblocks: int = QUEUE_MAXBLOCKS,
    ) -> None:
        self._capture_device = capture_device
        self._playback_device = playback_device
        self._sample_rate = sample_rate
        self._channels = channels
        self._block_frames = block_frames
        # PortAudio callback runs on a high-priority thread; we share
        # an int16 array buffer via a bounded queue. put_nowait/
        # get_nowait keep both callbacks lock-free.
        self._queue: queue.Queue = queue.Queue(maxsize=queue_maxblocks)
        self._preempted = False
        self._last_rms_dbfs: float = float("-inf")
        self.stats = BridgeStats()

        # Streams populated by start().
        self._in_stream = None  # type: Optional["sd.RawInputStream"]
        self._out_stream = None  # type: Optional["sd.RawOutputStream"]
        self._started = False
        # Lifecycle lock — start/stop are infrequent but might race
        # in tests.
        self._lifecycle_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open both streams. Raises on open failure — systemd's
        Restart=on-failure handles recovery."""
        with self._lifecycle_lock:
            if self._started:
                return
            import sounddevice as sd  # Pi-side dep, lazy

            # sd.RawInputStream / RawOutputStream avoid the numpy
            # roundtrip in PortAudio's callback boundary; capture
            # callback gets raw bytes, copies into a temp np array for
            # RMS + conversion, then queues bytes. Compared with
            # sd.InputStream + numpy buffers, this saves one allocation
            # per callback and keeps the per-frame Python work bounded.
            self._in_stream = sd.RawInputStream(
                device=self._capture_device,
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="int32",  # UAC2 gadget descriptor c_ssize=4
                blocksize=self._block_frames,
                callback=self._capture_callback,
            )
            self._out_stream = sd.RawOutputStream(
                device=self._playback_device,
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="int16",  # snd-aloop renderer-side convention
                blocksize=self._block_frames,
                callback=self._playback_callback,
            )
            # Start playback first so the loopback subdevice is open
            # for write before the gadget side starts producing —
            # avoids an immediate underrun spike at boot.
            self._out_stream.start()
            self._in_stream.start()
            self._started = True
            self.stats.started_at_mono = time.monotonic()
            logger.info(
                "event=usbsink.bridge_started capture=%s playback=%s "
                "rate=%d channels=%d block=%d",
                self._capture_device, self._playback_device,
                self._sample_rate, self._channels, self._block_frames,
            )

    def stop(self) -> None:
        """Close both streams. Idempotent; safe to call multiple times."""
        with self._lifecycle_lock:
            if not self._started:
                return
            # Stop the producer first so the queue stops growing,
            # then the consumer.
            for s, label in (
                (self._in_stream, "capture"),
                (self._out_stream, "playback"),
            ):
                if s is None:
                    continue
                try:
                    s.stop()
                    s.close()
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "event=usbsink.bridge_stream_close_failed "
                        "stream=%s error=%s", label, e,
                    )
            self._in_stream = None
            self._out_stream = None
            self._started = False
            logger.info("event=usbsink.bridge_stopped")

    def set_preempted(self, preempted: bool) -> None:
        """Mux preempt protocol. When True, playback callback emits
        silence regardless of what's in the queue. The capture side
        still runs (RMS keeps updating) so a host pause-then-resume can
        be detected and trigger a new mux-side transition."""
        if self._preempted == preempted:
            return
        self._preempted = preempted
        logger.info(
            "event=usbsink.preempt_changed preempted=%s",
            "true" if preempted else "false",
        )

    @property
    def is_preempted(self) -> bool:
        return self._preempted

    @property
    def last_rms_dbfs(self) -> float:
        """Most recent capture block's RMS in dBFS. -inf when no audio
        is flowing (host disconnected or silent). State publisher reads
        this at its own cadence."""
        return self._last_rms_dbfs

    @property
    def is_running(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # PortAudio callbacks. These run on PortAudio's audio thread —
    # avoid logging on the hot path (a stuck logger.handler can wedge
    # the audio thread). Errors are counted and surfaced via stats.
    # ------------------------------------------------------------------

    def _capture_callback(self, indata, frames, time_info, status) -> None:
        if status:
            # ALSA underruns / overflows on the gadget side. Counted,
            # not logged per-frame — too chatty. The diagnostic thread
            # in daemon.py logs the counter delta on a low cadence.
            self.stats.capture_errors += 1

        # RMS in S32 land. Each sample is signed int32; full-scale is
        # ±2^31. Use S32_MAX as 0 dBFS reference.
        import numpy as np  # lazy: avoid module-load cost outside Pi
        # `indata` is a bytes-like buffer from sd.RawInputStream
        # (CFFI cdata exposing the underlying ringbuffer). numpy
        # frombuffer creates a view, no copy.
        arr = np.frombuffer(indata, dtype=np.int32)
        # arr is interleaved L,R,L,R,... — RMS over both channels is
        # fine for "is the user playing audio" detection.
        if arr.size:
            # Use float64 sum to avoid intermediate int overflow on
            # large blocks; division by 2^62 maps S32^2 → unit interval.
            sq = arr.astype(np.float64) ** 2
            ms = float(sq.mean()) / (2.0 ** 62)
            self._last_rms_dbfs = (
                10.0 * math.log10(ms) if ms > 0.0 else float("-inf")
            )

        # S32 → S16 by truncating the low 16 bits. Equivalent to
        # right-shift 16 with sign extension. Acceptable quality loss
        # for the speaker-bound path (dongle is 24-bit anyway and
        # CamillaDSP captures S32 via plug wrapping).
        s16 = (arr >> 16).astype(np.int16)
        self.stats.frames_captured += frames

        try:
            self._queue.put_nowait(bytes(s16))
        except queue.Full:
            # Output side is too slow or stalled. Drop this block — the
            # alternative (blocking the PortAudio thread) would
            # propagate stall pressure into the gadget side and cause
            # the host to see XRUNs. Counted for the diagnostic thread.
            self.stats.frames_dropped_full += frames

    def _playback_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            self.stats.playback_errors += 1

        if self._preempted:
            # Silence the output buffer regardless of queue contents.
            # We DO still drain the queue (so backlogged frames don't
            # cause an ever-growing latency on un-preempt). Draining is
            # cheap (just a get_nowait that we throw away).
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            # outdata is bytes-writable; zero it byte-by-byte via
            # memoryview. frames * channels * 2 bytes.
            mv = memoryview(outdata).cast("b")
            for i in range(len(mv)):
                mv[i] = 0
            return

        try:
            block = self._queue.get_nowait()
        except queue.Empty:
            # Capture hasn't produced yet (boot startup) or host isn't
            # streaming. Output silence; counted as underrun.
            self.stats.frames_underrun += frames
            mv = memoryview(outdata).cast("b")
            for i in range(len(mv)):
                mv[i] = 0
            return

        # Both ends are sized BLOCK_FRAMES; block should match
        # `frames * channels * 2` bytes. If a previous host-side rate
        # negotiation produced a partial block, copy what fits.
        expected_bytes = frames * self._channels * 2
        out_mv = memoryview(outdata).cast("b")
        if len(block) == expected_bytes:
            out_mv[:expected_bytes] = block
        else:
            # Defensive: truncate to whichever is shorter, zero the rest.
            n = min(len(block), expected_bytes)
            out_mv[:n] = block[:n]
            for i in range(n, expected_bytes):
                out_mv[i] = 0
        self.stats.frames_played += frames
