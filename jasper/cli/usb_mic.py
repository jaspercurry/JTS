# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded clean-mic relay for the UAC2 Pi-to-host direction."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import threading
import time
from types import ModuleType
from typing import Any, Callable, Iterable

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event
from jasper.percentiles import nearest_rank_percentile
from jasper.usb_mic import (
    GADGET_PATH,
    INTENT_PATH,
    RELAY_STATUS_PATH,
    USB_HOST_MIC_UDP_PORT,
    USB_MIC_HEADER_BYTES,
    USB_MIC_HEADER_STRUCT,
    USB_MIC_RELAY_SCHEMA_VERSION,
    USB_MIC_SOURCE_AGE_BASIS,
    USB_MIC_SOURCE_AGE_SCOPE,
    USB_MIC_PACKET_MAGIC,
    USB_MIC_PACKET_VERSION,
    usb_mic_enabled,
)

logger = logging.getLogger("jasper.usb_mic")

SOURCE_RATE = 16_000
CHANNELS = 1
SAMPLE_BYTES = 2
PERIOD_FRAMES = 320
PERIOD_BYTES = PERIOD_FRAMES * SAMPLE_BYTES
# The bridge's dedicated USB-mic leg emits one native AEC frame (20 ms).
# This new relay accepts the old raw 20 ms and 80 ms shapes during a coupled
# upgrade, but an old relay cannot decode v2. All accepted shapes split into
# the same bounded 20 ms sink periods.
PACKET_BYTES = PERIOD_BYTES
LEGACY_PACKET_BYTES = 1280 * SAMPLE_BYTES
V2_PACKET_BYTES = USB_MIC_HEADER_BYTES + PACKET_BYTES
ACCEPTED_PACKET_BYTES = frozenset((PACKET_BYTES, LEGACY_PACKET_BYTES, V2_PACKET_BYTES))
QUEUE_PERIODS = 2
SOURCE_AGE_WINDOW_PERIODS = 512
SOURCE_AGE_BASIS = USB_MIC_SOURCE_AGE_BASIS
SOURCE_AGE_SCOPE = USB_MIC_SOURCE_AGE_SCOPE
RELAY_SCHEMA_VERSION = USB_MIC_RELAY_SCHEMA_VERSION
DROP_REGIME_BASIS = "status_interval_host_hw_ptr_advance"
# Local UDP can only queue hundreds of these 656-byte packets. Treat a jump
# larger than this generous bound as a sender discontinuity rather than
# publishing a catastrophic false loss count after a restart/reset.
MAX_PLAUSIBLE_SEQUENCE_GAP = 4096
UAC2_DEVICE = "plughw:CARD=UAC2Gadget,DEV=0"
# The bridge emits 20 ms frames, while the verified gadget PCM requires exact
# 10 ms writes. Keep these domains distinct: every QueuedFrame becomes two
# writer periods and pyalsaaudio realizes four periods / 40 ms of capacity.
ALSA_PERIOD_FRAMES = 160
ALSA_PERIOD_BYTES = ALSA_PERIOD_FRAMES * CHANNELS * SAMPLE_BYTES
ALSA_PERIODS = 4
ALSA_BUFFER_FRAMES = ALSA_PERIOD_FRAMES * ALSA_PERIODS
ALSA_PERIOD_MS = 10.0
GADGET_RATE = 48_000
GADGET_BUFFER_MS = 40.0
# A 20-40 ms band is the lowest hardware-reliable posture observed on the Pi.
# The original 10-30 ms band produced an ordinary-load xrun after 15 minutes;
# retaining one extra 10 ms period still keeps measured p95 well below 80 ms.
WRITER_TARGET_MS = 30.0
WRITER_LOW_MS = 20.0
WRITER_HIGH_MS = 40.0
WRITER_POLL_SECONDS = 0.005
SOURCE_PERIOD_WAIT_SECONDS = 0.005
IDLE_SANITIZE_SECONDS = 0.2
INVALID_FILL_GRACE_SECONDS = 2.0
MAX_XRUNS_PER_WINDOW = 5
XRUN_WINDOW_SECONDS = 10.0
AUDIO_PROGRESS_FRESH_SECONDS = 2.0
DROP_STREAK_WARN_INTERVALS = 2
HOST_PCM_STATUS_PATH = Path("/proc/asound/UAC2Gadget/pcm0p/sub0/status")


class RelayError(RuntimeError):
    """An expected relay failure that systemd should restart."""


@dataclass(frozen=True)
class QueuedFrame:
    """One native AEC frame plus bridge-emit metadata."""

    t_bridge_emit_ns: int
    seq: int | None
    pcm: bytes


@dataclass(frozen=True)
class SourceAgeSnapshot:
    """One bounded view of emit-to-final-ALSA-period ages."""

    samples_ms: tuple[float, ...]
    generation: int
    started_epoch_sec: float
    samples_appended: int = 0


@dataclass(frozen=True)
class WriterPeriod:
    """One exact pyalsaaudio period derived from a 20 ms bridge frame."""

    t_bridge_emit_ns: int
    seq: int | None
    pcm: bytes
    record_source_age: bool


@dataclass(frozen=True)
class WriterSnapshot:
    """Bounded observability for the in-process gadget writer."""

    fill_ms: float | None
    splices: int
    xruns: int
    resets: int
    idle_sanitizations: int
    silence_periods: int
    discarded_periods: int
    target_ms: float
    pcm_period_ms: float
    pcm_buffer_ms: float


@dataclass
class SequenceTracker:
    """Count plausible forward loss without lying on reorder or reset."""

    last_seq: int | None = None
    resets: int = 0
    reorders: int = 0
    discontinuities: int = 0

    def clear_baseline(self) -> None:
        self.last_seq = None

    def observe(self, seq: int) -> int:
        if self.last_seq is None:
            self.last_seq = seq
            return 0
        expected = (self.last_seq + 1) & 0xFFFFFFFF
        if seq == expected:
            self.last_seq = seq
            return 0
        if seq == self.last_seq:
            return 0
        # TimestampedLegEmitter always restarts at zero. Handle that explicit
        # reset before modular half-range math can mistake a high-half reset
        # for billions of forward losses.
        if seq == 0:
            self.last_seq = 0
            self.resets += 1
            return 0
        delta = (seq - self.last_seq) & 0xFFFFFFFF
        if delta >= 0x80000000:
            # A small/old packet arrived behind the high-water mark. Do not
            # move the baseline backward or the next in-order packet will look
            # lost even though it was already observed.
            self.reorders += 1
            return 0
        gap = delta - 1
        self.last_seq = seq
        if gap <= MAX_PLAUSIBLE_SEQUENCE_GAP:
            return gap
        self.discontinuities += 1
        return 0


class LatestAudioQueue:
    """Two-period drop-oldest queue: bounded memory and stale latency."""

    def __init__(self, max_periods: int = QUEUE_PERIODS) -> None:
        self._items: deque[QueuedFrame] = deque()
        self._max_periods = max_periods
        self._condition = threading.Condition()
        self._closed = False
        self.dropped = 0

    def put(self, frame: QueuedFrame) -> None:
        with self._condition:
            while len(self._items) >= self._max_periods:
                self._items.popleft()
                self.dropped += 1
            self._items.append(frame)
            self._condition.notify()

    def get(self, timeout: float) -> QueuedFrame | None:
        with self._condition:
            if not self._items and not self._closed:
                self._condition.wait(timeout)
            if self._items:
                return self._items.popleft()
            return None

    def take_latest(self) -> QueuedFrame | None:
        """Return only the freshest frame and count older queued history."""

        with self._condition:
            if not self._items:
                return None
            latest = self._items.pop()
            self.dropped += len(self._items)
            self._items.clear()
            return latest

    def discard_all(self) -> int:
        """Discard queued history and return the number of source frames."""

        with self._condition:
            discarded = len(self._items)
            self._items.clear()
            self.dropped += discarded
            return discarded

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._items.clear()
            self._condition.notify_all()


def _load_alsaaudio() -> ModuleType:
    """Import the Linux-only PCM binding without breaking non-Linux tooling."""

    import alsaaudio  # type: ignore[import-not-found]

    return alsaaudio


def _split_writer_periods(frame: QueuedFrame) -> tuple[WriterPeriod, WriterPeriod]:
    """Split one bridge frame into the exact 10 ms writes ALSA negotiated."""

    if len(frame.pcm) != PERIOD_BYTES:
        raise RelayError(
            f"invalid queued frame bytes={len(frame.pcm)} expected={PERIOD_BYTES}"
        )
    first = frame.pcm[:ALSA_PERIOD_BYTES]
    second = frame.pcm[ALSA_PERIOD_BYTES:]
    return (
        WriterPeriod(
            frame.t_bridge_emit_ns,
            frame.seq,
            first,
            False,
        ),
        WriterPeriod(
            frame.t_bridge_emit_ns,
            frame.seq,
            second,
            frame.seq is not None,
        ),
    )


class AlsaGadgetSink:
    """Keep the gadget ring fresh and near a measured occupancy target.

    The host can leave the UAC2 playback PCM ``RUNNING`` without advancing its
    clock. Frozen room audio must therefore be replaced with silence while
    idle, before a later host capture can consume it. A resume resets the PCM
    again, primes one full start-threshold buffer, then lets fill settle to the
    20 ms operating target. The optional export never blocks the AEC bridge.
    """

    def __init__(
        self,
        *,
        alsaaudio_module: ModuleType | None = None,
        status_reader: Callable[[], HostPcmSnapshot] | None = None,
        start_thread: bool = True,
    ) -> None:
        self.queue = LatestAudioQueue()
        self.frames_written = 0
        self.last_progress_epoch_sec = 0.0
        self.last_progress_monotonic = 0.0
        self.error = ""
        self._progress_lock = threading.Lock()
        self._source_ages_ms: deque[float] = deque(maxlen=SOURCE_AGE_WINDOW_PERIODS)
        self._source_age_generation = 0
        self._source_age_started_epoch_sec = time.time()
        self._source_age_samples_appended = 0
        self._alsa = alsaaudio_module or _load_alsaaudio()
        alsa_error = getattr(self._alsa, "ALSAAudioError", None)
        self._alsa_error_types: tuple[type[BaseException], ...] = (
            (OSError, alsa_error)
            if isinstance(alsa_error, type) and issubclass(alsa_error, BaseException)
            else (OSError,)
        )
        self._status_reader = status_reader or _read_host_pcm_status
        self._pcm: Any | None = None
        self._pcm_buffer_ms = GADGET_BUFFER_MS
        self._geometry_logged = False
        self._stop = threading.Event()
        self._pending: deque[WriterPeriod] = deque()
        self._last_hw_ptr: int | None = None
        self._frozen_since_monotonic = time.monotonic()
        self._idle_sanitized = False
        self._settling = True
        self._fill_ms: float | None = None
        self._invalid_fill_since_monotonic = 0.0
        self._writer_splices = 0
        self._writer_xruns = 0
        self._writer_resets = 0
        self._idle_sanitizations = 0
        self._silence_periods = 0
        self._discarded_periods = 0
        self._xrun_times: deque[float] = deque()
        self._last_splice_log_monotonic = 0.0
        self._last_xrun_log_monotonic = 0.0
        self._reset_pcm(reason="startup", freshest=None)
        self._thread: threading.Thread | None = None
        if start_thread:
            self._thread = threading.Thread(
                target=self._write_loop,
                name="usbmic-alsa-writer",
                daemon=True,
            )
            self._thread.start()

    def _open_pcm(self) -> None:
        pcm: Any | None = None
        try:
            pcm = self._alsa.PCM(
                type=self._alsa.PCM_PLAYBACK,
                mode=self._alsa.PCM_NONBLOCK,
                device=UAC2_DEVICE,
                rate=SOURCE_RATE,
                channels=CHANNELS,
                format=self._alsa.PCM_FORMAT_S16_LE,
                periodsize=ALSA_PERIOD_FRAMES,
                periods=ALSA_PERIODS,
            )
            info = pcm.info()
            realized = {
                "rate": int(info.get("rate", 0)),
                "channels": int(info.get("channels", 0)),
                "period_size": int(info.get("period_size", 0)),
                "buffer_size": int(info.get("buffer_size", 0)),
            }
            expected = {
                "rate": SOURCE_RATE,
                "channels": CHANNELS,
                "period_size": ALSA_PERIOD_FRAMES,
                "buffer_size": ALSA_BUFFER_FRAMES,
            }
            if realized != expected:
                raise RelayError(
                    f"unexpected ALSA geometry realized={realized} expected={expected}"
                )
            self._pcm_buffer_ms = realized["buffer_size"] / SOURCE_RATE * 1000.0
            self._pcm = pcm
            if not self._geometry_logged:
                log_event(
                    logger,
                    "usb_mic.writer_opened",
                    device=UAC2_DEVICE,
                    rate=realized["rate"],
                    channels=realized["channels"],
                    period_frames=realized["period_size"],
                    buffer_frames=realized["buffer_size"],
                    target_ms=WRITER_TARGET_MS,
                )
                self._geometry_logged = True
        except RelayError:
            if pcm is not None:
                try:
                    pcm.close()
                except self._alsa_error_types:
                    pass
            raise
        except self._alsa_error_types + (TypeError, ValueError) as exc:
            if pcm is not None:
                try:
                    pcm.close()
                except self._alsa_error_types:
                    pass
            raise RelayError(
                f"ALSA PCM open failed: {type(exc).__name__}: {exc}"
            ) from exc

    def _close_pcm(self) -> None:
        pcm, self._pcm = self._pcm, None
        if pcm is None:
            return
        try:
            pcm.drop()
        except self._alsa_error_types:
            pass
        try:
            pcm.close()
        except self._alsa_error_types:
            pass

    @staticmethod
    def _silence_period() -> WriterPeriod:
        return WriterPeriod(0, None, bytes(ALSA_PERIOD_BYTES), False)

    def _write_exact(self, period: WriterPeriod) -> bool:
        """Write one whole period; return false only for nonblocking EAGAIN."""

        if self._pcm is None:
            raise RelayError("ALSA PCM is not open")
        if len(period.pcm) != ALSA_PERIOD_BYTES:
            raise RelayError(
                f"invalid ALSA period bytes={len(period.pcm)} "
                f"expected={ALSA_PERIOD_BYTES}"
            )
        try:
            written = int(self._pcm.write(period.pcm))
        except self._alsa_error_types as exc:
            raise RelayError(f"ALSA write failed: {type(exc).__name__}: {exc}") from exc
        if written == 0:
            return False
        if written != ALSA_PERIOD_FRAMES:
            raise RelayError(
                f"ALSA period write returned frames={written} "
                f"expected={ALSA_PERIOD_FRAMES}"
            )
        now_monotonic = time.monotonic()
        now_epoch = time.time()
        with self._progress_lock:
            if period.record_source_age and period.seq is not None:
                source_age_ms = max(
                    0.0,
                    (
                        time.clock_gettime_ns(time.CLOCK_MONOTONIC)
                        - period.t_bridge_emit_ns
                    )
                    / 1_000_000.0,
                )
                self._source_ages_ms.append(source_age_ms)
                self._source_age_samples_appended += 1
            self.frames_written += written
            self.last_progress_monotonic = now_monotonic
            self.last_progress_epoch_sec = now_epoch
        return True

    def _prime_pcm(self, freshest: QueuedFrame | None) -> None:
        periods: list[WriterPeriod] = [
            self._silence_period(),
            self._silence_period(),
        ]
        if freshest is None:
            periods.extend((self._silence_period(), self._silence_period()))
        else:
            periods.extend(_split_writer_periods(freshest))
        for period in periods:
            if not self._write_exact(period):
                raise RelayError("ALSA PCM filled before its four-period preload")
            if period.seq is None:
                self._silence_periods += 1
        self._fill_ms = self._pcm_buffer_ms
        self._settling = True

    def _reset_pcm(
        self,
        *,
        reason: str,
        freshest: QueuedFrame | None,
    ) -> None:
        self._close_pcm()
        self._open_pcm()
        self._prime_pcm(freshest)
        self._last_hw_ptr = None
        self._frozen_since_monotonic = time.monotonic()
        if reason == "resume":
            self._writer_resets += 1
            log_event(
                logger,
                "usb_mic.writer_reset",
                reason=reason,
                resets=self._writer_resets,
                primed_audio=int(freshest is not None),
            )
        elif reason == "idle_sanitize":
            self._idle_sanitizations += 1
            log_event(
                logger,
                "usb_mic.writer_idle_sanitize",
                sanitizations=self._idle_sanitizations,
            )

    def _discard_pending(self) -> int:
        discarded = len(self._pending)
        self._pending.clear()
        self._discarded_periods += discarded
        return discarded

    def _replace_pending_with_freshest(self) -> bool:
        """Replace held audio only when something newer has actually arrived."""

        dropped_before = self.queue.dropped
        freshest = self.queue.take_latest()
        if freshest is None:
            return False
        queue_frames_discarded = max(0, self.queue.dropped - dropped_before)
        pending_discarded = self._discard_pending()
        self._discarded_periods += queue_frames_discarded * 2
        self._pending.extend(_split_writer_periods(freshest))
        return bool(pending_discarded or queue_frames_discarded)

    def _record_splice(self, *, direction: str, fill_ms: float) -> None:
        self._writer_splices += 1
        now = time.monotonic()
        if (
            self._last_splice_log_monotonic == 0.0
            or now - self._last_splice_log_monotonic >= 60.0
        ):
            log_event(
                logger,
                "usb_mic.writer_splice",
                direction=direction,
                fill_ms=round(fill_ms, 1),
                splices=self._writer_splices,
                level=logging.WARNING,
            )
            self._last_splice_log_monotonic = now

    def _recover_xrun(self, *, detail: str, now_monotonic: float) -> None:
        self._writer_xruns += 1
        self._xrun_times.append(now_monotonic)
        while (
            self._xrun_times
            and now_monotonic - self._xrun_times[0] > XRUN_WINDOW_SECONDS
        ):
            self._xrun_times.popleft()
        if len(self._xrun_times) > MAX_XRUNS_PER_WINDOW:
            raise RelayError(
                f"repeated ALSA writer xruns count={len(self._xrun_times)} "
                f"window_s={XRUN_WINDOW_SECONDS}: {detail}"
            )
        if (
            self._last_xrun_log_monotonic == 0.0
            or now_monotonic - self._last_xrun_log_monotonic >= 60.0
        ):
            log_event(
                logger,
                "usb_mic.writer_xrun",
                detail=detail,
                xruns=self._writer_xruns,
                level=logging.WARNING,
            )
            self._last_xrun_log_monotonic = now_monotonic
        self._discard_pending()
        freshest = self.queue.take_latest()
        self._reset_pcm(reason="xrun", freshest=freshest)

    @staticmethod
    def _fill_from_snapshot(snapshot: HostPcmSnapshot) -> float | None:
        if snapshot.appl_ptr is None or snapshot.hw_ptr is None:
            return None
        fill_frames = snapshot.appl_ptr - snapshot.hw_ptr
        max_frames = int(GADGET_RATE * GADGET_BUFFER_MS / 1000.0)
        if fill_frames < 0 or fill_frames > max_frames:
            return None
        return fill_frames / GADGET_RATE * 1000.0

    def _load_pending_frame(self, *, timeout: float = 0.0) -> None:
        if self._pending:
            return
        frame = self.queue.get(timeout=timeout)
        if frame is not None:
            self._pending.extend(_split_writer_periods(frame))

    def _pcm_is_xrun(self) -> bool:
        if self._pcm is None:
            return False
        xrun_state = getattr(self._alsa, "PCM_STATE_XRUN", None)
        if xrun_state is None:
            return False
        try:
            return self._pcm.state() == xrun_state
        except self._alsa_error_types as exc:
            raise RelayError(f"ALSA state query failed: {exc}") from exc

    def _writer_step(self, *, now_monotonic: float) -> None:
        snapshot = self._status_reader()
        previous_hw_ptr = self._last_hw_ptr
        current_hw_ptr = snapshot.hw_ptr
        advanced = bool(
            snapshot.running
            and current_hw_ptr is not None
            and previous_hw_ptr is not None
            and current_hw_ptr > previous_hw_ptr
        )
        frozen_for = max(
            0.0,
            now_monotonic - self._frozen_since_monotonic,
        )
        self._last_hw_ptr = current_hw_ptr
        fill_ms = self._fill_from_snapshot(snapshot)
        self._fill_ms = fill_ms

        if fill_ms is None:
            if self._invalid_fill_since_monotonic <= 0.0:
                self._invalid_fill_since_monotonic = now_monotonic
            elif (
                now_monotonic - self._invalid_fill_since_monotonic
                >= INVALID_FILL_GRACE_SECONDS
            ):
                raise RelayError(
                    "gadget occupancy unavailable or outside negotiated buffer "
                    f"for {INVALID_FILL_GRACE_SECONDS:.1f}s"
                )
        else:
            self._invalid_fill_since_monotonic = 0.0

        if self._pcm_is_xrun():
            self._recover_xrun(
                detail="PCM state is XRUN",
                now_monotonic=now_monotonic,
            )
            return

        if not advanced:
            if self._frozen_since_monotonic <= 0.0:
                self._frozen_since_monotonic = now_monotonic
                frozen_for = 0.0
            if frozen_for >= IDLE_SANITIZE_SECONDS and not self._idle_sanitized:
                self._discard_pending()
                self.queue.discard_all()
                self.reset_source_age_window()
                self._reset_pcm(reason="idle_sanitize", freshest=None)
                self._idle_sanitized = True
            return

        if self._idle_sanitized and frozen_for >= IDLE_SANITIZE_SECONDS:
            self._discard_pending()
            freshest = self.queue.take_latest()
            self.reset_source_age_window()
            self._reset_pcm(reason="resume", freshest=freshest)
            self._idle_sanitized = False
            return

        self._frozen_since_monotonic = now_monotonic
        self._idle_sanitized = False
        if fill_ms is None:
            return
        if self._settling:
            if fill_ms > WRITER_TARGET_MS:
                return
            self._settling = False

        if fill_ms >= WRITER_HIGH_MS:
            if self._replace_pending_with_freshest():
                self._record_splice(direction="drop", fill_ms=fill_ms)
            return

        self._load_pending_frame()
        # Poll twice per ALSA period and allow two writes at the low watermark.
        # That spare authority catches up after ordinary scheduler jitter;
        # a one-write-per-10-ms relative timer can only fall behind.
        writes_allowed = 2 if fill_ms <= WRITER_LOW_MS else 1
        current_fill_ms = fill_ms
        insert_splice_recorded = False
        for _index in range(writes_allowed):
            if current_fill_ms + ALSA_PERIOD_MS > WRITER_HIGH_MS:
                return
            self._load_pending_frame(
                timeout=(
                    SOURCE_PERIOD_WAIT_SECONDS
                    if not self._pending and current_fill_ms <= WRITER_LOW_MS
                    else 0.0
                )
            )
            if not self._pending and writes_allowed == 1:
                return
            period = self._pending[0] if self._pending else self._silence_period()
            inserting_silence = not self._pending
            try:
                if not self._write_exact(period):
                    if self._pending:
                        self._pending.popleft()
                        self._discarded_periods += 1
                        self._record_splice(
                            direction="drop",
                            fill_ms=current_fill_ms,
                        )
                    return
            except RelayError as exc:
                self._recover_xrun(
                    detail=str(exc),
                    now_monotonic=now_monotonic,
                )
                return
            if self._pending:
                self._pending.popleft()
            if inserting_silence:
                self._silence_periods += 1
                # Two exact 10 ms sink periods restore one 20 ms source-frame
                # deficit. Count that correction as one drift splice while the
                # exact inserted-period total remains separately observable.
                if not insert_splice_recorded:
                    self._record_splice(
                        direction="insert",
                        fill_ms=current_fill_ms,
                    )
                    insert_splice_recorded = True
            current_fill_ms = min(
                self._pcm_buffer_ms,
                current_fill_ms + ALSA_PERIOD_MS,
            )
            self._fill_ms = current_fill_ms

    def _write_loop(self) -> None:
        try:
            next_poll = time.monotonic()
            while True:
                wait_seconds = max(0.0, next_poll - time.monotonic())
                if self._stop.wait(wait_seconds):
                    return
                if self.queue.closed:
                    return
                self._writer_step(now_monotonic=time.monotonic())
                next_poll += WRITER_POLL_SECONDS
                if next_poll < time.monotonic() - WRITER_POLL_SECONDS:
                    next_poll = time.monotonic()
        except RelayError as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def check(self) -> None:
        if self.error:
            raise RelayError(f"ALSA writer stopped: {self.error}")
        if self._thread is not None and not self._thread.is_alive():
            raise RelayError("ALSA writer thread exited unexpectedly")

    def progress(self) -> tuple[int, float, float]:
        """Return frames, monotonic progress time, and wall-clock progress time."""

        with self._progress_lock:
            return (
                self.frames_written,
                self.last_progress_monotonic,
                self.last_progress_epoch_sec,
            )

    def source_ages_ms(self) -> tuple[float, ...]:
        """Return a stable snapshot of recent bridge-emit-to-write ages."""

        return self.source_age_snapshot().samples_ms

    def source_age_snapshot(self) -> SourceAgeSnapshot:
        """Return samples plus the reset generation that owns them."""

        with self._progress_lock:
            return SourceAgeSnapshot(
                samples_ms=tuple(self._source_ages_ms),
                generation=self._source_age_generation,
                started_epoch_sec=self._source_age_started_epoch_sec,
                samples_appended=self._source_age_samples_appended,
            )

    def reset_source_age_window(self) -> None:
        """Exclude prior/idle-session samples from the next recording window."""

        with self._progress_lock:
            self._source_ages_ms.clear()
            self._source_age_generation += 1
            self._source_age_started_epoch_sec = time.time()

    def writer_snapshot(self) -> WriterSnapshot:
        # One writer thread owns these scalar fields. Status accepts a
        # tick-level mixed snapshot rather than taking the source-age lock and
        # implying cross-field atomicity that the metrics do not require.
        return WriterSnapshot(
            fill_ms=self._fill_ms,
            splices=self._writer_splices,
            xruns=self._writer_xruns,
            resets=self._writer_resets,
            idle_sanitizations=self._idle_sanitizations,
            silence_periods=self._silence_periods,
            discarded_periods=self._discarded_periods,
            target_ms=WRITER_TARGET_MS,
            pcm_period_ms=ALSA_PERIOD_MS,
            pcm_buffer_ms=self._pcm_buffer_ms,
        )

    def close(self) -> None:
        self._stop.set()
        self.queue.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._close_pcm()


def _decode_audio_packet(
    payload: bytes,
    *,
    received_monotonic_ns: int,
) -> tuple[QueuedFrame, ...] | None:
    """Decode one supported relay datagram; return ``None`` if malformed."""

    if len(payload) == V2_PACKET_BYTES:
        try:
            magic, version, flags, seq, t_bridge_emit_ns = struct.unpack(
                USB_MIC_HEADER_STRUCT,
                payload[:USB_MIC_HEADER_BYTES],
            )
        except struct.error:
            return None
        if (
            magic != USB_MIC_PACKET_MAGIC
            or version != USB_MIC_PACKET_VERSION
            or flags != 0
            or t_bridge_emit_ns <= 0
            or t_bridge_emit_ns > received_monotonic_ns
        ):
            return None
        return (
            QueuedFrame(
                t_bridge_emit_ns=t_bridge_emit_ns,
                seq=seq,
                pcm=payload[USB_MIC_HEADER_BYTES:],
            ),
        )
    if len(payload) == PACKET_BYTES:
        return (QueuedFrame(received_monotonic_ns, None, payload),)
    if len(payload) == LEGACY_PACKET_BYTES:
        return tuple(
            QueuedFrame(
                received_monotonic_ns,
                None,
                payload[offset : offset + PERIOD_BYTES],
            )
            for offset in range(0, len(payload), PERIOD_BYTES)
        )
    return None


def _source_age_percentiles(samples_ms: Iterable[float]) -> dict[str, float | None]:
    values = tuple(samples_ms)

    def percentile(fraction: float) -> float | None:
        value = nearest_rank_percentile(values, fraction)
        return round(value, 1) if value is not None else None

    return {
        "source_age_ms_p50": percentile(0.50),
        "source_age_ms_p95": percentile(0.95),
        "source_age_ms_p99": percentile(0.99),
    }


@dataclass(frozen=True)
class HostPcmSnapshot:
    running: bool
    hw_ptr: int | None
    appl_ptr: int | None = None


def _read_host_pcm_status(
    path: Path = HOST_PCM_STATUS_PATH,
) -> HostPcmSnapshot:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return HostPcmSnapshot(False, None, None)
    hw_match = re.search(
        r"^\s*hw_ptr\s*:\s*(\d+)\s*$",
        text,
        re.MULTILINE,
    )
    appl_match = re.search(
        r"^\s*appl_ptr\s*:\s*(\d+)\s*$",
        text,
        re.MULTILINE,
    )
    return HostPcmSnapshot(
        running="state: RUNNING" in text,
        hw_ptr=int(hw_match.group(1)) if hw_match else None,
        appl_ptr=int(appl_match.group(1)) if appl_match else None,
    )


class HostProgressTracker:
    """Turn ALSA's RUNNING label into evidence of advancing host reads."""

    def __init__(self) -> None:
        self._previous = HostPcmSnapshot(False, None)
        self._running_since_monotonic = 0.0
        self.last_progress_monotonic = 0.0
        self.last_progress_epoch_sec = 0.0

    def observe(
        self,
        snapshot: HostPcmSnapshot,
        *,
        now_monotonic: float,
        now_epoch_sec: float,
    ) -> bool:
        advanced = bool(
            snapshot.running
            and self._previous.running
            and snapshot.hw_ptr is not None
            and self._previous.hw_ptr is not None
            and snapshot.hw_ptr > self._previous.hw_ptr
        )
        if snapshot.running and not self._previous.running:
            self._running_since_monotonic = now_monotonic
        if advanced:
            self.last_progress_monotonic = now_monotonic
            self.last_progress_epoch_sec = now_epoch_sec
        if not snapshot.running:
            self._running_since_monotonic = 0.0
        self._previous = snapshot
        return advanced

    def progress_age(self, now_monotonic: float) -> float | None:
        if not self._previous.running:
            return None
        baseline = max(
            self.last_progress_monotonic,
            self._running_since_monotonic,
        )
        return max(0.0, now_monotonic - baseline)

    @property
    def has_progressed(self) -> bool:
        return bool(
            self._previous.running
            and self.last_progress_monotonic >= self._running_since_monotonic
            and self.last_progress_monotonic > 0.0
        )


@dataclass
class DropRegimeCounters:
    """Queue drops attributed by sampled host-clock status interval."""

    streaming: int = 0
    idle: int = 0

    def record(self, count: int, *, host_clock_advancing: bool) -> None:
        if count <= 0:
            return
        if host_clock_advancing:
            self.streaming += count
        else:
            self.idle += count


def _audio_health_snapshot(
    *,
    now_monotonic: float,
    started_monotonic: float,
    last_packet_monotonic: float,
    last_sink_progress_monotonic: float,
    host_snapshot: HostPcmSnapshot,
    host_progress: HostProgressTracker,
    sustained_drops: bool,
) -> dict[str, bool | float | None | str]:
    """Classify source, writer, and host-clock progress without side effects."""

    source_baseline = last_packet_monotonic or started_monotonic
    source_age = max(0.0, now_monotonic - source_baseline)
    source_stalled = source_age > AUDIO_PROGRESS_FRESH_SECONDS
    host_progress_age = host_progress.progress_age(now_monotonic)
    host_was_streaming = host_progress.has_progressed
    host_progressing = bool(
        host_snapshot.running
        and host_was_streaming
        and host_progress_age is not None
        and host_progress_age <= AUDIO_PROGRESS_FRESH_SECONDS
    )
    host_stalled = bool(
        host_snapshot.running
        and host_was_streaming
        and host_progress_age is not None
        and host_progress_age > AUDIO_PROGRESS_FRESH_SECONDS
    )
    sink_baseline = last_sink_progress_monotonic or started_monotonic
    sink_progress_age = max(0.0, now_monotonic - sink_baseline)
    sink_stalled = bool(
        host_snapshot.running
        and host_was_streaming
        and sink_progress_age > AUDIO_PROGRESS_FRESH_SECONDS
    )
    # A non-advancing gadget hw_ptr is also what an idle Mac looks like, so it
    # clears Streaming but is not by itself a product fault. Drops become a
    # fault only while the independently observed host clock is advancing.
    drop_stalled = bool(host_progressing and sustained_drops)
    audio_stalled = source_stalled or drop_stalled
    reasons: list[str] = []
    if source_stalled:
        reasons.append("AEC source packets stopped")
    if drop_stalled:
        reasons.append("USB audio queue is dropping continuously")
    return {
        "audio_healthy": not audio_stalled,
        "audio_stalled": audio_stalled,
        "audio_health_detail": "; ".join(reasons),
        "source_stalled": source_stalled,
        "sink_stalled": sink_stalled,
        "host_stalled": host_stalled,
        "host_streaming": bool(
            host_progressing and not sink_stalled and not audio_stalled
        ),
        "packet_age_ms": round(source_age * 1000.0, 1),
        "sink_progress_age_ms": round(sink_progress_age * 1000.0, 1),
        "host_progress_age_ms": (
            round(host_progress_age * 1000.0, 1)
            if host_progress_age is not None
            else None
        ),
    }


def _ready(
    *,
    intent_path: str = INTENT_PATH,
    gadget_path: str = GADGET_PATH,
) -> tuple[bool, str]:
    if not usb_mic_enabled(intent_path):
        return False, "intent_off_or_invalid"
    function = Path(gadget_path) / "functions/uac2.usb0"
    try:
        p_chmask = (function / "p_chmask").read_text(encoding="utf-8").strip()
    except OSError:
        return False, "uac2_missing"
    if p_chmask != "1":
        return False, f"p_chmask_{p_chmask or 'missing'}"
    try:
        active = (
            subprocess.run(
                ["systemctl", "is-active", "--quiet", "jasper-aec-bridge.service"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        active = False
    return (True, "ready") if active else (False, "aec_bridge_inactive")


def _write_status(path: str, payload: dict[str, Any]) -> None:
    payload = {
        **payload,
        "schema_version": RELAY_SCHEMA_VERSION,
        "updated_epoch_sec": time.time(),
    }
    atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n", mode=0o644)


def run_relay(
    *,
    udp_port: int = USB_HOST_MIC_UDP_PORT,
    status_path: str = RELAY_STATUS_PATH,
) -> int:
    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
    sock.bind(("127.0.0.1", udp_port))
    sock.settimeout(0.2)
    sink = AlsaGadgetSink()
    packets = 0
    v2_packets = 0
    v1_packets = 0
    legacy_packets = 0
    periods = 0
    malformed_packets = 0
    packets_lost = 0
    sequence = SequenceTracker()
    started_monotonic = time.monotonic()
    relay_started_epoch_sec = time.time()
    last_packet_monotonic = 0.0
    last_packet_epoch_sec = 0.0
    last_status = 0.0
    last_status_drops = 0
    drop_regimes = DropRegimeCounters()
    drop_streak = 0
    host_progress = HostProgressTracker()
    last_host_clock_advancing = False
    last_audio_stalled: bool | None = None
    log_event(logger, "usb_mic.started", udp_port=udp_port)
    try:
        while not stop.is_set():
            sink.check()
            try:
                payload, _address = sock.recvfrom(65_536)
            except socket.timeout:
                payload = b""
            if payload:
                if len(payload) not in ACCEPTED_PACKET_BYTES:
                    malformed_packets += 1
                else:
                    received_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
                    frames = _decode_audio_packet(
                        payload,
                        received_monotonic_ns=received_ns,
                    )
                    if frames is None:
                        malformed_packets += 1
                    else:
                        packets += 1
                        if len(payload) == V2_PACKET_BYTES:
                            v2_packets += 1
                        elif len(payload) == PACKET_BYTES:
                            v1_packets += 1
                        else:
                            legacy_packets += 1
                        last_packet_monotonic = time.monotonic()
                        last_packet_epoch_sec = time.time()
                        packet_seq = frames[0].seq
                        if packet_seq is None:
                            sequence.clear_baseline()
                        else:
                            packets_lost += sequence.observe(packet_seq)
                        for frame in frames:
                            sink.queue.put(frame)
                            periods += 1
            now = time.monotonic()
            if now - last_status >= 0.5:
                now_epoch = time.time()
                host_snapshot = _read_host_pcm_status()
                host_clock_advanced = host_progress.observe(
                    host_snapshot,
                    now_monotonic=now,
                    now_epoch_sec=now_epoch,
                )
                # While the host clock is idle, repeatedly discard any samples
                # produced while the gadget ring is frozen. The first samples
                # written when a real host session resumes then remain in a
                # fresh generation instead of mixing with a prior recording.
                if not host_clock_advanced:
                    sink.reset_source_age_window()
                sink_frames, sink_progress_monotonic, sink_progress_epoch = (
                    sink.progress()
                )
                frames_written = sink_frames
                periods_dropped = sink.queue.dropped
                drops_since_status = max(0, periods_dropped - last_status_drops)
                status_interval = max(0.001, now - last_status) if last_status else 0.5
                last_host_clock_advancing = host_clock_advanced
                if host_clock_advanced and drops_since_status:
                    drop_streak += 1
                else:
                    drop_streak = 0
                sustained_drops = drop_streak >= DROP_STREAK_WARN_INTERVALS
                health = _audio_health_snapshot(
                    now_monotonic=now,
                    started_monotonic=started_monotonic,
                    last_packet_monotonic=last_packet_monotonic,
                    last_sink_progress_monotonic=sink_progress_monotonic,
                    host_snapshot=host_snapshot,
                    host_progress=host_progress,
                    sustained_drops=sustained_drops,
                )
                drop_regimes.record(
                    drops_since_status,
                    host_clock_advancing=host_clock_advanced,
                )
                source_age = sink.source_age_snapshot()
                source_ages_ms = source_age.samples_ms
                age_percentiles = _source_age_percentiles(source_ages_ms)
                writer = sink.writer_snapshot()
                audio_stalled = bool(health["audio_stalled"])
                if audio_stalled != last_audio_stalled:
                    log_event(
                        logger,
                        "usb_mic.audio_health",
                        state="stalled" if audio_stalled else "healthy",
                        detail=str(health["audio_health_detail"]),
                        host_pcm_running=int(host_snapshot.running),
                        periods_dropped=periods_dropped,
                        level=logging.WARNING if audio_stalled else logging.INFO,
                    )
                    last_audio_stalled = audio_stalled
                _write_status(
                    status_path,
                    {
                        "state": "running",
                        "packets_received": packets,
                        "v2_packets_received": v2_packets,
                        "v1_packets_received": v1_packets,
                        "legacy_packets_received": legacy_packets,
                        "malformed_packets": malformed_packets,
                        "packets_lost": packets_lost,
                        "sequence_resets": sequence.resets,
                        "sequence_reorders": sequence.reorders,
                        "sequence_discontinuities": sequence.discontinuities,
                        "periods_queued": periods,
                        "periods_dropped": periods_dropped,
                        "periods_dropped_streaming": drop_regimes.streaming,
                        "periods_dropped_idle": drop_regimes.idle,
                        "periods_dropped_since_status": drops_since_status,
                        "drop_rate_periods_per_sec": round(
                            drops_since_status / status_interval,
                            1,
                        ),
                        "sustained_drops": sustained_drops,
                        "frames_written": frames_written,
                        "last_packet_epoch_sec": last_packet_epoch_sec,
                        "last_sink_progress_epoch_sec": sink_progress_epoch,
                        "last_host_progress_epoch_sec": (
                            host_progress.last_progress_epoch_sec
                        ),
                        "host_pcm_running": host_snapshot.running,
                        "host_hw_ptr": host_snapshot.hw_ptr,
                        "host_appl_ptr": host_snapshot.appl_ptr,
                        "source_age_basis": SOURCE_AGE_BASIS,
                        "source_age_scope": SOURCE_AGE_SCOPE,
                        "source_age_sample_count": len(source_ages_ms),
                        "source_age_samples_appended": source_age.samples_appended,
                        "source_age_window_generation": source_age.generation,
                        "source_age_window_started_epoch_sec": (
                            source_age.started_epoch_sec
                        ),
                        "drop_regime_basis": DROP_REGIME_BASIS,
                        "writer_fill_ms": writer.fill_ms,
                        "writer_target_ms": writer.target_ms,
                        "writer_pcm_rate_hz": SOURCE_RATE,
                        "writer_pcm_period_frames": ALSA_PERIOD_FRAMES,
                        "writer_pcm_buffer_frames": round(
                            writer.pcm_buffer_ms * SOURCE_RATE / 1000.0
                        ),
                        "gadget_hardware_rate_hz": GADGET_RATE,
                        "writer_pcm_period_ms": writer.pcm_period_ms,
                        "writer_pcm_buffer_ms": writer.pcm_buffer_ms,
                        "writer_splices": writer.splices,
                        "writer_xruns": writer.xruns,
                        "writer_resets": writer.resets,
                        "writer_idle_sanitizations": writer.idle_sanitizations,
                        "writer_silence_periods": writer.silence_periods,
                        "writer_discarded_periods": writer.discarded_periods,
                        **age_percentiles,
                        **health,
                        "udp_port": udp_port,
                        "relay_pid": os.getpid(),
                        "relay_started_epoch_sec": relay_started_epoch_sec,
                    },
                )
                last_status = now
                last_status_drops = periods_dropped
    finally:
        sink.close()
        sock.close()
        residual_drops = max(0, sink.queue.dropped - last_status_drops)
        drop_regimes.record(
            residual_drops,
            host_clock_advancing=last_host_clock_advancing,
        )
        final_source_age = sink.source_age_snapshot()
        final_source_ages_ms = final_source_age.samples_ms
        final_writer = sink.writer_snapshot()
        _write_status(
            status_path,
            {
                "state": "stopped",
                "packets_received": packets,
                "v2_packets_received": v2_packets,
                "v1_packets_received": v1_packets,
                "legacy_packets_received": legacy_packets,
                "malformed_packets": malformed_packets,
                "packets_lost": packets_lost,
                "sequence_resets": sequence.resets,
                "sequence_reorders": sequence.reorders,
                "sequence_discontinuities": sequence.discontinuities,
                "periods_queued": periods,
                "periods_dropped": sink.queue.dropped,
                "periods_dropped_streaming": drop_regimes.streaming,
                "periods_dropped_idle": drop_regimes.idle,
                "frames_written": sink.progress()[0],
                "source_age_basis": SOURCE_AGE_BASIS,
                "source_age_scope": SOURCE_AGE_SCOPE,
                "source_age_sample_count": len(final_source_ages_ms),
                "source_age_samples_appended": final_source_age.samples_appended,
                "source_age_window_generation": final_source_age.generation,
                "source_age_window_started_epoch_sec": (
                    final_source_age.started_epoch_sec
                ),
                "drop_regime_basis": DROP_REGIME_BASIS,
                "writer_fill_ms": final_writer.fill_ms,
                "writer_target_ms": final_writer.target_ms,
                "writer_pcm_rate_hz": SOURCE_RATE,
                "writer_pcm_period_frames": ALSA_PERIOD_FRAMES,
                "writer_pcm_buffer_frames": round(
                    final_writer.pcm_buffer_ms * SOURCE_RATE / 1000.0
                ),
                "gadget_hardware_rate_hz": GADGET_RATE,
                "writer_pcm_period_ms": final_writer.pcm_period_ms,
                "writer_pcm_buffer_ms": final_writer.pcm_buffer_ms,
                "writer_splices": final_writer.splices,
                "writer_xruns": final_writer.xruns,
                "writer_resets": final_writer.resets,
                "writer_idle_sanitizations": final_writer.idle_sanitizations,
                "writer_silence_periods": final_writer.silence_periods,
                "writer_discarded_periods": final_writer.discarded_periods,
                **_source_age_percentiles(final_source_ages_ms),
                "host_streaming": False,
                "audio_healthy": False,
                "audio_stalled": False,
                "udp_port": udp_port,
                "relay_pid": os.getpid(),
                "relay_started_epoch_sec": relay_started_epoch_sec,
            },
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-ready", action="store_true")
    parser.add_argument("--udp-port", type=int, default=USB_HOST_MIC_UDP_PORT)
    parser.add_argument("--status-path", default=RELAY_STATUS_PATH)
    args = parser.parse_args(argv)
    if args.check_ready:
        ready, reason = _ready()
        if not ready:
            log_event(logger, "usb_mic.skip", reason=reason)
        return 0 if ready else 1
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s usb-mic %(levelname)s %(message)s",
    )
    try:
        return run_relay(udp_port=args.udp_port, status_path=args.status_path)
    except (OSError, RelayError) as exc:
        logger.error("USB mic relay failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
