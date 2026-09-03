# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Live RMS/peak meter over a wired measurement mic -- the level ramp's feed.

:mod:`jasper.audio_measurement.wired_capture` answers what the mic captured
during an excitation; this answers what it is hearing right now, which is what
a closed-loop level ramp needs. Same device, same PCM seam, same loud-failure
contract -- the ALSA open, the S32_LE format facts, and the bounded-wait
constants all come from that module. Output is
:class:`jasper.audio_measurement.ramp.LevelSample` batches.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable

from jasper.audio_measurement.wired_capture import (
    BYTES_PER_SAMPLE,
    MAX_CONSECUTIVE_READ_FAILURES,
    START_TIMEOUT_S,
    CapturePcm,
    WiredCaptureError,
    open_alsa_capture_pcm,
)

__all__ = ["WiredLevelMeter"]

#: 24-bit-in-32 full scale: a USB measurement mic delivers 24-bit samples
#: left-justified in an S32_LE container, so the rail is ``(2**23-1) << 8``.
_FULL_SCALE_COUNTS = float((2**23 - 1) << 8)

#: Floor for a silent chunk, matching the capture page's ``rmsToDbfs``.
_SILENT_DBFS = -120.0

#: How close to the rail counts as clipping: one 24-bit LSB in the S32
#: container, so a genuinely full-scale 24-bit sample is caught exactly.
_CLIP_GUARD_COUNTS = 256.0


def _counts_to_dbfs(counts: float) -> float:
    """S32 sample magnitude → dBFS, floored at the silent level."""
    if counts <= 0.0:
        return _SILENT_DBFS
    return max(_SILENT_DBFS, 20.0 * math.log10(counts / _FULL_SCALE_COUNTS))


class WiredLevelMeter:
    """Live RMS/peak meter over a wired measurement mic -- the ramp's mic feed.

    ``agc_frozen=True`` is a statement of fact, not a default: an ALSA capture
    has no browser gain control in the path, so the kernel's empirical AGC-slope
    machinery stays off -- which is precisely why a wired ramp needs
    :func:`jasper.active_speaker.seat_level_ramp.mic_is_not_observing` for its
    "is the mic responding at all" evidence.

    Level per chunk is the MAX across channels: a UMIK enumerating as stereo
    carries its capsule on one channel and near-silence on the other, and taking
    the max can only over-report, never hide a loud room.

    ``start()`` blocks until real audio arrives (so a caller never ramps into a
    dead mic), ``drain()`` takes everything measured since the last call,
    ``stop()`` is idempotent. ``pcm_factory`` is the test seam.
    """

    def __init__(
        self,
        device: str,
        *,
        sample_rate_hz: int = 48000,
        channels: int = 1,
        period_frames: int = 2048,
        pcm_factory: Callable[[], CapturePcm] | None = None,
    ) -> None:
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if channels <= 0:
            raise ValueError("channels must be positive")
        self._device = device
        self._sample_rate_hz = int(sample_rate_hz)
        self._channels = int(channels)
        self._pcm_factory = pcm_factory or (
            lambda: open_alsa_capture_pcm(
                device,
                sample_rate_hz=self._sample_rate_hz,
                channels=self._channels,
                period_frames=period_frames,
            )
        )
        self._stop_event = threading.Event()
        self._first_chunk = threading.Event()
        self._thread: threading.Thread | None = None
        self._pcm: CapturePcm | None = None
        self._lock = threading.Lock()
        self._pending: list[Any] = []
        self._seq = 0
        self._reader_error: WiredCaptureError | None = None
        #: Only a reader death BEFORE the mic ever proves itself alive fails
        #: start(). Set by the reader thread strictly before _first_chunk.set(),
        #: never after, so start()'s read of it is race-free without self._lock;
        #: a later death still reaches drain() via _reader_error.
        self._startup_error: WiredCaptureError | None = None

    def _measure(self, data: bytes, length: int) -> tuple[float, float, bool]:
        import numpy as np

        frame_bytes = self._channels * BYTES_PER_SAMPLE
        frames = np.frombuffer(
            data[: length * frame_bytes], dtype="<i4"
        ).reshape(-1, self._channels)
        if not frames.size:
            return _SILENT_DBFS, _SILENT_DBFS, False
        values = frames.astype(np.float64)
        peak_counts = float(np.abs(values).max())
        # Per-channel RMS, then the loudest channel (see the class docstring).
        rms_counts = float(np.sqrt(np.mean(np.square(values), axis=0)).max())
        clipping = peak_counts >= _FULL_SCALE_COUNTS - _CLIP_GUARD_COUNTS
        return _counts_to_dbfs(rms_counts), _counts_to_dbfs(peak_counts), clipping

    def _run_reader(self) -> None:
        from jasper.audio_measurement.ramp import LevelSample

        assert self._pcm is not None
        consecutive_failures = 0
        try:
            while not self._stop_event.is_set():
                try:
                    length, data = self._pcm.read()
                except (OSError, RuntimeError) as exc:
                    raise WiredCaptureError(
                        f"wired level meter read failed on {self._device}: {exc}"
                    ) from exc
                if length <= 0:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_READ_FAILURES:
                        raise WiredCaptureError(
                            f"wired level meter on {self._device} failed "
                            f"{consecutive_failures} consecutive reads — "
                            "treating the microphone as gone"
                        )
                    continue
                consecutive_failures = 0
                try:
                    rms_dbfs, peak_dbfs, clip = self._measure(data, length)
                except ValueError as exc:
                    # A short read that is not a whole number of frames: the
                    # reshape cannot represent it, and a meter that silently
                    # skipped such chunks would go quiet without saying so.
                    raise WiredCaptureError(
                        f"wired level meter got a partial frame from "
                        f"{self._device} ({length} frames): {exc}"
                    ) from exc
                with self._lock:
                    self._seq += 1
                    self._pending.append(
                        LevelSample(
                            seq=self._seq,
                            t_client_ms=int(time.monotonic() * 1000.0),
                            rms_dbfs=rms_dbfs,
                            peak_dbfs=peak_dbfs,
                            clip=clip,
                            agc_frozen=True,
                        )
                    )
                self._first_chunk.set()
        except WiredCaptureError as exc:
            self._reader_error = exc
            if not self._first_chunk.is_set():
                self._startup_error = exc
            self._first_chunk.set()

    def start(self, *, ready_timeout_s: float = START_TIMEOUT_S) -> None:
        """Open the device and block until real audio has been measured."""
        if self._thread is not None:
            raise WiredCaptureError("level meter already started")
        self._pcm = self._pcm_factory()
        self._thread = threading.Thread(
            target=self._run_reader, name="wired-level-meter", daemon=True
        )
        self._thread.start()
        if not self._first_chunk.wait(ready_timeout_s):
            self.stop()
            raise WiredCaptureError(
                f"no audio arrived from {self._device} within "
                f"{ready_timeout_s:g}s — the measurement microphone is not "
                "delivering samples"
            )
        if self._startup_error is not None:
            self.stop()
            raise self._startup_error

    def drain(self) -> list[Any]:
        """Take every sample measured since the last call.

        Raises the reader's :class:`WiredCaptureError` if the mic died, so a
        ramp aborts loudly instead of coasting on an empty feed.
        """
        if self._reader_error is not None:
            raise self._reader_error
        with self._lock:
            batch, self._pending = self._pending, []
        return batch

    def stop(self) -> None:
        """Stop the reader and close the device — idempotent, never raises."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=START_TIMEOUT_S)
        pcm, self._pcm = self._pcm, None
        if pcm is not None:
            try:
                pcm.close()
            except (OSError, RuntimeError):
                pass


