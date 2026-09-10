# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
import select
import socket
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from typing import TYPE_CHECKING

import numpy as np

from .assistant_loudness import (
    AssistantSourceMeter,
    DEFAULT_PROFILE_PATH as ASSISTANT_LOUDNESS_PROFILE_PATH,
    confidence_for_measurement,
    profile_for_outputd,
    update_profile_from_measurement,
    upsample_2x,
)
from .assistant_volume import EffectiveVolumeContext
from .fanin_coupling import assistant_wire_is_wide, resolve_ring_wire_format
from .log_event import log_event
from .platform import wire
from .tts_routing import FANIN_TTS_SOCKET

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


_OUTPUTD_AUDIO_FRAME_BYTES = 4  # stereo S16_LE — the narrow wire
_OUTPUTD_AUDIO_FRAME_BYTES_WIDE = 8  # stereo S32_LE — the wide wire
_OUTPUTD_SAMPLE_RATE = 48_000

# The exact i16 -> i32 spine-scale factor, 2^16. Named here because it is a
# CONTRACT with Rust, not a local convenience: it is the same power of two
# `jasper_resampler::widen_i16_to_i32` shifts by, so a wide payload and a narrow
# one describe the same signal at two scales and `narrow_i32_to_i16_round`
# inverts the promotion exactly. Pinned by tests/test_tts_wire_width.py.
_SPINE_SCALE = 65_536
_I32_MIN = -(2 ** 31)
_I32_MAX = 2 ** 31 - 1
_OUTPUTD_FLUSH_ACK_TIMEOUT_SEC = 3.0
# All IPC is local to the Pi. Healthy connects and control writes complete in
# milliseconds, while one second tolerates scheduler pressure without letting
# a dead owner or full Unix-socket buffer strand voice teardown indefinitely.
# Lock waits use the same ceiling: their owner is itself bounded by the socket
# timeout, and a timed-out waiter poisons the socket to wake that owner.
_OUTPUTD_IPC_CONNECT_TIMEOUT_SEC = 1.0
_OUTPUTD_IPC_IO_TIMEOUT_SEC = 1.0
_OUTPUTD_IPC_LOCK_TIMEOUT_SEC = 1.0
# A closed socket may not wake another thread's select on macOS.
_OUTPUTD_IPC_CANCEL_POLL_SEC = 0.05
# MEASURE_PAUSE is a rare safety-control request, not an audio hot path. Its
# canonical adapter call runs synchronously so it cannot outlive the reply;
# 250 ms matches one IPC audio chunk and leaves ample room inside the daemon's
# aggregate pause budget without stalling the event loop for a full IPC second.
_OUTPUTD_MEASUREMENT_CONTROL_SLICE_SEC = 0.25
# Keep individual IPC messages well below the daemon's 2 MiB hard cap.
# 250 ms chunks make barge-in/flush sharper and set the granularity at
# which the writer's pacing (below) applies backpressure. Chunking alone
# applies none — the owner drops on overflow rather than blocking.
#
# This is a BYTE ceiling, and deliberately stays one on both wires: it bounds
# the allocation a single AUDIO/AUDIO32 command can ask the daemon for, and that
# bound must not double because a box declared a wider wire. So the duration it
# buys is wire-dependent — 250 ms of the narrow S16 wire, 125 ms of the wide S32
# one — while the memory it costs the daemon is the same either way. Barge-in
# granularity on a wide box is correspondingly finer, not coarser.
_OUTPUTD_MAX_AUDIO_CHUNK_BYTES = (
    _OUTPUTD_SAMPLE_RATE * _OUTPUTD_AUDIO_FRAME_BYTES // 4
)
# Pace sustained writes so the IPC owner's pending-audio queue never
# overflows. The owner (jasper-fanin's TTS lane, DEFAULT_MAX_PENDING_FRAMES
# in rust/jasper-fanin/src/tts.rs = 2 s) DROPS whole audio commands that
# arrive while its queue is full — it cannot block the socket reader,
# because a blocked reader would also stall FLUSH (barge-in) behind queued
# audio. OpenAI Realtime delivers replies faster than realtime (~11 s of
# audio in ~4 s), so an unpaced writer overflows the budget and the
# surviving chunks play as garbled "fast-forward" audio
# (event=fanin.tts_command_dropped).
# Keeping ≤1.2 s queued ahead of realtime leaves 0.55 s of margin
# (2.0 s budget − 1.2 s watermark − one 0.25 s IPC chunk) against
# event-loop jitter AND the bounded drift from a concurrent same-object
# writer (the fire-and-forget listening chirp, ~0.3 s, whose ring update
# can race another write's local pacing mirror), while staying deep
# enough that a stalled writer has >1 s before audible underrun.
# tests/test_tts_ipc_pacing.py pins the watermark against the Rust
# budget so the two cannot silently drift apart.
_OUTPUTD_PACE_AHEAD_SEC = 1.2

# Pacing sleeps go through this alias so tests can substitute a spy
# without patching the global asyncio module.
_pace_sleep = asyncio.sleep


def _outputd_audio_chunks(data: bytes, frame_bytes: int = _OUTPUTD_AUDIO_FRAME_BYTES):
    """Split TTS IPC AUDIO payloads below the daemon's protocol cap.

    Rust rejects AUDIO chunks above 2 MiB before allocation. Cached cue
    WAVs are normally short, but dynamic spoken text can occasionally
    be long enough after 24 kHz mono -> 48 kHz stereo conversion to cross
    that limit. Chunking here keeps the protocol bounded without changing
    the public TtsPlayout.write contract.

    ``frame_bytes`` is the wire's stereo frame size — 4 on the narrow S16 wire,
    8 on the wide S32 one. The chunk ceiling stays a BYTE ceiling on both, which
    is the same bound the Rust parser applies, so a wide payload simply carries
    half the frames per chunk. Sizing by frames instead would double the bytes a
    single command asks the daemon to allocate.
    """
    if not data:
        return []
    if len(data) % frame_bytes != 0:
        raise ValueError("TTS IPC audio payload must contain whole stereo frames")
    chunk_size = _OUTPUTD_MAX_AUDIO_CHUNK_BYTES
    chunk_size -= chunk_size % frame_bytes
    if chunk_size <= 0:
        raise AssertionError("TTS IPC chunk size must hold at least one frame")
    for i in range(0, len(data), chunk_size):
        yield data[i:i + chunk_size]


def _quantize_to_wire(arr, *, wide: bool):
    """Quantize a resampled float array onto the box's assistant wire.

    ``arr`` is in i16 SAMPLE UNITS (the provider streams S16, and the resampler
    keeps that scale), regardless of which wire it is headed for. This is THE
    one place the assistant path leaves floating point.

    NARROW saturates and truncates toward zero. Its bytes are a shipped
    contract: rounding instead would change the signal on every box in the
    fleet.

    WIDE scales to the i32 spine (``_SPINE_SCALE``, the exact 2^16 the Rust
    ``widen_i16_to_i32`` shifts by) and quantizes round-to-nearest saturating.
    The multiply runs in float64 not for precision — ``arr`` is float32 and
    multiplying by a power of two is exact there — but so ``np.rint`` and the
    clip compare against the i32 rails at a width that represents every i32
    exactly. Payload precision stays bounded by float32's 24-bit mantissa; the
    i32 container is sized by the spine, not by a claim about assistant
    precision.
    """
    if wide:
        scaled = np.rint(arr.astype(np.float64) * _SPINE_SCALE)
        return np.clip(scaled, _I32_MIN, _I32_MAX).astype(np.int32)
    return np.clip(arr, -32768, 32767).astype(np.int16)


@lru_cache(maxsize=1)
def tts_wire_is_wide() -> bool:
    """Whether THIS BOX's assistant wire is wide (S32). Resolved ONCE per process.

    ONE RULE, ONE OWNER. Delegates to
    :func:`jasper.fanin_coupling.assistant_wire_is_wide`, which owns the
    sender's width decision — ``jasper-fanin`` accepts either verb. The box's
    declared ``S32_LE`` wire format is the whole verdict, and it is read
    file-fresh: ``jasper-voice`` never loaded ``fanin.env``, so ``os.environ``
    would be stale.

    An unreadable or unrecognized declaration resolves to the RESOLVER'S OWN
    DEFAULT rather than raising: ``jasper-fanin`` already parks at exit 78 on
    an unrecognized value and the doctor surfaces it, while raising here would
    take down the daemon that plays the failure cues. That default is DERIVED,
    not restated — ``resolve_ring_wire_format(None)`` is the same expression
    :func:`~jasper.fanin_coupling.read_declared_ring_wire_format` falls back to
    for an absent declaration, so this process cannot land on a width no
    undeclared box has.

    CACHED so the process has exactly ONE answer — the playout (quantizing
    provider TTS) and the daemon (baking earcons) must not disagree. Two of the
    three ways the answer can move restart ``jasper-voice`` and so rebuild the
    cache: a coupling flip through ``coupling_reconcile``, and a
    resolver-default move through a deploy's
    ``park_audio_clients_for_core_graph_restart``. The third — an operator
    hand-editing ``JASPER_FANIN_RING_WIRE_FORMAT`` on a live box — is NOT
    covered: this process keeps its old answer until the documented "set it,
    reconcile, arm" sequence restarts the daemons.

    A stale answer is a width disagreement, never a level error: the IPC verb
    is self-describing (``AUDIO`` vs ``AUDIO32``), so fan-in converts whichever
    it receives and logs ``event=fanin.tts_wire_width_mismatch``.

    Tests reset it with ``tts_wire_is_wide.cache_clear()``;
    ``tests/conftest.py`` does it automatically around every test.
    """
    try:
        return assistant_wire_is_wide()
    except (OSError, ValueError) as e:
        log_event(
            logger,
            "tts_wire.declaration_unreadable",
            exc_type=type(e).__name__,
            err=str(e),
            level=logging.WARNING,
        )
        return assistant_wire_is_wide(wire_format=resolve_ring_wire_format(None))


async def _outputd_io(stream, method: str, *args, on_accepted=None, **kwargs):
    """Own a socket operation through cancellation and observe completed writes.

    Cancellation closes the socket; bounded I/O slices let the worker observe
    that close. The worker must finish before another flush/write.
    """
    worker = asyncio.create_task(asyncio.to_thread(getattr(stream, method), *args, **kwargs))
    cancelled = False
    current = asyncio.current_task()
    while not worker.done():
        try:
            await asyncio.wait({worker})
        except asyncio.CancelledError:
            cancelled = True
            if isinstance(stream, _OutputdStreamAdapter):
                stream._poison(reason=None)
            if current is not None:
                current.uncancel()
    try:
        result = worker.result()
    except Exception:  # noqa: BLE001
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    if on_accepted is not None:
        await on_accepted()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _outputd_segment_kind(kind: str) -> str:
    if kind in {"assistant", "cue", "chirp"}:
        return kind
    logger.warning(
        "fan-in TTS IPC segment kind rejected: %r; falling back to assistant",
        kind,
    )
    return "assistant"


def _outputd_provider_token(provider_item_id: str | None) -> str:
    if provider_item_id is None:
        return "-"
    if _outputd_token_ok(provider_item_id):
        return provider_item_id
    logger.warning("fan-in TTS IPC provider item id rejected: %r", provider_item_id)
    return "-"


def _outputd_token_ok(value: str) -> bool:
    return bool(value) and value.isascii() and not any(ch.isspace() for ch in value)


def _outputd_profile_tokens(profile) -> list[str] | None:
    if profile is None:
        return None
    for field in (profile.provider, profile.model, profile.voice):
        if not _outputd_token_ok(field):
            logger.warning(
                "fan-in TTS IPC profile token rejected: provider=%r model=%r voice=%r",
                profile.provider, profile.model, profile.voice,
            )
            return None
    return [
        profile.provider,
        profile.model,
        profile.voice,
        f"{profile.source_lufs:.2f}",
        f"{profile.source_peak_dbfs:.2f}",
        f"{profile.confidence:.2f}",
    ]


class _OutputdStreamAdapter:
    """Tiny sync writer used by TtsPlayout.

    TtsPlayout does resample, mono-to-stereo, and drain accounting
    before calling ``self._stream.write(bytes)`` in a worker thread. This
    adapter preserves the blocking stream shape while swapping the final
    sink from PortAudio to the local TTS Unix socket.
    """

    def __init__(self, sock: socket.socket, *, wire_wide: bool = False) -> None:
        # The payload verb this connection speaks — the wire's own DECLARATION
        # of its sample width ("AUDIO" = S16LE, "AUDIO32" = S32LE at spine
        # scale). Fixed for the life of the connection because the box's wire is
        # fixed for the life of the daemon; see `TtsWireWidth` in
        # rust/jasper-tts-protocol/src/lib.rs for why the reader honours the
        # declaration rather than assuming one.
        self._audio_verb = (
            wire.TTS_AUDIO_WIDE if wire_wide else wire.TTS_AUDIO_NARROW
        )
        self._sock = sock
        self._sock.settimeout(_OUTPUTD_IPC_IO_TIMEOUT_SEC)
        self._recv_buffer = bytearray()
        self._lock = threading.Lock()
        self._active_segment: tuple[str, str, tuple[str, ...] | None] | None = None
        self._closed = False
        self._timeout_logged = False

    @property
    def closed(self) -> bool:
        return self._closed

    def _readline_locked(self, timeout_sec: float) -> bytes:
        """Read one daemon response line while the caller holds _lock."""
        deadline = time.monotonic() + timeout_sec
        while True:
            newline_at = self._recv_buffer.find(b"\n")
            if newline_at >= 0:
                line = bytes(self._recv_buffer[: newline_at + 1])
                del self._recv_buffer[: newline_at + 1]
                return line

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if self._closed:
                raise BrokenPipeError("TTS IPC socket is closed")
            readable, _, _ = select.select(
                [self._sock], [], [], min(remaining, _OUTPUTD_IPC_CANCEL_POLL_SEC),
            )
            if not readable:
                continue
            chunk = self._sock.recv(4096)
            if not chunk:
                return b""
            self._recv_buffer.extend(chunk)

    def _close_unlocked(self, *, send_close: bool) -> None:
        if self._closed:
            return
        try:
            if send_close:
                if self._active_segment is not None:
                    self._send_line(wire.TTS_SEGMENT_END)
                    self._active_segment = None
                self._send_line(wire.TTS_CLOSE)
        except OSError:
            pass
        self._poison(reason=None)

    def _poison(
        self,
        *,
        reason: str | None,
        timeout_sec: float | None = None,
    ) -> None:
        """Close from any thread; blocked operations check closure each slice."""

        if self._closed:
            return
        self._closed = True
        self._active_segment = None
        self._recv_buffer.clear()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        if reason is not None and not self._timeout_logged:
            self._timeout_logged = True
            if timeout_sec is None:
                timeout_sec = (
                    _OUTPUTD_IPC_LOCK_TIMEOUT_SEC
                    if reason == "lock"
                    else _OUTPUTD_IPC_IO_TIMEOUT_SEC
                )
            log_event(
                logger,
                "tts_fanin.adapter_timeout",
                phase=reason,
                timeout_sec=timeout_sec,
                action="socket_poisoned",
                level=logging.WARNING,
            )

    @staticmethod
    def _remaining_timeout(
        ceiling_sec: float,
        deadline_monotonic: float | None,
    ) -> float:
        if deadline_monotonic is None:
            return ceiling_sec
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError("TTS IPC aggregate deadline expired")
        return min(ceiling_sec, remaining)

    @contextmanager
    def _bounded_lock(
        self,
        *,
        deadline_monotonic: float | None = None,
    ):
        try:
            timeout_sec = self._remaining_timeout(
                _OUTPUTD_IPC_LOCK_TIMEOUT_SEC,
                deadline_monotonic,
            )
        except TimeoutError:
            self._poison(reason="lock", timeout_sec=0.0)
            raise
        acquired = self._lock.acquire(timeout=timeout_sec)
        if not acquired:
            # Only the owning worker can release this lock. Closing the
            # socket makes that worker fail within its current I/O slice.
            self._poison(reason="lock", timeout_sec=timeout_sec)
            raise TimeoutError(
                "TTS IPC adapter lock timed out after "
                f"{timeout_sec:.3f}s"
            )
        try:
            yield
        finally:
            self._lock.release()

    def _send_line(
        self,
        command: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        self._sendall_locked(
            wire.encode(command), deadline_monotonic=deadline_monotonic,
        )

    def _sendall_locked(
        self,
        data: bytes,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        if self._closed:
            raise BrokenPipeError("TTS IPC socket is closed")
        try:
            timeout_sec = self._remaining_timeout(
                _OUTPUTD_IPC_IO_TIMEOUT_SEC,
                deadline_monotonic,
            )
        except TimeoutError:
            self._poison(reason="send", timeout_sec=0.0)
            raise
        try:
            deadline = time.monotonic() + timeout_sec
            remaining_data = memoryview(data)
            while remaining_data:
                if self._closed:
                    raise BrokenPipeError("TTS IPC socket is closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self._sock.settimeout(min(remaining, _OUTPUTD_IPC_CANCEL_POLL_SEC))
                try:
                    sent = self._sock.send(remaining_data)
                except TimeoutError:
                    continue
                if sent == 0:
                    raise BrokenPipeError("TTS IPC socket stopped accepting bytes")
                remaining_data = remaining_data[sent:]
        except TimeoutError as e:
            self._poison(reason="send", timeout_sec=timeout_sec)
            raise TimeoutError(
                "TTS IPC send timed out after "
                f"{timeout_sec:.3f}s"
            ) from e
        except OSError:
            self._poison(reason=None)
            raise

    def set_gain_db(self, db: float) -> None:
        with self._bounded_lock():
            self._send_line(wire.tts_gain(db))

    def program_duck(self, on: bool) -> None:
        with self._bounded_lock():
            self._send_line(wire.tts_program_duck(on))

    def prepare_assistant(
        self,
        *,
        provider: str,
        model: str,
        voice: str,
        tts_envelope_lufs: float,
        volume_context: EffectiveVolumeContext | None = None,
    ) -> None:
        if not (
            _outputd_token_ok(provider)
            and _outputd_token_ok(model)
            and _outputd_token_ok(voice)
        ):
            logger.warning(
                "fan-in TTS IPC prepare rejected invalid profile identity: "
                "provider=%r model=%r voice=%r",
                provider, model, voice,
            )
            return
        with self._bounded_lock():
            self._send_line(
                wire.tts_prepare_assistant(
                    provider=provider,
                    model=model,
                    voice=voice,
                    tts_envelope_lufs=tts_envelope_lufs,
                    volume_context=volume_context,
                )
            )

    def pause_content_meter(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        with self._bounded_lock(deadline_monotonic=deadline_monotonic):
            self._send_line(
                wire.TTS_CONTENT_METER_PAUSE,
                deadline_monotonic=deadline_monotonic,
            )

    def resume_content_meter(self) -> None:
        with self._bounded_lock():
            self._send_line(wire.TTS_CONTENT_METER_RESUME)

    def start_segment(
        self,
        *,
        kind: str,
        provider_item_id: str | None,
        profile=None,
    ) -> None:
        profile_tokens = _outputd_profile_tokens(profile)
        segment = (
            _outputd_segment_kind(kind),
            _outputd_provider_token(provider_item_id),
            tuple(profile_tokens) if profile_tokens is not None else None,
        )
        with self._bounded_lock():
            if self._active_segment == segment:
                return
            if self._active_segment is not None:
                self._send_line(wire.TTS_SEGMENT_END)
            self._send_line(wire.tts_segment_start(*segment))
            self._active_segment = segment

    def end_segment(self) -> None:
        with self._bounded_lock():
            if self._active_segment is None:
                return
            self._send_line(wire.TTS_SEGMENT_END)
            self._active_segment = None

    def write(self, data: bytes) -> None:
        with self._bounded_lock():
            self._send_line(wire.tts_audio(self._audio_verb, len(data)))
            self._sendall_locked(data)

    def abort(self) -> None:
        self.flush_sync()

    def flush_sync(self) -> dict | None:
        with self._bounded_lock():
            try:
                self._send_line(wire.TTS_FLUSH_SYNC)
                self._active_segment = None
                line = self._readline_locked(_OUTPUTD_FLUSH_ACK_TIMEOUT_SEC)
            except TimeoutError:
                logger.warning(
                    "fan-in TTS IPC flush ack timed out after %.1fs; "
                    "closing socket",
                    _OUTPUTD_FLUSH_ACK_TIMEOUT_SEC,
                )
                self._close_unlocked(send_close=False)
                return None
            except OSError as e:
                logger.warning("fan-in TTS IPC flush failed: %s", e)
                self._close_unlocked(send_close=False)
                return None
        if not line:
            return None
        try:
            ack = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning("fan-in TTS IPC flush ack parse failed: %s", e)
            return None
        if not isinstance(ack, dict):
            logger.warning("fan-in TTS IPC flush ack had unexpected shape: %r", ack)
            return None
        return ack

    def start(self) -> None:
        # No-op: the stream stays open after FLUSH_SYNC. Satisfies the
        # abort()+start() shape TtsPlayout.flush falls back to
        # when a stream has no flush_sync.
        return None

    def close(self) -> None:
        if self._closed:
            return
        with self._bounded_lock():
            self._close_unlocked(send_close=True)


def confirmed_tts_flush(ack: object) -> bool:
    """Validate the shared fan-in/outputd stop and segment ledger reply."""
    if not isinstance(ack, dict) or ack.get("ok") is not True:
        return False
    for key in ("requests", "pending_frames", "segments", "flushed_frames", "max_audio_played_ms"):
        value = ack.get(key)
        if type(value) is not int or value < 0:
            return False
    events = ack.get("events")
    if ack["requests"] == 0 or not isinstance(events, list) or len(events) != ack["segments"]:
        return False
    segments = set()
    for event in events:
        if not isinstance(event, dict):
            return False
        for key in ("segment", "queued_frames", "written_frames", "drained_frames", "flushed_frames"):
            value = event.get(key)
            if type(value) is not int or value < 0:
                return False
        if event["segment"] in segments:
            return False
        segments.add(event["segment"])
        item = event.get("provider_item_id")
        if "provider_item_id" not in event or not (item is None or isinstance(item, str) and item):
            return False
        if event.get("kind") not in {"assistant", "cue", "chirp"}:
            return False
        if not 0 <= event["drained_frames"] <= event["written_frames"] <= event["queued_frames"]:
            return False
        if event["flushed_frames"] > event["queued_frames"]:
            return False
    return True


class TtsPlayout:
    """Assistant-audio playout: gain validation, drain-deadline timing, and
    the fan-in TTS IPC client.

    Provider PCM enters as 24 kHz mono; write() polyphase-upsamples it 2x to
    the fan-in socket's fixed 48 kHz, duplicates mono to stereo, updates the
    drain deadline, and writes bytes to this class's socket adapter. Gain
    travels as metadata so the TTS IPC owner applies the final clamp at its
    mix boundary.
    """

    INPUT_RATE = 24000

    # Floor — below this, TTS is effectively silent. Used when the
    # user mutes, when Camilla is unreachable at startup, or when a
    # volume reading looks malformed.
    MIN_TTS_GAIN_DB = -60.0

    def __init__(
        self,
        socket_path: str = FANIN_TTS_SOCKET,
        gain_db: float = 0.0,
        *,
        drain_tail_sec: float = 0.085,  # production wires from cfg.tts_drain_tail_sec
        provider: str = "",
        model: str = "",
        voice: str = "",
        profile_path: str = ASSISTANT_LOUDNESS_PROFILE_PATH,
        wire_wide: bool | None = None,
    ) -> None:
        # Initial value is the floor (effectively silent) so the daemon
        # cannot accidentally play TTS loud during the brief window
        # between construction and the first configured gain. Until
        # then we'd rather have inaudible TTS than blast.
        self._gain_db = self.MIN_TTS_GAIN_DB
        # Cumulative pacing-sleep time since the last take_paced_sec().
        self._paced_total_sec = 0.0
        self._stream: _OutputdStreamAdapter | None = None
        # One-shot latch so a write() before __aenter__ (no stream yet) is
        # audible in the journal instead of a silent no-op.
        self._closed_stream_warned = False
        # Drain tracking — see `expected_drain_at`. None (not 0.0)
        # because CLOCK_MONOTONIC's reference is platform-defined; 0.0
        # is briefly a legitimate now() value on a freshly-booted Pi.
        self._drain_tail_sec = float(drain_tail_sec)
        self._ring_end_monotonic: float | None = None
        # Emission-time admission authority — see set_emission_admission.
        self._emission_admission: "Callable[[], str | None] | None" = None
        self._emission_refusal_logged = False
        self.set_gain_db(gain_db)
        self._socket_path = socket_path
        self._provider = provider
        self._model = model
        self._voice = voice
        self._profile_path = profile_path
        # Resolved ONCE, at construction: `jasper-voice` is restarted by every
        # deploy and by the wizards that could change this, and a per-write file
        # read would put an open() on the audio path. A coupling flip that
        # changes the answer restarts this daemon (`coupling_reconcile`), so the
        # window in which this value can be stale is bounded by that restart;
        # fan-in logs `event=fanin.tts_wire_width_mismatch` if a payload lands
        # inside it.
        self._wire_wide = tts_wire_is_wide() if wire_wide is None else wire_wide
        self._frame_bytes = (
            _OUTPUTD_AUDIO_FRAME_BYTES_WIDE
            if self._wire_wide
            else _OUTPUTD_AUDIO_FRAME_BYTES
        )
        # One line naming the resolved width and where it came from, paired
        # with fan-in's own resolved line so a support read can compare the two.
        log_event(
            logger,
            "tts_wire.resolved",
            width="S32_LE" if self._wire_wide else "S16_LE",
            verb=wire.TTS_AUDIO_WIDE if self._wire_wide else wire.TTS_AUDIO_NARROW,
            frame_bytes=self._frame_bytes,
            source="explicit" if wire_wide is not None else "box_declaration",
            socket=socket_path,
        )
        self._assistant_meter: AssistantSourceMeter | None = None
        self._profile_cache_key: tuple[str, str, str, str] | None = None
        self._profile_cache = None
        # Keeps references so scheduled profile-save tasks (see
        # _schedule_assistant_source_profile_save) aren't garbage-collected
        # mid-flight.
        self._profile_save_tasks: set[asyncio.Task] = set()
        # One publisher owns reconnect. Without this lock, simultaneous meter
        # and audio callers can each connect after the same poisoned adapter
        # and leave one live but unreachable socket behind.
        self._outputd_reconnect_lock = asyncio.Lock()

    @property
    def gain_db(self) -> float:
        return self._gain_db

    def set_emission_admission(
        self,
        admission: "Callable[[], str | None] | None",
    ) -> None:
        """Install the authority asked before every write.

        `admission` returns a refusal code while assistant audio must not
        be heard at all (an armed room-correction window), else None. It is
        asked per write, not per episode, so a caller that passed an earlier
        check and is already mid-playout is refused too (issue #1913)."""
        self._emission_admission = admission

    async def write_segment(
        self,
        pcm: bytes,
        *,
        provider_item_id: str | None = None,
        segment_kind: str = "assistant",
        source_profile=None,
        pcm_wide: bool = False,
        on_first_write: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Return whether PCM reached the transport. Observe the first accepted
        chunk even if a later chunk fails or the write is cancelled.
        """
        admission = self._emission_admission
        refusal = admission() if admission is not None else None
        if refusal is not None:
            # Once per refusal streak: a burst-delivery provider hands over a
            # whole response as many chunks, and one line each would flood the
            # journal for the length of a held measurement session.
            if not self._emission_refusal_logged:
                self._emission_refusal_logged = True
                log_event(
                    logger,
                    "tts_write.refused",
                    reason=refusal,
                    segment_kind=segment_kind,
                )
            return False
        self._emission_refusal_logged = False
        return await self._write_segment(
            pcm,
            provider_item_id=provider_item_id,
            segment_kind=segment_kind,
            source_profile=source_profile,
            pcm_wide=pcm_wide,
            on_first_write=on_first_write,
        )

    def expected_drain_at(self) -> float:
        """Monotonic deadline at which the last-queued sample's tail
        will have cleared the OS audio stack — i.e. the speaker is
        silent. Returns ``0.0`` when nothing is queued (the sentinel
        naturally compares as "already drained" against
        ``time.monotonic()``)."""
        if self._ring_end_monotonic is None:
            return 0.0
        return self._ring_end_monotonic + self._drain_tail_sec

    async def wait_drained(self) -> None:
        """Block until ``expected_drain_at`` has passed. Cheap when
        nothing is queued (the 0.0 sentinel yields negative remaining,
        which skips the sleep). Single ``asyncio.sleep`` otherwise —
        deadline is known up-front, no polling."""
        remaining = self.expected_drain_at() - time.monotonic()
        if remaining > 0.0:
            await asyncio.sleep(remaining)

    def take_paced_sec(self) -> float:
        """Pacing-sleep seconds accumulated since the last call; resets.

        The voice daemon reads this once per turn for the turn-ended
        accounting line.
        """
        v = self._paced_total_sec
        self._paced_total_sec = 0.0
        return v

    async def _connect_stream_adapter(
        self,
    ) -> _OutputdStreamAdapter:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_OUTPUTD_IPC_CONNECT_TIMEOUT_SEC)
        connect_task = asyncio.create_task(
            asyncio.to_thread(sock.connect, self._socket_path)
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(connect_task),
                timeout=_OUTPUTD_IPC_CONNECT_TIMEOUT_SEC,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            # The thread is bounded by the socket timeout too, but it may
            # finish after this coroutine returns. Consume that late outcome.
            connect_task.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
            if isinstance(e, asyncio.CancelledError):
                raise
            log_event(
                logger,
                "tts_fanin.connect_timeout",
                socket=self._socket_path,
                timeout_sec=_OUTPUTD_IPC_CONNECT_TIMEOUT_SEC,
                level=logging.WARNING,
            )
            raise TimeoutError(
                "TTS IPC connect timed out after "
                f"{_OUTPUTD_IPC_CONNECT_TIMEOUT_SEC:.1f}s"
            ) from None
        except Exception as e:  # noqa: BLE001
            sock.close()
            logger.error(
                "fan-in TTS IPC connect failed: socket=%s exc=%s: %s",
                self._socket_path, type(e).__name__, e,
            )
            raise
        stream = _OutputdStreamAdapter(sock, wire_wide=self._wire_wide)
        try:
            stream.set_gain_db(self.gain_db)
        except OSError:
            stream.close()
            raise
        logger.info("fan-in TTS IPC connected: socket=%s", self._socket_path)
        return stream

    async def __aenter__(self) -> "TtsPlayout":
        self._stream = await self._connect_stream_adapter()
        return self

    async def _current_outputd_stream(self):
        stream = self._stream
        if isinstance(stream, _OutputdStreamAdapter) and stream.closed:
            async with self._outputd_reconnect_lock:
                # Another waiter may have published the replacement while we
                # queued for the reconnect lock. Re-read inside ownership so
                # every caller shares that adapter and no loser socket exists.
                stream = self._stream
                if not (
                    isinstance(stream, _OutputdStreamAdapter)
                    and stream.closed
                ):
                    return stream
                log_event(
                    logger,
                    "tts_fanin.reconnect",
                    reason="closed_socket",
                    socket=self._socket_path,
                )
                try:
                    stream = await self._connect_stream_adapter()
                except Exception as e:  # noqa: BLE001
                    log_event(
                        logger,
                        "tts_fanin.reconnect_failed",
                        reason="closed_socket",
                        socket=self._socket_path,
                        exc_type=type(e).__name__,
                        err=str(e),
                        level=logging.WARNING,
                    )
                    return None
                self._stream = stream
        return stream

    def set_gain_db(self, db: float) -> None:
        """Update TTS gain and push the wire-level value into the stream.

        Non-finite inputs are rejected and very low finite values floor
        to the mute-equivalent minimum. Single-float assignment is atomic
        under the GIL, so no lock is needed for concurrent reads of
        `gain_db`. The stream push below runs regardless of whether the
        clamp step above actually changed anything, so a rejected input
        still re-syncs the stream to the (unchanged) active gain.
        """
        try:
            parsed = float(db)
        except (TypeError, ValueError):
            logger.warning("tts gain rejected (not a number): %r", db)
        else:
            if parsed != parsed or parsed in (float("inf"), float("-inf")):
                logger.warning("tts gain rejected (non-finite): %r", db)
            else:
                clamped = max(self.MIN_TTS_GAIN_DB, parsed)
                if clamped != self._gain_db:
                    self._gain_db = clamped
                    # DEBUG (not INFO): the active TTS IPC owner publishes
                    # the richer assistant loudness decision telemetry, and
                    # this low-level floor log is noisy.
                    if clamped != parsed:
                        logger.debug(
                            "tts gain set: requested %.1f dB -> floored to "
                            "%.1f dB",
                            parsed, clamped,
                        )
                    else:
                        logger.debug("tts gain set: %.1f dB", clamped)
        stream = self._stream
        if isinstance(stream, _OutputdStreamAdapter) and stream.closed:
            return
        if stream is not None and hasattr(stream, "set_gain_db"):
            try:
                stream.set_gain_db(self.gain_db)
            except OSError as e:
                logger.warning("fan-in TTS IPC gain update failed: %s", e)

    async def program_duck(self, on: bool) -> bool:
        """Switch fan-in's program duck on/off over this playout's connection.

        Fan-in owns the duck depth; this only asks for the state. Goes through
        the same reconnect path as every other command, so the first turn
        after a fan-in restart ducks rather than playing over undimmed music.
        Returns False when there is no live connection to ask on or the ask
        failed, so the caller can own its own restore.
        """
        # A silent duck failure means music does not step back under the
        # assistant, so every path out of here says why.
        def failed(reason: str, **fields: str) -> bool:
            log_event(
                logger,
                "voice.duck_failed",
                on=str(bool(on)).lower(),
                reason=reason,
                level=logging.WARNING,
                **fields,
            )
            return False

        stream = await self._current_outputd_stream()
        duck = getattr(stream, "program_duck", None)
        if duck is None:
            return failed("no_connection")
        try:
            await asyncio.to_thread(duck, on)
        except OSError as e:
            return failed("send", detail=str(e))
        return True

    async def prepare_assistant_context(
        self,
        *,
        provider: str,
        model: str,
        voice: str,
        tts_envelope_lufs: float,
        canonical_volume_db: float | None = None,
        downstream_volume_db: float | None = None,
        context_tts_envelope_lufs: float | None = None,
        muted: bool | None = None,
        context_stamp_boot_ns: int | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._voice = voice
        for attempt in range(2):
            stream = await self._current_outputd_stream()
            if stream is None:
                return
            prepare = getattr(stream, "prepare_assistant", None)
            if prepare is None:
                return
            try:
                prepare_kwargs = {
                    "provider": provider,
                    "model": model,
                    "voice": voice,
                    "tts_envelope_lufs": tts_envelope_lufs,
                }
                if (
                    canonical_volume_db is not None
                    and downstream_volume_db is not None
                    and context_tts_envelope_lufs is not None
                    and muted is not None
                    and context_stamp_boot_ns is not None
                ):
                    prepare_kwargs["volume_context"] = EffectiveVolumeContext(
                        canonical_db=canonical_volume_db,
                        downstream_db=downstream_volume_db,
                        tts_envelope_lufs=context_tts_envelope_lufs,
                        muted=muted,
                        stamp_boot_ns=context_stamp_boot_ns,
                    )
                await asyncio.to_thread(
                    prepare,
                    **prepare_kwargs,
                )
                return
            except OSError as e:
                if (
                    attempt == 0
                    and isinstance(stream, _OutputdStreamAdapter)
                    and stream.closed
                    and not isinstance(e, TimeoutError)
                ):
                    log_event(
                        logger,
                        "tts_fanin.control_retry",
                        method="prepare_assistant",
                        reason="closed_socket",
                        exc_type=type(e).__name__,
                        err=str(e),
                    )
                    continue
                logger.warning("fan-in TTS IPC prepare assistant failed: %s", e)
                return

    async def pause_content_meter(self) -> None:
        await self._send_meter_control("pause_content_meter")

    async def pause_content_meter_for_measurement(
        self,
        deadline_monotonic: float,
    ) -> None:
        """Fail-closed meter pause that cannot outlive MEASURE_PAUSE.

        Do not reconnect here: isolation setup must prove the command landed
        on the canonical adapter it already owns. A poisoned/missing adapter
        rolls the window back; ordinary later access owns reconnection.
        """

        stream = self._stream
        if not isinstance(stream, _OutputdStreamAdapter) or stream.closed:
            raise OSError("canonical TTS IPC adapter unavailable")
        control_deadline = min(
            deadline_monotonic,
            time.monotonic() + _OUTPUTD_MEASUREMENT_CONTROL_SLICE_SEC,
        )
        # Deliberately synchronous: the bounded adapter critical section may
        # hold the event loop for at most 250 ms, and no worker can emit PAUSE
        # after this coroutine reports failure and voice reopens admission.
        stream.pause_content_meter(deadline_monotonic=control_deadline)

    async def resume_content_meter(self) -> None:
        await self._send_meter_control("resume_content_meter")

    async def _send_meter_control(self, method: str) -> None:
        for attempt in range(2):
            stream = await self._current_outputd_stream()
            if stream is None:
                return
            fn = getattr(stream, method, None)
            if fn is None:
                return
            try:
                await asyncio.to_thread(fn)
                return
            except OSError as e:
                if (
                    attempt == 0
                    and isinstance(stream, _OutputdStreamAdapter)
                    and stream.closed
                    and not isinstance(e, TimeoutError)
                ):
                    log_event(
                        logger,
                        "tts_fanin.control_retry",
                        method=method,
                        reason="closed_socket",
                        exc_type=type(e).__name__,
                        err=str(e),
                    )
                    continue
                logger.warning("fan-in TTS IPC %s failed: %s", method, e)
                return

    async def write(self, pcm: bytes) -> None:
        await self.write_segment(pcm)

    async def _write_segment(
        self,
        pcm: bytes,
        *,
        provider_item_id: str | None = None,
        segment_kind: str = "assistant",
        source_profile=None,
        pcm_wide: bool = False,
        on_first_write: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Send un-gained 48 kHz stereo PCM to the TTS IPC owner.

        Gain is sent as metadata and enforced by fan-in's final mix
        clamp. Drain accounting mirrors TtsPlayout.write so the voice
        daemon's turn-ending contract stays identical.

        ``pcm`` is 24 kHz mono. ``pcm_wide`` names its INPUT width, which is a
        per-caller fact rather than a per-box one: provider TTS is S16 from
        every supported API whatever this box's wire is, while a locally
        generated earcon is baked at the wire's own width (see
        ``jasper.voice.earcons._to_pcm32``). A wide input is normalized to i16
        sample units on the way in — an exact power-of-two divide — so
        everything downstream of this line is one code path at one scale.
        """
        if not pcm:
            return False
        if self._stream is None:
            if not self._closed_stream_warned:
                logger.warning(
                    "TtsPlayout.write called on a closed stream - "
                    "%d bytes silently dropped. Did you forget "
                    "`async with tts:`? (Suppressing further such "
                    "warnings for this instance.)",
                    len(pcm),
                )
                self._closed_stream_warned = True
            return False
        stream = await self._current_outputd_stream()
        if stream is None:
            return False

        if pcm_wide:
            # /2^16 is exact in binary floating point (it changes the exponent
            # only), so this costs nothing beyond the float32 mantissa the
            # whole path already runs at.
            arr = np.frombuffer(pcm, dtype=np.int32).astype(np.float32)
            arr = arr / np.float32(_SPINE_SCALE)
        else:
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if (
            segment_kind == "assistant"
            and self._provider
            and self._model
            and self._voice
        ):
            if self._assistant_meter is None:
                self._assistant_meter = AssistantSourceMeter()
            self._assistant_meter.observe_pcm_24k(pcm)
        # The wire is fixed at 48 kHz; provider/cue PCM is always 24 kHz, so
        # this upsample ratio is always exactly 2.
        arr = upsample_2x(arr).astype(np.float32, copy=False)
        mono = _quantize_to_wire(arr, wide=self._wire_wide)
        stereo = np.repeat(mono, 2)

        chunk_duration_sec = len(mono) / _OUTPUTD_SAMPLE_RATE
        write_start = time.monotonic()
        for attempt in range(2):
            try:
                if hasattr(stream, "set_gain_db"):
                    await _outputd_io(stream, "set_gain_db", self.gain_db)
                if hasattr(stream, "start_segment"):
                    profile = self._profile_for_segment(
                        segment_kind, source_profile=source_profile,
                    )
                    await _outputd_io(
                        stream, "start_segment",
                        kind=segment_kind,
                        provider_item_id=provider_item_id,
                        profile=profile,
                    )
                break
            except OSError as e:
                if (
                    attempt == 0
                    and isinstance(stream, _OutputdStreamAdapter)
                    and stream.closed
                    and not isinstance(e, TimeoutError)
                ):
                    log_event(
                        logger,
                        "tts_fanin.segment_setup_retry",
                        reason="closed_socket",
                        exc_type=type(e).__name__,
                        err=str(e),
                    )
                    stream = await self._current_outputd_stream()
                    if stream is None:
                        return False
                    continue
                raise
        paced_sec = 0.0
        accepted = False

        async def commit_chunk() -> None:
            nonlocal accepted
            sent_at = time.monotonic()
            committed_end = max(self._ring_end_monotonic or sent_at, sent_at)
            self._ring_end_monotonic = committed_end + len(chunk) / (
                _OUTPUTD_SAMPLE_RATE * self._frame_bytes
            )
            if not accepted:
                accepted = True
                if on_first_write is not None:
                    try:
                        await on_first_write()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("TTS acceptance observer failed: %s", e)

        for chunk in _outputd_audio_chunks(stereo.tobytes(), self._frame_bytes):
            now = time.monotonic()
            pace_excess = (self._ring_end_monotonic or now) - now - _OUTPUTD_PACE_AHEAD_SEC
            if pace_excess > 0:
                await _pace_sleep(pace_excess)
                paced_sec += pace_excess
                self._paced_total_sec += pace_excess
            try:
                await _outputd_io(stream, "write", chunk, on_accepted=commit_chunk)
            except OSError:
                if isinstance(stream, _OutputdStreamAdapter) and stream.closed:
                    log_event(
                        logger,
                        "tts_fanin.audio_write_failed",
                        reason="closed_socket",
                        level=logging.WARNING,
                    )
                raise
        queued_at = time.monotonic()
        # Exclude deliberate pacing sleeps so the warning keeps meaning
        # "the IPC itself is slow", not "the writer paced as designed".
        write_ms = (queued_at - write_start) * 1000 - paced_sec * 1000
        chunk_ms = chunk_duration_sec * 1000
        if write_ms > chunk_ms + 100:
            logger.warning(
                "fan-in TTS IPC write slow: %.0fms for %.0fms of audio "
                "(%d frames @ %d Hz)",
                write_ms, chunk_ms, len(mono), _OUTPUTD_SAMPLE_RATE,
            )
        return accepted

    def _profile_for_segment(self, segment_kind: str, *, source_profile=None):
        if source_profile is not None:
            return source_profile
        if (
            segment_kind == "chirp"
            or not (self._provider and self._model and self._voice)
        ):
            return None
        key = (self._provider, self._model, self._voice, self._profile_path)
        if self._profile_cache_key != key:
            self._profile_cache_key = key
            self._profile_cache = profile_for_outputd(
                self._provider,
                self._model,
                self._voice,
                path=self._profile_path,
            )
        return self._profile_cache

    async def end_segment(self) -> None:
        stream = self._stream
        if stream is None:
            self._schedule_assistant_source_profile_save()
            return
        if isinstance(stream, _OutputdStreamAdapter) and stream.closed:
            self._schedule_assistant_source_profile_save()
            return
        end = getattr(stream, "end_segment", None)
        if end is not None:
            try:
                await _outputd_io(stream, "end_segment")
            except OSError as e:
                logger.warning("fan-in TTS IPC segment end failed: %s", e)
        self._schedule_assistant_source_profile_save()

    def _pop_assistant_meter(self) -> AssistantSourceMeter | None:
        meter = self._assistant_meter
        self._assistant_meter = None
        return meter

    def _schedule_assistant_source_profile_save(self) -> None:
        """Run the profile save off the chirp's critical path.

        ``meter.finish()`` runs a pure-Python per-sample IIR filter twice
        over the reply audio (assistant_loudness.py's ``_biquad``); awaited
        inline here it blocked the loop for ~0.7s per second of reply,
        delaying the end-of-turn chirp. The meter is popped now, by value,
        so a segment that starts before this task gets to run cannot steal
        it from the segment that just ended.
        """
        meter = self._pop_assistant_meter()
        task = asyncio.create_task(self._save_assistant_source_profile(meter))
        self._profile_save_tasks.add(task)
        task.add_done_callback(self._profile_save_tasks.discard)

    async def _save_assistant_source_profile(
        self, meter: AssistantSourceMeter | None
    ) -> None:
        if meter is None or not (self._provider and self._model and self._voice):
            return
        measurement = await asyncio.to_thread(meter.finish)
        if measurement is None:
            return
        confidence = confidence_for_measurement(measurement)
        try:
            await asyncio.to_thread(
                update_profile_from_measurement,
                self._provider,
                self._model,
                self._voice,
                measurement,
                path=self._profile_path,
                method="passive_live",
                confidence=confidence,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("assistant loudness profile save failed: %s", e)
        else:
            self._profile_cache_key = None
            self._profile_cache = None

    async def flush(self) -> dict | None:
        stream = await self._current_outputd_stream()
        if stream is None:
            await self._save_assistant_source_profile(self._pop_assistant_meter())
            return None
        ack: dict | None = None
        try:
            flush_sync = getattr(stream, "flush_sync", None)
            if flush_sync is not None:
                ack = await _outputd_io(stream, "flush_sync")
            else:
                await _outputd_io(stream, "abort")
                await _outputd_io(stream, "start")
        except Exception as e:  # noqa: BLE001
            logger.warning("fan-in TTS IPC flush failed: %s", e)
        if confirmed_tts_flush(ack):
            self._ring_end_monotonic = None
            log_event(
                logger,
                "tts_flush.ack",
                transport="fanin",
                ok=ack.get("ok"),
                segments=ack.get("segments"),
                flushed_frames=ack.get("flushed_frames"),
                max_audio_played_ms=ack.get("max_audio_played_ms"),
            )
        else:
            ack = None
        self._schedule_assistant_source_profile_save()
        return ack

    async def __aexit__(self, *exc) -> None:
        if self._stream is not None:
            stream = self._stream
            self._stream = None
            close = getattr(stream, "close", None)
            if close is not None:
                await asyncio.to_thread(close)
