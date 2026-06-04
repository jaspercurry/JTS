"""Audio bridge: UAC2Gadget capture → fan-in renderer lane.

Two sounddevice streams connected by a bounded queue:

    sd.InputStream(UAC2Gadget, 48k S32 stereo)
        │  capture callback: convert S32→S16, compute RMS, enqueue
        ▼
    queue.Queue(maxsize=N)
        │
        ▼
    sd.OutputStream(usbsink_substream, 48k S16 stereo)
        │  playback callback: dequeue (or silence on underrun), gate
        │  on `preempted` (write zeros instead of audio)

Two separate streams (not one duplex `sd.Stream`) because PortAudio's
duplex mode requires both ends use the same underlying ALSA device
context; UAC2Gadget and the fan-in lane are different cards. Two
streams share a Python queue between their PortAudio threads; both
threads run lock-free except for the queue.

The playback target is `pcm.usbsink_substream`, the USB-in private
snd-aloop lane. jasper-fanin reads the capture side, sums it with the
other renderer lanes, and writes one music stream to CamillaDSP/AEC.

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

import numpy as np

logger = logging.getLogger(__name__)

# Stream parameters. Both ends are stereo @ 48 kHz to match the
# fan-in lane and the dongle dmix. Different sample sizes:
#   - capture: S32_LE (the UAC2 gadget descriptor's c_ssize=4)
#   - playback: S16_LE (the fixed format of the fan-in lanes;
#     the `plug:` front-end `pcm.usbsink_substream` would resample
#     anything else, but we already match so plug becomes a no-op)
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
    # Most recent non-zero PortAudio CallbackFlags value, one per
    # stream. Stashed (not logged) on the audio thread; the daemon's
    # diagnostic loop logs deltas at WARN cadence. We keep the most
    # recent rather than a bitwise union so the operator sees the
    # exact last-fault flags. Reset to 0 after the diag loop reports.
    last_capture_status: int = 0
    last_playback_status: int = 0
    started_at_mono: float = field(default_factory=time.monotonic)


class AudioBridge:
    """Captures the host's audio from the UAC2Gadget ALSA card and
    replays it into `pcm.usbsink_substream`, where jasper-fanin picks
    it up for the rest of the music chain.

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
        # PortAudio device-string for the gadget capture endpoint.
        # Sounddevice substring-matches against `sd.query_devices()`,
        # which formats the gadget as "UAC2_Gadget: PCM (hw:N,0)" —
        # note the UNDERSCORE. The kernel "short" name (used by
        # amixer / /proc/asound/<name>) is "UAC2Gadget" without the
        # underscore. See DaemonConfig docstring for the full
        # dual-naming explanation.
        capture_device: str = "UAC2_Gadget",
        playback_device: str = "usbsink_substream",
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

        # Pre-allocated scratch buffer for in-place RMS computation in
        # the capture callback. The callback runs on PortAudio's
        # realtime audio thread at ~100 Hz — any per-call malloc here
        # risks contending with the rest of the Pi for memory and
        # producing audible dropouts (especially on a 1 GB Pi under
        # load). Squaring is done into this buffer via np.square(out=)
        # with zero allocation. Sized to match one full block; if the
        # callback ever sees a smaller block (unusual but legal) we
        # only use the prefix.
        self._rms_scratch = np.zeros(
            channels * block_frames, dtype=np.float64,
        )
        # Pre-allocated silence block for the playback callback's
        # preempt / underrun paths. Without this, those paths fell back
        # to a Python-level zero-fill loop
        # (`for i in range(len(mv)): mv[i] = 0`) — ~1920 byte
        # assignments at 100 Hz on the realtime audio thread, ~38-77%
        # of the 10 ms callback budget on a Pi 5. With a pre-allocated
        # bytes object, the same path becomes one memoryview slice
        # assignment (~1 µs). Sized for one full block at S16 stereo.
        self._silence_block = bytes(block_frames * channels * 2)

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
            # Validate that PortAudio actually opened both streams at
            # the requested rate. UAC2 sample-rate negotiation is
            # legally allowed to honor a different rate than requested
            # (rare but possible on some host configurations); a
            # mismatch would produce buffer-size confusion in the
            # callbacks (the defensive truncation in _playback_callback
            # absorbs it but the user hears clicks/pops). Fail-fast
            # here is better than silent audio glitches — systemd's
            # Restart=on-failure will try again, and the doctor will
            # surface the issue on the next pass.
            for label, stream in (
                ("capture", self._in_stream),
                ("playback", self._out_stream),
            ):
                actual = float(stream.samplerate)
                if abs(actual - float(self._sample_rate)) > 0.5:
                    raise RuntimeError(
                        f"usbsink: {label} stream opened at "
                        f"{actual:.0f} Hz; expected {self._sample_rate} Hz "
                        f"(host negotiated a mismatched rate — likely a "
                        f"gadget descriptor / host-driver issue)"
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
            # not logged per-frame — too chatty AND the audio thread
            # MUST NOT call logger (a wedged logger.handler would freeze
            # the realtime thread). Stash the most-recent CallbackFlags
            # integer for the diagnostic loop to surface at WARN.
            self.stats.capture_errors += 1
            try:
                self.stats.last_capture_status = int(status)
            except (TypeError, ValueError):
                # status is normally a CallbackFlags enum (int-coerces)
                # but be defensive; we'd rather lose the flag than
                # raise on the audio thread.
                pass

        # `indata` is a bytes-like buffer from sd.RawInputStream
        # (CFFI cdata exposing the underlying ringbuffer). numpy
        # frombuffer creates a view, no copy.
        arr = np.frombuffer(indata, dtype=np.int32)

        # RMS computation, allocation-free. Each S32 sample is in
        # ±2^31; sum-of-squares is bounded by N · 2^62. We copy the
        # int32 view into the pre-allocated float64 scratch (sized
        # for the expected block), square in place, and take the
        # mean. Division by 2^62 maps S32² → unit interval.
        # arr is interleaved L,R,L,R,... — RMS over both channels
        # is fine for "is the user playing audio" detection.
        n = arr.size
        if n and n <= self._rms_scratch.size:
            scratch = self._rms_scratch[:n]
            np.copyto(scratch, arr)
            np.square(scratch, out=scratch)
            ms = float(scratch.mean()) / (2.0 ** 62)
            self._last_rms_dbfs = (
                10.0 * math.log10(ms) if ms > 0.0 else float("-inf")
            )

        # S32 → S16 by taking the high 16 bits. On little-endian
        # (Pi 5), reinterpreting an int32 array as int16 and taking
        # every odd-indexed sample selects the high half of each
        # int32 — equivalent to `arr >> 16` but a stride view rather
        # than an allocation. Acceptable quality loss for the
        # speaker-bound path (dongle is 24-bit anyway and CamillaDSP
        # captures S32 via plug wrapping).
        s16_view = arr.view(np.int16)[1::2]
        self.stats.frames_captured += frames

        try:
            # bytes() copies the view into a fresh bytes object for
            # the queue — this is the only unavoidable per-callback
            # allocation, since the underlying PortAudio buffer is
            # reused before the playback callback consumes it.
            self._queue.put_nowait(bytes(s16_view))
        except queue.Full:
            # Output side is too slow or stalled. Drop this block — the
            # alternative (blocking the PortAudio thread) would
            # propagate stall pressure into the gadget side and cause
            # the host to see XRUNs. Counted for the diagnostic thread.
            self.stats.frames_dropped_full += frames

    def _playback_callback(self, outdata, frames, time_info, status) -> None:
        if status:
            self.stats.playback_errors += 1
            try:
                self.stats.last_playback_status = int(status)
            except (TypeError, ValueError):
                pass

        # Cast both lvalue (outdata, CFFI buffer from sounddevice) and
        # rvalues (bytes objects with format "B") to format "b" so
        # memoryview slice-assignments succeed. Without matching
        # formats, the lvalue/rvalue mismatch raises
        # `ValueError: memoryview assignment: lvalue and rvalue have
        # different structures` — a latent bug that lived in the
        # original code until the PR3 callback tests caught it.
        expected_bytes = frames * self._channels * 2
        out_mv = memoryview(outdata).cast("b")
        silence_mv = memoryview(self._silence_block).cast("b")

        if self._preempted:
            # Silence the output buffer regardless of queue contents.
            # We DO still drain the queue (so backlogged frames don't
            # cause an ever-growing latency on un-preempt). Draining is
            # cheap (just a get_nowait that we throw away).
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            # Pre-allocated silence block — one slice-assignment
            # instead of a Python-level zero-fill loop. The loop
            # version cost ~38-77% of the 10 ms callback budget; the
            # slice copy is microseconds.
            out_mv[:expected_bytes] = silence_mv[:expected_bytes]
            return

        try:
            block = self._queue.get_nowait()
        except queue.Empty:
            # Capture hasn't produced yet (boot startup) or host isn't
            # streaming. Output silence; counted as underrun.
            self.stats.frames_underrun += frames
            out_mv[:expected_bytes] = silence_mv[:expected_bytes]
            return

        # Both ends are sized BLOCK_FRAMES; block should match
        # `frames * channels * 2` bytes. If a previous host-side rate
        # negotiation produced a partial block, copy what fits.
        block_mv = memoryview(block).cast("b")
        if len(block) == expected_bytes:
            out_mv[:expected_bytes] = block_mv
        else:
            # Defensive: truncate to whichever is shorter, zero the
            # rest from the pre-allocated silence block.
            n = min(len(block), expected_bytes)
            out_mv[:n] = block_mv[:n]
            out_mv[n:expected_bytes] = silence_mv[:expected_bytes - n]
        self.stats.frames_played += frames
