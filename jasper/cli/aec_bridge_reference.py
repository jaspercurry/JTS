# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The AEC bridge's far-end reference transport.

outputd's final speaker monitor arrives here as 48 kHz stereo UDP and leaves
as the 16 kHz mono frames AEC3 subtracts from the mic. Conversion, the queue
publish, and the clip accounting the RMS window reports all sit behind this
one surface.

Imports run one way only: nothing here reads `jasper.cli.aec_bridge`. The
process-wide `_BridgeStats` this transport counts into arrives as an argument,
the way the telemetry emitters take theirs, and the shutdown signal and
endpoint arrive from the caller that owns them.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from queue import Full, Queue
import socket
import threading
import time

import numpy as np

from jasper.dsp_numpy import butter2_highpass_sos, resample_poly, sosfilt
from jasper.cli.aec_bridge_engines import FRAME_SAMPLES, SAMPLE_RATE
from jasper.cli.aec_bridge_telemetry import DropLogDebouncer, _BridgeStats

# The bridge's own logger name: these lines reach journald beside the rest of
# jasper-aec-bridge's output, where log tooling already looks for them.
logger = logging.getLogger("jasper.aec_bridge")

# Wire geometry of the far-end reference. outputd sends its final speaker
# monitor at this rate/channel count; `ReferenceFrameConverter` folds it to
# the 16 kHz mono frames AEC3 consumes. The bridge has no other reference
# transport.
REF_RATE = 48000
REF_CHANNELS = 2

# Clipping counters for the ref pre-clip stage (after JASPER_AEC_REF_GAIN_DB),
# module-level for cheap cross-thread access: a race between increment and
# reset costs at most one frame in one log window's percentage.
_ref_clipped_samples = 0
_ref_total_samples = 0


def ref_clip_percent() -> float:
    """Percent of reference samples clipped since the last reset."""
    return (
        100.0 * _ref_clipped_samples / _ref_total_samples
        if _ref_total_samples else 0.0
    )


def reset_ref_clip_counters() -> None:
    global _ref_clipped_samples, _ref_total_samples

    _ref_clipped_samples = _ref_total_samples = 0


@dataclass(frozen=True)
class ReferenceFrameBatch:
    frames: tuple[bytes, ...]
    clipped_samples: int
    total_samples: int


class ReferenceFrameConverter:
    """Stateful 48 kHz stereo -> 16 kHz mono reference conversion.

    Transport adapters own capture and lifecycle. This class owns only the
    shared DSP contract: stereo folding, exact-size accumulation, resampling,
    stateful HPF, gain, clipping telemetry, and int16 framing.

    L+R are summed, not left-only: the speakers radiate the sum into a single
    mic and AEC3 is mono-reference, so an L-only reference would be blind to
    whatever is panned right. (The XMOS chip's USB-IN AEC requires left-only
    per datasheet §3.3, but that is the chip's own reference, not this one.)

    Accumulate rather than convert per delivery: a transport can hand over
    partial or oversized buffers, so frames accumulate at 48 kHz and only
    complete `capture_block`-sized chunks are emitted. WebRTC AEC3 strictly
    enforces equal mic and reference lengths.

    `JASPER_AEC_REF_GAIN_DB` boosts the digital reference before the engine
    sees it. AEC3 is tuned for conferencing, where ref RMS ≈ mic RMS or
    louder; here the digital reference is typically 25-30 dB *quieter* than
    what the mic captures, because amp + speakers + room amplify the acoustic
    path. The boost puts the adaptive filter back near its design point.
    """

    def __init__(self, *, ref_gain_db: float, ref_hpf_hz: float) -> None:
        self.ref_gain_db = float(ref_gain_db)
        self.ref_hpf_hz = float(ref_hpf_hz)
        self.capture_block = FRAME_SAMPLES * (REF_RATE // SAMPLE_RATE)
        self._ref_gain_lin = 10.0 ** (self.ref_gain_db / 20.0)
        self._hpf_sos = butter2_highpass_sos(self.ref_hpf_hz, SAMPLE_RATE)
        self._hpf_zi = np.zeros((self._hpf_sos.shape[0], 2), dtype=np.float64)
        self._accum_48 = np.empty(0, dtype=np.float32)

    @classmethod
    def from_env(cls) -> ReferenceFrameConverter:
        return cls(
            ref_gain_db=float(os.environ.get("JASPER_AEC_REF_GAIN_DB", "0")),
            ref_hpf_hz=float(os.environ.get("JASPER_AEC_REF_HPF_HZ", "125")),
        )

    def feed(self, interleaved: np.ndarray) -> ReferenceFrameBatch:
        arr = np.asarray(interleaved, dtype=np.int16).reshape(-1)
        usable = arr.size - (arr.size % REF_CHANNELS)
        if usable < REF_CHANNELS:
            return ReferenceFrameBatch((), 0, 0)
        arr = arr[:usable]

        left48 = arr[0::REF_CHANNELS].astype(np.float32)
        right48 = arr[1::REF_CHANNELS].astype(np.float32)
        mono48 = (left48 + right48) * 0.5
        self._accum_48 = np.concatenate((self._accum_48, mono48))

        frames: list[bytes] = []
        clipped_samples = 0
        total_samples = 0
        while self._accum_48.size >= self.capture_block:
            chunk = self._accum_48[:self.capture_block]
            self._accum_48 = self._accum_48[self.capture_block:]
            mono16 = resample_poly(chunk, up=1, down=3)
            mono16, self._hpf_zi = sosfilt(
                self._hpf_sos,
                mono16,
                zi=self._hpf_zi,
            )
            if self._ref_gain_lin != 1.0:
                mono16 = mono16 * self._ref_gain_lin
            clipped_samples += int(np.sum(np.abs(mono16) > 32767))
            total_samples += int(mono16.size)
            frames.append(
                np.clip(mono16, -32768, 32767).astype(np.int16).tobytes()
            )
        return ReferenceFrameBatch(
            frames=tuple(frames),
            clipped_samples=clipped_samples,
            total_samples=total_samples,
        )


def enqueue_reference_frames(
    ref_q: Queue[bytes],
    batch: ReferenceFrameBatch,
    *,
    stats: _BridgeStats,
    drop_log: DropLogDebouncer,
    drop_message: str,
) -> None:
    """Publish converted frames without letting reference capture block."""
    global _ref_clipped_samples, _ref_total_samples

    _ref_clipped_samples += batch.clipped_samples
    _ref_total_samples += batch.total_samples
    dropped = 0
    enqueued = 0
    for frame in batch.frames:
        try:
            ref_q.put_nowait(frame)
            enqueued += 1
        except Full:
            dropped += 1
    stats.record_reference_frames(enqueued)
    if dropped:
        stats.inc_nested("queue_drops", "ref", dropped)

    now = time.monotonic()
    report = (
        drop_log.record_many(now, dropped)
        if dropped
        else drop_log.flush(now)
    )
    if report is not None:
        logger.warning(drop_message, *report)


def outputd_ref_udp_thread(
    ref_q: Queue[bytes],
    *,
    host: str,
    port: int,
    stats: _BridgeStats,
    shutdown: threading.Event,
) -> None:
    """Receive outputd's final speaker-reference UDP tap and convert it
    to the 16 kHz mono frames AEC3 consumes.

    The bridge's only reference transport, and not a clocked ALSA capture
    loop: outputd sends the exact post-mix buffer it writes to the DAC.
    Software AEC, chip-AEC, corpus, and diagnostics all read it, so they all
    see the same final speaker reference.
    """
    converter = ReferenceFrameConverter.from_env()
    drop_log = DropLogDebouncer()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sock.settimeout(0.5)
    logger.info(
        "outputd ref UDP opened: %s:%d @ %d Hz stereo -> %d Hz mono "
        "(pre-AEC gain=%+.1f dB, HPF=%.0f Hz 2nd Butter)",
        host, port,
        REF_RATE,
        SAMPLE_RATE,
        converter.ref_gain_db,
        converter.ref_hpf_hz,
    )
    try:
        while not shutdown.is_set():
            try:
                data, _addr = sock.recvfrom(65536)
            except socket.timeout:
                continue
            if not data:
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            enqueue_reference_frames(
                ref_q,
                converter.feed(arr),
                stats=stats,
                drop_log=drop_log,
                drop_message=(
                    "outputd ref queue full, dropped %d frames in last %.1fs"
                ),
            )
    finally:
        sock.close()
